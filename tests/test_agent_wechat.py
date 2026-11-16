"""AgentWeChat 适配器的本机 HTTP 协议、持久化和网关回归测试。"""

import asyncio
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import importlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, unquote, urlsplit

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer

from wechatauto.bridge.journal import ConflictError, Journal
from wechatauto.bridge.wechatpad import create_app

try:
    adapter = importlib.import_module("wechatauto.bridge.agent_wechat")
except ModuleNotFoundError:
    adapter = None


class FakeDaemon:
    """使用真实回环 HTTP，字段名与 Rust HTTP 合约保持一致。"""

    def __init__(self):
        self.requests = []
        self.events = []
        self.messages = {}
        self.sends = []
        self.acks = []
        self.send_states = ["accepted"]
        self.server_id = "1234567890123456789"
        self.response_status = None
        self.extra_events_on_baseline = []
        self.raw_override = None
        self.delay = 0
        self.intent_overrides = {}
        self.self_contact_status = 200
        self.event_page_override = None
        self.event_status = 200
        self.accounts = {"id": "account", "ownUsername": "wxid_self", "authenticated": True,
                         "connection": "online", "incomingTransport": "native_push",
                         "contactSyncReady": True, "nativeRuntime": True,
                         "profileRecoveryPending": False, "pendingSavedLoginProjection": False}
        self.status = {"serverId": "daemon-source", "state": "ready", "version": {
            "product": "agent-wechat", "backend": "native", "apiVersion": 1,
            "capabilities": ["text-send", "events"]}}
        self.groups = {"room@chatroom": {"id": "room@chatroom", "roomId": "room@chatroom",
                                         "displayName": "Room"}}
        self.history = []
        self.member_pages = [[{"id": "member", "username": "member", "displayName": "Member"}]]
        self.history_page_requests = []
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.handle_request("GET")

            def do_POST(self):
                self.handle_request("POST")

            def handle_request(self, method):
                parts = urlsplit(self.path)
                path = unquote(parts.path)
                query = parse_qs(parts.query)
                size = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(size)) if size else None
                daemon.requests.append((method, path, query, dict(self.headers), body))
                if self.headers.get("Authorization") != "Bearer daemon-test-token":
                    self.reply(401, {"error": {"code": "unauthorized"}})
                    return
                if daemon.delay:
                    time.sleep(daemon.delay)
                if daemon.raw_override is not None:
                    self.reply(200, daemon.raw_override, raw=True)
                    return
                if daemon.response_status is not None:
                    self.send_response(daemon.response_status)
                    self.send_header("Location", daemon.url + "/redirect-target")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                prefix = "/v1/accounts/account"
                if path == "/v1/status":
                    self.reply(200, daemon.status)
                elif path == prefix:
                    self.reply(200, daemon.accounts)
                elif path == prefix + "/contacts/wxid_self":
                    self.reply(daemon.self_contact_status, {
                        "id": "wxid_self", "username": "wxid_self", "nickname": "Assistant"})
                elif path == prefix + "/threads":
                    boundary = str(len(daemon.events))
                    for message, origin in daemon.extra_events_on_baseline:
                        daemon.add_message(message, origin=origin)
                    daemon.extra_events_on_baseline = []
                    self.reply(200, {"items": [], "nextCursor": None, "sourceGeneration": "profile-1",
                                     "asOfEventCursor": boundary, "coverage": {}})
                elif path == prefix + "/events":
                    if daemon.event_status != 200:
                        self.reply(daemon.event_status, {"error": {"code": "event_cursor_expired"}})
                        return
                    after, limit = int(query["after"][0]), int(query["limit"][0])
                    items = daemon.events[after:after + limit]
                    self.reply(200, daemon.event_page_override or {
                        "items": items, "nextCursor": items[-1]["cursor"] if items else str(after)})
                elif path.startswith(prefix + "/consumers/"):
                    daemon.acks.append(body["cursor"])
                    self.reply(200, {"cursor": body["cursor"], "messageReadStateChanged": False})
                elif path == prefix + "/sends":
                    daemon.sends.append((dict(self.headers), body))
                    self.reply(202, daemon.intent("queued"))
                elif path == prefix + "/sends/send-intent":
                    state = daemon.send_states[0]
                    if len(daemon.send_states) > 1:
                        daemon.send_states.pop(0)
                    self.reply(200, daemon.intent(state))
                elif path.startswith(prefix + "/messages/"):
                    message = daemon.messages.get(path[len(prefix + "/messages/"):])
                    self.reply(200 if message else 404, message or {"error": {"code": "not_found"}})
                elif path == prefix + "/threads/room@chatroom/messages":
                    daemon.history_page_requests.append(query)
                    start = int(query.get("cursor", ["page-0"])[0].removeprefix("page-"))
                    limit = int(query["limit"][0])
                    end = start + limit
                    self.reply(200, {"items": daemon.history[start:end], "nextCursor": (
                        "page-" + str(end) if end < len(daemon.history) else None),
                        "sourceGeneration": "profile-1", "coverage": {}})
                elif path == prefix + "/groups/room@chatroom/members":
                    number = int(query.get("cursor", ["members-0"])[0].removeprefix("members-"))
                    self.reply(200, {"items": daemon.member_pages[number], "nextCursor": (
                        "members-" + str(number + 1) if number + 1 < len(daemon.member_pages) else None),
                        "coverage": {}, "sourceGeneration": "profile-1"})
                elif path.startswith(prefix + "/groups/"):
                    group = daemon.groups.get(path[len(prefix + "/groups/"):])
                    self.reply(200 if group else 404, group or {"error": {"code": "not_found"}})
                else:
                    self.reply(404, {"error": {"code": "not_found"}})

            def reply(self, status, value, raw=False):
                encoded = value if raw else json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                try:
                    self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.http.server_address[1]
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def intent(self, state):
        return {"id": "send-intent", "accountId": "account", "destination": "filehelper",
                "state": state, "clientId": "native-client", "serverId": self.server_id,
                "errorCode": None, "createdAt": "2026-01-01T00:00:00Z", **self.intent_overrides}

    def add_message(self, value, origin="live", **event_data):
        self.messages[value["id"]] = deepcopy(value)
        self.events.append({"cursor": str(len(self.events) + 1), "kind": "message.committed",
                            "data": {"messageId": value["id"], "threadId": value["threadId"],
                                     "direction": value["direction"], "origin": origin, **event_data},
                            "createdAt": "2026-01-01T00:00:00Z"})

    def close(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()


def message(identifier="opaque/id", chat="wxid_owner", sender="wxid_owner", body="hello", direction="incoming"):
    return {"id": identifier, "accountId": "account", "threadId": chat, "senderId": sender,
            "serverId": "987654321", "type": 1, "direction": direction, "body": body,
            "createdAt": "2026-01-01T00:00:00Z", "mentions": {"state": "unknown", "userIds": []},
            "version": "profile-1", "media": []}


class Fixture:
    def setup_fixture(self, config_overrides=None):
        self.assertIsNotNone(adapter, "AgentWeChat backend is missing")
        self.tmp = tempfile.TemporaryDirectory()
        self.daemon = FakeDaemon()
        self.token_path = Path(self.tmp.name) / "daemon.token"
        self.token_path.write_text("daemon-test-token\n")
        self.token_path.chmod(0o600)
        self.config = {"daemon_url": self.daemon.url, "daemon_token_file": str(self.token_path),
                       "account": "account", "expected_wxid": "wxid_self", "expected_server_id": "daemon-source",
                       "state_dir": str(Path(self.tmp.name) / "state"), "token": "bridge-secret",
                       "allowed_chats": ["filehelper", "wxid_owner", "room@chatroom"],
                       "write_chats": ["filehelper", "wxid_owner", "room@chatroom"],
                       "request_prefix": "/lb ", "group_request_prefix": "/lb ",
                       "send_confirm_timeout": .2, "send_poll_interval": .01, "event_page_size": 3}
        self.config.update(config_overrides or {})
        self.journal = Journal(Path(self.tmp.name) / "journal")
        self.backend = adapter.AgentWeChatBackend(self.config, self.journal)

    def close_fixture(self):
        if hasattr(self, "backend"):
            self.backend.close()
            self.journal.close()
            self.daemon.close()
            self.tmp.cleanup()


class AgentWeChatTests(Fixture, unittest.TestCase):
    def setUp(self):
        self.setup_fixture()

    def tearDown(self):
        self.close_fixture()

    def test_default_source_identity_keeps_the_original_journal_namespace(self):
        source = ["agent-wechat-v1", self.daemon.url, "daemon-source", "account", "wxid_self"]
        encoded = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        expected = "agent-wechat-v1:" + hashlib.sha256(encoded.encode()).hexdigest()
        self.assertEqual(self.backend.source_identity, expected)

    def test_profile_pins_native_daemon_and_exact_account(self):
        self.assertEqual(self.backend.profile(), {"wxid": "wxid_self", "nickname": "Assistant"})
        for target, field, wrong in ((self.daemon.status, "serverId", "other"),
                                     (self.daemon.status["version"], "backend", "simulator"),
                                     (self.daemon.accounts, "ownUsername", "wxid_other"),
                                     (self.daemon.accounts, "id", "other"),
                                     (self.daemon.accounts, "authenticated", False),
                                     (self.daemon.accounts, "incomingTransport", "heartbeat_polling")):
            with self.subTest(field=field):
                old = target[field]
                target[field] = wrong
                self.assertFalse(self.backend.is_logged_in())
                target[field] = old

    def test_profile_uses_verified_account_identity_if_self_contact_is_not_saved(self):
        self.daemon.self_contact_status = 404
        self.assertEqual(self.backend.profile(), {"wxid": "wxid_self", "nickname": ""})
        self.daemon.self_contact_status = 503
        with self.assertRaises(RuntimeError):
            self.backend.profile()

    def test_url_config_rejects_remote_hosts_credentials_paths_queries_and_bad_bounds(self):
        for url in ("https://127.0.0.1:1234", "http://localhost:1234", "http://example.invalid:1234",
                    "http://user:pass@127.0.0.1:1234", "http://127.0.0.1:1234/path",
                    "http://127.0.0.1:1234?secret=x", "http://127.0.0.1:1234#fragment"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                adapter.validate_config({**self.config, "daemon_url": url})
        for changes in ({"allowed_chats": []}, {"event_page_size": 201}, {"request_timeout": 1000},
                        {"expected_wxid": ""}, {"surprise": True}):
            with self.subTest(changes=changes), self.assertRaises((ValueError, TypeError)):
                adapter.validate_config({**self.config, **changes})

    def test_private_config_and_token_reject_permissions_and_symlinks(self):
        config_path = Path(self.tmp.name) / "config.json"
        config_path.write_text(json.dumps(self.config))
        config_path.chmod(0o600)
        self.assertEqual(adapter.load_config(config_path)["account"], "account")
        config_path.chmod(0o644)
        with self.assertRaises(PermissionError):
            adapter.load_config(config_path)
        link = Path(self.tmp.name) / "linked-token"
        link.symlink_to(self.token_path)
        with self.assertRaises(PermissionError):
            adapter.AgentWeChatBackend({**self.config, "daemon_token_file": str(link)}, self.journal)
        self.token_path.chmod(0o644)
        with self.assertRaises(PermissionError):
            adapter.AgentWeChatBackend(self.config, self.journal)

    def test_http_redirect_and_unbounded_or_invalid_json_are_rejected(self):
        self.daemon.response_status = 302
        self.assertFalse(self.backend.is_logged_in())
        self.assertFalse(any(row[1] == "/redirect-target" for row in self.daemon.requests))
        self.daemon.response_status = None
        for raw in (b"not-json", b"[]", b"x" * (adapter.MAX_JSON_BYTES + 1)):
            self.daemon.raw_override = raw
            self.assertFalse(self.backend.is_logged_in())

    def test_environment_proxy_is_not_used(self):
        old = {key: os.environ.get(key) for key in ("HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy")}
        try:
            os.environ.update({"HTTP_PROXY": "http://127.0.0.1:1", "http_proxy": "http://127.0.0.1:1",
                               "NO_PROXY": "", "no_proxy": ""})
            self.assertTrue(self.backend.is_logged_in())
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_http_total_deadline_cancels_a_stalled_response(self):
        self.backend.config["request_timeout"] = .1
        self.daemon.delay = .5
        start = time.monotonic()
        self.assertFalse(self.backend.is_logged_in())
        self.assertLess(time.monotonic() - start, .4)

    def test_send_waits_for_accepted_get_and_uses_stable_bounded_idempotency_key(self):
        self.daemon.send_states = ["queued", "submitting", "accepted"]
        receipt = self.backend.send_text("filehelper", "Reply", [], "bridge-client")
        self.assertEqual(receipt, {"confirmed": True, "msg_id": "1234567890123456789"})
        self.assertEqual(len(self.daemon.sends), 1)
        headers, body = self.daemon.sends[0]
        self.assertEqual(body, {"threadId": "filehelper", "text": "Reply"})
        self.assertTrue(0 < len(headers["Idempotency-Key"]) <= 200)
        self.assertNotIn("bridge-client", headers["Idempotency-Key"])
        gets = [row for row in self.daemon.requests if row[1].endswith("/sends/send-intent")]
        self.assertEqual(len(gets), 3)

    def test_unsupported_or_unauthorized_sends_fail_before_post(self):
        for invoke in (lambda: self.backend.send_image("filehelper", "aGVsbG8=", [], "image"),
                       lambda: self.backend.send_text("filehelper", "hello", ["member"], "mention"),
                       lambda: self.backend.send_text("wxid_other", "hello", [], "other"),
                       lambda: self.backend.send_text("filehelper", "界" * 32768, [], "oversize")):
            with self.assertRaises((ValueError, PermissionError, RuntimeError)):
                invoke()
        self.assertEqual(self.daemon.sends, [])

    def test_send_needs_review_timeout_and_invalid_receipts_are_never_confirmed(self):
        for state, server_id in (("needs_review", "123"), ("failed", "123"), ("queued", "123"),
                                 ("accepted", "0"), ("accepted", "-1"), ("accepted", "abc")):
            with self.subTest(state=state, server_id=server_id):
                self.daemon.send_states, self.daemon.server_id = [state], server_id
                result = self.backend.send_text("filehelper", "Reply", [], state + server_id)
                self.assertFalse(result.get("confirmed"))

    def test_send_intent_identity_mismatch_is_never_reported_as_delivered(self):
        for override in ({"accountId": "other"}, {"destination": "other"}):
            self.daemon.intent_overrides = override
            with self.assertRaises(RuntimeError):
                self.backend.send_text("filehelper", "Reply", [], "client")

    def test_first_atomic_baseline_preserves_arrival_before_first_event_poll(self):
        self.daemon.add_message(message("old"))
        self.daemon.extra_events_on_baseline = [(message("between"), "live")]
        self.assertEqual(self.backend.poll_once(), 1)
        self.assertEqual([row["payload"]["content"]["str"] for row in self.journal.unacked()], ["hello"])
        self.assertEqual(len(self.journal.unacked()), 1)
        self.assertEqual(self.daemon.acks[-1], "2")
        self.assertEqual(self.backend.poll_once(), 0)

    def test_event_cursor_and_message_commit_are_atomic_before_daemon_ack(self):
        self.backend.poll_once()
        self.daemon.add_message(message("retry-after-disk-failure"))
        with self.journal._lock, self.journal._db:
            self.journal._db.execute("""CREATE TEMP TRIGGER fail_cursor BEFORE UPDATE ON cursors
                WHEN NEW.local_id>OLD.local_id BEGIN SELECT RAISE(ABORT,'simulated disk failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.backend.poll_once()
        self.assertEqual(self.journal.get_cursor(adapter.CURSOR_CHAT, adapter.CURSOR_SHARD), 0)
        self.assertEqual(self.journal.unacked(), [])
        self.assertEqual(self.daemon.acks[-1], "0")
        with self.journal._lock, self.journal._db:
            self.journal._db.execute("DROP TRIGGER fail_cursor")
        self.assertEqual(self.backend.poll_once(), 1)
        self.assertEqual(self.journal.get_cursor(adapter.CURSOR_CHAT, adapter.CURSOR_SHARD), 1)
        self.assertEqual(self.daemon.acks[-1], "1")
        self.assertEqual(len(self.journal.unacked()), 1)

    def test_invalid_or_expired_cursor_never_rebaselines_or_acks_ahead(self):
        self.backend.poll_once()
        self.daemon.add_message(message("pending"))
        self.daemon.event_page_override = {"items": self.daemon.events, "nextCursor": "2"}
        with self.assertRaises(RuntimeError):
            self.backend.poll_once()
        self.assertEqual(self.journal.get_cursor(adapter.CURSOR_CHAT, adapter.CURSOR_SHARD), 0)
        self.assertEqual(self.daemon.acks, ["0"])
        self.daemon.event_page_override = None
        self.daemon.response_status = 409
        self.assertRaises(RuntimeError, self.backend.poll_once)
        self.daemon.response_status = None
        self.backend.poll_once()
        self.assertEqual(len([row for row in self.daemon.requests if row[1].endswith("/threads")]), 1)
        self.assertEqual(len(self.journal.unacked()), 1)

    def test_inbound_exact_allowlists_origins_directions_prefixes_and_owner_dm(self):
        self.backend.poll_once()
        cases = [
            (message("owner"), "live", True),
            (message("outside", chat="wxid_other", sender="wxid_other"), "live", False),
            (message("catchup"), "catchup", False),
            (message("local", chat="filehelper", sender="wxid_self", body="/lb echo", direction="outgoing"), "local_send", False),
            (message("self-other", chat="wxid_owner", sender="wxid_self", direction="outgoing"), "live", False),
            (message("helper", chat="filehelper", sender="wxid_self", body="/lb ask", direction="outgoing"), "live", True),
            (message("helper-blank", chat="filehelper", sender="wxid_self", body="/lb  ", direction="outgoing"), "live", False),
            (message("helper-other", chat="filehelper", sender="wxid_other", body="/lb ask"), "live", False),
            (message("group-no-prefix", chat="room@chatroom", sender="member"), "live", False),
            (message("group", chat="room@chatroom", sender="member", body="/lb group ask"), "live", True),
        ]
        for value, origin, _ in cases:
            self.daemon.add_message(value, origin)
        total = sum(self.backend.poll_once() for _ in range(5))
        self.assertEqual(total, sum(keep for _, _, keep in cases))
        payloads = [row["payload"] for row in self.journal.unacked()]
        self.assertEqual([row["content"]["str"] for row in payloads], ["hello", "ask", "member:\n/lb group ask"])
        self.assertEqual(payloads[0]["from_user_name"], {"str": "wxid_owner"})
        self.assertEqual(len({row["_bridge_event_id"] for row in payloads}), 3)

    def test_message_identity_account_thread_sender_and_direction_mismatch_fail_closed(self):
        self.backend.poll_once()
        for index, changes in enumerate(({"accountId": "other"}, {"threadId": "other"},
                                         {"senderId": "other"}, {"direction": "outgoing"})):
            value = message("bad-%d" % index)
            self.daemon.add_message(value)
            self.daemon.messages[value["id"]].update(changes)
            with self.assertRaises(RuntimeError):
                self.backend.poll_once()
            self.assertEqual(self.journal.unacked(), [])
            self.daemon.messages[value["id"]] = value
            self.backend.poll_once()
            self.journal.ack(self.journal.unacked()[0]["event_id"])

    def test_journal_reopen_resumes_and_legacy_gui_source_is_rejected(self):
        self.backend.poll_once()
        self.daemon.add_message(message("first"))
        self.backend.poll_once()
        identity = self.journal.unacked()[0]["event_id"]
        self.journal.ack(identity)
        self.backend.close()
        self.journal.close()
        self.journal = Journal(Path(self.tmp.name) / "journal")
        self.backend = adapter.AgentWeChatBackend(self.config, self.journal)
        self.daemon.add_message(message("later"))
        self.assertEqual(self.backend.poll_once(), 1)
        self.assertNotEqual(self.journal.unacked()[0]["event_id"], identity)
        legacy = Journal(Path(self.tmp.name) / "legacy")
        try:
            legacy.bind_identity("legacy-gui-source")
            with self.assertRaises(ConflictError):
                adapter.AgentWeChatBackend(self.config, legacy)
        finally:
            legacy.close()

    def test_history_uses_opaque_profile_cursors_and_exact_authorized_groups(self):
        self.daemon.history = [message("history-%d" % number, chat="room@chatroom", sender="member")
                               for number in range(209)]
        rows = self.backend.get_history("room@chatroom", limit=2, offset=203)
        self.assertEqual([row["local_id"] for row in rows], ["history-203", "history-204"])
        self.assertEqual(rows[0]["create_time"], 1767225600)
        self.assertEqual(len(self.daemon.history_page_requests), 2)
        self.assertEqual(self.daemon.history_page_requests[1]["cursor"], ["page-200"])
        groups = self.backend.list_groups(limit=5, allowed_groups=["room@chatroom"])
        self.assertEqual(groups, [{"username": "room@chatroom", "name": "Room"}])
        for call in (lambda: self.backend.get_history("other@chatroom"),
                     lambda: self.backend.list_groups(allowed_groups=["other@chatroom"])):
            with self.assertRaises(PermissionError):
                call()
        self.daemon.member_pages.append([{"id": "member2", "username": "member2", "displayName": None}])
        self.assertEqual(self.backend.chatroom_members("room@chatroom"), [
            {"user_name": "member", "nick_name": "Member"}, {"user_name": "member2", "nick_name": "member2"}])


class ControlledReplayTests(Fixture, unittest.TestCase):
    def setUp(self):
        self.setup_fixture({"initial_event_cursor": "0", "incoming_since": 1767225600})

    def tearDown(self):
        self.close_fixture()

    def test_old_live_and_catchup_are_dropped_and_new_catchup_is_durable_once_across_restart(self):
        for identifier, origin in (("old-live", "live"), ("old-catchup", "catchup")):
            self.daemon.add_message({**message(identifier), "createdAt": "2025-12-31T23:59:59Z"}, origin)
        self.daemon.add_message(message("at-cutoff"), "catchup")
        self.assertEqual(self.backend.poll_once(), 1)
        self.assertEqual(len(self.journal.unacked()), 1)
        first_event_id = self.journal.unacked()[0]["event_id"]
        self.backend.close()
        self.journal.close()
        self.journal = Journal(Path(self.tmp.name) / "journal")
        self.backend = adapter.AgentWeChatBackend(self.config, self.journal)
        self.assertEqual(self.backend.poll_once(), 0)
        self.assertEqual(self.journal.unacked()[0]["event_id"], first_event_id)
        self.daemon.add_message({**message("after-restart"), "createdAt": "2026-01-01T00:00:01Z"}, "catchup")
        self.assertEqual(self.backend.poll_once(), 1)
        self.daemon.add_message(message("at-cutoff"), "catchup")
        self.backend.poll_once()
        self.assertEqual(len(self.journal.unacked()), 2)
        self.assertEqual(len({row["event_id"] for row in self.journal.unacked()}), 2)
        self.assertEqual(self.journal.get_cursor(adapter.CURSOR_CHAT, adapter.CURSOR_SHARD), 5)
        self.assertFalse(any(row[1].endswith("/threads") for row in self.daemon.requests))

    def test_configured_cursor_survives_login_delay_and_precedes_first_poll(self):
        self.backend.close()
        self.journal.close()
        self.config["initial_event_cursor"] = "2"
        self.journal = Journal(Path(self.tmp.name) / "handoff-journal")
        self.backend = adapter.AgentWeChatBackend(self.config, self.journal)
        for identifier in ("before-capture-1", "before-capture-2"):
            self.daemon.add_message(message(identifier), "catchup")
        self.daemon.accounts["authenticated"] = False
        self.assertRaises(RuntimeError, self.backend.poll_once)
        self.assertIsNone(self.journal.get_cursor(adapter.CURSOR_CHAT, adapter.CURSOR_SHARD))
        self.daemon.add_message(message("during-login"), "catchup")
        self.daemon.accounts["authenticated"] = True
        self.assertEqual(self.backend.poll_once(), 1)
        self.assertEqual(len(self.journal.unacked()), 1)
        queries = [row[2] for row in self.daemon.requests if row[1].endswith("/events")]
        self.assertEqual(queries[0]["after"], ["2"])
        self.assertEqual(self.journal.get_cursor(adapter.CURSOR_CHAT, adapter.CURSOR_SHARD), 3)
        self.assertFalse(any(row[1].endswith("/threads") for row in self.daemon.requests))

    def test_replay_options_require_a_valid_pair(self):
        base = {key: value for key, value in self.config.items()
                if key not in ("initial_event_cursor", "incoming_since")}
        invalid = [
            {"initial_event_cursor": "0"}, {"incoming_since": 1767225600},
            {"initial_event_cursor": 0, "incoming_since": 1767225600},
            {"initial_event_cursor": "-1", "incoming_since": 1767225600},
            {"initial_event_cursor": "1e0", "incoming_since": 1767225600},
            {"initial_event_cursor": str(2 ** 63), "incoming_since": 1767225600},
            {"initial_event_cursor": "0", "incoming_since": True},
            {"initial_event_cursor": "0", "incoming_since": 1.5},
            {"initial_event_cursor": "0", "incoming_since": 0},
            {"initial_event_cursor": "0", "incoming_since": int(time.time()) + 3600},
        ]
        for options in invalid:
            with self.subTest(options=options), self.assertRaises(ValueError):
                adapter.validate_config({**base, **options})
        normalized = adapter.validate_config({**self.config, "initial_event_cursor": "0002"})
        self.assertEqual(normalized["initial_event_cursor"], "2")

    def test_replay_cutoff_and_initial_cursor_are_bound_to_the_journal_source(self):
        for changes in ({"incoming_since": 1767225601}, {"initial_event_cursor": "1"}):
            with self.subTest(changes=changes), self.assertRaises(ConflictError):
                adapter.AgentWeChatBackend({**self.config, **changes}, self.journal)
        original_default = {key: value for key, value in self.config.items()
                            if key not in ("initial_event_cursor", "incoming_since")}
        with self.assertRaises(ConflictError):
            adapter.AgentWeChatBackend(original_default, self.journal)

    def test_catchup_retains_allowlist_direction_and_prefix_gates_and_excludes_local_echo(self):
        cases = [
            (message("local", chat="filehelper", sender="wxid_self", body="/lb echo", direction="outgoing"), "local_send"),
            (message("outside", chat="wxid_other", sender="wxid_other"), "catchup"),
            (message("outgoing", chat="wxid_owner", sender="wxid_self", direction="outgoing"), "catchup"),
            (message("no-prefix", chat="room@chatroom", sender="member"), "catchup"),
            (message("group", chat="room@chatroom", sender="member", body="/lb group"), "catchup"),
            (message("helper", chat="filehelper", sender="wxid_self", body="/lb helper", direction="outgoing"), "catchup"),
        ]
        for value, origin in cases:
            self.daemon.add_message(value, origin)
        self.assertEqual(sum(self.backend.poll_once() for _ in range(2)), 2)
        self.assertEqual([row["payload"]["content"]["str"] for row in self.journal.unacked()],
                         ["member:\n/lb group", "helper"])

    def test_history_still_returns_records_older_than_the_routing_cutoff(self):
        self.daemon.history = [{**message("old-history", chat="room@chatroom", sender="member"),
                                "createdAt": "2025-12-31T23:59:59Z"}]
        rows = self.backend.get_history("room@chatroom")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["create_time"], 1767225599)

    def test_expired_replay_cursor_does_not_reset_or_ack_past_the_gap(self):
        self.backend.poll_once()
        self.daemon.add_message(message("while-offline"), "catchup")
        self.daemon.event_status = 409
        with self.assertRaises(RuntimeError):
            self.backend.poll_once()
        self.assertEqual(self.journal.get_cursor(adapter.CURSOR_CHAT, adapter.CURSOR_SHARD), 0)
        self.assertEqual(self.daemon.acks, ["0"])
        self.assertEqual(self.journal.unacked(), [])
        self.assertFalse(any(row[1].endswith("/threads") for row in self.daemon.requests))
        self.daemon.event_status = 200
        self.assertEqual(self.backend.poll_once(), 1)


class RuntimeStartupTests(Fixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.setup_fixture()
        self.daemon.accounts["contactSyncReady"] = False
        config = {**self.config, "poll_interval": .1}
        self.runner = web.AppRunner(adapter.create_runtime_app(config, backend=self.backend, journal=self.journal))
        self.startup = asyncio.create_task(self.runner.setup())

    async def asyncTearDown(self):
        if not self.startup.done():
            self.startup.cancel()
        await asyncio.gather(self.startup, return_exceptions=True)
        await self.runner.cleanup()
        await asyncio.to_thread(self.close_fixture)

    async def wait_for_request(self, path):
        async def observed():
            while not any(request[1] == path for request in self.daemon.requests):
                await asyncio.sleep(.005)

        await asyncio.wait_for(observed(), 2)

    def assert_journal_released(self):
        reopened = Journal(Path(self.tmp.name) / "journal")
        reopened.close()

    async def test_setup_waits_for_verified_account_before_listener_and_captures_baseline(self):
        await self.wait_for_request("/v1/accounts/account")
        self.assertFalse(self.startup.done())
        self.assertIsNone(self.runner.server)
        self.assertFalse(self.runner.sites)
        self.assertIsNone(self.journal.get_cursor(adapter.CURSOR_CHAT, adapter.CURSOR_SHARD))
        self.daemon.extra_events_on_baseline = [(message("during-startup-baseline"), "live")]
        self.daemon.accounts["contactSyncReady"] = True
        await asyncio.wait_for(self.startup, 3)
        self.assertEqual(self.journal.get_cursor(adapter.CURSOR_CHAT, adapter.CURSOR_SHARD), 1)
        self.assertEqual(len(self.journal.unacked()), 1)
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        async with ClientSession() as client:
            response = await client.get("http://127.0.0.1:%d/health" % port)
            self.assertEqual(response.status, 200)
            self.assertTrue((await response.json())["ready"])

    async def test_cancelled_setup_closes_backend_and_releases_journal_before_first_yield(self):
        await self.wait_for_request("/v1/accounts/account")
        self.assertFalse(self.startup.done())
        self.startup.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await self.startup
        self.assertTrue(self.backend._closed)
        self.assert_journal_released()

    async def test_cancelled_setup_drains_inflight_http_without_blocking_the_event_loop(self):
        self.daemon.delay = .2
        await self.wait_for_request("/v1/status")
        started = time.monotonic()
        self.startup.cancel()
        await asyncio.sleep(.02)
        self.assertLess(time.monotonic() - started, .15)
        with self.assertRaises(asyncio.CancelledError):
            await self.startup
        self.assertTrue(self.backend._closed)
        self.assert_journal_released()


class GatewayContractTests(Fixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.setup_fixture()
        self.client = TestClient(TestServer(create_app(self.backend, self.journal, "bridge-secret")))
        await self.client.start_server()

    async def asyncTearDown(self):
        if hasattr(self, "client"):
            await self.client.close()
        await asyncio.to_thread(self.close_fixture)

    async def send(self, client_id="request", text="Reply"):
        response = await self.client.post("/message/SendTextMessage?key=bridge-secret", json={
            "ClientMsgID": client_id, "MsgItem": [{"ToUserName": "filehelper", "TextContent": text,
                                                     "AtWxIDList": []}]})
        return response.status, await response.json()

    async def test_gateway_deduplicates_confirmed_and_needs_review_sends(self):
        first = await self.send()
        self.assertEqual(first[0], 200)
        self.assertEqual(first[1]["Data"][0]["NewMsgId"], "1234567890123456789")
        self.assertEqual(await self.send(), first)
        self.assertEqual(len(self.daemon.sends), 1)
        self.daemon.send_states = ["needs_review"]
        for _ in range(2):
            status, body = await self.send("uncertain")
            self.assertEqual(status, 409)
            self.assertEqual(body["Data"]["status"], "uncertain")
        self.assertEqual(len(self.daemon.sends), 2)
        await self.client.close()
        self.backend.close()
        self.journal.close()
        self.journal = Journal(Path(self.tmp.name) / "journal")
        self.backend = adapter.AgentWeChatBackend(self.config, self.journal)
        self.client = TestClient(TestServer(create_app(self.backend, self.journal, "bridge-secret")))
        await self.client.start_server()
        self.assertEqual((await self.send("uncertain"))[0], 409)
        self.assertEqual(await self.send(), first)
        self.assertEqual(len(self.daemon.sends), 2)

    async def test_gateway_websocket_ack_keeps_protocol_event_durable_until_langbot_ack(self):
        await asyncio.to_thread(self.backend.poll_once)
        self.daemon.add_message(message("new-incoming"))
        await asyncio.to_thread(self.backend.poll_once)
        ws = await self.client.ws_connect("/ws/GetSyncMsg?key=bridge-secret")
        payload = await ws.receive_json(timeout=3)
        self.assertEqual(payload["from_user_name"], {"str": "wxid_owner"})
        self.assertEqual(self.daemon.acks[-1], "1")
        self.assertEqual(len(self.journal.unacked()), 1)
        await ws.send_json({"type": "ack", "event_id": payload["_bridge_event_id"]})
        for _ in range(100):
            if not self.journal.unacked():
                break
            await asyncio.sleep(.01)
        self.assertEqual(self.journal.unacked(), [])
        await ws.close()

    async def test_history_gateway_preserves_independent_owner_and_current_group_authorization(self):
        await self.client.close()
        history = {"token": "independent-history-secret-at-least-32-bytes",
                   "owners": [{"bot_uuid": "native-bot", "launcher_id": "wxid_owner"}],
                   "allowed_groups": ["room@chatroom", "other@chatroom"]}
        self.backend.close()
        self.backend = adapter.AgentWeChatBackend({**self.config, "native_history": history}, self.journal)
        self.client = TestClient(TestServer(create_app(self.backend, self.journal, "bridge-secret", history_config=history)))
        await self.client.start_server()
        self.daemon.history = [message("history", chat="room@chatroom", sender="member")]
        request = {"group_username": "room@chatroom", "start_time": 1767225500, "end_time": 1767225700}
        path = "/native-history/v1/messages"
        response = await self.client.post(path + "?key=bridge-secret", json=request)
        self.assertEqual(response.status, 401)
        headers = {"Authorization": "Bearer " + history["token"], "X-LangBot-Bot-UUID": "native-bot",
                   "X-LangBot-Launcher-ID": "wxid_owner", "X-LangBot-Launcher-Type": "person"}
        response = await self.client.post(path, headers=headers, json=request)
        result = await response.json()
        self.assertEqual(response.status, 200)
        self.assertEqual(result["access_scope"], "owner_private")
        self.assertEqual(result["messages"][0]["text"], "hello")
        headers.update({"X-LangBot-Launcher-ID": "room@chatroom", "X-LangBot-Launcher-Type": "group"})
        response = await self.client.post(path, headers=headers, json=request)
        self.assertEqual((await response.json())["access_scope"], "current_group")
        response = await self.client.post(path, headers=headers, json={**request, "group_username": "other@chatroom"})
        self.assertEqual(response.status, 403)
        self.assertFalse(any(row[1].endswith("/threads/other@chatroom/messages") for row in self.daemon.requests))


if __name__ == "__main__":
    unittest.main()
