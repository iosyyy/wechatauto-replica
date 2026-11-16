"""群历史 capability 的离线合成 HTTP 验收，不读取微信账号或数据库。"""

import copy
import time
import unittest

from aiohttp.test_utils import TestClient, TestServer

from wechatauto.bridge.history import MAX_MESSAGE_BYTES, MAX_RESPONSE_BYTES, validate_history_config
from wechatauto.bridge.wechatpad import create_app


TOKEN = "synthetic-history-token-1234567890123456"
CONFIG = {"token": TOKEN, "owners": [{"bot_uuid": "test-bot", "launcher_id": "wxid_owner"}],
          "allowed_groups": ["one@chatroom", "two@chatroom"]}
HEADERS = {"Authorization": "Bearer " + TOKEN, "X-LangBot-Bot-UUID": "test-bot",
           "X-LangBot-Launcher-ID": "wxid_owner", "X-LangBot-Launcher-Type": "person"}
GROUP_HEADERS = {**HEADERS, "X-LangBot-Launcher-Type": "group", "X-LangBot-Launcher-ID": "one@chatroom"}


class Backend:
    def __init__(self):
        self.calls = []
        self.rows = []
        self.groups = [{"username": "one@chatroom", "name": "同名群"},
                       {"username": "two@chatroom", "name": "同名群"}]
        self.error = False

    def list_groups(self, limit, offset, allowed_groups):
        self.calls.append(("groups", limit, offset, allowed_groups))
        if self.error:
            raise RuntimeError(TOKEN + " sensitive error")
        return [row for row in self.groups if row["username"] in allowed_groups][offset:offset + limit]

    def get_history(self, group, limit, offset):
        self.calls.append(("messages", group, limit, offset))
        if self.error:
            raise RuntimeError(TOKEN + " sensitive message body")
        return self.rows[offset:offset + limit]


