"""Linux KDF 捕获的离线签名、账号绑定和密钥持久化测试。"""

import hashlib
import runpy
import socket
import types
import threading
import json
import os
import platform
from pathlib import Path
import stat
import struct
import tempfile
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

from linux_fixtures import DatabaseFixture, MASTER, encrypt_pages
from wechatauto.platforms import linux_capture as capture


def elf_image(passwords=1, hmacs=1):
    image = bytearray(8192)
    image[:7] = b'\x7fELF\x02\x01\x01'
    struct.pack_into('<HHIQQQIHHHHHH', image, 16, 3, 62, 1, 0, 64, 0, 0, 64, 56, 2, 0, 0, 0)
    struct.pack_into('<IIQQQQQQ', image, 64, 1, 4, 0, 0, 0, 4096, 4096, 4096)
    struct.pack_into('<IIQQQQQQ', image, 120, 1, 5, 4096, 4096, 0, 4096, 4096, 4096)
    for i in range(passwords):
        at = 4200 + i * 300
        image[at:at + len(capture.PASSWORD_KDF)] = capture.PASSWORD_KDF
    for i in range(hmacs):
        at = 5000 + i * 300
        image[at:at + len(capture.HMAC_KDF)] = capture.HMAC_KDF
    return bytes(image)


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_unique_elf_signature_is_before_indirect_call_and_hash_pinned(self):
        raw = elf_image()
        boundary = capture.locate_kdf_boundary(raw, hashlib.sha256(raw).hexdigest())
        self.assertEqual(boundary.signature_address, 4200)
        self.assertEqual(boundary.virtual_address, 4200 + len(capture.PASSWORD_KDF) - 3)
        with self.assertRaises(capture.CaptureError):
            capture.locate_kdf_boundary(raw, '0' * 64)

    def test_malformed_non_pie_and_ambiguous_elf_fail_closed(self):
        cases = [b'', b'\x7fELF', elf_image(0), elf_image(2), elf_image(hmacs=0), elf_image(hmacs=2)]
        for at, fmt, value in ((16, '<H', 2), (18, '<H', 183), (32, '<Q', 99999),
                               (54, '<H', 55), (56, '<H', 65535), (152, '<Q', 999999)):
            raw = bytearray(elf_image())
            struct.pack_into(fmt, raw, at, value)
            cases.append(bytes(raw))
        for raw in cases:
            with self.subTest(length=len(raw)), self.assertRaises(capture.CaptureError):
                capture.locate_kdf_boundary(raw, hashlib.sha256(raw).hexdigest())

    def test_real_encrypted_pages_derive_per_file_and_store_only_verified_raw_keys(self):
        fx = DatabaseFixture(self.base, master=True)
        bound = capture.BoundDatabases(fx.storage)
        self.addCleanup(bound.close)
        self.assertEqual(bound.verify_candidate(b'\xff' * 32), {})
        verified = bound.verify_candidate(MASTER)
        self.assertEqual({p: k.hex() for p, k in verified.items()}, fx.keys)
        output = self.base / 'private' / 'keys.json'
        bound.persist_verified(verified, output)
        self.assertEqual(json.loads(output.read_text()), fx.keys)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(output.parent.stat().st_mode), 0o700)
        self.assertNotIn(MASTER.hex(), output.read_text())

    def test_changed_pages_and_account_inode_fail_before_persist(self):
        fx = DatabaseFixture(self.base, master=True)
        bound = capture.BoundDatabases(fx.storage)
        self.addCleanup(bound.close)
        keys = bound.verify_candidate(MASTER)
        path = fx.storage / 'message/message_0.db'
        old = path.read_bytes()
        path.write_bytes(old[:80] + bytes([old[80] ^ 1]) + old[81:])
        output = self.base / 'private' / 'keys.json'
        with self.assertRaises(capture.CaptureError):
            bound.persist_verified(keys, output)
        self.assertFalse(output.exists())
        moved = fx.storage.with_name('old_storage')
        fx.storage.rename(moved)
        fx.storage.mkdir()
        with self.assertRaises(capture.CaptureError):
            bound.verify_candidate(MASTER)

    def test_replaced_database_inode_is_rejected_even_with_identical_verified_bytes(self):
        fx = DatabaseFixture(self.base, master=True)
        bound = capture.BoundDatabases(fx.storage)
        self.addCleanup(bound.close)
        keys = bound.verify_candidate(MASTER)
        source = fx.storage / 'message/message_0.db'
        replacement = self.base / 'replacement.db'
        replacement.write_bytes(source.read_bytes())
        original_inode = source.stat().st_ino
        os.replace(replacement, source)
        self.assertNotEqual(source.stat().st_ino, original_inode)
        output = self.base / 'private' / 'keys.json'
        with self.assertRaisesRegex(capture.CaptureError, 'database_identity_changed'):
            bound.verify_candidate(MASTER)
        with self.assertRaisesRegex(capture.CaptureError, 'database_identity_changed'):
            bound.persist_verified(keys, output)
        self.assertFalse(output.exists())

    def test_replaced_subdirectory_with_another_valid_encrypted_database_is_rejected(self):
        fx = DatabaseFixture(self.base / 'selected', master=True)
        other = DatabaseFixture(self.base / 'other', account='wxid_other', master=True)
        bound = capture.BoundDatabases(fx.storage)
        self.addCleanup(bound.close)
        keys = bound.verify_candidate(MASTER)
        (fx.storage / 'message').rename(self.base / 'original-message')
        (fx.storage / 'message').mkdir()
        shutil.copyfile(other.storage / 'message/message_0.db', fx.storage / 'message/message_0.db')
        output = self.base / 'private' / 'keys.json'
        with self.assertRaisesRegex(capture.CaptureError, 'database_identity_changed'):
            bound.verify_candidate(MASTER)
        with self.assertRaisesRegex(capture.CaptureError, 'database_identity_changed'):
            bound.persist_verified(keys, output)
        self.assertFalse(output.exists())

    def _replace_message_directory_during_read(self, fx, read_occurrence=1):
        source = fx.storage / 'message/message_0.db'
        identity = (source.stat().st_dev, source.stat().st_ino)
        original_read = os.read
        reads = 0
        def swap_after_read(fd, length):
            nonlocal reads
            page = original_read(fd, length)
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) == identity:
                reads += 1
            if (info.st_dev, info.st_ino) == identity and reads == read_occurrence:
                moved = self.base / 'detached-message'
                source.parent.rename(moved)
                source.parent.mkdir()
                shutil.copyfile(moved / source.name, source)
            return page
        return swap_after_read

    def test_intermediate_directory_replaced_during_read_is_rejected(self):
        fx = DatabaseFixture(self.base, master=True)
        bound = capture.BoundDatabases(fx.storage)
        self.addCleanup(bound.close)
        with patch.object(capture.os, 'read', side_effect=self._replace_message_directory_during_read(fx)):
            with self.assertRaisesRegex(capture.CaptureError, 'database_identity_changed'):
                bound.page1('message/message_0.db')

    def test_intermediate_directory_replaced_during_persist_cannot_write_keys(self):
        fx = DatabaseFixture(self.base, master=True)
        bound = capture.BoundDatabases(fx.storage)
        self.addCleanup(bound.close)
        keys = bound.verify_candidate(MASTER)
        output = self.base / 'private' / 'keys.json'
        with patch.object(capture.os, 'read', side_effect=self._replace_message_directory_during_read(fx, read_occurrence=3)):
            with self.assertRaisesRegex(capture.CaptureError, 'database_identity_changed'):
                bound.persist_verified(keys, output)
        self.assertFalse(output.exists())

    def test_rewritten_salt_on_original_inode_is_rejected(self):
        fx = DatabaseFixture(self.base / 'selected', master=True)
        other = DatabaseFixture(self.base / 'other', account='wxid_other', master=True)
        bound = capture.BoundDatabases(fx.storage)
        self.addCleanup(bound.close)
        source = fx.storage / 'message/message_0.db'
        original_inode = source.stat().st_ino
        source.write_bytes((other.storage / 'message/message_0.db').read_bytes())
        self.assertEqual(source.stat().st_ino, original_inode)
        with self.assertRaisesRegex(capture.CaptureError, 'database_identity_changed'):
            bound.verify_candidate(MASTER)

    def test_in_place_encrypted_update_with_original_inode_and_salt_is_allowed(self):
        fx = DatabaseFixture(self.base, master=True)
        bound = capture.BoundDatabases(fx.storage)
        self.addCleanup(bound.close)
        source = fx.storage / 'message/message_0.db'
        original = source.stat()
        salt = source.read_bytes()[:16]
        fx.append_wal(0, 'normal in-place update')
        plain = fx.base / (fx.account + '-message_message_0.db.plain')
        source.write_bytes(encrypt_pages(plain.read_bytes(), bytes.fromhex(fx.keys['message/message_0.db']), salt))
        self.assertEqual(source.stat().st_ino, original.st_ino)
        self.assertNotEqual(source.stat().st_mtime_ns, original.st_mtime_ns)
        self.assertEqual(source.read_bytes()[:16], salt)
        keys = bound.verify_candidate(MASTER)
        output = self.base / 'private' / 'keys.json'
        bound.persist_verified(keys, output)
        self.assertEqual(json.loads(output.read_text()), fx.keys)

    def test_account_replacement_during_page_read_does_not_cross_binding(self):
        fx = DatabaseFixture(self.base, master=True)
        bound = capture.BoundDatabases(fx.storage)
        self.addCleanup(bound.close)
        original = bound.check_identity
        def swap_after_check():
            original()
            fx.storage.rename(fx.storage.with_name('original'))
            (fx.storage / 'message').mkdir(parents=True)
            (fx.storage / 'message/message_0.db').write_bytes(b'X' * 4096)
            bound.check_identity = original
        bound.check_identity = swap_after_check
        with self.assertRaises(capture.CaptureError):
            bound.page1('message/message_0.db')

    def test_source_symlinks_and_unverified_or_insecure_outputs_rejected(self):
        fx = DatabaseFixture(self.base, master=True)
        alias = self.base / 'storage-link'
        alias.symlink_to(fx.storage, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            capture.BoundDatabases(alias)
        bound = capture.BoundDatabases(fx.storage)
        self.addCleanup(bound.close)
        output = self.base / 'private' / 'keys.json'
        with self.assertRaises(capture.CaptureError):
            bound.persist_verified({'message/message_0.db': b'\0' * 32}, output)
        keys = bound.verify_candidate(MASTER)
        output.parent.mkdir(mode=0o755)
        with self.assertRaises(RuntimeError):
            bound.persist_verified(keys, output)
        output.parent.chmod(0o700)
        victim = self.base / 'victim'
        victim.write_text('preserved')
        output.symlink_to(victim)
        with self.assertRaises(RuntimeError):
            bound.persist_verified(keys, output)
        self.assertEqual(victim.read_text(), 'preserved')

    def test_maps_require_unique_executable_inode_base_and_executable_signature_mapping(self):
        raw = elf_image()
        boundary = capture.locate_kdf_boundary(raw, hashlib.sha256(raw).hexdigest())
        maps = '700000-701000 r--p 00000000 08:01 99 /opt/wechat/wechat\n701000-702000 r-xp 00001000 08:01 99 /opt/wechat/wechat\n'
        self.assertEqual(capture.loaded_addresses(maps, 8, 1, 99, boundary), (0x701068, 0x701068 + len(capture.PASSWORD_KDF) - 3))
        for invalid in (maps + maps, maps.replace('r-xp', 'rw-p'), maps.replace('99', '100')):
            with self.assertRaises(capture.CaptureError):
                capture.loaded_addresses(invalid, 8, 1, 99, boundary)


@unittest.skipUnless(sys.platform == 'linux' and platform.machine() in ('x86_64', 'AMD64') and shutil.which('gdb') and shutil.which('cc'),
                     '需要 Linux x86_64、GDB 和 C 编译器；不改变主机 ptrace 策略')
class NativeObserverIntegrationTests(unittest.TestCase):
    def test_real_hardware_observer_success_and_timeout_preserve_existing_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            fixture = Path(__file__).parent / 'fixtures/linux_kdf_fixture.c'
            executable = base / 'fixture'
            subprocess.run(['cc', '-O0', '-fPIE', '-pie', '-o', str(executable), str(fixture)], check=True)
            fx = DatabaseFixture(base / 'db', master=True)
            trigger = base / 'trigger'
            child = subprocess.Popen([str(executable), str(trigger)], stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), 'ready')
                expected = hashlib.sha256(executable.read_bytes()).hexdigest()
                output = base / 'private' / 'keys.json'
                capture.capture_existing(child.pid, fx.storage, output, timeout=10,
                    expected_sha256=expected, on_ready=trigger.touch)
                self.assertIsNone(child.poll())
                self.assertEqual(json.loads(output.read_text()), fx.keys)
                self.assertEqual(Path('/proc/%d/status' % child.pid).read_text().split('TracerPid:')[1].split()[0], '0')
                with self.assertRaisesRegex(capture.CaptureError, 'timeout'):
                    capture.capture_existing(child.pid, fx.storage, base / 'private' / 'timeout.json',
                                             timeout=1, expected_sha256=expected)
                self.assertIsNone(child.poll())
                self.assertEqual(Path('/proc/%d/status' % child.pid).read_text().split('TracerPid:')[1].split()[0], '0')
                self.assertFalse((base / 'private' / 'timeout.json').exists())
            finally:
                child.terminate()
                child.wait(timeout=5)
                child.stdout.close()


