# -*- coding: utf-8 -*-
"""一键发·多平台内容发布工作台桌面端入口。"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
import os
import sys
import time
from io import BytesIO
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent
QT_BIN_DIR = ROOT_DIR / "runtime" / "python" / "Lib" / "site-packages" / "PyQt6" / "Qt6" / "bin"
if os.name == "nt" and QT_BIN_DIR.exists():
    os.add_dll_directory(str(QT_BIN_DIR))

from PyQt6.QtCore import QDate, QTime, QTimer
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QMessageBox

from app_core import (
    account_service,
    activation_service,
    content_bundle,
    media_service,
    publish_service,
    task_service,
    controlled_publish,
)
from app_core.release_integrity import verify_release_artifact
from app_core.branding import APP_ICON_RELATIVE_PATH, APP_TITLE, APP_VERSION, PRODUCT_NAME
from app_core.database import ensure_schema
from app_core.paths import USER_DATA_DIR, WECHAT_DRAFT_BRIDGE_DIR
from app_core.source_live_runtime import installed_gui_block_reason
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


def create_main_window() -> MainWindow:
    """创建并完成正常桌面端接线。"""

    window = MainWindow()
    window.publish.configure_wechat_draft_queue(WECHAT_DRAFT_BRIDGE_DIR)
    return window


def run_ui_test() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication([])
    configure_application(app)
    apply_style(app)
    window = create_main_window()
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


def _controlled_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")), flush=True)


def _read_controlled_request(path_value: str) -> dict:
    if path_value == "-":
        raw = sys.stdin.read()
    else:
        path = Path(str(path_value or "")).expanduser().resolve()
        if not path.is_file():
            raise controlled_publish.ControlledPublishError(
                "controlled_request_file_missing", "受控发布请求文件不存在"
            )
        raw = path.read_text(encoding="utf-8")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise controlled_publish.ControlledPublishError(
            "controlled_request_json_invalid", "受控发布请求不是有效 UTF-8 JSON"
        ) from exc
    if not isinstance(value, dict):
        raise controlled_publish.ControlledPublishError(
            "controlled_request_invalid", "受控发布请求必须是 JSON 对象"
        )
    return value


def _wait_for_controlled_task(task_id: int, *, interactive_verification: bool) -> None:
    """等待任务完成；正式任务遇到抖音验证时只拉起本机原生窗口。"""

    app = None
    douyin_verification_broker = None
    verification_dialog = None
    if interactive_verification:
        from app_core.douyin_verification import (
            verification_broker as douyin_verification_broker,
        )
        from ui.douyin_verification_dialog import DouyinVerificationDialog

        app = QApplication.instance() or QApplication([sys.argv[0]])
        configure_application(app)
        apply_style(app)
        verification_dialog = DouyinVerificationDialog
    while publish_service.is_task_running(task_id):
        task_service.touch_task_heartbeat(task_id)
        if app is not None:
            app.processEvents()
            request_id = douyin_verification_broker.request_for_task(task_id)
            if request_id:
                verification_dialog(
                    request_id,
                    broker=douyin_verification_broker,
                ).exec()
        time.sleep(0.25)


def run_controlled_publish_cli(args: argparse.Namespace) -> int:
    """本机 CLI：默认预检，标准输出只承诺稳定 JSON 状态。"""

    # 平台执行器沿用桌面日志器；CLI 必须把这些运行日志移到 stderr，
    # 给调用方保留只包含状态 JSON 的 stdout。
    from utils.log import redirect_console_logger

    redirect_console_logger(sys.stderr)
    ensure_schema()
    action = str(args.controlled_publish_action or "")
    try:
        if action in {"metrics-sync", "metrics-get", "metrics-status"}:
            from app_core.content_project_gateway import ContentProjectGateway

            project_id = str(args.content_project_id or "").strip()
            if not project_id:
                raise controlled_publish.ControlledPublishError(
                    "content_project_id_required",
                    "项目数据操作必须提供 content project id",
                )
            gateway = ContentProjectGateway()
            if action == "metrics-sync":
                result = gateway.sync_project_metrics(project_id)
            elif action == "metrics-get":
                result = gateway.get_project_metrics(
                    project_id, int(args.metrics_days)
                )
            else:
                result = gateway.metrics_sync_status(project_id)
            _controlled_json(result)
            return 0
        if action == "status":
            if not args.controlled_publish_task_id:
                raise controlled_publish.ControlledPublishError(
                    "controlled_task_id_required", "查询任务必须提供 taskId"
                )
            _controlled_json(
                controlled_publish.task_status(args.controlled_publish_task_id)
            )
            return 0
        if action == "authorize":
            if not args.controlled_publish_task_id:
                raise controlled_publish.ControlledPublishError(
                    "controlled_task_id_required", "创建授权必须提供预检 taskId"
                )
            _controlled_json(
                controlled_publish.authorize_completed_check(
                    args.controlled_publish_task_id
                )
            )
            return 0
        if action == "create":
            if not args.controlled_publish_request:
                raise controlled_publish.ControlledPublishError(
                    "controlled_request_file_required", "创建任务必须提供 JSON 请求文件"
                )
            request = _read_controlled_request(args.controlled_publish_request)
            initial = controlled_publish.submit_request(request)
            _controlled_json(initial)
            task_id = int(initial["taskId"])
            # CLI 进程保持到后台发布线程结束；Codex 可同时用 status 查询 taskId。
            try:
                _wait_for_controlled_task(
                    task_id,
                    interactive_verification=(
                        str(request.get("mode") or "preflight").strip().lower()
                        == "formal"
                    ),
                )
            except KeyboardInterrupt:
                task_service.fail_active_task(
                    task_id,
                    error_code="controlled_cli_interrupted",
                    message="受控发布命令被中断，未取得最终回执的平台已安全停止",
                    event_type="controlled_cli_interrupted",
                )
                final = controlled_publish.task_status(task_id)
                _controlled_json(final)
                return 130
            final = controlled_publish.task_status(task_id)
            if final != initial:
                _controlled_json(final)
            return 0 if final["status"] == "success" else 2
        if action in {"silicon-preflight", "silicon-formal"}:
            if not args.controlled_publish_request:
                raise controlled_publish.ControlledPublishError(
                    "controlled_request_file_required",
                    "硅基进化自动直发必须提供 JSON 请求文件",
                )
            request = _read_controlled_request(args.controlled_publish_request)
            request["mode"] = (
                "preflight" if action == "silicon-preflight" else "formal"
            )
            initial = controlled_publish.submit_silicon_evolution_request(request)
            _controlled_json(initial)
            task_id = int(initial["taskId"])
            try:
                _wait_for_controlled_task(
                    task_id,
                    interactive_verification=(action == "silicon-formal"),
                )
            except KeyboardInterrupt:
                task_service.fail_active_task(
                    task_id,
                    error_code="controlled_cli_interrupted",
                    message="自动直发命令被中断，未取得最终回执的平台已安全停止",
                    event_type="controlled_cli_interrupted",
                )
                _controlled_json(controlled_publish.task_status(task_id))
                return 130
            final = controlled_publish.task_status(task_id)
            if final != initial:
                _controlled_json(final)
            return 0 if final["status"] == "success" else 2
        raise controlled_publish.ControlledPublishError(
            "controlled_action_invalid",
            "受控 action 不受支持",
        )
    except controlled_publish.ControlledPublishError as exc:
        _controlled_json(
            {
                "status": "rejected",
                "errorCode": exc.error_code,
                "errorText": exc.public_message,
            }
        )
        return 2
    except Exception as exc:
        _controlled_json(
            {
                "status": "failed",
                "errorCode": str(
                    getattr(exc, "error_code", "controlled_internal_error")
                ),
                "errorText": str(
                    getattr(exc, "public_message", f"{type(exc).__name__}：{exc}")
                ),
            }
        )
        return 2
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
    parser.add_argument(
        "--controlled-publish-action",
        choices=(
            "create",
            "status",
            "authorize",
            "silicon-preflight",
            "silicon-formal",
            "metrics-sync",
            "metrics-get",
            "metrics-status",
        ),
        help="本机受控发布接口；默认只能由请求中的 preflight 模式启动预检。",
    )
    parser.add_argument(
        "--controlled-publish-request",
        metavar="JSON_OR_STDIN",
        help="受控发布请求 JSON 文件；使用 - 从标准输入读取。",
    )
    parser.add_argument(
        "--controlled-publish-task-id",
        type=int,
        metavar="TASK_ID",
    )
    parser.add_argument(
        "--content-project-id",
        metavar="PROJECT_ID",
        help="内容项目数据同步或查询使用的本机项目标识。",
    )
    parser.add_argument(
        "--metrics-days",
        type=int,
        choices=(1, 7, 30),
        default=1,
        help="项目数据查询窗口，只允许 1、7、30 天。",
    )
    parser.add_argument(
        "--mcp-server",
        action="store_true",
        help="通过本机 stdio 启动一键发 MCP 受控发布适配器。",
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
    if args.controlled_publish_action:
        return run_controlled_publish_cli(args)
    if args.mcp_server:
        # MCP 的 stdout 是协议信道；平台执行日志只能进入 stderr。
        from utils.log import redirect_console_logger
        from app_core.oneclick_mcp_server import run_stdio_server

        redirect_console_logger(sys.stderr)
        ensure_schema()
        run_stdio_server()
        return 0
    source_live_conflict = installed_gui_block_reason(
        os.environ, USER_DATA_DIR / "source-live-session.json"
    )
    if source_live_conflict:
        app = QApplication(sys.argv)
        configure_application(app)
        QMessageBox.warning(
            None,
            "正式客户端暂不能打开",
            source_live_conflict + "，再重新打开正式客户端。",
        )
        return 2
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
    window = create_main_window()
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
