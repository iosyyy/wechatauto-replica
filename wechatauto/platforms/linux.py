"""Linux/POSIX 只读数据库后端；密钥由调用方提供，不扫描进程。"""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import sys
import tempfile

LINUX_CACHE_VERSION = 1


class LinuxKeyError(RuntimeError):
    """缺少密钥或页一 HMAC 校验失败，不尝试其它账号或进程。"""


def _account_name(name):
    if not isinstance(name, str) or not name or name in (".", "..") or any(
            c in name for c in ("/", "\\", "\x00", ":")):
        raise ValueError("非法账号 account：必须为单个目录名")
    return name


def _relative_db(rel):
    if (not isinstance(rel, str) or not rel or "\\" in rel or ":" in rel
            or "\x00" in rel or rel.startswith("/")
            or any(p in ("", ".", "..") for p in rel.split("/"))
            or not rel.endswith(".db")):
        raise ValueError("密钥文件包含不安全的数据库相对路径")
    return str(PurePosixPath(rel))


def _not_symlink(path):
    if path.is_symlink():
        raise RuntimeError("拒绝符号链接 symlink: %s" % path.name)


def _absolute(path):
    path = Path(os.path.abspath(path))
    if sys.platform == "darwin" and path.parts[1:2] in (("var",), ("tmp",)):
        path = Path("/private") / path.relative_to("/")
    return path


def _directory_fd(path):
    path = _absolute(path)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except OSError:
        os.close(fd)
        raise RuntimeError("目录不存在或包含符号链接 symlink") from None


@contextmanager
def _read_file(path):
    """逐段以 O_NOFOLLOW 打开，阻止检查之后替换目录或文件的链接逃逸。"""
    path = _absolute(path)
    _not_symlink(path)
    fd = _directory_fd(path.parent)
    file_fd = None
    try:
        file_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise RuntimeError("需要常规文件: %s" % path.name)
        with os.fdopen(file_fd, "rb") as source:
            file_fd = None
            yield source
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(fd)


def account_dirs(root):
    root = _absolute(root)
    _not_symlink(root)
    if not root.is_dir():
        return []
    os.close(_directory_fd(root))
    out = []
    for child in sorted(root.iterdir()):
        if child.is_symlink():
            continue
        storage = child / "db_storage"
        if child.is_dir() and storage.is_dir():
            _not_symlink(storage)
            out.append(child)
    return out


def _locate(root):
    path = _absolute(Path(root).expanduser())
    _not_symlink(path)
    if not path.is_dir():
        return None
    for candidate in (path, path / "xwechat_files"):
        if account_dirs(candidate):
            return str(candidate.resolve())
    return None


def discover(db_dir=None):
    """解析 XDG 文档目录的数据文件，只展开 HOME，不执行 shell。"""
    explicit = db_dir if db_dir is not None else os.environ.get("WECHATAUTO_DATA_DIR")
    if explicit is not None and explicit != "":
        found = _locate(explicit)
        if not found:
            raise RuntimeError("指定的 db_dir/WECHATAUTO_DATA_DIR 中没有账号数据库目录")
        return found
    if db_dir is not None:
        raise RuntimeError("指定的 db_dir 数据目录为空")
    if sys.platform != "linux":
        return None
    home = Path.home()
    config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    documents = []
    try:
        with _read_file(config / "user-dirs.dirs") as source:
            data = source.read(65536).decode("utf-8")
        for line in data.splitlines():
            match = re.fullmatch(r'\s*XDG_DOCUMENTS_DIR="((?:[^"\\]|\\.)*)"\s*(?:#.*)?', line)
            if not match:
                continue
            value = re.sub(r'\\(["\\])', r'\1', match.group(1))
            value = value.replace("${HOME}", str(home)).replace("$HOME", str(home))
            if "$" not in value and "`" not in value and os.path.isabs(value):
                documents.append(Path(value))
    except (OSError, RuntimeError, UnicodeDecodeError):
        pass
    documents.extend((home / "Documents", home / "文档"))
    for documents_dir in documents:
        found = _locate(documents_dir / "xwechat_files")
        if found:
            return found
    return None


