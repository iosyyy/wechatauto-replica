"""官方 Linux 微信的严格原生桥接运行层；只轮询显式允许的新消息。"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
from contextlib import closing
from dataclasses import replace
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import threading

from aiohttp import web

from wechatauto.db import WeChatDB, _verify_enc_key
from wechatauto.platforms.linux import _read_file
from wechatauto.platforms.linux_sender import (
    AgentDesktopDriver, DesktopLayout, LinuxSender, Selector, SendNotSupported,
)

from .journal import Journal
from .history import validate_history_config
from .wechatpad import MAX_IMAGE_BYTES, create_app, encode_native_record


def _private_file(path):
    """逐段拒绝符号链接，再校验常规文件、当前 UID、0600 和单个硬链接。"""
    with _read_file(Path(path)) as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise PermissionError("runtime files must be caller-owned private regular files")


def _exact(value, label, maximum=4096):
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value or len(value) > maximum:
        raise ValueError("invalid " + label)
    return value


def _layout(value):
    if not isinstance(value, dict):
        raise ValueError("desktop_layout must contain an observed layout")
    if set(value) - {"app", "window_id", "search", "search_result", "header", "composer", "send",
                     "activation_mode", "input_mode", "composer_input_mode", "header_aliases", "chat_row",
                     "reuse_verified_controls"}:
        raise ValueError("unknown desktop_layout field")
    selectors = {}
    for name in ("search", "search_result", "header", "composer", "send", "chat_row"):
        item = value.get(name)
        if name == "chat_row" and item is None:
            continue
        if not isinstance(item, dict) or set(item) - {"role", "name", "native_id", "description",
                                                    "required_action", "name_first_line"}:
            raise ValueError("invalid observed desktop selector")
        _exact(item.get("role"), "selector role", 128)
        for key, text in item.items():
            if key != "role" and text is not None:
                _exact(text, "selector identity", 1024)
        selectors[name] = Selector(**item)
    window_id = value.get("window_id")
    if window_id is not None:
        _exact(window_id, "window_id", 256)
    activation = value.get("activation_mode", "semantic")
    input_mode = value.get("input_mode", "ref")
    composer_input_mode = value.get("composer_input_mode", "clipboard")
    reuse = value.get("reuse_verified_controls", False)
    if type(reuse) is not bool or (reuse and (composer_input_mode != "set_value" or activation != "focused_return")):
        raise ValueError("invalid verified-control reuse mode")
    if activation not in ("semantic", "focused_return", "observed_coordinates"):
        raise ValueError("invalid activation_mode")
    if input_mode not in ("ref", "clipboard_coordinates"):
        raise ValueError("invalid input_mode")
    if composer_input_mode not in ("clipboard", "set_value"):
        raise ValueError("invalid composer_input_mode")
    if composer_input_mode == "set_value" and input_mode != "clipboard_coordinates":
        raise ValueError("set_value composer input requires clipboard_coordinates navigation")
    aliases = value.get("header_aliases")
    if aliases is not None and (not isinstance(aliases, dict) or aliases not in ({}, {"filehelper": "File Transfer"})):
        raise ValueError("unverified system header alias")
    return DesktopLayout(app=_exact(value.get("app"), "observed app", 128), window_id=window_id,
                         activation_mode=activation, input_mode=input_mode, composer_input_mode=composer_input_mode,
                         reuse_verified_controls=reuse, header_aliases=aliases, **selectors)


def validate_config(value):
    """归一化配置，物理输入只接受 JSON true，绝不从字符串或数字推断。"""
    if not isinstance(value, dict):
        raise ValueError("config must be an object")
    config = dict(value)
    for name in ("db_dir", "keys_file", "state_dir"):
        path = Path(_exact(config.get(name), name))
        if not path.is_absolute():
            raise ValueError("runtime paths must be absolute")
        config[name] = str(path)
    account = _exact(config.get("account"), "account", 256)
    if account in (".", "..") or not re.fullmatch(r"[A-Za-z0-9_@.-]+", account):
        raise ValueError("invalid account directory")
    _exact(config.get("token"), "token")
    config["native_history"] = validate_history_config(config.get("native_history"), config["token"])
    if not isinstance(config.get("expected_exe_sha256"), str) or not re.fullmatch(
            r"[0-9a-fA-F]{64}", config["expected_exe_sha256"]):
        raise ValueError("expected_exe_sha256 is required")
    config["expected_exe_sha256"] = config["expected_exe_sha256"].lower()
    config.setdefault("executable", "/opt/wechat/wechat")
    if not Path(_exact(config["executable"], "executable")).is_absolute():
        raise ValueError("executable must be absolute")
    config.setdefault("listen_host", "0.0.0.0")
    _exact(config["listen_host"], "listen_host", 256)
    config.setdefault("port", 5080)
    if type(config["port"]) is not int or not 1 <= config["port"] <= 65535:
        raise ValueError("invalid port")
    for name in ("allowed_chats", "write_chats"):
        config.setdefault(name, ["filehelper"])
        values = config[name]
        if not isinstance(values, list) or len(values) > 100 or not values:
            raise ValueError("invalid chat allowlist")
        config[name] = list(dict.fromkeys(_exact(item, "chat username", 256) for item in values))
    config.setdefault("request_prefix", "/lb ")
    prefix = config["request_prefix"]
    if not isinstance(prefix, str) or not prefix or len(prefix) > 128 or "\x00" in prefix:
        raise ValueError("request_prefix must be nonempty")
    config.setdefault("group_request_prefix", None)
    group_prefix = config["group_request_prefix"]
    if group_prefix is not None and (not isinstance(group_prefix, str) or not group_prefix
                                     or len(group_prefix) > 128 or "\x00" in group_prefix):
        raise ValueError("invalid group_request_prefix")
    config.setdefault("allow_physical", False)
    if type(config["allow_physical"]) is not bool:
        raise TypeError("allow_physical must be an explicit boolean")
    config.setdefault("bind_current_window", False)
    config.setdefault("fast_desktop_checks", False)
    if type(config["fast_desktop_checks"]) is not bool:
        raise TypeError("fast_desktop_checks must be an explicit boolean")
    if type(config["bind_current_window"]) is not bool:
        raise TypeError("bind_current_window must be an explicit boolean")
    if config["bind_current_window"] and not config["allow_physical"]:
        raise ValueError("binding the current window requires explicit physical input permission")
    observed = _layout(config.get("desktop_layout"))
    if (observed.activation_mode != "semantic" or observed.input_mode != "ref") and not config["allow_physical"]:
        raise ValueError("the selected desktop modes require explicit physical input permission")
    if config["allow_physical"] and not observed.window_id and not config["bind_current_window"]:
        raise ValueError("physical input requires an explicitly configured observed window ID")
    config.setdefault("desktop_timeout", 20)
    if type(config["desktop_timeout"]) not in (int, float) or not 1 <= config["desktop_timeout"] <= 60:
        raise ValueError("desktop_timeout must be between 1 and 60 seconds")
    config.setdefault("poll_interval", 1.0)
    if type(config["poll_interval"]) not in (int, float) or not 0.1 <= config["poll_interval"] <= 60:
        raise ValueError("invalid poll_interval")
    config.setdefault("delivery_confirm_timeout", 30)
    if type(config["delivery_confirm_timeout"]) not in (int, float) or not 1 <= config["delivery_confirm_timeout"] <= 60:
        raise ValueError("delivery_confirm_timeout must be between 1 and 60 seconds")
    state = Path(config["state_dir"])
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    Journal._check_private(state, directory=True)
    if Path(config["keys_file"]).exists():
        _private_file(config["keys_file"])
    return config


def load_config(path):
    """读取调用方提供的 0600 JSON；缺少密钥文件时允许启动，但保持未就绪。"""
    _private_file(path)
    with _read_file(Path(path)) as stream:
        content = stream.read(128 * 1024 + 1)
    if len(content) > 128 * 1024:
        raise ValueError("config exceeds the size limit")
    try:
        value = json.loads(content)
    except (UnicodeError, ValueError):
        raise ValueError("invalid runtime config JSON") from None
    return validate_config(value)


def _page(limit, offset):
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 1000:
        raise ValueError("history pagination exceeds the allowed bounds")


class NativeProcessGuard:
    """固定进程实例、映像、唯一账号目录、活动 FD、当前 contact 页及可见窗口。

    desktop_probe(args)->JSON、proc_root 和 scan_roots 仅供合成测试注入。
    默认检查当前 HOME 与 /tmp；无法完成任何证据时返回 False，不降级成目录名猜测。
    只在内存中消费 list-windows/snapshot 输出，不把完整树、消息或凭据写入日志。
    """

    def __init__(self, config, db_provider, *, environment=None, proc_root="/proc", scan_roots=None,
                 desktop_probe=None):
        self.config = config
        self.db_provider = db_provider
        self.environment = dict(os.environ if environment is None else environment)
        self.proc_root = Path(proc_root)
        self.scan_roots = [Path(path) for path in (scan_roots if scan_roots is not None else
                                                  [self.environment.get("HOME", str(Path.home())), "/tmp"])]
        self.desktop_probe = desktop_probe or self._desktop
        self.observed = _layout(config["desktop_layout"])
        self.window_id = self.observed.window_id
        self._pinned_process = None
        self._binary = None
        self.verified = False
        self.timeout = config.get("desktop_timeout", 20)

    def _desktop(self, args):
        result = subprocess.run(["agent-desktop", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", timeout=self.timeout * 2 + 2,
                                env=self.environment, check=False)
        if result.returncode or len(result.stdout.encode("utf-8")) > 8 * 1024 * 1024:
            raise RuntimeError("desktop observation unavailable")
        return json.loads(result.stdout)

    def _binary_identity(self):
        executable = Path(self.config["executable"])
        info = executable.stat()
        stamp = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if self._binary is None or self._binary[0] != stamp:
            digest = hashlib.sha256()
            with executable.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != self.config["expected_exe_sha256"]:
                raise RuntimeError("native executable hash changed")
            self._binary = (stamp, digest.hexdigest())
        return self._binary

    def _processes(self):
        processes, mains = [], []
        for entry in self.proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat().st_uid != os.getuid():
                    continue
                executable = str((entry / "exe").readlink())
                if executable != self.config["executable"]:
                    continue
                command = (entry / "cmdline").read_bytes().split(b"\0")
                raw_stat = (entry / "stat").read_text()
            except FileNotFoundError:
                continue
            tail = raw_stat[raw_stat.rfind(")") + 2:].split()
            identity = (int(entry.name), tail[19], executable, self._binary_identity())
            processes.append(entry)
            if not any(argument.startswith(b"--type=") for argument in command):
                mains.append(identity)
        if len(mains) != 1:
            raise RuntimeError("native main process is ambiguous or unavailable")
        if self._pinned_process is not None and self._pinned_process != mains[0]:
            raise RuntimeError("native process instance changed")
        self._pinned_process = mains[0]
        return processes, mains[0][0]

    def _sources(self, selected):
        found = set()
        visited = 0

        def unreadable(error):
            raise RuntimeError("native source discovery is incomplete") from None

        for base in self.scan_roots:
            if not base.is_dir() or not os.access(base, os.W_OK):
                raise RuntimeError("native source discovery root is unavailable")
            for root, directories, _ in os.walk(base, followlinks=False, onerror=unreadable):
                visited += 1
                if visited > 100000:
                    raise RuntimeError("native source discovery exceeded the bound")
                if "db_storage" in directories:
                    source = Path(root) / "db_storage"
                    if source.is_symlink():
                        raise RuntimeError("native source directory is linked")
                    found.add(source.resolve())
                    directories.remove("db_storage")
        if found != {selected}:
            raise RuntimeError("native account source is ambiguous")

    @staticmethod
    def _active_sources(processes, selected):
        witnessed = False
        for process in processes:
            for descriptor in (process / "fd").iterdir():
                try:
                    target = str(descriptor.readlink())
                except FileNotFoundError:
                    continue
                if "db_storage" not in Path(target).parts:
                    continue
                if target.endswith(" (deleted)") or not Path(target).is_absolute() or not Path(target).resolve().is_relative_to(selected):
                    raise RuntimeError("active native database belongs to another source")
                witnessed = True
        if not witnessed:
            raise RuntimeError("active native database identity is unavailable")

    def _window(self, pid):
        deadline = str(int(self.timeout * 1000))
        envelope = self.desktop_probe(["list-windows", "--app", self.observed.app, "--timeout-ms", deadline])
        windows = envelope.get("data") if envelope.get("ok") is True else None
        if not isinstance(windows, list):
            raise RuntimeError("native window inventory unavailable")
        matching = [item for item in windows if item.get("app_name", "").lower() == self.observed.app.lower()
                    and item.get("title") == "Weixin"]
        if len(matching) != 1:
            raise RuntimeError("native window is ambiguous")
        window = matching[0]
        if (window.get("accessible") is not True or window.get("visible") is not True
                or window.get("minimized") is not False or window.get("pid") != pid):
            raise RuntimeError("native window identity is unverified")
        window_id = _exact(window.get("id"), "native window", 256)
        if self.window_id is not None and self.window_id != window_id:
            raise RuntimeError("native window changed")
        return window_id

    def _gui(self, pid):
        window_id = self._window(pid)
        deadline = str(int(self.timeout * 1000))
        observed = self.desktop_probe(["snapshot", "--app", self.observed.app, "--window-id", window_id,
                                       "--max-depth", "50", "--timeout-ms", deadline])
        data = observed.get("data", {})
        if observed.get("ok") is not True or data.get("complete") is not True or data.get("identity_unproven"):
            raise RuntimeError("native UI state is unavailable")
        tree = data.get("tree")
        if not isinstance(tree, dict):
            raise RuntimeError("native UI tree is unavailable")
        for node in AgentDesktopDriver._nodes(tree):
            label = node.get("name")
            if isinstance(label, str) and " ".join(label.split()).casefold() in ("login", "log in", "登录"):
                raise RuntimeError("native login is visible")
        return window_id

    def _verify(self, expected, *, include_gui):
        self.verified = False
        try:
            db = self.db_provider()
            if (db is None or db.account != self.config["account"] or expected != db.wxid
                    or Path(db.db_dir).resolve() != Path(self.config["db_dir"]).resolve()):
                return False
            selected = db._linux_reader.storage.resolve()
            self._sources(selected)
            processes, pid = self._processes()
            self._active_sources(processes, selected)
            contacts = [(rel, path) for rel, path, _ in db._db_files if Path(path).name == "contact.db"]
            if len(contacts) != 1:
                return False
            rel, path = contacts[0]
            if not _verify_enc_key(db._keys[rel], db._linux_reader.page1(Path(path))):
                return False
            window_id = self._gui(pid) if include_gui else self._window(pid)
            processes, _ = self._processes()
            self._sources(selected)
            self._active_sources(processes, selected)
            if not _verify_enc_key(db._keys[rel], db._linux_reader.page1(Path(path))):
                return False
            self.window_id = window_id
            self.verified = True
            return True
        except Exception:
            return False

    def __call__(self, expected):
        return self._verify(expected, include_gui=True)

    def verify_source(self, expected):
        """实时校验源、进程、FD、HMAC、窗口；写入界面由发送器的完整树另行验证。"""
        return self._verify(expected, include_gui=False)


class NativeBackend:
    """同一 RLock 串行化读取、轮询、GUI 发送及发送回执登记。

    config 是 load_config 返回的映射；journal 由调用方创建和关闭。
    account_guard/driver_factory/db_factory 仅为离线测试注入点，CLI 不提供绕过开关。
    profile、is_logged_in、send_text、send_image、chatroom_members 实现网关的同步后端接口。
    poll_once 每个允许会话/分片最多处理 100 行；首次或新分片只建立基线，不导入历史。
    get_history/list_groups 是有界内部接口，可供独立授权的历史 API 调用。
    """

    def __init__(self, config, journal, *, account_guard=None, driver_factory=None,
                 db_factory=WeChatDB, environment=None):
        self.config = validate_config(config)
        self.journal = journal
        identity = hashlib.sha256((str(Path(self.config["db_dir"]).resolve()) + "\0" + self.config["account"]).encode()).hexdigest()
        journal.bind_identity(identity)
        self.lock = threading.RLock()
        self.db = None
        self.sender = None
        self._closed = False
        self._db_factory = db_factory
        self._driver_factory = driver_factory or AgentDesktopDriver
        self.environment = dict(os.environ if environment is None else environment)
        self.account_guard = account_guard or NativeProcessGuard(self.config, lambda: self.db,
                                                                environment=self.environment)

    def ensure_loaded(self):
        """密钥存在且全部通过读取器 HMAC 校验后才暴露 DB；失败不伪造账号。"""
        with self.lock:
            if self._closed:
                raise RuntimeError("native backend is closed")
            try:
                _private_file(self.config["keys_file"])
                if self.db is None:
                    candidate = self._db_factory(db_dir=self.config["db_dir"], account=self.config["account"],
                                                 keys_file=self.config["keys_file"],
                                                 workdir=str(Path(self.config["state_dir"]) / "reader"))
                    self._self_info(candidate)
                    self.db = candidate
                return self.db
            except Exception:
                raise RuntimeError("verified native database is unavailable") from None

    def _self_info(self, db):
        if db.account != self.config["account"] or Path(db.db_dir).resolve() != Path(self.config["db_dir"]).resolve():
            raise RuntimeError("native account binding changed")
        info = db.get_self_info()
        username = info.get("username")
        if not username or username != db.wxid:
            raise RuntimeError("native self identity is unavailable")
        connection = db._contact_conn()
        if connection is None:
            raise RuntimeError("native self contact is unavailable")
        with closing(connection) as conn:
            rows = conn.execute("SELECT username,nick_name FROM contact WHERE username=? LIMIT 2", (username,)).fetchall()
        if len(rows) != 1 or rows[0]["username"] != username:
            raise RuntimeError("native self contact is ambiguous or missing")
        return {"wxid": username, "nickname": rows[0]["nick_name"] or ""}

    def _ready(self, *, source_only=False):
        db = self.ensure_loaded()
        profile = self._self_info(db)
        probe = self.account_guard
        if source_only and self.config["fast_desktop_checks"] and isinstance(probe, NativeProcessGuard):
            probe = probe.verify_source
        if probe(profile["wxid"]) is not True:
            raise RuntimeError("native GUI account is unverified")
        return db, profile

    def profile(self):
        with self.lock:
            return self._ready()[1]

    def is_logged_in(self):
        with self.lock:
            try:
                self._ready()
                return True
            except Exception:
                return False

    def _get_sender(self):
        if self.sender is not None:
            # 缓存仅省去额外 GUI 探测，发送器仍在准备和提交前独立校验账号。
            self._self_info(self.ensure_loaded())
            return self.sender
        db, profile = self._ready(source_only=self.config["fast_desktop_checks"])
        if self.sender is None:
            observed = _layout(self.config["desktop_layout"])
            if observed.window_id is None and self.config["allow_physical"]:
                if (not self.config["bind_current_window"] or not isinstance(self.account_guard, NativeProcessGuard)
                        or self.account_guard.verified is not True or not self.account_guard.window_id):
                    raise RuntimeError("physical input requires a currently verified native window")
            if isinstance(self.account_guard, NativeProcessGuard) and self.account_guard.verified is True:
                observed = replace(observed, window_id=self.account_guard.window_id)
            driver = self._driver_factory(observed, env=self.environment,
                                          allow_physical=self.config["allow_physical"],
                                          timeout=self.config["desktop_timeout"])
            guard = self.account_guard
            if self.config["fast_desktop_checks"] and isinstance(guard, NativeProcessGuard):
                if not isinstance(driver, AgentDesktopDriver):
                    raise RuntimeError("Source-only guard requires the verified native desktop driver")
                guard = guard.verify_source
            self.sender = LinuxSender(db, driver, account_guard=guard, own_username=profile["wxid"],
                                      verify_timeout=self.config["delivery_confirm_timeout"])
        return self.sender

    def _write_allowed(self, chat):
        if chat not in self.config["write_chats"]:
            raise PermissionError("target is not in write_chats")

    def _receipt(self, chat, receipt):
        if receipt.get("confirmed") is True:
            self.journal.suppress_sent(chat, receipt["shard"], int(receipt["local_id"]))
        return receipt

    def send_text(self, to, text, ats, client_id):
        self._write_allowed(to)
        with self.lock:
            return self._receipt(to, self._get_sender().send_text(to, text, ats, client_id))

    def send_image(self, to, image, ats, client_id):
        self._write_allowed(to)
        if not isinstance(image, str) or len(image) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
            raise ValueError("image exceeds the size limit")
        try:
            raw = base64.b64decode(image, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("invalid image base64") from None
        if not raw or len(raw) > MAX_IMAGE_BYTES or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            raise SendNotSupported("Only bounded PNG input can enter the native sender")
        with self.lock:
            sender = self._get_sender()
            descriptor, temporary = tempfile.mkstemp(prefix=".send-", suffix=".png", dir=self.config["state_dir"])
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(raw)
                return self._receipt(to, sender.send_image(to, temporary, ats, client_id))
            finally:
                os.unlink(temporary)

    def chatroom_members(self, room):
        if room not in self.config["allowed_chats"] or not room.endswith("@chatroom"):
            raise PermissionError("room is not in allowed_chats")
        with self.lock:
            db, _ = self._ready()
            return [{"user_name": row["username"], "nick_name": row["remark"] or row["nick_name"] or row["username"]}
                    for row in db.get_group_members(room)]

    def _inbound(self, chat, shard, row, own):
        if self.journal.is_suppressed(chat, shard, int(row["local_id"])):
            return None
        sender = row["sender_username"]
        content = row["content"]
        if chat == "filehelper":
            prefix = self.config["request_prefix"]
            if sender != own or row["msg_type"] != 1 or not content.startswith(prefix):
                return None
            content = content[len(prefix):]
            if not content.strip():
                return None
        elif not sender or sender == own:
            return None
        elif chat.endswith("@chatroom") and self.config["group_request_prefix"] is not None:
            # 安静保存普通群记录，仅显式提问进入模型；历史工具仍直接读取所有本地记录。
            body = content[len(sender) + 2:] if content.startswith(sender + ':\n') else content
            if row["msg_type"] != 1 or not body.startswith(self.config["group_request_prefix"]):
                return None
        return encode_native_record({**row, "username": chat, "content": content, "shard": shard}, own)

    def poll_once(self):
        """只处理显式允许的新行；事件与水位同事务，任何失败均保留可重读位置。"""
        admitted = 0
        with self.lock:
            db, profile = self._ready(source_only=True)
            for chat in self.config["allowed_chats"]:
                table = "Msg_" + hashlib.md5(chat.encode()).hexdigest()
                for shard in db._message_dbs():
                    with closing(db._open(shard)) as conn:
                        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                        cursor = self.journal.get_cursor(chat, shard)
                        if cursor is None:
                            maximum = int(conn.execute("SELECT MAX(local_id) FROM " + table).fetchone()[0] or 0) if exists else 0
                            self.journal.record_observation(chat, shard, maximum)
                            continue
                        if not exists:
                            continue
                        rows = conn.execute(
                            "SELECT local_id,local_type,real_sender_id,create_time,message_content,server_id,sort_seq "
                            "FROM " + table + " WHERE local_id>? ORDER BY local_id LIMIT 100", (cursor,)).fetchall()
                        for raw in rows:
                            sender = conn.execute("SELECT user_name FROM Name2Id WHERE rowid=?", (raw["real_sender_id"],)).fetchone()
                            content = raw["message_content"]
                            if isinstance(content, bytes):
                                content = db._friendly_content(content, db._msg_type_name(raw["local_type"]))
                            row = {"local_id": int(raw["local_id"]), "msg_type": int(raw["local_type"]) & 0xFFFF,
                                   "sender_username": sender[0] if sender else "", "content": content or "",
                                   "create_time": int(raw["create_time"]), "server_id": raw["server_id"],
                                   "sort_seq": raw["sort_seq"]}
                            payload = self._inbound(chat, shard, row, profile["wxid"])
                            self.journal.record_observation(chat, shard, row["local_id"], payload)
                            admitted += payload is not None
        return admitted

    def get_history(self, group_username, limit=50, offset=0):
        """只读本地已同步记录；跨群工具另有独立所有者身份和群白名单。"""
        _page(limit, offset)
        groups = set(self.config["allowed_chats"]) | set((self.config["native_history"] or {}).get("allowed_groups", []))
        if group_username not in groups or not group_username.endswith("@chatroom"):
            raise PermissionError("group is not in allowed_chats")
        with self.lock:
            return self._ready(source_only=True)[0].get_messages(group_username, limit=limit, offset=offset)

    def list_groups(self, limit=50, offset=0, allowed_groups=None):
        """直接在 contact SQL 分页，只返回群 username/name，不展开成员或消息。"""
        _page(limit, offset)
        with self.lock:
            if allowed_groups is not None and (not isinstance(allowed_groups, list) or len(allowed_groups) > 100
                    or any(not isinstance(x, str) or not x.endswith('@chatroom') for x in allowed_groups)):
                raise ValueError("invalid history groups")
            if allowed_groups == []:
                return []
            db, _ = self._ready(source_only=True)
            with closing(db._contact_conn()) as conn:
                predicate = "username LIKE '%@chatroom'"
                values = []
                if allowed_groups is not None:
                    predicate += ' AND username IN (' + ','.join('?' for _ in allowed_groups) + ')'
                    values.extend(allowed_groups)
                rows = conn.execute("SELECT username,nick_name FROM contact WHERE " + predicate +
                                    " ORDER BY username LIMIT ? OFFSET ?", (*values, limit, offset)).fetchall()
            return [{"username": row["username"], "name": row["nick_name"] or row["username"]} for row in rows]

    def close(self):
        """停止新操作；调用方须在轮询任务退出后再关闭 journal。"""
        with self.lock:
            self._closed = True
            self.sender = self.db = None


BACKEND_KEY = web.AppKey("native_backend", NativeBackend)
JOURNAL_KEY = web.AppKey("native_journal", Journal)


def create_runtime_app(config, *, backend=None, journal=None, environment=None):
    """创建网关、后台轮询和关闭钩子；默认启用 NativeProcessGuard。

    返回 aiohttp Application，BACKEND_KEY/JOURNAL_KEY 可用于同进程运维查询。
    注入的 backend 和 journal 也由应用在 cleanup 时关闭，调用方不得先行关闭它们。
    """
    config = validate_config(config)
    if journal is None:
        journal = backend.journal if backend is not None else Journal(Path(config["state_dir"]) / "journal")
    if backend is None:
        try:
            backend = NativeBackend(config, journal, environment=environment)
        except Exception:
            journal.close()
            raise
    if backend.journal is not journal:
        raise ValueError("runtime backend and gateway must share one journal")
    app = create_app(backend, journal, config["token"], history_config=config["native_history"])
    app[BACKEND_KEY] = backend
    app[JOURNAL_KEY] = journal

    async def lifetime(application):
        stopped = asyncio.Event()

        async def poll():
            available = None
            while not stopped.is_set():
                try:
                    await asyncio.to_thread(backend.poll_once)
                    if available is not True:
                        logging.getLogger(__name__).info("Native bridge reader ready")
                    available = True
                except Exception:
                    if available is not False:
                        logging.getLogger(__name__).warning("Native bridge is waiting for a verified account")
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
    """以私有配置启动监听器；密钥或 GUI 尚未验证时只保持未就绪。"""
    parser = argparse.ArgumentParser(description="Native Linux WeChat WeChatPad-compatible bridge")
    parser.add_argument("--config", required=True, help="Caller-owned 0600 runtime JSON file")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except Exception:
        parser.error("runtime config validation failed")
    app = create_runtime_app(config)
    try:
        web.run_app(app, host=config["listen_host"], port=config["port"], access_log=None, print=None)
    finally:
        app[BACKEND_KEY].close()
        app[JOURNAL_KEY].close()


if __name__ == "__main__":
    main()
