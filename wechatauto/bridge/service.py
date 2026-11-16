"""在现有私人桌面中托管桥接；停止命令只定位本桥接进程，不操作微信。"""

import argparse
import fcntl
import json
import logging
import os
from pathlib import Path
import select
import signal
import tempfile
import time

from aiohttp import web

from .native import _private_file, create_runtime_app, load_config


def _start_time(pid, proc_root=Path('/proc')):
    value = (proc_root / str(pid) / 'stat').read_text()
    return value[value.rfind(')') + 2:].split()[19]


def desktop_environment(executable, *, timeout=60):
    """只从当前 UID 的唯一官方微信主进程继承四个显示会话变量。"""
    deadline = time.monotonic() + timeout
    while True:
        candidates = []
        for entry in Path('/proc').iterdir():
            try:
                if not entry.name.isdigit() or entry.stat().st_uid != os.getuid():
                    continue
                if str((entry / 'exe').readlink()) != executable:
                    continue
                arguments = (entry / 'cmdline').read_bytes().split(b'\0')
                if any(argument.startswith(b'--type=') for argument in arguments):
                    continue
                selected = {}
                for item in (entry / 'environ').read_bytes().split(b'\0'):
                    key, separator, value = item.partition(b'=')
                    if separator and key in (b'DBUS_SESSION_BUS_ADDRESS', b'DISPLAY',
                                             b'XAUTHORITY', b'XDG_RUNTIME_DIR'):
                        selected[key.decode()] = value.decode()
                if len(selected) == 4 and all(selected.values()):
                    candidates.append(selected)
            except (FileNotFoundError, ProcessLookupError):
                continue
        if len(candidates) == 1:
            return {**os.environ, **candidates[0]}
        if len(candidates) > 1 or time.monotonic() >= deadline:
            raise RuntimeError('A unique native desktop session is unavailable')
        time.sleep(0.5)


def _write_pid(path, config_path):
    descriptor, temporary = tempfile.mkstemp(prefix='.native-pid-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump({'pid': os.getpid(), 'start': _start_time(os.getpid()),
                       'config': str(config_path)}, stream)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _wait_exit(descriptor, timeout):
    return bool(select.select([descriptor], [], [], timeout)[0])


def stop_service(config_path, config, *, proc_root=Path('/proc'), open_pidfd=None,
                 send_signal=None, wait_exit=None):
    """核对 UID、启动实例、模块和配置后才终止桥接，PID 复用时拒绝。"""
    path = Path(config['state_dir']) / 'service.pid'
    if not path.exists():
        return
    _private_file(path)
    receipt = json.loads(path.read_text())
    pid = receipt.get('pid')
    if type(pid) is not int or pid <= 1 or receipt.get('config') != str(config_path):
        raise RuntimeError('Invalid native bridge process receipt')
    process = proc_root / str(pid)
    opener = open_pidfd or getattr(os, 'pidfd_open', None)
    sender = send_signal or getattr(signal, 'pidfd_send_signal', None)
    if opener is None or sender is None:
        raise RuntimeError('Stopping requires Linux pidfd support')
    descriptor = None
    try:
        descriptor = opener(pid, 0)
        if process.stat().st_uid != os.getuid() or _start_time(pid, proc_root) != receipt.get('start'):
            raise RuntimeError('Native bridge process identity changed')
        arguments = (process / 'cmdline').read_bytes().split(b'\0')
        if not all(value in arguments for value in
                   (b'wechatauto.bridge.service', b'run', str(config_path).encode())):
            raise RuntimeError('Native bridge command identity changed')
        sender(descriptor, signal.SIGTERM, None, 0)
        if not (wait_exit or _wait_exit)(descriptor, 30):
            raise TimeoutError('Native bridge has not finished shutting down')
    except (FileNotFoundError, ProcessLookupError):
        return
    finally:
        if descriptor is not None:
            os.close(descriptor)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['run', 'stop'])
    parser.add_argument('--config', required=True)
    arguments = parser.parse_args()
    os.umask(0o077)
    config_path = Path(arguments.config).absolute()
    config = load_config(config_path)
    if arguments.action == 'stop':
        stop_service(config_path, config)
        return
    lock = os.open(Path(config['state_dir']) / 'service.lock',
                   os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _write_pid(Path(config['state_dir']) / 'service.pid', config_path)
        environment = desktop_environment(config['executable'])
        logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
        app = create_runtime_app(config, environment=environment)
        web.run_app(app, host=config['listen_host'], port=config['port'],
                    access_log=None, print=None)
    finally:
        os.close(lock)


if __name__ == '__main__':
    main()