def _private_dir(path):
    """仅新建私有目录；既有目录权限不合适时拒绝，绝不修改外部权限。"""
    path = _absolute(path)
    if not path.exists():
        if not path.parent.exists():
            _private_dir(path.parent)
        os.close(_directory_fd(path.parent))
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    _not_symlink(path)
    os.close(_directory_fd(path))
    info = path.stat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise RuntimeError("缓存目录必须由当前用户拥有且权限为 0700: %s" % path)


class LinuxReader:
    def __init__(self, db, db_dir, account, keys_file, workdir, master_key):
        root = discover(db_dir)
        if not root:
            raise RuntimeError("未找到 Linux 数据库目录，请显式指定 db_dir")
        self.root = Path(root)
        accounts = account_dirs(root)
        if account is None:
            if len(accounts) != 1:
                raise RuntimeError("存在多个或没有账号，请显式指定 account")
            account = accounts[0].name
        _account_name(account)
        self.storage = self.root / account / "db_storage"
        self._validate_source(self.storage, directory=True)
        self.identity = hashlib.sha256((str(self.root) + "\0" + account).encode()).hexdigest()
        self.explicit_keys = _absolute(keys_file) if keys_file is not None else None
        self.master_key = master_key
        db.db_dir, db.account = str(self.root), account
        db.account_dir = str(self.storage.parent)
        db._keys, db.unkeyed = {}, []
        db.master_key, db.cfg_dword = master_key, None
        self.db = db
        db._db_files = self.collect()
        if not db._db_files:
            raise RuntimeError("指定账号没有可读取的数据库")
        cache_base = (Path(workdir).expanduser() if workdir is not None else
                      Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
                      / "wechatauto" / "linux")
        self.workdir = _absolute(cache_base) / ("reader-" + self.identity)
        if self.workdir.resolve().is_relative_to(self.root):
            raise RuntimeError("缓存 workdir 不得位于微信数据目录内")
        if self.explicit_keys is not None and self.explicit_keys.is_relative_to(self.workdir):
            raise ValueError("显式 keys_file 密钥文件必须位于读取器私有 workdir 之外；"
                             "复用内部密钥缓存时请省略 keys_file")
        self._prepare_cache()
        db.workdir = str(self.workdir)
        self.cache_keys = self.workdir / "keys.json"
        db.keys_file = str(self.explicit_keys or self.cache_keys)
        self.load_keys()

    def _validate_source(self, path, directory=False):
        path = Path(path)
        try:
            relative = path.relative_to(self.root)
        except ValueError:
            raise RuntimeError("数据库路径超出所选账号根目录") from None
        cursor = self.root
        _not_symlink(cursor)
        for name in relative.parts:
            cursor = cursor / name
            _not_symlink(cursor)
        if directory and not path.is_dir():
            raise RuntimeError("指定账号 account 的 db_storage 不存在")
        if not path.resolve().is_relative_to(self.root):
            raise RuntimeError("数据库路径超出所选账号根目录")
        return path

    @contextmanager
    def source(self, path):
        """从固定根目录逐段打开，不跟随账号、分片目录或最终文件链接。"""
        path = self._validate_source(path)
        rel = path.relative_to(self.root)
        fd = _directory_fd(self.root)
        opened = None
        try:
            for component in rel.parts[:-1]:
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            opened = os.open(rel.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            if not stat.S_ISREG(os.fstat(opened).st_mode):
                raise RuntimeError("数据库必须为常规文件")
            with os.fdopen(opened, "rb") as stream:
                opened = None
                yield stream
        finally:
            if opened is not None:
                os.close(opened)
            os.close(fd)

    def collect(self):
        self._validate_source(self.storage, directory=True)
        files = []
        for directory, dirs, names in os.walk(self.storage, followlinks=False):
            parent = Path(directory)
            for name in dirs:
                self._validate_source(parent / name, directory=True)
            dirs[:] = [name for name in dirs if not (parent == self.storage and name == "migrate")]
            for name in names:
                if not name.endswith(".db"):
                    continue
                path = self._validate_source(parent / name)
                with self.source(path) as stream:
                    size = os.fstat(stream.fileno()).st_size
                files.append((path.relative_to(self.storage).as_posix(), str(path), size))
        return sorted(files)

    def _prepare_cache(self):
        _private_dir(self.workdir)
        marker = self.workdir / ".owner.json"
        expected = {"format": 1, "identity": self.identity}
        if marker.exists():
            with _read_file(marker) as source:
                if json.load(source) != expected:
                    raise RuntimeError("缓存目录身份不匹配")
        elif any(self.workdir.iterdir()):
            raise RuntimeError("缓存目录已有无关文件，拒绝覆盖")
        else:
            self.write_private(marker, json.dumps(expected).encode())
        for path in self.workdir.iterdir():
            _not_symlink(path)
            if path.is_file() and (path.stat().st_uid != os.getuid()
                                   or stat.S_IMODE(path.stat().st_mode) != 0o600):
                raise RuntimeError("缓存文件权限必须为 0600: %s" % path.name)

    def write_private(self, target, payload):
        self.check_cache(target)
        fd, temporary = tempfile.mkstemp(prefix=".write-", dir=self.workdir)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(payload)
            self.check_cache(target)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def check_cache(self, path):
        _private_dir(self.workdir)
        path = Path(path)
        if path.parent != self.workdir:
            raise RuntimeError("缓存路径超出私有工作目录")
        _not_symlink(path)
        if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
            raise RuntimeError("拒绝覆盖异常缓存文件")

    def page1(self, path):
        with self.source(path) as source:
            return source.read(4096)

    def load_keys(self):
        from wechatauto.db import _pbkdf2, _verify_enc_key
        keys, supplied = {}, {}
        source = self.explicit_keys or self.cache_keys
        if self.explicit_keys is not None or (self.master_key is None and source.exists()):
            try:
                with _read_file(source) as stream:
                    supplied = json.load(stream)
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                raise LinuxKeyError("无法读取 keys_file 密钥 JSON 文件") from None
            if not isinstance(supplied, dict):
                raise ValueError("密钥 JSON 必须为相对路径到十六进制密钥的映射")
            parsed = {}
            for rel, value in supplied.items():
                _relative_db(rel)
                if not isinstance(value, str) or not re.fullmatch(r"(?:[0-9a-fA-F]{64}|[0-9a-fA-F]{96})", value):
                    raise ValueError("密钥必须为 64 或 96 位十六进制字符串")
                parsed[rel] = bytes.fromhex(value)
            supplied = parsed
        if self.explicit_keys is not None:
            known = {rel for rel, _, _ in self.db._db_files}
            if any(rel not in known for rel in supplied):
                raise LinuxKeyError("keys_file 密钥条目未对应所选账号的真实数据库")
        master = None
        if self.master_key is not None:
            if not isinstance(self.master_key, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", self.master_key):
                raise ValueError("主密钥必须为 64 位十六进制字符串")
            master = bytes.fromhex(self.master_key)
        for rel, path, _ in self.db._db_files:
            page = self.page1(path)
            key = supplied.get(rel)
            if key is None and master is not None:
                key = _pbkdf2(master, page[:16], 256000)
            if key is not None and _verify_enc_key(key, page):
                keys[rel] = key[:32] if len(key) == 48 and _verify_enc_key(key[:32], page) else key
        self.db._keys = keys
        self.db.unkeyed = [rel for rel, _, _ in self.db._db_files if rel not in keys]
        if self.db.unkeyed:
            raise LinuxKeyError("数据库缺少可验证密钥: %s；请提供 keys_file 或 master_key，"
                                "Linux 暂不自动提取密钥" % ", ".join(self.db.unkeyed))
        payload = json.dumps({rel: key.hex() for rel, key in keys.items()}, sort_keys=True).encode()
        if self.cache_keys.exists():
            with _read_file(self.cache_keys) as stream:
                old = stream.read()
            if old != payload:
                self.write_private(self.workdir / "keys.json.bak", old)
        self.write_private(self.cache_keys, payload)

    def refresh(self):
        files = self.collect()
        if files != self.db._db_files or self.db.unkeyed:
            self.db._db_files = files
            self.load_keys()

    def cache_name(self, rel):
        return "db-" + hashlib.sha256(rel.encode()).hexdigest() + ".db"

    def watermark_path(self, path):
        path = _absolute(path)
        if path.parent != self.workdir:
            raise ValueError("Linux Listener watermark_file 必须位于 db.workdir 私有目录")
        if path.name != "listener_watermark.json":
            raise ValueError("Linux Listener 水位文件名必须为 listener_watermark.json")
        self.check_cache(path)
        return str(path)

    @staticmethod
    def check_snapshot(path):
        """用转义后的只读 URI 校验真实快照，路径中的 ?/# 不能变成参数。"""
        try:
            connection = sqlite3.connect(_absolute(path).as_uri() + "?mode=ro", uri=True)
            try:
                rows = connection.execute("PRAGMA quick_check").fetchall()
            finally:
                connection.close()
            return bool(rows) and all(str(row[0]) == "ok" for row in rows)
        except sqlite3.Error:
            return False

    def open(self, rel):
        from wechatauto.db import PAGE_SZ, STAMP_VERSION, _sqlite_text_factory, _verify_enc_key
        source = Path(self.db._db_path(rel))
        self._validate_source(source)
        if rel not in self.db._keys:
            self.load_keys()
        key = self.db._keys[rel]
        if not _verify_enc_key(key, self.page1(source)):
            self.load_keys()
            key = self.db._keys[rel]
        target = self.workdir / self.cache_name(rel)
        stamp = Path(str(target) + ".stamp")
        wal = Path(str(source) + "-wal")
        for path in (target, stamp):
            self.check_cache(path)
        self._validate_source(wal)
        info = source.stat()
        wal_info = wal.stat() if wal.exists() else None
        revision = [STAMP_VERSION, LINUX_CACHE_VERSION, info.st_mtime_ns, info.st_size,
                    wal_info.st_mtime_ns if wal_info else 0, wal_info.st_size if wal_info else 0]
        old = None
        if stamp.exists():
            try:
                with _read_file(stamp) as stream:
                    old = json.load(stream)
            except (ValueError, OSError):
                pass
        if old != revision or not target.exists():
            with tempfile.TemporaryDirectory(prefix=".build-", dir=self.workdir) as temporary:
                temporary = Path(temporary)
                snapshot, plain = temporary / "encrypted", temporary / "plain"
                for attempt in range(4):
                    self._snapshot(source, snapshot)
                    self.db._decrypt_file(str(snapshot), str(plain), key)
                    os.chmod(plain, 0o600)
                    if self.check_snapshot(plain):
                        break
                else:
                    raise RuntimeError("数据库解密快照校验失败: %s" % rel)
                if wal_info and wal_info.st_size > self.db.WAL_HEADER_SZ:
                    wal_snapshot, merged = temporary / "wal", temporary / "merged"
                    self._snapshot(wal, wal_snapshot)
                    shutil.copyfile(plain, merged)
                    os.chmod(merged, 0o600)
                    self.db._merge_wal(str(merged), str(wal_snapshot), key, 0)
                    if not self.check_snapshot(merged):
                        raise RuntimeError("WAL 合并快照校验失败: %s" % rel)
                    os.replace(merged, plain)
                self.check_cache(target)
                os.replace(plain, target)
                self.write_private(stamp, json.dumps(revision).encode())
        connection = sqlite3.connect(target.as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.text_factory = _sqlite_text_factory
        return connection

    def _snapshot(self, source, target):
        with self.source(source) as stream:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as out:
                shutil.copyfileobj(stream, out)

    def invalidate(self):
        for path in self.workdir.iterdir():
            if re.fullmatch(r"db-[0-9a-f]{64}\.db(?:\.stamp)?", path.name):
                self.check_cache(path)
                path.unlink()


def list_accounts(db_dir=None):
    root = discover(db_dir)
    if not root:
        return []
    out = []
    for directory in account_dirs(root):
        out.append({"account": directory.name, "wxid": re.sub(r"_\w{4}$", "", directory.name),
                    "path": str(directory), "last_activity": directory.stat().st_mtime})
    return out
