# -*- coding: utf-8 -*-
"""一键发·多平台内容发布工作台桌面端入口。"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import os
import sys
from io import BytesIO
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent
QT_BIN_DIR = ROOT_DIR / "runtime" / "python" / "Lib" / "site-packages" / "PyQt6" / "Qt6" / "bin"
if os.name == "nt" and QT_BIN_DIR.exists():
    os.add_dll_directory(str(QT_BIN_DIR))

from PyQt6.QtCore import QDate, QTime, QTimer
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from app_core import (
    account_service,
    activation_service,
    content_bundle,
    media_service,
    publish_service,
    task_service,
)
from app_core.release_integrity import verify_release_artifact
from app_core.branding import APP_ICON_RELATIVE_PATH, APP_TITLE, APP_VERSION, PRODUCT_NAME
from app_core.database import ensure_schema
from ui.common import apply_style
from ui.main_window import LicenseDialog, MainWindow
from ui.runtime_log import install_runtime_log_capture


def configure_application(app: QApplication) -> None:
    """设置操作系统和 Qt 使用的产品元数据。"""

    app.setApplicationName(PRODUCT_NAME)
    app.setApplicationDisplayName(APP_TITLE)
    app.setApplicationVersion(APP_VERSION)
    icon_path = ROOT_DIR / APP_ICON_RELATIVE_PATH
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))


def hide_windows_console() -> None:
    """正常桌面启动时隐藏 Python 控制台，运行记录改在客户端内显示。"""

    if os.name != "nt":
        return
    try:
        import ctypes

        console = ctypes.windll.kernel32.GetConsoleWindow()
        if console:
            ctypes.windll.user32.ShowWindow(console, 0)
    except Exception:
        # 隐藏控制台失败不影响桌面客户端本身启动。
        return


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
        await page.set_content("<title>YiJianFa Browser Test</title><main id='status'>ready</main>")
        title = await page.title()
        status = await page.locator("#status").inner_text()
        await browser.close()
    if title != "YiJianFa Browser Test" or status != "ready":
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
        "YIJIANFA_RELEASE_VERIFIED "
        f"version={manifest['version']} build={manifest.get('buildId') or '-'}"
    )


def start_authorized_wechat_schedule(
    window: MainWindow,
    manifest_path: str,
    schedule_time: str,
    account_name: str,
) -> None:
    """开发版验收入口：把内容包真实带入发布页并启动已授权定时任务。"""

    bundle = content_bundle.load_content_bundle(
        manifest_path,
        expected_type="text",
    )
    schedule_at = datetime.fromisoformat(schedule_time.replace("T", " "))
    accounts = [
        item
        for item in account_service.list_accounts()
        if int(item.get("type") or 0) == 10
        and str(item.get("profileName") or "") == account_name
        and str(item.get("userName") or "") == account_name
    ]
    if len(accounts) != 1:
        raise RuntimeError(
            f"公众号账号必须唯一匹配“{account_name}”，实际 {len(accounts)} 个"
        )

    imported = media_service.import_files_with_records(
        list(bundle["coverPaths"].values()),
        category="内容包",
    )
    if len(imported) != len(bundle["coverPaths"]):
        raise RuntimeError("公众号封面导入素材库失败")
    source_to_row = {
        str(item["sourcePath"]): item
        for item in imported
    }

    page = window.publish
    window._set_current_page(3)
    page.refresh(force=True)
    page._select_content_type(2)
    page.preflight.setChecked(False)
    # 受控真实验收遇到未知确认时必须把同一浏览器页面留给用户查看。
    page.background_mode.setChecked(False)
    page._selected_media_ids.clear()
    page._selected_account_ids = {int(accounts[0]["id"])}
    page._imported_article_image_specs.clear()
    page._imported_ai_disclosure = dict(bundle.get("aiDisclosure") or {})
    page.common_title_input.setText(bundle["title"])
    page.title_input.setPlainText(bundle["body"])
    page.platform_titles[10].setText(bundle["title"])
    page.platform_texts[10].setPlainText(bundle["body"])
    page.original_declaration.setChecked(False)
    page.ai_generated_content.setChecked(
        bool(page._imported_ai_disclosure.get("containsAiGeneratedContent"))
    )
    if page.wechat_group_notification:
        page.wechat_group_notification.setChecked(True)
    page.common_schedule_enabled.setChecked(True)
    page.common_schedule_date.setDate(
        QDate(schedule_at.year, schedule_at.month, schedule_at.day)
    )
    page.common_schedule_time.setTime(
        QTime(schedule_at.hour, schedule_at.minute)
    )
    page._sync_common_schedule_values()
    page.refresh(force=True)
    for ratio, source_path in bundle["coverPaths"].items():
        stored_path = str(source_to_row[source_path]["file_path"])
        combo = page.cover_34 if ratio == "3:4" else page.cover_43
        page._set_combo_data(combo, stored_path)
        platform_combo = (
            page.platform_cover_34[10]
            if ratio == "3:4"
            else page.platform_cover_43[10]
        )
        page._set_combo_data(platform_combo, stored_path)
    page._refresh_platform_cover_previews()
    page.update_cover_summary()

    payloads = page.collect_payloads("publish")
    task = publish_service.start_desktop_publish(payloads)
    page.active_task_id = int(task["id"])
    page.active_task_is_preflight = False
    page.active_task_mode = "publish"
    page.active_task_background_mode = True
    page.active_task_started_at = datetime.now()
    page.seen_event_ids.clear()
    page.log.clear()
    page.log.append(
        f"已从开发版导入内容包并启动公众号定时发表：{task['taskNo']}"
    )
    page.log.append(f"目标时间：{schedule_time}（Asia/Shanghai）")
    page._update_task_progress(
        {
            "status": "pending",
            "dryRun": 0,
            "itemCount": task.get("itemCount", 0),
        }
    )
    page._set_running(True, f"公众号定时发表运行中：{task['taskNo']}")
    page.task_timer.start()
    print(
        "ONECLICK_DEV_WECHAT_SCHEDULE_STARTED "
        f"taskId={task['id']} taskNo={task['taskNo']} "
        f"schedule={schedule_time}",
        flush=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--ui-test", action="store_true")
    parser.add_argument(
        "--wechat-verification-demo",
        action="store_true",
        help="仅显示本地模拟微信验证对话框，不连接公众号。",
    )
    parser.add_argument("--browser-self-test", action="store_true")
    parser.add_argument(
        "--page",
        choices=("workspace", "accounts", "media", "publish", "commerce", "tasks"),
        help="启动时直接打开指定工作页，便于本地验收。",
    )
    parser.add_argument("--verify-release", metavar="ZIP")
    parser.add_argument("--manifest", metavar="JSON")
    parser.add_argument("--signature", metavar="SIG")
    parser.add_argument(
        "--authorized-wechat-scheduled-publish",
        metavar="MANIFEST",
        help="仅在用户已明确授权时，从开发版导入并执行公众号定时发表。",
    )
    parser.add_argument(
        "--wechat-schedule-time",
        metavar="YYYY-MM-DD_HH:MM",
    )
    parser.add_argument(
        "--wechat-account-name",
        default="硅基进化",
    )
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
    app = QApplication(sys.argv)
    configure_application(app)
    install_runtime_log_capture()
    hide_windows_console()
    apply_style(app)
    ensure_schema()
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
    if args.authorized_wechat_scheduled_publish:
        if not args.wechat_schedule_time:
            parser.error(
                "--authorized-wechat-scheduled-publish 必须同时提供 "
                "--wechat-schedule-time"
            )
        QTimer.singleShot(
            500,
            lambda: start_authorized_wechat_schedule(
                window,
                args.authorized_wechat_scheduled_publish,
                args.wechat_schedule_time.replace("_", " "),
                args.wechat_account_name,
            ),
        )
    if args.wechat_verification_demo:
        import qrcode

        from app_core.wechat_verification import verification_broker
        from ui.wechat_verification_dialog import WechatVerificationDialog

        def make_demo_qr() -> bytes:
            image = qrcode.make("oneclick-local-wechat-verification-demo")
            output = BytesIO()
            image.save(output, format="PNG")
            return output.getvalue()

        request_id = verification_broker.create(
            task_id=900001,
            qr_image=make_demo_qr(),
            expires_in_seconds=90,
            refresh_callback=make_demo_qr,
        )
        QTimer.singleShot(
            350,
            lambda: WechatVerificationDialog(request_id, window).exec(),
        )
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
