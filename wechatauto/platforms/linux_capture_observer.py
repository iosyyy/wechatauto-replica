"""仅在 GDB 内运行的 attach 观察器；候选经继承 socket 传输。

不写目标内存/通用寄存器，不创建软件断点；硬件调试寄存器由 GDB 管理。
GDB 的内部 solib/thread 断点也受 may-write-memory off 限制，不能保证无中断。
签名边界与参数校验参考 chatlog-keeper；见 LICENSE.linux-capture。
"""

import json
import os
from pathlib import Path
import socket
import struct
import time

import gdb


def _receive(channel, size):
    result = bytearray()
    while len(result) < size:
        data = channel.recv(size - len(result))
        if not data:
            raise RuntimeError('channel_closed')
        result.extend(data)
    return bytes(result)


def _identity(config):
    pid = config['pid']
    raw = Path('/proc/%d/stat' % pid).read_text()
    start = int(raw[raw.rfind(')') + 2:].split()[19])
    info = os.stat('/proc/%d/exe' % pid)
    if (start, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
            config['start_time'], config['executable_dev'], config['executable_inode'],
            config['executable_size'], config['executable_mtime_ns']):
        raise RuntimeError('process_identity_changed')


def run(channel_fd):
    channel = socket.socket(fileno=channel_fd)
    bp = None
    attached = False
    failed = False
    try:
        channel.settimeout(10)
        length = struct.unpack('!I', _receive(channel, 4))[0]
        if not 0 < length <= 16384:
            raise RuntimeError('invalid_config')
        config = json.loads(_receive(channel, length))
        channel.settimeout(config['timeout'])
        deadline = time.monotonic() + config['timeout']
        # 官方文档：权限开关在底层限制写入；保留硬件断点与中断权限以便清理。
        # https://sourceware.org/gdb/current/onlinedocs/gdb.html/Observer-Mode.html
        for command in ('set pagination off', 'set confirm off', 'set print thread-events off',
                        'set auto-load off', 'set debuginfod enabled off', 'set auto-solib-add off',
                        'set may-write-memory off', 'set may-write-registers off',
                        'set may-insert-breakpoints on', 'set may-insert-tracepoints off',
                        'set may-insert-fast-tracepoints off', 'set detach-on-fork on',
                        'set follow-fork-mode parent'):
            gdb.execute(command, to_string=True)
        if gdb.parameter('may-write-memory') or gdb.parameter('may-write-registers'):
            raise RuntimeError('write_restrictions_unavailable')
        _identity(config)
        gdb.execute('attach %d' % config['pid'], to_string=True)
        attached = True
        inferior = gdb.selected_inferior()
        if inferior.pid != config['pid']:
            raise RuntimeError('attached_identity_mismatch')
        _identity(config)
        signature = bytes.fromhex(config['signature'])
        if bytes(inferior.read_memory(config['signature_address'], len(signature))) != signature:
            raise RuntimeError('loaded_signature_changed')
        hmac_signature = bytes.fromhex(config['hmac_signature'])
        if bytes(inferior.read_memory(config['hmac_address'], len(hmac_signature))) != hmac_signature:
            raise RuntimeError('loaded_companion_signature_changed')
        state = {'hits': 0, 'failed': False}
        seen = set()

        class KDFBreakpoint(gdb.Breakpoint):
            def stop(self):
                state['hits'] += 1
                try:
                    if time.monotonic() >= deadline or state['hits'] > 4096:
                        state['failed'] = True
                        return True
                    _identity(config)
                    if bytes(inferior.read_memory(config['signature_address'], len(signature))) != signature:
                        raise RuntimeError('loaded_signature_changed')
                    if bytes(inferior.read_memory(config['hmac_address'], len(hmac_signature))) != hmac_signature:
                        raise RuntimeError('loaded_companion_signature_changed')
                    frame = gdb.selected_frame()
                    reg = lambda name: int(frame.read_register(name))
                    sp = reg('rsp')
                    iterations = struct.unpack('<I', bytes(inferior.read_memory(sp, 4)))[0]
                    outputlen = struct.unpack('<I', bytes(inferior.read_memory(sp + 8, 4)))[0]
                    if (reg('ecx'), reg('r9d'), reg('esi'), iterations, outputlen) != (32, 16, 2, 256000, 32):
                        return False
                    candidate = bytes(inferior.read_memory(reg('rdx'), 32))
                    if candidate in seen:
                        return False
                    if len(seen) >= config['max_candidates']:
                        state['failed'] = True
                        return True
                    seen.add(candidate)
                    channel.sendall(b'C' + candidate)
                    candidate = None
                    answer = _receive(channel, 1)
                    if answer not in (b'Y', b'N'):
                        raise RuntimeError('invalid_reply')
                    return answer == b'Y'
                except BaseException:
                    state['failed'] = True
                    return True

        bp = KDFBreakpoint('*%#x' % config['call_address'], type=gdb.BP_HARDWARE_BREAKPOINT, internal=True)
        bp.silent = True
        if bp.type != gdb.BP_HARDWARE_BREAKPOINT:
            raise RuntimeError('hardware_breakpoint_required')
        channel.sendall(b'R')
        gdb.execute('continue', to_string=True)
        failed = state['failed']
    except (KeyboardInterrupt, Exception):
        failed = True
    finally:
        clean = True
        try:
            if bp is not None and bp.is_valid():
                bp.delete()
        except BaseException:
            clean = False
        try:
            # attach 部分失败也可能已经停止目标，因此始终检查当前 inferior。
            if attached or gdb.selected_inferior().pid:
                gdb.execute('detach', to_string=True)
        except BaseException:
            clean = False
        try:
            if failed:
                channel.sendall(b'E')
            if clean:
                channel.sendall(b'D')
        except OSError:
            pass
        channel.close()
