"""Linux 官方客户端发送：无障碍目标校验、一次提交、只读数据库确认。

布局必须来自当前客户端的真实无障碍观察；本模块没有微信控件名称或坐标兜底。
持久化 ClientMsgID 去重由调用方的发送日志负责，结果不明时禁止重新调用发送。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import subprocess
import threading
import time
from typing import Callable, Mapping, Optional


class LinuxSenderError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__("%s: %s" % (code, message))


class SendNotSupported(LinuxSenderError):
    def __init__(self, message: str):
        super().__init__("NOT_SUPPORTED", message)


class AgentDesktopError(LinuxSenderError):
    def __init__(self, code: str, message: str, *, safe: bool = False):
        self.safe = safe
        super().__init__(code, message)


@dataclass(frozen=True)
class Target:
    username: str
    title: str


class ReaderIdentityResolver:
    """精确查 username；昵称仅作为经过碰撞检查的界面定位文字。"""

    def __init__(self, db):
        self.db = db

    def resolve(self, username: str) -> Target:
        if not isinstance(username, str) or not username or username != username.strip():
            raise ValueError("username must be a nonempty exact identifier")
        conn = self.db._contact_conn()
        if conn is None:
            raise LinuxSenderError("CONTACT_DB_UNAVAILABLE", "Contact database is unavailable")
        try:
            rows = conn.execute(
                "SELECT username,nick_name,remark FROM contact WHERE username=?", (username,)
            ).fetchall()
            if len(rows) != 1:
                raise LinuxSenderError("TARGET_NOT_FOUND", "Exact target username was not found")
            row = rows[0]
            title = row["remark"] or row["nick_name"]
            if not isinstance(title, str) or not title:
                raise LinuxSenderError("TARGET_UNNAMED", "Target has no verified display name")
            collisions = conn.execute(
                "SELECT username FROM contact WHERE nick_name=? OR remark=? OR username=?",
                (title, title, title),
            ).fetchall()
            if {r["username"] for r in collisions} != {username}:
                raise LinuxSenderError("AMBIGUOUS_TARGET", "Target display name is not unique")
            return Target(username, title)
        finally:
            conn.close()

    def assert_alias_unique(self, username: str, alias: str):
        conn = self.db._contact_conn()
        if conn is None:
            raise LinuxSenderError("CONTACT_DB_UNAVAILABLE", "Contact database is unavailable")
        try:
            rows = conn.execute(
                "SELECT username FROM contact WHERE nick_name=? OR remark=? OR username=?",
                (alias, alias, alias),
            ).fetchall()
            if any(row["username"] != username for row in rows):
                raise LinuxSenderError("AMBIGUOUS_TARGET", "System display alias belongs to another contact")
        finally:
            conn.close()


class ReaderReadback:
    """按分片 local_id 高水位确认，避免旧正文、同序号和 sender_id 常量误判。"""

    def __init__(self, db):
        self.db = db

    @staticmethod
    def _table(username: str) -> str:
        return "Msg_" + hashlib.md5(username.encode("utf-8")).hexdigest()

    @staticmethod
    def _exists(conn, table: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is not None

    def watermark(self, username: str) -> dict:
        if not self.db.get_self_info().get("username"):
            raise LinuxSenderError("ACCOUNT_UNVERIFIED", "Local account identity is unavailable")
        mark = {}
        table = self._table(username)
        for rel in self.db._message_dbs():
            conn = self.db._open(rel)
            try:
                mark[rel] = int(conn.execute("SELECT MAX(local_id) FROM " + table).fetchone()[0] or 0) \
                    if self._exists(conn, table) else 0
            finally:
                conn.close()
        if not mark:
            raise LinuxSenderError("MESSAGE_DB_UNAVAILABLE", "No message shard could be observed")
        return mark

    def find_text(self, username: str, text: str, mark: Mapping) -> Optional[dict]:
        own_username = self.db.get_self_info().get("username")
        if not own_username:
            raise LinuxSenderError("ACCOUNT_UNVERIFIED", "Local account identity is unavailable")
        table = self._table(username)
        for rel in self.db._message_dbs():
            # 新出现的分片可能导入旧历史，不把未观察过的分片当作本次发送证据。
            if rel not in mark:
                continue
            conn = self.db._open(rel)
            try:
                if not self._exists(conn, table):
                    continue
                ids = conn.execute(
                    "SELECT rowid FROM Name2Id WHERE user_name=?", (own_username,),
                ).fetchall()
                for identity in ids:
                    rows = conn.execute(
                        "SELECT local_id,server_id,message_content FROM " + table +
                        " WHERE local_id>? AND local_type=1 AND real_sender_id=?"
                        " ORDER BY local_id DESC LIMIT 100", (mark[rel], identity[0]),
                    ).fetchall()
                    for row in rows:
                        content = row["message_content"]
                        if isinstance(content, bytes):
                            from wechatauto.db import WeChatDB
                            content = WeChatDB._friendly_content(content, "文本")
                        if content == text and int(row["server_id"] or 0) > 0:
                            return {"local_id": row["local_id"], "server_id": row["server_id"],
                                    "shard": rel}
            finally:
                conn.close()
        return None


@dataclass(frozen=True)
class Selector:
    """仅描述已观察到的精确身份；name 可包含动态的 {title}。"""

    role: str
    name: Optional[str] = None
    native_id: Optional[str] = None
    description: Optional[str] = None
    required_action: Optional[str] = None
    name_first_line: Optional[str] = None

    def matches(self, node: Mapping, target: Target) -> bool:
        if node.get("role") != self.role:
            return False
        if self.required_action is not None and self.required_action not in node.get("available_actions", []):
            return False
        if self.name_first_line is not None:
            expected = self.name_first_line.replace("{title}", target.title).replace("{header_title}", target.title)
            if not isinstance(node.get("name"), str) or node["name"].split("\n", 1)[0] != expected:
                return False
        for key in ("name", "native_id", "description"):
            expected = getattr(self, key)
            if expected is not None and node.get(key) != expected.replace("{title}", target.title):
                return False
        return True


@dataclass(frozen=True)
class DesktopLayout:
    app: str
    search: Selector
    search_result: Selector
    header: Selector
    composer: Selector
    send: Selector
    window_id: Optional[str] = None
    activation_mode: str = "semantic"
    input_mode: str = "ref"
    header_aliases: Optional[Mapping[str, str]] = None
    chat_row: Optional[Selector] = None
    composer_input_mode: str = "clipboard"
    reuse_verified_controls: bool = False


class AgentDesktopDriver:
    """agent-desktop 的限定 ref 驱动；物理操作必须显式允许且固定窗口。"""

    def __init__(self, layout: DesktopLayout, *, executable="agent-desktop", env=None,
                 run: Optional[Callable] = None, timeout=15, allow_physical=False):
        if not layout.app:
            raise ValueError("An observed agent-desktop app name is required")
        if not isinstance(allow_physical, bool):
            raise TypeError("allow_physical must be an explicit boolean")
        if layout.activation_mode not in ("semantic", "focused_return", "observed_coordinates"):
            raise ValueError("Unsupported activation_mode")
        if layout.input_mode not in ("ref", "clipboard_coordinates"):
            raise ValueError("Unsupported input_mode")
        if layout.composer_input_mode not in ("clipboard", "set_value"):
            raise ValueError("Unsupported composer_input_mode")
        if layout.composer_input_mode == "set_value" and layout.input_mode != "clipboard_coordinates":
            raise ValueError("set_value composer input requires the clipboard_coordinates search route")
        if type(layout.reuse_verified_controls) is not bool or (layout.reuse_verified_controls and
                (layout.composer_input_mode != "set_value" or layout.activation_mode != "focused_return")):
            raise ValueError("Control reuse requires explicit semantic input and focused Return")
        if layout.header_aliases is not None and (not isinstance(layout.header_aliases, Mapping)
                or dict(layout.header_aliases) not in ({}, {"filehelper": "File Transfer"})):
            raise ValueError("Only the observed filehelper system header alias is supported")
        if layout.chat_row is not None and layout.input_mode != "clipboard_coordinates":
            raise ValueError("chat_row requires the clipboard_coordinates input route")
        if (layout.activation_mode != "semantic" or layout.input_mode != "ref") and not allow_physical:
            raise ValueError("Configured physical route requires explicit physical input permission")
        if allow_physical and not layout.window_id:
            raise ValueError("Physical input requires a pinned observed window ID")
        self.layout = layout
        self.executable = executable
        self.env = env
        self.timeout = timeout
        self.allow_physical = allow_physical
        self._runner = run or self._run_process
        self._coordinate_process = None
        self._prepared = None

    def _run_process(self, args, stdin=None):
        try:
            result = subprocess.run(
                [self.executable, *args], input=stdin, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", timeout=self.timeout * 2 + 2,
                env=self.env, check=False,
            )
            return json.loads(result.stdout)
        except (OSError, subprocess.TimeoutExpired, UnicodeError, ValueError) as exc:
            # 不把进程参数、正文或底层输出放进异常；超时可能已经投递。
            raise AgentDesktopError("CLI_UNAVAILABLE", type(exc).__name__, safe=False) from None

    def _command(self, args, stdin=None):
        answer = self._runner(args, stdin=stdin)
        if not isinstance(answer, dict) or not answer.get("ok"):
            error = answer.get("error", {}) if isinstance(answer, dict) else {}
            disposition = error.get("disposition", {})
            safe = disposition.get("delivery") == "not_delivered" and disposition.get("retry") == "safe"
            raise AgentDesktopError(error.get("code", "CLI_FAILED"), "Desktop command failed", safe=safe)
        return answer.get("data", {})

    def _snapshot_data(self, wait_for=None, *, window_id=None, include_bounds=False):
        args = ["snapshot", "--app", self.layout.app, "--timeout-ms", str(int(self.timeout * 1000)),
                "--max-depth", "50"]
        selected_window = window_id or self.layout.window_id
        if selected_window:
            args.extend(["--window-id", selected_window])
        if include_bounds or self.layout.input_mode == "clipboard_coordinates" \
                or self.layout.activation_mode == "observed_coordinates":
            args.append("--include-bounds")
        if wait_for:
            args.extend(["--wait-for", wait_for, "--wait-timeout", str(int(min(20, self.timeout) * 1000))])
        data = self._command(args)
        if data.get("complete") is not True or data.get("identity_unproven"):
            raise LinuxSenderError("INCOMPLETE_TREE", "Complete accessibility identity is required")
        for node in self._nodes(data.get("tree", {})):
            name = node.get("name")
            if isinstance(name, str) and " ".join(name.split()).casefold() in ("login", "log in", "登录"):
                raise LinuxSenderError("LOGIN_VISIBLE", "Login state cannot accept message input")
        return data

    def _snapshot(self, wait_for=None):
        return self._snapshot_data(wait_for).get("tree", {})

    @staticmethod
    def _nodes(root, blocked_states=("hidden",)):
        if any(isinstance(state, str) and state.lower() in blocked_states
               for state in root.get("states", [])):
            return
        yield root
        for child in root.get("children", []):
            yield from AgentDesktopDriver._nodes(child, blocked_states)

    def _one(self, tree, selector, target, *, actionable=True):
        blocked = ("hidden", "offscreen") if self.layout.input_mode == "clipboard_coordinates" \
            or self.layout.activation_mode == "observed_coordinates" else ("hidden",)
        nodes = [node for node in self._nodes(tree, blocked) if selector.matches(node, target)]
        if len(nodes) != 1:
            raise LinuxSenderError("AMBIGUOUS_TARGET", "Expected exactly one observed UI control")
        if actionable and not nodes[0].get("ref_id"):
            raise LinuxSenderError("UNADDRESSABLE_TARGET", "UI control has no semantic ref")
        return nodes[0]

    def _chat_controls(self, tree, target):
        header_target = self._header_target(target)
        header = self._one(tree, self.layout.header, header_target, actionable=False)
        if header_target.title not in (header.get("name"), header.get("value")):
            raise LinuxSenderError("WRONG_CONVERSATION", "Conversation header was not verified")
        ref_required = self.layout.input_mode != "clipboard_coordinates" \
            and self.layout.activation_mode != "observed_coordinates"
        return (self._one(tree, self.layout.composer, header_target, actionable=ref_required),
                self._one(tree, self.layout.send, target, actionable=ref_required))

    def _header_target(self, target):
        title = (self.layout.header_aliases or {}).get(target.username, target.title)
        return Target(target.username, title)

    def _target_headers(self, tree, target):
        expected = self._header_target(target)
        return [node for node in self._nodes(tree, ("hidden", "offscreen"))
                if self.layout.header.matches(node, expected)
                and expected.title in (node.get("name"), node.get("value"))]

    def _has_unique_target_header(self, tree, target):
        headers = self._target_headers(tree, target)
        if len(headers) > 1:
            raise LinuxSenderError("AMBIGUOUS_TARGET", "Current target conversation header is not unique")
        return bool(headers)

    def _action(self, command, ref, *, stdin=None):
        args = [command, ref]
        if stdin is not None:
            args.append("--stdin")
        if self.allow_physical and command in ("click", "type", "clear", "focus"):
            args.append("--headed")
        return self._command(args, stdin=stdin)

    def _value(self, node, *, force_get=False):
        if not force_get and self.layout.input_mode == "clipboard_coordinates" and isinstance(node.get("value"), str):
            return node["value"]
        ref = node.get("ref_id")
        if not ref:
            raise LinuxSenderError("VALUE_UNREADABLE", "An exact readable control value is required")
        data = self._command(["get", ref, "--property", "value"])
        value = data.get("value")
        if data.get("ref") != ref or data.get("property") != "value" or not isinstance(value, str):
            raise LinuxSenderError("VALUE_UNREADABLE", "An exact readable control value is required")
        return value

    @staticmethod
    def _bounds(value):
        if not isinstance(value, Mapping):
            raise LinuxSenderError("BOUNDS_UNVERIFIED", "Observed bounds are required")
        numbers = tuple(value.get(key) for key in ("x", "y", "width", "height"))
        if any(type(number) not in (int, float) or not math.isfinite(number) for number in numbers) \
                or numbers[2] <= 0 or numbers[3] <= 0 \
                or not math.isfinite(numbers[0] + numbers[2]) or not math.isfinite(numbers[1] + numbers[3]):
            raise LinuxSenderError("BOUNDS_UNVERIFIED", "Observed bounds must be finite and positive")
        return numbers

    def _windows(self):
        windows = self._command(["list-windows", "--app", self.layout.app,
                                 "--timeout-ms", str(int(self.timeout * 1000))])
        if not isinstance(windows, list):
            raise LinuxSenderError("WINDOW_UNVERIFIED", "Complete window inventory is required")
        return windows

    def _main_window(self, windows):
        matches = [window for window in windows if window.get("id") == self.layout.window_id]
        if len(matches) != 1:
            raise LinuxSenderError("WINDOW_UNVERIFIED", "Pinned main window is not unique")
        window = matches[0]
        identity = (window.get("pid"), window.get("process_instance"))
        if window.get("visible") is not True or window.get("minimized") is True \
                or type(identity[0]) is not int or identity[0] <= 0 \
                or not isinstance(identity[1], str) or not identity[1] \
                or str(window.get("app_name", "")).casefold() != self.layout.app.casefold():
            raise LinuxSenderError("WINDOW_UNVERIFIED", "Main window identity is unavailable")
        self._bounds(window.get("bounds"))
        if self._coordinate_process is None:
            self._coordinate_process = identity
        if identity != self._coordinate_process:
            raise LinuxSenderError("WINDOW_CHANGED", "Main window process identity changed")
        return window

    def _owned_popup(self, window, main):
        return window.get("id") != main["id"] and window.get("title") == "" \
            and window.get("visible") is True and window.get("minimized") is not True \
            and (window.get("pid"), window.get("process_instance")) == self._coordinate_process \
            and str(window.get("app_name", "")).casefold() == self.layout.app.casefold()

    def _owned_window(self, windows, main, window_id):
        matches = [window for window in windows if window.get("id") == window_id]
        if len(matches) != 1 or (window_id != main["id"] and not self._owned_popup(matches[0], main)):
            raise LinuxSenderError("WINDOW_UNVERIFIED", "Target window ownership is unverified")
        self._bounds(matches[0].get("bounds"))
        return matches[0]

    def _coordinate_view(self, window_id=None, *, wait_for=None):
        windows = self._windows()
        main = self._main_window(windows)
        if main.get("is_focused") is not True:
            raise LinuxSenderError("FOCUS_UNVERIFIED", "Pinned main window must remain focused")
        selected = window_id or main["id"]
        window = self._owned_window(windows, main, selected)
        data = self._snapshot_data(wait_for, window_id=selected, include_bounds=True)
        if data.get("window", {}).get("id") != selected:
            raise LinuxSenderError("WINDOW_UNVERIFIED", "Snapshot window identity did not match")
        current = self._windows()
        active = self._main_window(current)
        observed = self._owned_window(current, active, selected)
        if active.get("is_focused") is not True or observed.get("bounds") != window.get("bounds"):
            raise LinuxSenderError("WINDOW_CHANGED", "Window focus or bounds changed during observation")
        return data.get("tree", {}), observed

    def _focus_main(self):
        main = self._main_window(self._windows())
        data = self._command(["focus-window", "--app", self.layout.app,
                              "--window-id", main["id"], "--headed"])
        if data.get("focused", {}).get("id") != main["id"]:
            raise LinuxSenderError("FOCUS_UNVERIFIED", "Pinned main window could not be focused")
        return self._coordinate_view()

    def _coordinate_click(self, selector, target, *, window_id=None, before_click=None,
                          validate_tree=None, require_title=False, require_first_line=False):
        if before_click is not None:
            before_click()
        tree, window = self._coordinate_view(window_id)
        node = self._one(tree, selector, target, actionable=False)
        if "disabled" in node.get("states", []):
            raise LinuxSenderError("CONTROL_DISABLED", "Coordinate target is disabled")
        if require_title and target.title not in (node.get("name"), node.get("value")):
            raise LinuxSenderError("WRONG_SEARCH_RESULT", "Exact search identity is required")
        if require_first_line and (not isinstance(node.get("name"), str)
                                   or node["name"].split("\n", 1)[0] != target.title):
            raise LinuxSenderError("WRONG_SEARCH_RESULT", "Exact chat row identity is required")
        x, y, width, height = self._bounds(node.get("bounds"))
        wx, wy, ww, wh = self._bounds(window.get("bounds"))
        if x < wx or y < wy or x + width > wx + ww or y + height > wy + wh:
            raise LinuxSenderError("BOUNDS_UNVERIFIED", "Control bounds extend outside its owned window")
        if validate_tree is not None:
            validate_tree(tree)
        px, py = int(x + width / 2), int(y + height / 2)
        if not (x <= px < x + width and y <= py < y + height):
            raise LinuxSenderError("BOUNDS_UNVERIFIED", "No integer input point lies inside the control")
        point = "%d,%d" % (px, py)
        self._command(["mouse-click", "--xy", point, "--headed"])

    def _scoped_press(self, keys):
        main = self._main_window(self._windows())
        if main.get("is_focused") is not True:
            raise LinuxSenderError("FOCUS_UNVERIFIED", "Pinned main window must remain focused")
        self._command(["press", keys, "--app", self.layout.app,
                       "--window-id", main["id"], "--headed"])

    def _clipboard_matches(self, text):
        data = self._command(["clipboard-get", "--format", "text"])
        if data.get("type") != "text" or data.get("found") is False or data.get("text") != text:
            raise LinuxSenderError("CLIPBOARD_MISMATCH", "Clipboard text could not be verified")

    def _coordinate_field(self, selector, target, *, verify_chat=False):
        tree, _ = self._coordinate_view()
        if verify_chat:
            self._chat_controls(tree, target)
        node = self._one(tree, selector, target, actionable=False)
        if "focused" not in node.get("states", []):
            raise LinuxSenderError("FOCUS_UNVERIFIED", "Exact input control focus was not observed")
        return node

    def _clipboard_fill(self, selector, target, text, *, replace=False):
        self._command(["clipboard-set", "--stdin"], stdin=text)
        self._clipboard_matches(text)
        self._coordinate_click(selector, target)
        field = self._coordinate_field(selector, target, verify_chat=not replace)
        if replace:
            self._scoped_press("ctrl+a")
            field = self._coordinate_field(selector, target)
        elif self._value(field) != "":
            raise LinuxSenderError("DRAFT_PRESENT", "Composer must be verifiably empty before pasting")
        self._clipboard_matches(text)
        self._scoped_press("ctrl+v")
        field = self._coordinate_field(selector, target, verify_chat=not replace)
        if self._value(field) != text:
            raise LinuxSenderError("TEXT_MISMATCH", "Pasted text did not match the request")

    def _fill_composer(self, tree, target, text):
        composer, _ = self._chat_controls(tree, target)
        semantic = self.layout.composer_input_mode == "set_value"
        if self._value(composer, force_get=semantic) != "":
            raise LinuxSenderError("DRAFT_PRESENT", "Composer must be verifiably empty before writing")
        if not semantic:
            self._clipboard_fill(self.layout.composer, target, text)
            return
        self._action("set-value", composer["ref_id"], stdin=text)
        if self.layout.reuse_verified_controls:
            if self._value(composer, force_get=True) != text:
                raise LinuxSenderError("TEXT_MISMATCH", "Composer text did not match the requested text")
            _, send = self._chat_controls(tree, target)
            self._prepared = (target, hashlib.sha256(text.encode()).digest(), composer, send)
            return
        updated, _ = self._coordinate_view()
        composer, _ = self._chat_controls(updated, target)
        if self._value(composer, force_get=True) != text:
            raise LinuxSenderError("TEXT_MISMATCH", "Composer text did not match the requested text")

    def _popup_result(self, target):
        windows = self._windows()
        main = self._main_window(windows)
        matches = []
        for window in windows:
            if not self._owned_popup(window, main):
                continue
            tree, _ = self._coordinate_view(window["id"])
            nodes = [node for node in self._nodes(tree, ("hidden", "offscreen"))
                     if self.layout.search_result.matches(node, target)
                     and target.title in (node.get("name"), node.get("value"))]
            if len(nodes) > 1:
                raise LinuxSenderError("AMBIGUOUS_TARGET", "Popup search result is not unique")
            if nodes:
                matches.append(window["id"])
        if len(matches) != 1:
            raise LinuxSenderError("AMBIGUOUS_TARGET", "Exactly one owned popup result is required")
        return matches[0]

    def _require_closed_popups(self):
        windows = self._windows()
        main = self._main_window(windows)
        if any(window.get("id") != main["id"] and window.get("visible") is True
               and window.get("title") == "" for window in windows):
            raise LinuxSenderError("POPUP_STILL_OPEN", "Owned search popups must close before selecting a chat row")
        if main.get("is_focused") is not True:
            raise LinuxSenderError("FOCUS_UNVERIFIED", "Pinned main window must be focused before selecting a chat row")

    def _selected_popup_open(self, popup_id):
        windows = self._windows()
        main = self._main_window(windows)
        selected = [window for window in windows if window.get("id") == popup_id]
        if len(selected) > 1:
            raise LinuxSenderError("AMBIGUOUS_TARGET", "Selected popup identity is not unique")
        if selected and (selected[0].get("title") != ""
                         or (selected[0].get("pid"), selected[0].get("process_instance")) != self._coordinate_process
                         or str(selected[0].get("app_name", "")).casefold() != self.layout.app.casefold()):
            raise LinuxSenderError("WINDOW_CHANGED", "Selected popup ownership changed")
        for window in windows:
            if window.get("id") == main["id"] or window.get("visible") is not True or window.get("title") != "":
                continue
            if not self._owned_popup(window, main):
                raise LinuxSenderError("WINDOW_UNVERIFIED", "Unowned popup is visible")
            if window.get("id") != popup_id:
                raise LinuxSenderError("AMBIGUOUS_TARGET", "Another popup is visible during result selection")
        return bool(selected and selected[0].get("visible") is True)

    def _wait_popup_closed(self, popup_id):
        if self._selected_popup_open(popup_id):
            wait_ms = max(1, int(min(20, self.timeout) * 1000))
            try:
                self._command(["wait", "--event", "window-closed", "--window-id", popup_id,
                               "--app", self.layout.app, "--timeout", str(wait_ms)])
            except AgentDesktopError:
                # 关闭可能发生在 wait 的基线建立之前，最终只相信当前只读窗口清单。
                if self._selected_popup_open(popup_id):
                    raise LinuxSenderError("POPUP_STILL_OPEN", "Selected popup did not close within the wait budget") from None
            if self._selected_popup_open(popup_id):
                raise LinuxSenderError("POPUP_STILL_OPEN", "Selected popup did not close within the wait budget")
        view = self._focus_main()
        self._require_closed_popups()
        return view

    def _chat_ready_selector(self, target):
        if self.layout.composer.name:
            name = self.layout.composer.name.replace("{title}", self._header_target(target).title)
            name = name.replace("{header_title}", self._header_target(target).title)
            return self.layout.composer.role + ":" + name
        title = self._header_target(target).title
        name = (self.layout.header.name or title).replace("{title}", title).replace("{header_title}", title)
        return self.layout.header.role + ":" + name

    def _prepare_coordinates(self, target, text):
        tree, _ = self._focus_main()
        if self._has_unique_target_header(tree, target):
            self._require_closed_popups()
        elif self.layout.reuse_verified_controls and self.layout.chat_row is not None and any(
                self.layout.chat_row.matches(node, self._header_target(target))
                for node in self._nodes(tree, ("hidden", "offscreen"))):
            # 最近会话身份已由精确 username 和昵称碰撞校验绑定，避免全局搜索混入同名历史结果。
            self._coordinate_click(self.layout.chat_row, self._header_target(target),
                                   before_click=self._require_closed_popups, require_first_line=True)
            tree, _ = self._coordinate_view(wait_for=self._chat_ready_selector(target))
        else:
            self._clipboard_fill(self.layout.search, target, target.title, replace=True)
            popup = self._popup_result(target)
            self._coordinate_click(self.layout.search_result, target, window_id=popup, require_title=True)
            tree, _ = self._wait_popup_closed(popup)
            if not self._has_unique_target_header(tree, target):
                if self.layout.chat_row is not None:
                    self._coordinate_click(self.layout.chat_row, self._header_target(target),
                                           before_click=self._require_closed_popups, require_first_line=True)
                tree, _ = self._coordinate_view(wait_for=self._chat_ready_selector(target))
        self._fill_composer(tree, target, text)

    def _activate(self, node, selector, target, *, before_activate=None, validate_tree=None):
        if self.layout.activation_mode == "observed_coordinates":
            self._coordinate_click(selector, target, before_click=before_activate, validate_tree=validate_tree)
            return
        if self.layout.activation_mode == "focused_return":
            self._action("focus", node["ref_id"])
            if before_activate is not None:
                before_activate()
            tree = self._snapshot()
            focused = self._one(tree, selector, target)
            state = self._command(["is", focused["ref_id"], "--property", "focused"])
            if state.get("property") != "focused" or state.get("result") is not True \
                    or state.get("applicable") is False:
                raise LinuxSenderError("FOCUS_UNVERIFIED", "Activation target focus was not verified")
            if validate_tree is not None:
                validate_tree(tree)
            self._command(["press", "return", "--app", self.layout.app,
                           "--window-id", self.layout.window_id, "--headed"])
            return
        if before_activate is not None:
            before_activate()
            tree = self._snapshot()
            node = self._one(tree, selector, target)
            if validate_tree is not None:
                validate_tree(tree)
        self._action("click", node["ref_id"])

    def _pending_text(self, tree, target, text):
        composer, send = self._chat_controls(tree, target)
        if self._value(composer) != text:
            raise LinuxSenderError("TEXT_MISMATCH", "Composer text changed before submission")
        return send

    def prepare_text(self, target: Target, text: str):
        self._prepared = None
        if self.layout.input_mode == "clipboard_coordinates":
            self._prepare_coordinates(target, text)
            return
        if self.allow_physical and any(len(value.encode("utf-8")) > 10000 for value in (target.title, text)):
            raise ValueError("Physical text input exceeds agent-desktop's 10000-byte limit")
        search = self._one(self._snapshot(), self.layout.search, target)
        if self.allow_physical:
            self._action("clear", search["ref_id"])
            search = self._one(self._snapshot(), self.layout.search, target)
            if self._value(search) != "":
                raise LinuxSenderError("SEARCH_NOT_EMPTY", "Search control was not cleared")
        write = "type" if self.allow_physical else "set-value"
        self._action(write, search["ref_id"], stdin=target.title)
        result = self._one(self._snapshot(self.layout.search_result.role + ":" + target.title),
                           self.layout.search_result, target)
        if target.title not in (result.get("name"), result.get("value")):
            raise LinuxSenderError("WRONG_SEARCH_RESULT", "Search identity was not verified")
        self._activate(result, self.layout.search_result, target)
        composer, _ = self._chat_controls(
            self._snapshot(self.layout.header.role + ":" + self._header_target(target).title), target)
        if self._value(composer) != "":
            raise LinuxSenderError("DRAFT_PRESENT", "Composer must be verifiably empty before writing")
        self._action(write, composer["ref_id"], stdin=text)
        composer, _ = self._chat_controls(self._snapshot(), target)
        if self._value(composer) != text:
            raise LinuxSenderError("TEXT_MISMATCH", "Composer text did not match the requested text")

    def submit_text(self, target: Target, text: str, *, before_submit=None):
        if self.layout.reuse_verified_controls:
            prepared, self._prepared = self._prepared, None
            if prepared is None or prepared[0] != target or prepared[1] != hashlib.sha256(text.encode()).digest():
                raise LinuxSenderError("PREPARATION_MISMATCH", "Submission must match this preparation exactly")
            if self._value(prepared[2], force_get=True) != text:
                raise LinuxSenderError("TEXT_MISMATCH", "Prepared composer text changed")
            # focus 只重新定位控件；慢账号回调后仍以新的完整树校验会话、正文和焦点再提交。
            self._activate(prepared[3], self.layout.send, target, before_activate=before_submit,
                           validate_tree=lambda tree: self._pending_text(tree, target, text))
            # 提交后不再遍历整棵树，唯一送达证据仍是 LinuxSender 的正数 server_id 回读。
            return
        send = self._pending_text(self._snapshot(), target, text)
        self._activate(send, self.layout.send, target, before_activate=before_submit,
                       validate_tree=lambda tree: self._pending_text(tree, target, text))
        try:
            self._chat_controls(self._snapshot(), target)
        except Exception:
            # 提交已执行，后续观察失败也不能标为未投递。
            raise AgentDesktopError("POST_SUBMIT_UNVERIFIED", "Post-submit observation failed", safe=False) from None


class LinuxSender:
    """同步发送入口；结果不明只轮询回读，不二次输入或提交。"""

    def __init__(self, db, driver, *, resolver=None, readback=None,
                 account_guard: Optional[Callable[[str], bool]] = None, own_username=None, verify_timeout=8,
                 verify_interval=0.25, clock=time.monotonic, sleep=time.sleep):
        if verify_timeout < 0 or verify_interval <= 0:
            raise ValueError("Verification timeouts must be nonnegative and interval positive")
        self.db = db
        self.driver = driver
        self.resolver = resolver or ReaderIdentityResolver(db)
        self.readback = readback or ReaderReadback(db)
        self.account_guard = account_guard
        self.own_username = own_username
        self.verify_timeout = verify_timeout
        self.verify_interval = verify_interval
        self.clock = clock
        self.sleep = sleep
        self._lock = threading.Lock()

    def _expected_account(self):
        try:
            expected = self.db.get_self_info().get("username") if self.db is not None else self.own_username
        except Exception:
            raise LinuxSenderError("ACCOUNT_UNVERIFIED", "Reader account identity is unavailable") from None
        if not isinstance(expected, str) or not expected or expected != expected.strip():
            raise LinuxSenderError("ACCOUNT_UNVERIFIED", "An exact reader account identity is required")
        if self.own_username is not None and expected != self.own_username:
            raise LinuxSenderError("ACCOUNT_CHANGED", "Reader account identity changed")
        return expected

    def _check_account(self, expected):
        if self._expected_account() != expected:
            raise LinuxSenderError("ACCOUNT_CHANGED", "Reader account identity changed")
        if not callable(self.account_guard):
            raise LinuxSenderError("GUI_ACCOUNT_UNVERIFIED", "An independent GUI account guard is required")
        try:
            verified = self.account_guard(expected)
        except Exception:
            raise LinuxSenderError("GUI_ACCOUNT_UNVERIFIED", "GUI account identity could not be verified") from None
        if verified is not True:
            raise LinuxSenderError("GUI_ACCOUNT_UNVERIFIED", "GUI account identity did not match the reader")

    def send_text(self, username, text, ats=None, client_id=None) -> dict:
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        if not text or "\x00" in text or len(text.encode("utf-8")) > 128 * 1024:
            raise ValueError("text must contain 1 to 131072 UTF-8 bytes without NUL")
        if ats:
            raise SendNotSupported("Native Linux mentions have not been verified")
        with self._lock:
            expected = self._expected_account()
            target = self.resolver.resolve(username)
            aliases = getattr(getattr(self.driver, "layout", None), "header_aliases", None) or {}
            if username in aliases:
                validate_alias = getattr(self.resolver, "assert_alias_unique", None)
                if not callable(validate_alias):
                    raise LinuxSenderError("ALIAS_UNVERIFIED", "System display alias uniqueness must be verified")
                validate_alias(username, aliases[username])
            mark = self.readback.watermark(username)
            self._check_account(expected)
            self.driver.prepare_text(target, text)
            self._check_account(expected)
            try:
                self.driver.submit_text(target, text, before_submit=lambda: self._check_account(expected))
            except AgentDesktopError as exc:
                if exc.safe:
                    raise
            except LinuxSenderError:
                raise
            except Exception:
                # 未知异常可能发生在投递以后，只做只读确认。
                pass
            until = self.clock() + self.verify_timeout
            while True:
                try:
                    found = self.readback.find_text(username, text, mark)
                except Exception:
                    found = None
                if found:
                    server_id = int(found.get("server_id") or 0)
                    msg_id = str(server_id) if server_id > 0 else \
                        "local:%s:%s:%s" % (username, found.get("shard", ""), found["local_id"])
                    return {"status": "confirmed", "confirmed": True, "msg_id": msg_id,
                            "username": username, "client_id": client_id, **found}
                left = until - self.clock()
                if left <= 0:
                    return {"status": "uncertain", "confirmed": False,
                            "username": username, "client_id": client_id}
                self.sleep(min(left, self.verify_interval))

    def send_image(self, username, path, ats=None, client_id=None) -> dict:
        raise SendNotSupported("Native Linux image preview and submission have not been verified")
