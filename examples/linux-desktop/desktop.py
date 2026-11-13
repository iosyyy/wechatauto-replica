"""启动官方微信的私人 X11 桌面；不读取消息、不提取密钥、不自动发送。"""

import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import time
import urllib.request

CONFIG = "/etc/supervisor/wechat-desktop.conf"
PASSWORD = Path("/run/secrets/desktop_password")


def validate_password(path=PASSWORD):
    """只接受当前用户拥有的私人普通文件，避免无密码启动和静默截断。"""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Desktop password file must be private and owned by the desktop user")
        value = os.read(descriptor, 65).strip()
        if not re.fullmatch(rb"[A-Za-z0-9]{8}", value):
            raise ValueError("Desktop password must contain exactly eight alphanumeric characters")
    finally:
        os.close(descriptor)


def wait_display():
    """等待实际显示就绪；失败时退出，由 supervisor 处理重启。"""
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        result = subprocess.run(["xdpyinfo"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
        if result.returncode == 0:
            return
        time.sleep(0.15)
    raise RuntimeError("The private X display did not become ready")


def health():
    """检查运行组件，不把等待扫码等同于账号已在线。"""
    subprocess.run(["supervisorctl", "-c", CONFIG, "status"], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=4)
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
            "http://127.0.0.1:6080/vnc.html", timeout=3) as response:
        if response.status != 200:
            raise RuntimeError("Desktop web entry is unavailable")


def main():
    if sys.argv[1:2] == ["wait-display"]:
        wait_display()
        if len(sys.argv) < 3:
            raise ValueError("Missing desktop child command")
        os.execvp(sys.argv[2], sys.argv[2:])
    if sys.argv[1:] == ["health"]:
        health()
        return
    if len(sys.argv) != 1:
        raise ValueError("Unknown desktop command")
    os.umask(0o077)
    validate_password()
    for path in (Path.home() / "Documents", Path.home() / ".local/state/wechat-desktop",
                 Path("/tmp/wechat-desktop"), Path(os.environ["XDG_RUNTIME_DIR"])):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    authority = os.environ["XAUTHORITY"]
    subprocess.run(["xauth", "-f", authority], input=("add " + os.environ["DISPLAY"]
                   + " . " + secrets.token_hex(16) + "\n").encode(), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    os.execvp("supervisord", ["supervisord", "-n", "-c", CONFIG])


if __name__ == "__main__":
    main()
