"""托管停止边界：陈旧 PID、错误进程和不私密的凭据不能收到信号。"""

import json
import os
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import Mock, patch

from wechatauto.bridge import service
from wechatauto.bridge.service import stop_service


class NativeServiceTest(unittest.TestCase):
    def setUp(self):
        previous_umask = os.umask(0o077)
        os.umask(previous_umask)
        self.addCleanup(os.umask, previous_umask)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)
        self.proc = self.root / 'proc'
        self.process = self.proc / '123'
        self.process.mkdir(parents=True)
        self.config_path = self.root / 'config.json'
        self.config = {'state_dir': str(self.state)}
        self.receipt = {'pid': 123, 'start': '900', 'config': str(self.config_path)}
        self.command = [b'/usr/bin/python3', b'-m', b'wechatauto.bridge.service',
                        b'run', b'--config', str(self.config_path).encode()]
        self.write_process()
        self.write_receipt()
        self.kill = Mock()
        self.wait_exit = Mock(return_value=True)
        self.pidfd = None

    def write_process(self, start='900'):
        tail = ['0'] * 20
        tail[19] = start
        (self.process / 'stat').write_text('123 (bridge worker) ' + ' '.join(tail))
        (self.process / 'cmdline').write_bytes(b'\0'.join(self.command) + b'\0')

    def write_receipt(self):
        path = self.state / 'service.pid'
        path.write_text(json.dumps(self.receipt))
        path.chmod(0o600)

    def stop(self):
        self.pidfd = os.open(self.root, os.O_RDONLY)
        try:
            stop_service(self.config_path, self.config, proc_root=self.proc,
                         open_pidfd=lambda pid, flags: self.pidfd, send_signal=self.kill,
                         wait_exit=self.wait_exit)
        finally:
            try:
                os.close(self.pidfd)
            except OSError:
                pass

    def test_verified_bridge_receives_only_term(self):
        self.stop()
        self.kill.assert_called_once_with(self.pidfd, signal.SIGTERM, None, 0)
        self.wait_exit.assert_called_once_with(self.pidfd, 30)
        with self.assertRaises(OSError):
            os.fstat(self.pidfd)

    def test_reuse_after_validation_signals_bound_pidfd_only(self):
        original_read = Path.read_bytes

        def reuse_after_read(path):
            result = original_read(path)
            if path == self.process / 'cmdline':
                self.command = [b'/opt/wechat/wechat']
                self.write_process(start='901')
            return result

        with patch.object(Path, 'read_bytes', reuse_after_read):
            self.stop()
        self.kill.assert_called_once_with(self.pidfd, signal.SIGTERM, None, 0)
        self.assertNotEqual(self.pidfd, 123)

    def test_reused_pid_is_never_signalled(self):
        self.write_process(start='901')
        with self.assertRaises(RuntimeError):
            self.stop()
        self.kill.assert_not_called()

    def test_shutdown_wait_timeout_has_no_numeric_kill_fallback(self):
        self.wait_exit.return_value = False
        with self.assertRaises(TimeoutError):
            self.stop()
        self.kill.assert_called_once_with(self.pidfd, signal.SIGTERM, None, 0)

    def test_current_receipt_exists_while_waiting_for_desktop(self):
        config = {**self.config, 'executable': '/opt/wechat/wechat'}

        def waiting(executable):
            receipt = json.loads((self.state / 'service.pid').read_text())
            self.assertEqual(receipt['pid'], os.getpid())
            self.assertEqual(receipt['start'], '900')
            self.assertEqual(receipt['config'], str(self.config_path))
            raise InterruptedError('desktop not available yet')

        with patch('sys.argv', ['bridge', 'run', '--config', str(self.config_path)]), \
                patch.object(service, 'load_config', return_value=config), \
                patch.object(service, '_start_time', return_value='900'), \
                patch.object(service, 'desktop_environment', side_effect=waiting):
            with self.assertRaises(InterruptedError):
                service.main()

    def test_wechat_instead_of_bridge_is_never_signalled(self):
        self.command = [b'/opt/wechat/wechat']
        self.write_process()
        with self.assertRaises(RuntimeError):
            self.stop()
        self.kill.assert_not_called()

    def test_other_config_or_stop_command_is_never_signalled(self):
        for replacement in (b'stop', b'/other/config.json'):
            with self.subTest(replacement=replacement):
                self.command = [b'python3', b'-m', b'wechatauto.bridge.service',
                                b'run', b'--config', str(self.config_path).encode()]
                self.command[3 if replacement == b'stop' else 5] = replacement
                self.write_process()
                with self.assertRaises(RuntimeError):
                    self.stop()
        self.kill.assert_not_called()

    def test_missing_process_needs_no_signal(self):
        (self.process / 'stat').unlink()
        self.stop()
        self.kill.assert_not_called()

    def test_public_receipt_is_rejected(self):
        (self.state / 'service.pid').chmod(0o644)
        with self.assertRaises(PermissionError):
            self.stop()
        self.kill.assert_not_called()

    def test_linked_receipt_is_rejected(self):
        path = self.state / 'service.pid'
        destination = self.state / 'other.json'
        path.rename(destination)
        path.symlink_to(destination)
        with self.assertRaises((PermissionError, OSError, RuntimeError)):
            self.stop()
        self.kill.assert_not_called()
