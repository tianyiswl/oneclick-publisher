# -*- coding: utf-8 -*-
"""开发版启动器回归测试。"""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
BUILD_SCRIPT = ROOT / "tools" / "build_dev_launcher.py"
LAUNCHER_SOURCE = ROOT / "tools" / "yijianfa_dev_launcher.swift"


class DevLauncherTests(unittest.TestCase):
    def test_native_launcher_source_starts_current_worktree_in_commerce_page(self) -> None:
        """开发版必须由原生 .app 承载，避免脚本 .app 被 LaunchServices 拒绝。"""

        self.assertTrue(BUILD_SCRIPT.is_file())
        self.assertTrue(LAUNCHER_SOURCE.is_file())
        builder = BUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('OUTPUT = ROOT / "开发版客户端"', builder)
        source = LAUNCHER_SOURCE.read_text(encoding="utf-8")
        self.assertIn("Process()", source)
        self.assertIn("process.processIdentifier", source)
        self.assertIn("desktop_native_app.py", source)
        self.assertIn('"--page", "commerce"', source)
        root_resolution = source.split("let python", 1)[0]
        self.assertEqual(root_resolution.count(".deletingLastPathComponent()"), 5)
        self.assertNotIn("release/", source)


if __name__ == "__main__":
    unittest.main()
