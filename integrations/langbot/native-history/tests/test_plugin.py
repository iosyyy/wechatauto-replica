import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from history_client import HistoryClient, HistoryError, settings_from_config

spec = importlib.util.spec_from_file_location("native_history_plugin", ROOT / "main.py")
plugin_main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin_main)

CONFIG = {"server_url": "http://127.0.0.1:5080", "api_token": "synthetic-token-12345678901234567890",
          "owners_json": json.dumps([{"bot_uuid": "bot-one", "launcher_id": "wxid_owner"}])}


class PluginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = settings_from_config(CONFIG)
        self.client = HistoryClient(self.settings, "bot-one", "wxid_owner")

    def test_configuration_has_no_global_token_and_rejects_unsafe_urls(self):
        self.assertNotIn(CONFIG["api_token"], repr(self.settings))
        for change in ({"api_token": ""}, {"owners_json": "[]"},
                       {"server_url": "http://8.8.8.8:5080"},
                       {"server_url": "http://secret@127.0.0.1:5080"},
                       {"server_url": "http://127.0.0.1:5080/?key=secret"},
                       {"server_url": "http://192.168.1.2:5080"}):
            with self.subTest(change=change), self.assertRaises(HistoryError):
                settings_from_config({**CONFIG, **change})
        self.assertEqual(settings_from_config({**CONFIG, "server_url": "http://192.168.1.2:5080",
                                               "allow_private_http": True}).server_url,
                         "http://192.168.1.2:5080")

    def test_exact_private_docker_service_requires_explicit_private_http(self):
        for hostname in ("wechatauto-linux-desktop-1", "agent-wechat-native-1"):
            url = "http://" + hostname + ":5080"
            for flag in (None, False, "true", 1):
                with self.subTest(hostname=hostname, flag=flag), self.assertRaises(HistoryError):
                    settings_from_config({**CONFIG, "server_url": url, "allow_private_http": flag})
            with self.subTest(hostname=hostname, flag=True):
                self.assertEqual(settings_from_config({**CONFIG, "server_url": url,
                                                       "allow_private_http": True}).server_url, url)
        for hostname in ("other-service", "wechatauto-linux-desktop-1.example.com", "wechatauto-linux-desktop-2",
                         "agent-wechat-native-1.example.com", "agent-wechat-native-2", "agent-wechat-native-1."):
            with self.subTest(hostname=hostname), self.assertRaises(HistoryError):
                settings_from_config({**CONFIG, "server_url": "http://" + hostname + ":5080",
                                      "allow_private_http": True})

    async def test_owner_is_resolved_from_host_query_before_history_request(self):
        variables = {"launcher_type": "person", "launcher_id": "wxid_owner"}
        proxy = SimpleNamespace(get_bot_uuid=AsyncMock(return_value="bot-one"),
                                get_query_vars=AsyncMock(return_value=variables))
        plugin = SimpleNamespace(get_config=lambda: CONFIG, plugin_runtime_handler=object())
        session = SimpleNamespace(launcher_type=SimpleNamespace(value="person"), launcher_id="wxid_owner")
        with patch.object(plugin_main, "QueryBasedAPIProxy", return_value=proxy) as factory:
            client = await plugin_main.NativeHistoryPlugin.authorized_client(plugin, session, 7)
        self.assertEqual(client.launcher, "wxid_owner")
        self.assertEqual(factory.call_args.kwargs["query_id"], 7)
        proxy.get_query_vars.assert_awaited_once()

    async def test_current_group_is_resolved_from_matching_trusted_host_context(self):
        variables = {"launcher_type": "group", "launcher_id": "one@chatroom"}
        proxy = SimpleNamespace(get_bot_uuid=AsyncMock(return_value="bot-one"),
                                get_query_vars=AsyncMock(return_value=variables))
        plugin = SimpleNamespace(get_config=lambda: CONFIG, plugin_runtime_handler=object())
        session = SimpleNamespace(launcher_type=SimpleNamespace(value="group"), launcher_id="one@chatroom")
        with patch.object(plugin_main, "QueryBasedAPIProxy", return_value=proxy):
            client = await plugin_main.NativeHistoryPlugin.authorized_client(plugin, session, 7)
        self.assertEqual(client.launcher_type, "group")
        self.assertEqual(client.launcher, "one@chatroom")
        self.assertEqual(await client.resolve_group(None), "one@chatroom")
        self.assertEqual(await client.resolve_group("one@chatroom"), "one@chatroom")
        for bot, launcher in (("other-bot", "one@chatroom"), ("bot-one", "wxid_owner"),
                              ("bot-one", "*@chatroom")):
            proxy.get_bot_uuid.return_value = bot
            proxy.get_query_vars.return_value = {"launcher_type": "group", "launcher_id": launcher}
            session.launcher_id = launcher
            with patch.object(plugin_main, "QueryBasedAPIProxy", return_value=proxy), self.assertRaises(HistoryError):
                await plugin_main.NativeHistoryPlugin.authorized_client(plugin, session, 7)

    async def test_group_nonowner_wrong_bot_missing_context_and_session_spoofing_are_denied(self):
        session = SimpleNamespace(launcher_type=SimpleNamespace(value="person"), launcher_id="wxid_owner")
        for bot, variables in (("bot-one", {"launcher_type": "group", "launcher_id": "one@chatroom"}),
                               ("bot-one", {"launcher_type": "person", "launcher_id": "other"}),
                               ("bot-two", {"launcher_type": "person", "launcher_id": "wxid_owner"}),
                               ("bot-one", {})):
            proxy = SimpleNamespace(get_bot_uuid=AsyncMock(return_value=bot),
                                    get_query_vars=AsyncMock(return_value=variables))
            plugin = SimpleNamespace(get_config=lambda: CONFIG, plugin_runtime_handler=object())
            with patch.object(plugin_main, "QueryBasedAPIProxy", return_value=proxy), self.assertRaises(HistoryError):
                await plugin_main.NativeHistoryPlugin.authorized_client(plugin, session, 7)
        proxy.get_query_vars.return_value = {"launcher_type": "person", "launcher_id": "wxid_owner"}
        session.launcher_type.value = "group"
        with patch.object(plugin_main, "QueryBasedAPIProxy", return_value=proxy), self.assertRaises(HistoryError):
            await plugin_main.NativeHistoryPlugin.authorized_client(plugin, session, 7)

    async def test_tool_parameters_cannot_supply_owner_identity(self):
        client = SimpleNamespace(groups=AsyncMock())
        plugin = SimpleNamespace(authorized_client=AsyncMock(return_value=client))
        result = await plugin_main.NativeHistoryPlugin.invoke(
            plugin, "groups", {"launcher_id": "wxid_owner"}, None, 7)
        self.assertEqual(result, "Error: Unknown tool parameter.")
        client.groups.assert_not_awaited()

    async def test_group_tool_omitted_target_uses_current_group_and_rejects_cross_group_ids_or_names(self):
        client = HistoryClient(self.settings, "bot-one", "one@chatroom", launcher_type="group")
        client._request = AsyncMock(return_value={"group_username": "one@chatroom", "messages": [{"text": "evidence"}]})
        plugin = SimpleNamespace(authorized_client=AsyncMock(return_value=client))
        result = await plugin_main.NativeHistoryPlugin.invoke(plugin, "messages", {}, None, 7)
        self.assertEqual(json.loads(result)["group_username"], "one@chatroom")
        self.assertEqual(client._request.call_args.args[1]["group_username"], "one@chatroom")
        client._request.reset_mock()
        for target in ("two@chatroom", "同名群", "one"):
            result = await plugin_main.NativeHistoryPlugin.invoke(plugin, "messages", {"group": target}, None, 7)
            self.assertTrue(result.startswith("Error: Group requests"))
        for params in ({"launcher_type": "person"}, {"bot_uuid": "bot-one"}, {"launcher_id": "wxid_owner"}):
            result = await plugin_main.NativeHistoryPlugin.invoke(plugin, "messages", params, None, 7)
            self.assertEqual(result, "Error: Unknown tool parameter.")
        with self.assertRaises(HistoryError):
            await client.messages("two@chatroom", 50, 0, 1, 2)
        client._request.assert_not_awaited()

    async def test_group_headers_bind_current_context_and_cross_group_responses_are_not_returned(self):
        actual_async_client = httpx.AsyncClient
        client = HistoryClient(self.settings, "bot-one", "one@chatroom", launcher_type="group")
        seen = []
        reply = {"groups": [{"username": "one@chatroom", "name": "Current"}],
                 "has_more": False, "next_offset": None}

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json=reply)

        def factory(**kwargs):
            return actual_async_client(transport=httpx.MockTransport(handler), **kwargs)

        with patch("history_client.httpx.AsyncClient", side_effect=factory):
            result = await client.groups()
            self.assertEqual(result["groups"][0]["username"], "one@chatroom")
            self.assertEqual(seen[0].headers["X-LangBot-Launcher-Type"], "group")
            self.assertEqual(seen[0].headers["X-LangBot-Launcher-ID"], "one@chatroom")
            reply["groups"] = [{"username": "two@chatroom", "name": "not-current"}]
            with self.assertRaisesRegex(HistoryError, "different conversation scope"):
                await client.groups()
            reply.clear()
            reply.update({"group_username": "two@chatroom", "messages": [{"text": "secret"}]})
            with self.assertRaisesRegex(HistoryError, "different conversation scope"):
                await client.messages("one@chatroom", 50, 0, 1, 2)

    async def test_denied_tool_does_not_return_other_group_history(self):
        plugin = SimpleNamespace(authorized_client=AsyncMock(side_effect=HistoryError("private only")))
        result = await plugin_main.NativeHistoryPlugin.invoke(
            plugin, "messages", {"group": "other@chatroom"}, None, 7)
        self.assertEqual(result, "Error: private only")

    async def test_empty_and_ambiguous_names_are_explicit(self):
        self.client.groups = AsyncMock(return_value={"groups": [], "has_more": False})
        with self.assertRaisesRegex(HistoryError, "No authorized local group"):
            await self.client.resolve_group("空群")
        self.client.groups.return_value = {"groups": [{"name": "同名", "username": "one@chatroom"},
                                                       {"name": "同名", "username": "two@chatroom"}],
                                           "has_more": False}
        with self.assertRaisesRegex(HistoryError, "ambiguous"):
            await self.client.resolve_group("同名")
        self.assertEqual(await self.client.resolve_group("one@chatroom"), "one@chatroom")

    async def test_name_resolution_follows_bounded_pages_without_fuzzy_matching(self):
        self.client.groups = AsyncMock(side_effect=[{"groups": [], "has_more": True, "next_offset": 50},
                                                    {"groups": [{"name": "测试群", "username": "one@chatroom"}],
                                                     "has_more": False}])
        self.assertEqual(await self.client.resolve_group("测试群"), "one@chatroom")
        self.assertEqual(self.client.groups.await_count, 2)

    async def test_message_bounds_are_checked_before_network(self):
        self.client._request = AsyncMock()
        for limit, offset, start, end in ((True, 0, 1, 2), (51, 0, 1, 2), (1, 1001, 1, 2),
                                         (1, 0, 2, 2), (1, 0, 1, 604802)):
            with self.assertRaises(HistoryError):
                await self.client.messages("one@chatroom", limit, offset, start, end)
        self.client._request.assert_not_awaited()

    async def test_http_errors_never_include_credentials_or_provider_bodies(self):
        actual_async_client = httpx.AsyncClient
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(503, text=CONFIG["api_token"] + " raw private messages")

        def factory(**kwargs):
            return actual_async_client(transport=httpx.MockTransport(handler), **kwargs)

        with patch("history_client.httpx.AsyncClient", side_effect=factory):
            with self.assertRaises(HistoryError) as caught:
                await self.client.groups()
        self.assertEqual(str(caught.exception), "Native history request failed (HTTP 503).")
        self.assertNotIn(CONFIG["api_token"], str(caught.exception))
        self.assertNotIn("key=", str(seen[0].url))
        self.assertEqual(seen[0].headers["X-LangBot-Launcher-ID"], "wxid_owner")
        self.assertEqual(seen[0].headers["X-LangBot-Launcher-Type"], "person")

    async def test_response_bytes_are_bounded(self):
        actual_async_client = httpx.AsyncClient

        def factory(**kwargs):
            transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 65537))
            return actual_async_client(transport=transport, **kwargs)

        with patch("history_client.httpx.AsyncClient", side_effect=factory), self.assertRaisesRegex(HistoryError, "size bound"):
            await self.client.groups()


if __name__ == "__main__":
    unittest.main()
