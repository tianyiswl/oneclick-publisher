# -*- coding: utf-8 -*-
"""一键发内置发布能力的离线完整性检查。"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from app_core import publish_runtime
from myUtils import postVideo
from utils import base_social_media


ROOT_DIR = Path(__file__).resolve().parent


class EmbeddedCapabilitiesTests(unittest.TestCase):
    def test_publish_runtime_uses_embedded_capability_modules(self) -> None:
        """正式发布运行时必须能从一键发自身加载，不依赖外部工程。"""

        for module in (publish_runtime, postVideo, base_social_media):
            module_path = Path(module.__file__).resolve()
            self.assertTrue(module_path.is_relative_to(ROOT_DIR), module_path)

    def test_embedded_browser_initialization_script_exists(self) -> None:
        script_path = ROOT_DIR / "utils" / "stealth.min.js"
        self.assertTrue(script_path.is_file())
        self.assertGreater(script_path.stat().st_size, 100)

    def test_macos_bundle_resources_are_browser_candidates(self) -> None:
        executable = Path("/Applications/一键发.app/Contents/MacOS/一键发")
        with (
            patch.object(
                base_social_media.sys,
                "_MEIPASS",
                "/tmp/Frameworks",
                create=True,
            ),
            patch.object(base_social_media.sys, "executable", str(executable)),
        ):
            candidates = base_social_media._runtime_base_dirs()

        self.assertIn(
            Path("/Applications/一键发.app/Contents/Resources"),
            candidates,
        )

    def test_domestic_platform_entrypoints_are_callable(self) -> None:
        entrypoints = (
            postVideo.post_video_DouYin,
            postVideo.post_video_tencent,
            postVideo.post_video_ks,
            postVideo.post_video_bilibili,
        )
        self.assertTrue(all(callable(entrypoint) for entrypoint in entrypoints))


if __name__ == "__main__":
    unittest.main()
