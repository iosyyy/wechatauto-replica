"""Linux 已运行客户端的受限 KDF 观察器；只持久化页一验证过的裸密钥。

签名字节和 ELF 定位逻辑改编自 chatlog-keeper，见 LICENSE.linux-capture。
该模块不启动、重启或终止微信，不修改 ptrace/Yama/seccomp 设置。
"""

from dataclasses import dataclass
import hashlib
import json
import mmap
import os
from pathlib import Path
import signal
import socket
import stat
import struct
import subprocess
import sys
import time

from .linux import _absolute, _directory_fd, _private_dir, _relative_db

SUPPORTED_SHA256 = '2749535f299ba67feb8ef80cab898bb0531fa8f376b16f8a72db706811c84132'
PASSWORD_KDF = bytes.fromhex('498b4678498bbe80000000458b5e04418b76348b4b044d8b46484883ec08ff730841524153ff5030')
HMAC_KDF = bytes.fromhex('498b4678498bbe80000000418b7634458b5608418b4e104d8b4650488b53084883ec08ff7310514152ff5030')


class CaptureError(RuntimeError):
    """捕获的绑定、验证或清理条件不满足。"""


@dataclass(frozen=True)
class KDFBoundary:
    virtual_address: int
    signature_address: int
    image_sha256: str
    hmac_signature_address: int


def locate_kdf_boundary(image, expected_sha256=SUPPORTED_SHA256):
    """仅接受固定哈希的 ELF64 AMD64 PIE 及唯一的一对本地 KDF 签名。"""
    digest = hashlib.sha256(image).hexdigest()
    if digest != expected_sha256:
        raise CaptureError('executable_hash_mismatch')
    try:
        if len(image) < 64 or image[:7] != b'\x7fELF\x02\x01\x01':
            raise ValueError()
        kind, machine, version = struct.unpack_from('<HHI', image, 16)
        phoff = struct.unpack_from('<Q', image, 32)[0]
        ehsize, phsize, count = struct.unpack_from('<HHH', image, 52)
        if (kind, machine, version, ehsize, phsize) != (3, 62, 1, 64, 56):
            raise ValueError()
        if not 0 < count <= 128 or phoff < 64 or phoff + phsize * count > len(image):
            raise ValueError()
        passwords, hmacs, matches, loads = [], [], [], []
        for i in range(count):
            typ, flags, off, va, _, size, memsize, alignment = struct.unpack_from('<IIQQQQQQ', image, phoff + i * phsize)
            if typ != 1:
                continue
            if (size > memsize or off + size > len(image) or va + memsize > 1 << 63
                    or (alignment > 1 and (alignment & (alignment - 1) or off % alignment != va % alignment))):
                raise ValueError()
            if any(va < end and start < va + memsize for start, end in loads):
                raise ValueError()
            loads.append((va, va + memsize))
            if off == 0 and va == 0:
                matches.append('zero')
            if not flags & 1:
                continue
            segment = image[off:off + size]
            for signature, found in ((PASSWORD_KDF, passwords), (HMAC_KDF, hmacs)):
                start = 0
                while True:
                    pos = segment.find(signature, start)
                    if pos < 0:
                        break
                    found.append((i, va + pos))
                    start = pos + 1
        if (len(passwords) != 1 or len(hmacs) != 1 or matches != ['zero']
                or passwords[0][0] != hmacs[0][0]
                or not len(PASSWORD_KDF) <= hmacs[0][1] - passwords[0][1] < 4096):
            raise ValueError()
        address = passwords[0][1]
        return KDFBoundary(address + len(PASSWORD_KDF) - 3, address, digest, hmacs[0][1])
    except (ValueError, struct.error, OverflowError):
        raise CaptureError('unsupported_or_ambiguous_elf') from None


