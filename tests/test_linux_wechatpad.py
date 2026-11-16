"""兼容网关的离线 HTTP、WebSocket 和持久化验收。"""

import asyncio
import base64
import importlib
import io
from pathlib import Path
import stat
import sqlite3
import tempfile
import threading
import time
import unittest

from aiohttp import WSServerHandshakeError, WSMsgType
from aiohttp.test_utils import TestClient, TestServer

try:
    gateway = importlib.import_module("wechatauto.bridge.wechatpad")
    journal_module = importlib.import_module("wechatauto.bridge.journal")
except ModuleNotFoundError:
    gateway = journal_module = None


class Backend:
    def __init__(self):
        self.calls = []
        self.confirmed = True
        self.error = False
        self.active = self.maximum = 0
        self.threads = []

    def profile(self):
        self.threads.append(threading.get_ident())
        return {"wxid": "wxid_self", "nickname": "测试助手"}

    def is_logged_in(self):
        return True

    def chatroom_members(self, room):
        return [{"user_name": "wxid_member", "nick_name": "成员"}]

    def send_text(self, to, text, ats, client_id):
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        self.calls.append((to, text, ats, client_id))
        self.threads.append(threading.get_ident())
        time.sleep(0.015)
        self.active -= 1
        if self.error:
            raise RuntimeError("含敏感信息的后端故障")
        return {"confirmed": self.confirmed, "msg_id": "msg-123"}

    def send_image(self, to, image, ats, client_id):
        return self.send_text(to, image, ats, client_id)


def text_body(client_id="request-1", text="hello"):
    return {"ClientMsgID": client_id, "MsgItem": [{
        "AtWxIDList": [], "ImageContent": "", "MsgType": 0,
        "TextContent": text, "ToUserName": "filehelper",
    }]}


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(journal_module, "durable bridge Journal is missing")
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name) / "private"
        self.journal = journal_module.Journal(self.directory)

    def tearDown(self):
        if hasattr(self, "journal"):
            self.journal.close()
            self.tmp.cleanup()

    def test_private_directory_and_database_permissions(self):
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.journal.path.stat().st_mode), 0o600)

    def test_rejects_shared_directory_and_symlink(self):
        shared = Path(self.tmp.name) / "shared"
        shared.mkdir(mode=0o755)
        with self.assertRaises(PermissionError):
            journal_module.Journal(shared)
        link = Path(self.tmp.name) / "link"
        link.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(PermissionError):
            journal_module.Journal(link)

    def test_events_survive_reopen_and_ack_and_keyset_pagination(self):
        for number in range(205):
            self.journal.enqueue_inbound(str(number), {"content": {"str": str(number)}})
        self.journal.enqueue_inbound("0", {"content": {"str": "0"}})
        first = self.journal.unacked(limit=100)
        self.assertEqual(len(first), 100)
        second = self.journal.unacked(after=first[-1]["seq"], limit=100)
        self.assertEqual(second[0]["event_id"], "100")
        self.assertTrue(self.journal.ack("0"))
        self.journal.close()
        self.journal = journal_module.Journal(self.directory)
        self.assertEqual(len(self.journal.unacked(limit=500)), 204)
        self.assertEqual(self.journal.unacked(limit=1)[0]["event_id"], "1")
        self.assertFalse(self.journal.ack("unknown"))

    def test_same_event_id_with_different_payload_fails(self):
        self.journal.enqueue_inbound("same", {"value": 1})
        with self.assertRaises(journal_module.ConflictError):
            self.journal.enqueue_inbound("same", {"value": 2})

    def test_interrupted_send_becomes_uncertain_on_reopen(self):
        claimed = self.journal.claim_outbox("request", {"text": "hello"})
        self.assertTrue(claimed["claimed"])
        self.assertEqual(claimed["status"], "sending")
        self.journal.close()
        self.journal = journal_module.Journal(self.directory)
        repeated = self.journal.claim_outbox("request", {"text": "hello"})
        self.assertFalse(repeated["claimed"])
        self.assertEqual(repeated["status"], "uncertain")

    def test_second_active_journal_cannot_recover_inflight_send(self):
        self.journal.claim_outbox("request", {"text": "hello"})
        with self.assertRaises(RuntimeError):
            journal_module.Journal(self.directory)
        self.assertEqual(self.journal.get_outbox("request")["status"], "sending")

    def test_confirmed_receipt_survives_reopen(self):
        self.journal.claim_outbox("request", {"text": "hello"})
        receipt = {"confirmed": True, "msg_id": "msg-123"}
        self.journal.finish_outbox("request", receipt)
        self.journal.close()
        self.journal = journal_module.Journal(self.directory)
        result = self.journal.claim_outbox("request", {"text": "hello"})
        self.assertEqual(result, {"claimed": False, "status": "sent", "receipt": receipt})

    def test_inbound_event_size_is_bounded(self):
        with self.assertRaises(ValueError):
            self.journal.enqueue_inbound("huge", {"content": "x" * journal_module.MAX_EVENT_BYTES})

    def test_failed_open_releases_process_lock(self):
        path = self.journal.path
        self.journal.close()
        path.write_bytes(b"not a sqlite database")
        with self.assertRaises(sqlite3.DatabaseError):
            journal_module.Journal(self.directory)
        path.unlink()
        self.journal = journal_module.Journal(self.directory)
        self.assertEqual(self.journal.counts()["inbox_unacked"], 0)


class EncodingTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(gateway, "WeChatPad gateway is missing")

    def test_native_group_record_has_sender_prefix_and_stable_id(self):
        record = {"username": "room@chatroom", "sender_username": "wxid_member",
                  "content": "hi", "type": "文本", "create_time": 123,
                  "shard": "message_1.db", "local_id": 5, "sort_seq": 7}
        message = gateway.encode_native_record(record, "wxid_self")
        self.assertEqual(message["from_user_name"], {"str": "room@chatroom"})
        self.assertEqual(message["to_user_name"], {"str": "wxid_self"})
        self.assertEqual(message["content"], {"str": "wxid_member:\nhi"})
        self.assertEqual(message["msg_type"], 1)
        self.assertEqual(message, gateway.encode_native_record(record, "wxid_self"))
        self.assertNotEqual(message["_bridge_event_id"],
                            gateway.encode_native_record(record, "other_account")["_bridge_event_id"])

    def test_group_without_resolved_sender_is_rejected(self):
        with self.assertRaises(ValueError):
            gateway.encode_native_record({"username": "room@chatroom", "content": "x",
                                          "type": "文本", "create_time": 1}, "wxid_self")


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertIsNotNone(gateway, "WeChatPad gateway is missing")
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = journal_module.Journal(Path(self.tmp.name) / "private")
        self.backend = Backend()
        self.client = TestClient(TestServer(gateway.create_app(self.backend, self.journal, "test-secret")))
        await self.client.start_server()

    async def asyncTearDown(self):
        if hasattr(self, "client"):
            await self.client.close()
            self.journal.close()
            self.tmp.cleanup()

    async def send(self, body, kind="Text"):
        response = await self.client.post(f"/message/Send{kind}Message?key=test-secret", json=body)
        return response, await response.json()

    async def test_auth_covers_http_websocket_and_unknown_routes(self):
        for path in ("/login/GetLoginStatus", "/user/GetProfile", "/unsupported", "/ws/GetSyncMsg"):
            for suffix in ("", "?key=wrong"):
                response = await self.client.get(path + suffix)
                self.assertEqual(response.status, 401)
        with self.assertRaises(WSServerHandshakeError):
            await self.client.ws_connect("/GetSyncMsg?key=wrong")
        response = await self.client.get("/health")
        health = await response.json()
        self.assertEqual(response.status, 200)
        self.assertNotIn("test-secret", str(health))
        self.assertNotIn("wxid_self", str(health))

    async def test_login_profile_and_sdk_chatroom_shape(self):
        status = await self.client.get("/login/GetLoginStatus?key=test-secret")
        self.assertEqual((await status.json())["Code"], 200)
        profile = await self.client.get("/user/GetProfile?key=test-secret")
        self.assertEqual((await profile.json())["Data"]["userInfo"]["userName"], {"str": "wxid_self"})
        self.assertNotEqual(self.backend.threads[-1], threading.get_ident())
        for path in ("/group/GetChatroomMemberDetail", "/chatroom/GetChatRoomMemberDetail"):
            response = await self.client.post(path + "?key=test-secret", json={"ChatRoomName": "room@chatroom"})
            self.assertEqual((await response.json())["Data"]["member_data"]["chatroom_member_list"],
                             [{"user_name": "wxid_member", "nick_name": "成员"}])

    async def test_confirmed_send_deduplicates_after_journal_reopen(self):
        response, body = await self.send(text_body())
        self.assertEqual(response.status, 200)
        self.assertEqual(body["Code"], 200)
        self.assertEqual(body["Data"][0]["NewMsgId"], "msg-123")
        response2, body2 = await self.send(text_body())
        self.assertEqual(body2, body)
        self.assertEqual(len(self.backend.calls), 1)
        self.assertNotEqual(self.backend.threads[-1], threading.get_ident())
        self.assertEqual(self.journal.get_outbox("request-1")["status"], "sent")
        await self.client.close()
        directory = self.journal.directory
        self.journal.close()
        self.journal = journal_module.Journal(directory)
        self.client = TestClient(TestServer(gateway.create_app(self.backend, self.journal, "test-secret")))
        await self.client.start_server()
        response3, body3 = await self.send(text_body())
        self.assertEqual(body3, body)
        self.assertEqual(len(self.backend.calls), 1)

    async def test_reused_client_id_with_different_request_is_conflict(self):
        await self.send(text_body())
        response, body = await self.send(text_body(text="different"))
        self.assertEqual(response.status, 409)
        self.assertNotEqual(body["Code"], 200)
        self.assertEqual(len(self.backend.calls), 1)

    async def test_unconfirmed_send_is_uncertain_and_never_blindly_retried(self):
        self.backend.confirmed = False
        for _ in range(2):
            response, body = await self.send(text_body())
            self.assertEqual(response.status, 409)
            self.assertEqual(body["Data"]["status"], "uncertain")
            self.assertNotEqual(body["Code"], 200)
        self.assertEqual(len(self.backend.calls), 1)

    async def test_backend_exception_is_uncertain_without_exception_leak(self):
        self.backend.error = True
        response, body = await self.send(text_body())
        self.assertEqual(response.status, 409)
        self.assertEqual(body["Data"]["status"], "uncertain")
        self.assertNotIn("敏感", str(body))
        await self.send(text_body())
        self.assertEqual(len(self.backend.calls), 1)

    async def test_dispatch_logs_only_exception_type_and_code_without_sensitive_body(self):
        secret = "secret-exception-body https://private.invalid/?key=test-secret private-message-body"
        coded = RuntimeError(secret)
        coded.code = "GUI_ACCOUNT_UNVERIFIED"
        calls = []
        for index, error in enumerate((coded, TimeoutError(secret))):
            with self.subTest(kind=type(error).__name__):
                def _command():
                    raise error

                def _snapshot_data():
                    return _command()

                def _coordinate_view():
                    return _snapshot_data()

                def _prepare_coordinates():
                    return _coordinate_view()

                def fail(*args, error=error):
                    calls.append(args)
                    return _prepare_coordinates()

                self.backend.send_text = fail
                request = text_body("private-client-%d" % index, "private-message-body")
                with self.assertLogs("wechatauto.bridge.wechatpad", level="WARNING") as logs:
                    response, body = await self.send(request)
                self.assertEqual(response.status, 409)
                self.assertEqual(body["Data"]["status"], "uncertain")
                self.assertEqual(self.journal.get_outbox(request["ClientMsgID"])["status"], "uncertain")
                self.assertEqual(len(logs.records), 1)
                prefix = "bridge dispatch failed exception_type=%s code=%s frames=" % (
                    type(error).__name__, getattr(error, "code", "unclassified"))
                diagnostic = logs.records[0].getMessage()
                self.assertTrue(diagnostic.startswith(prefix))
                frames = diagnostic[len(prefix):].split(">")
                expected_frames = [["_prepare_coordinates", "_coordinate_view", "_snapshot_data", "_command"]]
                if isinstance(error, TimeoutError):
                    # Python 3.12 的线程 Future 可能重建 TimeoutError，丢弃线程内堆栈。
                    expected_frames.append(["dispatch", "to_thread"])
                self.assertIn([frame.split(":")[0] for frame in frames], expected_frames)
                self.assertTrue(all(frame.split(":")[1].isdigit() for frame in frames))
                self.assertIsNone(logs.records[0].exc_info)
                for value in (secret, "private.invalid", "test-secret", "private-message-body",
                              request["ClientMsgID"], "filehelper", __file__, ".py", "Traceback"):
                    self.assertNotIn(value, str(logs.output))
                await self.send(request)
                self.assertEqual(len(calls), index + 1)

    async def test_concurrent_distinct_requests_serialize_gui_sends(self):
        results = await asyncio.gather(*(self.send(text_body(f"id-{i}")) for i in range(6)))
        self.assertTrue(all(response.status == 200 for response, _ in results))
        self.assertEqual(self.backend.maximum, 1)

    async def test_image_request_uses_sdk_payload_and_item_client_id(self):
        image = base64.b64encode(b"synthetic-image").decode()
        body = text_body()
        del body["ClientMsgID"]
        body["MsgItem"][0].update({"TextContent": "", "ImageContent": image, "ClientMsgID": "image-1"})
        response, result = await self.send(body, "Image")
        self.assertEqual(result["Code"], 200)
        self.assertEqual(self.backend.calls, [("filehelper", image, [], "image-1")])

    async def test_invalid_payloads_and_unsupported_api_fail_before_sending(self):
        for body in ({}, {"MsgItem": []}, {"MsgItem": [{}, {}]}, text_body(text="x" * 32769)):
            response, result = await self.send(body)
            self.assertEqual(response.status, 400)
            self.assertNotEqual(result["Code"], 200)
        response = await self.client.post("/message/SendVoice?key=test-secret", json={})
        self.assertEqual(response.status, 501)
        self.assertEqual(self.backend.calls, [])

    async def test_http_payload_is_bounded(self):
        response = await self.client.post("/message/SendTextMessage?key=test-secret",
                                          data=io.BytesIO(b"x" * (gateway.MAX_HTTP_BYTES + 1)))
        self.assertEqual(response.status, 413)
        self.assertEqual(self.backend.calls, [])

    async def test_ws_replays_all_pages_ack_persists_and_live_events_arrive(self):
        for number in range(205):
            self.journal.enqueue_inbound(str(number), {"content": {"str": str(number)}})
        ws = await self.client.ws_connect("/ws/GetSyncMsg?key=test-secret")
        received = [await ws.receive_json(timeout=3) for _ in range(205)]
        self.assertEqual([item["_bridge_event_id"] for item in received], [str(i) for i in range(205)])
        for number in range(204):
            await ws.send_json({"type": "ack", "event_id": str(number)})
        for _ in range(100):
            if len(self.journal.unacked(limit=500)) == 1:
                break
            await asyncio.sleep(.01)
        await ws.close()
        ws = await self.client.ws_connect("/GetSyncMsg?key=test-secret")
        self.assertEqual((await ws.receive_json(timeout=3))["_bridge_event_id"], "204")
        self.journal.enqueue_inbound("new", {"content": {"str": "new"}})
        self.assertEqual((await ws.receive_json(timeout=3))["_bridge_event_id"], "new")
        await ws.close()

    async def test_ws_rejects_malformed_ack(self):
        ws = await self.client.ws_connect("/GetSyncMsg?key=test-secret")
        await ws.send_json({"type": "ack", "event_id": {"bad": "value"}})
        result = await ws.receive(timeout=3)
        self.assertIn(result.type, (WSMsgType.CLOSE, WSMsgType.CLOSED))
        await ws.close()

    async def test_websocket_ack_frame_is_bounded(self):
        ws = await self.client.ws_connect("/GetSyncMsg?key=test-secret")
        await ws.send_str("x" * (gateway.MAX_WS_BYTES + 1))
        result = await ws.receive(timeout=3)
        self.assertEqual(result.type, WSMsgType.CLOSE)
        self.assertEqual(result.data, 1009)
        await ws.close()

    async def test_replay_json_is_under_langbot_character_limit_for_unicode(self):
        self.journal.enqueue_inbound("unicode", {"content": {"str": "😀" * 120000}})
        ws = await self.client.ws_connect("/GetSyncMsg?key=test-secret")
        result = await ws.receive(timeout=3)
        self.assertEqual(result.type, WSMsgType.TEXT)
        self.assertLessEqual(len(result.data), 1024 * 1024)
        await ws.close()

    async def test_default_access_log_omits_query_credentials_and_path(self):
        with self.assertLogs("aiohttp.access", level="INFO") as logs:
            response = await self.client.get("/token-in-path?key=test-secret")
            await response.read()
            await asyncio.sleep(.01)
        self.assertNotIn("test-secret", str(logs.output))
        self.assertNotIn("token-in-path", str(logs.output))


if __name__ == "__main__":
    unittest.main()
