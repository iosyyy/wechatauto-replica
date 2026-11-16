"""Linux 发送的目标校验、单次投递与数据库回读测试。"""

from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from wechatauto.platforms.linux_sender import (
    AgentDesktopDriver, AgentDesktopError, DesktopLayout, LinuxSender,
    LinuxSenderError, ReaderIdentityResolver, ReaderReadback, Selector,
    SendNotSupported, Target,
)


class FakeResolver:
    def resolve(self, username):
        return Target(username, "Fixture Friend")


class FakeReadback:
    def __init__(self):
        self.calls = []
        self.result = None

    def watermark(self, username):
        self.calls.append(("watermark", username))
        return {"message_0.db": 42}

    def find_text(self, username, text, mark):
        self.calls.append(("find", username, text, mark))
        return self.result


class FakeDriver:
    def __init__(self):
        self.calls = []
        self.submit_error = None
        self.prepare_error = None

    def prepare_text(self, target, text):
        self.calls.append(("prepare", target, text))
        if self.prepare_error:
            raise self.prepare_error

    def submit_text(self, target, text, *, before_submit=None):
        if before_submit is not None:
            before_submit()
        self.calls.append(("submit", target, text))
        if self.submit_error:
            raise self.submit_error


class LinuxSenderTests(unittest.TestCase):
    def setUp(self):
        self.driver = FakeDriver()
        self.readback = FakeReadback()
        self.sender = LinuxSender(None, self.driver, resolver=FakeResolver(),
                                  readback=self.readback, verify_timeout=0, own_username="wxid_reader_a",
                                  account_guard=lambda expected: expected == "wxid_reader_a")

    def test_missing_account_guard_fails_before_any_gui_action(self):
        self.sender.account_guard = None
        with self.assertRaises(LinuxSenderError):
            self.sender.send_text("wxid_friend", "hello")
        self.assertEqual(self.driver.calls, [])

    def test_reader_a_gui_b_is_refused_before_any_gui_action(self):
        active_gui_account = "wxid_gui_b"
        self.sender.db = SimpleNamespace(get_self_info=lambda: {"username": "wxid_reader_a"})
        self.sender.account_guard = lambda expected: active_gui_account == expected
        with self.assertRaises(LinuxSenderError):
            self.sender.send_text("wxid_friend", "hello")
        self.assertEqual(self.driver.calls, [])

    def test_account_switch_during_preparation_is_refused_before_submit(self):
        active_gui_account = ["wxid_reader_a"]
        guarded = []

        def guard(expected):
            guarded.append(expected)
            return active_gui_account[0] == expected

        prepare = self.driver.prepare_text

        def switching_prepare(target, text):
            prepare(target, text)
            active_gui_account[0] = "wxid_gui_b"

        self.driver.prepare_text = switching_prepare
        self.sender.account_guard = guard
        with self.assertRaises(LinuxSenderError):
            self.sender.send_text("wxid_friend", "hello")
        self.assertEqual(guarded, ["wxid_reader_a", "wxid_reader_a"])
        self.assertEqual([row[0] for row in self.driver.calls], ["prepare"])

    def test_guard_error_and_truthy_nonboolean_result_fail_closed(self):
        def raising_guard(expected):
            raise RuntimeError("private guard details")

        for guard in (raising_guard, lambda expected: "wxid_reader_a"):
            with self.subTest(guard=guard):
                self.sender.account_guard = guard
                with self.assertRaises(LinuxSenderError) as caught:
                    self.sender.send_text("wxid_friend", "hello")
                self.assertNotIn("private guard details", str(caught.exception))
                self.assertEqual(self.driver.calls, [])

    def test_missing_expected_account_cannot_be_replaced_by_a_guard_success(self):
        self.sender.own_username = None
        self.sender.account_guard = lambda expected: True
        with self.assertRaises(LinuxSenderError):
            self.sender.send_text("wxid_friend", "hello")
        self.assertEqual(self.driver.calls, [])

    def test_reader_account_switch_during_preparation_is_refused_before_submit(self):
        reader_account = ["wxid_reader_a"]
        self.sender.db = SimpleNamespace(get_self_info=lambda: {"username": reader_account[0]})
        prepare = self.driver.prepare_text

        def switching_prepare(target, text):
            prepare(target, text)
            reader_account[0] = "wxid_reader_b"

        self.driver.prepare_text = switching_prepare
        with self.assertRaises(LinuxSenderError):
            self.sender.send_text("wxid_friend", "hello")
        self.assertEqual([row[0] for row in self.driver.calls], ["prepare"])

    def test_text_is_inserted_and_submitted_once_then_confirmed_by_database(self):
        self.readback.result = {"local_id": 43, "server_id": 100, "shard": "message_0.db"}
        result = self.sender.send_text("wxid_friend", "中文 😀\nsecond", client_id="client-1")
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["local_id"], 43)
        self.assertEqual(result["client_id"], "client-1")
        self.assertIs(result["confirmed"], True)
        self.assertEqual(result["msg_id"], "100")
        self.assertEqual([r[0] for r in self.driver.calls], ["prepare", "submit"])
        self.assertEqual(self.readback.calls[0][0], "watermark")

    def test_unknown_submit_result_is_read_back_without_resubmitting(self):
        self.driver.submit_error = AgentDesktopError("TIMEOUT", "uncertain", safe=False)
        result = self.sender.send_text("wxid_friend", "hello")
        self.assertEqual(result["status"], "uncertain")
        self.assertIs(result["confirmed"], False)
        self.assertEqual(len(self.driver.calls), 2)

    def test_database_can_confirm_after_uncertain_submit(self):
        self.driver.submit_error = AgentDesktopError("TIMEOUT", "uncertain", safe=False)
        self.readback.result = {"local_id": 43, "shard": "message_0.db"}
        self.assertEqual(self.sender.send_text("wxid_friend", "hello")["status"], "confirmed")

    def test_proven_not_delivered_submit_fails_without_resubmitting(self):
        self.driver.submit_error = AgentDesktopError("STALE_REF", "absent", safe=True)
        with self.assertRaisesRegex(LinuxSenderError, "STALE_REF"):
            self.sender.send_text("wxid_friend", "hello")
        self.assertEqual(len(self.driver.calls), 2)

    def test_prepare_failure_never_submits(self):
        self.driver.prepare_error = LinuxSenderError("DRAFT_PRESENT", "draft exists")
        with self.assertRaises(LinuxSenderError):
            self.sender.send_text("wxid_friend", "hello")
        self.assertEqual(len(self.driver.calls), 1)

    def test_watermark_error_fails_before_touching_ui(self):
        self.readback.watermark = lambda username: (_ for _ in ()).throw(RuntimeError("DB down"))
        with self.assertRaises(RuntimeError):
            self.sender.send_text("wxid_friend", "hello")
        self.assertEqual(self.driver.calls, [])

    def test_readback_failure_after_submit_is_uncertain(self):
        self.readback.find_text = lambda *args: (_ for _ in ()).throw(RuntimeError("DB down"))
        self.assertEqual(self.sender.send_text("wxid_friend", "hello")["status"], "uncertain")
        self.assertEqual(len(self.driver.calls), 2)

    def test_delayed_database_confirmation_only_polls_without_ui_retries(self):
        clock = [0.0]
        attempts = []

        def find(*args):
            attempts.append(args)
            return {"local_id": 43} if len(attempts) == 3 else None

        self.readback.find_text = find
        self.sender.verify_timeout = 1
        self.sender.clock = lambda: clock[0]
        self.sender.sleep = lambda delay: clock.__setitem__(0, clock[0] + delay)
        self.assertEqual(self.sender.send_text("wxid_friend", "hello")["status"], "confirmed")
        self.assertEqual(len(attempts), 3)
        self.assertEqual([r[0] for r in self.driver.calls], ["prepare", "submit"])

    def test_empty_invalid_text_mentions_and_images_do_not_touch_ui(self):
        for text in ("", "\x00", None, 12):
            with self.subTest(text=text), self.assertRaises((ValueError, TypeError)):
                self.sender.send_text("wxid_friend", text)
        with self.assertRaises(SendNotSupported):
            self.sender.send_text("wxid_friend", "hello", ats=["wxid_other"])
        with self.assertRaises(SendNotSupported):
            self.sender.send_image("wxid_friend", "/tmp/image.png")
        self.assertEqual(self.driver.calls, [])


class ReaderBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.contact = Path(self.tmp.name) / "contact.db"
        self.message = Path(self.tmp.name) / "message.db"
        with closing(sqlite3.connect(self.contact)) as conn, conn:
            conn.executescript("""
                CREATE TABLE contact(username TEXT,nick_name TEXT,remark TEXT);
                INSERT INTO contact VALUES('wxid_friend','Fixture Friend','');
                INSERT INTO contact VALUES('filehelper','文件传输助手','');
            """)
        import hashlib
        self.table = "Msg_" + hashlib.md5(b"wxid_friend").hexdigest()
        with closing(sqlite3.connect(self.message)) as conn, conn:
            conn.executescript("""
                CREATE TABLE Name2Id(user_name TEXT);
                INSERT INTO Name2Id VALUES('wxid_other'),('wxid_not_self'),('wxid_self');
                CREATE TABLE %s(local_id INTEGER,local_type INTEGER,real_sender_id INTEGER,
                    create_time INTEGER,message_content TEXT,sort_seq INTEGER,server_id INTEGER);
                INSERT INTO %s VALUES(1,1,3,1,'old',7,100);
            """ % (self.table, self.table))
        self.db = self

    def tearDown(self):
        self.tmp.cleanup()

    def _contact_conn(self):
        conn = sqlite3.connect(self.contact)
        conn.row_factory = sqlite3.Row
        return conn

    def _message_dbs(self):
        return ["message/message_0.db"]

    def _open(self, rel):
        conn = sqlite3.connect(self.message)
        conn.row_factory = sqlite3.Row
        return conn

    def get_self_info(self):
        return {"username": "wxid_self"}

    def test_exact_username_resolution_and_collision_refusal(self):
        resolver = ReaderIdentityResolver(self.db)
        self.assertEqual(resolver.resolve("filehelper").title, "文件传输助手")
        with self.assertRaises(LinuxSenderError):
            resolver.resolve("Fixture Friend")
        with closing(sqlite3.connect(self.contact)) as conn, conn:
            conn.execute("INSERT INTO contact VALUES('wxid_collision','Fixture Friend','different')")
        with self.assertRaisesRegex(LinuxSenderError, "AMBIGUOUS_TARGET"):
            resolver.resolve("wxid_friend")

    def test_username_sql_metacharacters_are_not_interpolated(self):
        resolver = ReaderIdentityResolver(self.db)
        with self.assertRaises(LinuxSenderError) as caught:
            resolver.resolve("wxid_friend' OR 1=1 --")
        self.assertEqual(caught.exception.code, "TARGET_NOT_FOUND")
        self.assertEqual(resolver.resolve("wxid_friend").username, "wxid_friend")

    def test_header_alias_collision_is_refused_before_any_gui_action(self):
        resolver = ReaderIdentityResolver(self.db)
        resolver.assert_alias_unique("filehelper", "File Transfer")
        with closing(sqlite3.connect(self.contact)) as conn, conn:
            conn.execute("INSERT INTO contact VALUES('wxid_collision','File Transfer','')")
        driver = FakeDriver()
        driver.layout = SimpleNamespace(header_aliases={"filehelper": "File Transfer"})
        sender = LinuxSender(self.db, driver, resolver=resolver, readback=FakeReadback(),
                             account_guard=lambda expected: True, verify_timeout=0)
        with self.assertRaises(LinuxSenderError) as caught:
            sender.send_text("filehelper", "body")
        self.assertEqual(caught.exception.code, "AMBIGUOUS_TARGET")
        self.assertEqual(driver.calls, [])

    def test_watermark_covers_all_rows_and_sender_mapping_is_per_shard(self):
        readback = ReaderReadback(self.db)
        mark = readback.watermark("wxid_friend")
        self.assertIsNone(readback.find_text("wxid_friend", "old", mark))
        with closing(sqlite3.connect(self.message)) as conn, conn:
            conn.execute("INSERT INTO %s VALUES(2,1,2,1,'wanted',7,101)" % self.table)
        self.assertIsNone(readback.find_text("wxid_friend", "wanted", mark))
        with closing(sqlite3.connect(self.message)) as conn, conn:
            conn.execute("INSERT INTO %s VALUES(3,1,3,1,'wanted suffix',7,102)" % self.table)
            conn.execute("INSERT INTO %s VALUES(4,1,3,1,'wanted',7,103)" % self.table)
        self.assertEqual(readback.find_text("wxid_friend", "wanted", mark)["local_id"], 4)

    def test_a_new_conversation_can_be_confirmed_only_on_a_preobserved_shard(self):
        readback = ReaderReadback(self.db)
        mark = readback.watermark("filehelper")
        self.assertEqual(list(mark.values()), [0])
        self.assertIsNone(readback.find_text("wxid_friend", "old", {}))

    def test_pending_local_row_without_server_id_does_not_confirm_delivery(self):
        readback = ReaderReadback(self.db)
        mark = readback.watermark("wxid_friend")
        with closing(sqlite3.connect(self.message)) as conn, conn:
            conn.execute("INSERT INTO %s VALUES(2,1,3,1,'pending',7,0)" % self.table)
        self.assertIsNone(readback.find_text("wxid_friend", "pending", mark))
        with closing(sqlite3.connect(self.message)) as conn, conn:
            conn.execute("UPDATE %s SET server_id=104 WHERE local_id=2" % self.table)
        self.assertEqual(readback.find_text("wxid_friend", "pending", mark)["server_id"], 104)


class FakeCLI:
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.calls = []
        self.current = None

    def __call__(self, args, stdin=None):
        self.calls.append((args, stdin))
        if args[0] == "snapshot":
            self.current = self.snapshots.pop(0)
            return {"ok": True, "data": self.current}
        if args[0] == "get":
            found = next(n for n in AgentDesktopDriver._nodes(self.current["tree"])
                         if n.get("ref_id") == args[1])
            return {"ok": True, "data": {"ref": args[1], "property": "value", "value": found.get("value")}}
        if args[0] == "is":
            found = next(n for n in AgentDesktopDriver._nodes(self.current["tree"])
                         if n.get("ref_id") == args[1])
            return {"ok": True, "data": {"property": "focused", "applicable": True,
                                          "result": "focused" in found.get("states", [])}}
        return {"ok": True, "data": {"disposition": {"delivery": "delivered_verified", "retry": "unsafe"}}}


class CoordinateCLI:
    """只模拟窗口、文字与焦点，不触及真实桌面。"""

    def __init__(self):
        self.calls = []
        self.clipboard = ""
        self.search = ""
        self.composer = ""
        self.focused = None
        self.chat_open = False
        self.popup = False
        self.sent = 0
        self.clipboard_mismatch = False
        self.search_width = 200
        self.popup_pid = 15
        self.popup_title = ""
        self.extra_popup = False
        self.popup_opens_chat = True
        self.popup_stays_open = False
        self.main_focused = True
        self.lose_focus_after_popup = False
        self.close_popup_during_wait = False
        self.chat_row_opens_async = False
        self.chat_pending = False
        self.composer_name = ""
        self.chat_row_toggles_open_chat = False
        self.hide_closed_chat_controls = False

    def windows(self):
        result = [{"id": "w-main", "pid": 15, "process_instance": "instance-a", "title": "Fixture",
                   "app_name": "fixture-app", "visible": True, "minimized": False,
                   "is_focused": self.main_focused,
                   "bounds": {"x": 0, "y": 0, "width": 600, "height": 500}}]
        if self.popup:
            result.append({"id": "w-popup", "pid": self.popup_pid, "process_instance": "instance-a",
                           "title": self.popup_title, "app_name": "fixture-app", "visible": True,
                           "minimized": False, "is_focused": False,
                           "bounds": {"x": 10, "y": 50, "width": 200, "height": 150}})
            if self.extra_popup:
                result.append({**result[-1], "id": "w-popup-2"})
        return result

    def state(self, window):
        if window != "w-main":
            result = snapshot(node("option", "Fixture Friend", "@p:e1",
                                   bounds={"x": 10, "y": 80, "width": 200, "height": 30}),
                              node("option", "Suggestion Fixture Friend", "@p:e2",
                                   bounds={"x": 10, "y": 120, "width": 200, "height": 30}))
        else:
            result = snapshot(
                node("textfield", "Fixture Search", "@m:e1", native_id="search", value=self.search,
                     states=["focused"] if self.focused == "search" else [],
                     bounds={"x": 10, "y": 10, "width": self.search_width, "height": 30}),
                node("textfield", "Fixture Search", "@hidden:e1", native_id="search", states=["hidden"]),
                node("label", "Fixture Friend" if self.chat_open else "Old Chat", "@m:e2"),
                node("textfield", self.composer_name, "@m:e3", native_id="composer", value=self.composer,
                     states=["focused"] if self.focused == "composer" else [],
                     bounds={"x": 220, "y": 300, "width": 300, "height": 120}),
                node("button", "Fixture Send", "@m:e4",
                     bounds={"x": 500, "y": 450, "width": 60, "height": 30}),
                node("option", "Fixture Friend\nchanging preview", "@m:e5", available_actions=["Select"],
                     bounds={"x": 10, "y": 210, "width": 200, "height": 60}))
            if self.hide_closed_chat_controls and not self.chat_open:
                result["tree"]["children"] = [item for item in result["tree"]["children"]
                                               if item.get("ref_id") not in ("@m:e2", "@m:e3", "@m:e4")]
        result["window"] = {"id": window, "title": "Fixture" if window == "w-main" else ""}
        return result

    def __call__(self, args, stdin=None):
        self.calls.append((args, stdin))
        if args[0] == "list-windows":
            data = self.windows()
        elif args[0] == "snapshot":
            if self.chat_pending and "--wait-for" in args:
                self.chat_pending = False
                self.chat_open = True
            if self.hide_closed_chat_controls and not self.chat_open and "--wait-for" in args:
                return {"ok": False, "error": {"code": "TIMEOUT", "disposition":
                                                {"delivery": "not_delivered", "retry": "safe"}}}
            data = self.state(args[args.index("--window-id") + 1])
        elif args[0] == "clipboard-set":
            self.clipboard = stdin
            data = {"type": "text"}
        elif args[0] == "focus-window":
            self.main_focused = True
            data = {"focused": self.windows()[0]}
        elif args[0] == "clipboard-get":
            data = {"type": "text", "text": "wrong" if self.clipboard_mismatch else self.clipboard}
        elif args[0] == "get":
            value = self.composer if args[1] == "@m:e3" else self.search
            data = {"ref": args[1], "property": "value", "value": value}
        elif args[0] == "set-value":
            if args[1] != "@m:e3":
                raise AssertionError("Unexpected semantic text target")
            self.composer = stdin
            data = {}
        elif args[0] == "mouse-click":
            point = tuple(float(v) for v in args[args.index("--xy") + 1].split(","))
            if point == (110, 25):
                self.focused = "search"
            elif point == (110, 95):
                self.popup = self.popup_stays_open
                self.chat_open = self.popup_opens_chat
                self.main_focused = not self.lose_focus_after_popup
            elif point == (110, 240):
                if self.chat_row_toggles_open_chat and self.chat_open:
                    self.chat_open = False
                else:
                    self.chat_pending = self.chat_row_opens_async
                    self.chat_open = not self.chat_pending
            elif point == (370, 360):
                self.focused = "composer"
            elif point == (530, 465):
                self.sent += 1
                self.composer = ""
            else:
                raise AssertionError("Unexpected coordinate")
            data = {}
        elif args[0] == "press":
            if args[1] == "ctrl+v":
                if self.focused == "search":
                    self.search = self.clipboard
                    self.popup = True
                elif self.focused == "composer":
                    self.composer = self.clipboard
                else:
                    raise AssertionError("Paste without verified focus")
            data = {}
        elif args[0] == "wait":
            if self.close_popup_during_wait:
                self.popup = False
            data = {}
        else:
            raise AssertionError("Unexpected command: " + args[0])
        return {"ok": True, "data": data}