def loaded_addresses(maps, major, minor, inode, boundary):
    """以设备与 inode 匹配主程序；拒绝重复装载和非可执行地址。"""
    matching = []
    for line in maps.splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) < 5:
            raise CaptureError('invalid_process_maps')
        try:
            devmajor, devminor = (int(x, 16) for x in fields[3].split(':'))
            start, end = (int(x, 16) for x in fields[0].split('-'))
            if (devmajor, devminor, int(fields[4])) == (major, minor, inode):
                matching.append((start, end, int(fields[2], 16), fields[1]))
        except ValueError:
            raise CaptureError('invalid_process_maps') from None
    bases = [start for start, _, offset, _ in matching if offset == 0]
    if len(bases) != 1:
        raise CaptureError('ambiguous_executable_mapping')
    base = bases[0]
    signature = base + boundary.signature_address
    call = base + boundary.virtual_address
    for address, length in ((signature, len(PASSWORD_KDF)),
                            (base + boundary.hmac_signature_address, len(HMAC_KDF))):
        if sum('x' in perms and start <= address and address + length <= end
               for start, end, _, perms in matching) != 1:
            raise CaptureError('signature_not_executable')
    return signature, call


def process_start_time(pid):
    """stat 的 comm 可含空格或括号，故从最后一个右括号之后解析。"""
    raw = Path('/proc/%d/stat' % pid).read_text()
    return int(raw[raw.rfind(')') + 2:].split()[19])


def pin_process(pid, expected_sha256=SUPPORTED_SHA256):
    if not isinstance(pid, int) or pid <= 1:
        raise CaptureError('invalid_pid')
    start = process_start_time(pid)
    with open('/proc/%d/exe' % pid, 'rb') as executable:
        info = os.fstat(executable.fileno())
        with mmap.mmap(executable.fileno(), 0, access=mmap.ACCESS_READ) as image:
            boundary = locate_kdf_boundary(image, expected_sha256)
    if process_start_time(pid) != start:
        raise CaptureError('process_identity_changed')
    signature, call = loaded_addresses(Path('/proc/%d/maps' % pid).read_text(),
                                       os.major(info.st_dev), os.minor(info.st_dev), info.st_ino, boundary)
    return {'pid': pid, 'start_time': start, 'executable_dev': info.st_dev,
            'executable_inode': info.st_ino, 'executable_size': info.st_size,
            'executable_mtime_ns': info.st_mtime_ns, 'image_sha256': boundary.image_sha256,
            'signature_address': signature, 'call_address': call, 'signature': PASSWORD_KDF.hex(),
            'hmac_address': signature - boundary.signature_address + boundary.hmac_signature_address,
            'hmac_signature': HMAC_KDF.hex()}


