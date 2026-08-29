# -*- coding: utf-8 -*-
"""Facebook Page-bound Reel form fill and exact readback contract."""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from app_core.overseas_meta_errors import FacebookPagePublishError
from app_core.overseas_meta_page_identity import (
    FacebookPageIdentity,
    activate_saved_facebook_page,
    normalize_facebook_page_id,
    validate_facebook_page_binding,
)


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COMPLETE_VIDEO_STATES = frozenset(
    {"complete", "completed", "processed", "ready"}
)
_FINAL_ACTION_LABELS = (
    "Publish",
    "Publish now",
    "Share",
    "Share now",
    "Post",
    "发布",
    "立即发布",
    "分享",
    "立即分享",
)


@dataclass(frozen=True, slots=True)
class FacebookPageFormExpectation:
    page_id: str
    content_kind: Literal["reel"]
    video_name: str
    video_size: int
    video_sha256: str
    caption: str
    visibility: Literal["public"]


@dataclass(frozen=True, slots=True)
class FacebookPageFormSnapshot:
    page_id: str
    content_kind: str
    video_name: str
    video_count: int
    caption: str
    visibility: str
    final_action_label: str
    final_action_ready: bool


def canonical_meta_caption(value: object) -> str:
    """Normalize editor-only whitespace without changing content order."""

    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ").replace("\u202f", " ")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def _safe_receipt(
    expected: FacebookPageFormExpectation,
    *,
    phase: str,
    platform_write_occurred: bool = False,
    final_button_enabled: bool | None = None,
) -> dict[str, object]:
    receipt: dict[str, object] = {
        "phase": phase,
        "platformWriteOccurred": bool(platform_write_occurred),
        "finalActionTriggered": False,
    }
    page_id = str(getattr(expected, "page_id", "") or "").strip()
    if page_id.isascii() and page_id.isdigit():
        receipt["pageId"] = page_id
    video_name = Path(str(getattr(expected, "video_name", "") or "")).name
    if video_name and len(video_name) <= 512 and "\r" not in video_name and "\n" not in video_name:
        receipt["videoName"] = video_name
    video_size = getattr(expected, "video_size", None)
    if type(video_size) is int and video_size >= 0:
        receipt["videoSize"] = video_size
    video_sha256 = str(getattr(expected, "video_sha256", "") or "")
    if _SHA256.fullmatch(video_sha256):
        receipt["videoSha256"] = video_sha256
    caption = getattr(expected, "caption", None)
    if type(caption) is str:
        receipt["captionSha256"] = hashlib.sha256(
            canonical_meta_caption(caption).encode("utf-8")
        ).hexdigest()
    visibility = getattr(expected, "visibility", None)
    if visibility == "public":
        receipt["visibility"] = "public"
    if type(final_button_enabled) is bool:
        receipt["finalButtonEnabled"] = final_button_enabled
    return receipt


def _form_readback_failed(
    expected: FacebookPageFormExpectation,
    *,
    platform_write_occurred: bool = False,
    final_button_enabled: bool | None = None,
) -> FacebookPagePublishError:
    return FacebookPagePublishError(
        "facebook_page_form_readback_failed",
        "Facebook Page Reel 表单回读不一致，已在最终操作前停止。",
        receipt=_safe_receipt(
            expected,
            phase="form_readback",
            platform_write_occurred=platform_write_occurred,
            final_button_enabled=final_button_enabled,
        ),
    )


def _upload_failed(
    expected: FacebookPageFormExpectation,
    *,
    platform_write_occurred: bool = False,
) -> FacebookPagePublishError:
    return FacebookPagePublishError(
        "facebook_upload_failed",
        "Facebook Page Reel 视频上传或预览回读失败，已停止。",
        receipt=_safe_receipt(
            expected,
            phase="form_upload",
            platform_write_occurred=platform_write_occurred,
        ),
    )


def _page_readback_failed(page_id: object) -> FacebookPagePublishError:
    receipt: dict[str, object] = {
        "phase": "form_readback",
        "platformWriteOccurred": False,
        "finalActionTriggered": False,
    }
    normalized = str(page_id or "").strip()
    if normalized.isascii() and normalized.isdigit():
        receipt["pageId"] = normalized
    return FacebookPagePublishError(
        "facebook_page_form_readback_failed",
        "Facebook Page Reel 表单回读不一致，已在最终操作前停止。",
        receipt=receipt,
    )


