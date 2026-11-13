"""Linux 读取路径的真实加密库与安全边界回归测试。"""

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from linux_fixtures import CHAT, MASTER, DatabaseFixture
from wechatauto.db import Listener, STAMP_VERSION, WeChatDB, auto_detect_db_dir, list_accounts


@unittest.skipIf(os.name == "nt", "仅验证 POSIX 数据库后端")
class LinuxReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.fx = DatabaseFixture(self.base)

    def tearDown(self):
        self.tmp.cleanup()

    def reader(self, **kwargs):
        return WeChatDB(**self.fx.parameters(**kwargs))

    def test_real_group_queries_shard_sender_mapping_and_source_unchanged(self):
        self.fx.add_message(1, "wxid_bob", 2, "second")
        self.fx.write_keys()
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
                  for p in self.fx.storage.rglob("*") if p.is_file()}
        with patch.object(WeChatDB, "extract_keys", side_effect=AssertionError("Windows extraction")), \
             patch.object(WeChatDB, "_find_weixin_pids", side_effect=AssertionError("Windows process")):
            db = self.reader()
            self.assertEqual(db.search_contact("Alice")[0]["username"], "wxid_alice")
            self.assertEqual(db.group_name_to_id("Fixture Group"), CHAT)
            self.assertEqual(db.get_groups()[0]["member_count"], 2)
            self.assertTrue(db.get_group_members(CHAT)[0]["is_owner"])
            messages = db.get_new_messages(CHAT)
            self.assertEqual([m["sender_username"] for m in messages], ["wxid_alice", "wxid_bob"])
            self.assertEqual(db.sender_username(messages[1]["shard"], 1), "wxid_bob")
            for message in messages:
                self.assertEqual(db.get_message_row(CHAT, message["local_id"], shard=message["shard"])
                                 ["sender_username"], message["sender_username"])
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
                                  for p in before})

    def test_explicit_salt_keys(self):
        fx = DatabaseFixture(self.base / "explicit", explicit_salt=True)
        self.assertEqual(WeChatDB(**fx.parameters()).get_messages(CHAT)[0]["content"], "hello")

    def test_master_key_derived_per_file(self):
        fx = DatabaseFixture(self.base / "master", master=True)
        self.assertEqual(WeChatDB(**fx.parameters(keys_file=None, master_key=MASTER.hex()))
                         .get_messages(CHAT)[0]["content"], "hello")
        with self.assertRaisesRegex(RuntimeError, "密钥"):
            WeChatDB(**fx.parameters(keys_file=None, master_key="00" * 32))
        db = WeChatDB(**fx.parameters(keys_file=None, master_key=MASTER.hex()))
        fx.add_message(1, "wxid_bob", 2, "master new shard")
        self.assertEqual(db.get_messages(CHAT)[0]["content"], "master new shard")

    def test_verified_private_cache_can_be_reused_without_external_file(self):
        self.reader()
        self.assertEqual(self.reader(keys_file=None).get_messages(CHAT)[0]["content"], "hello")

    def test_wrong_missing_and_malformed_key_material_fail_without_windows_fallback(self):
        for data in ({}, {k: "00" * 32 for k in self.fx.keys}, {"../outside.db": "ab" * 32},
                     {"/outside.db": "ab" * 32}, {"C:\\outside.db": "ab" * 32},
                     {k: "not-a-key" for k in self.fx.keys}, []):
            with self.subTest(data=data):
                self.fx.keys_file.write_text(json.dumps(data))
                with patch.object(WeChatDB, "extract_keys", side_effect=AssertionError("Windows")):
                    with self.assertRaisesRegex((RuntimeError, ValueError), "密钥|keys_file"):
                        self.reader()
        with self.assertRaisesRegex(ValueError, "主密钥"):
            self.reader(keys_file=None, master_key="00")

    def test_explicit_keys_take_precedence_and_are_never_modified(self):
        before = self.fx.keys_file.read_bytes()
        mode = self.fx.keys_file.stat().st_mode
        db = self.reader()
        self.assertEqual(before, self.fx.keys_file.read_bytes())
        self.assertEqual(mode, self.fx.keys_file.stat().st_mode)
        self.assertEqual(db.get_messages(CHAT)[0]["content"], "hello")
        self.fx.keys_file.write_text(json.dumps({k: "00" * 32 for k in self.fx.keys}))
        wrong = self.fx.keys_file.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "密钥"):
            self.reader()
        self.assertEqual(self.fx.keys_file.read_bytes(), wrong)
        self.assertFalse(Path(str(self.fx.keys_file) + ".bak").exists())

    def test_explicit_keys_inside_private_workdir_are_rejected_without_modification(self):
        db = self.reader()
        cache = Path(db.workdir)
        database = db._linux_reader.cache_name("message/message_0.db")
        supplied = self.fx.keys_file.read_bytes()
        for name in ("listener_watermark.json", database, database + ".stamp"):
            with self.subTest(name=name):
                alias = cache / name
                alias.write_bytes(supplied)
                alias.chmod(0o600)
                try:
                    with self.assertRaisesRegex((RuntimeError, ValueError), "keys_file|密钥"):
                        self.reader(keys_file=str(alias))
                finally:
                    self.assertEqual(alias.read_bytes(), supplied)
                    alias.unlink()

    def test_multiple_accounts_require_selection_and_never_switch(self):
        other = DatabaseFixture(self.base, account="wxid_other_efgh")
        with self.assertRaisesRegex(RuntimeError, "多个|account"):
            self.reader(account=None)
        self.assertEqual(self.reader().account, self.fx.account)
        with self.assertRaisesRegex(RuntimeError, "密钥"):
            self.reader(keys_file=str(other.keys_file))
        self.assertEqual({a["account"] for a in list_accounts(str(self.fx.root))},
                         {self.fx.account, other.account})

    def test_account_traversal_and_absolute_paths_rejected(self):
        for account in ("../other", "/tmp", "a/b", "a\\b", ".", ".."):
            with self.subTest(account=account):
                with self.assertRaisesRegex((RuntimeError, ValueError), "账号|account"):
                    self.reader(account=account)

    def test_source_and_key_symlink_escapes_rejected(self):
        outside = self.base / "outside.db"
        outside.write_bytes(b"outside")
        (self.fx.storage / "message" / "escape.db").symlink_to(outside)
        with self.assertRaisesRegex((RuntimeError, ValueError), "符号链接|symlink"):
            self.reader()
        (self.fx.storage / "message" / "escape.db").unlink()
        link = self.base / "key-link.json"
        link.symlink_to(self.fx.keys_file)
        with self.assertRaisesRegex((RuntimeError, ValueError, OSError), "符号链接|symlink|密钥"):
            self.reader(keys_file=str(link))

    def test_account_directory_storage_and_wal_symlinks_rejected(self):
        account_link = self.fx.root / "linked-account"
        account_link.symlink_to(self.fx.storage.parent, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "符号链接|symlink"):
            self.reader(account=account_link.name)
        wal = Path(str(self.fx.storage / "message/message_0.db") + "-wal")
        wal.symlink_to(self.fx.keys_file)
        with self.assertRaisesRegex(RuntimeError, "符号链接|symlink"):
            self.reader().get_messages(CHAT)
        wal.unlink()
        message = self.fx.storage / "message"
        moved = self.base / "moved-message"
        message.rename(moved)
        message.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "符号链接|symlink"):
            self.reader()

    def test_workdir_parent_and_cache_file_symlinks_cannot_overwrite_external_files(self):
        outside = self.base / "outside"
        outside.mkdir()
        linked = self.base / "linked-work"
        linked.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "符号链接|symlink"):
            self.reader(workdir=str(linked))
        self.assertEqual(list(outside.iterdir()), [])
        db = self.reader()
        victim = outside / "victim"
        victim.write_text("preserve")
        target = Path(db.workdir) / db._linux_reader.cache_name("message/message_0.db")
        target.symlink_to(victim)
        with self.assertRaisesRegex(RuntimeError, "符号链接|symlink"):
            db.get_messages(CHAT)
        self.assertEqual(victim.read_text(), "preserve")

    def test_keys_file_parent_symlink_rejected(self):
        directory = self.base / "keys-parent-link"
        directory.symlink_to(self.base, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "符号链接|symlink"):
            self.reader(keys_file=str(directory / self.fx.keys_file.name))

    def test_fifo_keys_and_database_files_are_rejected_without_blocking(self):
        script = """
import json
import sys
from wechatauto.db import WeChatDB
try:
    WeChatDB(**json.loads(sys.argv[1]))
except (RuntimeError, ValueError, OSError):
    pass
else:
    raise AssertionError('FIFO must be rejected')
"""
        keys_fifo = self.base / "fifo-keys.json"
        os.mkfifo(keys_fifo, 0o600)
        cases = (("keys_file", keys_fifo),
                 ("database", self.fx.storage / "message/fifo.db"))
        for kind, fifo in cases:
            with self.subTest(kind=kind):
                if kind == "database":
                    os.mkfifo(fifo, 0o600)
                parameters = self.fx.parameters(**({"keys_file": str(fifo)} if kind == "keys_file" else {}))
                try:
                    result = subprocess.run(
                        [sys.executable, "-c", script, json.dumps(parameters)],
                        cwd=Path(__file__).resolve().parents[1], capture_output=True,
                        text=True, timeout=2,
                    )
                except subprocess.TimeoutExpired:
                    self.fail("%s FIFO open blocked instead of rejecting the file" % kind)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_missing_keys_file_and_explicit_data_root_fail_clearly(self):
        with self.assertRaisesRegex(RuntimeError, "keys_file|密钥"):
            self.reader(keys_file=str(self.base / "missing-keys.json"))
        with self.assertRaisesRegex(RuntimeError, "db_dir|目录"):
            self.reader(db_dir=str(self.base / "wrong-root"))
        with self.assertRaisesRegex(RuntimeError, "密钥"):
            self.reader(keys_file=None)

    def test_existing_cache_without_owner_marker_is_not_overwritten(self):
        db = self.reader()
        workdir = Path(db.workdir)
        (workdir / ".owner.json").unlink()
        victim = workdir / "unrelated.txt"
        victim.write_text("preserve")
        with self.assertRaisesRegex(RuntimeError, "无关|覆盖"):
            self.reader()
        self.assertEqual(victim.read_text(), "preserve")

    def test_real_wal_merge_ignores_stale_generation_and_preserves_sources(self):
        db = self.reader()
        self.assertEqual(db.get_messages(CHAT)[0]["content"], "hello")
        wal = self.fx.append_wal(0, "from WAL")
        before = {p: p.read_bytes() for p in self.fx.storage.rglob("*") if p.is_file()}
        self.assertEqual(db.get_messages(CHAT)[0]["content"], "from WAL")
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertEqual(wal.read_bytes(), before[wal])

    def test_cache_uri_special_characters_read_real_database_and_wal_without_stray_files(self):
        for index, separator in enumerate(("?", "#")):
            with self.subTest(separator=separator):
                fx = DatabaseFixture(self.base / ("valid-source-%d" % index))
                prefix = "valid-cache-%d" % index
                workdir = self.base / (prefix + separator + "cache")
                db = WeChatDB(**fx.parameters(workdir=str(workdir)))
                self.assertEqual(db.get_messages(CHAT)[0]["content"], "hello")
                fx.append_wal(0, "escaped WAL")
                self.assertEqual(db.get_messages(CHAT)[0]["content"], "escaped WAL")
                self.assertFalse((self.base / prefix).exists())

    def test_cache_uri_special_characters_reject_corrupt_database_without_stray_files(self):
        for index, separator in enumerate(("", "?", "#")):
            with self.subTest(separator=separator):
                fx = DatabaseFixture(self.base / ("corrupt-source-%d" % index))
                encrypted = fx.storage / "message/message_0.db"
                damaged = bytearray(encrypted.read_bytes())
                damaged[4096:8192] = b"\xff" * 4096
                encrypted.write_bytes(damaged)
                prefix = "corrupt-cache-%d" % index
                workdir = self.base / (prefix + separator + "cache")
                db = WeChatDB(**fx.parameters(workdir=str(workdir)))
                with self.assertRaisesRegex(RuntimeError, "快照校验失败"):
                    connection = db._open("message/message_0.db")
                    connection.close()
                target = Path(db.workdir) / db._linux_reader.cache_name("message/message_0.db")
                self.assertFalse(target.exists())
                if separator:
                    self.assertFalse((self.base / prefix).exists())

    def test_cache_uri_validation_upgrade_rebuilds_previously_accepted_corrupt_cache(self):
        db = self.reader(workdir=str(self.base / "upgrade?cache"))
        rel = "message/message_0.db"
        target = Path(db.workdir) / db._linux_reader.cache_name(rel)
        plain = self.base / (self.fx.account + "-message_message_0.db.plain")
        damaged = bytearray(plain.read_bytes())
        damaged[4096:8192] = b"\xff" * 4096
        target.write_bytes(damaged)
        target.chmod(0o600)
        source = self.fx.storage / rel
        info = source.stat()
        stamp = Path(str(target) + ".stamp")
        stamp.write_text(json.dumps([STAMP_VERSION, info.st_mtime_ns, info.st_size, 0, 0]))
        stamp.chmod(0o600)
        connection = db._open(rel)
        try:
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchall()[0][0], "ok")
        finally:
            connection.close()
        self.assertFalse((self.base / "upgrade").exists())

    def test_secure_cache_modes_and_account_root_isolation(self):
        with patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.base / "cache")}):
            db = self.reader(workdir=None)
            db.get_messages(CHAT)
            self.reader(workdir=None)
        self.assertEqual(stat.S_IMODE(Path(db.workdir).stat().st_mode), 0o700)
        for path in Path(db.workdir).rglob("*"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600, str(path))
        fx = DatabaseFixture(self.base / "same-account")
        db2 = WeChatDB(**fx.parameters(workdir=str(self.base / "work")))
        self.assertNotEqual(db.workdir, db2.workdir)
        self.assertNotEqual(self.reader().workdir, db2.workdir)

    def test_new_shard_reloads_explicit_keys_and_reports_unkeyed(self):
        db = self.reader()
        self.fx.add_message(1, "wxid_bob", 2, "new shard")
        with patch.object(WeChatDB, "extract_keys", side_effect=AssertionError("Windows")):
            with self.assertRaisesRegex(RuntimeError, "密钥"):
                db.get_messages(CHAT)
            self.assertIn("message/message_1.db", db.unkeyed)
            self.fx.write_keys()
            self.assertEqual(db.get_messages(CHAT)[0]["content"], "new shard")
            self.assertEqual(db.unkeyed, [])
            backup = Path(db.workdir) / "keys.json.bak"
            self.assertTrue(backup.is_file())
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)

    def test_listener_reads_actual_database_and_restarts_from_watermark(self):
        db = self.reader()
        got, done = [], threading.Event()
        def callback(message, listener):
            got.append(message["content"])
            done.set()
        listener = Listener(db, interval=0.01, watermark={CHAT: 0}, persist_interval=0)
        listener.add_listener(CHAT, callback)
        listener.start()
        try:
            self.assertTrue(done.wait(3))
        finally:
            listener.stop()
        self.assertEqual(got, ["hello"])
        self.assertEqual(stat.S_IMODE(Path(listener.watermark_file).stat().st_mode), 0o600)
        self.reader()
        restarted = Listener(db, interval=0.01, watermark={CHAT: 0}, persist_interval=0)
        restarted.add_listener(CHAT, callback)
        self.assertEqual(restarted.watermark[CHAT], 1)
        restarted._poll_once()
        restarted.stop()
        self.assertEqual(got, ["hello"])

    def test_listener_rejects_external_or_other_cache_watermark_files(self):
        db = self.reader()
        outside = self.base / "keep.json"
        outside.write_text("preserve")
        for path in (outside, Path(db.workdir) / "keys.json"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "watermark|水位"):
                    Listener(db, watermark_file=str(path))
        self.assertEqual(outside.read_text(), "preserve")

    def test_legacy_new_without_constructor_retains_default_backend(self):
        db = object.__new__(WeChatDB)
        self.assertIsNone(db._linux_reader)


@unittest.skipIf(os.name == "nt", "仅验证 Linux 目录发现")
class LinuxDiscoveryTests(unittest.TestCase):
    def test_xdg_documents_parsed_as_data_and_override_does_not_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td).resolve()
            fx = DatabaseFixture(home / "my documents")
            config = home / ".config"
            config.mkdir()
            (config / "user-dirs.dirs").write_text('XDG_DOCUMENTS_DIR="$HOME/my documents"\n')
            env = {"HOME": str(home), "XDG_CONFIG_HOME": str(config), "WECHATAUTO_DATA_DIR": ""}
            with patch.dict(os.environ, env), patch("sys.platform", "linux"):
                self.assertEqual(auto_detect_db_dir(), str(fx.root))
                with patch.dict(os.environ, {"WECHATAUTO_DATA_DIR": str(home / "missing")}):
                    with self.assertRaisesRegex(RuntimeError, "目录|WECHATAUTO_DATA_DIR"):
                        auto_detect_db_dir()
                (config / "user-dirs.dirs").write_text('XDG_DOCUMENTS_DIR="$(touch pwned)"\n')
                self.assertIsNone(auto_detect_db_dir())
                self.assertFalse((home / "pwned").exists())


if __name__ == "__main__":
    unittest.main()
