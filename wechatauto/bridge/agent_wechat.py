"""AgentWeChat 原生协议后端：复用 LangBot 网关、持久日志及独立历史权限。"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import hashlib
import http.client
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import re
import socket
import stat
import threading
import time
from urllib.parse import quote, urlencode, urlsplit

from aiohttp import web

from wechatauto.platforms.linux import _read_file

from .history import validate_history_config
from .journal import Journal, _identifier, _json
from .wechatpad import MAX_TEXT_CHARS, create_app, encode_native_record


MAX_JSON_BYTES = 1024 * 1024
MAX_CONFIG_BYTES = 128 * 1024
MAX_TOKEN_BYTES = 4096
MAX_TEXT_BYTES = 64 * 1024
CURSOR_CHAT = "agent-wechat"
CURSOR_SHARD = "events-v1"
logger = logging.getLogger(__name__)


class AgentWeChatError(RuntimeError):
    """只公开固定诊断码，绝不携带响应正文、凭据或消息内容。"""

    def __init__(self, code="AGENT_WECHAT_UNAVAILABLE"):
        self.code = code
        super().__init__(code)


def _exact(value, label, maximum=256):
    if (not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise ValueError("invalid " + label)
    return value


def _chat(value):
    value = _exact(value, "chat username")
    if not re.fullmatch(r"[A-Za-z0-9_.@-]+", value):
        raise ValueError("invalid chat username")
    return value


def _segment(value):
    return quote(_exact(value, "resource identifier", 8192), safe="")


def _private_bytes(path, maximum):
    """在同一文件描述符上校验所有权、0600、常规文件及硬链接数，然后有界读取。"""
    try:
        with _read_file(Path(path)) as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise PermissionError("runtime files must be caller-owned private regular files")
            raw = stream.read(maximum + 1)
    except (OSError, RuntimeError):
        raise PermissionError("runtime file is unavailable or not private") from None
    if len(raw) > maximum:
        raise ValueError("runtime file exceeds the size limit")
    return raw


def _number(value, label, minimum, maximum, integer=False):
    if (type(value) not in ((int,) if integer else (int, float)) or not math.isfinite(value)
            or not minimum <= value <= maximum):
        raise ValueError("invalid " + label)
    return value


def _daemon_url(value):
    value = _exact(value, "daemon_url", 512)
    try:
        parsed = urlsplit(value)
        address = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port
    except ValueError:
        raise ValueError("daemon_url must use an explicit loopback IP and port") from None
    if (parsed.scheme != "http" or not address.is_loopback or not port or parsed.username is not None
            or parsed.password is not None or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("daemon_url must be a plain loopback HTTP origin")
    host = "[" + str(address) + "]" if address.version == 6 else str(address)
    return "http://%s:%d" % (host, port)


def validate_config(value):
    """协议配置只接受显式账号、源身份与精确会话白名单，不继承 GUI 配置。"""
    fields = {"daemon_url", "daemon_token_file", "account", "expected_wxid", "expected_server_id",
              "state_dir", "token", "allowed_chats", "write_chats", "request_prefix", "group_request_prefix",
              "native_history", "listen_host", "port", "poll_interval", "request_timeout", "send_confirm_timeout",
              "send_poll_interval", "event_page_size"}
    if not isinstance(value, dict) or set(value) - fields:
        raise ValueError("invalid AgentWeChat configuration fields")
    config = dict(value)
    config["daemon_url"] = _daemon_url(config.get("daemon_url"))
    for name in ("daemon_token_file", "state_dir"):
        path = Path(_exact(config.get(name), name, 4096))
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("runtime paths must be absolute without parent traversal")
        config[name] = str(path)
    config["account"] = _chat(config.get("account"))
    config["expected_wxid"] = _chat(config.get("expected_wxid"))
    config["expected_server_id"] = _exact(config.get("expected_server_id"), "expected_server_id")
    config["token"] = _exact(config.get("token"), "bridge token", MAX_TOKEN_BYTES)
    config["native_history"] = validate_history_config(config.get("native_history"), config["token"])
    for name in ("allowed_chats", "write_chats"):
        entries = config.get(name)
        if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
            raise ValueError("explicit chat allowlists are required")
        config[name] = list(dict.fromkeys(_chat(entry) for entry in entries))
    for name in ("request_prefix", "group_request_prefix"):
        prefix = config.setdefault(name, "/lb ")
        if (not isinstance(prefix, str) or not prefix or len(prefix) > 128
                or any(ord(character) < 32 or ord(character) == 127 for character in prefix)):
            raise ValueError("a nonempty request prefix is required")
    config.setdefault("listen_host", "127.0.0.1")
    _exact(config["listen_host"], "listen_host")
    config.setdefault("port", 5080)
    _number(config["port"], "port", 1, 65535, integer=True)
    for name, default, minimum, maximum, integer in (
            ("poll_interval", .5, .1, 60, False), ("request_timeout", 5, .1, 10, False),
            ("send_confirm_timeout", 30, .1, 60, False), ("send_poll_interval", .2, .01, 2, False),
            ("event_page_size", 100, 1, 200, True)):
        _number(config.setdefault(name, default), name, minimum, maximum, integer)
    return config


def load_config(path):
    """读取私有 JSON；不会把解析失败的正文或令牌写入异常。"""
    raw = _private_bytes(path, MAX_CONFIG_BYTES)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError("invalid runtime config JSON") from None
    return validate_config(value)


def _decimal(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,19}", value) or int(value) > 2 ** 63 - 1:
        raise AgentWeChatError("AGENT_WECHAT_INVALID_CURSOR")
    return int(value)


def _timestamp(value, missing=False):
    if value is None and missing:
        return 0
    try:
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError()
        created = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if created.tzinfo is None:
            raise ValueError()
        result = int(created.timestamp())
        if result < 0:
            raise ValueError()
        return result
    except (ValueError, OverflowError, OSError):
        raise AgentWeChatError("AGENT_WECHAT_INVALID_MESSAGE_TIME") from None


class AgentWeChatBackend:
    """同步、有界的原生 HTTP 后端；RLock 串行保护发送、读取和事件提交。"""

    def __init__(self, config, journal):
        self.config = validate_config(config)
        self.journal = journal
        raw = _private_bytes(self.config["daemon_token_file"], MAX_TOKEN_BYTES)
        try:
            self._token = _exact(raw.decode("utf-8").rstrip("\r\n"), "daemon token", MAX_TOKEN_BYTES)
            self._token.encode("ascii")
        except (ValueError, UnicodeError):
            raise ValueError("invalid private daemon token") from None
        if self._token == self.config["token"] or self._token == (self.config["native_history"] or {}).get("token"):
            raise ValueError("daemon, bridge and history tokens must be distinct")
        source = ["agent-wechat-v1", self.config["daemon_url"], self.config["expected_server_id"],
                  self.config["account"], self.config["expected_wxid"]]
        self.source_identity = "agent-wechat-v1:" + hashlib.sha256(_json(source).encode()).hexdigest()
        journal.bind_identity(self.source_identity)
        self.lock = threading.RLock()
        self._closed = False
        self._base = "/v1/accounts/" + _segment(self.config["account"])
        self._consumer = "langbot-" + self.source_identity.rsplit(":", 1)[-1][:32]

    def _request(self, method, path, *, query=None, body=None, headers=None, expected=200, timeout=None):
        if self._closed:
            raise AgentWeChatError("AGENT_WECHAT_CLOSED")
        origin = urlsplit(self.config["daemon_url"])
        deadline = time.monotonic() + (self.config["request_timeout"] if timeout is None else
                                        min(timeout, self.config["request_timeout"]))
        connection = http.client.HTTPConnection(origin.hostname, origin.port,
                                                 timeout=max(.001, deadline - time.monotonic()))
        request_headers = {"Authorization": "Bearer " + self._token, "Accept": "application/json",
                           "Connection": "close"}
        request_headers.update(headers or {})
        encoded = None if body is None else _json(body).encode("utf-8")
        if encoded is not None:
            if len(encoded) > MAX_JSON_BYTES:
                raise ValueError("daemon request exceeds the size limit")
            request_headers["Content-Type"] = "application/json"
        target = path + ("?" + urlencode(query) if query else "")
        timer = None
        expired = threading.Event()
        try:
            connection.connect()
            connected_socket = connection.sock

            def expire():
                expired.set()
                try:
                    connected_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            timer = threading.Timer(max(.001, deadline - time.monotonic()), expire)
            timer.daemon = True
            timer.start()
            connection.request(method, target, body=encoded, headers=request_headers)
            response = connection.getresponse()
            if response.status != expected:
                raise AgentWeChatError("AGENT_WECHAT_NOT_FOUND" if response.status == 404 else "AGENT_WECHAT_HTTP_FAILURE")
            if response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise AgentWeChatError("AGENT_WECHAT_INVALID_JSON")
            raw = response.read(MAX_JSON_BYTES + 1)
            if expired.is_set() or time.monotonic() > deadline:
                raise AgentWeChatError("AGENT_WECHAT_TIMEOUT")
            if len(raw) > MAX_JSON_BYTES:
                raise AgentWeChatError("AGENT_WECHAT_JSON_TOO_LARGE")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except AgentWeChatError:
            raise
        except (ValueError, UnicodeError, OSError, http.client.HTTPException):
            raise AgentWeChatError("AGENT_WECHAT_REQUEST_FAILED") from None
        finally:
            if timer is not None:
                timer.cancel()
            connection.close()

    def _ready(self):
        status = self._request("GET", "/v1/status")
        version = status.get("version")
        if (status.get("serverId") != self.config["expected_server_id"] or status.get("state") != "ready"
                or not isinstance(version, dict) or version.get("product") != "agent-wechat"
                or version.get("backend") != "native" or type(version.get("apiVersion")) is not int
                or version["apiVersion"] != 1 or not isinstance(version.get("capabilities"), list)
                or not {"text-send", "events"}.issubset(version["capabilities"])):
            raise AgentWeChatError("AGENT_WECHAT_SOURCE_MISMATCH")
        account = self._request("GET", self._base)
        if (account.get("id") != self.config["account"] or account.get("ownUsername") != self.config["expected_wxid"]
                or account.get("authenticated") is not True or account.get("connection") != "online"
                or account.get("incomingTransport") != "native_push" or account.get("contactSyncReady") is not True
                or account.get("nativeRuntime") is not True or account.get("profileRecoveryPending") is not False
                or account.get("pendingSavedLoginProjection") is not False):
            raise AgentWeChatError("AGENT_WECHAT_ACCOUNT_UNVERIFIED")
        return account

    def profile(self):
        with self.lock:
            self._ready()
            own = self.config["expected_wxid"]
            try:
                contact = self._request("GET", self._base + "/contacts/" + _segment(own))
            except AgentWeChatError as error:
                if error.code == "AGENT_WECHAT_NOT_FOUND":
                    return {"wxid": own, "nickname": ""}
                raise
            if contact.get("id") != own or contact.get("username") != own:
                raise AgentWeChatError("AGENT_WECHAT_PROFILE_MISMATCH")
            nickname = contact.get("nickname") or contact.get("displayName") or ""
            if not isinstance(nickname, str) or len(nickname) > 256:
                raise AgentWeChatError("AGENT_WECHAT_INVALID_PROFILE")
            return {"wxid": own, "nickname": nickname}

    def is_logged_in(self):
        with self.lock:
            try:
                self._ready()
                return True
            except (AgentWeChatError, ValueError, TypeError):
                return False

    def _intent(self, value, destination, identifier=None):
        if (value.get("accountId") != self.config["account"] or value.get("destination") != destination
                or (identifier is not None and value.get("id") != identifier)
                or value.get("state") not in {"queued", "submitting", "accepted", "failed", "needs_review", "cancelled"}):
            raise AgentWeChatError("AGENT_WECHAT_SEND_MISMATCH")
        return _exact(value.get("id"), "send intent identifier")

    def send_text(self, to, text, ats, client_id):
        _chat(to)
        _identifier(client_id)
        if to not in self.config["write_chats"]:
            raise PermissionError("target is not in write_chats")
        if ats != []:
            raise ValueError("AgentWeChat bridge does not support mentions")
        if (not isinstance(text, str) or not text.strip() or "\x00" in text or len(text) > MAX_TEXT_CHARS
                or len(text.encode("utf-8")) > MAX_TEXT_BYTES):
            raise ValueError("invalid bounded text")
        with self.lock:
            previous = self.journal.get_outbox(client_id)
            if previous is not None and previous["status"] in ("uncertain", "sent"):
                raise AgentWeChatError("AGENT_WECHAT_SEND_ALREADY_ATTEMPTED")
            self._ready()
            key = "langbot-v1-" + hashlib.sha256(_json([self.source_identity, client_id]).encode()).hexdigest()
            intent = self._request("POST", self._base + "/sends", body={"threadId": to, "text": text},
                                   headers={"Idempotency-Key": key}, expected=202)
            intent_id = self._intent(intent, to)
            deadline = time.monotonic() + self.config["send_confirm_timeout"]
            while time.monotonic() < deadline:
                try:
                    intent = self._request("GET", self._base + "/sends/" + _segment(intent_id),
                                           timeout=deadline - time.monotonic())
                except AgentWeChatError:
                    if time.monotonic() >= deadline:
                        return {"confirmed": False}
                    raise
                self._intent(intent, to, intent_id)
                state, server_id = intent["state"], intent.get("serverId")
                if state == "accepted":
                    confirmed = (isinstance(server_id, str) and len(server_id) <= 20
                                 and bool(re.fullmatch(r"[0-9]+", server_id)) and 0 < int(server_id) < 2 ** 64)
                    return {"confirmed": confirmed, **({"msg_id": server_id} if confirmed else {})}
                if state in ("failed", "needs_review", "cancelled"):
                    return {"confirmed": False}
                time.sleep(min(self.config["send_poll_interval"], max(0, deadline - time.monotonic())))
            return {"confirmed": False}

    def send_image(self, to, image, ats, client_id):
        raise ValueError("AgentWeChat bridge supports text only")

    def _page(self, path, limit, cursor=None):
        query = {"limit": limit}
        if cursor is not None:
            query["cursor"] = cursor
        page = self._request("GET", path, query=query)
        items, following = page.get("items"), page.get("nextCursor")
        if (not isinstance(items, list) or len(items) > limit or any(not isinstance(item, dict) for item in items)
                or (following is not None and (not isinstance(following, str) or not following
                                              or len(following) > 8192 or following == cursor))):
            raise AgentWeChatError("AGENT_WECHAT_INVALID_PAGE")
        return page

    def chatroom_members(self, room):
        if room not in self.config["allowed_chats"] or not room.endswith("@chatroom"):
            raise PermissionError("room is not in allowed_chats")
        with self.lock:
            self._ready()
            result, cursor, seen = [], None, set()
            for _ in range(50):
                page = self._page(self._base + "/groups/" + _segment(room) + "/members", 200, cursor)
                for member in page["items"]:
                    if member.get("deleted") is True:
                        continue
                    username = _chat(member.get("username"))
                    if member.get("id") != username or username in seen:
                        raise AgentWeChatError("AGENT_WECHAT_INVALID_MEMBER")
                    nickname = member.get("displayName") or username
                    if not isinstance(nickname, str) or len(nickname) > 256:
                        raise AgentWeChatError("AGENT_WECHAT_INVALID_MEMBER")
                    result.append({"user_name": username, "nick_name": nickname})
                    seen.add(username)
                cursor = page.get("nextCursor")
                if cursor is None:
                    return result
            raise AgentWeChatError("AGENT_WECHAT_MEMBER_PAGE_BOUND")

    def _message(self, value, thread, identifier=None):
        if (value.get("accountId") != self.config["account"] or value.get("threadId") != thread
                or (identifier is not None and value.get("id") != identifier)):
            raise AgentWeChatError("AGENT_WECHAT_MESSAGE_MISMATCH")
        _exact(value.get("id"), "message identifier", 8192)
        if type(value.get("type")) is not int:
            raise AgentWeChatError("AGENT_WECHAT_INVALID_MESSAGE")
        return value

    def _inbound(self, event):
        data = event["data"]
        if event["kind"] != "message.committed" or data.get("origin") != "live":
            return None
        if data.get("accountId", self.config["account"]) != self.config["account"]:
            raise AgentWeChatError("AGENT_WECHAT_MESSAGE_MISMATCH")
        thread = _chat(data.get("threadId"))
        if thread not in self.config["allowed_chats"]:
            return None
        identifier = _exact(data.get("messageId"), "event message identifier", 8192)
        value = self._message(self._request("GET", self._base + "/messages/" + _segment(identifier)), thread, identifier)
        own, sender = self.config["expected_wxid"], value.get("senderId")
        if value.get("direction") != data.get("direction"):
            raise AgentWeChatError("AGENT_WECHAT_MESSAGE_MISMATCH")
        if value["type"] != 1 or value.get("recall") is not None:
            return None
        body = value.get("body")
        if not isinstance(body, str) or "\x00" in body or len(body.encode("utf-8")) > MAX_TEXT_BYTES:
            raise AgentWeChatError("AGENT_WECHAT_INVALID_MESSAGE")
        if thread == "filehelper":
            if sender != own or value.get("direction") != "outgoing" or not body.startswith(self.config["request_prefix"]):
                return None
            body = body[len(self.config["request_prefix"]):]
        else:
            if not sender or sender == own or value.get("direction") != "incoming":
                return None
            _chat(sender)
            if thread.endswith("@chatroom"):
                content = body[len(sender) + 2:] if body.startswith(sender + ":\n") else body
                if not content.startswith(self.config["group_request_prefix"]):
                    return None
            elif sender != thread:
                raise AgentWeChatError("AGENT_WECHAT_MESSAGE_MISMATCH")
        if not body.strip():
            return None
        event_id = hashlib.sha256(_json([self.source_identity, identifier]).encode()).hexdigest()
        return encode_native_record({"username": thread, "sender_username": sender, "content": body,
                                     "type": 1, "create_time": _timestamp(value.get("createdAt")),
                                     "server_id": value.get("serverId")}, own, event_id)

    def poll_once(self):
        """先事务提交事件与游标再 ACK；初次原子基线之后的消息立即从事件流读取。"""
        with self.lock:
            self._ready()
            cursor = self.journal.get_cursor(CURSOR_CHAT, CURSOR_SHARD)
            if cursor is None:
                page = self._page(self._base + "/threads", 1)
                cursor = _decimal(page.get("asOfEventCursor"))
                self.journal.record_observation(CURSOR_CHAT, CURSOR_SHARD, cursor)
            page = self._request("GET", self._base + "/events", query={"after": str(cursor),
                                  "limit": self.config["event_page_size"]})
            items = page.get("items")
            if not isinstance(items, list) or len(items) > self.config["event_page_size"]:
                raise AgentWeChatError("AGENT_WECHAT_INVALID_EVENT_PAGE")
            positions, previous = [], cursor
            for event in items:
                if (not isinstance(event, dict) or not isinstance(event.get("kind"), str)
                        or not isinstance(event.get("data"), dict)):
                    raise AgentWeChatError("AGENT_WECHAT_INVALID_EVENT")
                position = _decimal(event.get("cursor"))
                if position <= previous:
                    raise AgentWeChatError("AGENT_WECHAT_INVALID_CURSOR")
                positions.append(position)
                previous = position
            if _decimal(page.get("nextCursor")) != previous:
                raise AgentWeChatError("AGENT_WECHAT_INVALID_CURSOR")
            admitted = 0
            for event, position in zip(items, positions):
                payload = self._inbound(event)
                self.journal.record_observation(CURSOR_CHAT, CURSOR_SHARD, position, payload)
                admitted += payload is not None
                cursor = position
            self._request("POST", self._base + "/consumers/" + self._consumer + "/ack", body={"cursor": str(cursor)})
            return admitted

    def _history_groups(self):
        return {group for group in self.config["allowed_chats"] if group.endswith("@chatroom")} | set(
            (self.config["native_history"] or {}).get("allowed_groups", []))

    @staticmethod
    def _bounds(limit, offset):
        _number(limit, "history limit", 1, 100, integer=True)
        _number(offset, "history offset", 0, 1000, integer=True)

    def get_history(self, group_username, limit=50, offset=0):
        """沿服务端不透明游标读取有界页；只用单页切片适配原有 offset 接口。"""
        self._bounds(limit, offset)
        if group_username not in self._history_groups():
            raise PermissionError("group is not authorized for history")
        with self.lock:
            self._ready()
            path = self._base + "/threads/" + _segment(group_username) + "/messages"
            cursor, skipped, result, visited = None, 0, [], set()
            for _ in range(6):
                page = self._page(path, 200, cursor)
                for value in page["items"]:
                    if value.get("deleted") is True:
                        continue
                    if skipped < offset:
                        skipped += 1
                        continue
                    self._message(value, group_username)
                    result.append({"local_id": value["id"], "shard": "agent-wechat-profile", "sort_seq": value["id"],
                                   "sender_username": value.get("senderId") or "", "type": value["type"],
                                   "content": value.get("body") or "", "create_time": _timestamp(value.get("createdAt"), missing=True)})
                    if len(result) == limit:
                        return result
                cursor = page.get("nextCursor")
                if cursor is None:
                    return result
                if cursor in visited:
                    raise AgentWeChatError("AGENT_WECHAT_INVALID_PAGE")
                visited.add(cursor)
            raise AgentWeChatError("AGENT_WECHAT_HISTORY_PAGE_BOUND")

    def list_groups(self, limit=50, offset=0, allowed_groups=None):
        """只逐项读取至多 100 个已授权群的详情，不扫描其他群或消息。"""
        self._bounds(limit, offset)
        permitted = self._history_groups()
        if allowed_groups is None:
            allowed_groups = sorted(permitted)
        if (not isinstance(allowed_groups, list) or len(allowed_groups) > 100
                or any(group not in permitted for group in allowed_groups)):
            raise PermissionError("group is not authorized for history")
        selected = list(dict.fromkeys(allowed_groups))[offset:offset + limit]
        with self.lock:
            self._ready()
            result = []
            for group in selected:
                value = self._request("GET", self._base + "/groups/" + _segment(group))
                if value.get("id") != group or value.get("roomId") != group:
                    raise AgentWeChatError("AGENT_WECHAT_GROUP_MISMATCH")
                name = value.get("displayName") or group
                if not isinstance(name, str):
                    raise AgentWeChatError("AGENT_WECHAT_INVALID_GROUP")
                result.append({"username": group, "name": name})
            return result

    def close(self):
        with self.lock:
            self._closed = True
            self._token = ""


BACKEND_KEY = web.AppKey("agent_wechat_backend", AgentWeChatBackend)
JOURNAL_KEY = web.AppKey("agent_wechat_journal", Journal)


def create_runtime_app(config, *, backend=None, journal=None):
    """创建现有兼容网关，轮询同一协议后端并由 cleanup 统一关闭。"""
    config = validate_config(config)
    if journal is None:
        journal = backend.journal if backend is not None else Journal(Path(config["state_dir"]) / "journal")
    try:
        if backend is None:
            backend = AgentWeChatBackend(config, journal)
        if backend.journal is not journal:
            raise ValueError("backend and gateway must share one journal")
        app = create_app(backend, journal, config["token"], history_config=config["native_history"])
    except Exception:
        journal.close()
        raise
    app[BACKEND_KEY], app[JOURNAL_KEY] = backend, journal

    async def lifetime(application):
        stopped = asyncio.Event()

        async def poll():
            available = None
            while not stopped.is_set():
                try:
                    await asyncio.to_thread(backend.poll_once)
                    if available is not True:
                        logger.info("AgentWeChat bridge reader ready")
                    available = True
                except Exception:
                    if available is not False:
                        logger.warning("AgentWeChat bridge is waiting for a verified account or cursor")
                    available = False
                try:
                    await asyncio.wait_for(stopped.wait(), config["poll_interval"])
                except asyncio.TimeoutError:
                    pass

        task = asyncio.create_task(poll())
        try:
            yield
        finally:
            stopped.set()
            await task
            backend.close()
            journal.close()

    app.cleanup_ctx.append(lifetime)
    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description="AgentWeChat WeChatPad-compatible LangBot bridge")
    parser.add_argument("--config", required=True, help="Caller-owned 0600 runtime JSON file")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        app = create_runtime_app(config)
    except Exception:
        parser.error("private AgentWeChat runtime configuration could not be validated")
    web.run_app(app, host=config["listen_host"], port=config["port"], access_log=None, print=None)


if __name__ == "__main__":
    main()
