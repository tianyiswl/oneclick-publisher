# -*- coding: utf-8 -*-
"""平台预检素材生成入口测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import ImageFont

from tools import create_platform_test_assets


class PlatformTestAssetGenerationTests(unittest.TestCase):
    def test_probe_only_entry_succeeds_when_poster_font_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(create_platform_test_assets, "ROOT", root), patch.object(
                ImageFont,
                "truetype",
                side_effect=OSError("缺少海报字体"),
            ):
                exit_code = create_platform_test_assets.main(
                    ["--douyin-commerce-probe-only"]
                )

            probe = root / "ui" / "assets" / "douyin-commerce-probe.mp4"
            self.assertEqual(exit_code, 0)
            self.assertTrue(probe.is_file())
            self.assertIn(b"ftyp", probe.read_bytes()[:32])


if __name__ == "__main__":
    unittest.main()
