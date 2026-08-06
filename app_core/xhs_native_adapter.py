# -*- coding: utf-8 -*-
"""一键发内置的小红书页面适配器。

本模块根据蚁小二 4.13.25 恢复代码中的小红书任务模型与本地 RPA 顺序重写，
但运行时完全属于一键发：不启动蚁小二客户端、不调用 yxer、不访问蚁小二网关
或远程签名服务，也不读取蚁小二账号目录。

小红书恢复代码中的直接 HTTP 发布依赖私有动态签名，无法作为独立能力安全移植。
因此这里使用已登录的官方创作页面，让平台页面自身完成必要的请求校验；所有关键
字段都要求页面回读一致，未知控件或提示一律安全停止。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
from typing import Any
from zoneinfo import ZoneInfo


XHS_PUBLISH_URL = (
    "https://creator.xiaohongshu.com/publish/publish?source=official"
)

MAX_IMAGES = 18
MAX_TITLE_LENGTH = 20
MAX_DESCRIPTION_LENGTH = 1000
MAX_TOPICS = 10

_IMAGE_EXTENSIONS = {".avif", ".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"}
_VIDEO_EXTENSIONS = {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"}
_AI_LABEL = "AI生成内容"


class XhsNativeAdapterError(RuntimeError):
    """小红书字段、控件或回读不能被唯一确认时安全停止。"""


def _normalized(value: object) -> str:
    return " ".join(str(value or "").replace("\u200b", "").split())


def _topic_candidate_matches(candidate_name: object, topic: str) -> bool:
    """只按官方候选项的话题名称做精确匹配。

    小红书候选卡片的整体文本还包含“活动话题”和浏览量，不能把整张
    卡片与 ``#话题`` 直接比较，否则会把正确的唯一候选误判为失败。
    """

    return _normalized(candidate_name) == f"#{str(topic).strip().lstrip('#')}"


async def _is_actual_viewport_visible(locator) -> bool:
    """排除平台遗留节点、无障碍副本和视口外节点。"""

    return bool(
        await locator.evaluate(
            """node => {
                const style = getComputedStyle(node);
                const rect = node.getBoundingClientRect();
                const opacity = Number(style.opacity || 1);
                const intersectionWidth = Math.max(
                    0,
                    Math.min(rect.right, innerWidth) - Math.max(rect.left, 0)
                );
                const intersectionHeight = Math.max(
                    0,
                    Math.min(rect.bottom, innerHeight) - Math.max(rect.top, 0)
                );
                return !node.hidden
                    && node.getAttribute('aria-hidden') !== 'true'
                    && style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && opacity > 0.01
                    && rect.width > 0
                    && rect.height > 0
                    && intersectionWidth > 0
                    && intersectionHeight > 0;
            }"""
        )
    )


def _topics(payload: dict[str, Any]) -> list[str]:
    topics = list(
        dict.fromkeys(
            str(value).strip().lstrip("#")
            for value in payload.get("tags") or []
            if str(value).strip().lstrip("#")
        )
    )
    if len(topics) > MAX_TOPICS:
        raise XhsNativeAdapterError(
            f"小红书最多支持 {MAX_TOPICS} 个话题"
        )
    return topics


def _schedule_timestamp(payload: dict[str, Any]) -> int | None:
    enabled = payload.get("enableTimer") is True
    raw = str(payload.get("scheduleTime") or "").strip().replace("T", " ")
    if not enabled:
        if raw:
            raise XhsNativeAdapterError(
                "小红书未开启定时发布，但载荷中存在定时时间"
            )
        return None
    try:
        local_time = datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(
            tzinfo=ZoneInfo("Asia/Shanghai")
        )
    except ValueError as exc:
        raise XhsNativeAdapterError(
            "小红书定时时间必须为 YYYY-MM-DD HH:mm"
        ) from exc
    return int(local_time.timestamp() * 1000)


def _visibility_code(value: object) -> int:
    """映射蚁小二恢复任务模型中的小红书可见性编码。"""

    normalized = str(value or "public").strip().lower()
    mapping = {
        "public": 0,
        "公开": 0,
        "private": 1,
        "仅自己可见": 1,
        "friend": 4,
        "friends": 4,
        "仅好友可见": 4,
    }
    if normalized not in mapping:
        raise XhsNativeAdapterError(f"小红书不支持的可见性：{value}")
    return mapping[normalized]


def build_native_contract(payload: dict[str, Any]) -> dict[str, Any]:
    """生成一键发内部任务契约；此函数纯本地运行，不访问平台。"""

    content_type = str(payload.get("contentType") or "").strip()
    if content_type not in {"article", "video"}:
        raise XhsNativeAdapterError(
            "小红书只支持图文或视频，不支持纯文字发布"
        )

    files = [Path(str(value)).resolve() for value in payload.get("fileList") or []]
    if not files or any(not item.is_file() for item in files):
        raise XhsNativeAdapterError("小红书存在不可读取的本地素材")

    cover_path = Path(str(payload.get("coverPath") or "")).resolve()
    cover = ""
    if str(payload.get("coverPath") or "").strip():
        if not cover_path.is_file() or cover_path.suffix.lower() not in _IMAGE_EXTENSIONS:
            raise XhsNativeAdapterError("小红书封面不是可读取的本地图片")
        cover = str(cover_path)

    # 小红书图文没有独立封面上传：第一张正文图片就是笔记封面。若发布页选择了
    # 封面，必须把它稳定放在图片序列首位，不能像旧链路一样静默忽略。
    if content_type == "article" and cover:
        files = [item for item in files if item != cover_path]
        files.insert(0, cover_path)

    if content_type == "article":
        if not 1 <= len(files) <= MAX_IMAGES:
            raise XhsNativeAdapterError(
                f"小红书图文图片数量必须为 1-{MAX_IMAGES}"
            )
        if any(item.suffix.lower() not in _IMAGE_EXTENSIONS for item in files):
            raise XhsNativeAdapterError("小红书图文任务只能包含图片素材")
    else:
        if len(files) != 1:
            raise XhsNativeAdapterError("小红书视频任务必须且只能包含一个视频")
        if files[0].suffix.lower() not in _VIDEO_EXTENSIONS:
            raise XhsNativeAdapterError("小红书视频任务包含不支持的视频格式")

    title = _normalized(payload.get("title"))
    description = str(payload.get("description") or "").strip()
    if not title or len(title) > MAX_TITLE_LENGTH:
        raise XhsNativeAdapterError(
            f"小红书标题必须为 1-{MAX_TITLE_LENGTH} 字"
        )
    if not description or len(description) > MAX_DESCRIPTION_LENGTH:
        raise XhsNativeAdapterError(
            f"小红书正文必须为 1-{MAX_DESCRIPTION_LENGTH} 字"
        )

    media = [
        {
            "path": str(item),
            "size": item.stat().st_size,
            "format": item.suffix.lstrip(".").lower(),
        }
        for item in files
    ]
    return {
        "formType": "task",
        "contentType": content_type,
        "nativePublishType": "imageText" if content_type == "article" else "video",
        "title": title,
        "description": description,
        "media": media,
        "images": media if content_type == "article" else [],
        "video": media[0] if content_type == "video" else None,
        "coverPath": cover,
        "coverMode": "first-image" if content_type == "article" else "custom-video-cover",
        "visibleType": _visibility_code(payload.get("visibility")),
        "scheduledTime": _schedule_timestamp(payload),
        "topics": _topics(payload),
        "locationKeyword": _normalized(payload.get("locationKeyword")),
        "collectionName": _normalized(payload.get("collectionName")),
        "aiDeclaration": payload.get("aiGenerated") is True,
        "originalDeclaration": payload.get("originalDeclaration") is True,
        # 用于离线测试和运行时审计，防止以后又退回外部客户端或私有签名服务。
        "executionBackend": "oneclick-official-creator-page",
        "requiresYixiaoerClient": False,
        "requiresYixiaoerGateway": False,
        "usesPrivateSignatureService": False,
    }


async def _set_dom_value(locator, value: str) -> None:
    """写入平台受控输入框并派发与人工输入一致的基础事件。"""

    await locator.evaluate(
        """(element, nextValue) => {
            const prototype = element instanceof HTMLTextAreaElement
                ? HTMLTextAreaElement.prototype
                : HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;
            if (setter && 'value' in element) setter.call(element, nextValue);
            else element.textContent = nextValue;
            element.dispatchEvent(new Event('input', { bubbles: true }));
            element.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
        value,
    )