class CoordinateRouteTests(unittest.TestCase):
    def layout(self):
        return DesktopLayout("fixture-app", Selector("textfield", native_id="search"),
                             Selector("option", name="{title}"), Selector("label", name="{title}"),
                             Selector("textfield", native_id="composer"), Selector("button", name="Fixture Send"),
                             window_id="w-main", input_mode="clipboard_coordinates",
                             activation_mode="observed_coordinates")

    def driver(self, cli):
        return AgentDesktopDriver(self.layout(), run=cli, allow_physical=True)

    def test_current_chat_fast_path_only_pastes_body_once_and_skips_all_navigation(self):
        from dataclasses import replace
        cli = CoordinateCLI()
        cli.chat_open = True
        row = Selector("option", name_first_line="{header_title}", required_action="Select")
        driver = AgentDesktopDriver(replace(self.layout(), chat_row=row), run=cli, allow_physical=True)
        driver.prepare_text(Target("wxid_friend", "Fixture Friend"), "中文\nbody")
        writes = [stdin for args, stdin in cli.calls if args[0] == "clipboard-set"]
        self.assertEqual(writes, ["中文\nbody"])
        points = [args[args.index("--xy") + 1] for args, _ in cli.calls if args[0] == "mouse-click"]
        self.assertEqual(points, ["370,360"])
        self.assertEqual(sum(args[:2] == ["press", "ctrl+v"] for args, _ in cli.calls), 1)
        self.assertEqual(sum(args[:2] == ["press", "ctrl+a"] for args, _ in cli.calls), 0)
        self.assertFalse(any(args[0] == "wait" or "--wait-for" in args for args, _ in cli.calls))
        self.assertEqual(cli.composer, "中文\nbody")
        self.assertEqual(cli.sent, 0)
        self.assertEqual(sum(args[0] == "snapshot" for args, _ in cli.calls), 4)

    def set_value_layout(self):
        from dataclasses import replace
        return replace(self.layout(), composer_input_mode="set_value")

    def test_set_value_mode_writes_text_once_headless_and_confirms_with_exact_get(self):
        for text in ("plain text", "中文 😀\n第二行\n"):
            cli = CoordinateCLI()
            cli.chat_open = True
            driver = AgentDesktopDriver(self.set_value_layout(), run=cli, allow_physical=True)
            driver.prepare_text(Target("wxid_friend", "Fixture Friend"), text)
            writes = [(args, stdin) for args, stdin in cli.calls if args[0] == "set-value"]
            with self.subTest(text=text):
                self.assertEqual(writes, [(["set-value", "@m:e3", "--stdin"], text)])
                self.assertEqual(sum(args[0] == "get" for args, _ in cli.calls), 2)
                self.assertEqual(sum(args[0] == "snapshot" for args, _ in cli.calls), 2)
                self.assertFalse(any(args[0] in ("clipboard-set", "mouse-click", "press") for args, _ in cli.calls))
                self.assertEqual(cli.composer, text)
                self.assertEqual(cli.sent, 0)

    def test_set_value_mode_preserves_search_clipboard_route(self):
        cli = CoordinateCLI()
        driver = AgentDesktopDriver(self.set_value_layout(), run=cli, allow_physical=True)
        driver.prepare_text(Target("wxid_friend", "Fixture Friend"), "中文\nbody")
        self.assertEqual([stdin for args, stdin in cli.calls if args[0] == "clipboard-set"], ["Fixture Friend"])
        self.assertEqual(sum(args[:2] == ["press", "ctrl+v"] for args, _ in cli.calls), 1)
        self.assertEqual([args[args.index("--xy") + 1] for args, _ in cli.calls if args[0] == "mouse-click"],
                         ["110,25", "110,95"])
        self.assertEqual(sum(args[0] == "set-value" for args, _ in cli.calls), 1)
        self.assertEqual(cli.composer, "中文\nbody")

    def test_set_value_mode_rejects_unknown_or_nonempty_live_value_before_writing(self):
        for value in (None, "draft visible only to live get"):
            cli = CoordinateCLI()
            cli.chat_open = True
            run = cli.__call__

            def live_value(args, stdin=None):
                if args[0] == "get":
                    cli.calls.append((args, stdin))
                    return {"ok": True, "data": {"ref": args[1], "property": "value", "value": value}}
                return run(args, stdin)

            driver = AgentDesktopDriver(self.set_value_layout(), run=live_value, allow_physical=True)
            with self.subTest(value=value), self.assertRaises(LinuxSenderError):
                driver.prepare_text(Target("wxid_friend", "Fixture Friend"), "body")
            self.assertFalse(any(args[0] in ("set-value", "clipboard-set", "mouse-click", "press") for args, _ in cli.calls))

    def test_set_value_mode_accepts_omitted_snapshot_value_only_after_exact_get(self):
        cli = CoordinateCLI()
        cli.chat_open = True
        state = cli.state

        def omitted_value(window):
            result = state(window)
            if window == "w-main":
                result["tree"]["children"][3].pop("value", None)
            return result

        cli.state = omitted_value
        driver = AgentDesktopDriver(self.set_value_layout(), run=cli, allow_physical=True)
        driver.prepare_text(Target("wxid_friend", "Fixture Friend"), "中文\nbody")
        self.assertEqual(cli.composer, "中文\nbody")
        self.assertEqual(sum(args[0] == "get" for args, _ in cli.calls), 2)
        self.assertEqual(sum(args[0] == "set-value" for args, _ in cli.calls), 1)

    def test_set_value_mode_post_write_get_must_match_even_when_snapshot_matches(self):
        cli = CoordinateCLI()
        cli.chat_open = True
        run = cli.__call__
        reads = []

        def contradicting_get(args, stdin=None):
            result = run(args, stdin)
            if args[0] == "get":
                reads.append(args)
                if len(reads) == 2:
                    result["data"]["value"] = "different live body"
            return result

        driver = AgentDesktopDriver(self.set_value_layout(), run=contradicting_get, allow_physical=True)
        with self.assertRaises(LinuxSenderError) as caught:
            driver.prepare_text(Target("wxid_friend", "Fixture Friend"), "requested")
        self.assertEqual(caught.exception.code, "TEXT_MISMATCH")
        self.assertEqual(sum(args[0] == "set-value" for args, _ in cli.calls), 1)
        self.assertFalse(any(args[0] in ("clipboard-set", "mouse-click", "press") for args, _ in cli.calls))
        self.assertEqual(cli.sent, 0)

    def test_set_value_mode_preserves_three_independent_account_guards(self):
        cli = CoordinateCLI()
        cli.chat_open = True
        driver = AgentDesktopDriver(self.set_value_layout(), run=cli, allow_physical=True)
        readback = FakeReadback()
        readback.result = {"local_id": 43, "server_id": 100, "shard": "message_0.db"}
        guards = []

        def guard(expected):
            guards.append((expected, sum(args[0] == "set-value" for args, _ in cli.calls), cli.sent))
            return True

        sender = LinuxSender(None, driver, resolver=FakeResolver(), readback=readback,
                             own_username="wxid_self", account_guard=guard, verify_timeout=0)
        self.assertIs(sender.send_text("wxid_friend", "body")["confirmed"], True)
        self.assertEqual(guards, [("wxid_self", 0, 0), ("wxid_self", 1, 0), ("wxid_self", 1, 0)])
        self.assertEqual(cli.sent, 1)

    def test_set_value_error_never_falls_back_to_clipboard_or_second_write(self):
        for safe in (False, True):
            cli = CoordinateCLI()
            cli.chat_open = True
            run = cli.__call__

            def refused_write(args, stdin=None):
                if args[0] == "set-value":
                    cli.calls.append((args, stdin))
                    if not safe:
                        cli.composer = stdin
                    return {"ok": False, "error": {"code": "TIMEOUT", "disposition":
                                                    {"delivery": "not_delivered" if safe else "uncertain",
                                                     "retry": "safe" if safe else "unsafe"}}}
                return run(args, stdin)

            driver = AgentDesktopDriver(self.set_value_layout(), run=refused_write, allow_physical=True)
            with self.subTest(safe=safe), self.assertRaises(AgentDesktopError) as caught:
                driver.prepare_text(Target("wxid_friend", "Fixture Friend"), "body")
            self.assertEqual(caught.exception.safe, safe)
            self.assertEqual(sum(args[0] == "set-value" for args, _ in cli.calls), 1)
            self.assertFalse(any(args[0] in ("clipboard-set", "mouse-click", "press") for args, _ in cli.calls))
            self.assertEqual(cli.sent, 0)

    def test_set_value_mode_is_explicit_and_requires_coordinate_search_route(self):
        from dataclasses import replace
        for layout in (replace(self.layout(), composer_input_mode="automatic"),
                       replace(self.layout(), input_mode="ref", composer_input_mode="set_value")):
            with self.subTest(layout=layout), self.assertRaises(ValueError):
                AgentDesktopDriver(layout, allow_physical=True)

    def test_zero_target_header_preserves_search_popup_and_chat_row_navigation(self):
        from dataclasses import replace
        cli = CoordinateCLI()
        cli.popup_opens_chat = False
        cli.hide_closed_chat_controls = True
        row = Selector("option", name_first_line="{header_title}", required_action="Select")
        driver = AgentDesktopDriver(replace(self.layout(), chat_row=row), run=cli, allow_physical=True)
        driver.prepare_text(Target("wxid_friend", "Fixture Friend"), "body")
        self.assertEqual([stdin for args, stdin in cli.calls if args[0] == "clipboard-set"],
                         ["Fixture Friend", "body"])
        points = [args[args.index("--xy") + 1] for args, _ in cli.calls if args[0] == "mouse-click"]
        self.assertEqual(points, ["110,25", "110,95", "110,240", "370,360"])
        self.assertEqual(cli.composer, "body")

    def test_popup_that_already_opens_target_never_reclicks_chat_row(self):
        from dataclasses import replace
        cli = CoordinateCLI()
        cli.popup_opens_chat = True
        cli.chat_row_toggles_open_chat = True
        cli.hide_closed_chat_controls = True
        cli.composer_name = "Fixture Composer"
        row = Selector("option", name_first_line="{header_title}", required_action="Select")
        layout = replace(self.layout(), chat_row=row,
                         composer=Selector("textfield", name="Fixture Composer", native_id="composer"))
        run = cli.__call__
        trace = []

        def recorded(args, stdin=None):
            result = run(args, stdin)
            entry = {"command": args[0]}
            if args[0] == "snapshot" and result.get("ok"):
                nodes = list(AgentDesktopDriver._nodes(result["data"]["tree"]))
                entry["target_header"] = sum(item.get("role") == "label" and
                                               item.get("name") == "Fixture Friend" for item in nodes)
                entry["composer"] = sum(item.get("native_id") == "composer" for item in nodes)
            trace.append(entry)
            return result

        driver = AgentDesktopDriver(layout, run=recorded, allow_physical=True)
        driver.prepare_text(Target("wxid_friend", "Fixture Friend"), "body")
        self.assertTrue(any(entry.get("target_header") == 0 for entry in trace))
        self.assertTrue(any(entry.get("target_header") == 1 and entry.get("composer") == 1 for entry in trace))
        points = [args[args.index("--xy") + 1] for args, _ in cli.calls if args[0] == "mouse-click"]
        self.assertEqual(points, ["110,25", "110,95", "370,360"])
        self.assertEqual(cli.composer, "body")
        self.assertEqual(cli.sent, 0)

    def test_post_popup_partial_or_ambiguous_target_never_falls_back_to_chat_row(self):
        from dataclasses import replace
        for problem in ("headers", "composers", "missing_composer", "missing_send", "unknown", "draft"):
            cli = CoordinateCLI()
            cli.popup_opens_chat = True
            cli.chat_row_toggles_open_chat = True
            cli.hide_closed_chat_controls = True
            if problem == "draft":
                cli.composer = "existing private draft"
            state = cli.state

            def post_popup_state(window):
                result = state(window)
                if window != "w-main" or not cli.chat_open:
                    return result
                nodes = result["tree"]["children"]
                if problem == "headers":
                    nodes.append({**nodes[2], "ref_id": "@duplicate:header"})
                elif problem == "composers":
                    nodes.append({**nodes[3], "ref_id": "@duplicate:composer"})
                elif problem == "missing_composer":
                    nodes.pop(3)
                elif problem == "missing_send":
                    nodes.pop(4)
                elif problem == "unknown":
                    nodes[3].pop("value", None)
                return result

            cli.state = post_popup_state
            run = cli.__call__

            def read_unknown(args, stdin=None):
                if args[0] == "get":
                    return {"ok": True, "data": {"ref": args[1], "property": "value", "value": None}}
                return run(args, stdin)

            row = Selector("option", name_first_line="{header_title}", required_action="Select")
            driver = AgentDesktopDriver(replace(self.layout(), chat_row=row), run=read_unknown,
                                        allow_physical=True)
            with self.subTest(problem=problem), self.assertRaises(LinuxSenderError):
                driver.prepare_text(Target("wxid_friend", "Fixture Friend"), "body")
            points = [args[args.index("--xy") + 1] for args, _ in cli.calls if args[0] == "mouse-click"]
            self.assertEqual(points, ["110,25", "110,95"])
            self.assertEqual([stdin for args, stdin in cli.calls if args[0] == "clipboard-set"],
                             ["Fixture Friend"])
            self.assertEqual(cli.sent, 0)

    def test_ready_header_ambiguity_or_missing_controls_never_falls_back_to_search(self):
        for problem in ("headers", "composers", "missing_composer", "missing_send"):
            cli = CoordinateCLI()
            cli.chat_open = True
            state = cli.state

            def invalid_current_chat(window):
                result = state(window)
                if window != "w-main":
                    return result
                nodes = result["tree"]["children"]
                if problem == "headers":
                    nodes.append({**nodes[2], "ref_id": "@duplicate:header"})
                elif problem == "composers":
                    nodes.append({**nodes[3], "ref_id": "@duplicate:composer"})
                elif problem == "missing_composer":
                    nodes.pop(3)
                else:
                    nodes.pop(4)
                return result

            cli.state = invalid_current_chat
            with self.subTest(problem=problem), self.assertRaises(LinuxSenderError):
                self.driver(cli).prepare_text(Target("wxid_friend", "Fixture Friend"), "body")
            self.assertFalse(any(args[0] in ("clipboard-set", "mouse-click", "press") for args, _ in cli.calls))

    def test_ready_header_with_draft_or_unknown_value_never_navigates_or_writes(self):
        for problem in ("draft", "unknown"):
            cli = CoordinateCLI()
            cli.chat_open = True
            if problem == "draft":
                cli.composer = "existing private draft"
            state = cli.state

            def unknown_value(window):
                result = state(window)
                if window == "w-main" and problem == "unknown":
                    result["tree"]["children"][3].pop("value", None)
                return result

            cli.state = unknown_value
            run = cli.__call__

            def read_unknown(args, stdin=None):
                if args[0] == "get":
                    return {"ok": True, "data": {"ref": args[1], "property": "value", "value": None}}
                return run(args, stdin)

            driver = AgentDesktopDriver(self.layout(), run=read_unknown, allow_physical=True)
            with self.subTest(problem=problem), self.assertRaises(LinuxSenderError):
                driver.prepare_text(Target("wxid_friend", "Fixture Friend"), "body")
            self.assertFalse(any(args[0] in ("clipboard-set", "mouse-click", "press") for args, _ in cli.calls))
            self.assertEqual(cli.composer, "existing private draft" if problem == "draft" else "")

    def test_current_chat_with_open_popup_is_not_treated_as_ready(self):
        cli = CoordinateCLI()
        cli.chat_open = True
        cli.popup = True
        with self.assertRaises(LinuxSenderError):
            self.driver(cli).prepare_text(Target("wxid_friend", "Fixture Friend"), "body")
        self.assertFalse(any(args[0] in ("clipboard-set", "mouse-click", "press") for args, _ in cli.calls))

    def test_partial_current_chat_snapshot_cannot_enable_fast_path_or_navigation(self):
        cli = CoordinateCLI()
        cli.chat_open = True
        run = cli.__call__

        def partial(args, stdin=None):
            result = run(args, stdin)
            if args[0] == "snapshot":
                result["data"]["complete"] = False
            return result

        driver = AgentDesktopDriver(self.layout(), run=partial, allow_physical=True)
        with self.assertRaises(LinuxSenderError) as caught:
            driver.prepare_text(Target("wxid_friend", "Fixture Friend"), "body")
        self.assertEqual(caught.exception.code, "INCOMPLETE_TREE")
        self.assertFalse(any(args[0] in ("clipboard-set", "mouse-click", "press") for args, _ in cli.calls))

    def test_explicit_coordinate_clipboard_route_pastes_unicode_once_and_selects_owned_popup(self):
        cli = CoordinateCLI()
        driver = self.driver(cli)
        target = Target("wxid_friend", "Fixture Friend")
        driver.prepare_text(target, "中文 😀\nline two")
        self.assertEqual(cli.composer, "中文 😀\nline two")
        driver.submit_text(target, "中文 😀\nline two")
        self.assertEqual(cli.sent, 1)
        self.assertEqual(sum(args[:2] == ["press", "ctrl+v"] for args, _ in cli.calls), 2)
        self.assertFalse(any(args[0] in ("get", "click", "type", "set-value") for args, _ in cli.calls))
        for args, _ in cli.calls:
            if args[0] == "press":
                self.assertIn("w-main", args)
                self.assertIn("--headed", args)

    def test_bad_bounds_or_clipboard_mismatch_cannot_deliver_gui_input(self):
        for kind in ("bounds", "clipboard"):
            cli = CoordinateCLI()
            if kind == "bounds":
                cli.search_width = 0
            else:
                cli.clipboard_mismatch = True
            with self.subTest(kind=kind), self.assertRaises(LinuxSenderError):
                self.driver(cli).prepare_text(Target("wxid_friend", "Fixture Friend"), "body")
            self.assertFalse(any(args[0] in ("mouse-click", "press") for args, _ in cli.calls))

    def test_foreign_titled_or_ambiguous_popups_never_receive_target_click(self):
        for kind in ("pid", "title", "multiple"):
            cli = CoordinateCLI()
            if kind == "pid":
                cli.popup_pid = 99
            elif kind == "title":
                cli.popup_title = "Unverified Popup"
            else:
                cli.extra_popup = True
            with self.subTest(kind=kind), self.assertRaises(LinuxSenderError):
                self.driver(cli).prepare_text(Target("wxid_friend", "Fixture Friend"), "body")
            self.assertFalse(cli.chat_open)
            self.assertEqual(cli.sent, 0)
            self.assertEqual(cli.composer, "")

    def test_existing_composer_draft_is_not_overwritten(self):
        cli = CoordinateCLI()
        cli.composer = "preserve existing draft"
        with self.assertRaises(LinuxSenderError):
            self.driver(cli).prepare_text(Target("wxid_friend", "Fixture Friend"), "body")
        self.assertEqual(cli.composer, "preserve existing draft")
        self.assertEqual(cli.sent, 0)

    def test_lost_main_focus_is_restored_with_explicit_headed_window_scope(self):
        cli = CoordinateCLI()
        cli.main_focused = False
        self.driver(cli).prepare_text(Target("wxid_friend", "Fixture Friend"), "body")
        focuses = [args for args, _ in cli.calls if args[0] == "focus-window"]
        self.assertEqual(focuses, [["focus-window", "--app", "fixture-app",
                                    "--window-id", "w-main", "--headed"]] * 2)
        self.assertEqual(cli.composer, "body")

    def test_main_activation_is_explicit_even_if_inventory_already_reports_focused(self):
        cli = CoordinateCLI()
        self.driver(cli)._focus_main()
        self.assertEqual([args for args, _ in cli.calls if args[0] == "focus-window"],
                         [["focus-window", "--app", "fixture-app", "--window-id", "w-main", "--headed"]])

    def test_chat_row_is_clicked_once_then_observed_composer_is_awaited(self):
        from dataclasses import replace
        cli = CoordinateCLI()
        cli.popup_opens_chat = False
        cli.chat_row_opens_async = True
        cli.composer_name = "Fixture Composer"
        row = Selector("option", name_first_line="{header_title}", required_action="Select")
        layout = replace(self.layout(), chat_row=row,
                         composer=Selector("textfield", name="Fixture Composer", native_id="composer"))
        driver = AgentDesktopDriver(layout, run=cli, allow_physical=True, timeout=20)
        driver.prepare_text(Target("wxid_friend", "Fixture Friend"), "body")
        self.assertEqual(cli.composer, "body")
        row_clicks = [args for args, _ in cli.calls
                      if args[0] == "mouse-click" and args[args.index("--xy") + 1] == "110,240"]
        self.assertEqual(len(row_clicks), 1)
        waits = [args for args, _ in cli.calls if args[0] == "snapshot" and "--wait-for" in args]
        self.assertEqual(len(waits), 1)
        self.assertEqual(waits[0][waits[0].index("--wait-for") + 1], "textfield:Fixture Composer")
        self.assertLessEqual(int(waits[0][waits[0].index("--wait-timeout") + 1]), 20000)

    def test_chat_readiness_timeout_never_reclicks_row_or_writes_body(self):
        from dataclasses import replace
        cli = CoordinateCLI()
        cli.popup_opens_chat = False
        cli.chat_row_opens_async = True
        cli.composer_name = "Fixture Composer"
        run = cli.__call__

        def never_ready(args, stdin=None):
            if args[0] == "snapshot" and "--wait-for" in args:
                cli.calls.append((args, stdin))
                return {"ok": False, "error": {"code": "TIMEOUT", "disposition":
                                                {"delivery": "not_delivered", "retry": "safe"}}}
            return run(args, stdin)

        row = Selector("option", name_first_line="{header_title}", required_action="Select")
        layout = replace(self.layout(), chat_row=row,
                         composer=Selector("textfield", name="Fixture Composer", native_id="composer"))
        driver = AgentDesktopDriver(layout, run=never_ready, allow_physical=True)
        with self.assertRaises(AgentDesktopError):
            driver.prepare_text(Target("wxid_friend", "Fixture Friend"), "body")
        self.assertEqual(cli.composer, "")
        self.assertEqual(cli.sent, 0)
        self.assertEqual(sum(args[0] == "mouse-click" and args[args.index("--xy") + 1] == "110,240"
                             for args, _ in cli.calls), 1)

    def test_draft_changed_during_account_guard_is_never_submitted(self):
        cli = CoordinateCLI()
        cli.chat_open = True
        cli.composer = "requested text"
        driver = self.driver(cli)

        def slow_guard_changes_draft():
            cli.composer = "changed draft during account check"

        with self.assertRaises(LinuxSenderError):
            driver.submit_text(Target("wxid_friend", "Fixture Friend"), "requested text",
                               before_submit=slow_guard_changes_draft)
        self.assertEqual(cli.sent, 0)
        self.assertEqual(cli.composer, "changed draft during account check")

    def test_focus_lost_during_account_guard_prevents_coordinate_input(self):
        cli = CoordinateCLI()
        cli.chat_open = True
        cli.composer = "requested text"
        windows = cli.windows
        focused = [True]

        def current_windows():
            result = windows()
            result[0]["is_focused"] = focused[0]
            return result

        cli.windows = current_windows
        driver = self.driver(cli)
        with self.assertRaises(LinuxSenderError):
            driver.submit_text(Target("wxid_friend", "Fixture Friend"), "requested text",
                               before_submit=lambda: focused.__setitem__(0, False))
        self.assertEqual(cli.sent, 0)
        self.assertFalse(any(args[0] == "mouse-click" for args, _ in cli.calls))

    def test_send_bounds_are_reobserved_after_the_account_guard(self):
        cli = CoordinateCLI()
        cli.chat_open = True
        cli.composer = "requested"
        state = cli.state
        send_x = [500]

        def current_state(window):
            result = state(window)
            if window == "w-main":
                result["tree"]["children"][4]["bounds"]["x"] = send_x[0]
            return result

        cli.state = current_state
        run = cli.__call__

        def moved_button(args, stdin=None):
            if args[0] == "mouse-click" and args[args.index("--xy") + 1] == "480,465":
                cli.calls.append((args, stdin))
                cli.sent += 1
                cli.composer = ""
                return {"ok": True, "data": {}}
            return run(args, stdin)

        driver = AgentDesktopDriver(self.layout(), run=moved_button, allow_physical=True)
        driver.submit_text(Target("wxid_friend", "Fixture Friend"), "requested",
                           before_submit=lambda: send_x.__setitem__(0, 450))
        points = [args[args.index("--xy") + 1] for args, _ in cli.calls if args[0] == "mouse-click"]
        self.assertEqual(points, ["480,465"])
        self.assertEqual(cli.sent, 1)

    def test_explicit_chat_row_uses_exact_first_line_after_popup_is_closed(self):
        from dataclasses import replace
        cli = CoordinateCLI()
        cli.popup_opens_chat = False
        row = Selector("option", name_first_line="{header_title}", required_action="Select")
        driver = AgentDesktopDriver(replace(self.layout(), chat_row=row), run=cli, allow_physical=True)
        driver.prepare_text(Target("wxid_friend", "Fixture Friend"), "body")
        self.assertTrue(cli.chat_open)
        self.assertEqual(cli.composer, "body")

    def test_chat_row_is_not_clicked_while_popup_is_still_visible(self):
        from dataclasses import replace
        cli = CoordinateCLI()
        cli.popup_opens_chat = False
        cli.popup_stays_open = True
        row = Selector("option", name_first_line="{header_title}", required_action="Select")
        driver = AgentDesktopDriver(replace(self.layout(), chat_row=row), run=cli, allow_physical=True)
        with self.assertRaises(LinuxSenderError):
            driver.prepare_text(Target("wxid_friend", "Fixture Friend"), "body")
        self.assertFalse(cli.chat_open)
        self.assertEqual(cli.composer, "")
        self.assertEqual(sum(args[0] == "wait" for args, _ in cli.calls), 1)
        self.assertEqual(sum(args[0] == "focus-window" for args, _ in cli.calls), 1)

    def test_closed_popup_with_unfocused_main_restores_focus_before_chat_row(self):
        from dataclasses import replace
        cli = CoordinateCLI()
        cli.popup_opens_chat = False
        cli.lose_focus_after_popup = True
        row = Selector("option", name_first_line="{header_title}", required_action="Select")
        driver = AgentDesktopDriver(replace(self.layout(), chat_row=row), run=cli, allow_physical=True)
        driver.prepare_text(Target("wxid_friend", "Fixture Friend"), "body")
        self.assertEqual(cli.composer, "body")
        mouse_points = [args[args.index("--xy") + 1] for args, _ in cli.calls if args[0] == "mouse-click"]
        self.assertEqual(mouse_points.count("110,95"), 1)
        focus_index = max(i for i, (args, _) in enumerate(cli.calls) if args[0] == "focus-window")
        popup_index = next(i for i, (args, _) in enumerate(cli.calls)
                           if args[0] == "mouse-click" and args[args.index("--xy") + 1] == "110,95")
        row_index = next(i for i, (args, _) in enumerate(cli.calls)
                         if args[0] == "mouse-click" and args[args.index("--xy") + 1] == "110,240")
        self.assertLess(focus_index, row_index)
        self.assertLess(popup_index, focus_index)
        self.assertIn("--headed", cli.calls[focus_index][0])

    def test_async_popup_close_waits_once_by_known_id_and_never_reclicks_popup(self):
        from dataclasses import replace
        cli = CoordinateCLI()
        cli.popup_opens_chat = False
        cli.popup_stays_open = True
        cli.close_popup_during_wait = True
        cli.lose_focus_after_popup = True
        row = Selector("option", name_first_line="{header_title}", required_action="Select")
        driver = AgentDesktopDriver(replace(self.layout(), chat_row=row), run=cli,
                                    allow_physical=True, timeout=30)
        driver.prepare_text(Target("wxid_friend", "Fixture Friend"), "body")
        waits = [args for args, _ in cli.calls if args[0] == "wait"]
        self.assertEqual(len(waits), 1)
        self.assertEqual(waits[0][:3], ["wait", "--event", "window-closed"])
        self.assertIn("w-popup", waits[0])
        self.assertLessEqual(int(waits[0][waits[0].index("--timeout") + 1]), 20000)
        self.assertEqual(sum(args[0] == "mouse-click" and args[args.index("--xy") + 1] == "110,95"
                             for args, _ in cli.calls), 1)
        self.assertEqual(cli.composer, "body")

    def test_popup_close_race_is_confirmed_by_inventory_after_wait_error(self):
        from dataclasses import replace
        cli = CoordinateCLI()
        cli.popup_opens_chat = False
        cli.popup_stays_open = True
        cli.close_popup_during_wait = True
        cli.lose_focus_after_popup = True
        run = cli.__call__

        def missed_close_event(args, stdin=None):
            result = run(args, stdin)
            if args[0] == "wait":
                return {"ok": False, "error": {"code": "TIMEOUT", "disposition":
                                                {"delivery": "not_delivered", "retry": "safe"}}}
            return result

        row = Selector("option", name_first_line="{header_title}", required_action="Select")
        driver = AgentDesktopDriver(replace(self.layout(), chat_row=row), run=missed_close_event,
                                    allow_physical=True)
        driver.prepare_text(Target("wxid_friend", "Fixture Friend"), "body")
        self.assertEqual(cli.composer, "body")
        self.assertEqual(sum(args[0] == "wait" for args, _ in cli.calls), 1)
        self.assertEqual(sum(args[0] == "mouse-click" and args[args.index("--xy") + 1] == "110,95"
                             for args, _ in cli.calls), 1)

    def test_changed_owner_or_extra_popup_during_close_is_refused(self):
        from dataclasses import replace
        row = Selector("option", name_first_line="{header_title}", required_action="Select")
        for change in ("owner", "extra"):
            cli = CoordinateCLI()
            cli.popup_opens_chat = False
            cli.popup_stays_open = True
            cli.lose_focus_after_popup = True
            run = cli.__call__

            def changed_popup(args, stdin=None):
                result = run(args, stdin)
                if args[0] == "mouse-click" and args[args.index("--xy") + 1] == "110,95":
                    if change == "owner":
                        cli.popup_pid = 99
                    else:
                        cli.extra_popup = True
                return result

            driver = AgentDesktopDriver(replace(self.layout(), chat_row=row), run=changed_popup,
                                        allow_physical=True)
            with self.subTest(change=change), self.assertRaises(LinuxSenderError):
                driver.prepare_text(Target("wxid_friend", "Fixture Friend"), "body")
            self.assertFalse(cli.chat_open)
            self.assertEqual(cli.composer, "")
            self.assertFalse(any(args[0] == "wait" for args, _ in cli.calls))
            self.assertEqual(sum(args[0] == "focus-window" for args, _ in cli.calls), 1)

    def test_changed_window_instance_is_refused_before_coordinate_input(self):
        cli = CoordinateCLI()
        driver = self.driver(cli)
        driver._coordinate_process = (15, "old-instance")
        with self.assertRaises(LinuxSenderError):
            driver.prepare_text(Target("wxid_friend", "Fixture Friend"), "body")
        self.assertFalse(any(args[0] in ("mouse-click", "press") for args, _ in cli.calls))

    def test_disabled_or_offscreen_coordinate_target_is_not_clicked(self):
        for token in ("disabled", "offscreen"):
            cli = CoordinateCLI()
            state = cli.state

            def unavailable_state(window):
                result = state(window)
                if window == "w-main":
                    result["tree"]["children"][0]["states"] = [token]
                return result

            cli.state = unavailable_state
            with self.subTest(token=token), self.assertRaises(LinuxSenderError):
                self.driver(cli).prepare_text(Target("wxid_friend", "Fixture Friend"), "body")
            self.assertFalse(any(args[0] in ("mouse-click", "press") for args, _ in cli.calls))

    def test_control_bounds_must_stay_inside_window_and_have_an_integer_input_point(self):
        for bounds in ({"x": -1, "y": 10, "width": 200, "height": 30},
                       {"x": 10, "y": 10, "width": float("nan"), "height": 30},
                       {"x": 10.9, "y": 10, "width": 0.01, "height": 30}):
            cli = CoordinateCLI()
            state = cli.state

            def invalid_state(window):
                result = state(window)
                if window == "w-main":
                    result["tree"]["children"][0]["bounds"] = bounds
                return result

            cli.state = invalid_state
            with self.subTest(bounds=bounds), self.assertRaises(LinuxSenderError):
                self.driver(cli).prepare_text(Target("wxid_friend", "Fixture Friend"), "body")
            self.assertFalse(any(args[0] in ("mouse-click", "press") for args, _ in cli.calls))

    def test_missing_snapshot_value_requires_a_successful_get_and_never_means_empty(self):
        cli = CoordinateCLI()
        state = cli.state

        def omitted_value(window):
            result = state(window)
            if window == "w-main":
                result["tree"]["children"][3].pop("value", None)
            return result

        cli.state = omitted_value
        run = cli.__call__

        def ambiguous_get(args, stdin=None):
            if args[0] == "get":
                return {"ok": False, "error": {"code": "AMBIGUOUS_TARGET", "disposition":
                                                {"delivery": "not_delivered", "retry": "safe"}}}
            return run(args, stdin)

        driver = AgentDesktopDriver(self.layout(), run=ambiguous_get, allow_physical=True)
        with self.assertRaises(AgentDesktopError):
            driver.prepare_text(Target("wxid_friend", "Fixture Friend"), "body")
        self.assertEqual(cli.composer, "")
        self.assertEqual(cli.sent, 0)


