"""原生桥接运行层的真实加密数据库与合成桌面边界。"""

from contextlib import closing
import asyncio
import base64
import hashlib
import importlib
import json
from pathlib import Path
import sqlite3
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from aiohttp.test_utils import TestClient, TestServer

from linux_fixtures import CHAT, DatabaseFixture, encrypt_pages
from wechatauto.bridge.journal import ConflictError, Journal

try:
    native = importlib.import_module("wechatauto.bridge.native")
except ModuleNotFoundError:
    native = None


def layout():
    return {"app": "fixture-wechat", "window_id": "window-1",
            **{name: {"role": "fixture-" + name, "name": "{title}"}
               for name in ("search", "search_result", "header", "composer", "send")}}


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(native, "native runtime is missing")
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.fx = DatabaseFixture(self.base)
        self.fx.keys_file.chmod(0o600)
        self.state = self.base / "state"
        self.state.mkdir(mode=0o700)
        self.config = {"db_dir": str(self.fx.root), "account": self.fx.account,
                       "keys_file": str(self.fx.keys_file), "state_dir": str(self.state),
                       "token": "synthetic-token", "desktop_layout": layout(),
                       "expected_exe_sha256": "0" * 64}
        self.journal = Journal(self.state / "journal")
        self.backend = None
        self.filehelper_table = "Msg_" + hashlib.md5(b"filehelper").hexdigest()
        self.rewrite("contact/contact.db", "INSERT INTO contact VALUES(5,'filehelper','Fixture Helper','','')")
        self.rewrite("message/message_0.db", """
            INSERT INTO Name2Id VALUES('wxid_fixture');
            CREATE TABLE %s(local_id INTEGER PRIMARY KEY,local_type INTEGER,real_sender_id INTEGER,
              create_time INTEGER,message_content TEXT,source BLOB,packed_info_data BLOB,
              compress_content BLOB,server_id INTEGER,sort_seq INTEGER);
            INSERT INTO %s VALUES(1,1,2,1700000000,'/lb old',NULL,NULL,NULL,101,1);
        """ % (self.filehelper_table, self.filehelper_table))

    def tearDown(self):
        if hasattr(self, "journal"):
            if self.backend:
                self.backend.close()
            self.journal.close()
            self.tmp.cleanup()

    def rewrite(self, rel, sql):
        plain = self.fx.base / (self.fx.account + "-" + rel.replace("/", "_") + ".plain")
        with closing(sqlite3.connect(plain)) as conn:
            conn.executescript(sql)
            conn.commit()
        salt = hashlib.sha256((self.fx.account + rel).encode()).digest()[:16]
        target = self.fx.storage / rel
        target.write_bytes(encrypt_pages(plain.read_bytes(), bytes.fromhex(self.fx.keys[rel])[:32], salt))

    def append(self, identifier, content, sender=2):
        escaped = content.replace("'", "''")
        self.rewrite("message/message_0.db", "INSERT INTO %s VALUES(%d,1,%d,1700000000,'%s',NULL,NULL,NULL,%d,%d)"
                     % (self.filehelper_table, identifier, sender, escaped, identifier + 100, identifier))

    def start(self, **kwargs):
        self.backend = native.NativeBackend(self.config, self.journal,
                                            account_guard=lambda expected: expected == "wxid_fixture", **kwargs)
        return self.backend

    def test_private_config_validation_defaults_and_strict_physical_opt_in(self):
        path = self.base / "config.json"
        path.write_text(json.dumps(self.config))
        path.chmod(0o600)
        loaded = native.load_config(path)
        self.assertEqual(loaded["allowed_chats"], ["filehelper"])
        self.assertEqual(loaded["write_chats"], ["filehelper"])
        self.assertEqual(loaded["listen_host"], "0.0.0.0")
        self.assertEqual(loaded["port"], 5080)
        for invalid in ("true", 1, None):
            value = {**self.config, "allow_physical": invalid}
            path.write_text(json.dumps(value))
            with self.assertRaises((TypeError, ValueError)):
                native.load_config(path)
        path.chmod(0o644)
        with self.assertRaises(PermissionError):
            native.load_config(path)

    def test_desktop_modes_and_timeout_pass_through_with_explicit_physical_permission(self):
        self.config["desktop_layout"].update(activation_mode="focused_return", input_mode="clipboard_coordinates")
        with self.assertRaises(ValueError):
            native.validate_config(self.config)
        self.config.update(allow_physical=True, desktop_timeout=20)
        calls = []

        def factory(observed, **kwargs):
            calls.append((observed, kwargs))
            return self.driver()

        backend = self.start(driver_factory=factory)
        backend._get_sender()
        observed, options = calls[0]
        self.assertEqual(observed.activation_mode, "focused_return")
        self.assertEqual(observed.input_mode, "clipboard_coordinates")
        self.assertEqual(options["timeout"], 20)
        self.assertTrue(options["allow_physical"])

    def test_composer_input_mode_defaults_to_clipboard_and_passes_explicit_set_value(self):
        self.assertEqual(native._layout(layout()).composer_input_mode, "clipboard")
        self.config["allow_physical"] = True
        self.config["desktop_layout"].update(input_mode="clipboard_coordinates", composer_input_mode="set_value")
        captured = []
        backend = self.start(driver_factory=lambda observed, **kwargs: captured.append(observed) or self.driver())
        backend._get_sender()
        self.assertEqual(captured[0].input_mode, "clipboard_coordinates")
        self.assertEqual(captured[0].composer_input_mode, "set_value")

    def test_composer_input_mode_rejects_unknown_values_or_missing_physical_contract(self):
        for mode in (None, True, 1, [], {}, "automatic", "SET_VALUE", ""):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                native.validate_config({**self.config, "desktop_layout": {**layout(), "composer_input_mode": mode}})
        with self.assertRaises(ValueError):
            native.validate_config({**self.config, "allow_physical": True,
                                    "desktop_layout": {**layout(), "composer_input_mode": "set_value"}})
        observed = {**layout(), "input_mode": "clipboard_coordinates", "composer_input_mode": "set_value"}
        with self.assertRaises(ValueError):
            native.validate_config({**self.config, "desktop_layout": observed})
        with self.assertRaises(ValueError):
            native.validate_config({**self.config, "allow_physical": True,
                                    "desktop_layout": {**observed, "window_id": None}})

    def test_auto_binding_is_explicit_strict_and_cannot_use_injected_boolean_guard(self):
        self.config["allow_physical"] = True
        self.config["desktop_layout"]["window_id"] = None
        with self.assertRaises(ValueError):
            native.validate_config(self.config)
        for invalid in ("true", 1, None):
            with self.assertRaises((ValueError, TypeError)):
                native.validate_config({**self.config, "bind_current_window": invalid})
        self.config["bind_current_window"] = True
        backend = self.start(driver_factory=lambda *args, **kwargs: self.fail("unverified dynamic window"))
        with self.assertRaises(RuntimeError):
            backend._get_sender()

    def test_desktop_configuration_rejects_unknown_modes_and_invalid_timeout(self):
        for key, value in (("activation_mode", "automatic"), ("input_mode", "mouse_fallback")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                native.validate_config({**self.config, "desktop_layout": {**layout(), key: value}})
        for timeout in (0, True, None, 61):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                native.validate_config({**self.config, "desktop_timeout": timeout})

    def test_observed_action_and_system_header_alias_are_preserved_for_driver(self):
        self.config["allow_physical"] = True
        observed = self.config["desktop_layout"]
        observed.update(activation_mode="observed_coordinates", input_mode="clipboard_coordinates",
                        header_aliases={"filehelper": "File Transfer"})
        observed["search"]["required_action"] = "SetFocus"
        captured = []
        backend = self.start(driver_factory=lambda value, **kwargs: captured.append(value) or self.driver())
        backend._get_sender()
        self.assertEqual(captured[0].activation_mode, "observed_coordinates")
        self.assertEqual(captured[0].input_mode, "clipboard_coordinates")
        self.assertEqual(captured[0].search.required_action, "SetFocus")
        self.assertEqual(captured[0].header_aliases, {"filehelper": "File Transfer"})
        for aliases in ({"wxid_alice": "File Transfer"}, {"filehelper": "guessed alias"}, ["File Transfer"]):
            with self.subTest(aliases=aliases), self.assertRaises(ValueError):
                native.validate_config({**self.config, "desktop_layout": {**observed, "header_aliases": aliases}})

    def test_observed_chat_row_first_line_is_preserved_without_guessing_preview(self):
        self.config["allow_physical"] = True
        self.config["desktop_layout"].update(
            activation_mode="observed_coordinates", input_mode="clipboard_coordinates",
            header_aliases={"filehelper": "File Transfer"},
            chat_row={"role": "option", "name_first_line": "{header_title}", "required_action": "SetFocus"})
        captured = []
        backend = self.start(driver_factory=lambda value, **kwargs: captured.append(value) or self.driver())
        backend._get_sender()
        self.assertEqual(captured[0].chat_row.name_first_line, "{header_title}")
        self.assertEqual(captured[0].chat_row.role, "option")
        self.assertEqual(captured[0].chat_row.required_action, "SetFocus")

    def test_keys_missing_is_lazy_and_never_fakes_readiness_or_profile(self):
        self.fx.keys_file.unlink()
        backend = self.start()
        self.assertIsNone(backend.db)
        self.assertFalse(backend.is_logged_in())
        with self.assertRaises(RuntimeError):
            backend.profile()
        self.fx.write_keys()
        self.fx.keys_file.chmod(0o600)
        self.assertEqual(backend.profile(), {"wxid": "wxid_fixture", "nickname": "Me"})
        self.assertTrue(backend.is_logged_in())

    def test_self_contact_is_required_instead_of_directory_fallback(self):
        self.rewrite("contact/contact.db", "DELETE FROM contact WHERE username='wxid_fixture'")
        backend = self.start()
        self.assertFalse(backend.is_logged_in())
        with self.assertRaises(RuntimeError):
            backend.profile()

    def test_baseline_skips_history_only_new_prefixed_self_filehelper_enters_gateway(self):
        backend = self.start()
        self.assertEqual(backend.poll_once(), 0)
        self.assertEqual(self.journal.unacked(), [])
        self.append(2, "normal outgoing")
        self.append(3, "/lb incoming peer", sender=1)
        self.append(4, "/lb fresh request")
        self.assertEqual(backend.poll_once(), 1)
        events = self.journal.unacked()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["content"], {"str": "fresh request"})
        self.assertEqual(events[0]["payload"]["from_user_name"], {"str": "filehelper"})
        self.assertEqual(self.journal.get_cursor("filehelper", "message/message_0.db"), 4)

    def test_persisted_cursor_reopen_does_not_rebaseline_new_messages(self):
        backend = self.start()
        backend.poll_once()
        backend.close()
        self.backend = None
        self.journal.close()
        self.append(2, "/lb after restart")
        self.journal = Journal(self.state / "journal")
        backend = self.start()
        self.assertEqual(backend.poll_once(), 1)
        self.assertEqual(self.journal.unacked()[0]["payload"]["content"]["str"], "after restart")
        self.assertEqual(backend.poll_once(), 0)

    def test_journal_failure_cannot_advance_cursor_and_lose_message(self):
        backend = self.start()
        backend.poll_once()
        self.append(2, "/lb durable")
        with patch.object(self.journal, "record_observation", side_effect=OSError("synthetic full disk")):
            with self.assertRaises(OSError):
                backend.poll_once()
        self.assertEqual(self.journal.get_cursor("filehelper", "message/message_0.db"), 1)
        self.assertEqual(backend.poll_once(), 1)

    def test_confirmed_send_receipt_suppresses_prefixed_echo(self):
        backend = self.start(driver_factory=lambda *args, **kwargs: self.driver())
        backend.poll_once()
        receipt = backend.send_text("filehelper", "/lb robot echo", [], "send-1")
        self.assertTrue(receipt["confirmed"])
        self.assertTrue(self.journal.is_suppressed("filehelper", receipt["shard"], receipt["local_id"]))
        self.assertEqual(backend.poll_once(), 0)
        self.assertEqual(self.journal.unacked(), [])

    def driver(self):
        owner = self

        class Driver:
            def prepare_text(self, target, text):
                pass

            def submit_text(self, target, text, before_submit=None):
                if before_submit:
                    before_submit()
                owner.append(2, text)

        return Driver()

    def test_write_allowlist_rejects_before_sender(self):
        backend = self.start(driver_factory=lambda *args, **kwargs: self.fail("GUI must not be constructed"))
        with self.assertRaises(PermissionError):
            backend.send_text("wxid_alice", "unapproved", [], "send-1")

    def test_cached_sender_avoids_redundant_gui_readiness_and_keeps_three_send_guards(self):
        guards = []
        driver = self.driver()
        driver.prepare_text = Mock(wraps=driver.prepare_text)
        driver.submit_text = Mock(wraps=driver.submit_text)
        backend = self.start(driver_factory=lambda *args, **kwargs: driver)
        backend.account_guard = lambda expected: guards.append(expected) or expected == "wxid_fixture"
        sender = backend._get_sender()
        self.assertEqual(guards, ["wxid_fixture"])
        guards.clear()
        receipt = backend.send_text("filehelper", "cached safe send", [], "cached-1")
        self.assertTrue(receipt["confirmed"])
        self.assertIs(backend.sender, sender)
        self.assertEqual(guards, ["wxid_fixture"] * 3)
        driver.prepare_text.assert_called_once()
        driver.submit_text.assert_called_once()

    def test_cached_sender_account_guard_rejection_occurs_before_any_gui_or_write(self):
        state = {"ready": True, "raise": False}
        guards = []

        def guard(expected):
            guards.append(expected)
            if state["raise"]:
                raise RuntimeError("synthetic guard failure")
            return state["ready"]

        driver = self.driver()
        driver.prepare_text = Mock(wraps=driver.prepare_text)
        driver.submit_text = Mock(wraps=driver.submit_text)
        backend = self.start(driver_factory=lambda *args, **kwargs: driver)
        backend.account_guard = guard
        backend._get_sender()
        state["ready"] = False
        for error in (False, True):
            with self.subTest(guard_raises=error):
                state["raise"] = error
                guards.clear()
                with self.assertRaises(RuntimeError) as caught:
                    backend.send_text("filehelper", "must not write", [], "rejected")
                self.assertEqual(getattr(caught.exception, "code", None), "GUI_ACCOUNT_UNVERIFIED")
                self.assertEqual(guards, ["wxid_fixture"])
                driver.prepare_text.assert_not_called()
                driver.submit_text.assert_not_called()
                self.assertEqual([row["local_id"] for row in backend.db.get_messages("filehelper", limit=10)], [1])

    def test_cached_sender_does_not_relax_profile_or_poll_readiness(self):
        guards = []
        ready = [True]
        backend = self.start(driver_factory=lambda *args, **kwargs: self.driver())
        backend.account_guard = lambda expected: guards.append(expected) or ready[0]
        backend._get_sender()
        ready[0] = False
        guards.clear()
        self.assertFalse(backend.is_logged_in())
        with self.assertRaises(RuntimeError):
            backend.profile()
        with self.assertRaises(RuntimeError):
            backend.poll_once()
        self.assertEqual(guards, ["wxid_fixture"] * 3)
        self.assertIsNone(self.journal.get_cursor("filehelper", "message/message_0.db"))
        self.assertEqual(self.journal.unacked(), [])

    def test_cached_sender_keeps_private_keys_database_binding_and_closed_checks(self):
        driver = self.driver()
        driver.prepare_text = Mock(wraps=driver.prepare_text)
        driver.submit_text = Mock(wraps=driver.submit_text)
        backend = self.start(driver_factory=lambda *args, **kwargs: driver)
        backend._get_sender()
        self.fx.keys_file.unlink()
        with self.assertRaises(RuntimeError):
            backend.send_text("filehelper", "no keys", [], "rejected")
        self.fx.write_keys()
        self.fx.keys_file.chmod(0o600)
        backend.db.account = "wxid_other_efgh"
        with self.assertRaises(RuntimeError):
            backend.send_text("filehelper", "wrong account", [], "rejected")
        backend.close()
        with self.assertRaises(RuntimeError):
            backend.send_text("filehelper", "closed", [], "rejected")
        driver.prepare_text.assert_not_called()
        driver.submit_text.assert_not_called()

    def test_history_is_internal_bounded_and_explicitly_allowed(self):
        self.config["allowed_chats"] = ["filehelper", CHAT]
        backend = self.start()
        self.assertEqual(backend.get_history(CHAT, 1, 0)[0]["content"], "hello")
        self.assertEqual(backend.list_groups(limit=1, offset=0)[0]["username"], CHAT)
        self.assertEqual(backend.list_groups(limit=1, offset=1), [])
        for limit, offset in ((101, 0), (0, 0), (1, 1001), (True, 0)):
            with self.assertRaises(ValueError):
                backend.get_history(CHAT, limit, offset)
        with self.assertRaises(PermissionError):
            backend.get_history("private@chatroom", 1, 0)

    def test_guard_must_return_exact_true_and_account_namespace_is_bound(self):
        backend = self.start()
        backend.account_guard = lambda expected: "truthy"
        self.assertFalse(backend.is_logged_in())
        backend.close()
        self.backend = None
        with self.assertRaises(ConflictError):
            native.NativeBackend({**self.config, "account": "wxid_other_efgh"}, self.journal,
                                 account_guard=lambda expected: True)

    def test_cursor_and_event_are_one_transaction_even_on_collision(self):
        backend = self.start()
        backend.poll_once()
        payload = {"_bridge_event_id": "collision", "content": {"str": "a"}}
        self.journal.record_observation("filehelper", "message/message_0.db", 2, payload)
        with self.assertRaises(ConflictError):
            self.journal.record_observation("filehelper", "message/message_0.db", 3,
                                            {**payload, "content": {"str": "b"}})
        self.assertEqual(self.journal.get_cursor("filehelper", "message/message_0.db"), 2)

    def test_polling_drains_bounded_pages_and_new_shards_skip_imported_history(self):
        backend = self.start()
        backend.poll_once()
        self.rewrite("message/message_0.db", "\n".join(
            "INSERT INTO %s VALUES(%d,1,2,1700000000,'/lb request %d',NULL,NULL,NULL,%d,%d);"
            % (self.filehelper_table, number, number, number + 100, number) for number in range(2, 104)))
        self.assertEqual(backend.poll_once(), 100)
        self.assertEqual(backend.poll_once(), 2)
        self.fx.add_message(1, "wxid_fixture", 200, "/lb imported history")
        self.fx.write_keys()
        self.fx.keys_file.chmod(0o600)
        self.assertEqual(backend.poll_once(), 0)
        self.assertEqual(len(self.journal.unacked(limit=500)), 102)

    def test_image_tempfile_is_private_and_removed_when_sender_rejects_it(self):
        backend = self.start(driver_factory=lambda *args, **kwargs: self.driver())
        sender = backend._get_sender()
        paths = []

        def reject(chat, path, ats, client_id):
            image = Path(path)
            paths.append(image)
            self.assertEqual(stat.S_IMODE(image.stat().st_mode), 0o600)
            self.assertEqual(image.parent, self.state)
            raise native.SendNotSupported("synthetic unsupported image submission")

        with patch.object(sender, "send_image", side_effect=reject):
            with self.assertRaises(native.SendNotSupported):
                backend.send_image("filehelper", base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode(), [], "image-1")
        self.assertEqual(len(paths), 1)
        self.assertFalse(paths[0].exists())

    def test_oversized_image_is_rejected_before_base64_allocation_or_gui(self):
        backend = self.start(driver_factory=lambda *args, **kwargs: self.fail("GUI must not be constructed"))
        with patch.object(native, "MAX_IMAGE_BYTES", 4), \
             patch.object(native.base64, "b64decode", side_effect=AssertionError("oversized decode")):
            with self.assertRaises(ValueError):
                backend.send_image("filehelper", "a" * 12, [], "image-1")

    def test_allowed_group_incoming_is_admitted_but_own_group_reply_is_not(self):
        self.config["allowed_chats"] = ["filehelper", CHAT]
        backend = self.start()
        backend.poll_once()
        table = "Msg_" + hashlib.md5(CHAT.encode()).hexdigest()
        self.rewrite("message/message_0.db", """
            INSERT INTO %s VALUES(2,1,2,1700000000,'own group reply',NULL,NULL,NULL,102,2);
            INSERT INTO %s VALUES(3,1,1,1700000000,'incoming group request',NULL,NULL,NULL,103,3);
        """ % (table, table))
        self.assertEqual(backend.poll_once(), 1)
        payload = self.journal.unacked()[0]["payload"]
        self.assertEqual(payload["content"]["str"], "wxid_alice:\nincoming group request")


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.case = NativeTests()
        self.case.setUp()
        self.client = None

    async def asyncTearDown(self):
        if self.client:
            await self.client.close()
        self.case.tearDown()

    async def test_runtime_starts_poller_serves_gateway_and_closes_journal(self):
        self.assertTrue(hasattr(native, "create_runtime_app"), "native aiohttp lifecycle is missing")
        case = self.case
        case.config["poll_interval"] = .1
        backend = case.start()
        app = native.create_runtime_app(case.config, backend=backend, journal=case.journal)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        response = await self.client.get("/health")
        self.assertEqual((await response.json())["ready"], True)
        for _ in range(100):
            if case.journal.get_cursor("filehelper", "message/message_0.db") is not None:
                break
            await asyncio.sleep(.01)
        ws = await self.client.ws_connect("/GetSyncMsg?key=synthetic-token")
        with backend.lock:
            case.append(2, "/lb runtime request")
        self.assertEqual((await ws.receive_json(timeout=2))["content"]["str"], "runtime request")
        await ws.close()
        await self.client.close()
        self.assertFalse(backend.is_logged_in())
        with self.assertRaises(sqlite3.ProgrammingError):
            case.journal.counts()

    async def test_cli_reads_private_config_and_starts_configured_listener(self):
        self.assertTrue(hasattr(native, "main"), "native CLI is missing")
        path = self.case.base / "config.json"
        path.write_text(json.dumps(self.case.config))
        path.chmod(0o600)
        self.case.journal.close()
        with patch.object(native.web, "run_app") as serve:
            native.main(["--config", str(path)])
        self.assertEqual(serve.call_args.kwargs["host"], "0.0.0.0")
        self.assertEqual(serve.call_args.kwargs["port"], 5080)
        self.assertIsNone(serve.call_args.kwargs["access_log"])


