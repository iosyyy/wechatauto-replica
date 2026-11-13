"""安装读取后端时不要求 Windows GUI 依赖。"""
from pathlib import Path
import unittest

from packaging.requirements import Requirement

try:
    import tomllib
except ImportError:
    tomllib = None


@unittest.skipIf(tomllib is None, "元数据检查使用 Python 3.11+ tomllib")
class PackageMetadataTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / "pyproject.toml"
        self.requirements = [
            Requirement(r) for r in tomllib.loads(path.read_text(encoding="utf-8"))["project"]["dependencies"]
        ]

    def names_for(self, platform):
        return {r.name for r in self.requirements
                if r.marker is None or r.marker.evaluate({"sys_platform": platform})}

    def test_linux_installs_reader_without_windows_or_gui_stack(self):
        names = self.names_for("linux")
        self.assertTrue({"cryptography", "psutil", "colorama", "zstandard"} <= names)
        self.assertFalse({"pywin32", "winsdk", "uiautomation", "opencv-python",
                          "pyautogui", "imageio-ffmpeg"} & names)

    def test_windows_still_installs_existing_gui_dependencies(self):
        self.assertTrue({"pywin32", "winsdk", "uiautomation", "opencv-python",
                         "pyautogui", "imageio-ffmpeg"} <= self.names_for("win32"))


if __name__ == "__main__":
    unittest.main()