class ConfigTests(unittest.TestCase):
    def test_absent_config_disables_and_empty_group_allowlist_is_valid(self):
        self.assertIsNone(validate_history_config(None))
        config = copy.deepcopy(CONFIG)
        config["allowed_groups"] = []
        self.assertEqual(validate_history_config(config)["allowed_groups"], [])

    def test_invalid_or_shared_credentials_and_nonexact_bindings_fail_closed(self):
        for changes in ({"token": "short"}, {"owners": []}, {"allowed_groups": ["*"]},
                        {"allowed_groups": ["wxid_private"]}, {"extra": True},
                        {"owners": [{"bot_uuid": "test-bot", "launcher_id": "owner", "type": "group"}]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_history_config({**CONFIG, **changes})
        with self.assertRaises(ValueError):
            validate_history_config(CONFIG, TOKEN)


class HistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = Backend()
        self.client = TestClient(TestServer(create_app(self.backend, None, "bridge-key", history_config=CONFIG)))
        await self.client.start_server()
        self.end = int(time.time())

    async def asyncTearDown(self):
        await self.client.close()

    async def request(self, path="messages", body=None, headers=None):
        if body is None:
            body = {"group_username": "one@chatroom", "start_time": self.end - 3600, "end_time": self.end}
        response = await self.client.post("/native-history/v1/" + path, json=body,
                                          headers=HEADERS if headers is None else headers)
        return response, await response.json()

    def row(self, number, content="合成内容", created=None):
        return {"local_id": number, "sort_seq": 10000 - number, "shard": "synthetic.db",
                "sender_username": "wxid_member", "type": "文本", "content": content,
                "create_time": self.end - 10 if created is None else created}

    async def test_disabled_routes_return_404_even_with_bridge_key(self):
        client = TestClient(TestServer(create_app(self.backend, None, "bridge-key")))
        await client.start_server()
        try:
            response = await client.post("/native-history/v1/groups?key=bridge-key", json={}, headers=HEADERS)
            self.assertEqual(response.status, 404)
            self.assertEqual(self.backend.calls, [])
        finally:
            await client.close()

    async def test_history_requires_dedicated_bearer_and_exact_private_owner_headers(self):
        for headers, status in (({}, 401), ({"Authorization": "Bearer bridge-key"}, 401),
                                ({**HEADERS, "X-LangBot-Launcher-ID": "other@chatroom"}, 403),
                                ({**HEADERS, "X-LangBot-Bot-UUID": "other"}, 403),
                                ({**HEADERS, "X-LangBot-Launcher-Type": "unknown"}, 403),
                                ({key: value for key, value in HEADERS.items()
                                  if key != "X-LangBot-Launcher-Type"}, 403)):
            response, result = await self.request("groups", {}, headers)
            self.assertEqual(response.status, status)
            self.assertNotIn(TOKEN, str(result))
        response = await self.client.post("/native-history/v1/groups?key=" + TOKEN, json={})
        self.assertEqual(response.status, 401)
        self.assertEqual(self.backend.calls, [])

    async def test_allowed_group_listing_returns_only_current_group(self):
        response, result = await self.request("groups", {}, GROUP_HEADERS)
        self.assertEqual(response.status, 200)
        self.assertEqual(result["groups"], [self.backend.groups[0]])
        self.assertEqual(result["access_scope"], "current_group")
        self.assertEqual(self.backend.calls[-1][-1], ["one@chatroom"])
        self.assertFalse(result["has_more"])
        self.assertNotIn("two@chatroom", str(result))

    async def test_group_history_defaults_to_current_room_and_never_reads_another_room(self):
        self.backend.rows = [self.row(1, "current-group evidence")]
        body = {"start_time": self.end - 100, "end_time": self.end}
        response, result = await self.request(body=body, headers=GROUP_HEADERS)
        self.assertEqual(response.status, 200)
        self.assertEqual(result["group_username"], "one@chatroom")
        self.assertEqual(result["access_scope"], "current_group")
        self.assertEqual(result["messages"][0]["text"], "current-group evidence")
        self.assertEqual(self.backend.calls[-1][1], "one@chatroom")
        self.backend.calls.clear()
        for group in ("two@chatroom", "同名群", "wxid_owner"):
            response, result = await self.request(body={**body, "group_username": group}, headers=GROUP_HEADERS)
            self.assertEqual(response.status, 403)
            self.assertNotIn("evidence", str(result))
        self.assertEqual(self.backend.calls, [])

    async def test_group_unknown_bot_nonallowlisted_room_and_person_spoofing_are_denied(self):
        for changes in ({"X-LangBot-Bot-UUID": "other-bot"},
                        {"X-LangBot-Launcher-ID": "other@chatroom"},
                        {"X-LangBot-Launcher-ID": "wxid_owner"},
                        {"X-LangBot-Launcher-Type": "person"}):
            response, _ = await self.request("groups", {}, {**GROUP_HEADERS, **changes})
            self.assertEqual(response.status, 403)
        response, _ = await self.request("groups", {"launcher_id": "wxid_owner", "launcher_type": "person"}, GROUP_HEADERS)
        self.assertEqual(response.status, 400)
        self.assertEqual(self.backend.calls, [])

    async def test_group_backend_scope_violation_does_not_return_other_allowlisted_room(self):
        self.backend.list_groups = lambda **kwargs: [self.backend.groups[1]]
        response, result = await self.request("groups", {}, GROUP_HEADERS)
        self.assertEqual(response.status, 503)
        self.assertNotIn("two@chatroom", str(result))

    async def test_other_group_and_private_chat_are_denied_before_backend(self):
        for group in ("other@chatroom", "wxid_owner", "同名群", "../../database"):
            response, _ = await self.request(body={"group_username": group})
            self.assertEqual(response.status, 403)
        self.assertEqual(self.backend.calls, [])

    async def test_empty_group_list_is_honest_and_does_not_expand_scope(self):
        self.backend.groups = []
        response, result = await self.request("groups", {})
        self.assertEqual(response.status, 200)
        self.assertEqual(result["groups"], [])
        self.assertFalse(result["has_more"])
        self.assertEqual(result["completeness"], "local_only")
        self.assertIn("empty page", result["note"])
        self.assertEqual(self.backend.calls[-1][-1], CONFIG["allowed_groups"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_group_page_is_bounded_and_backend_scope_violation_fails(self):
        response, result = await self.request("groups", {"limit": 1})
        self.assertEqual(response.status, 200)
        self.assertEqual(len(result["groups"]), 1)
        self.assertEqual(result["next_offset"], 1)
        self.backend.list_groups = lambda **kwargs: [{"username": "other@chatroom", "name": "secret"}]
        response, result = await self.request("groups", {})
        self.assertEqual(response.status, 503)
        self.assertNotIn("secret", str(result))

    async def test_invalid_request_fields_and_bounds_are_rejected_without_reads(self):
        base = {"group_username": "one@chatroom", "start_time": self.end - 100, "end_time": self.end}
        for changes in ({"limit": True}, {"limit": 51}, {"offset": 1001}, {"offset": -1},
                        {"start_time": self.end}, {"start_time": self.end - 604801},
                        {"end_time": self.end + 3600}, {"sql": "select *"}, {"path": "/tmp/x"}):
            response, _ = await self.request(body={**base, **changes})
            self.assertEqual(response.status, 400, changes)
        self.assertEqual(self.backend.calls, [])

    async def test_body_size_is_bounded(self):
        response, _ = await self.request("groups", {"padding": "x" * 5000})
        self.assertEqual(response.status, 413)
        self.assertEqual(self.backend.calls, [])

    async def test_history_page_preserves_real_rows_and_time_window_is_explicit(self):
        self.backend.rows = [self.row(1, created=self.end + 1), self.row(2), self.row(3)]
        response, result = await self.request(body={"group_username": "one@chatroom", "limit": 2,
                                                  "start_time": self.end - 100, "end_time": self.end})
        self.assertEqual(response.status, 200)
        self.assertEqual([row["text"] for row in result["messages"]], ["合成内容"])
        self.assertEqual(result["inspected_count"], 2)
        self.assertEqual(result["next_offset"], 2)
        self.assertEqual(result["time_filter"], "applied_to_inspected_page")
        self.assertEqual(self.backend.calls[-1], ("messages", "one@chatroom", 3, 0))
        self.assertNotIn("synthetic.db", str(result))

    async def test_empty_time_filtered_page_has_next_without_false_empty_period_claim(self):
        self.backend.rows = [self.row(1, created=self.end + 1), self.row(2)]
        response, result = await self.request(body={"group_username": "one@chatroom", "limit": 1,
                                                  "start_time": self.end - 100, "end_time": self.end})
        self.assertEqual(result["messages"], [])
        self.assertEqual(result["next_offset"], 1)
        self.assertIn("inspected_page", result["status"])

    async def test_text_and_total_bytes_are_bounded_without_skipping_next_record(self):
        self.backend.rows = [self.row(number, "鱼" * 5000) for number in range(51)]
        response, result = await self.request()
        self.assertEqual(response.status, 200)
        self.assertLessEqual(len(await response.read()), MAX_RESPONSE_BYTES)
        self.assertTrue(result["response_byte_bound_reached"])
        self.assertEqual(result["next_offset"], len(result["messages"]))
        for row in result["messages"]:
            self.assertTrue(row["text_truncated"])
            self.assertLessEqual(len(row["text"].encode("utf-8")), MAX_MESSAGE_BYTES)

    async def test_nontext_payloads_are_not_forwarded(self):
        row = self.row(1, "secret XML/media body")
        row["type"] = "图片"
        self.backend.rows = [row]
        response, result = await self.request()
        self.assertEqual(response.status, 200)
        self.assertTrue(result["messages"][0]["non_text_content_omitted"])
        self.assertNotIn("secret XML", str(result))

    async def test_reads_are_fresh_and_backend_errors_never_echo_secrets_or_message_body(self):
        self.backend.rows = [self.row(1, "first")]
        _, first = await self.request()
        self.backend.rows = [self.row(2, "second")]
        _, second = await self.request()
        self.assertEqual(first["messages"][0]["text"], "first")
        self.assertEqual(second["messages"][0]["text"], "second")
        self.backend.error = True
        response, result = await self.request()
        self.assertEqual(response.status, 503)
        self.assertNotIn(TOKEN, str(result))
        self.assertNotIn("sensitive", str(result))


if __name__ == "__main__":
    unittest.main()