class GuardTests(unittest.TestCase):
    setUp = NativeTests.setUp
    tearDown = NativeTests.tearDown
    rewrite = NativeTests.rewrite
    start = NativeTests.start

    def guard(self):
        self.assertTrue(hasattr(native, "NativeProcessGuard"), "strict native process guard is missing")
        self.executable = self.base / "wechat-fixture"
        self.executable.write_bytes(b"synthetic pinned executable")
        self.config["executable"] = str(self.executable)
        self.config["expected_exe_sha256"] = hashlib.sha256(self.executable.read_bytes()).hexdigest()
        self.proc = self.base / "proc"
        process = self.proc / "42"
        (process / "fd").mkdir(parents=True)
        (process / "exe").symlink_to(self.executable)
        (process / "cmdline").write_bytes(str(self.executable).encode() + b"\0")
        self.process_stat = process / "stat"
        self.process_stat.write_text("42 (wechat) " + " ".join(["S"] + ["1"] * 18 + ["123"]))
        (process / "fd/3").symlink_to(self.fx.storage / "contact/contact.db")
        self.windows = [{"accessible": True, "app_name": "fixture-wechat", "id": "window-1",
                         "is_focused": True, "minimized": False, "pid": 42,
                         "process_instance": "opaque-instance", "title": "Weixin", "visible": True}]
        self.tree = {"role": "window", "name": "Weixin", "children": []}
        self.probes = []

        def probe(args):
            self.probes.append(args)
            return {"ok": True, "data": self.windows if args[0] == "list-windows" else
                    {"complete": True, "identity_unproven": False, "tree": self.tree}}

        backend = self.start()
        backend.ensure_loaded()
        guard = native.NativeProcessGuard(self.config, lambda: backend.db, proc_root=self.proc,
                                          scan_roots=[self.fx.root], desktop_probe=probe,
                                          environment={"HOME": str(self.fx.root)})
        return guard

    def test_current_process_unique_source_fd_hmac_and_window_are_required(self):
        guard = self.guard()
        self.assertIs(guard("wxid_fixture"), True)
        self.assertEqual(guard.window_id, "window-1")
        self.assertTrue(any(args[0] == "list-windows" for args in self.probes))
        self.assertFalse(guard("wxid_other"))

    def test_process_restart_cannot_rebind_existing_guard(self):
        guard = self.guard()
        self.assertTrue(guard("wxid_fixture"))
        self.process_stat.write_text("42 (wechat) " + " ".join(["S"] + ["1"] * 18 + ["456"]))
        self.assertFalse(guard("wxid_fixture"))

    def test_other_account_directory_or_foreign_fd_fails_closed(self):
        guard = self.guard()
        other = self.fx.root / "wxid_other_efgh/db_storage"
        other.mkdir(parents=True)
        self.assertFalse(guard("wxid_fixture"))
        other.rmdir()
        fd = self.proc / "42/fd/3"
        fd.unlink()
        fd.symlink_to(other / "contact/contact.db")
        self.assertFalse(guard("wxid_fixture"))

    def test_missing_fd_or_changed_executable_or_current_contact_hmac_fails(self):
        guard = self.guard()
        self.assertTrue(guard("wxid_fixture"))
        fd = self.proc / "42/fd/3"
        fd.unlink()
        self.assertFalse(guard("wxid_fixture"))
        fd.symlink_to(self.fx.storage / "contact/contact.db")
        self.executable.write_bytes(b"changed executable")
        self.assertFalse(guard("wxid_fixture"))

    def test_login_visible_ambiguous_windows_and_unverified_page_fail_closed(self):
        guard = self.guard()
        self.tree["children"] = [{"role": "button", "name": "Login"}]
        self.assertFalse(guard("wxid_fixture"))
        self.tree["children"] = [{"role": "button", "name": "Login", "states": ["hidden"]}]
        self.assertTrue(guard("wxid_fixture"))
        self.windows.append(dict(self.windows[0], id="window-2"))
        self.assertFalse(guard("wxid_fixture"))
        self.windows.pop()
        contact = self.fx.storage / "contact/contact.db"
        data = bytearray(contact.read_bytes())
        data[200] ^= 1
        contact.write_bytes(data)
        self.assertFalse(guard("wxid_fixture"))

    def test_localized_login_labels_revoke_previously_verified_guard(self):
        guard = self.guard()
        self.backend.account_guard = guard
        self.assertTrue(guard("wxid_fixture"))
        for label in ("Login", "Log In", "Log in", "  LOGIN\t", "\t lOg \n In  ", "登录", " \t登录\n"):
            with self.subTest(label=label):
                self.tree["children"] = [{"role": "button", "name": label}]
                self.assertFalse(guard("wxid_fixture"))
                self.assertFalse(guard.verified)
                self.assertFalse(self.backend.is_logged_in())
        self.tree["children"] = [{"role": "button", "name": " Log In ", "states": ["hidden"]}]
        self.assertTrue(guard("wxid_fixture"))

    def test_dynamic_window_binding_requires_complete_native_guard(self):
        self.config.update(allow_physical=True, bind_current_window=True, desktop_timeout=20)
        self.config["desktop_layout"]["window_id"] = None
        guard = self.guard()
        self.assertIsNone(guard.window_id)
        backend = self.backend
        backend.account_guard = guard
        calls = []
        backend._driver_factory = lambda observed, **kwargs: calls.append((observed, kwargs)) or SimpleNamespace()
        backend._get_sender()
        self.assertTrue(guard.verified)
        self.assertEqual(calls[0][0].window_id, "window-1")
        self.assertEqual(calls[0][1]["timeout"], 20)
        snapshots = [args for args in self.probes if args[0] == "snapshot"]
        self.assertEqual(snapshots[-1][snapshots[-1].index("--timeout-ms") + 1], "20000")
        self.windows[0]["id"] = "replacement-window"
        self.assertFalse(guard("wxid_fixture"))
        self.assertFalse(guard.verified)

    def test_dynamic_binding_refuses_login_or_missing_source_evidence_before_driver(self):
        self.config.update(allow_physical=True, bind_current_window=True)
        self.config["desktop_layout"]["window_id"] = None
        guard = self.guard()
        self.backend.account_guard = guard
        self.backend._driver_factory = lambda *args, **kwargs: self.fail("unverified GUI must not be constructed")
        self.tree["children"] = [{"role": "button", "name": "Login"}]
        with self.assertRaises(RuntimeError):
            self.backend._get_sender()
        self.assertIsNone(guard.window_id)
        self.tree["children"] = []
        (self.proc / "42/fd/3").unlink()
        with self.assertRaises(RuntimeError):
            self.backend._get_sender()

    def test_dynamic_binding_refuses_localized_login_labels_before_driver(self):
        self.config.update(allow_physical=True, bind_current_window=True)
        self.config["desktop_layout"]["window_id"] = None
        guard = self.guard()
        self.backend.account_guard = guard
        calls = []
        self.backend._driver_factory = lambda *args, **kwargs: calls.append(args) or SimpleNamespace()
        for label in ("Log In", "Log in", " \tLoG \n IN  ", "登录", "\t登录 "):
            with self.subTest(label=label):
                self.tree["children"] = [{"role": "button", "name": label}]
                with self.assertRaises(RuntimeError):
                    self.backend._get_sender()
                self.assertFalse(guard.verified)
                self.assertIsNone(guard.window_id)
                self.assertIsNone(self.backend.sender)
                self.assertEqual(calls, [])

    def test_cached_sender_visible_login_fails_native_pre_guard_before_any_gui(self):
        guard = self.guard()
        self.backend.account_guard = guard
        driver = SimpleNamespace(prepare_text=Mock(), submit_text=Mock())
        self.backend._driver_factory = lambda *args, **kwargs: driver
        self.backend._get_sender()
        self.assertTrue(guard.verified)
        self.tree["children"] = [{"role": "button", "name": " Log In "}]
        with self.assertRaises(RuntimeError) as caught:
            self.backend.send_text("filehelper", "must not write", [], "rejected")
        self.assertEqual(getattr(caught.exception, "code", None), "GUI_ACCOUNT_UNVERIFIED")
        self.assertFalse(guard.verified)
        driver.prepare_text.assert_not_called()
        driver.submit_text.assert_not_called()
        self.assertEqual([row["local_id"] for row in self.backend.db.get_messages("filehelper", limit=10)], [1])

    def test_process_exit_during_ui_observation_fails_this_probe_and_retries_safely(self):
        guard = self.guard()
        probe = guard.desktop_probe
        original = self.process_stat.read_text()

        def exiting(args):
            answer = probe(args)
            if args[0] == "snapshot":
                self.process_stat.unlink()
            return answer

        guard.desktop_probe = exiting
        self.assertFalse(guard("wxid_fixture"))
        self.process_stat.write_text(original)
        guard.desktop_probe = probe
        self.assertTrue(guard("wxid_fixture"))

    def test_account_fd_switch_during_ui_observation_cannot_complete_binding(self):
        guard = self.guard()
        probe = guard.desktop_probe

        def switched(args):
            answer = probe(args)
            if args[0] == "snapshot":
                descriptor = self.proc / "42/fd/3"
                descriptor.unlink()
                descriptor.symlink_to(self.base / "other/db_storage/contact/contact.db")
            return answer

        guard.desktop_probe = switched
        self.assertFalse(guard("wxid_fixture"))
        self.assertFalse(guard.verified)

    def test_desktop_subprocess_retains_session_environment_and_timeout(self):
        self.config["desktop_timeout"] = 20
        guard = self.guard()
        guard.environment["DBUS_SESSION_BUS_ADDRESS"] = "unix:synthetic-session"
        result = SimpleNamespace(returncode=0, stdout='{"ok": true, "data": []}')
        with patch.object(native.subprocess, "run", return_value=result) as run:
            guard._desktop(["list-windows", "--app", "fixture-wechat"])
        self.assertEqual(run.call_args.kwargs["env"]["DBUS_SESSION_BUS_ADDRESS"], "unix:synthetic-session")
        self.assertGreater(run.call_args.kwargs["timeout"], 20)


if __name__ == "__main__":
    unittest.main()