class FacebookPageFormAdapter:
    """Fill one fresh Page Reel composer and return its pre-click snapshot."""

    def __init__(
        self,
        page,
        *,
        wait_for_verification: Callable[..., Awaitable[None]],
    ) -> None:
        if not callable(wait_for_verification):
            raise TypeError("wait_for_verification must be callable")
        self.page = page
        self._wait_for_verification = wait_for_verification
        self._upload_attempted = False
        self._caption_written = False
        self._platform_write_occurred = False
        self._current_expected: FacebookPageFormExpectation | None = None

    def _form_failure(
        self,
        expected: FacebookPageFormExpectation,
        *,
        final_button_enabled: bool | None = None,
    ) -> FacebookPagePublishError:
        return _form_readback_failed(
            expected,
            platform_write_occurred=self._platform_write_occurred,
            final_button_enabled=final_button_enabled,
        )

    def _upload_failure(
        self,
        expected: FacebookPageFormExpectation,
    ) -> FacebookPagePublishError:
        return _upload_failed(
            expected,
            platform_write_occurred=self._platform_write_occurred,
        )

    def _preserve_verification_error(
        self,
        error: FacebookPagePublishError,
        expected: FacebookPageFormExpectation,
    ) -> FacebookPagePublishError:
        if error.error_code in {
            "facebook_verification_required",
            "facebook_verification_timeout",
        }:
            error.receipt = _safe_receipt(
                expected,
                phase="verification",
                platform_write_occurred=self._platform_write_occurred,
            )
        return error

    @staticmethod
    def _validate_expectation(
        expected: FacebookPageFormExpectation,
    ) -> tuple[str, str]:
        if not isinstance(expected, FacebookPageFormExpectation):
            raise TypeError("expected must be FacebookPageFormExpectation")
        try:
            page_id = normalize_facebook_page_id(expected.page_id)
        except FacebookPagePublishError as exc:
            raise _form_readback_failed(expected) from exc
        if expected.content_kind != "reel" or expected.visibility != "public":
            raise _form_readback_failed(expected)
        caption = canonical_meta_caption(expected.caption)
        if type(expected.caption) is not str or not caption:
            raise _form_readback_failed(expected)
        if (
            type(expected.video_name) is not str
            or not expected.video_name
            or type(expected.video_size) is not int
            or expected.video_size < 0
            or type(expected.video_sha256) is not str
            or _SHA256.fullmatch(expected.video_sha256) is None
        ):
            raise _upload_failed(expected)
        return page_id, caption

    @staticmethod
    def _validate_local_video(expected: FacebookPageFormExpectation) -> None:
        path = Path(expected.video_name)
        try:
            if not path.is_file() or path.stat().st_size != expected.video_size:
                raise _upload_failed(expected)
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        except FacebookPagePublishError:
            raise
        except OSError as exc:
            raise _upload_failed(expected) from exc
        if digest.hexdigest() != expected.video_sha256:
            raise _upload_failed(expected)

    async def select_expected_page(
        self,
        expected_page_id: str,
    ) -> FacebookPageIdentity:
        try:
            expected = normalize_facebook_page_id(expected_page_id)
            selected = await activate_saved_facebook_page(self.page, expected)
        except Exception as exc:
            raise _page_readback_failed(expected_page_id) from exc
        if normalize_facebook_page_id(selected.page_id) != expected:
            raise _page_readback_failed(expected_page_id)
        return selected

    async def open_fresh_reel_composer(self, expected_page_id: str) -> None:
        await self.select_expected_page(expected_page_id)
        try:
            await self._click_create_reel_entry(expected_page_id)
            await self._wait_for_verification(self.page)
            selected = await validate_facebook_page_binding(
                self.page,
                {"accountReference": expected_page_id},
            )
            content_kind = str(await self._read_content_kind() or "").casefold()
        except FacebookPagePublishError as exc:
            if exc.error_code in {
                "facebook_verification_required",
                "facebook_verification_timeout",
            }:
                raise
            raise _page_readback_failed(expected_page_id) from exc
        except Exception as exc:
            raise _page_readback_failed(expected_page_id) from exc
        if (
            normalize_facebook_page_id(selected.page_id) != expected_page_id
            or content_kind != "reel"
        ):
            raise _page_readback_failed(expected_page_id)

    async def fill_and_readback(
        self,
        expected: FacebookPageFormExpectation,
    ) -> FacebookPageFormSnapshot:
        page_id, expected_caption = self._validate_expectation(expected)
        self._validate_local_video(expected)
        self._current_expected = expected

        try:
            await self._wait_for_verification(self.page)
            await self.open_fresh_reel_composer(page_id)
            initial_kind = str(await self._read_content_kind() or "").casefold()
            restored_draft = bool(await self._read_restored_draft())
            initial_previews = await self._read_video_previews()
            initial_caption = canonical_meta_caption(
                await self._read_caption_editor()
            )
        except FacebookPagePublishError as exc:
            raise self._preserve_verification_error(exc, expected)
        except Exception as exc:
            raise self._form_failure(expected) from exc
        if (
            initial_kind != "reel"
            or restored_draft
            or initial_previews
            or initial_caption
        ):
            raise self._form_failure(expected)

        try:
            await self._recheck_expected_page(expected)
            await self._clear_caption_editor()
            if canonical_meta_caption(await self._read_caption_editor()):
                raise self._form_failure(expected)
        except FacebookPagePublishError as exc:
            raise self._preserve_verification_error(exc, expected)
        except Exception as exc:
            raise self._form_failure(expected) from exc

        if self._upload_attempted:
            raise self._upload_failure(expected)
        self._upload_attempted = True
        try:
            await self._recheck_expected_page(expected)
            await self._upload_video_once(expected.video_name)
            self._platform_write_occurred = True
            video_name, video_count = await self._wait_for_completed_video(
                expected
            )
        except FacebookPagePublishError as exc:
            raise self._preserve_verification_error(exc, expected)
        except Exception as exc:
            raise self._upload_failure(expected) from exc

        if self._caption_written:
            raise self._form_failure(expected)
        self._caption_written = True
        try:
            await self._recheck_expected_page(expected)
            await self._write_caption_once(expected_caption)
            self._platform_write_occurred = True
            caption = canonical_meta_caption(await self._read_caption_editor())
            if caption != expected_caption:
                raise self._form_failure(expected)
            await self._select_public_visibility()
            visibility = str(await self._read_visibility() or "").casefold()
            if visibility != "public":
                raise self._form_failure(expected)

            await self._wait_for_verification(self.page)
            selected = await self._recheck_expected_page(expected)
            content_kind = str(await self._read_content_kind() or "").casefold()
            final_restored_draft = bool(await self._read_restored_draft())
            final_previews = await self._read_video_previews()
            final_caption = canonical_meta_caption(
                await self._read_caption_editor()
            )
            final_visibility = str(
                await self._read_visibility() or ""
            ).casefold()
        except FacebookPagePublishError as exc:
            raise self._preserve_verification_error(exc, expected)
        except Exception as exc:
            raise self._form_failure(expected) from exc

        if (
            normalize_facebook_page_id(selected.page_id) != page_id
            or content_kind != "reel"
            or final_restored_draft
            or len(final_previews) != 1
            or Path(str(final_previews[0][0])).name != Path(expected.video_name).name
            or str(final_previews[0][1] or "").casefold()
            not in _COMPLETE_VIDEO_STATES
            or final_caption != expected_caption
            or final_visibility != "public"
        ):
            raise self._form_failure(expected)

        button = await self.final_action_button()
        try:
            label = " ".join((await self._button_label(button)).split())
            ready = bool(await self._button_ready(button))
        except Exception as exc:
            raise self._form_failure(expected) from exc
        if label not in _FINAL_ACTION_LABELS or not ready:
            raise self._form_failure(
                expected,
                final_button_enabled=ready,
            )

        return FacebookPageFormSnapshot(
            page_id=page_id,
            content_kind="reel",
            video_name=video_name,
            video_count=video_count,
            caption=final_caption,
            visibility="public",
            final_action_label=label,
            final_action_ready=ready,
        )

    async def final_action_button(self) -> Any:
        try:
            buttons = await self._final_action_buttons()
        except Exception as exc:
            if self._current_expected is not None:
                raise self._form_failure(self._current_expected) from exc
            raise
        if len(buttons) != 1:
            if self._current_expected is not None:
                raise self._form_failure(self._current_expected)
            raise RuntimeError("Facebook Page Reel final action is not unique")
        return buttons[0]

    async def _wait_for_completed_video(
        self,
        expected: FacebookPageFormExpectation,
    ) -> tuple[str, int]:
        expected_name = Path(expected.video_name).name
        for _ in range(24):
            await self._wait_for_verification(self.page)
            await self._recheck_expected_page(expected)
            previews = await self._read_video_previews()
            if len(previews) > 1:
                raise self._upload_failure(expected)
            if len(previews) == 1:
                actual_name = Path(str(previews[0][0] or "")).name
                state = str(previews[0][1] or "").casefold()
                if actual_name != expected_name:
                    raise self._upload_failure(expected)
                if state in _COMPLETE_VIDEO_STATES:
                    return actual_name, 1
            await self._sleep()
        raise self._upload_failure(expected)

    async def _recheck_expected_page(
        self,
        expected: FacebookPageFormExpectation,
    ) -> FacebookPageIdentity:
        try:
            selected = await validate_facebook_page_binding(
                self.page,
                {"accountReference": expected.page_id},
            )
        except Exception as exc:
            raise self._form_failure(expected) from exc
        if normalize_facebook_page_id(selected.page_id) != expected.page_id:
            raise self._form_failure(expected)
        return selected

    async def _click_create_reel_entry(self, expected_page_id: str) -> None:
        page_selector = (
            f'[data-page-id="{expected_page_id}"]'
            '[data-page-active="true"] [data-meta-create-reel]'
        )
        candidates = await self._visible_from_locator(self.page.locator(page_selector))
        if len(candidates) != 1:
            raise RuntimeError("fresh Reel entry is not unique")
        await candidates[0].click()

    async def _read_content_kind(self) -> str:
        for selector, attribute in (
            ("[data-meta-content-kind]", "data-meta-content-kind"),
            ("[data-content-kind]", "data-content-kind"),
            ("[data-composer-kind]", "data-composer-kind"),
        ):
            items = await self._visible_from_locator(self.page.locator(selector))
            if items:
                if len(items) != 1:
                    return ""
                return str(await items[0].get_attribute(attribute) or "")
        reel = await self._visible_from_locator(
            self.page.locator('[data-testid="reel-composer"]')
        )
        return "reel" if len(reel) == 1 else ""

    async def _read_restored_draft(self) -> bool:
        restored: list[Any] = []
        for selector in (
            '[data-restored-draft="true"]',
            '[data-draft-restored="true"]',
            '[data-meta-composer-state="restored"]',
        ):
            restored.extend(
                await self._visible_from_locator(self.page.locator(selector))
            )
        if restored:
            return True

        fresh: list[Any] = []
        for selector in (
            '[data-restored-draft="false"]',
            '[data-draft-restored="false"]',
            '[data-meta-composer-state="fresh"]',
        ):
            fresh.extend(
                await self._visible_from_locator(self.page.locator(selector))
            )
        if len(fresh) != 1:
            raise RuntimeError("fresh Reel composer state is not provable")
        return False

    async def _read_video_previews(self) -> list[tuple[str, str]]:
        items: list[Any] = []
        for selector in (
            "[data-meta-video-preview]",
            "[data-video-preview]",
            '[data-testid="video-preview"]',
        ):
            items = await self._visible_from_locator(self.page.locator(selector))
            if items:
                break
        if not items:
            empty_states: list[Any] = []
            for selector in (
                '[data-meta-media-empty="true"]',
                '[data-testid="reel-composer-media-empty"]',
            ):
                empty_states.extend(
                    await self._visible_from_locator(self.page.locator(selector))
                )
            if len(empty_states) == 1:
                return []
            if len(empty_states) > 1:
                raise RuntimeError("Reel composer empty media state is ambiguous")

            collections: list[Any] = []
            for selector in (
                "[data-meta-media-collection]",
                '[data-testid="reel-composer-media"]',
            ):
                collections.extend(
                    await self._visible_from_locator(self.page.locator(selector))
                )
            if len(collections) != 1:
                raise RuntimeError("Reel composer media state is not readable")
            count_value = (
                await collections[0].get_attribute("data-video-count")
                or await collections[0].get_attribute("data-media-count")
            )
            try:
                media_count = int(str(count_value))
            except (TypeError, ValueError) as exc:
                raise RuntimeError("Reel composer media count is not readable") from exc
            if media_count == 0:
                return []
            raise RuntimeError("Reel composer media previews are not readable")
        result: list[tuple[str, str]] = []
        for item in items:
            name = (
                await item.get_attribute("data-video-name")
                or await item.get_attribute("data-file-name")
                or await item.get_attribute("title")
                or ""
            )
            state = (
                await item.get_attribute("data-upload-status")
                or await item.get_attribute("data-status")
                or ""
            )
            result.append((str(name), str(state)))
        return result

    async def _caption_editor(self) -> Any:
        for selector in (
            "[data-meta-caption-editor]",
            '[contenteditable="true"][role="textbox"]',
            'textarea[placeholder*="caption" i]',
        ):
            items = await self._visible_from_locator(self.page.locator(selector))
            if items:
                if len(items) != 1:
                    raise RuntimeError("caption editor is not unique")
                return items[0]
        raise RuntimeError("caption editor is missing")

    async def _read_caption_editor(self) -> str:
        editor = await self._caption_editor()
        try:
            return str(await editor.input_value())
        except Exception:
            try:
                return str(await editor.inner_text())
            except Exception:
                return str(await editor.text_content() or "")

    async def _clear_caption_editor(self) -> None:
        editor = await self._caption_editor()
        try:
            await editor.fill("")
            return
        except Exception:
            await editor.click()
            await self.page.keyboard.press("ControlOrMeta+A")
            await self.page.keyboard.press("Backspace")

    async def _upload_video_once(self, file_path: str) -> None:
        inputs = self.page.locator('input[type="file"][accept*="video" i]')
        count = int(await inputs.count())
        if count == 0:
            inputs = self.page.locator('input[type="file"]')
            count = int(await inputs.count())
        if count != 1:
            raise RuntimeError("video input is not unique")
        await inputs.nth(0).set_input_files(file_path)

    async def _write_caption_once(self, caption: str) -> None:
        editor = await self._caption_editor()
        try:
            await editor.fill(caption)
            return
        except Exception:
            await editor.click()
            await self.page.keyboard.insert_text(caption)

    async def _select_public_visibility(self) -> None:
        candidates = await self._visible_from_locator(
            self.page.locator('[data-meta-visibility-option="public"]')
        )
        if not candidates:
            candidates = []
            for label in ("Public", "公开"):
                candidates.extend(
                    await self._visible_from_locator(
                        self.page.get_by_role(
                            "radio",
                            name=label,
                            exact=True,
                        )
                    )
                )
        if len(candidates) != 1:
            raise RuntimeError("public visibility control is not unique")
        control = candidates[0]
        try:
            if await control.is_checked():
                return
            await control.check()
        except Exception:
            await control.click()

    async def _read_visibility(self) -> str:
        current = await self._visible_from_locator(
            self.page.locator("[data-meta-visibility-current]")
        )
        if current:
            if len(current) != 1:
                return ""
            return str(
                await current[0].get_attribute("data-meta-visibility-current")
                or ""
            )
        checked: list[str] = []
        for label, value in (
            ("Public", "public"),
            ("公开", "public"),
            ("Private", "private"),
            ("仅自己", "private"),
        ):
            locator = self.page.get_by_role("radio", name=label, exact=True)
            for item in await self._visible_from_locator(locator):
                try:
                    if await item.is_checked():
                        checked.append(value)
                except Exception:
                    return ""
        return checked[0] if len(checked) == 1 else ""

    async def _final_action_buttons(self) -> list[Any]:
        candidates = await self._visible_from_locator(
            self.page.locator("[data-meta-final-action]")
        )
        if candidates:
            return candidates
        for label in _FINAL_ACTION_LABELS:
            candidates.extend(
                await self._visible_from_locator(
                    self.page.get_by_role(
                        "button",
                        name=label,
                        exact=True,
                    )
                )
            )
        return candidates

    @staticmethod
    async def _button_label(button: Any) -> str:
        try:
            label = await button.inner_text()
        except Exception:
            label = await button.get_attribute("aria-label")
        return str(label or "").strip()

    @staticmethod
    async def _button_ready(button: Any) -> bool:
        return bool(await button.is_enabled())

    async def _sleep(self) -> None:
        wait = getattr(self.page, "wait_for_timeout", None)
        if callable(wait):
            await wait(250)
        else:
            await asyncio.sleep(0.25)

    @staticmethod
    async def _visible_from_locator(locator: Any) -> list[Any]:
        result: list[Any] = []
        count = int(await locator.count())
        for index in range(count):
            item = locator.nth(index)
            try:
                if await item.is_visible():
                    result.append(item)
            except AttributeError:
                result.append(item)
        return result