async def _first_visible(locator, label: str):
    visible = []
    for index in range(await locator.count()):
        item = locator.nth(index)
        try:
            in_viewport = await _is_actual_viewport_visible(item)
            if await item.is_visible() and in_viewport:
                visible.append(item)
        except Exception:
            continue
    if len(visible) != 1:
        raise XhsNativeAdapterError(
            f"小红书{label}无法唯一识别，可见候选={len(visible)}"
        )
    return visible[0]


class XhsNativeAdapter:
    """一键发自有的小红书图文/视频执行适配器。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = dict(payload)
        self.contract = build_native_contract(self.payload)

    @property
    def ai_generated(self) -> bool:
        return bool(self.contract["aiDeclaration"])

    @property
    def original_declaration(self) -> bool:
        return bool(self.contract["originalDeclaration"])

    @property
    def content_type(self) -> str:
        return str(self.contract["contentType"])

    async def _select_content_type(self, page) -> None:
        label = "上传图文" if self.content_type == "article" else "上传视频"
        container = page.locator(".upload-container").first
        try:
            await container.wait_for(state="visible", timeout=15_000)
            await page.locator(".upload-container .creator-tab").first.wait_for(
                state="attached",
                timeout=5_000,
            )
        except Exception as exc:
            raise XhsNativeAdapterError("小红书发布类型区域加载超时") from exc
        selector = page.locator(
            ".upload-container .header-tabs "
            ".creator-tab:not([aria-hidden='true'])"
        ).filter(has_text=label)
        if await selector.count():
            tab = await _first_visible(selector, f"{label}入口")
            await tab.click(timeout=8_000)
            await page.wait_for_timeout(600)
            return

        # 小红书有时默认进入视频页且不渲染视频标签；只允许视频沿用该默认态。
        if self.content_type == "video":
            upload = page.locator("input.upload-input[type=file]")
            if await upload.count():
                return
        raise XhsNativeAdapterError(f"小红书{label}入口无法识别")

    async def _upload_media(self, page) -> int:
        if self.content_type == "article":
            candidates = page.locator(
                "input.upload-input[type=file][accept*='image'], "
                ".upload-container input.upload-input[type=file], "
                "input[type=file][multiple]"
            )
            paths = [item["path"] for item in self.contract["images"]]
        else:
            candidates = page.locator(
                "input.upload-input[type=file][accept*='video'], "
                ".upload-container input.upload-input[type=file]"
            )
            paths = [self.contract["video"]["path"]]

        if not await candidates.count():
            raise XhsNativeAdapterError("小红书素材上传控件未加载")
        upload = candidates.first
        await upload.wait_for(state="attached", timeout=10_000)
        await upload.set_input_files(paths if len(paths) > 1 else paths[0])
        # 选择视频后平台会立即销毁 file input，因此不能再从
        # input.files 回读。改为验证平台真实渲染出的图片预览或视频
        # 文件面板，这也比“本地已选文件”更接近用户可见结果。
        try:
            if self.content_type == "article":
                preview_area = page.locator(".img-preview-area").first
                await preview_area.wait_for(state="visible", timeout=20_000)
                preview_images = preview_area.locator("img.img.preview")
                await preview_images.first.wait_for(state="visible", timeout=10_000)
                actual_count = await preview_images.count()
                if actual_count != len(paths):
                    raise XhsNativeAdapterError(
                        "小红书图片预览数量回读不一致："
                        f"期望={len(paths)}，实际={actual_count}"
                    )
            else:
                filename = Path(paths[0]).name
                media_panel = page.locator(
                    ".publish-page-content-media",
                    has_text=filename,
                ).first
                await media_panel.wait_for(state="visible", timeout=30_000)
                panel_text = _normalized(await media_panel.inner_text())
                if filename not in panel_text:
                    raise XhsNativeAdapterError(
                        f"小红书视频文件回读不一致：{filename}"
                    )
                await page.locator(".tiptap.ProseMirror").first.wait_for(
                    state="visible",
                    timeout=20_000,
                )
        except XhsNativeAdapterError:
            raise
        except Exception as exc:
            raise XhsNativeAdapterError("小红书素材上传后回读超时") from exc
        return len(paths)

    async def _fill_text_fields(self, page) -> dict[str, Any]:
        title_input = await _first_visible(
            page.locator(
                'input[placeholder="填写标题会有更多赞哦"], '
                'input[placeholder*="填写标题"], '
                ".d-input-wrapper .d-input input"
            ),
            "标题输入框",
        )
        editor = await _first_visible(
            page.locator(".tiptap.ProseMirror"),
            "正文编辑器",
        )
        await _set_dom_value(title_input, self.contract["title"])
        await editor.evaluate(
            """(node, value) => {
                node.textContent = value;
                node.dispatchEvent(new InputEvent('input', {
                    bubbles: true,
                    inputType: 'insertText',
                    data: value,
                }));
            }""",
            self.contract["description"],
        )
        title_readback = _normalized(await title_input.input_value())
        body_readback = _normalized(await editor.inner_text())
        if title_readback != self.contract["title"]:
            raise XhsNativeAdapterError("小红书标题写入后回读不一致")
        if _normalized(self.contract["description"]) not in body_readback:
            raise XhsNativeAdapterError("小红书正文写入后回读不一致")
        return {
            "title": title_readback,
            "body": await editor.inner_text(),
        }

    async def _set_video_cover(self, page) -> bool:
        """有自定义视频封面时在真实封面编辑器内上传并确认。"""

        cover_path = str(self.contract.get("coverPath") or "")
        if self.content_type != "video" or not cover_path:
            return False

        # 页面标题和真正的可点击卡片都显示“设置封面”，不能按纯文字
        # 匹配。真实交互入口是封面预览区内的 upload-cover 卡片。
        trigger = await _first_visible(
            page.locator(".cover-plugin-preview .upload-cover"),
            "视频封面入口",
        )
        await trigger.click(timeout=5_000)

        modal_candidates = page.locator(
            ".cover-modal, [role='dialog']:has-text('封面'), "
            "[aria-modal='true']:has-text('封面')"
        )
        modal = await _first_visible(modal_candidates, "视频封面编辑器")
        image_inputs = modal.locator("input[type=file][accept*='image']")
        try:
            await image_inputs.first.wait_for(state="attached", timeout=20_000)
        except Exception as exc:
            raise XhsNativeAdapterError(
                "小红书视频封面编辑器加载超时"
            ) from exc
        if await image_inputs.count() != 1:
            raise XhsNativeAdapterError(
                "小红书视频封面编辑器未返回唯一图片上传控件"
            )
        image_input = image_inputs.first
        await image_input.set_input_files(cover_path)
        selected_count = await image_input.evaluate(
            "node => node.files ? node.files.length : 0"
        )
        if int(selected_count or 0) != 1:
            raise XhsNativeAdapterError("小红书视频封面文件回读失败")
        await page.wait_for_timeout(800)

        confirm_candidates = modal.get_by_role(
            "button",
            name=re.compile(r"^(完成|确认|确定)$"),
        )
        confirm = await _first_visible(confirm_candidates, "视频封面确认按钮")
        if not await confirm.is_enabled():
            raise XhsNativeAdapterError("小红书视频封面确认按钮不可用")
        await confirm.click(timeout=5_000)
        try:
            await modal.wait_for(state="hidden", timeout=15_000)
        except Exception as exc:
            raise XhsNativeAdapterError(
                "小红书视频封面确认后编辑器未关闭"
            ) from exc
        return True

    async def fill_content(self, page) -> dict[str, Any]:
        """打开官方发布页并完成素材、标题、正文回填与本地回读。"""

        await page.goto(
            XHS_PUBLISH_URL,
            wait_until="domcontentloaded",
            timeout=45_000,
        )
        await page.wait_for_timeout(800)
        await self._select_content_type(page)
        selected_count = await self._upload_media(page)
        readback = await self._fill_text_fields(page)
        cover_applied = await self._set_video_cover(page)
        readback.update(
            {
                "contentType": self.content_type,
                "mediaCount": selected_count,
                "imageCount": selected_count if self.content_type == "article" else 0,
                "videoCount": selected_count if self.content_type == "video" else 0,
                "tags": list(self.contract["topics"]),
                "coverMode": self.contract["coverMode"],
                "coverApplied": (
                    bool(self.contract["coverPath"])
                    if self.content_type == "article"
                    else cover_applied
                ),
                "executionBackend": self.contract["executionBackend"],
            }
        )
        return readback

    async def fill_article(self, page) -> dict[str, Any]:
        """兼容既有调用；新代码应调用 :meth:`fill_content`。"""

        if self.content_type != "article":
            raise XhsNativeAdapterError("当前任务不是小红书图文任务")
        return await self.fill_content(page)

    async def fill_video(self, page) -> dict[str, Any]:
        if self.content_type != "video":
            raise XhsNativeAdapterError("当前任务不是小红书视频任务")
        return await self.fill_content(page)

    async def fill_official_topics(self, page) -> list[str]:
        topics = self.contract["topics"]
        if not topics:
            return []
        editor = await _first_visible(
            page.locator(".tiptap.ProseMirror"),
            "正文编辑器",
        )
        for topic in topics:
            await editor.click(timeout=3_000)
            await page.keyboard.press("End")
            await page.keyboard.insert_text(f" #{topic}")
            await page.wait_for_timeout(700)
            candidates = page.locator(".tippy-box .items .item")
            matched = []
            for index in range(await candidates.count()):
                candidate = candidates.nth(index)
                try:
                    names = candidate.locator(".name")
                    if (
                        await candidate.is_visible()
                        and await _is_actual_viewport_visible(candidate)
                        and await names.count() == 1
                        and _topic_candidate_matches(
                            await names.first.inner_text(), topic
                        )
                    ):
                        matched.append(candidate)
                except Exception:
                    continue
            if len(matched) != 1:
                raise XhsNativeAdapterError(
                    f"小红书未返回唯一官方话题候选：#{topic}"
                )
            await matched[0].click(timeout=3_000)
            await page.wait_for_timeout(500)
        nodes = editor.locator("a.tiptap-topic")
        node_texts = [
            _normalized(await nodes.nth(index).inner_text())
            for index in range(await nodes.count())
        ]
        if len(node_texts) < len(topics):
            raise XhsNativeAdapterError("小红书官方话题节点回读不足")
        return node_texts

    async def _set_exact_declaration(
        self,
        page,
        label: str,
        expected: bool,
    ) -> None:
        if not expected:
            return
        trigger = await _first_visible(
            page.get_by_text(label, exact=True),
            f"声明控件“{label}”",
        )
        await trigger.click(timeout=5_000)
        await page.wait_for_timeout(400)
        if label not in _normalized(
            await page.locator("body").inner_text(timeout=3_000)
        ):
            raise XhsNativeAdapterError(f"小红书声明回读失败：{label}")

    async def set_declarations(self, page) -> None:
        if self.contract["aiDeclaration"]:
            trigger = await _first_visible(
                page.get_by_text("添加内容类型声明", exact=True),
                "AI 声明入口",
            )
            await trigger.click(timeout=5_000)
            await self._set_exact_declaration(page, _AI_LABEL, True)
        await self._set_exact_declaration(
            page,
            "原创声明",
            self.contract["originalDeclaration"],
        )

    async def set_schedule(self, page, target: datetime) -> str:
        wrapper = page.locator(".post-time-wrapper").first
        await wrapper.wait_for(state="visible", timeout=10_000)
        await wrapper.scroll_into_view_if_needed(timeout=5_000)
        checkbox = wrapper.locator("input[type='checkbox']").first
        if not await checkbox.count():
            raise XhsNativeAdapterError("小红书定时开关无法唯一识别")
        if not await checkbox.is_checked():
            # 平台把原生 checkbox 缩成 0 尺寸，用户真正点击的是
            # d-switch-simulator。直接 check 隐藏 input 会在视口判定上失败。
            switch = await _first_visible(
                wrapper.locator(".d-switch-simulator"),
                "定时发布开关",
            )
            await switch.click(timeout=5_000)
            await page.wait_for_timeout(300)
        if not await checkbox.is_checked():
            raise XhsNativeAdapterError("小红书定时开关开启后回读失败")

        field = wrapper.locator(".date-picker-container input.d-text").first
        await field.wait_for(state="visible", timeout=8_000)
        expected = target.strftime("%Y-%m-%d %H:%M")
        await field.fill(expected, timeout=5_000)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(700)
        actual = _normalized(await field.input_value())
        if actual != expected or not await checkbox.is_checked():
            raise XhsNativeAdapterError(
                f"小红书定时时间回读不一致：目标={expected}，实际={actual or '空'}"
            )
        return actual

    async def verify_immediate_publish(self, page) -> None:
        """未设置定时时只读确认平台没有意外开启定时。"""

        wrapper = page.locator(".post-time-wrapper").first
        if not await wrapper.count():
            return
        checkbox = wrapper.locator("input[type='checkbox']").first
        if await checkbox.count() and await checkbox.is_checked():
            raise XhsNativeAdapterError(
                "任务要求立即发布，但平台定时开关已开启，已安全停止"
            )

    async def final_button(self, page, expected_label: str = "定时发布"):
        selectors = (
            page.locator(
                f"xhs-publish-btn[submit-text='{expected_label}']"
            ),
            page.get_by_role("button", name=expected_label, exact=True),
        )
        for candidates in selectors:
            visible = []
            for index in range(await candidates.count()):
                candidate = candidates.nth(index)
                try:
                    if await candidate.is_visible():
                        visible.append(candidate)
                except Exception:
                    continue
            if len(visible) > 1:
                raise XhsNativeAdapterError(
                    f"小红书最终“{expected_label}”按钮存在多个可见候选"
                )
            if len(visible) == 1:
                if not await visible[0].is_enabled():
                    raise XhsNativeAdapterError(
                        f"小红书最终“{expected_label}”按钮当前不可用"
                    )
                return visible[0]
        raise XhsNativeAdapterError(
            f"小红书最终“{expected_label}”按钮无法唯一识别"
        )

    async def read_final_button(self, button) -> dict[str, Any]:
        return await button.evaluate(
            """node => ({
                tag: node.tagName,
                text: node.innerText || '',
                attrs: {
                    submitText: node.getAttribute('submit-text'),
                    submitDisabled: node.getAttribute('submit-disabled'),
                },
            })"""
        )

    async def submit_final_button(self, button) -> None:
        await button.click(timeout=10_000)
