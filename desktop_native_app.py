# -*- coding: utf-8 -*-
"""发射台· 多平台发布助手桌面端入口。"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent
QT_BIN_DIR = ROOT_DIR / "runtime" / "python" / "Lib" / "site-packages" / "PyQt6" / "Qt6" / "bin"
if os.name == "nt" and QT_BIN_DIR.exists():
    os.add_dll_directory(str(QT_BIN_DIR))

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from app_core import account_service, activation_service, media_service, task_service
from app_core.release_integrity import verify_release_artifact
from app_core.branding import APP_ICON_RELATIVE_PATH, APP_TITLE, APP_VERSION, PRODUCT_NAME
from app_core.database import ensure_schema
from ui.common import apply_style
from ui.main_window import LicenseDialog, MainWindow


def configure_application(app: QApplication) -> None:
    """设置操作系统和 Qt 使用的产品元数据。"""

    app.setApplicationName(PRODUCT_NAME)
    app.setApplicationDisplayName(APP_TITLE)
    app.setApplicationVersion(APP_VERSION)
    icon_path = ROOT_DIR / APP_ICON_RELATIVE_PATH
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))


def run_self_test() -> None:
    ensure_schema()
    account_service.account_stats()
    media_service.media_stats()
    task_service.list_tasks()
    print("NATIVE_DESKTOP_SELF_TEST_OK")


def run_ui_test() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication([])
    configure_application(app)
    apply_style(app)
    window = MainWindow()
    window.show()
    app.processEvents()
    print("NATIVE_DESKTOP_UI_OK")


async def _run_browser_self_test() -> None:
    from playwright.async_api import async_playwright

    from utils.base_social_media import launch_chromium_with_codecs

    async with async_playwright() as playwright:
        browser = await launch_chromium_with_codecs(playwright, headless=True)
        page = await browser.new_page()
        await page.set_content("<title>Fashetai Browser Test</title><main id='status'>ready</main>")
        title = await page.title()
        status = await page.locator("#status").inner_text()
        await browser.close()
    if title != "Fashetai Browser Test" or status != "ready":
        raise RuntimeError("浏览器自检页面回读失败")


def run_browser_self_test() -> None:
    asyncio.run(_run_browser_self_test())
    print("NATIVE_DESKTOP_BROWSER_OK")


def run_release_verification(
    artifact_path: str,
    manifest_path: str,
    signature_path: str,
) -> None:
    manifest = verify_release_artifact(
        Path(artifact_path),
        Path(manifest_path),
        Path(signature_path),
    )
    print(
        "FASHETAI_RELEASE_VERIFIED "
        f"version={manifest['version']} build={manifest.get('buildId') or '-'}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--ui-test", action="store_true")
    parser.add_argument("--browser-self-test", action="store_true")
    parser.add_argument(
        "--page",
        choices=("workspace", "accounts", "media", "publish", "tasks"),
        help="启动时直接打开指定工作页，便于本地验收。",
    )
    parser.add_argument("--verify-release", metavar="ZIP")
    parser.add_argument("--manifest", metavar="JSON")
    parser.add_argument("--signature", metavar="SIG")
    args = parser.parse_args()
    if args.self_test:
        run_self_test()
        return 0
    if args.ui_test:
        run_ui_test()
        return 0
    if args.browser_self_test:
        run_browser_self_test()
        return 0
    if args.verify_release:
        if not args.manifest or not args.signature:
            parser.error("--verify-release 必须同时提供 --manifest 和 --signature")
        run_release_verification(args.verify_release, args.manifest, args.signature)
        return 0
    ensure_schema()
    app = QApplication(sys.argv)
    configure_application(app)
    apply_style(app)
    status = activation_service.license_status()
    if not status.get("accessAllowed"):
        dialog = LicenseDialog(activation_required=True)
        dialog.exec()
        if not activation_service.license_status().get("accessAllowed"):
            return 0
    window = MainWindow()
    window.show()
    if args.page:
        # 窗口首次 show 后再应用启动页，避免 Qt 初始布局将
        # 导航选中态与 QStackedWidget 内容重置为不同页。
        QTimer.singleShot(
            0,
            lambda page_key=args.page: window.set_current_page_by_key(page_key),
        )
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
