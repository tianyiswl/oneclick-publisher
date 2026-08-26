"""抖音图文矩阵串行执行器。

每个账号只打开一次会话，并在同一会话内完成页面回读、填写和可选提交。
一个账号失败不会阻断后续账号，但任何未取得平台回执的提交都不会记为成功。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import threading
from typing import Any, AsyncContextManager, Callable, Mapping

from . import account_service, task_service
from .douyin_graphic_editor import DouyinGraphicEditor
from .douyin_graphic_matrix_service import effective_item_payload
from .paths import COOKIE_DIR


_pause_requested_task_ids: set[int] = set()
_pause_lock = threading.Lock()


def request_pause(task_id: int) -> bool:
    with _pause_lock:
        normalized = int(task_id)
        if normalized in _pause_requested_task_ids:
            return False
        _pause_requested_task_ids.add(normalized)
        return True


def _pause_requested(task_id: int) -> bool:
    with _pause_lock:
        return int(task_id) in _pause_requested_task_ids


def _clear_pause(task_id: int) -> None:
    with _pause_lock:
        _pause_requested_task_ids.discard(int(task_id))


class DouyinGraphicMatrixExecutorError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = str(error_code)
        self.public_message = str(message)
        super().__init__(message)


def public_error_code(exc: BaseException) -> str:
    return str(
        getattr(exc, "error_code", "")
        or "douyin_graphic_matrix_account_failed"
    )


def public_result(
    target: Mapping[str, Any], status: str, **detail: Any
) -> dict[str, Any]:
    return {
        "itemIndex": int(target.get("itemIndex") or 0),
        "accountId": int(target.get("accountId") or 0),
        "status": str(status),
        "errorCode": str(detail.get("error_code") or ""),
        "errorText": str(detail.get("error_text") or ""),
        "receipt": (
            dict(detail["receipt"])
            if isinstance(detail.get("receipt"), Mapping)
            else None
        ),
    }


def _matrix_account(account_id: int) -> dict[str, Any]:
    matches = [
        row
        for row in account_service.list_accounts()
        if int(row.get("id") or 0) == int(account_id)
    ]
    if len(matches) != 1 or int(matches[0].get("type") or 0) != 3:
        raise DouyinGraphicMatrixExecutorError(
            "douyin_graphic_account_missing", "抖音图文矩阵账号已不可用"
        )
    return dict(matches[0])


@asynccontextmanager
async def open_matrix_account_page(account: Mapping[str, Any]):
    """打开一个账号专属的前台会话，离开时必定关闭。"""

    storage_state = COOKIE_DIR / Path(str(account.get("filePath") or "")).name
    if not storage_state.is_file():
        raise DouyinGraphicMatrixExecutorError(
            "douyin_graphic_account_session_expired",
            "抖音图文矩阵账号的本地登录会话不存在，请重新登录",
        )
    expected_name = " ".join(
        str(account.get("userName") or account.get("profileName") or "").split()
    )
    if not expected_name:
        raise DouyinGraphicMatrixExecutorError(
            "douyin_graphic_account_identity_missing", "抖音账号缺少可回读的昵称"
        )

    from playwright.async_api import async_playwright
    from utils.base_social_media import (
        launch_publish_browser,
        new_publish_context,
        set_init_script,
    )
    from .douyin_publish_executor import _readback_douyin_session_identity

    playwright = await async_playwright().start()
    browser = None
    context = None
    try:
        browser = await launch_publish_browser(playwright)
        context = await new_publish_context(browser, storage_state=str(storage_state))
        context = await set_init_script(context)
        page = await context.new_page()
        await _readback_douyin_session_identity(page, expected_name)
        yield page
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
        try:
            await playwright.stop()
        except Exception:
            pass


async def _run(
    matrix: Mapping[str, Any],
    *,
    task_id: int,
    submit: bool,
    editor_factory: Callable[..., DouyinGraphicEditor],
    browser_launcher: Callable[[Mapping[str, Any]], AsyncContextManager[Any]],
    account_resolver: Callable[[int], Mapping[str, Any]],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    paused = False
    for target in matrix.get("targets") or []:
        if _pause_requested(int(task_id)):
            task_service.pause_douyin_graphic_matrix(int(task_id))
            paused = True
            break
        item_index = int(target.get("itemIndex") or 0)
        item = task_service.matrix_item_for_index(int(task_id), item_index)
        # 成功、失败等终态都不会在同一任务内重新提交。重试由之后的
        # failed-only 新任务承载，避免重复发布已成功账号。
        if str(item.get("status") or "") != "pending":
            continue
        task_service.start_matrix_item(int(task_id), int(item["id"]))
        try:
            account_id = int(target.get("accountId") or 0)
            account = dict(account_resolver(account_id))
            if int(account.get("id") or 0) != account_id or int(account.get("type") or 0) != 3:
                raise DouyinGraphicMatrixExecutorError(
                    "douyin_graphic_account_mismatch", "矩阵任务账号与当前会话不一致"
                )
            payload = effective_item_payload(matrix, target)
            async with browser_launcher(account) as page:
                editor = editor_factory(
                    task_id=int(task_id), item_id=int(item["id"])
                )
                await editor.prepare(page, payload)
                if _pause_requested(int(task_id)):
                    task_service.pause_douyin_graphic_matrix(
                        int(task_id), current_item_id=int(item["id"])
                    )
                    paused = True
                    break
                receipt = (
                    await editor.submit_and_read_receipt(page, payload)
                    if submit
                    else None
                )
            message = (
                "抖音图文平台回执已确认"
                if submit
                else "抖音图文平台预检通过，未点击最终发布"
            )
            task_service.finish_matrix_item(
                int(task_id),
                int(item["id"]),
                ok=True,
                message=message,
                receipt=receipt,
            )
            results.append(public_result(target, "success", receipt=receipt))
        except Exception as exc:
            code = public_error_code(exc)
            text = str(exc) or type(exc).__name__
            task_service.finish_matrix_item(
                int(task_id),
                int(item["id"]),
                ok=False,
                message=text,
                error_code=code,
            )
            results.append(
                public_result(
                    target,
                    "failed",
                    error_code=code,
                    error_text=text,
                )
            )
        finally:
            task_service.touch_task_heartbeat(int(task_id))
        if paused:
            break
    if not paused:
        task_service.close_matrix_parent(int(task_id))
    _clear_pause(int(task_id))
    return results


async def run_matrix(
    matrix: Mapping[str, Any],
    *,
    task_id: int,
    editor_factory: Callable[..., DouyinGraphicEditor] = DouyinGraphicEditor,
    browser_launcher: Callable[[Mapping[str, Any]], AsyncContextManager[Any]] = open_matrix_account_page,
    account_resolver: Callable[[int], Mapping[str, Any]] = _matrix_account,
) -> list[dict[str, Any]]:
    return await _run(
        matrix,
        task_id=int(task_id),
        submit=True,
        editor_factory=editor_factory,
        browser_launcher=browser_launcher,
        account_resolver=account_resolver,
    )


async def run_matrix_preflight(
    matrix: Mapping[str, Any],
    *,
    task_id: int,
    editor_factory: Callable[..., DouyinGraphicEditor] = DouyinGraphicEditor,
    browser_launcher: Callable[[Mapping[str, Any]], AsyncContextManager[Any]] = open_matrix_account_page,
    account_resolver: Callable[[int], Mapping[str, Any]] = _matrix_account,
) -> list[dict[str, Any]]:
    return await _run(
        matrix,
        task_id=int(task_id),
        submit=False,
        editor_factory=editor_factory,
        browser_launcher=browser_launcher,
        account_resolver=account_resolver,
    )


def run_matrix_sync(matrix: Mapping[str, Any], **kwargs: Any) -> list[dict[str, Any]]:
    return asyncio.run(run_matrix(matrix, **kwargs))


def run_matrix_preflight_sync(
    matrix: Mapping[str, Any], **kwargs: Any
) -> list[dict[str, Any]]:
    return asyncio.run(run_matrix_preflight(matrix, **kwargs))
