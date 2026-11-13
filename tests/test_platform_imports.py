"""无 Windows 依赖的独立进程也能使用数据库公开接口。"""
import os
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(sys.platform == "win32", "这里验证非 Windows 导入边界")
class PlatformImportTests(unittest.TestCase):
    def run_python(self, code):
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=ROOT,
            capture_output=True, text=True, timeout=15,
            env={**os.environ, "PYTHONPATH": str(ROOT)},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_package_import_does_not_load_gui_or_database(self):
        self.run_python("""
import sys
import wechatauto
assert wechatauto.__version__
assert 'wechatauto.wx' not in sys.modules
assert 'wechatauto.db' not in sys.modules
assert 'uiautomation' not in sys.modules
assert 'win32gui' not in sys.modules
""")

    def test_database_export_and_listener_do_not_load_windows_modules(self):
        self.run_python("""
import sys
from wechatauto import WeChatDB, list_accounts
from wechatauto.db import Listener
assert WeChatDB.__name__ == 'WeChatDB'
assert callable(list_accounts) and callable(Listener)
assert 'wechatauto.wx' not in sys.modules
assert 'wechatauto.guia' not in sys.modules
assert 'uiautomation' not in sys.modules
""")

    def test_platform_independent_parameters_remain_available(self):
        self.run_python("""
from wechatauto import WxParam, WxResponse, PROJECT_NAME
assert WxParam.DB_VERIFY_TIMEOUT > 0
assert WxResponse('成功', 'ok').is_success
assert PROJECT_NAME == 'wechatauto'
""")

    def test_windows_gui_fails_explicitly_without_loading_it(self):
        self.run_python("""
import sys
import wechatauto
try:
    wechatauto.WeChatGUI
except RuntimeError as error:
    assert 'Windows' in str(error), str(error)
else:
    raise AssertionError('非 Windows 平台不能提供 Windows GUI 驱动')
assert 'wechatauto.guia' not in sys.modules
""")

    def test_unknown_export_raises_attribute_error(self):
        self.run_python("""
import wechatauto
assert not hasattr(wechatauto, 'not_a_public_api')
assert 'WeChatDB' in dir(wechatauto)
""")

    def test_windows_export_keeps_original_backend(self):
        self.run_python("""
import sys
from types import ModuleType
import wechatauto
original = ModuleType('wechatauto.wx')
original.WeChat = object()
sys.modules['wechatauto.wx'] = original
sys.platform = 'win32'
assert wechatauto.WeChat is original.WeChat
assert wechatauto.WeChat is original.WeChat
""")


if __name__ == "__main__":
    unittest.main()
