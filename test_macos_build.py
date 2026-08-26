# -*- coding: utf-8 -*-
"""macOS 客户端构建资源的离线测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.build_macos import (
    assert_archive_safe,
    bundle_playwright_browsers,
    resolve_playwright_browser_dirs,
    write_spec,
)


class MacOSBuildTests(unittest.TestCase):
    def test_packaged_entrypoint_includes_matrix_controlled_action(self) -> None:
        source = (Path(__file__).resolve().parent / "desktop_native_app.py").read_text(
            encoding="utf-8"
        )

        self.assertIn('action == "matrix"', source)
        self.assertIn("start_douyin_graphic_matrix", source)

    def test_customer_archive_rejects_seller_license_material(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = Path(temp_dir) / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr(
                    "一键发.app/Contents/Resources/seller_tools/license_crypto.py",
                    "private signing code",
                )

            with self.assertRaisesRegex(RuntimeError, "seller_tools"):
                assert_archive_safe(archive)

    def test_resolve_required_playwright_browser_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache = root / "cache"
            manifest = root / "browsers.json"
            records = [
                {"name": "chromium", "revision": "1223"},
                {"name": "chromium-headless-shell", "revision": "1223"},
                {"name": "ffmpeg", "revision": "1011"},
                {"name": "firefox", "revision": "1522"},
            ]
            manifest.write_text(
                json.dumps({"browsers": records}),
                encoding="utf-8",
            )
            expected_names = (
                "chromium-1223",
                "chromium_headless_shell-1223",
                "ffmpeg-1011",
            )
            for name in expected_names:
                (cache / name).mkdir(parents=True)

            resolved = resolve_playwright_browser_dirs(cache, manifest)

            self.assertEqual([path.name for path in resolved], list(expected_names))

    def test_missing_browser_resource_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache = root / "cache"
            manifest = root / "browsers.json"
            cache.mkdir()
            manifest.write_text(
                json.dumps(
                    {
                        "browsers": [
                            {"name": "chromium", "revision": "1223"},
                            {
                                "name": "chromium-headless-shell",
                                "revision": "1223",
                            },
                            {"name": "ffmpeg", "revision": "1011"},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "chromium-1223"):
                resolve_playwright_browser_dirs(cache, manifest)

    def test_bundle_browser_resources_preserves_directory_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app = root / "一键发.app"
            browser = root / "chromium-1223"
            executable = browser / "chrome-mac-arm64" / "chrome"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"browser")

            target_root = bundle_playwright_browsers(app, [browser])

            copied = target_root / "chromium-1223" / "chrome-mac-arm64" / "chrome"
            self.assertEqual(copied.read_bytes(), b"browser")

    def test_macos_spec_bundles_assets_including_probe_not_demo_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            spec_path = Path(temp_dir) / "YiJianFa.spec"
            write_spec(spec_path, Path(temp_dir) / "app.icns")
            spec = spec_path.read_text(encoding="utf-8")

        self.assertIn('"ui/assets"', spec)
        self.assertIn(
            '"mcp", filter_submodules=lambda name: not name.startswith("mcp.cli")',
            spec,
        )
        self.assertNotIn("demo-runtime", spec)


if __name__ == "__main__":
    unittest.main()
