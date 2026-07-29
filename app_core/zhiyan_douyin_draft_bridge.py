# -*- coding: utf-8 -*-
"""知言抖音预发布桥：独立登录、填表、选平台话题并只保存草稿。"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from playwright.async_api import async_playwright

from app_core.zhiyan_release_importer import load_zhiyan_release_payload
from uploader.douyin_uploader.main import DouYinVideo
from utils.base_social_media import (
    goto_and_reveal,
    launch_publish_browser,
    new_publish_context,
    save_context_storage_state,
    set_init_script,
)


DOUYIN_HOME_URL = "https://creator.douyin.com/"
LOGIN_TEXTS = ("手机号登录", "扫码登录", "验证码登录")


class ZhiyanDraftBridgeError(RuntimeError):
    """知言草稿桥未能安全完成。"""


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_bridge_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """再次校验导入载荷，避免 CLI 层错误透传到发布运行时。"""
    if payload.get("type") != 3 or payload.get("platform") != "抖音":
        raise ZhiyanDraftBridgeError("预发布桥只接受抖音载荷")
    if payload.get("saveDraftOnly") is not True:
        raise ZhiyanDraftBridgeError("预发布桥要求 saveDraftOnly=true")
    if payload.get("publishAllowed") is not False:
        raise ZhiyanDraftBridgeError("预发布桥要求 publishAllowed=false")
    if payload.get("debugDryRun") is not True:
        raise ZhiyanDraftBridgeError("导入载荷缺少预发布安全声明 debugDryRun=true")

    paths = {}
    for key in ("videoPath", "verticalCoverPath", "horizontalCoverPath"):
        value = payload.get(key)
        path = Path(str(value or ""))
        if not path.is_absolute() or not path.is_file():
            raise ZhiyanDraftBridgeError(f"{key} 必须是存在的绝对文件路径")
        paths[key] = str(path.resolve())

    tags = payload.get("tags")
    if not isinstance(tags, list) or not 1 <= len(tags) <= 5:
        raise ZhiyanDraftBridgeError("抖音平台话题必须为 1-5 个")
    normalized_tags = []
    for tag in tags:
        value = str(tag or "").strip().lstrip("#")
        if not value or "#" in value:
            raise ZhiyanDraftBridgeError("抖音平台话题格式错误")
        normalized_tags.append(value)
    if len(set(normalized_tags)) != len(normalized_tags):
        raise ZhiyanDraftBridgeError("抖音平台话题不得重复")

    title = str(payload.get("title") or "").strip()
    detail = str(payload.get("detail") or "")
    if not title or not detail.strip():
        raise ZhiyanDraftBridgeError("抖音标题和详情不能为空")
    if "#" in detail:
        raise ZhiyanDraftBridgeError("抖音详情不能包含手写 # 文本")

    checked = dict(payload)
    checked.update(paths)
    checked["tags"] = normalized_tags
    return checked


async def _visible_exact_text(page, value: str) -> bool:
    locator = page.get_by_text(value, exact=True)
    for index in range(await locator.count()):
        try:
            if await locator.nth(index).is_visible():
                return True
        except Exception:
            continue
    return False


async def _read_account_status(page, expected_name: str, expected_id: str | None) -> dict[str, Any]:
    try:
        body_text = await page.locator("body").inner_text(timeout=5000)
    except Exception:
        body_text = ""
    login_visible = any([await _visible_exact_text(page, value) for value in LOGIN_TEXTS])
    name_visible = await _visible_exact_text(page, expected_name)
    name_confirmed = name_visible or expected_name in body_text
    id_confirmed = bool(expected_id and expected_id in body_text)
    return {
        "expected_name": expected_name,
        "expected_id": expected_id,
        "name_confirmed": name_confirmed,
        "id_confirmed": id_confirmed,
        "login_visible": login_visible,
        "url": str(page.url),
    }


def account_status_is_allowed(status: Mapping[str, Any], expected_id: str | None) -> bool:
    if status.get("login_visible") or not status.get("name_confirmed"):
        return False
    if expected_id and not status.get("id_confirmed"):
        return False
    return True


async def wait_for_expected_account(
    page,
    expected_name: str,
    expected_id: str | None,
    timeout_seconds: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_status = None
    while time.monotonic() < deadline:
        last_status = await _read_account_status(page, expected_name, expected_id)
        if account_status_is_allowed(last_status, expected_id):
            return last_status
        await page.wait_for_timeout(1000)

    raise ZhiyanDraftBridgeError(
        "未在规定时间内确认知言抖音账号。请在打开的独立浏览器中完成扫码登录；"
        f"最后状态={last_status}"
    )


def _base_evidence(payload: Mapping[str, Any], account_state: Path) -> dict[str, Any]:
    media = {}
    for key in ("videoPath", "verticalCoverPath", "horizontalCoverPath"):
        path = Path(str(payload[key]))
        media[key] = {
            "path": str(path),
            "sha256": _sha256(path),
            "size": path.stat().st_size,
        }
    return {
        "schemaVersion": "zhiyan-douyin-draft-evidence/v1",
        "topic": payload.get("topic"),
        "sourceConfigPath": payload.get("sourceConfigPath"),
        "accountStatePath": str(account_state),
        "safety": {
            "runtimeMode": "save_draft_only",
            "formalPublishAllowed": False,
            "publishControlExposed": False,
            "importDryRunDeclaration": True,
        },
        "content": {
            "title": payload.get("title"),
            "detail": payload.get("detail"),
            "topics": list(payload.get("tags") or []),
            "topicEntryMethod": "platform_candidate_selection",
        },
        "media": media,
    }


def write_evidence(path: Path, data: Mapping[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


async def run_zhiyan_douyin_draft_bridge(
    config_path: Path,
    account_state: Path,
    evidence_path: Path,
    *,
    expected_account_name: str = "知言",
    expected_account_id: str | None = "LGZX88888",
    login_timeout_seconds: int = 600,
    login_only: bool = False,
    verify_existing_draft: bool = False,
) -> dict[str, Any]:
    expected_account_name = str(expected_account_name or "").strip()
    expected_account_id = str(expected_account_id or "").strip()
    if not expected_account_name or not expected_account_id:
        raise ZhiyanDraftBridgeError("知言草稿桥必须同时提供非空账号名称和抖音号")
    if login_only and verify_existing_draft:
        raise ZhiyanDraftBridgeError("--login-only 与 --verify-existing-draft 不能同时使用")
    payload = validate_bridge_payload(load_zhiyan_release_payload(config_path))
    account_state = account_state.expanduser().resolve()
    evidence_path = evidence_path.expanduser().resolve()
    cookies_root = (Path(__file__).resolve().parents[1] / "cookiesFile").resolve()
    if account_state.suffix.lower() != ".json" or cookies_root not in account_state.parents:
        raise ZhiyanDraftBridgeError(
            f"知言独立登录态必须是 cookiesFile 目录内的 .json 文件：{account_state}"
        )
    protected_paths = {
        Path(str(payload["sourceConfigPath"])).resolve(),
        Path(str(payload["videoPath"])).resolve(),
        Path(str(payload["verticalCoverPath"])).resolve(),
        Path(str(payload["horizontalCoverPath"])).resolve(),
        account_state,
    }
    if evidence_path in protected_paths:
        raise ZhiyanDraftBridgeError("草稿证据路径不能与配置、素材或登录态文件相同")
    evidence = _base_evidence(payload, account_state)
    evidence["startedAt"] = _now_iso()

    async with async_playwright() as playwright:
        browser = await launch_publish_browser(playwright)
        context = None
        page = None
        try:
            storage_state = str(account_state) if account_state.is_file() else None
            context = await new_publish_context(browser, storage_state=storage_state)
            context = await set_init_script(context)
            page = await context.new_page()
            await goto_and_reveal(page, DOUYIN_HOME_URL, timeout=30000)
            account = await wait_for_expected_account(
                page,
                expected_account_name,
                expected_account_id,
                login_timeout_seconds,
            )
            evidence["account"] = account
            await save_context_storage_state(context, account_state)

            if login_only:
                evidence["status"] = "login_state_ready"
                evidence["completedAt"] = _now_iso()
                write_evidence(evidence_path, evidence)
                return evidence

            app = DouYinVideo(
                title=payload["title"],
                description=payload["detail"],
                file_path=payload["videoPath"],
                tags=payload["tags"],
                publish_date=0,
                account_file=str(account_state),
                thumbnail_path=payload["verticalCoverPath"],
                thumbnail_paths={
                    "3:4": payload["verticalCoverPath"],
                    "4:3": payload["horizontalCoverPath"],
                },
                dry_run=False,
                save_draft_only=True,
            )
            app.external_page = page
            app.external_context = context
            app.external_browser = browser
            if verify_existing_draft:
                evidence["safety"]["operation"] = "verify_existing_draft_only"
                await goto_and_reveal(
                    page,
                    "https://creator.douyin.com/creator-micro/content/post/video?enter_from=publish_page",
                    timeout=30000,
                )
                await page.wait_for_timeout(1200)
                draft_result = await app.verify_existing_draft(page)
            else:
                draft_result = await app.upload(playwright)
            if not isinstance(draft_result, dict) or draft_result.get("status") != "draft_saved":
                raise ZhiyanDraftBridgeError("草稿运行时未返回可验证的 draft_saved 证据")
            await save_context_storage_state(context, account_state, include_indexed_db=True)

            evidence["status"] = "draft_saved_not_published"
            evidence["draft"] = draft_result
            evidence["completedAt"] = _now_iso()
            write_evidence(evidence_path, evidence)
            return evidence
        except Exception as exc:
            evidence["status"] = "blocked_or_failed"
            evidence["error"] = str(exc)
            if page is not None:
                failure_screenshot = evidence_path.with_name(
                    f"{evidence_path.stem}_失败截图.png"
                )
                try:
                    await page.screenshot(path=str(failure_screenshot), full_page=True)
                    evidence["failureScreenshot"] = str(failure_screenshot)
                except Exception:
                    pass
            if context is not None and evidence.get("account"):
                try:
                    await save_context_storage_state(
                        context,
                        account_state,
                        include_indexed_db=True,
                    )
                    evidence["sessionStatePreservedAfterFailure"] = True
                except Exception:
                    evidence["sessionStatePreservedAfterFailure"] = False
            evidence["completedAt"] = _now_iso()
            write_evidence(evidence_path, evidence)
            if isinstance(exc, ZhiyanDraftBridgeError):
                raise
            raise ZhiyanDraftBridgeError(str(exc)) from exc
        finally:
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass
            try:
                await browser.close()
            except Exception:
                pass