class ObserverTests(unittest.TestCase):
    def exercise_observer(self, reply=b'Y', invalid_arguments=False, cleanup_error=False):
        commands, breakpoints = [], []
        inferior = types.SimpleNamespace(pid=0)
        reads = {'candidate': 0}
        def read_memory(address, length):
            if address == 1000:
                return capture.PASSWORD_KDF
            if address == 2000:
                return capture.HMAC_KDF
            if address == 3000:
                return struct.pack('<I', 256000)
            if address == 3008:
                return struct.pack('<I', 32)
            if address == 4000:
                reads['candidate'] += 1
                return bytes([reads['candidate']]) * 32
            raise AssertionError('unexpected memory read')
        inferior.read_memory = read_memory
        class Breakpoint:
            def __init__(self, address, type, internal):
                self.type = type
                self.valid = True
                breakpoints.append(self)
            def is_valid(self):
                return self.valid
            def delete(self):
                commands.append('delete hardware breakpoint')
                self.valid = False
        registers = {'rsp': 3000, 'ecx': 31 if invalid_arguments else 32,
                     'r9d': 16, 'esi': 2, 'rdx': 4000}
        def execute(command, **kwargs):
            commands.append(command)
            if command.startswith('attach'):
                inferior.pid = 123
            elif command == 'continue':
                for _ in range(2):
                    if breakpoints[0].stop():
                        break
            elif command == 'detach':
                if cleanup_error:
                    raise RuntimeError('synthetic detach failure')
                inferior.pid = 0
            return ''
        fake = types.SimpleNamespace(Breakpoint=Breakpoint, BP_HARDWARE_BREAKPOINT=7,
                    execute=execute, parameter=lambda name: False,
                    selected_inferior=lambda: inferior,
                    selected_frame=lambda: types.SimpleNamespace(read_register=registers.__getitem__))
        server, client = socket.socketpair()
        server.settimeout(2)
        self.addCleanup(server.close)
        config = {'pid': 123, 'timeout': 1, 'max_candidates': 1,
                  'signature_address': 1000, 'call_address': 1037,
                  'signature': capture.PASSWORD_KDF.hex(), 'hmac_address': 2000,
                  'hmac_signature': capture.HMAC_KDF.hex()}
        payload = json.dumps(config).encode()
        server.sendall(struct.pack('!I', len(payload)) + payload)
        with patch.dict('sys.modules', {'gdb': fake}):
            namespace = runpy.run_path(str(Path(capture.__file__).with_name('linux_capture_observer.py')))
        namespace['run'].__globals__['_identity'] = lambda config: commands.append('check identity')
        worker = threading.Thread(target=namespace['run'], args=(client.detach(),))
        worker.start()
        self.assertEqual(server.recv(1), b'R')
        events = bytearray()
        while True:
            event = server.recv(1)
            if not event:
                break
            events.extend(event)
            if event == b'C':
                candidate = capture._receive(server, 32, __import__('time').monotonic() + 2)
                self.assertEqual(len(candidate), 32)
                server.sendall(reply)
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertLess(commands.index('check identity'), commands.index('attach 123'))
        self.assertLess(commands.index('delete hardware breakpoint'), commands.index('detach'))
        self.assertFalse(any(command == 'kill' or command.startswith('set $') for command in commands))
        return bytes(events)

    def test_observer_success_and_candidate_cap_delete_breakpoint_before_detach(self):
        self.assertEqual(self.exercise_observer(), b'CD')
        self.assertEqual(self.exercise_observer(reply=b'N'), b'CED')

    def test_observer_rejects_wrong_runtime_arguments_without_reading_candidate(self):
        self.assertEqual(self.exercise_observer(invalid_arguments=True), b'D')

    def test_observer_does_not_claim_detach_when_cleanup_failed(self):
        self.assertEqual(self.exercise_observer(cleanup_error=True), b'C')

    def test_gdb_attaches_with_write_restrictions_and_detaches_on_attach_error(self):
        commands = []
        fake = types.SimpleNamespace()
        fake.selected_inferior = lambda: types.SimpleNamespace(pid=123)
        def execute(command, **kwargs):
            commands.append(command)
            if command == 'attach 123':
                raise RuntimeError('synthetic attach failure')
            return ''
        fake.execute = execute
        fake.parameter = lambda name: False
        server, client = socket.socketpair()
        self.addCleanup(server.close)
        self.addCleanup(client.close)
        config = {'pid': 123, 'timeout': 1}
        payload = json.dumps(config).encode()
        server.sendall(struct.pack('!I', len(payload)) + payload)
        observer = Path(capture.__file__).with_name('linux_capture_observer.py')
        with patch.dict('sys.modules', {'gdb': fake}):
            namespace = runpy.run_path(str(observer))
            namespace['run'].__globals__['_identity'] = lambda config: None
            namespace['run'](client.detach())
        events = server.recv(1024)
        self.assertEqual(events, b'ED')
        self.assertLess(commands.index('set may-write-memory off'), commands.index('attach 123'))
        self.assertLess(commands.index('set may-write-registers off'), commands.index('attach 123'))
        self.assertIn('detach', commands)
        self.assertNotIn('kill', commands)


if __name__ == '__main__':
    unittest.main()