def snapshot(*nodes, complete=True):
    return {"complete": complete, "tree": {"role": "window", "children": list(nodes)}}


def node(role, name, ref, **kwargs):
    return {"role": role, "name": name, "ref_id": ref, **kwargs}


class AgentDesktopDriverTests(unittest.TestCase):
    def layout(self):
        return DesktopLayout("fixture-app", Selector("textfield", native_id="search"),
                             Selector("listitem", name="{title}"),
                             Selector("label", name="{title}"),
                             Selector("textfield", native_id="composer"),
                             Selector("button", name="Fixture Send"))

    def chat(self, text=""):
        return snapshot(node("label", "Fixture Friend", "@s:e1"),
                        node("textfield", "", "@s:e2", native_id="composer", value=text),
                        node("button", "Fixture Send", "@s:e3"))

    def focused_layout(self):
        from dataclasses import replace
        return replace(self.layout(), window_id="w-fixture", activation_mode="focused_return")

    def focused_chat(self, text=""):
        state = self.chat(text)
        state["tree"]["children"][2]["ref_id"] = "@focused:e3"
        state["tree"]["children"][2]["states"] = ["focused"]
        return state

    def test_filehelper_header_alias_and_required_action_preserve_exact_search_identity(self):
        from dataclasses import replace
        layout = replace(self.layout(), header=Selector("statictext", name="{title}", required_action="SetFocus"),
                         header_aliases={"filehelper": "File Transfer"})
        driver = AgentDesktopDriver(layout)
        target = Target("filehelper", "文件传输助手")
        state = self.chat()["tree"]
        state["children"][0] = node("statictext", "File Transfer", "@h:e1", available_actions=["SetFocus"])
        state["children"].append(node("statictext", "File Transfer", "@h:e2", available_actions=[]))
        composer, _ = driver._chat_controls(state, target)
        self.assertEqual(composer["native_id"], "composer")
        result = snapshot(node("listitem", "文件传输助手", "@r:e1"))["tree"]
        self.assertEqual(driver._one(result, layout.search_result, target)["name"], "文件传输助手")
        with self.assertRaises(LinuxSenderError):
            driver._chat_controls(state, Target("wxid_other", "文件传输助手"))

    def test_unobserved_header_aliases_are_not_accepted(self):
        from dataclasses import replace
        for aliases in ({"wxid_other": "File Transfer"}, {"filehelper": "Different Chat"}):
            with self.subTest(aliases=aliases), self.assertRaises(ValueError):
                AgentDesktopDriver(replace(self.layout(), header_aliases=aliases))

    def test_focused_return_is_explicit_and_requires_physical_permission(self):
        with self.assertRaises(ValueError):
            AgentDesktopDriver(self.focused_layout())

    def test_focused_return_verifies_ref_focus_and_uses_one_window_scoped_return(self):
        cli = FakeCLI([self.chat("expected"), self.focused_chat("expected"), self.chat()])
        driver = AgentDesktopDriver(self.focused_layout(), run=cli, allow_physical=True)
        guarded = []
        driver.submit_text(Target("wxid_friend", "Fixture Friend"), "expected",
                           before_submit=lambda: guarded.append(cli.calls[-1][0][0]))
        self.assertEqual(guarded, ["focus"])
        focuses = [args for args, _ in cli.calls if args[0] == "focus"]
        self.assertEqual(focuses, [["focus", "@s:e3", "--headed"]])
        presses = [args for args, _ in cli.calls if args[0] == "press"]
        self.assertEqual(presses, [["press", "return", "--app", "fixture-app",
                                   "--window-id", "w-fixture", "--headed"]])
        self.assertFalse(any(args[0] == "click" for args, _ in cli.calls))
        self.assertIn(["is", "@focused:e3", "--property", "focused"], [args for args, _ in cli.calls])

    def test_focused_return_cannot_submit_if_focus_or_composer_recheck_fails(self):
        for state in (self.chat("expected"), self.focused_chat("changed")):
            cli = FakeCLI([self.chat("expected"), state])
            driver = AgentDesktopDriver(self.focused_layout(), run=cli, allow_physical=True)
            with self.subTest(state=state), self.assertRaises(LinuxSenderError):
                driver.submit_text(Target("wxid_friend", "Fixture Friend"), "expected")
            self.assertFalse(any(args[0] in ("click", "press") for args, _ in cli.calls))

    def test_focused_return_rechecks_draft_after_account_guard(self):
        cli = FakeCLI([self.chat("requested"), self.focused_chat("requested"), self.chat("changed")])
        driver = AgentDesktopDriver(self.focused_layout(), run=cli, allow_physical=True)

        def change_during_guard():
            cli.current["tree"]["children"][1]["value"] = "changed"
            cli.snapshots[0]["tree"]["children"][1]["value"] = "changed"

        with self.assertRaises(LinuxSenderError):
            driver.submit_text(Target("wxid_friend", "Fixture Friend"), "requested",
                               before_submit=change_during_guard)
        self.assertFalse(any(args[0] in ("click", "press") for args, _ in cli.calls))

    def test_semantic_click_rechecks_draft_after_account_guard(self):
        cli = FakeCLI([self.chat("requested"), self.chat("changed")])
        driver = AgentDesktopDriver(self.layout(), run=cli)
        with self.assertRaises(LinuxSenderError):
            driver.submit_text(Target("wxid_friend", "Fixture Friend"), "requested",
                               before_submit=lambda: None)
        self.assertFalse(any(args[0] == "click" for args, _ in cli.calls))

    def test_focused_return_is_used_for_search_selection_and_single_send(self):
        cli = FakeCLI([snapshot(node("textfield", "", "@a:e1", native_id="search", value="old")),
                       snapshot(node("textfield", "", "@b:e1", native_id="search", value="")),
                       snapshot(node("listitem", "Fixture Friend", "@c:e1")),
                       snapshot(node("listitem", "Fixture Friend", "@d:e1", states=["focused"])),
                       self.chat(), self.chat("expected"), self.chat("expected"),
                       self.focused_chat("expected"), self.chat()])
        driver = AgentDesktopDriver(self.focused_layout(), run=cli, allow_physical=True)
        target = Target("wxid_friend", "Fixture Friend")
        driver.prepare_text(target, "expected")
        driver.submit_text(target, "expected")
        mutations = [(args[0], stdin) for args, stdin in cli.calls
                     if args[0] in ("clear", "type", "focus", "press", "click")]
        self.assertEqual(mutations, [("clear", None), ("type", "Fixture Friend"),
                                     ("focus", None), ("press", None), ("type", "expected"),
                                     ("focus", None), ("press", None)])

    def test_uncertain_focused_return_is_not_repeated(self):
        cli = FakeCLI([self.chat("expected"), self.focused_chat("expected")])
        original = cli.__call__

        def uncertain_return(args, stdin=None):
            if args[0] == "press":
                cli.calls.append((args, stdin))
                return {"ok": False, "error": {"code": "TIMEOUT", "disposition":
                                                {"delivery": "uncertain", "retry": "unsafe"}}}
            return original(args, stdin)

        driver = AgentDesktopDriver(self.focused_layout(), run=uncertain_return, allow_physical=True)
        driver.prepare_text = lambda target, text: None
        sender = LinuxSender(None, driver, resolver=FakeResolver(), readback=FakeReadback(),
                             own_username="wxid_reader_a", account_guard=lambda expected: True,
                             verify_timeout=0)
        self.assertEqual(sender.send_text("wxid_friend", "expected")["status"], "uncertain")
        self.assertEqual(sum(args[0] == "press" for args, _ in cli.calls), 1)
        self.assertFalse(any(args[0] == "click" for args, _ in cli.calls))

    def test_search_and_unicode_input_use_fresh_refs_and_stdin(self):
        cli = FakeCLI([snapshot(node("textfield", "", "@a:e1", native_id="search")),
                       snapshot(node("listitem", "Fixture Friend", "@b:e1")),
                       self.chat(), self.chat("中文 😀"), self.chat("中文 😀"), self.chat()])
        driver = AgentDesktopDriver(self.layout(), run=cli)
        target = Target("wxid_friend", "Fixture Friend")
        driver.prepare_text(target, "中文 😀")
        driver.submit_text(target, "中文 😀")
        actions = [(args[0], args[1], stdin) for args, stdin in cli.calls
                   if args[0] in ("set-value", "type", "clear", "click")]
        self.assertEqual(actions, [("set-value", "@a:e1", "Fixture Friend"),
                                   ("click", "@b:e1", None),
                                   ("set-value", "@s:e2", "中文 😀"),
                                   ("click", "@s:e3", None)])
        self.assertTrue(all("中文" not in arg for args, _ in cli.calls for arg in args))
        self.assertTrue(all("--headed" not in args for args, _ in cli.calls))

    def test_physical_mode_requires_explicit_opt_in_and_pinned_window(self):
        with self.assertRaises(ValueError):
            AgentDesktopDriver(self.layout(), allow_physical=True)
        for invalid in ("false", "true", 1, None):
            with self.subTest(invalid=invalid), self.assertRaises(TypeError):
                AgentDesktopDriver(self.layout(), allow_physical=invalid)

    def test_physical_mode_clears_search_and_types_once_with_verified_values(self):
        layout = self.layout()
        from dataclasses import replace
        layout = replace(layout, window_id="w-fixture")
        cli = FakeCLI([snapshot(node("textfield", "", "@a:e1", native_id="search", value="old search")),
                       snapshot(node("textfield", "", "@b:e1", native_id="search", value="")),
                       snapshot(node("listitem", "Fixture Friend", "@c:e1")),
                       self.chat(), self.chat("中文 😀"), self.chat("中文 😀"), self.chat()])
        driver = AgentDesktopDriver(layout, run=cli, allow_physical=True)
        target = Target("wxid_friend", "Fixture Friend")
        driver.prepare_text(target, "中文 😀")
        driver.submit_text(target, "中文 😀")
        actions = [(args, stdin) for args, stdin in cli.calls
                   if args[0] in ("set-value", "type", "clear", "click")]
        self.assertEqual([(args[0], stdin) for args, stdin in actions],
                         [("clear", None), ("type", "Fixture Friend"), ("click", None),
                          ("type", "中文 😀"), ("click", None)])
        self.assertTrue(all("--headed" in args and args[1].startswith("@") for args, _ in actions))
        self.assertTrue(all("w-fixture" in args for args, _ in cli.calls if args[0] == "snapshot"))

    def test_unreadable_get_value_is_never_treated_as_an_empty_composer(self):
        state = self.chat()
        cli = FakeCLI([state])
        original = cli.__call__

        def unreadable(args, stdin=None):
            if args[0] == "get":
                return {"ok": True, "data": {"ref": args[1], "property": "value", "value": None}}
            return original(args, stdin)

        driver = AgentDesktopDriver(self.layout(), run=unreadable)
        with self.assertRaises(LinuxSenderError):
            driver.submit_text(Target("wxid_friend", "Fixture Friend"), "expected")
        self.assertFalse(any(args[0] == "click" for args, _ in cli.calls))

    def test_partial_tree_duplicate_target_or_wrong_header_is_refused(self):
        for state in (snapshot(complete=False),
                      snapshot(node("listitem", "Fixture Friend", "@b:e1"),
                               node("listitem", "Fixture Friend", "@b:e2")),
                      self.chat("different")):
            with self.subTest(state=state):
                cli = FakeCLI([state])
                driver = AgentDesktopDriver(self.layout(), run=cli)
                with self.assertRaises(LinuxSenderError):
                    driver.submit_text(Target("wxid_friend", "Fixture Friend"), "expected")
                self.assertTrue(all(args[0] in ("snapshot", "get") for args, _ in cli.calls))

    def test_existing_draft_is_never_overwritten(self):
        cli = FakeCLI([snapshot(node("textfield", "", "@a:e1", native_id="search")),
                       snapshot(node("listitem", "Fixture Friend", "@b:e1")),
                       self.chat("private existing draft")])
        driver = AgentDesktopDriver(self.layout(), run=cli)
        with self.assertRaises(LinuxSenderError) as caught:
            driver.prepare_text(Target("wxid_friend", "Fixture Friend"), "expected")
        self.assertEqual(caught.exception.code, "DRAFT_PRESENT")
        writes = [stdin for args, stdin in cli.calls if args[0] == "set-value"]
        self.assertEqual(writes, ["Fixture Friend"])

    def test_duplicate_search_results_are_not_clicked(self):
        cli = FakeCLI([snapshot(node("textfield", "", "@a:e1", native_id="search")),
                       snapshot(node("listitem", "Fixture Friend", "@b:e1"),
                                node("listitem", "Fixture Friend", "@b:e2"))])
        driver = AgentDesktopDriver(self.layout(), run=cli)
        with self.assertRaises(LinuxSenderError) as caught:
            driver.prepare_text(Target("wxid_friend", "Fixture Friend"), "expected")
        self.assertEqual(caught.exception.code, "AMBIGUOUS_TARGET")
        self.assertFalse(any(args[0] == "click" for args, _ in cli.calls))

    def test_conversation_switch_before_submit_is_refused(self):
        state = self.chat("expected")
        state["tree"]["children"][0]["name"] = "Other Conversation"
        cli = FakeCLI([state])
        driver = AgentDesktopDriver(self.layout(), run=cli)
        with self.assertRaises(LinuxSenderError):
            driver.submit_text(Target("wxid_friend", "Fixture Friend"), "expected")
        self.assertFalse(any(args[0] == "click" for args, _ in cli.calls))

    def test_hidden_composer_or_hidden_ancestor_never_submits(self):
        for ancestor_hidden in (False, True):
            state = self.chat("expected")
            composer = state["tree"]["children"][1]
            if ancestor_hidden:
                state["tree"]["children"][1] = {
                    "role": "group", "states": ["hidden"], "children": [composer]}
            else:
                composer["states"] = ["hidden"]
            with self.subTest(ancestor_hidden=ancestor_hidden):
                cli = FakeCLI([state])
                driver = AgentDesktopDriver(self.layout(), run=cli)
                with self.assertRaises(LinuxSenderError):
                    driver.submit_text(Target("wxid_friend", "Fixture Friend"), "expected")
                self.assertFalse(any(args[0] == "click" for args, _ in cli.calls))

    def test_hidden_twin_is_excluded_and_post_submit_observes_fresh_tree(self):
        state = self.chat("expected")
        state["tree"]["children"].append({"role": "group", "states": ["hidden"], "children": [
            node("textfield", "", "@hidden:e2", native_id="composer", value="other draft")]})
        cli = FakeCLI([state, self.chat()])
        driver = AgentDesktopDriver(self.layout(), run=cli)
        driver.submit_text(Target("wxid_friend", "Fixture Friend"), "expected")
        self.assertEqual([args[0] for args, _ in cli.calls], ["snapshot", "get", "click", "snapshot"])
        self.assertEqual(cli.calls[2][0], ["click", "@s:e3"])

    def test_post_submit_observation_failure_is_never_safe_to_repeat(self):
        cli = FakeCLI([self.chat("expected"), snapshot(complete=False)])
        driver = AgentDesktopDriver(self.layout(), run=cli)
        with self.assertRaises(AgentDesktopError) as caught:
            driver.submit_text(Target("wxid_friend", "Fixture Friend"), "expected")
        self.assertEqual(caught.exception.code, "POST_SUBMIT_UNVERIFIED")
        self.assertFalse(caught.exception.safe)
        self.assertEqual([args[0] for args, _ in cli.calls], ["snapshot", "get", "click", "snapshot"])

    def test_final_account_guard_runs_after_value_read_and_before_send_click(self):
        cli = FakeCLI([self.chat("expected")])
        driver = AgentDesktopDriver(self.layout(), run=cli)

        def deny_after_observation():
            self.assertEqual([args[0] for args, _ in cli.calls], ["snapshot", "get"])
            raise LinuxSenderError("GUI_ACCOUNT_UNVERIFIED", "Account changed during observation")

        with self.assertRaises(LinuxSenderError):
            driver.submit_text(Target("wxid_friend", "Fixture Friend"), "expected",
                               before_submit=deny_after_observation)
        self.assertFalse(any(args[0] == "click" for args, _ in cli.calls))

    def test_headless_refusal_never_switches_to_physical_mode(self):
        calls = []
        state = snapshot(node("textfield", "", "@a:e1", native_id="search"))

        def refusing(args, stdin=None):
            calls.append(args)
            if args[0] == "snapshot":
                return {"ok": True, "data": state}
            return {"ok": False, "error": {"code": "POLICY_DENIED", "disposition":
                                            {"delivery": "not_delivered", "retry": "safe"}}}

        driver = AgentDesktopDriver(self.layout(), run=refusing)
        with self.assertRaises(AgentDesktopError):
            driver.prepare_text(Target("wxid_friend", "Fixture Friend"), "expected")
        self.assertEqual([args[0] for args in calls], ["snapshot", "set-value"])
        self.assertFalse(any("--headed" in args for args in calls))
        self.assertFalse(driver.allow_physical)

    def test_physical_text_limit_is_checked_before_any_ui_action(self):
        from dataclasses import replace
        cli = FakeCLI([])
        driver = AgentDesktopDriver(replace(self.layout(), window_id="w-fixture"),
                                    run=cli, allow_physical=True)
        with self.assertRaises(ValueError):
            driver.prepare_text(Target("wxid_friend", "Fixture Friend"), "中" * 3334)
        self.assertEqual(cli.calls, [])

    def test_cli_delivery_disposition_is_preserved(self):
        def failed(args, stdin=None):
            return {"ok": False, "error": {"code": "STALE_REF", "disposition":
                                            {"delivery": "not_delivered", "retry": "safe"}}}
        driver = AgentDesktopDriver(self.layout(), run=failed)
        with self.assertRaises(AgentDesktopError) as caught:
            driver.submit_text(Target("wxid_friend", "Fixture Friend"), "text")
        self.assertTrue(caught.exception.safe)

    def test_cli_uses_argv_and_unicode_stdin_without_shell_or_body_in_arguments(self):
        driver = AgentDesktopDriver(self.layout())
        content = "中文\n`touch /tmp/not-executed` $(true) 😀"
        result = SimpleNamespace(stdout='{"ok":true,"data":{}}', stderr="", returncode=0)
        with patch("wechatauto.platforms.linux_sender.subprocess.run", return_value=result) as run:
            driver._command(["set-value", "@s:e1", "--stdin"], stdin=content)
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["agent-desktop", "set-value", "@s:e1", "--stdin"])
        self.assertEqual(kwargs["input"], content)
        self.assertFalse(kwargs.get("shell", False))
        self.assertEqual(kwargs["encoding"], "utf-8")

    def test_cli_timeout_does_not_leak_message_or_claim_safe_retry(self):
        driver = AgentDesktopDriver(self.layout())
        with patch("wechatauto.platforms.linux_sender.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("private body", 1, output="private body")):
            with self.assertRaises(AgentDesktopError) as caught:
                driver._command(["click", "@s:e1"])
        self.assertFalse(caught.exception.safe)
        self.assertNotIn("private body", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
