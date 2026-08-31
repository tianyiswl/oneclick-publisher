# -*- coding: utf-8 -*-
"""Instagram/Facebook 浏览器正式发布的受控入口。

该通道复用恢复的 Meta Business Suite 执行器，但必须在一键发
桌面端完成两个独立确认，并且只在 Meta 返回明确成功证据时
才记为成功。验证码、二次验证、风控或未知页面状态会在可见
浏览器中安全停止。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright

from myUtils.postVideo import post_video_instagram
from utils.base_social_media import (
    launch_publish_browser,
    new_publish_context,
    set_init_script,
)
from utils.publish_observer import publish_context, publish_event

from uploader.meta_uploader.content_list import (
    FacebookPageContentBaseline,
    FacebookPageContentReader,
)
from uploader.meta_uploader.main import FACEBOOK_PAGE_HOME_URL, MetaReelVideo
from uploader.meta_uploader.page_form import (
    FacebookPageFormAdapter,
    FacebookPageFormExpectation,
    FacebookPageFormSnapshot,
)

from . import account_service, database
from .meta_browser_policy import (
    META_BROWSER_AUTOMATION_ACKNOWLEDGED,
    META_BROWSER_PLATFORM_TYPES,
    META_BROWSER_PUBLISH_CONFIRMED,
    browser_publish_confirmation_valid,
)
from .overseas_meta_errors import (
    FacebookPagePublishError,
    project_facebook_page_receipt,
)
from .overseas_meta_content import (
    canonical_facebook_page_caption,
    facebook_page_caption_sha256,
)
from .overseas_meta_page_identity import facebook_page_v1_enabled
from .paths import COOKIE_DIR
from .wechat_publish_policy import local_timezone_name


PLATFORM_NAMES = {8: "Instagram Reels", 9: "Facebook Reels"}
HANDLERS = {8: post_video_instagram}
_FACEBOOK_PAGE_CONTEXT_TIMEZONE = "Asia/Shanghai"


class OverseasBrowserPublishError(RuntimeError):
    """Meta 浏览器发布无法在受控边界内继续。"""


_LEGACY_META_CONFIRMATION_KEYS = frozenset(
    {
        META_BROWSER_PUBLISH_CONFIRMED,
        META_BROWSER_AUTOMATION_ACKNOWLEDGED,
        "overseasVideoPublishConfirmed",
    }
)

def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _hash_json(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _stream_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise FacebookPagePublishError(
            "facebook_video_file_invalid",
            "Facebook Page 视频素材无法安全读取。",
        ) from exc
    return digest.hexdigest()


def _facebook_validation_error(
    message: str,
    *,
    page_id: object = "",
    error_code: str = "facebook_publish_authorization_invalid",
) -> FacebookPagePublishError:
    receipt: dict[str, object] = {
        "phase": "local_validation",
        "platformWriteOccurred": False,
        "finalActionTriggered": False,
    }
    candidate = str(page_id or "").strip()
    if candidate.isascii() and candidate.isdigit():
        receipt["pageId"] = candidate
    return FacebookPagePublishError(error_code, message, receipt=receipt)


def _validate_facebook_page_payload(
    payload: Mapping[str, Any],
    *,
    mode: str,
) -> dict[str, Any]:
    """Freeze one local Page target without trusting caller confirmation flags."""

    sanitized = {
        key: value
        for key, value in dict(payload).items()
        if key not in _LEGACY_META_CONFIRMATION_KEYS
    }
    page_id = str(sanitized.get("facebookExpectedPageReference") or "").strip()
    expected_runtime = "preflight" if mode == "preflight" else "publish"
    expected_dry_run = mode == "preflight"
    from .controlled_publish import (
        ControlledPublishError,
        is_exact_facebook_page_platform_type,
        validate_facebook_page_v1_metadata,
    )

    if (
        not facebook_page_v1_enabled()
        or not is_exact_facebook_page_platform_type(sanitized)
        or str(sanitized.get("contentType") or "") != "video"
        or str(sanitized.get("runtimeMode") or "") != expected_runtime
        or sanitized.get("debugDryRun") is not expected_dry_run
        or sanitized.get("facebookControlledPublish") is not True
        or not page_id.isascii()
        or not page_id.isdigit()
    ):
        raise _facebook_validation_error(
            "Facebook Page 受控任务的运行模式或目标字段无效。",
            page_id=page_id,
        )

    try:
        validate_facebook_page_v1_metadata(sanitized)
    except ControlledPublishError as exc:
        raise _facebook_validation_error(
            exc.public_message,
            page_id=page_id,
            error_code=exc.error_code,
        ) from exc

    account_ids = sanitized.get("accountIds")
    account_files = sanitized.get("accountList")
    if (
        not isinstance(account_ids, list)
        or len(account_ids) != 1
        or type(account_ids[0]) is not int
        or account_ids[0] <= 0
        or not isinstance(account_files, list)
        or len(account_files) != 1
        or type(account_files[0]) is not str
        or not account_files[0].strip()
        or Path(account_files[0]).name != account_files[0]
    ):
        raise _facebook_validation_error(
            "Facebook Page 受控任务必须精确绑定一个已保存账号。",
            page_id=page_id,
        )
    account_id = int(account_ids[0])
    account_file = str(account_files[0])
    accounts = [
        row
        for row in account_service.list_accounts()
        if int(row.get("id") or 0) == account_id
    ]
    if len(accounts) != 1:
        raise _facebook_validation_error(
            "Facebook Page 已保存账号不存在或不可用。",
            page_id=page_id,
        )
    account = accounts[0]
    try:
        saved_page_id = account_service.validate_saved_facebook_page_account(account)
    except FacebookPagePublishError:
        raise
    if (
        saved_page_id != page_id
        or Path(str(account.get("filePath") or "")).name != account_file
        or not (COOKIE_DIR / account_file).is_file()
    ):
        raise _facebook_validation_error(
            "Facebook Page 已保存主体或会话与任务不一致。",
            page_id=page_id,
            error_code="facebook_page_identity_mismatch",
        )

    files = sanitized.get("fileList")
    if (
        not isinstance(files, list)
        or len(files) != 1
        or type(files[0]) is not str
    ):
        raise _facebook_validation_error(
            "Facebook Page 每个受控任务必须精确包含一个 Reel 视频。",
            page_id=page_id,
        )
    video_path = Path(files[0])
    try:
        video_size = video_path.stat().st_size
    except OSError:
        video_size = -1
    if not video_path.is_absolute() or not video_path.is_file() or video_size < 0:
        raise _facebook_validation_error(
            "Facebook Page Reel 视频不存在。",
            page_id=page_id,
            error_code="facebook_video_file_invalid",
        )
    declared_video_size = sanitized.get("facebookVideoSize")
    if declared_video_size is not None and (
        type(declared_video_size) is not int
        or declared_video_size < 0
        or declared_video_size != video_size
    ):
        raise _facebook_validation_error(
            "Facebook Page Reel 视频大小与授权快照不一致。",
            page_id=page_id,
            error_code="facebook_video_file_invalid",
        )
    sanitized["facebookVideoSize"] = video_size
    video_hash = str(sanitized.get("facebookVideoSha256") or "")
    if len(video_hash) != 64 or _stream_sha256(video_path) != video_hash:
        raise _facebook_validation_error(
            "Facebook Page Reel 视频哈希与授权快照不一致。",
            page_id=page_id,
            error_code="facebook_video_file_invalid",
        )
    caption = canonical_facebook_page_caption(
        sanitized.get("facebookFinalCaption")
    )
    caption_hash = facebook_page_caption_sha256(caption)
    if (
        not caption
        or str(sanitized.get("facebookCaptionSha256") or "") != caption_hash
    ):
        raise _facebook_validation_error(
            "Facebook Page Reel 文案与授权快照不一致。",
            page_id=page_id,
        )

    expectation = FacebookPageFormExpectation(
        page_id=page_id,
        content_kind="reel",
        video_name=video_path.name,
        video_size=video_size,
        video_sha256=video_hash,
        caption=caption,
        visibility="public",
        video_path=str(video_path),
    )
    return {
        "payload": sanitized,
        "accountId": account_id,
        "accountFile": account_file,
        "pageId": page_id,
        "videoPath": video_path,
        "expectation": expectation,
    }


@asynccontextmanager
async def _facebook_page_session(
    prepared: Mapping[str, Any],
    *,
    progress: Callable[[str, Mapping[str, object]], None] | None = None,
):
    """Open one isolated visible session shared by Page preflight and formal."""

    browser = None
    context = None
    async with async_playwright() as playwright:
        try:
            browser = await launch_publish_browser(playwright)
            context = await new_publish_context(
                browser,
                storage_state=str(COOKIE_DIR / str(prepared["accountFile"])),
                timezone_id=_FACEBOOK_PAGE_CONTEXT_TIMEZONE,
            )
            await set_init_script(context)
            page = await context.new_page()
            verifier_owner = MetaReelVideo(
                "",
                str(prepared["videoPath"]),
                [],
                str(prepared["accountFile"]),
                target_platform="facebook",
                dry_run=True,
                dry_run_hold_browser=False,
                facebook_expected_page_id=str(prepared["pageId"]),
                facebook_video_sha256=str(
                    prepared["expectation"].video_sha256
                ),
                facebook_final_caption=str(prepared["expectation"].caption),
                execution_progress=progress,
            )
            await page.goto(FACEBOOK_PAGE_HOME_URL, wait_until="domcontentloaded")
            await verifier_owner._wait_for_manual_intervention(page)
            yield context, page, verifier_owner._wait_for_manual_intervention
        finally:
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    await browser.close()
                except Exception:
                    pass


def _form_snapshot_projection(
    expected: FacebookPageFormExpectation,
    snapshot: FacebookPageFormSnapshot,
) -> dict[str, object]:
    video_name = Path(str(snapshot.video_name)).name
    return {
        "pageId": str(snapshot.page_id),
        "contentKind": str(snapshot.content_kind),
        "videoName": video_name,
        "videoCount": int(snapshot.video_count),
        "videoSize": int(expected.video_size),
        "videoSha256": str(expected.video_sha256),
        "captionSha256": facebook_page_caption_sha256(snapshot.caption),
        "visibility": str(snapshot.visibility),
        "finalButtonLabel": str(snapshot.final_action_label),
        "finalButtonReady": bool(snapshot.final_action_ready),
    }


def _public_form_receipt(
    prepared: Mapping[str, Any],
    snapshot: FacebookPageFormSnapshot,
    *,
    phase: str,
    final_action_triggered: bool,
) -> dict[str, object]:
    from .controlled_publish import facebook_form_snapshot_hash

    expected = prepared["expectation"]
    projection = _form_snapshot_projection(expected, snapshot)
    receipt: dict[str, object] = {
        "accountId": int(prepared["accountId"]),
        "pageId": projection["pageId"],
        "videoName": projection["videoName"],
        "videoSize": projection["videoSize"],
        "videoSha256": projection["videoSha256"],
        "captionSha256": projection["captionSha256"],
        "visibility": projection["visibility"],
        "phase": phase,
        "platformWriteOccurred": True,
        "finalActionTriggered": bool(final_action_triggered),
        "finalButtonEnabled": bool(projection["finalButtonReady"]),
    }
    receipt["formSnapshotHash"] = facebook_form_snapshot_hash(receipt)
    return project_facebook_page_receipt(receipt)


async def _facebook_page_preflight_async(
    payload: Mapping[str, Any],
    *,
    progress: Callable[[str, Mapping[str, object]], None] | None = None,
) -> dict[str, Any]:
    try:
        prepared = _validate_facebook_page_payload(payload, mode="preflight")
        async with _facebook_page_session(prepared, progress=progress) as (
            context,
            page,
            verifier,
        ):
            adapter = FacebookPageFormAdapter(page, wait_for_verification=verifier)
            snapshot = await adapter.fill_and_readback(prepared["expectation"])
            receipt = _public_form_receipt(
                prepared,
                snapshot,
                phase="platform_form_verified",
                final_action_triggered=False,
            )
            publish_event(
                "facebook_platform_form_verified",
                "Facebook Page Reel 表单已逐字段回读，未点击最终发布按钮",
            )
            return {
                "type": 9,
                "ok": True,
                "phase": "platform_form_verified",
                "message": "Facebook Page Reel 表单已核对，停在最终发布按钮前。",
                "receipt": receipt,
            }
    except FacebookPagePublishError as exc:
        raise FacebookPagePublishError(
            exc.error_code,
            "Facebook Page Reel 预检未能完成，已停止。",
            receipt=exc.receipt,
            outcome_ambiguous=exc.outcome_ambiguous,
        ) from exc
    except Exception as exc:
        raise FacebookPagePublishError(
            "facebook_page_preflight_failed",
            "Facebook Page Reel 预检未能完成，已停止。",
        ) from exc


def _run_facebook_page_preflight_form_sync(
    payload: Mapping[str, Any],
    *,
    progress: Callable[[str, Mapping[str, object]], None] | None = None,
) -> dict[str, Any]:
    return asyncio.run(_facebook_page_preflight_async(payload, progress=progress))


def _authorization_error() -> FacebookPagePublishError:
    return FacebookPagePublishError(
        "facebook_publish_authorization_invalid",
        "Facebook Page 正式发布授权无效，必须重新完成平台预检。",
    )


def _load_authorized_preflight_receipt(
    task_id: int,
    payload: Mapping[str, Any],
) -> dict[str, object]:
    """Re-read the exact authorized preflight receipt and its claim hash."""

    from .controlled_publish import facebook_preflight_receipt_hash

    with database.connect() as conn:
        # Python's sqlite3 driver does not open a transaction for SELECTs.
        # Begin one explicitly so the claim hash and the returned safe receipt
        # are derived from the same immutable database snapshot (including WAL).
        conn.execute("BEGIN")
        claim = conn.execute(
            """
            SELECT preflightTaskId, preflightReceiptHash, state, workerStartedAt
            FROM facebook_page_publish_claims WHERE taskId = ?
            """,
            (int(task_id),),
        ).fetchone()
        if (
            claim is None
            or str(claim["state"] or "") != "reserved"
            or not str(claim["workerStartedAt"] or "")
        ):
            raise _authorization_error()
        try:
            actual_hash = facebook_preflight_receipt_hash(
                conn,
                int(claim["preflightTaskId"]),
                [payload],
            )
        except (TypeError, ValueError) as exc:
            raise _authorization_error() from exc
        if actual_hash != str(claim["preflightReceiptHash"] or ""):
            raise _authorization_error()
        rows = conn.execute(
            """
            SELECT receiptJson FROM publish_task_items
            WHERE taskId = ? AND platformType = 9 AND status = 'success'
            ORDER BY id
            """,
            (int(claim["preflightTaskId"]),),
        ).fetchall()
        if len(rows) != 1:
            raise _authorization_error()
        try:
            receipt = project_facebook_page_receipt(
                json.loads(str(rows[0]["receiptJson"] or ""))
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise _authorization_error() from exc
    return receipt


def _assert_authorized_form_snapshot(
    prepared: Mapping[str, Any],
    snapshot: FacebookPageFormSnapshot,
    authorized_receipt: Mapping[str, object],
) -> None:
    current = _public_form_receipt(
        prepared,
        snapshot,
        phase="platform_form_verified",
        final_action_triggered=False,
    )
    stable_keys = (
        "accountId",
        "pageId",
        "videoName",
        "videoSize",
        "videoSha256",
        "captionSha256",
        "visibility",
        "finalButtonEnabled",
        "formSnapshotHash",
    )
    if (
        str(snapshot.content_kind) != "reel"
        or int(snapshot.video_count) != 1
        or authorized_receipt.get("phase") != "platform_form_verified"
        or authorized_receipt.get("platformWriteOccurred") is not True
        or authorized_receipt.get("finalActionTriggered") is not False
        or any(current.get(key) != authorized_receipt.get(key) for key in stable_keys)
    ):
        raise FacebookPagePublishError(
            "facebook_page_form_readback_failed",
            "Facebook Page 正式表单与授权预检快照不一致，已停止。",
            receipt={**current, "phase": "form_readback"},
        )


def _baseline_projection(
    baseline: FacebookPageContentBaseline,
) -> dict[str, object]:
    return {
        "pageId": baseline.page_id,
        "rows": [
            {
                "reelId": row.reel_id,
                "url": row.url,
                "publishedAt": row.published_at,
                "captionSha256": row.caption_sha256,
            }
            for row in baseline.rows
        ],
    }


def _claim_form_snapshot(
    prepared: Mapping[str, Any],
    snapshot: FacebookPageFormSnapshot,
) -> dict[str, object]:
    expected = prepared["expectation"]
    return {
        "pageId": snapshot.page_id,
        "videoName": Path(str(snapshot.video_name)).name,
        "videoSize": expected.video_size,
        "videoSha256": expected.video_sha256,
        "captionSha256": facebook_page_caption_sha256(snapshot.caption),
        "visibility": snapshot.visibility,
        "finalButtonLabel": snapshot.final_action_label,
        "finalButtonReady": snapshot.final_action_ready,
    }


def _load_clicked_at(task_id: int) -> str:
    with database.connect() as conn:
        row = conn.execute(
            """
            SELECT clickedAt FROM facebook_page_publish_claims WHERE taskId = ?
            """,
            (int(task_id),),
        ).fetchone()
    clicked_at = str(row["clickedAt"] or "") if row is not None else ""
    if not clicked_at:
        raise FacebookPagePublishError(
            "facebook_claim_lifecycle_invalid",
            "Facebook Page 点击时间未持久化，结果按未知处理。",
            outcome_ambiguous=True,
        )
    return clicked_at


def _outcome_receipt(
    prepared: Mapping[str, Any],
    snapshot: FacebookPageFormSnapshot,
    baseline: FacebookPageContentBaseline,
    *,
    post_click_state: str | None = None,
) -> dict[str, object]:
    receipt = _public_form_receipt(
        prepared,
        snapshot,
        phase="ambiguous",
        final_action_triggered=True,
    )
    receipt["baselineHash"] = _hash_json(_baseline_projection(baseline))
    receipt["formSnapshotHash"] = _hash_json(
        _claim_form_snapshot(prepared, snapshot)
    )
    if post_click_state in {
        "confirmation_pending",
        "composer_unchanged",
        "transitioned_unknown",
        "unknown",
    }:
        receipt["postClickState"] = post_click_state
    return receipt


def _preclick_failure_receipt(
    prepared: Mapping[str, Any],
    snapshot: FacebookPageFormSnapshot,
    baseline: FacebookPageContentBaseline,
) -> dict[str, object]:
    receipt = _public_form_receipt(
        prepared,
        snapshot,
        phase="form_readback",
        final_action_triggered=False,
    )
    receipt["baselineHash"] = _hash_json(_baseline_projection(baseline))
    receipt["formSnapshotHash"] = _hash_json(
        _claim_form_snapshot(prepared, snapshot)
    )
    return receipt


def _assert_final_form_snapshot(
    prepared: Mapping[str, Any],
    before: FacebookPageFormSnapshot,
    after: FacebookPageFormSnapshot,
    baseline: FacebookPageContentBaseline,
) -> None:
    expected = prepared["expectation"]
    if (
        str(after.page_id) != str(expected.page_id)
        or str(after.content_kind) != "reel"
        or Path(str(after.video_name)).name != Path(str(expected.video_name)).name
        or int(after.video_count) != 1
        or str(after.caption) != str(expected.caption)
        or str(after.visibility) != "public"
        or str(after.final_action_label) != str(before.final_action_label)
        or after.final_action_ready is not True
    ):
        raise FacebookPagePublishError(
            "facebook_page_form_readback_failed",
            "Facebook Page 最终提交前表单发生变化，已停止。",
            receipt=_preclick_failure_receipt(prepared, before, baseline),
        )


async def _facebook_page_formal_async(
    payload: Mapping[str, Any],
    *,
    task_id: int,
    progress: Callable[[str, Mapping[str, object]], None],
) -> dict[str, Any]:
    prepared = _validate_facebook_page_payload(payload, mode="formal")
    authorized = _load_authorized_preflight_receipt(int(task_id), prepared["payload"])
    async with _facebook_page_session(prepared, progress=progress) as (
        context,
        page,
        verifier,
    ):
        reader = FacebookPageContentReader(
            context,
            wait_for_verification=verifier,
            trusted_display_timezone=ZoneInfo(_FACEBOOK_PAGE_CONTEXT_TIMEZONE),
        )
        baseline = await reader.capture_baseline(str(prepared["pageId"]))
        adapter = FacebookPageFormAdapter(page, wait_for_verification=verifier)
        snapshot = await adapter.fill_and_readback(prepared["expectation"])
        _assert_authorized_form_snapshot(prepared, snapshot, authorized)
        baseline_projection = _baseline_projection(baseline)
        form_projection = _claim_form_snapshot(prepared, snapshot)
        claim_receipt: dict[str, object] = {
            **_public_form_receipt(
                prepared,
                snapshot,
                phase="final_action_claimed",
                final_action_triggered=False,
            ),
            "baseline": baseline_projection,
            "baselineHash": _hash_json(baseline_projection),
            "formSnapshot": form_projection,
            "formSnapshotHash": _hash_json(form_projection),
        }
        final_snapshot, button = await adapter.verify_final_form(
            prepared["expectation"]
        )
        _assert_final_form_snapshot(
            prepared,
            snapshot,
            final_snapshot,
            baseline,
        )
        progress("final_action_claimed", claim_receipt)
        await button.click()
        progress(
            "final_action_clicked",
            {
                **_outcome_receipt(prepared, snapshot, baseline),
                "phase": "final_action_clicked",
            },
        )
        try:
            clicked_at = _load_clicked_at(int(task_id))
        except Exception as exc:
            raise FacebookPagePublishError(
                "facebook_publish_outcome_unknown",
                "Facebook Page 点击后未能读取持久化时间，结果保持未知。",
                receipt=_outcome_receipt(prepared, snapshot, baseline),
                outcome_ambiguous=True,
            ) from exc

        post_click_state = "unknown"
        observe_post_click = getattr(adapter, "observe_post_click_state", None)
        if callable(observe_post_click):
            try:
                observed = str(
                    await observe_post_click(str(prepared["pageId"]))
                    or "unknown"
                )
                if observed in {
                    "confirmation_pending",
                    "composer_unchanged",
                    "transitioned_unknown",
                    "unknown",
                }:
                    post_click_state = observed
            except Exception:
                post_click_state = "unknown"

        decision: object | None = None
        try:
            decision = await reader.read_platform_decision(
                str(prepared["pageId"]),
                page=page,
            )
        except FacebookPagePublishError as exc:
            if exc.error_code in {
                "facebook_verification_required",
                "facebook_verification_timeout",
            }:
                raise FacebookPagePublishError(
                    "facebook_publish_outcome_unknown",
                    "Facebook Page 最终点击后安全验证未完成，发布结果保持未知。",
                    receipt=_outcome_receipt(
                        prepared,
                        snapshot,
                        baseline,
                        post_click_state=post_click_state,
                    ),
                    outcome_ambiguous=True,
                ) from exc
            # A composer transition can make the old page unreadable without
            # proving success or rejection.  Keep the browser session alive
            # and continue with the independent content-list contract.
            pass
        except Exception:
            # The composer can disappear immediately after a successful click.
            # A missing decision is not evidence that the Reel was rejected, so
            # continue with the independent, read-only content-list contract.
            pass
        else:
            progress(
                "platform_decision_observed",
                {
                    "platformDecision": decision,
                    "postClickState": post_click_state,
                },
            )

        try:
            match = await reader.readback_unique_reel(
                baseline=baseline,
                expected_page_id=str(prepared["pageId"]),
                expected_caption_sha256=str(
                    prepared["payload"]["facebookCaptionSha256"]
                ),
                clicked_at=clicked_at,
            )
        except Exception as exc:
            raise FacebookPagePublishError(
                "facebook_publish_outcome_unknown",
                "Facebook Page 点击后内容列表读取失败，结果保持未知。",
                receipt=_outcome_receipt(
                    prepared,
                    snapshot,
                    baseline,
                    post_click_state=post_click_state,
                ),
                outcome_ambiguous=True,
            ) from exc
        if match.status == "unique" and match.receipt is not None:
            progress(
                "readback_unique",
                {
                    "reelMatch": match,
                    "platformDecision": decision,
                    "postClickState": post_click_state,
                },
            )
            receipt = project_facebook_page_receipt(
                {
                    **_outcome_receipt(prepared, snapshot, baseline),
                    "phase": "published_readback_confirmed",
                    "reelId": match.receipt.reel_id,
                    "url": match.receipt.url,
                    "publishedAt": match.receipt.published_at,
                }
            )
            return {
                "ok": True,
                "phase": "published_readback_confirmed",
                "message": "Facebook Page 新 Reel 已通过同页内容列表唯一回读。",
                "receipt": receipt,
            }

        stage = "readback_mismatch" if match.status == "mismatch" else "readback_none"
        progress(
            stage,
            {
                "platformDecision": decision,
                "postClickState": post_click_state,
            },
        )
        if match.status == "mismatch":
            error_code = "facebook_publish_readback_mismatch"
        else:
            error_code = {
                "confirmation_pending": "facebook_post_click_confirmation_pending",
                "composer_unchanged": "facebook_post_click_composer_unchanged",
                "transitioned_unknown": "facebook_post_click_transition_unknown",
            }.get(post_click_state, "facebook_publish_outcome_unknown")
        message = (
            "Facebook Page 出现新 Reel，但主体或文案哈希不匹配。"
            if match.status == "mismatch"
            else "Facebook Page 点击后未唯一回读目标 Reel。"
        )
        raise FacebookPagePublishError(
            error_code,
            message,
            receipt=_outcome_receipt(
                prepared,
                snapshot,
                baseline,
                post_click_state=post_click_state,
            ),
            outcome_ambiguous=True,
        )


def run_facebook_page_publish_sync(
    payload: dict[str, Any],
    *,
    task_id: int,
    progress: Callable[[str, Mapping[str, object]], None],
) -> dict[str, Any]:
    """Run one claim-owned Page worker; never retry the final click."""

    if type(task_id) is not int or task_id <= 0 or not callable(progress):
        raise _authorization_error()
    with publish_context(
        task_id=task_id,
        platform_type=9,
        mode="publish",
        background_mode=False,
    ):
        return asyncio.run(
            _facebook_page_formal_async(
                payload,
                task_id=task_id,
                progress=progress,
            )
        )


def _schedule_time(payload: dict[str, Any]) -> str | None:
    value = str(payload.get("scheduleTime") or "").strip()
    enabled = bool(payload.get("enableTimer"))
    if not enabled:
        return None
    if not value:
        raise ValueError("已开启 Meta 定时发布，但未设置发布时间")
    timezone_name = str(payload.get("scheduleTimezone") or "").strip()
    if timezone_name and timezone_name != local_timezone_name():
        raise ValueError(
            "Meta 浏览器定时只能使用当前系统时区，请重新选择时间"
        )
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise ValueError("Meta 定时时间必须为 YYYY-MM-DD HH:MM") from exc
    if parsed <= datetime.now():
        raise ValueError("Meta 定时发布必须晚于当前时间")
    return value


def validate_meta_browser_publish_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """只校验本地文件、会话和一次性确认，不启动浏览器。"""

    raw_platform_type = payload.get("type")
    platform_type = raw_platform_type if type(raw_platform_type) is int else 0
    errors: list[str] = []
    if platform_type not in META_BROWSER_PLATFORM_TYPES:
        errors.append("当前载荷不是 Meta 浏览器发布目标")
    if str(payload.get("contentType") or "") != "video":
        errors.append("Meta 浏览器通道当前只支持 Reels 视频")
    if str(payload.get("runtimeMode") or "") != "publish":
        errors.append("Meta 浏览器正式通道必须使用 publish 模式")
    if payload.get("debugDryRun") is not False:
        errors.append("Meta 正式发布必须明确 debugDryRun=false")
    if not browser_publish_confirmation_valid(payload):
        errors.append("缺少 Meta 浏览器正式发布的两项独立确认")
    account_files = [
        Path(str(item)).name
        for item in payload.get("accountList") or []
        if str(item).strip()
    ]
    if len(account_files) != 1:
        errors.append("Meta 浏览器正式发布每次必须精确选择一个账号")
    elif not (COOKIE_DIR / account_files[0]).is_file():
        errors.append("一键发 Meta 本地会话不存在，请重新登录")
    files = [Path(str(item)) for item in payload.get("fileList") or []]
    if not files:
        errors.append("未选择 Meta Reels 视频")
    elif any(not item.is_file() for item in files):
        errors.append("Meta Reels 视频文件不存在")
    if not str(payload.get("title") or "").strip():
        errors.append("Meta Reels 标题不能为空")
    if str(payload.get("visibility") or "public") != "public":
        errors.append("Meta Reels 浏览器正式通道当前只允许公开发布")
    if platform_type == 8 and payload.get("shareToFeed") is False:
        errors.append(
            "Instagram 浏览器正式通道尚无法回读“仅 Reels”，"
            "请保持“同时分享到动态”开启"
        )
    if payload.get("aiGenerated") is True:
        errors.append(
            f"{PLATFORM_NAMES.get(platform_type, 'Meta')} 浏览器正式通道"
            "尚未可靠回读 AI 声明，已安全停止"
        )
    schedule = None
    try:
        schedule = _schedule_time(payload)
    except ValueError as exc:
        errors.append(str(exc))
    return {
        "ok": not errors,
        "errors": errors,
        "platformType": platform_type,
        "accountList": account_files,
        "files": files,
        "scheduleTime": schedule,
    }

def run_meta_browser_publish_sync(payload: dict[str, Any]) -> dict[str, Any]:
    """执行可见 Meta 浏览器发布，必须回读平台成功证据。"""

    from .controlled_publish import is_exact_facebook_page_platform_type

    if is_exact_facebook_page_platform_type(payload):
        raise FacebookPagePublishError(
            "facebook_publish_authorization_invalid",
            "Facebook Page 正式发布只能由数据库 claim 的专用 worker 启动。",
        )

    checked = validate_meta_browser_publish_payload(payload)
    if not checked["ok"]:
        raise OverseasBrowserPublishError("；".join(checked["errors"]))
    platform_type = int(checked["platformType"])
    handler = HANDLERS[platform_type]
    with publish_context(mode="publish", background_mode=False):
        results = handler(
            str(payload.get("title") or ""),
            [str(path) for path in checked["files"]],
            list(payload.get("tags") or []),
            checked["accountList"],
            payload.get("category"),
            bool(checked["scheduleTime"]),
            1,
            [checked["scheduleTime"][-5:]] if checked["scheduleTime"] else [],
            0,
            description=str(payload.get("description") or ""),
            cover_path=payload.get("coverPath"),
            cover_paths=payload.get("coverPaths") or {},
            schedule_time=checked["scheduleTime"],
            jitter_minutes=0,
            dry_run=False,
            dry_run_hold_browser=False,
            publish_confirmed=bool(payload[META_BROWSER_PUBLISH_CONFIRMED]),
            automation_acknowledged=bool(
                payload[META_BROWSER_AUTOMATION_ACKNOWLEDGED]
            ),
            visibility=str(payload.get("visibility") or "public"),
            collection_name=str(payload.get("collectionName") or ""),
            ai_generated=bool(payload.get("aiGenerated", False)),
            share_to_feed=bool(payload.get("shareToFeed", True)),
        )
    normalized = [item for item in (results or []) if isinstance(item, dict)]
    expected_status = "scheduled" if checked["scheduleTime"] else "published"
    verified = bool(normalized) and all(
        item.get("status") == expected_status and item.get("evidence")
        for item in normalized
    )
    if not verified:
        raise OverseasBrowserPublishError(
            f"{PLATFORM_NAMES[platform_type]} 最终按钮已处理，但没有得到可验证的平台成功回执"
        )
    references = [str(item.get("evidence") or "") for item in normalized]
    return {
        "ok": True,
        "platformType": platform_type,
        "platform": PLATFORM_NAMES[platform_type],
        "scheduled": bool(checked["scheduleTime"]),
        "scheduledAt": checked["scheduleTime"],
        "published": not bool(checked["scheduleTime"]),
        "operationReferences": references,
        "message": (
            f"{PLATFORM_NAMES[platform_type]} 已回读定时成功：{checked['scheduleTime']}"
            if checked["scheduleTime"]
            else f"{PLATFORM_NAMES[platform_type]} 已回读公开发布成功"
        ),
    }
