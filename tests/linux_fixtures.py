"""用 SQLite C API 创建带保留区的真实数据库，再按 SQLCipher 页格式加密。"""

import ctypes
import _sqlite3
from contextlib import closing
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PAGE = 4096
RESERVE = 80
CHAT = "fixture@chatroom"
MASTER = bytes(range(32))


def plain_database(path, script):
    """建表前调用 SQLITE_FCNTL_RESERVE_BYTES（38），不修改 SQLite 文件头。"""
    lib = ctypes.CDLL(_sqlite3.__file__)
    lib.sqlite3_open.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
    lib.sqlite3_file_control.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
    lib.sqlite3_exec.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p,
                               ctypes.c_void_p, ctypes.POINTER(ctypes.c_char_p)]
    lib.sqlite3_close.argtypes = [ctypes.c_void_p]
    handle = ctypes.c_void_p()
    assert lib.sqlite3_open(str(path).encode(), ctypes.byref(handle)) == 0
    try:
        reserve = ctypes.c_int(RESERVE)
        assert lib.sqlite3_file_control(handle, None, 38, ctypes.byref(reserve)) == 0
        error = ctypes.c_char_p()
        code = lib.sqlite3_exec(handle, ("PRAGMA page_size=4096;" + script).encode(),
                                None, None, ctypes.byref(error))
        assert code == 0, error.value
    finally:
        lib.sqlite3_close(handle)
    raw = Path(path).read_bytes()
    assert raw[20] == RESERVE
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    return raw


def encrypt_pages(raw, key, salt, explicit_salt=False):
    result = bytearray()
    mac_key = hashlib.pbkdf2_hmac("sha512", key, bytes(x ^ 0x3A for x in salt), 2, 32)
    for index in range(0, len(raw), PAGE):
        pgno = index // PAGE + 1
        page = raw[index:index + PAGE]
        start = 16 if pgno == 1 else 0
        iv = hashlib.sha256(salt + struct.pack("<I", pgno)).digest()[:16]
        enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        payload = enc.update(page[start:PAGE - RESERVE]) + enc.finalize()
        prefix = (page[:16] if explicit_salt else salt) if pgno == 1 else b""
        body = prefix + payload + iv
        digest = hmac.new(mac_key, body[start:] + struct.pack("<I", pgno), hashlib.sha512).digest()
        result.extend(body + digest)
    return bytes(result)


class DatabaseFixture:
    def __init__(self, base, account="wxid_fixture_abcd", master=False, explicit_salt=False):
        self.base = Path(base)
        self.root = self.base / "xwechat_files"
        self.account = account
        self.storage = self.root / account / "db_storage"
        self.storage.mkdir(parents=True)
        self.keys = {}
        self.master = master
        self.explicit_salt = explicit_salt
        self.keys_file = self.base / (account + "-keys.json")
        self.add_contact()
        self.add_message(0, "wxid_alice", 1, "hello")
        self.write_keys()

    def add_database(self, rel, script):
        target = self.storage / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        plain = self.base / (self.account + "-" + rel.replace("/", "_") + ".plain")
        raw = plain_database(plain, script)
        salt = hashlib.sha256((self.account + rel).encode()).digest()[:16]
        key = (hashlib.pbkdf2_hmac("sha512", MASTER, salt, 256000, 32) if self.master
               else hashlib.sha256(("key:" + self.account + rel).encode()).digest())
        target.write_bytes(encrypt_pages(raw, key, salt, self.explicit_salt))
        self.keys[rel] = (key + salt if self.explicit_salt else key).hex()
        return target, raw, key, salt

    def add_contact(self):
        self.add_database("contact/contact.db", """
            CREATE TABLE contact(id INTEGER PRIMARY KEY, username TEXT, nick_name TEXT, remark TEXT, alias TEXT);
            INSERT INTO contact VALUES (1,'wxid_alice','Alice','friend',''),(2,'wxid_bob','Bob','',''),
              (3,'fixture@chatroom','Fixture Group','',''),(4,'wxid_fixture','Me','','');
            CREATE TABLE chat_room(id INTEGER PRIMARY KEY,username TEXT,owner TEXT);
            INSERT INTO chat_room VALUES(1,'fixture@chatroom','wxid_alice');
            CREATE TABLE chatroom_member(room_id INTEGER,member_id INTEGER);
            INSERT INTO chatroom_member VALUES(1,1),(1,2);
        """)

    def add_message(self, shard, sender, seq, content):
        table = "Msg_" + hashlib.md5(CHAT.encode()).hexdigest()
        quote = lambda s: "'" + s.replace("'", "''") + "'"
        return self.add_database("message/message_%d.db" % shard, """
            CREATE TABLE Name2Id(user_name TEXT);
            INSERT INTO Name2Id VALUES(%s);
            CREATE TABLE %s(local_id INTEGER,local_type INTEGER,real_sender_id INTEGER,
              create_time INTEGER,message_content TEXT,source BLOB,packed_info_data BLOB,
              compress_content BLOB,server_id INTEGER,sort_seq INTEGER);
            INSERT INTO %s VALUES(1,1,1,1700000000,%s,NULL,NULL,NULL,100,%d);
        """ % (quote(sender), table, table, quote(content), seq))

    def append_wal(self, shard, content):
        """把真实 SQLite 更新后的页面编码成 WCDB WAL，并追加旧世代干扰帧。"""
        rel = "message/message_%d.db" % shard
        plain = self.base / (self.account + "-" + rel.replace("/", "_") + ".plain")
        table = "Msg_" + hashlib.md5(CHAT.encode()).hexdigest()
        with closing(sqlite3.connect(plain)) as conn:
            conn.execute("UPDATE %s SET message_content=?,sort_seq=2" % table, (content,))
            conn.commit()
        raw = plain.read_bytes()
        salt = hashlib.sha256((self.account + rel).encode()).digest()[:16]
        key = bytes.fromhex(self.keys[rel])[:32]
        encrypted = encrypt_pages(raw, key, salt, self.explicit_salt)
        wal_salt = b"fixture!"
        frames = bytearray()
        for index in range(0, len(raw), PAGE):
            pgno = index // PAGE + 1
            frames.extend(struct.pack(">II", pgno, len(raw) // PAGE) + wal_salt + b"\x00" * 8)
            frames.extend(encrypted[index:index + PAGE])
        frames.extend(struct.pack(">II", 1, len(raw) // PAGE) + b"old-salt" + b"\x00" * 8)
        frames.extend(b"\xff" * PAGE)
        wal = Path(str(self.storage / rel) + "-wal")
        wal.write_bytes(b"\x00" * 16 + wal_salt + b"\x00" * 8 + frames)
        return wal

    def write_keys(self):
        self.keys_file.write_text(json.dumps(self.keys), encoding="utf-8")

    def parameters(self, **overrides):
        return {"db_dir": str(self.root), "account": self.account,
                "keys_file": str(self.keys_file), "workdir": str(self.base / "work"), **overrides}
