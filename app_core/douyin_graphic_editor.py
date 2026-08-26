"""抖音图文编辑页的唯一填写和回读适配器。

预检和正式执行共用 ``prepare``；只有获得一次性授权的正式执行
才能调用 ``submit_and_read_receipt``。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo


DOUYIN_GRAPHIC_UPLOAD_URL = (
    "https://creator.douyin.com/creator-micro/content/upload?default-tab=3"
)
_SHANGHAI = ZoneInfo("Asia/Shanghai")


class DouyinGraphicEditorError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = str(error_code)
        self.public_message = str(message)
        super().__init__(message)


@dataclass(frozen=True)
class DouyinGraphicReadback:
    image_count: int
    title: str
    body: str
    tags: tuple[str, ...]
    scheduled_at: str


def _normalized(value: object) -> str:
    return " ".join(str(value or "").replace("\u200b", " ").split())


def _topic_names(payload: Mapping[str, Any]) -> list[str]:
    result: list[str] = []
    for value in payload.get("tags") or []:
        name = str(value or "").strip().lstrip("#").strip()
        if name and name not in result:
            result.append(name)
    return result


def _schedule(payload: Mapping[str, Any]) -> str:
    value = _normalized(payload.get("scheduleTime")).replace("T", " ")
    if payload.get("enableTimer") is not True:
        return ""
    try:
        datetime.strptime(value, "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise DouyinGraphicEditorError(
            "douyin_graphic_schedule_invalid",
            "抖音图文定时必须为 YYYY-MM-DD HH:mm（北京时间）",
        ) from exc
    if str(payload.get("scheduleTimezone") or "Asia/Shanghai") != "Asia/Shanghai":
        raise DouyinGraphicEditorError(
            "douyin_graphic_schedule_invalid", "抖音图文定时只接受北京时间"
        )
    return value


class DouyinGraphicEditor:
    def __init__(self, *, task_id: int | None = None, item_id: int | None = None) -> None:
        self.task_id = int(task_id or 0)
        self.item_id = int(item_id or 0)

    async def prepare(
        self, page: Any, payload: Mapping[str, Any]
    ) -> DouyinGraphicReadback:
        # 保留内容包传入的可见路径；macOS 的 /var 解析后会变为
        # /private/var，不应仅因符号链接让任务快照和实际上传路径不一致。
        files = [str(Path(str(path)).absolute()) for path in payload.get("fileList") or []]
        if not files or len(files) > 35 or not all(Path(path).is_file() for path in files):
            raise DouyinGraphicEditorError(
                "douyin_graphic_images_invalid", "抖音图文需要 1–35 张可读图片"
            )
        title = str(payload.get("title") or "").strip()
        body = str(payload.get("description") or "").strip()
        if not title or not body:
            raise DouyinGraphicEditorError(
                "douyin_graphic_content_invalid", "抖音图文标题和正文不能为空"
            )
        tags = _topic_names(payload)

        image_count = await self._upload_images(page, files)
        if image_count != len(files):
            raise DouyinGraphicEditorError(
                "douyin_graphic_image_upload_failed",
                f"抖音图文图片数量回读不一致：期望 {len(files)}，实际 {image_count}",
            )

        content = await self._sync_content(
            page, title=title, body=body, tags=tags, first_file=files[0]
        )
        actual_title = _normalized(content.get("title"))
        actual_body = _normalized(content.get("body") or content.get("description"))
        actual_tags = tuple(_topic_names({"tags": content.get("tags") or []}))
        if actual_title != _normalized(title) or actual_body != _normalized(body):
            raise DouyinGraphicEditorError(
                "douyin_graphic_content_readback_mismatch",
                "抖音图文标题或正文回读不一致",
            )
        if actual_tags != tuple(tags):
            missing = [tag for tag in tags if tag not in actual_tags]
            raise DouyinGraphicEditorError(
                "douyin_topic_entity_missing",
                "抖音图文话题未形成官方平台实体：" + "、".join(missing or tags),
            )

        scheduled_at = await self._set_schedule(page, payload)
        return DouyinGraphicReadback(
            image_count=image_count,
            title=actual_title,
            body=actual_body,
            tags=actual_tags,
            scheduled_at=scheduled_at,
        )

    async def submit_and_read_receipt(
        self, page: Any, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        submit_hook = getattr(page, "douyin_graphic_submit", None)
        if callable(submit_hook):
            await submit_hook()
        else:
            await self._submit_real_page(page)

        receipt_hook = getattr(page, "douyin_graphic_receipt", None)
        if callable(receipt_hook):
            receipt = await receipt_hook(dict(payload))
        else:
            receipt = await self._read_real_receipt(page, payload)
        if not isinstance(receipt, Mapping) or not any(
            _normalized(receipt.get(key))
            for key in ("platformPostId", "postUrl", "publishedAt", "scheduledAt")
        ):
            raise DouyinGraphicEditorError(
                "douyin_graphic_submit_receipt_missing",
                "抖音图文提交后没有取得平台作品或定时回执",
            )
        return {
            key: receipt.get(key)
            for key in (
                "platformPostId",
                "postUrl",
                "publishedAt",
                "scheduledAt",
                "scheduleTime",
                "timezone",
            )
            if receipt.get(key) not in (None, "")
        }

    async def _upload_images(self, page: Any, files: list[str]) -> int:
        hook = getattr(page, "douyin_graphic_upload", None)
        if callable(hook):
            return int(await hook(files))
        await page.goto(
            DOUYIN_GRAPHIC_UPLOAD_URL,
            wait_until="domcontentloaded",
            timeout=45_000,
        )
        upload = page.locator('input[type="file"]').first
        await upload.wait_for(state="attached", timeout=15_000)
        await upload.set_input_files(files)
        await page.wait_for_url("**/creator-micro/content/post/image*", timeout=30_000)
        for _ in range(60):
            count = await page.evaluate(
                """
                expected => {
                  const visible = node => {
                    const style = getComputedStyle(node);
                    const rect = node.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden'
                      && rect.width > 20 && rect.height > 20;
                  };
                  const groups = [
                    '[class*="image-card"]', '[class*="upload-item"]',
                    '[class*="material-item"]', '[class*="preview-item"]'
                  ];
                  let best = 0;
                  for (const selector of groups) {
                    const nodes = Array.from(document.querySelectorAll(selector))
                      .filter(node => visible(node) && node.querySelector('img'));
                    best = Math.max(best, nodes.length);
                  }
                  for (const input of document.querySelectorAll('input[type="file"]')) {
                    best = Math.max(best, input.files ? input.files.length : 0);
                  }
                  return Math.min(best, expected);
                }
                """,
                len(files),
            )
            if int(count or 0) == len(files):
                return len(files)
            await page.wait_for_timeout(500)
        return 0

    async def _sync_content(
        self,
        page: Any,
        *,
        title: str,
        body: str,
        tags: list[str],
        first_file: str,
    ) -> Mapping[str, Any]:
        hook = getattr(page, "douyin_graphic_sync_content", None)
        if callable(hook):
            return await hook(title=title, body=body, tags=tags)
        from uploader.douyin_uploader.main import DouYinVideo

        helper = DouYinVideo(
            title=title,
            file_path=first_file,
            tags=tags,
            publish_date=0,
            account_file="",
            dry_run=True,
            dry_run_hold_browser=False,
            description=body,
        )
        try:
            result = await helper.sync_uploaded_editor_content(
                page, title=title, description=body, tags=tags
            )
        except Exception as exc:
            code = str(getattr(exc, "error_code", "") or "")
            if code == "douyin_topic_entity_missing":
                raise DouyinGraphicEditorError(code, str(exc)) from exc
            raise DouyinGraphicEditorError(
                code or "douyin_graphic_content_readback_failed", str(exc)
            ) from exc
        return result

    async def _set_schedule(self, page: Any, payload: Mapping[str, Any]) -> str:
        value = _schedule(payload)
        hook = getattr(page, "douyin_graphic_set_schedule", None)
        if callable(hook):
            return _normalized(await hook(value))
        from uploader.douyin_uploader.main import DouYinVideo

        helper = DouYinVideo(
            title="schedule",
            file_path=str((payload.get("fileList") or [""])[0]),
            tags=[],
            publish_date=0,
            account_file="",
            dry_run=True,
            dry_run_hold_browser=False,
            description="schedule",
        )
        try:
            if not value:
                return _normalized(await helper.clear_schedule_time_douyin(page))
            target = datetime.strptime(value, "%Y-%m-%d %H:%M")
            readback = await helper.set_schedule_time_douyin(page, target)
        except Exception as exc:
            raise DouyinGraphicEditorError(
                "douyin_graphic_schedule_readback_mismatch", str(exc)
            ) from exc
        if not helper._schedule_time_matches(str(readback), value):
            raise DouyinGraphicEditorError(
                "douyin_graphic_schedule_readback_mismatch",
                "抖音图文定时回读不一致",
            )
        return value

    async def _submit_real_page(self, page: Any) -> None:
        from uploader.douyin_uploader.main import DouYinVideo
        from .douyin_publish_executor import _handle_publish_verification

        helper = DouYinVideo(
            title="submit",
            file_path="",
            tags=[],
            publish_date=0,
            account_file="",
            dry_run=False,
            dry_run_hold_browser=False,
            description="submit",
        )
        try:
            button = await helper.wait_publish_button_ready(page)
            await button.click(timeout=10_000)
            await helper._wait_formal_publish_result(
                page,
                on_verification=lambda challenge: _handle_publish_verification(
                    helper,
                    page,
                    challenge,
                    task_id=self.task_id,
                ),
            )
        except Exception as exc:
            raise DouyinGraphicEditorError(
                str(getattr(exc, "error_code", "") or "douyin_graphic_submit_failed"),
                str(exc),
            ) from exc

    async def _read_real_receipt(
        self, page: Any, payload: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        value = _schedule(payload)
        if value:
            from .douyin_publish_executor import _scheduled_submission_readback

            target = datetime.strptime(value, "%Y-%m-%d %H:%M")
            readback = await _scheduled_submission_readback(
                page, title=str(payload.get("title") or ""), target=target
            )
            return {
                "postUrl": str(readback.get("url") or ""),
                "scheduledAt": value,
                "timezone": "Asia/Shanghai",
            }
        current_url = str(getattr(page, "url", "") or "")
        if "/creator-micro/content/manage" not in current_url:
            return None
        return {
            "postUrl": current_url,
            "publishedAt": datetime.now(_SHANGHAI).strftime("%Y-%m-%d %H:%M:%S"),
            "timezone": "Asia/Shanghai",
        }
