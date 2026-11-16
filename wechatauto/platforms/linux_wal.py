"""Linux 私有快照的 SQLCipher WAL 合并；只回放完整且经过校验的提交。

WAL 头、累积校验与提交边界遵循 SQLite 官方格式：
https://www.sqlite.org/fileformat.html#walformat
"""

import hashlib
import hmac
import struct

from wechatauto.db import PAGE_SZ, RESERVE_SZ, _decrypt_page, _pbkdf2


def _checksum(data, state, byteorder):
    first, second = state
    for left, right in struct.iter_unpack(byteorder + 'II', data):
        first = (first + left + second) & 0xffffffff
        second = (second + right + first) & 0xffffffff
    return first, second


def _valid_frame_mac(page, pgno, mac_key):
    start = 16 if pgno == 1 else 0
    digest = hmac.new(mac_key, page[start:PAGE_SZ - RESERVE_SZ + 16], hashlib.sha512)
    digest.update(struct.pack('<I', pgno))
    return hmac.compare_digest(digest.digest(), page[-64:])


def merge_committed_wal(destination, wal_path, key, source_salt):
    """将只读加密 WAL 快照回放到私有明文副本，返回最后提交处的帧数。

    source_salt 必须来自同一主库加密快照；48 字节 key 使用其显式 salt。
    无有效提交时不改副本；损坏、旧世代、未提交或不完整尾部均不应用。
    """
    if len(key) not in (32, 48) or len(source_salt) != 16:
        raise ValueError('SQLCipher key/salt length invalid')
    salt = key[32:] if len(key) == 48 else source_salt
    mac_key = _pbkdf2(key[:32], bytes(value ^ 0x3a for value in salt), 2)
    with open(wal_path, 'rb') as wal:
        header = wal.read(32)
        if len(header) != 32:
            return 0
        magic, version, page_size = struct.unpack('>III', header[:12])
        if magic not in (0x377f0682, 0x377f0683) or version != 3007000 or page_size != PAGE_SZ:
            return 0
        byteorder = '<' if magic == 0x377f0682 else '>'
        state = _checksum(header[:24], (0, 0), byteorder)
        if state != struct.unpack('>II', header[24:]):
            return 0
        generation = header[16:24]
        frames, last_commit = 0, 0
        while True:
            frame_header = wal.read(24)
            page = wal.read(PAGE_SZ)
            if len(frame_header) != 24 or len(page) != PAGE_SZ:
                break
            pgno, commit_size = struct.unpack('>II', frame_header[:8])
            if (not 0 < pgno < 0xffffffff or commit_size >= 0xffffffff
                    or frame_header[8:16] != generation):
                break
            expected = _checksum(frame_header[:8] + page, state, byteorder)
            if expected != struct.unpack('>II', frame_header[16:24]):
                break
            if not _valid_frame_mac(page, pgno, mac_key):
                break
            state = expected
            frames += 1
            if commit_size:
                last_commit = frames
        if not last_commit:
            return 0
        # 先确定完整提交前缀，再写临时副本；不暴露未提交事务的任何页面。
        wal.seek(32)
        final_size = 0
        with open(destination, 'r+b') as out:
            for _ in range(last_commit):
                frame_header = wal.read(24)
                page = wal.read(PAGE_SZ)
                pgno, commit_size = struct.unpack('>II', frame_header[:8])
                out.seek((pgno - 1) * PAGE_SZ)
                out.write(_decrypt_page(key, page, pgno))
                if commit_size:
                    out.truncate(commit_size * PAGE_SZ)
                    final_size = commit_size
            out.seek(0)
            page1 = bytearray(out.read(PAGE_SZ))
            if len(page1) != PAGE_SZ:
                raise RuntimeError('WAL 提交缺少完整页一')
            page1[28:32] = struct.pack('>I', final_size)
            page1[92:96] = page1[24:28]
            out.seek(0)
            out.write(page1)
    return last_commit
