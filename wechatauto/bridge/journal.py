"""桥接收发日志：私有 SQLite、显式 ACK，以及发送结果不明时禁止重试。"""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading


MAX_EVENT_BYTES = 512 * 1024


class ConflictError(ValueError):
    """相同幂等键对应了不同内容。"""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _identifier(value):
    if not isinstance(value, str) or not value or len(value) > 256 or "\x00" in value:
        raise ValueError("invalid event or client identifier")
    return value


class Journal:
    """使用调用方所有的 0700 目录，数据库固定为 journal.sqlite3（0600）。

    enqueue_inbound(event_id, payload) 持久化一个新事件，返回其递增序号；相同键/内容无副作用。
    unacked(after=0, limit=100) 按 seq 分页返回 {seq,event_id,payload}，只返回未确认事件。
    ack(event_id) 在事务中确认事件；重连从 after=0 重新投递未 ACK 数据。
    claim_outbox(client_id, request) 原子登记请求并占用发送权；claimed=True 才允许执行 GUI。
    finish_outbox(client_id, receipt) 记录已确认回执；mark_uncertain(client_id) 禁止自动重发。
    get_outbox(client_id) 返回状态供运维确认，close() 关闭连接。

    只能由一个活动桥接进程持有本日志。重新打开时将中断的 sending 转为 uncertain。
    日志不会自行读取历史或丢弃未确认事件，数据保留策略由部署方显式管理。
    """

    def __init__(self, directory):
        self.directory = Path(directory).absolute()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._check_private(self.directory, directory=True)
        self.path = self.directory / "journal.sqlite3"
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise PermissionError("journal database must be a private regular file") from exc
        os.close(descriptor)
        self._check_private(self.path, directory=False)
        lock_path = self.directory / "journal.lock"
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise PermissionError("journal lock must be a private regular file") from exc
        try:
            self._check_private(lock_path, directory=False)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(descriptor)
            raise RuntimeError("journal is already held by another bridge") from exc
        except Exception:
            os.close(descriptor)
            raise
        self._descriptor = descriptor
        self._lock = threading.RLock()
        self._db = None
        try:
            self._initialize()
        except BaseException:
            if self._db is not None:
                self._db.close()
            os.close(self._descriptor)
            self._descriptor = None
            raise

    def _initialize(self):
        """持锁后建表并恢复中断状态，失败由构造器释放文件句柄。"""
        self._db = sqlite3.connect(self.path, check_same_thread=False, timeout=5)
        self._db.row_factory = sqlite3.Row
        with self._db:
            self._db.execute("PRAGMA journal_mode=DELETE")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS inbox (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,
                    acked INTEGER NOT NULL DEFAULT 0 CHECK (acked IN (0,1))
                );
                CREATE INDEX IF NOT EXISTS inbox_pending ON inbox(seq) WHERE acked=0;
                CREATE TABLE IF NOT EXISTS outbox (
                    client_id TEXT PRIMARY KEY,
                    request_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending','sending','sent','uncertain')),
                    receipt TEXT
                );
                CREATE TABLE IF NOT EXISTS identity (name TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cursors (
                    chat TEXT NOT NULL, shard TEXT NOT NULL, local_id INTEGER NOT NULL,
                    PRIMARY KEY(chat,shard)
                );
                CREATE TABLE IF NOT EXISTS sent_messages (
                    chat TEXT NOT NULL, shard TEXT NOT NULL, local_id INTEGER NOT NULL,
                    PRIMARY KEY(chat,shard,local_id)
                );
            """)
            self._db.execute("UPDATE outbox SET status='uncertain' WHERE status='sending'")

    @staticmethod
    def _check_private(path, directory):
        info = path.lstat()
        valid_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        expected_mode = 0o700 if directory else 0o600
        if (not valid_type or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != expected_mode or (not directory and info.st_nlink != 1)):
            raise PermissionError("journal path must be caller-owned and private")

    def enqueue_inbound(self, event_id, payload):
        """持久化显式传入的新消息，不扫描历史；超大事件和键冲突会拒绝。"""
        _identifier(event_id)
        if not isinstance(payload, dict):
            raise ValueError("event payload must be an object")
        normalized = dict(payload, _bridge_event_id=event_id)
        encoded = _json(normalized)
        if len(encoded.encode("utf-8")) > MAX_EVENT_BYTES:
            raise ValueError("inbound event exceeds the size limit")
        with self._lock, self._db:
            return self._insert_inbound(event_id, encoded)

    def _insert_inbound(self, event_id, encoded):
        """复用调用方的事务，避免事件提交先于水位提交。"""
        row = self._db.execute("SELECT seq,payload FROM inbox WHERE event_id=?", (event_id,)).fetchone()
        if row:
            if row["payload"] != encoded:
                raise ConflictError("event identifier was reused with different content")
            return row["seq"]
        return self._db.execute("INSERT INTO inbox(event_id,payload) VALUES (?,?)", (event_id, encoded)).lastrowid

    def bind_identity(self, value):
        """把日志绑定到单个源账号命名空间，禁止重用其他账号的状态。"""
        _identifier(value)
        with self._lock, self._db:
            row = self._db.execute("SELECT value FROM identity WHERE name='source'").fetchone()
            if row is not None and row[0] != value:
                raise ConflictError("journal belongs to another source account")
            self._db.execute("INSERT INTO identity VALUES('source',?) ON CONFLICT DO NOTHING", (value,))

    @staticmethod
    def _position(chat, shard, local_id):
        _identifier(chat)
        _identifier(shard)
        if isinstance(local_id, bool) or not isinstance(local_id, int) or local_id < 0:
            raise ValueError("invalid native message position")

    def get_cursor(self, chat, shard):
        """返回已持久化的 local_id；None 表示该会话/分片尚未建立历史基线。"""
        with self._lock:
            row = self._db.execute("SELECT local_id FROM cursors WHERE chat=? AND shard=?", (chat, shard)).fetchone()
        return row[0] if row is not None else None

    def record_observation(self, chat, shard, local_id, payload=None):
        """事件和单调 local_id 水位原子提交；payload=None 用于跳过或建立历史基线。"""
        self._position(chat, shard, local_id)
        encoded = event_id = None
        if payload is not None:
            if not isinstance(payload, dict):
                raise ValueError("event payload must be an object")
            event_id = _identifier(payload.get("_bridge_event_id"))
            encoded = _json(payload)
            if len(encoded.encode("utf-8")) > MAX_EVENT_BYTES:
                raise ValueError("inbound event exceeds the size limit")
        with self._lock, self._db:
            if encoded is not None:
                self._insert_inbound(event_id, encoded)
            self._db.execute(
                "INSERT INTO cursors VALUES(?,?,?) ON CONFLICT(chat,shard) DO UPDATE "
                "SET local_id=MAX(cursors.local_id,excluded.local_id)", (chat, shard, local_id))

    def suppress_sent(self, chat, shard, local_id):
        """保存本桥接已确认的发送位置，轮询器据此排除回声。"""
        self._position(chat, shard, local_id)
        with self._lock, self._db:
            self._db.execute("INSERT INTO sent_messages VALUES(?,?,?) ON CONFLICT DO NOTHING", (chat, shard, local_id))

    def is_suppressed(self, chat, shard, local_id):
        """精确按会话和分片查询，不能把另一分片的同号行当作本次发送。"""
        with self._lock:
            return self._db.execute("SELECT 1 FROM sent_messages WHERE chat=? AND shard=? AND local_id=?",
                                    (chat, shard, local_id)).fetchone() is not None

    def unacked(self, after=0, limit=100):
        """以递增 seq 作为游标，单页最多 500 项；不加载全部消息。"""
        if not isinstance(after, int) or after < 0 or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("invalid journal page")
        with self._lock:
            rows = self._db.execute(
                "SELECT seq,event_id,payload FROM inbox WHERE acked=0 AND seq>? ORDER BY seq LIMIT ?",
                (after, limit)).fetchall()
        return [{"seq": row["seq"], "event_id": row["event_id"], "payload": json.loads(row["payload"])}
                for row in rows]

    def ack(self, event_id):
        """确认事件；不存在时返回 False，不根据连接顺序隐式确认。"""
        _identifier(event_id)
        with self._lock, self._db:
            return self._db.execute("UPDATE inbox SET acked=1 WHERE event_id=?", (event_id,)).rowcount > 0

    def claim_outbox(self, client_id, request):
        """比较规范化请求哈希并原子领取发送权；重复的 sending/uncertain 永不再次领取。"""
        _identifier(client_id)
        request_hash = hashlib.sha256(_json(request).encode("utf-8")).hexdigest()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO outbox(client_id,request_hash,status) VALUES (?,?,'pending') ON CONFLICT DO NOTHING",
                (client_id, request_hash))
            row = self._db.execute("SELECT * FROM outbox WHERE client_id=?", (client_id,)).fetchone()
            if row["request_hash"] != request_hash:
                raise ConflictError("ClientMsgID was reused with a different request")
            claimed = row["status"] == "pending"
            if claimed:
                self._db.execute("UPDATE outbox SET status='sending' WHERE client_id=?", (client_id,))
            return {"claimed": claimed, "status": "sending" if claimed else row["status"],
                    "receipt": json.loads(row["receipt"]) if row["receipt"] else None}

    def finish_outbox(self, client_id, receipt):
        """仅允许已领取的请求落为 sent，回执必须明确包含 confirmed=True。"""
        if not isinstance(receipt, dict) or receipt.get("confirmed") is not True:
            raise ValueError("send receipt is not confirmed")
        with self._lock, self._db:
            updated = self._db.execute(
                "UPDATE outbox SET status='sent',receipt=? WHERE client_id=? AND status='sending'",
                (_json(receipt), client_id)).rowcount
            if not updated:
                raise ValueError("send is not in sending state")

    def mark_uncertain(self, client_id):
        """GUI 调用报错或无确认时只标记，不尝试猜测是否发送成功。"""
        with self._lock, self._db:
            self._db.execute("UPDATE outbox SET status='uncertain' WHERE client_id=? AND status='sending'", (client_id,))

    def get_outbox(self, client_id):
        """查询一个请求，未登记时返回 None；不返回消息正文。"""
        with self._lock:
            row = self._db.execute("SELECT * FROM outbox WHERE client_id=?", (client_id,)).fetchone()
        if row is None:
            return None
        return {"client_id": row["client_id"], "status": row["status"], "request_hash": row["request_hash"],
                "receipt": json.loads(row["receipt"]) if row["receipt"] else None}

    def counts(self):
        """仅用于健康检查的匿名队列计数。"""
        with self._lock:
            inbox = self._db.execute("SELECT COUNT(*) FROM inbox WHERE acked=0").fetchone()[0]
            outbox = dict(self._db.execute("SELECT status,COUNT(*) FROM outbox GROUP BY status").fetchall())
        return {"inbox_unacked": inbox, "outbox": outbox}

    def close(self):
        """关闭连接；调用方应先停止轮询器和 HTTP 服务。"""
        with self._lock:
            self._db.close()
            if self._descriptor is not None:
                os.close(self._descriptor)
                self._descriptor = None
