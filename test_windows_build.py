# -*- coding: utf-8 -*-
"""Windows 客户端构建工具的离线测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from pathlib import Path

from conf import resolve_user_data_dir
from tools.build_windows import (
    archive_filename,
    assert_windows_platform,
    assert_archive_safe,
    bundle_playwright_browsers,
    render_spec,
    resolve_playwright_browser_dirs,
)
from app_core.branding import APP_EXECUTABLE_NAME


class WindowsBuildTests(unittest.TestCase):
    def test_controlled_publish_cli_routes_runtime_logs_to_stderr(self) -> None:
        source = (Path(__file__).resolve().parent / "desktop_native_app.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("redirect_console_logger(sys.stderr)", source)
        self.assertIn("_wait_for_controlled_task", source)
        self.assertIn("DouyinVerificationDialog", source)
        self.assertIn("douyin_verification_broker.request_for_task(task_id)", source)

    def test_source_cli_can_explicitly_reuse_installed_user_data(self):
        with patch.dict(
            "os.environ",
            {"YIJIANFA_USER_DATA_DIR": "/tmp/yijianfa-controlled-user-data"},
        ):
            data_dir = resolve_user_data_dir(
                frozen=False,
                platform_name="darwin",
                source_dir=Path("/tmp/source"),
            )
        self.assertEqual(data_dir, Path("/tmp/yijianfa-controlled-user-data"))
    def test_frozen_windows_uses_local_app_data_outside_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bundle_root = root / "bundle"
            local_app_data = root / "AppData" / "Local"

            data_dir = resolve_user_data_dir(
                frozen=True,
                platform_name="win32",
                home=root / "home",
                local_app_data=local_app_data,
                source_dir=bundle_root,
            )

            self.assertEqual(data_dir, local_app_data / "一键发")
            self.assertNotIn(bundle_root, data_dir.parents)

    def test_archive_filename_is_ascii_and_versioned(self) -> None:
        self.assertEqual(
            archive_filename("20260805", "0.4.1"),
            "YiJianFa_0.4.1_Windows_x64_20260805.zip",
        )

    def test_resolve_required_playwright_browser_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache = root / "ms-playwright"
            manifest = root / "browsers.json"
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
            cache = root / "ms-playwright"
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

    def test_bundle_browser_resources_uses_pyinstaller_internal_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dist_root = root / APP_EXECUTABLE_NAME
            browser = root / "chromium-1223"
            executable = browser / "chrome-win" / "chrome.exe"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"browser")

            target_root = bundle_playwright_browsers(dist_root, [browser])

            copied = (
                target_root / "chromium-1223" / "chrome-win" / "chrome.exe"
            )
            self.assertEqual(
                target_root,
                dist_root / "_internal" / "runtime" / "playwright-browsers",
            )
            self.assertEqual(copied.read_bytes(), b"browser")

    def test_archive_with_runtime_cookie_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = Path(temp_dir) / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr(f"{APP_EXECUTABLE_NAME}/cookiesFile/account.json", "secret")

            with self.assertRaisesRegex(
                RuntimeError,
                "yijianfa/cookiesfile/<redacted>",
            ):
                assert_archive_safe(archive)

    def test_archive_with_seller_tool_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = Path(temp_dir) / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr(
                    f"{APP_EXECUTABLE_NAME}/seller_tools/license_crypto.py",
                    "private signing code",
                )

            with self.assertRaisesRegex(RuntimeError, "seller_tools"):
                assert_archive_safe(archive)

    def test_windows_spec_uses_ascii_executable_and_no_macos_bundle(self) -> None:
        spec = render_spec(Path(r"C:\work\oneclick"))

        self.assertIn("desktop_native_app.py", spec)
        self.assertIn("name='YiJianFa'", spec)
        self.assertIn('"tzdata"', spec)
        self.assertIn("COLLECT(", spec)
        self.assertNotIn("BUNDLE(", spec)

    def test_windows_spec_bundles_assets_including_probe_not_demo_runtime(self) -> None:
        spec = render_spec(Path(r"C:\\work\\oneclick"))

        self.assertIn('"ui/assets"', spec)
        self.assertNotIn("demo-runtime", spec)

    def test_windows_build_requirements_include_timezone_database(self) -> None:
        requirements = (
            Path(__file__).resolve().parent / "requirements-oneclick.txt"
        ).read_text(encoding="utf-8")

        self.assertIn("tzdata==2026.3", requirements)

    def test_non_windows_platform_fails_before_real_build(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Windows"):
            assert_windows_platform("darwin")

    def test_windows_workflow_requires_manual_trigger_and_supports_private_delivery(
        self,
    ) -> None:
        workflow = (
            Path(__file__).resolve().parent
            / ".github"
            / "workflows"
            / "build-windows.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("workflow_dispatch", workflow)
        self.assertNotIn("push:", workflow)
        self.assertNotIn("codex/finalize-macos-feedback", workflow)
        self.assertIn("windows-latest", workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn("验证 Windows 源码离屏界面", workflow)
        self.assertIn('"desktop_native_app.py", "--ui-test"', workflow)
        self.assertIn("python -m playwright install chromium", workflow)
        self.assertIn("tools/build_windows.py", workflow)
        self.assertIn("actions/upload-artifact@v4", workflow)
        self.assertIn("retention-days: 30", workflow)
        self.assertIn("default: artifact", workflow)
        self.assertIn("inputs.delivery == 'artifact'", workflow)
        self.assertIn("inputs.delivery == 'prerelease'", workflow)
        self.assertIn("gh release create", workflow)
        self.assertIn("--prerelease", workflow)


if __name__ == "__main__":
    unittest.main()