class BoundDatabases:
    """固定账号与数据库 inode/salt，逐段无链接读取，保存前重新校验页一。"""

    def __init__(self, storage):
        self.storage = _absolute(storage)
        if self.storage.name != 'db_storage':
            raise CaptureError('account_storage_required')
        self.fd = _directory_fd(self.storage)
        info = os.fstat(self.fd)
        self.identity = (info.st_dev, info.st_ino)
        self.file_identities = {}
        self._pinning = True
        try:
            self.files = self._collect()
            if not self.files:
                raise CaptureError('no_encrypted_databases')
            self._pinning = False
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def check_identity(self):
        fd = _directory_fd(self.storage)
        try:
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != self.identity:
                raise CaptureError('account_storage_changed')
        finally:
            os.close(fd)

    def _collect(self):
        self.check_identity()
        paths = []
        for directory, dirs, names in os.walk(self.storage, followlinks=False):
            parent = Path(directory)
            for name in dirs:
                fd = _directory_fd(parent / name)
                os.close(fd)
            dirs[:] = [name for name in dirs if not (parent == self.storage and name == 'migrate')]
            for name in names:
                if name.endswith('.db'):
                    rel = (parent / name).relative_to(self.storage).as_posix()
                    _relative_db(rel)
                    page = self.page1(rel)
                    if len(page) != 4096 or page.startswith(b'SQLite format 3\0'):
                        raise CaptureError('unsupported_database_page')
                    paths.append(rel)
        return sorted(paths)

    def page1(self, rel):
        self.check_identity()
        _relative_db(rel)
        fd = os.dup(self.fd)
        opened = None
        try:
            parts = Path(rel).parts
            for part in parts[:-1]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            opened = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            info = os.fstat(opened)
            if not stat.S_ISREG(info.st_mode):
                raise CaptureError('database_not_regular')
            expected = self.file_identities.get(rel)
            if not self._pinning and (expected is None or (info.st_dev, info.st_ino) != expected[:2]):
                raise CaptureError('database_identity_changed')
            page = os.read(opened, 4096)
            identity = (info.st_dev, info.st_ino, page[:16])
            if self._pinning and expected is None:
                self.file_identities[rel] = identity
            elif identity != expected:
                raise CaptureError('database_identity_changed')
            # 旧 fd 可指向已被替换的分片目录；从固定账号根重新逐段遍历。
            current_fd = os.dup(self.fd)
            current_file = None
            try:
                for part in parts[:-1]:
                    next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current_fd)
                    os.close(current_fd)
                    current_fd = next_fd
                current_file = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current_fd)
                current = os.fstat(current_file)
                if (current.st_dev, current.st_ino) != identity[:2]:
                    raise CaptureError('database_identity_changed')
            except OSError:
                raise CaptureError('database_identity_changed') from None
            finally:
                if current_file is not None:
                    os.close(current_file)
                os.close(current_fd)
            self.check_identity()
            return page
        finally:
            if opened is not None:
                os.close(opened)
            os.close(fd)

    def verify_candidate(self, candidate):
        from wechatauto.db import _pbkdf2, _verify_enc_key
        if len(candidate) != 32:
            raise CaptureError('invalid_candidate_length')
        self.check_identity()
        verified = {}
        for rel in self.files:
            page = self.page1(rel)
            key = _pbkdf2(candidate, page[:16], 256000)
            if _verify_enc_key(key, page):
                verified[rel] = key
        return verified

    def persist_verified(self, keys, output):
        from wechatauto.db import _verify_enc_key
        self.check_identity()
        if set(keys) != set(self.files) or self._collect() != self.files:
            raise CaptureError('incomplete_database_keys')
        if any(len(key) != 32 or not _verify_enc_key(key, self.page1(rel)) for rel, key in keys.items()):
            raise CaptureError('fresh_page_verification_failed')
        output = _absolute(output)
        if output.is_relative_to(self.storage.parent):
            raise CaptureError('output_inside_account')
        _private_dir(output.parent)
        directory = _directory_fd(output.parent)
        temporary = None
        try:
            try:
                info = os.stat(output.name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                info = None
            if info is not None and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                                     or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
                raise CaptureError('unsafe_output_file')
            temporary = '.capture-' + os.urandom(12).hex()
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            with os.fdopen(fd, 'w') as target:
                json.dump({rel: key.hex() for rel, key in keys.items()}, target, sort_keys=True)
                target.flush()
                os.fsync(target.fileno())
            self.check_identity()
            if any(not _verify_enc_key(key, self.page1(rel)) for rel, key in keys.items()):
                raise CaptureError('fresh_page_verification_failed')
            os.replace(temporary, output.name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
            os.close(directory)
        return str(output)


def _receive(channel, size, deadline):
    result = bytearray()
    while len(result) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        channel.settimeout(remaining)
        data = channel.recv(size - len(result))
        if not data:
            raise CaptureError('observer_channel_closed')
        result.extend(data)
    return bytes(result)


def capture_existing(pid, storage, output, *, timeout=60, max_candidates=64, gdb='gdb',
                     expected_sha256=SUPPORTED_SHA256, on_ready=None):
    """观察已运行的 PID；返回密钥文件路径，超时仍清理并分离。

    expected_sha256 仅为离线合成测试开放覆盖；真实客户端应使用默认固定哈希。
    on_ready 回调仅通知硬件断点已安装，不传递候选或密钥。
    """
    if sys.platform != 'linux':
        raise CaptureError('linux_required')
    if not 1 <= timeout <= 300 or not 1 <= max_candidates <= 64:
        raise ValueError('timeout/candidate limit out of range')
    pin = pin_process(pid, expected_sha256)
    bound = BoundDatabases(storage)
    parent, child = socket.socketpair()
    process = None
    detached = False
    keys = {}
    outcome = 'capture_timeout'
    deadline = time.monotonic() + timeout
    observer = Path(__file__).with_name('linux_capture_observer.py')
    command = [gdb, '--nx', '--nh', '--batch', '--quiet', '--iex', 'set auto-load off',
               '--iex', 'set debuginfod enabled off', '-ex',
               'python exec(compile(open(%r, "rb").read(), %r, "exec")); run(%d)' %
               (str(observer), str(observer), child.fileno())]
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, pass_fds=(child.fileno(),), start_new_session=True)
        child.close()
        pin.update(timeout=timeout, max_candidates=max_candidates)
        payload = json.dumps(pin).encode()
        parent.sendall(struct.pack('!I', len(payload)) + payload)
        count = 0
        try:
            while True:
                event = _receive(parent, 1, deadline)
                if event == b'R':
                    if on_ready is not None:
                        on_ready()
                elif event == b'C':
                    count += 1
                    if count > max_candidates:
                        raise CaptureError('candidate_limit')
                    candidate = _receive(parent, 32, deadline)
                    keys.update(bound.verify_candidate(candidate))
                    candidate = None
                    complete = set(keys) == set(bound.files)
                    parent.sendall(b'Y' if complete else b'N')
                    if complete:
                        outcome = 'success'
                elif event == b'D':
                    detached = True
                    break
                elif event == b'E':
                    outcome = 'observer_failed'
                else:
                    raise CaptureError('observer_protocol_failed')
        except (TimeoutError, socket.timeout):
            outcome = 'capture_timeout'
        finally:
            if not detached and process.poll() is None:
                process.send_signal(signal.SIGINT)
                cleanup_deadline = time.monotonic() + 10
                try:
                    while True:
                        cleanup_event = _receive(parent, 1, cleanup_deadline)
                        if cleanup_event == b'D':
                            detached = True
                            break
                        if cleanup_event == b'C':
                            _receive(parent, 32, cleanup_deadline)
                            parent.sendall(b'Y')
                        elif cleanup_event not in (b'E', b'R'):
                            raise CaptureError('observer_protocol_failed')
                except (OSError, CaptureError):
                    pass
            if not detached:
                raise CaptureError('observer_cleanup_unconfirmed; inspect debugger PID %d' % process.pid)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                raise CaptureError('observer_exit_unconfirmed; debugger PID %d' % process.pid) from None
        if outcome != 'success':
            raise CaptureError(outcome)
        info = os.stat('/proc/%d/exe' % pid)
        if (process_start_time(pid), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
                pin['start_time'], pin['executable_dev'], pin['executable_inode'],
                pin['executable_size'], pin['executable_mtime_ns']):
            raise CaptureError('process_identity_changed')
        return bound.persist_verified(keys, output)
    finally:
        parent.close()
        child.close()
        bound.close()


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='Attach to a pinned existing Linux WeChat PID and save only HMAC-verified raw keys.')
    parser.add_argument('--pid', required=True, type=int)
    parser.add_argument('--storage', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--timeout', type=int, default=60)
    parser.add_argument('--gdb', default='gdb')
    args = parser.parse_args(argv)
    try:
        result = capture_existing(args.pid, args.storage, args.output, timeout=args.timeout,
                                  gdb=args.gdb, on_ready=lambda: print('observer_ready', flush=True))
    except (CaptureError, OSError, ValueError) as exc:
        print('capture_failed: %s' % exc, file=sys.stderr)
        return 1
    print('verified_keys_saved: %s' % result)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
