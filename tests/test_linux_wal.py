"""Linux 只合并已提交且经过校验的 SQLCipher WAL。"""

from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch

from linux_fixtures import CHAT, DatabaseFixture, encrypt_pages, plain_database, wal_bytes
from wechatauto.db import WeChatDB



class LinuxWalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.fx = DatabaseFixture(self.base)
        self.rel = 'message/message_0.db'
        self.source = self.fx.storage / self.rel
        self.initial_encrypted = self.source.read_bytes()
        self.initial_pages = len(self.initial_encrypted) // 4096
        self.key = bytes.fromhex(self.fx.keys[self.rel])
        self.salt = self.initial_encrypted[:16]

    def tearDown(self):
        self.tmp.cleanup()

    def test_preallocated_main_and_only_stale_wal_keep_original_logical_size(self):
        self.source.write_bytes(self.initial_encrypted + b'\0' * (4096 * 64 - len(self.initial_encrypted)))
        wal = Path(str(self.source) + '-wal')
        wal.write_bytes(wal_bytes([(1, self.initial_pages, self.initial_encrypted[:4096], b'old-salt')]))
        db = WeChatDB(**self.fx.parameters())
        with closing(db._open(self.rel)) as connection:
            self.assertEqual(connection.execute('PRAGMA quick_check').fetchone()[0], 'ok')
            self.assertEqual(connection.execute('PRAGMA page_count').fetchone()[0], self.initial_pages)
        self.assertEqual(db.get_messages(CHAT)[0]['content'], 'hello')


    def checkpoint_states(self):
        rel = 'message/message_9.db'
        source, initial, key, salt = self.fx.add_database(
            rel, "CREATE TABLE a(v TEXT); CREATE TABLE b(v TEXT);"
                 "INSERT INTO a VALUES('A0'); INSERT INTO b VALUES('B0');")
        plain = self.fx.base / (self.fx.account + '-message_message_9.db.plain')
        with closing(sqlite3.connect(plain)) as connection:
            roots = dict(connection.execute("SELECT name,rootpage FROM sqlite_master WHERE name IN ('a','b')"))
            connection.execute("UPDATE a SET v='A1'")
            connection.commit()
        state_a = encrypt_pages(plain.read_bytes(), key, salt)
        with closing(sqlite3.connect(plain)) as connection:
            connection.execute("UPDATE b SET v='B1'")
            connection.commit()
        state_b = encrypt_pages(plain.read_bytes(), key, salt)
        pages = len(state_b) // 4096
        wal = Path(str(source) + '-wal')
        old_wal = wal_bytes([(1, 0, state_a[:4096]),
                             (roots['a'], pages, state_a[(roots['a'] - 1) * 4096:roots['a'] * 4096])])
        new_wal = wal_bytes([(1, 0, state_b[:4096]),
                             (roots['b'], pages, state_b[(roots['b'] - 1) * 4096:roots['b'] * 4096])],
                            salt=b'new-gen!')
        self.fx.write_keys()
        return rel, source, wal, state_a, old_wal, new_wal

    def test_checkpoint_and_wal_reset_between_copies_retry_the_entire_snapshot(self):
        rel, source, wal, state_a, old_wal, new_wal = self.checkpoint_states()
        wal.write_bytes(old_wal)
        db = WeChatDB(**self.fx.parameters())
        reader = db._linux_reader
        original = reader._snapshot
        copies = 0
        def checkpoint_after_main(path, target, expected=None):
            nonlocal copies
            original(path, target, expected)
            if Path(path).resolve() == source.resolve():
                copies += 1
                if copies == 1:
                    source.write_bytes(state_a)
                    wal.write_bytes(new_wal)
        with patch.object(reader, '_snapshot', side_effect=checkpoint_after_main):
            with closing(db._open(rel)) as connection:
                self.assertEqual(connection.execute('PRAGMA quick_check').fetchone()[0], 'ok')
                state = (connection.execute('SELECT v FROM a').fetchone()[0],
                         connection.execute('SELECT v FROM b').fetchone()[0])
        self.assertEqual(state, ('A1', 'B1'))
        self.assertGreaterEqual(copies, 2)

    def test_persistently_changing_bundle_does_not_replace_previous_valid_cache(self):
        rel, source, wal, state_a, old_wal, _ = self.checkpoint_states()
        db = WeChatDB(**self.fx.parameters())
        with closing(db._open(rel)):
            pass
        reader = db._linux_reader
        cached = Path(db.workdir) / reader.cache_name(rel)
        before = cached.read_bytes()
        wal.write_bytes(old_wal)
        original = reader._snapshot
        modified_ns = source.stat().st_mtime_ns
        def checkpoint_every_time(path, target, expected=None):
            nonlocal modified_ns
            original(path, target, expected)
            if Path(path).resolve() == source.resolve():
                source.write_bytes(state_a)
                # 相同密文重写可能仍在同一时钟刻度；显式推进 mtime 保证每轮都可观察。
                modified_ns += 1_000_000_000
                os.utime(source, ns=(source.stat().st_atime_ns, modified_ns))
        with patch.object(reader, '_snapshot', side_effect=checkpoint_every_time):
            with self.assertRaisesRegex(RuntimeError, '快照'):
                with closing(db._open(rel)):
                    pass
        self.assertEqual(cached.read_bytes(), before)

    def updated_frames(self, content):
        self.fx.append_wal(0, content)
        plain = self.fx.base / (self.fx.account + '-message_message_0.db.plain')
        encrypted = encrypt_pages(plain.read_bytes(), self.key[:32], self.salt, self.fx.explicit_salt)
        count = len(encrypted) // 4096
        return [(i + 1, count if i + 1 == count else 0, encrypted[i * 4096:(i + 1) * 4096])
                for i in range(count)]

    def merge(self, encoded, key=None, salt=None, encrypted=None):
        from wechatauto.platforms.linux_wal import merge_committed_wal
        output = self.base / 'merged.db'
        wal = self.base / 'source.wal'
        wal.write_bytes(encoded)
        encrypted_source = self.base / 'source.db'
        encrypted_source.write_bytes(encrypted if encrypted is not None else self.initial_encrypted)
        WeChatDB.__new__(WeChatDB)._decrypt_file(str(encrypted_source), str(output), key or self.key)
        before = output.read_bytes()
        applied = merge_committed_wal(output, wal, key or self.key, salt or self.salt)
        return output, applied, before

    def contents(self, output):
        table = 'Msg_' + hashlib.md5(CHAT.encode()).hexdigest()
        with closing(sqlite3.connect(output)) as connection:
            self.assertEqual(connection.execute('PRAGMA quick_check').fetchone()[0], 'ok')
            return connection.execute('SELECT message_content FROM ' + table).fetchone()[0]

    def test_last_committed_update_visible_and_uncommitted_tail_ignored_both_byte_orders(self):
        committed = self.updated_frames('committed')
        uncommitted = [(pgno, 0, page) for pgno, _, page in self.updated_frames('pending')]
        for order in ('little', 'big'):
            with self.subTest(byteorder=order):
                output, applied, _ = self.merge(wal_bytes(committed + uncommitted, byteorder=order))
                self.assertEqual(applied, len(committed))
                self.assertEqual(self.contents(output), 'committed')

    def test_no_commit_or_invalid_header_leaves_main_snapshot_byte_identical(self):
        frames = [(pgno, 0, page) for pgno, _, page in self.updated_frames('pending')]
        encoded = wal_bytes(frames)
        cases = [encoded, encoded[:15]]
        for offset, value in ((0, 0), (4, 3007001), (8, 1024), (24, 0)):
            bad = bytearray(encoded)
            struct.pack_into('>I', bad, offset, value)
            cases.append(bytes(bad))
        for candidate in cases:
            with self.subTest(length=len(candidate)):
                output, applied, before = self.merge(candidate)
                self.assertEqual(applied, 0)
                self.assertEqual(output.read_bytes(), before)

    def test_invalid_tail_stops_at_last_valid_commit_without_skipping_to_later_frames(self):
        committed = self.updated_frames('committed')
        next_transaction = self.updated_frames('must not apply')
        count = len(committed)
        for fault in ('truncated', 'generation', 'checksum', 'hmac'):
            with self.subTest(fault=fault):
                tail = list(next_transaction)
                last = tail[-1]
                if fault == 'generation':
                    tail[-1] = (*last, b'old-salt')
                if fault == 'hmac':
                    damaged = bytearray(last[2])
                    damaged[-1] ^= 1
                    tail[-1] = (last[0], last[1], bytes(damaged))
                encoded = bytearray(wal_bytes(committed + tail))
                if fault == 'truncated':
                    del encoded[-100:]
                if fault == 'checksum':
                    encoded[32 + (count + len(tail) - 1) * 4120 + 16] ^= 1
                if fault != 'truncated':
                    later = wal_bytes(committed + tail + committed)
                    encoded.extend(later[len(encoded):])
                output, applied, _ = self.merge(bytes(encoded))
                self.assertEqual(applied, count)
                self.assertEqual(self.contents(output), 'committed')

    def test_explicit_salt_key_uses_source_salt_for_frame_hmac(self):
        fx = DatabaseFixture(self.base / 'explicit', explicit_salt=True)
        self.fx = fx
        self.source = fx.storage / self.rel
        self.initial_encrypted = self.source.read_bytes()
        self.key = bytes.fromhex(fx.keys[self.rel])
        self.salt = self.key[32:]
        frames = self.updated_frames('explicit committed')
        output, applied, _ = self.merge(wal_bytes(frames))
        self.assertEqual(applied, len(frames))
        self.assertEqual(self.contents(output), 'explicit committed')

    def test_last_commit_size_shrinks_file_and_header_instead_of_using_maximum(self):
        plain = self.base / 'large.plain'
        raw = plain_database(plain, "CREATE TABLE t(id INTEGER PRIMARY KEY,v BLOB);"
                             "INSERT INTO t VALUES(1,zeroblob(50000));")
        initial = encrypt_pages(raw, self.key, self.salt)
        with closing(sqlite3.connect(plain)) as connection:
            connection.execute("UPDATE t SET v='small'")
            connection.commit()
            connection.execute('VACUUM')
        small = plain.read_bytes()
        self.assertLess(len(small), len(raw))
        second = encrypt_pages(small, self.key, self.salt)
        frames = []
        for image in (initial, second):
            count = len(image) // 4096
            frames.extend((i + 1, count if i + 1 == count else 0, image[i * 4096:(i + 1) * 4096])
                          for i in range(count))
        output, applied, _ = self.merge(wal_bytes(frames), encrypted=initial)
        self.assertEqual(applied, len(frames))
        self.assertEqual(output.stat().st_size, len(small))
        self.assertEqual(struct.unpack('>I', output.read_bytes()[28:32])[0], len(small) // 4096)
        with closing(sqlite3.connect(output)) as connection:
            self.assertEqual(connection.execute('PRAGMA quick_check').fetchone()[0], 'ok')
            self.assertEqual(connection.execute('SELECT v FROM t').fetchone()[0], 'small')

    def test_freelist_leaf_with_arbitrary_first_byte_is_applied_without_page_type_filter(self):
        plain = self.base / 'freelist.plain'
        initial_raw = plain_database(plain, "CREATE TABLE t(id INTEGER PRIMARY KEY,v BLOB);"
                                    "INSERT INTO t VALUES(1,'retained'),(2,zeroblob(50000));")
        initial = encrypt_pages(initial_raw, self.key, self.salt)
        with closing(sqlite3.connect(plain)) as connection:
            connection.execute('DELETE FROM t WHERE id=2')
            connection.commit()
        updated = bytearray(plain.read_bytes())
        trunk = struct.unpack('>I', updated[32:36])[0]
        self.assertGreater(trunk, 0)
        leaf_count = struct.unpack('>I', updated[(trunk - 1) * 4096 + 4:(trunk - 1) * 4096 + 8])[0]
        self.assertGreater(leaf_count, 0)
        leaf = struct.unpack('>I', updated[(trunk - 1) * 4096 + 8:(trunk - 1) * 4096 + 12])[0]
        updated[(leaf - 1) * 4096] = 0x7f
        encrypted = encrypt_pages(bytes(updated), self.key, self.salt)
        count = len(encrypted) // 4096
        frames = [(i + 1, count if i + 1 == count else 0, encrypted[i * 4096:(i + 1) * 4096])
                  for i in range(count)]
        output, applied, _ = self.merge(wal_bytes(frames), encrypted=initial)
        self.assertEqual(applied, count)
        self.assertEqual(output.read_bytes()[(leaf - 1) * 4096], 0x7f)
        with closing(sqlite3.connect(output)) as connection:
            self.assertEqual(connection.execute('PRAGMA quick_check').fetchone()[0], 'ok')
            self.assertEqual(connection.execute('SELECT v FROM t').fetchone()[0], 'retained')


if __name__ == '__main__':
    unittest.main()
