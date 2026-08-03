# -*- coding: utf-8 -*-
"""一键发的真实平台预检执行器。

此模块只使用一键发保存的官方会话：上传测试素材、填写表单并核对页面。
它明确禁止点击“发表”“发布”“保存草稿”“预览”等会产生内容结果的按钮。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from html import escape, unescape
from pathlib import Path
import re
from urllib.parse import parse_qs, urlparse

from . import account_service
from .paths import COOKIE_DIR


_XHS_PUBLISH_URL = "https://creator.xiaohongshu.com/publish/publish?source=official"
_WECHAT_HOME_URL = "https://mp.weixin.qq.com/"
_VIDEO_CHANNEL_HOME_URL = "https://channels.weixin.qq.com/platform/"
_VIDEO_CHANNEL_VIDEO_URL = "https://channels.weixin.qq.com/platform/post/create"
_DOUYIN_UPLOAD_URL = "https://creator.douyin.com/creator-micro/content/upload"
_KUAISHOU_VIDEO_URL = "https://cp.kuaishou.com/article/publish/video"
_BILIBILI_VIDEO_URL = "https://member.bilibili.com/platform/upload/video/frame?page_from=creative_home_top_upload"
_BILIBILI_ARTICLE_URL = "https://member.bilibili.com/platform/upload/text/new-edit"
_TEST_PREFIX = "一键发功能测试"
_DOUYIN_LOCATION_TRIGGER_SELECTORS = (
    'div.semi-select span:has-text("输入地理位置")',
    'div.semi-select span:has-text("添加位置")',
    '[role="combobox"]:has-text("输入地理位置")',
    '[role="combobox"]:has-text("添加位置")',
)
_DOUYIN_LOCATION_INPUT_SELECTORS = (
    'div[role="listbox"] input',
    '.semi-select-dropdown input',
    '.semi-portal input[placeholder*="搜索"]',
    'input[placeholder*="地理位置"]',
    'input[placeholder*="位置"]',
)
_DOUYIN_LOCATION_OPTION_SELECTORS = (
    'div[role="listbox"] [role="option"]',
    '.semi-select-option-list .semi-select-option',
    '[role="listbox"] [class*="option"]',
)
_WECHAT_AUTHOR_ONLY_OPERATION = "wechat_author_only"
_WECHAT_COVER_ONLY_OPERATION = "wechat_cover_only"
_WECHAT_AUTHOR_TRIGGER_SELECTORS = (
    "#js_author_area input#author",
    "#js_author_area input.js_author[name='author']",
    "#js_author_area [role='combobox']",
    "#js_author_area .js_author_select",
    ".js_author_area [role='combobox']",
    "[data-field='author'] [role='combobox']",
    "[data-testid='author-selector']",
)
_WECHAT_AUTHOR_LIST_SELECTORS = (
    "#js_author_area .js_author_list .weui-desktop-dropdown-menu",
    "#js_author_area .js_author_list .weui-desktop-dropdown__list",
    ".js_author_select_list[role='listbox']",
    ".js_author_select_list",
    "[data-field='author-options'][role='listbox']",
    "[data-testid='author-options']",
    ".weui-desktop-popover__content [role='listbox']",
)
_WECHAT_AUTHOR_OPTION_SELECTORS = (
    "li.js_item[data-idx].weui-desktop-dropdown__list-ele",
    "li.js_item[data-idx]",
    "[data-author-id][role='option']",
    "[data-author-id]",
    ".js_author_item[role='option']",
    ".js_author_item",
    "[role='option']",
)
_WECHAT_AUTHOR_EMPTY_MARKERS = (
    "暂无作者",
    "没有可用作者",
    "无可用作者",
    "作者列表为空",
)
_WECHAT_AUTHOR_NON_OPTION_MARKERS = (
    "添加作者",
    "新建作者",
    "管理作者",
    "作者管理",
    "设置作者",
    "去设置",
)
_WECHAT_DEFAULT_TEMPLATE = "warm-jade"
_WECHAT_SILICON_EVOLUTION_TEMPLATE = "silicon-evolution-tech-v1"
_WECHAT_MOBILE_TEMPLATES = {
    _WECHAT_SILICON_EVOLUTION_TEMPLATE: {
        "name": "硅基进化科技编辑版",
        "section": (
            "font-family:-apple-system,BlinkMacSystemFont,Segoe UI,PingFang SC,"
            "Hiragino Sans GB,Microsoft YaHei,sans-serif;font-size:16px;"
            "line-height:1.92;color:#263247;word-break:break-word;"
            "background:#ffffff;padding:20px 16px 10px;"
        ),
        "paragraph": (
            "margin:0 0 22px;font-size:16px;line-height:1.92;"
            "letter-spacing:0.02em;color:#263247;text-align:justify;"
        ),
        "heading2": (
            "margin:44px 0 18px;padding:0 0 0 14px;"
            "border-left:4px solid #6557f5;font-size:21px;line-height:1.4;"
            "font-weight:700;color:#111827;letter-spacing:-0.02em;"
        ),
        "heading3": (
            "margin:30px 0 13px;padding-left:10px;border-left:2px solid #aeb8ff;"
            "font-size:18px;line-height:1.45;font-weight:700;color:#344054;"
        ),
        "list": "margin:6px 0 24px;padding-left:1.45em;color:#263247;",
        "list_item": (
            "margin:8px 0;font-size:16px;line-height:1.88;"
            "color:#263247;padding-left:2px;"
        ),
        "quote": (
            "margin:26px 0;padding:16px 18px;border-left:3px solid #aeb8ff;"
            "background:#f7f8ff;color:#475467;font-size:15px;line-height:1.88;"
            "border-radius:4px;"
        ),
        "rule": "border:0;border-top:1px solid #d7deea;margin:36px 0;",
        "link": "#4b46c6",
        "link_border": "#aeb8ff",
        "strong": "#111827",
        "code": "#4b46c6",
        "code_background": "#f2f4ff",
    },
    "warm-jade": {
        "name": "暖纸青墨",
        "section": (
            "font-family:-apple-system,BlinkMacSystemFont,Segoe UI,PingFang SC,"
            "Hiragino Sans GB,Microsoft YaHei,sans-serif;font-size:16px;"
            "line-height:1.92;color:#514d47;word-break:break-word;"
            "background:#fffcf6;padding:20px 16px 10px;"
        ),
        "paragraph": (
            "margin:0 0 22px;font-size:16px;line-height:1.92;"
            "letter-spacing:0.025em;color:#514d47;text-align:justify;"
        ),
        "heading2": (
            "margin:46px 0 20px;padding:0 0 10px 12px;"
            "border-left:3px solid #6f9185;border-bottom:1px solid #e7e0d3;"
            "font-size:20px;line-height:1.45;font-weight:700;color:#315d54;"
        ),
        "heading3": (
            "margin:32px 0 14px;padding-left:10px;border-left:2px solid #c2d0ca;"
            "font-size:17px;line-height:1.55;font-weight:700;color:#4c6f66;"
        ),
        "list": "margin:4px 0 24px;padding-left:1.45em;color:#514d47;",
        "list_item": (
            "margin:9px 0;font-size:16px;line-height:1.88;"
            "color:#514d47;padding-left:2px;"
        ),
        "quote": (
            "margin:26px 0;padding:16px 17px;border-left:3px solid #86a399;"
            "background:#f3f4ee;color:#56615d;font-size:15px;line-height:1.88;"
            "border-radius:4px;"
        ),
        "rule": "border:0;border-top:1px solid #ddd7cc;margin:36px 0;",
        "link": "#3f756a",
        "link_border": "#afc4bd",
        "strong": "#315f56",
        "code": "#46625c",
        "code_background": "#f0efe9",
    },
    "warm-umber": {
        "name": "暖纸赭棕",
        "section": (
            "font-family:-apple-system,BlinkMacSystemFont,Segoe UI,PingFang SC,"
            "Hiragino Sans GB,Microsoft YaHei,sans-serif;font-size:16px;"
            "line-height:1.92;color:#514b45;word-break:break-word;"
            "background:#fcfaf6;padding:20px 16px 10px;"
        ),
        "paragraph": (
            "margin:0 0 22px;font-size:16px;line-height:1.92;"
            "letter-spacing:0.025em;color:#514b45;text-align:justify;"
        ),
        "heading2": (
            "margin:46px 0 20px;padding:10px 0 9px;"
            "border-top:1px solid #d8c7b8;border-bottom:1px solid #e9ded4;"
            "font-size:19px;line-height:1.48;font-weight:700;color:#79563f;"
        ),
        "heading3": (
            "margin:32px 0 14px;font-size:17px;line-height:1.55;"
            "font-weight:700;color:#836248;"
        ),
        "list": "margin:4px 0 24px;padding-left:1.45em;color:#514b45;",
        "list_item": (
            "margin:9px 0;font-size:16px;line-height:1.88;"
            "color:#514b45;padding-left:2px;"
        ),
        "quote": (
            "margin:26px 0;padding:16px 17px;border-left:3px solid #b49278;"
            "background:#f5efe8;color:#62574e;font-size:15px;line-height:1.88;"
            "border-radius:4px;"
        ),
        "rule": "border:0;border-top:1px solid #ded2c7;margin:36px 0;",
        "link": "#8a5f45",
        "link_border": "#ccb5a4",
        "strong": "#76513b",
        "code": "#725a48",
        "code_background": "#f2ece6",
    },
}


class PreflightError(RuntimeError):
    """平台预检无法停在最终发布前时抛出。"""


def _account_for_payload(payload: dict) -> dict:
    platform_type = int(payload.get("type") or 0)
    files = {Path(str(value)).name for value in payload.get("accountList") or []}
    for account in account_service.list_accounts():
        if int(account.get("type") or 0) == platform_type and Path(str(account.get("filePath") or "")).name in files:
            return account
    raise PreflightError("未找到一键发已登录账号，请先在账号管理中完成登录")


def _wechat_template_for_payload(payload: dict, account: dict | None = None) -> str:
    """根据明确内容包标记或账号身份选择公众号正文模板。"""

    requested = str(payload.get("wechatArticleTemplate") or "").strip()
    resolved_account = account
    if resolved_account is None:
        try:
            resolved_account = _account_for_payload(payload)
        except PreflightError:
            resolved_account = None

    names = {
        _normalized_page_text(value)
        for value in (
            (resolved_account or {}).get("profileName"),
            (resolved_account or {}).get("userName"),
        )
        if _normalized_page_text(value)
    }
    is_silicon_evolution = any("硅基进化" in name for name in names)

    if requested:
        if requested not in _WECHAT_MOBILE_TEMPLATES:
            raise PreflightError(f"公众号正文模板不支持：{requested}")
        if requested == _WECHAT_SILICON_EVOLUTION_TEMPLATE and not is_silicon_evolution:
            raise PreflightError("硅基进化科技编辑版仅可用于硅基进化公众号账号")
        return requested
    if is_silicon_evolution:
        return _WECHAT_SILICON_EVOLUTION_TEMPLATE
    return _WECHAT_DEFAULT_TEMPLATE


def _storage_state(account: dict) -> Path:
    path = COOKIE_DIR / Path(str(account.get("filePath") or "")).name
    if not path.is_file():
        raise PreflightError("一键发本地登录会话不存在，请重新登录")
    return path


def _payload_text(payload: dict, suffix: str) -> tuple[str, str]:
    title = " ".join(str(payload.get("title") or "").split()) or f"{_TEST_PREFIX}｜{suffix}"
    description = " ".join(str(payload.get("description") or "").split())
    if not description:
        description = f"{_TEST_PREFIX}：仅核对上传和表单填写链路，不保存草稿，不公开发布。"
    return title[:64], description[:900]


def _normalized_page_text(value: object) -> str:
    """统一页面回读文本，忽略富文本编辑器的零宽占位字符。"""

    return " ".join(str(value or "").replace("\u200b", "").split())


def _douyin_location_candidate_name(value: object) -> str:
    """从抖音地点候选文本中提取主名称。

    平台通常把地点名和地址分成两行；只用首个非空行做唯一精确匹配，
    避免把“北海”误选成同名商户或其它城市的地点。
    """

    for line in str(value or "").splitlines():
        normalized = _normalized_page_text(line)
        if normalized:
            return normalized
    return ""


def _douyin_exact_location_indexes(keyword: object, candidates: list[object]) -> list[int]:
    """返回与用户输入同名的候选序号；上层必须要求结果唯一。"""

    expected = _normalized_page_text(keyword).casefold()
    if not expected:
        return []
    return [
        index
        for index, value in enumerate(candidates)
        if _douyin_location_candidate_name(value).casefold() == expected
    ]


def _douyin_location_match_indexes(
    expected: object,
    candidates: list[object],
) -> list[int]:
    """按 POI ID，其次名称与地址，匹配用户在一键发中明确选择的地点。"""

    if not isinstance(expected, dict):
        return []
    expected_id = _normalized_page_text(
        expected.get("poiId") or expected.get("poi_id") or expected.get("id")
    )
    expected_name = _normalized_page_text(expected.get("name")).casefold()
    if not expected_id or not expected_name:
        return []

    structured = [value if isinstance(value, dict) else {} for value in candidates]
    visible_ids = [
        _normalized_page_text(
            value.get("poiId") or value.get("poi_id") or value.get("id")
        )
        for value in structured
    ]
    if any(visible_ids):
        return [index for index, value in enumerate(visible_ids) if value == expected_id]

    name_indexes = [
        index
        for index, value in enumerate(structured)
        if _normalized_page_text(value.get("name")).casefold() == expected_name
    ]
    if len(name_indexes) <= 1:
        return name_indexes

    expected_address = _normalized_page_text(expected.get("address")).casefold()
    if not expected_address:
        return name_indexes
    address_indexes = []
    for index in name_indexes:
        address = _normalized_page_text(structured[index].get("address")).casefold()
        if address and (
            address == expected_address
            or expected_address in address
            or address in expected_address
        ):
            address_indexes.append(index)
    return address_indexes or name_indexes


def _wechat_payload_text(payload: dict) -> tuple[str, str]:
    """公众号正文保留完整长度和换行，不沿用短内容平台的 900 字压缩。"""

    title = " ".join(str(payload.get("title") or "").split()) or f"{_TEST_PREFIX}｜公众号预检"
    description = str(payload.get("description") or "").strip()
    if not description:
        description = f"{_TEST_PREFIX}：仅核对上传和表单填写链路，不保存草稿，不公开发布。"
    return title[:64], description


def _wechat_original_requested(payload: dict) -> bool:
    """只有表单明确传入布尔值 true 时才允许进入作者流程。"""

    value = payload.get("originalDeclaration", False)
    if not isinstance(value, bool):
        raise PreflightError("公众号原创声明必须是明确的布尔值")
    return value


def _wechat_inline_markdown(value: str, template: dict | None = None) -> str:
    """转义任意原始 HTML，只保留原文已有的链接、加粗和行内代码。"""

    styles = template or _WECHAT_MOBILE_TEMPLATES[_WECHAT_DEFAULT_TEMPLATE]
    rendered = escape(value, quote=True)
    rendered = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        lambda match: (
            f'<a href="{match.group(2)}" target="_blank" rel="noopener noreferrer" '
            f'style="color:{styles["link"]};text-decoration:none;'
            f'border-bottom:1px solid {styles["link_border"]};">'
            f"{match.group(1)}</a>"
        ),
        rendered,
    )
    rendered = re.sub(
        r"\*\*([^*]+)\*\*",
        rf'<strong style="font-weight:700;color:{styles["strong"]};">\1</strong>',
        rendered,
    )
    return re.sub(
        r"`([^`]+)`",
        rf'<code style="font-family:SFMono-Regular,Consolas,monospace;font-size:14px;'
        rf'color:{styles["code"]};background:{styles["code_background"]};'
        rf'padding:2px 5px;border-radius:3px;">\1</code>',
        rendered,
    )


def _wechat_markdown_to_html(
    value: str,
    template_id: str = _WECHAT_DEFAULT_TEMPLATE,
) -> str:
    """把内容包 Markdown 套入固定的公众号移动端长文阅读模板。"""

    if template_id not in _WECHAT_MOBILE_TEMPLATES:
        raise ValueError(f"未知公众号阅读模板：{template_id}")
    styles = _WECHAT_MOBILE_TEMPLATES[template_id]
    blocks: list[str] = []
    paragraphs: list[str] = []
    list_items: list[str] = []
    quotes: list[str] = []

    def flush_paragraph() -> None:
        if paragraphs:
            blocks.append(
                f'<p style="{styles["paragraph"]}">'
                f"{_wechat_inline_markdown(' '.join(paragraphs), styles)}</p>"
            )
            paragraphs.clear()

    def flush_list() -> None:
        if list_items:
            items = "".join(
                f'<li style="{styles["list_item"]}">'
                f"{_wechat_inline_markdown(item, styles)}</li>"
                for item in list_items
            )
            blocks.append(
                f'<ul style="{styles["list"]}">{items}</ul>'
            )
            list_items.clear()

    def flush_quote() -> None:
        if quotes:
            blocks.append(
                f'<blockquote style="{styles["quote"]}">'
                f"{_wechat_inline_markdown(' '.join(quotes), styles)}</blockquote>"
            )
            quotes.clear()

    for raw_line in str(value or "").splitlines():
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            flush_list()
            flush_quote()
            continue
        if line == "---":
            flush_paragraph()
            flush_list()
            flush_quote()
            blocks.append(f'<hr style="{styles["rule"]}">')
            continue
        if line.startswith("## "):
            flush_paragraph()
            flush_list()
            flush_quote()
            blocks.append(
                f'<h2 style="{styles["heading2"]}">'
                f"{_wechat_inline_markdown(line[3:].strip(), styles)}</h2>"
            )
            continue
        if line.startswith("### "):
            flush_paragraph()
            flush_list()
            flush_quote()
            blocks.append(
                f'<h3 style="{styles["heading3"]}">'
                f"{_wechat_inline_markdown(line[4:].strip(), styles)}</h3>"
            )
            continue
        if line.startswith("- "):
            flush_paragraph()
            flush_quote()
            list_items.append(line[2:].strip())
            continue
        if line.startswith("> "):
            flush_paragraph()
            flush_list()
            quotes.append(line[2:].strip())
            continue
        flush_list()
        flush_quote()
        paragraphs.append(line)
    flush_paragraph()
    flush_list()
    flush_quote()
    return (
        '<section data-oneclick-template="wechat-mobile-editorial-v2" '
        f'data-oneclick-theme="{template_id}" style="{styles["section"]}">'
        + "".join(blocks)
        + "</section>"
    )


def _wechat_markdown_visible_text(value: str) -> str:
    """得到富文本在页面上应显示的全文，用于 DOM 回读比对。"""

    visible_lines: list[str] = []
    for raw_line in str(value or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("## "):
            line = line[3:].strip()
        elif line.startswith("### "):
            line = line[4:].strip()
        elif line.startswith("- "):
            line = line[2:].strip()
        elif line.startswith("> "):
            line = line[2:].strip()
        elif line == "---":
            continue
        line = re.sub(r"\[([^\]]+)\]\(https?://[^)\s]+\)", r"\1", line)
        line = re.sub(r"\*\*([^*]+)\*\*", r"\1", line)
        line = re.sub(r"`([^`]+)`", r"\1", line)
        visible_lines.append(line)
    return "\n".join(visible_lines)


async def _wechat_fill_rich_text(
    editor,
    markdown_text: str,
    template_id: str = _WECHAT_DEFAULT_TEMPLATE,
) -> str:
    """写入结构化富文本并返回预期的可见全文；不提交编辑器内容。"""

    html = _wechat_markdown_to_html(markdown_text, template_id)
    if not html:
        raise PreflightError("公众号正文转换后为空")
    await editor.evaluate(
        """(element, value) => {
            element.innerHTML = value;
            element.dispatchEvent(new InputEvent('input', {
                bubbles: true,
                inputType: 'insertText',
                data: null,
            }));
        }""",
        html,
    )
    return _wechat_markdown_visible_text(markdown_text)


async def _set_dom_value(locator, value: str) -> None:
    """兼容平台编辑器的受控输入框，只派发输入事件、不提交表单。"""

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


async def _xhs_preflight(page, payload: dict) -> str:
    # 延迟导入以避免账号预检模块与平台适配器形成循环依赖。
    from .xhs_native_adapter import XhsNativeAdapter, XhsNativeAdapterError

    try:
        adapter = XhsNativeAdapter(payload)
        readback = await adapter.fill_content(page)
        topic_nodes = await adapter.fill_official_topics(page)
        await adapter.set_declarations(page)
        if payload.get("enableTimer") is True:
            target = datetime.strptime(
                str(payload.get("scheduleTime") or "").replace("T", " "),
                "%Y-%m-%d %H:%M",
            )
            schedule_readback = await adapter.set_schedule(page, target)
        else:
            await adapter.verify_immediate_publish(page)
            schedule_readback = "立即发布"
    except XhsNativeAdapterError as exc:
        raise PreflightError(str(exc)) from exc

    label = "图文" if readback["contentType"] == "article" else "视频"
    # 安全边界：预检允许回填并回读素材、文本、官方话题、声明与定时
    # 配置；绝不查找或点击草稿、预览和最终发布按钮，也不处理平台
    # 发布确认弹窗。
    return (
        f"小红书{label}素材已由平台回读{readback['mediaCount']}项，"
        f"标题、正文、{len(topic_nodes)}个官方话题、"
        f"声明配置和发布时间（{schedule_readback}）已回读；"
        "未保存草稿、未预览、未发布"
    )


def _wechat_dialog_is_in_viewport(snapshot: dict) -> bool:
    """只把实际占据视口且未被隐藏的弹窗视为阻断。"""

    if snapshot.get("hidden") or snapshot.get("ariaHidden"):
        return False
    if snapshot.get("display") == "none" or snapshot.get("visibility") != "visible":
        return False
    try:
        if float(snapshot.get("opacity") or 0) <= 0:
            return False
        width = float(snapshot.get("width") or 0)
        height = float(snapshot.get("height") or 0)
        intersection_width = float(snapshot.get("intersectionWidth") or 0)
        intersection_height = float(snapshot.get("intersectionHeight") or 0)
    except (TypeError, ValueError):
        return False
    return (
        width > 0
        and height > 0
        and intersection_width > 0
        and intersection_height > 0
        and bool(snapshot.get("hitTestMatches"))
    )


def _wechat_normalize_image_url(value: object) -> str:
    """统一编辑器图片地址与弹层 CSS background-image，忽略瞬时查询参数。"""

    normalized = unescape(str(value or "")).strip()
    if normalized.startswith("url(") and normalized.endswith(")"):
        normalized = normalized[4:-1].strip()
    normalized = normalized.strip("\"'")
    return normalized.split("?", 1)[0]


async def _wechat_blocking_dialog_text(page) -> str:
    """读取真正遮挡当前视口的公众号提示，但绝不点击账号设置入口。"""

    dialogs = page.locator(".weui-desktop-dialog")
    texts: list[str] = []
    for index in range(await dialogs.count()):
        dialog = dialogs.nth(index)
        snapshot = await dialog.evaluate(
            """element => {
                const style = getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                const viewportWidth = window.innerWidth || document.documentElement.clientWidth;
                const viewportHeight = window.innerHeight || document.documentElement.clientHeight;
                const left = Math.max(0, rect.left);
                const top = Math.max(0, rect.top);
                const right = Math.min(viewportWidth, rect.right);
                const bottom = Math.min(viewportHeight, rect.bottom);
                const x = Math.min(Math.max(left + Math.max(0, right - left) / 2, 0), Math.max(0, viewportWidth - 1));
                const y = Math.min(Math.max(top + Math.max(0, bottom - top) / 2, 0), Math.max(0, viewportHeight - 1));
                const hit = document.elementFromPoint(x, y);
                return {
                    hidden: element.hidden,
                    ariaHidden: element.getAttribute('aria-hidden') === 'true',
                    display: style.display,
                    visibility: style.visibility,
                    opacity: style.opacity,
                    width: rect.width,
                    height: rect.height,
                    intersectionWidth: Math.max(0, right - left),
                    intersectionHeight: Math.max(0, bottom - top),
                    hitTestMatches: Boolean(
                        hit && (hit === element || element.contains(hit))
                    ),
                };
            }"""
        )
        if not _wechat_dialog_is_in_viewport(snapshot):
            continue
        text = _normalized_page_text(await dialog.inner_text())
        if text:
            texts.append(text)
    blockers = [
        text
        for text in texts
        if any(marker in text for marker in ("尚未实名", "未设置头像和名称", "未授权使用切换账号能力"))
    ]
    return "；".join(blockers)


async def _wechat_open_article_editor(page) -> None:
    """打开当前一键发会话对应的公众号文章编辑器，不填写或提交任何字段。"""

    await page.goto(_WECHAT_HOME_URL, wait_until="domcontentloaded", timeout=45_000)
    await page.wait_for_timeout(700)
    token = parse_qs(urlparse(page.url).query).get("token", [""])[0]
    if not token:
        raise PreflightError("公众号后台未返回编辑授权信息，请重新登录")
    await page.goto(
        f"https://mp.weixin.qq.com/cgi-bin/appmsg?action=edit&type=10&lang=zh_CN&token={token}",
        wait_until="domcontentloaded",
        timeout=45_000,
    )
    await page.wait_for_timeout(1_000)
    blocker = await _wechat_blocking_dialog_text(page)
    if blocker:
        raise PreflightError("公众号编辑器存在账号前置阻断：" + blocker)


async def _wechat_first_visible_nodes(root, selectors: tuple[str, ...]) -> list:
    """按稳定性顺序返回首组可见节点，避免多个宽泛选择器交叉误选。"""

    for selector in selectors:
        matches = root.locator(selector)
        visible = []
        for index in range(await matches.count()):
            node = matches.nth(index)
            try:
                if await node.is_visible():
                    visible.append(node)
            except Exception:
                continue
        if visible:
            return visible
    return []


async def _wechat_wait_visible_nodes(
    root,
    selectors: tuple[str, ...],
    page,
    *,
    attempts: int = 12,
) -> list:
    """等待异步作者弹层加载；超时后交由上层给出可区分的失败原因。"""

    for _ in range(attempts):
        nodes = await _wechat_first_visible_nodes(root, selectors)
        if nodes:
            return nodes
        await page.wait_for_timeout(250)
    return []


def _wechat_author_display_name(value: object) -> str:
    """从作者选项中提取显示名，丢弃“默认/已选择”等界面状态文字。"""

    lines = [
        _normalized_page_text(line)
        for line in str(value or "").splitlines()
        if _normalized_page_text(line)
    ]
    metadata = {"作者", "默认", "默认作者", "已选择", "当前作者"}
    for line in lines:
        if line in metadata:
            continue
        cleaned = line
        for marker in ("默认作者", "当前作者", "已选择", "默认"):
            cleaned = re.sub(rf"(?:\s*{marker}\s*)$", "", cleaned).strip()
        if cleaned:
            return cleaned
    return ""


async def _wechat_control_readback(locator) -> str:
    """兼容只读输入框、按钮和组合框的作者显示值。"""

    try:
        value = await locator.input_value()
        if _normalized_page_text(value):
            return _normalized_page_text(value)
    except Exception:
        pass
    try:
        return _normalized_page_text(await locator.inner_text())
    except Exception:
        return ""


async def _wechat_author_option_available(option) -> bool:
    """只接受作者列表内明确可点击、非管理入口的选项。"""

    try:
        if not await option.is_visible():
            return False
        if not await option.is_enabled():
            return False
        if await option.get_attribute("aria-disabled") == "true":
            return False
        if await option.get_attribute("disabled") is not None:
            return False
        text = _normalized_page_text(await option.inner_text())
    except Exception:
        return False
    if not _wechat_author_display_name(text):
        return False
    return not any(marker in text for marker in _WECHAT_AUTHOR_NON_OPTION_MARKERS)


async def _wechat_select_default_author(page) -> str:
    """选择当前账号作者列表中的第一个可用项，并强制回读确认。"""

    triggers = await _wechat_first_visible_nodes(
        page,
        _WECHAT_AUTHOR_TRIGGER_SELECTORS,
    )
    if not triggers:
        raise PreflightError("公众号作者控件未识别，页面结构可能已变化")
    if len(triggers) != 1:
        raise PreflightError("公众号作者控件不唯一，预检拒绝猜测")
    trigger = triggers[0]
    await trigger.click(timeout=8_000)
    await page.wait_for_timeout(400)

    author_lists = await _wechat_wait_visible_nodes(
        page,
        _WECHAT_AUTHOR_LIST_SELECTORS,
        page,
    )
    if not author_lists:
        raise PreflightError("公众号作者列表加载失败或控件结构已变化")
    if len(author_lists) != 1:
        raise PreflightError("公众号出现多个作者列表，预检拒绝猜测")
    author_list = author_lists[0]
    list_text = _normalized_page_text(await author_list.inner_text())
    if any(marker in list_text for marker in _WECHAT_AUTHOR_EMPTY_MARKERS):
        raise PreflightError("当前公众号账号没有可用作者")
    options = await _wechat_wait_visible_nodes(
        author_list,
        _WECHAT_AUTHOR_OPTION_SELECTORS,
        page,
    )
    available = [
        option
        for option in options
        if await _wechat_author_option_available(option)
    ]
    if not available:
        raise PreflightError("公众号作者选项未识别或列表尚未加载完成")

    first_author = available[0]
    expected_name = _wechat_author_display_name(await first_author.inner_text())
    if not expected_name:
        raise PreflightError("公众号第一个作者选项缺少可回读名称")
    await first_author.click(timeout=8_000)
    await page.wait_for_timeout(300)

    updated_triggers = await _wechat_first_visible_nodes(
        page,
        _WECHAT_AUTHOR_TRIGGER_SELECTORS,
    )
    if len(updated_triggers) != 1:
        raise PreflightError("公众号作者选择后控件状态不唯一")
    actual_name = await _wechat_control_readback(updated_triggers[0])
    if expected_name not in actual_name:
        raise PreflightError(
            f"公众号作者回读不一致：期望“{expected_name}”，页面显示“{actual_name or '空'}”"
        )
    return expected_name


def _validate_wechat_author_payload(payload: dict) -> None:
    """强制仅作者通道使用单一公众号账号和显式 dry-run 约束。"""

    if int(payload.get("type") or 0) != 10:
        raise PreflightError("公众号仅作者预检只允许 type=10")
    if str(payload.get("contentType") or "") != "article":
        raise PreflightError("公众号仅作者预检只允许 contentType=article")
    if str(payload.get("preflightOperation") or "") != _WECHAT_AUTHOR_ONLY_OPERATION:
        raise PreflightError(
            f"公众号仅作者预检必须设置 preflightOperation={_WECHAT_AUTHOR_ONLY_OPERATION}"
        )
    if str(payload.get("runtimeMode") or "") != "preflight":
        raise PreflightError("公众号仅作者预检必须保持 runtimeMode=preflight")
    if payload.get("debugDryRun") is not True:
        raise PreflightError("公众号仅作者预检必须保持 debugDryRun=true")
    if payload.get("publishAllowed") not in (None, False):
        raise PreflightError("公众号仅作者预检不允许 publishAllowed=true")
    account_list = payload.get("accountList")
    if not isinstance(account_list, (list, tuple)) or len(account_list) != 1:
        raise PreflightError("公众号仅作者预检必须且只能指定一个一键发已登录账号")
    if not str(account_list[0] or "").strip():
        raise PreflightError("公众号仅作者预检的账号标识不能为空")


def _validate_wechat_cover_payload(payload: dict) -> Path:
    """强制仅封面通道使用单一公众号账号和显式 dry-run 约束。"""

    if int(payload.get("type") or 0) != 10:
        raise PreflightError("公众号仅封面预检只允许 type=10")
    if str(payload.get("contentType") or "") != "article":
        raise PreflightError("公众号仅封面预检只允许 contentType=article")
    if str(payload.get("preflightOperation") or "") != _WECHAT_COVER_ONLY_OPERATION:
        raise PreflightError(
            f"公众号仅封面预检必须设置 preflightOperation={_WECHAT_COVER_ONLY_OPERATION}"
        )
    if str(payload.get("runtimeMode") or "") != "preflight":
        raise PreflightError("公众号仅封面预检必须保持 runtimeMode=preflight")
    if payload.get("debugDryRun") is not True:
        raise PreflightError("公众号仅封面预检必须保持 debugDryRun=true")
    if payload.get("publishAllowed") not in (None, False):
        raise PreflightError("公众号仅封面预检不允许 publishAllowed=true")
    account_list = payload.get("accountList")
    if not isinstance(account_list, (list, tuple)) or len(account_list) != 1:
        raise PreflightError("公众号仅封面预检必须且只能指定一个一键发已登录账号")
    if not str(account_list[0] or "").strip():
        raise PreflightError("公众号仅封面预检的账号标识不能为空")
    cover = Path(str(payload.get("coverPath") or "")).resolve()
    if not cover.is_file():
        raise PreflightError("公众号仅封面预检需要可读取的本地封面图片")
    return cover


async def _wechat_author_only_preflight(page, payload: dict) -> str:
    """仅选择并回读作者；不填写正文，不处理封面，也不触碰提交类控件。"""

    _validate_wechat_author_payload(payload)
    await _wechat_open_article_editor(page)
    author_name = await _wechat_select_default_author(page)
    return (
        f"公众号作者已选择并回读：{author_name}；"
        "未填写正文、未处理封面、未保存草稿、未预览、未发表"
    )


async def _wechat_cover_only_preflight(page, payload: dict) -> str:
    """仅上传、选择并回读封面，完成后必须回到主题编辑页。"""

    cover = _validate_wechat_cover_payload(payload)
    await _wechat_open_article_editor(page)
    editors = page.locator("div.ProseMirror")
    if await editors.count() < 2:
        raise PreflightError("公众号编辑器字段未加载完成")
    editor = editors.nth(1)
    cover_index = await editor.locator("img").count()
    await _wechat_insert_body_images(page, editor, [cover])
    await _wechat_select_cover_from_content(
        page,
        editor,
        cover_image_index=cover_index,
    )
    await _wechat_remove_temporary_cover(editor, cover_index)
    state = await _wechat_cover_crop_snapshot(page)
    if (
        state.get("dialogVisible")
        or not state.get("coverReady")
        or not state.get("editorVisible")
    ):
        raise PreflightError("公众号封面回读闭环不完整")
    return (
        "公众号封面已选择、裁剪确认并回到主题编辑页；"
        "未填写标题正文、未保存草稿、未预览、未发表"
    )


async def _wechat_cover_crop_snapshot(page) -> dict:
    """读取真实视口中的封面裁剪状态，并只标记唯一可用的确认控件。"""

    return await page.evaluate(
        r"""() => {
          const normalize = value => String(value || '').replace(/\s+/g, ' ').trim();
          const visible = element => {
            if (!element) return false;
            const rect = element.getBoundingClientRect();
            const style = getComputedStyle(element);
            return rect.width > 0 && rect.height > 0
              && rect.bottom > 0 && rect.right > 0
              && rect.top < innerHeight && rect.left < innerWidth
              && style.display !== 'none' && style.visibility !== 'hidden'
              && Number(style.opacity || 1) > 0;
          };
          const rendered = element => {
            if (!element) return false;
            const rect = element.getBoundingClientRect();
            const style = getComputedStyle(element);
            return rect.width > 0 && rect.height > 0
              && style.display !== 'none' && style.visibility !== 'hidden'
              && Number(style.opacity || 1) > 0;
          };
          const enabled = element =>
            !element.disabled
            && element.getAttribute('aria-disabled') !== 'true'
            && !element.classList.contains('disabled')
            && !element.classList.contains('weui-desktop-btn_disabled');
          document.querySelectorAll('[data-oneclick-cover-confirm]')
            .forEach(element => element.removeAttribute('data-oneclick-cover-confirm'));

          const dialogs = Array.from(document.querySelectorAll('.weui-desktop-dialog'))
            .filter(visible);
          const cropDialogs = dialogs.filter(dialog => {
            const text = normalize(dialog.innerText);
            return text.includes('编辑封面') || text.includes('裁剪封面');
          });
          const dialog = cropDialogs.at(-1) || null;
          const interactive = dialog
            ? Array.from(dialog.querySelectorAll(
                'button,a,[role="button"],.weui-desktop-btn'
              ))
            : [];
          const labels = ['完成', '确定', '确认'];
          const matching = Array.from(new Set(interactive)).filter(element =>
            labels.includes(normalize(element.innerText || element.textContent))
          );
          const usable = matching.filter(element => visible(element) && enabled(element));
          const hidden = matching.filter(element => !visible(element) || !enabled(element));
          if (usable.length === 1) {
            usable[0].setAttribute('data-oneclick-cover-confirm', '1');
          }
          const loading = dialog
            ? Array.from(dialog.querySelectorAll(
                '[aria-busy="true"],[class*="loading"],[class*="spinner"],'
                + '.weui-desktop-loading,.weui-desktop-loading__icon'
              )).some(visible)
            : false;
          const coverArea = document.querySelector('#js_cover_area');
          const coverVisuals = coverArea
            ? Array.from(coverArea.querySelectorAll(
                'img[src],img[data-src],.js_share_type_image,'
                + '.js_cover_preview,.js_cover_preview_new,.js_cover_preview_square,'
                + '.first_appmsg_cover,[class*="cover_preview"],[class*="cover_img"]'
              ))
            : [];
          const coverVisualReady = coverVisuals.some(element => {
            if (!rendered(element)) return false;
            const tag = element.tagName?.toLowerCase() || '';
            const source = tag === 'img'
              ? (element.currentSrc || element.getAttribute('src')
                || element.getAttribute('data-src') || '')
              : '';
            const background = getComputedStyle(element).backgroundImage || '';
            const realSource = Boolean(
              source
              && !source.startsWith('data:image/svg+xml')
            );
            const realBackground = Boolean(
              background
              && background !== 'none'
              && background !== 'url("")'
              && background !== "url('')"
              && background.includes('url(')
              && !background.includes('data:image/svg+xml')
            );
            return realSource || realBackground;
          });
          const coverLoading = Boolean(coverArea && Array.from(
            coverArea.querySelectorAll(
              '.js_cover_loading,.select-cover__loading__mask,.weui-desktop-loading'
            )
          ).some(rendered));
          const coverReady = coverVisualReady && !coverLoading;
          const editorVisible = Array.from(document.querySelectorAll('div.ProseMirror'))
            .filter(rendered).length >= 2
            && location.pathname.includes('/cgi-bin/appmsg');
          return {
            dialogVisible: Boolean(dialog),
            dialogCount: cropDialogs.length,
            loading,
            usableControls: usable.map(element =>
              normalize(element.innerText || element.textContent)
            ),
            hiddenControls: hidden.map(element =>
              normalize(element.innerText || element.textContent)
            ),
            coverLoading,
            coverReady,
            editorVisible,
          };
        }"""
    )


async def _wechat_wait_cover_crop_ready(
    page,
    *,
    attempts: int = 240,
    interval_ms: int = 250,
) -> tuple[str, dict]:
    """有上限地等待裁剪完成控件或平台自动完成，不把“上一步”当确认。"""

    last: dict = {}
    for _ in range(attempts):
        last = await _wechat_cover_crop_snapshot(page)
        if int(last.get("dialogCount") or 0) > 1:
            raise PreflightError("公众号同时出现多个封面裁剪弹层，预检拒绝猜测")
        controls = list(last.get("usableControls") or [])
        if len(controls) > 1:
            raise PreflightError("公众号封面裁剪出现多个可用确认控件，预检拒绝猜测")
        if controls:
            return "confirm", last
        if (
            not last.get("dialogVisible")
            and last.get("coverReady")
            and last.get("editorVisible")
        ):
            return "auto-complete", last
        await page.wait_for_timeout(interval_ms)
    if last.get("loading"):
        raise PreflightError("公众号封面裁剪内容加载超时，未出现真实可用的完成控件")
    if last.get("hiddenControls"):
        raise PreflightError("公众号封面裁剪完成控件存在但不可见或不可用")
    raise PreflightError("公众号封面裁剪弹层未显示真实可用的完成控件")


async def _wechat_wait_cover_return_to_editor(
    page,
    *,
    attempts: int = 160,
    interval_ms: int = 250,
) -> dict:
    """确认裁剪弹层已关闭、封面已回读且主题编辑器重新可用。"""

    last: dict = {}
    for _ in range(attempts):
        last = await _wechat_cover_crop_snapshot(page)
        if (
            not last.get("dialogVisible")
            and last.get("coverReady")
            and last.get("editorVisible")
        ):
            return last
        await page.wait_for_timeout(interval_ms)
    raise PreflightError(
        "公众号封面确认后未能回到主题编辑页，"
        f"弹层可见={bool(last.get('dialogVisible'))}、"
        f"封面回读={bool(last.get('coverReady'))}、"
        f"编辑器可见={bool(last.get('editorVisible'))}"
    )


async def _wechat_select_cover_from_content(page, editor, cover_image_index: int = 0) -> None:
    """从已插入正文的图片中选择封面，不再假设存在封面本地文件输入。"""

    body_images = editor.locator("img")
    if await body_images.count() <= cover_image_index:
        raise PreflightError("公众号正文中没有可供选择的指定封面图片")
    target_sources = {
        _wechat_normalize_image_url(value)
        for value in await body_images.nth(cover_image_index).evaluate(
            """element => [
                element.getAttribute('src'),
                element.getAttribute('data-src'),
                element.currentSrc,
            ].filter(Boolean)"""
        )
        if value
    }
    if not target_sources:
        raise PreflightError("公众号指定封面图片缺少可回读的图片地址")
    trigger = page.locator("#js_cover_area .js_cover_btn_area").first
    if await trigger.count() == 0:
        raise PreflightError("公众号封面选择入口未加载完成")
    await trigger.click(timeout=10_000)
    await page.wait_for_timeout(500)
    blocker = await _wechat_blocking_dialog_text(page)
    if blocker:
        raise PreflightError(
            "公众号后台阻止设置封面，预检不会修改账号设置：" + blocker
        )

    options = page.locator(".js_selectCoverFromContent")
    selected_option = None
    for index in range(await options.count()):
        option = options.nth(index)
        if await option.is_visible():
            selected_option = option
            break
    if selected_option is None:
        raise PreflightError("公众号“从正文选择”封面入口未显示")
    await selected_option.click(timeout=10_000)
    await page.wait_for_timeout(700)

    dialogs = page.locator(".weui-desktop-dialog")
    chosen_dialog = None
    chosen_image = None
    for dialog_index in range(await dialogs.count()):
        dialog = dialogs.nth(dialog_index)
        if not await dialog.is_visible():
            continue
        items = dialog.locator(".appmsg_content_img_item")
        for image_index in range(await items.count()):
            item = items.nth(image_index)
            candidate = item.locator(".appmsg_content_img.cover").first
            if not await item.is_visible() or not await candidate.is_visible():
                continue
            candidate_url = _wechat_normalize_image_url(
                await candidate.evaluate(
                    "element => getComputedStyle(element).backgroundImage"
                )
            )
            if candidate_url in target_sources:
                chosen_dialog = dialog
                chosen_image = item
                break
        if chosen_image is not None:
            break
    if chosen_image is None or chosen_dialog is None:
        raise PreflightError("公众号“从正文选择”弹层未匹配到指定封面图片")

    await chosen_image.click(timeout=10_000)
    next_buttons = chosen_dialog.get_by_text("下一步", exact=True)
    next_button = None
    for index in range(await next_buttons.count()):
        button = next_buttons.nth(index)
        if await button.is_visible():
            next_button = button
            break
    if next_button is None:
        raise PreflightError("公众号封面弹层未显示“下一步”")
    await next_button.click(timeout=10_000)
    completion_mode, _snapshot = await _wechat_wait_cover_crop_ready(page)
    if completion_mode == "confirm":
        finish_button = page.locator('[data-oneclick-cover-confirm="1"]')
        if await finish_button.count() != 1:
            raise PreflightError("公众号封面裁剪确认控件状态已变化")
        if not await finish_button.is_visible() or not await finish_button.is_enabled():
            raise PreflightError("公众号封面裁剪确认控件已变为不可用")
        await finish_button.click(timeout=10_000)
    await _wechat_wait_cover_return_to_editor(page)


async def _wechat_insert_body_images(page, editor, files: list[Path]) -> int:
    """经正文工具栏逐张插入图片，并以正文图片节点回读确认。"""

    if not files:
        return 0
    image_tool = page.locator("#js_editor_insertimage")
    image_input = image_tool.locator("input[type=file]")
    if await image_tool.count() == 0 or await image_input.count() == 0:
        raise PreflightError("公众号正文图片上传入口未加载完成")
    before = await editor.locator("img").count()
    await image_tool.hover()
    await image_input.set_input_files([str(path) for path in files])
    await page.wait_for_timeout(4_000)
    after = await editor.locator("img").count()
    if after < before + len(files):
        raise PreflightError(
            f"公众号正文图片未完整插入：期望新增 {len(files)} 张，实际新增 {after - before} 张"
        )
    return after - before


def _wechat_markdown_section_anchors(markdown_text: str) -> tuple[list[str], list[str]]:
    """提取可在富文本 DOM 中回读的章节标题和普通段落锚点。"""

    headings: list[str] = []
    paragraphs: list[str] = []
    paragraph_lines: list[str] = []

    def flush_paragraph() -> None:
        if not paragraph_lines:
            return
        text = _normalized_page_text(
            _wechat_markdown_visible_text(" ".join(paragraph_lines))
        )
        if text:
            paragraphs.append(text)
        paragraph_lines.clear()

    for raw_line in str(markdown_text or "").splitlines():
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            continue
        if line.startswith("## "):
            flush_paragraph()
            heading = _normalized_page_text(
                _wechat_markdown_visible_text(line)
            )
            if heading:
                headings.append(heading)
            continue
        if line.startswith(("### ", "- ", "> ")) or line == "---":
            flush_paragraph()
            continue
        paragraph_lines.append(line)
    flush_paragraph()
    return headings, paragraphs


def _wechat_resolve_body_image_anchors(
    markdown_text: str,
    files: list[Path],
    image_placements: list[dict] | None = None,
) -> list[str]:
    """把内容包位置语义解析为 DOM 文本锚点；空字符串代表导语后。"""

    specs_by_path = {
        str(Path(str(item.get("path") or "")).resolve()): item
        for item in image_placements or []
        if item.get("path")
    }
    specs: list[dict[str, str]] = []
    has_intro_image = False
    for path in files:
        raw = specs_by_path.get(str(path.resolve()), {})
        placement = str(raw.get("placement") or "").strip()
        anchor = str(raw.get("anchor") or "").strip()
        if placement in {"after_intro", "before_section_1"}:
            has_intro_image = True
        specs.append({"placement": placement, "anchor": anchor})

    for index, spec in enumerate(specs):
        if spec["placement"]:
            continue
        if not has_intro_image:
            spec["placement"] = "after_intro"
            has_intro_image = True
        else:
            spec["placement"] = "auto_distribute"

    headings, paragraphs = _wechat_markdown_section_anchors(markdown_text)
    explicit_heading_anchors = {
        spec["anchor"]
        for spec in specs
        if spec["placement"] == "after_heading" and spec["anchor"]
    }
    auto_indexes = [
        index
        for index, spec in enumerate(specs)
        if spec["placement"] == "auto_distribute"
    ]
    candidates = [
        heading for heading in headings if heading not in explicit_heading_anchors
    ]
    if len(candidates) < len(auto_indexes):
        # 章节不足时使用正文段落作为补充锚点，仍避免集中追加到文章末尾。
        candidates = paragraphs[1:] or paragraphs
    auto_anchors: list[str] = []
    for position in range(len(auto_indexes)):
        if not candidates:
            auto_anchors.append("")
            continue
        candidate_index = min(
            len(candidates) - 1,
            ((position + 1) * len(candidates)) // (len(auto_indexes) + 1),
        )
        auto_anchors.append(candidates[candidate_index])

    resolved: list[str] = []
    auto_position = 0
    for spec in specs:
        placement = spec["placement"]
        if placement in {"after_intro", "before_section_1"}:
            resolved.append("")
        elif placement == "after_heading":
            if not spec["anchor"]:
                raise PreflightError("公众号正文图片 after_heading 缺少标题锚点")
            resolved.append(spec["anchor"])
        elif placement == "auto_distribute":
            resolved.append(auto_anchors[auto_position])
            auto_position += 1
        else:
            raise PreflightError(f"公众号正文图片位置不支持：{placement}")
    return resolved


async def _wechat_place_body_image_anchor(editor, anchor_text: str = "") -> str:
    """把插图光标放到对应论点后；无锚点时默认放在导语与第一节之间。"""

    result = await editor.evaluate(
        r"""(element, requestedAnchor) => {
            const normalized = value => String(value || '').replace(/\s+/g, ' ').trim();
            const blocks = Array.from(element.querySelectorAll('p,h2,h3,li,blockquote'));
            const anchor = normalized(requestedAnchor);
            let target = null;
            let mode = '';
            if (anchor) {
                target = blocks.find(node => normalized(node.innerText).includes(anchor)) || null;
                if (!target) return 'anchor-not-found';
                mode = 'after-explicit-anchor';
            } else {
                target = element.querySelector('h2');
                if (target) mode = 'before-first-heading';
                else {
                    target = element.querySelector('p');
                    mode = target ? 'after-first-paragraph' : 'at-end';
                }
            }
            const range = document.createRange();
            if (mode === 'before-first-heading') range.setStartBefore(target);
            else if (target) range.setStartAfter(target);
            else {
                range.selectNodeContents(element);
                range.collapse(false);
            }
            range.collapse(true);
            const selection = window.getSelection();
            selection.removeAllRanges();
            selection.addRange(range);
            element.focus();
            return mode;
        }""",
        anchor_text,
    )
    if result == "anchor-not-found":
        raise PreflightError(f"公众号正文未找到插图锚点：{anchor_text}")
    return str(result or "")


async def _wechat_insert_anchored_body_images(
    page,
    editor,
    files: list[Path],
    anchors: list[str] | None = None,
) -> int:
    """逐张按内容锚点插入正文图，避免平台把全部图片机械追加到文末。"""

    inserted_files = 0
    requested_anchors = anchors or []
    for index, path in enumerate(files):
        anchor_text = requested_anchors[index] if index < len(requested_anchors) else ""
        await _wechat_place_body_image_anchor(editor, str(anchor_text or ""))
        inserted_nodes = await _wechat_insert_body_images(page, editor, [path])
        if inserted_nodes < 1:
            raise PreflightError(f"公众号正文插图未能写入：{path.name}")
        inserted_files += 1
    return inserted_files


async def _wechat_verify_body_image_placements(
    editor,
    expected_count: int,
    anchors: list[str] | None = None,
) -> list[dict]:
    """独立回读正文图片最终 DOM 位置，不用上传数量代替位置证据。"""

    requested_anchors = [str(item or "") for item in (anchors or [])]
    states = await editor.evaluate(
        r"""(element, requestedAnchors) => {
            const normalize = value => String(value || '').replace(/\s+/g, ' ').trim();
            const images = Array.from(element.querySelectorAll('img')).filter(image => {
              const source = image.currentSrc || image.getAttribute('src')
                || image.getAttribute('data-src') || '';
              return Boolean(source)
                && !image.classList.contains('ProseMirror-separator');
            });
            const blocks = Array.from(element.querySelectorAll('p,h2,h3,li,blockquote'));
            const firstHeading = element.querySelector('h2');
            const precedes = (left, right) => Boolean(
              left.compareDocumentPosition(right) & Node.DOCUMENT_POSITION_FOLLOWING
            );
            return images.map((image, index) => {
              const anchor = normalize(requestedAnchors[index] || '');
              const rect = image.getBoundingClientRect();
              const style = getComputedStyle(image);
              const rendered = rect.width > 0 && rect.height > 0
                && style.display !== 'none' && style.visibility !== 'hidden'
                && Number(style.opacity || 1) > 0;
              const introBlocks = blocks.filter(block =>
                block !== firstHeading
                && !['H2', 'H3'].includes(block.tagName)
                && normalize(block.innerText)
                && precedes(block, image)
              );
              if (!anchor) {
                return {
                  index,
                  mode: 'after_intro',
                  anchor: '',
                  hasFirstHeading: Boolean(firstHeading),
                  beforeFirstHeading: Boolean(firstHeading && precedes(image, firstHeading)),
                  introBlocksBefore: introBlocks.length,
                  rendered,
                };
              }
              const anchorBlock = blocks.find(block =>
                normalize(block.innerText).includes(anchor)
              ) || null;
              return {
                index,
                mode: 'after_anchor',
                anchor,
                anchorFound: Boolean(anchorBlock),
                afterAnchor: Boolean(anchorBlock && precedes(anchorBlock, image)),
                rendered,
              };
            });
        }""",
        requested_anchors,
    )
    if len(states) != expected_count:
        raise PreflightError(
            f"公众号正文图片最终回读数量不一致：期望 {expected_count} 张，实际 {len(states)} 张"
        )
    if len(requested_anchors) != expected_count:
        raise PreflightError("公众号正文图片位置约定数量与素材数量不一致")
    for index, state in enumerate(states):
        if not state.get("rendered"):
            raise PreflightError(
                f"公众号正文图片最终回读不可见：第 {index + 1} 张"
            )
        if not requested_anchors[index]:
            if state.get("hasFirstHeading") and not state.get("beforeFirstHeading"):
                raise PreflightError(
                    "公众号正文图片未位于导语后、第一节前"
                )
            if int(state.get("introBlocksBefore") or 0) < 1:
                raise PreflightError(
                    "公众号正文图片 before_section_1 前未回读到导语内容"
                )
            if (
                not state.get("hasFirstHeading")
                and int(state.get("introBlocksBefore") or 0) != 1
            ):
                raise PreflightError(
                    "公众号无章节标题时，正文图片未紧跟首段导语"
                )
            continue
        if not state.get("anchorFound"):
            raise PreflightError(
                f"公众号正文图片最终回读未找到锚点：{requested_anchors[index]}"
            )
        if not state.get("afterAnchor"):
            raise PreflightError(
                f"公众号正文图片未位于指定锚点之后：{requested_anchors[index]}"
            )
    return list(states)


async def _wechat_remove_temporary_cover(editor, first_image_index: int) -> int:
    """封面从正文选择成功后移除临时图片，不让封面候选滞留在正文末尾。"""

    removed = await editor.evaluate(
        r"""(element, startIndex) => {
            const allImages = Array.from(element.querySelectorAll('img'));
            const targets = allImages.slice(startIndex);
            const emptyBlocks = new Set();
            for (const image of targets) {
                const block = image.closest('p,figure');
                const text = String(block?.innerText || '').replace(/\s+/g, '').trim();
                if (block && block !== element && !text) emptyBlocks.add(block);
                else image.remove();
            }
            for (const block of emptyBlocks) block.remove();
            element.dispatchEvent(new InputEvent('input', {
                bubbles: true,
                inputType: 'deleteContent',
                data: null,
            }));
            return allImages.length - element.querySelectorAll('img').length;
        }""",
        first_image_index,
    )
    remaining = await editor.locator("img").count()
    if remaining > first_image_index:
        raise PreflightError("公众号临时封面图未能从正文安全移除")
    return int(removed or 0)


async def _wechat_prepare_article_images(
    page,
    editor,
    cover: Path,
    files: list[Path],
    image_anchors: list[str] | None = None,
) -> int:
    """临时插入封面完成选择，再按内容锚点插入真正的正文图片。"""

    first_cover_index = await editor.locator("img").count()
    await _wechat_insert_body_images(page, editor, [cover])
    await _wechat_select_cover_from_content(
        page,
        editor,
        cover_image_index=first_cover_index,
    )
    await _wechat_remove_temporary_cover(editor, first_cover_index)
    body_files = [path for path in files if path != cover]
    return await _wechat_insert_anchored_body_images(
        page,
        editor,
        body_files,
        image_anchors,
    )


async def _wechat_preflight(
    page,
    payload: dict,
    *,
    account: dict | None = None,
) -> str:
    title, description = _wechat_payload_text(payload)
    cover = Path(str(payload.get("coverPath") or "")).resolve()
    if not cover.is_file():
        raise PreflightError("公众号文章预检需要本地封面图片")
    content_type = str(payload.get("contentType") or "")
    files = [Path(str(item)).resolve() for item in payload.get("fileList") or []]
    if content_type == "article" and (not files or not all(path.is_file() for path in files)):
        raise PreflightError("公众号图文预检缺少可读取的正文图片素材")
    template_id = _wechat_template_for_payload(payload, account)
    await _wechat_open_article_editor(page)
    # 新版公众号将标题和正文都实现为 ProseMirror：第一个是标题，第二个是正文。
    editors = page.locator("div.ProseMirror")
    if await editors.count() < 2:
        raise PreflightError("公众号编辑器字段未加载完成")
    title_editor = editors.nth(0)
    editor = editors.nth(1)
    await title_editor.fill(title, force=True, timeout=10_000)
    visible_description = await _wechat_fill_rich_text(
        editor,
        description,
        template_id,
    )
    if _normalized_page_text(await title_editor.inner_text()) != _normalized_page_text(title):
        raise PreflightError("公众号标题字段未能回读测试值")
    if _normalized_page_text(await editor.inner_text()) != _normalized_page_text(visible_description):
        raise PreflightError("公众号正文字段未能回读测试值")
    # 当前公众号封面没有独立本地文件输入：指定封面需先进入正文图片链路，
    # 再经“从正文选择”设为封面。全程不触碰草稿、预览或发表控件。
    image_anchors = _wechat_resolve_body_image_anchors(
        description,
        files,
        list(payload.get("imagePlacements") or []),
    )
    inserted_images = await _wechat_prepare_article_images(
        page,
        editor,
        cover,
        files,
        image_anchors,
    )
    await _wechat_verify_body_image_placements(
        editor,
        inserted_images,
        image_anchors,
    )
    author_message = "未勾选原创，作者流程已完全跳过；"
    if _wechat_original_requested(payload):
        author_name = await _wechat_select_default_author(page)
        author_message = f"原创作者已选择并回读：{author_name}；"
    content_label = "图文" if content_type == "article" else "文字"
    template_name = _WECHAT_MOBILE_TEMPLATES[template_id]["name"]
    # 安全边界：不点击“保存为草稿”“预览”“发表”。
    image_message = (
        f"正文图片已插入并完成最终位置回读 {inserted_images} 张；"
        if inserted_images
        else ""
    )
    return (
        f"公众号{content_label}封面已上传，标题和正文已回读，已套用{template_name}；"
        f"{image_message}{author_message}"
        "未保存草稿、未预览、未发表"
    )


async def _fill_first_supported(locator, value: str) -> bool:
    """只填写可见的标准编辑控件，并在写后回读，避免误触其他页面元素。"""

    try:
        if not await locator.count() or not await locator.is_visible(timeout=1_000):
            return False
        tag = await locator.evaluate("element => element.tagName.toLowerCase()")
        if tag in {"input", "textarea"}:
            await locator.fill(value, timeout=5_000)
            return " ".join((await locator.input_value()).split()) == " ".join(value.split())
        await locator.fill(value, timeout=5_000)
        return " ".join((await locator.inner_text()).split()) == " ".join(value.split())
    except Exception:
        return False


async def _video_channel_video_preflight(page, payload: dict) -> str:
    """视频号视频预检：上传、填写和回读，严格停在发表按钮之前。"""

    files = [Path(str(item)).resolve() for item in payload.get("fileList") or []]
    if not files or not files[0].is_file():
        raise PreflightError("视频号视频预检缺少可读取的视频素材")
    title, description = _payload_text(payload, "视频号视频预检")
    # 视频号会校验页面内导航来源；直接打开 /post/create 会被重定向回首页。
    # 因而先进入已登录首页，再经官方“发表视频”入口进入编辑页。
    await page.goto(_VIDEO_CHANNEL_HOME_URL, wait_until="domcontentloaded", timeout=45_000)
    await page.wait_for_timeout(2_500)
    publish_video = page.get_by_text("发表视频", exact=True)
    try:
        await publish_video.click(timeout=12_000)
    except Exception as exc:
        raise PreflightError("视频号首页未加载“发表视频”入口") from exc
    upload = page.locator("input[type=file]").first
    try:
        await upload.wait_for(state="attached", timeout=15_000)
    except Exception as exc:
        raise PreflightError("视频号发表视频页未加载上传控件") from exc
    if not await upload.count():
        raise PreflightError("视频号发表视频页未加载上传控件")
    await upload.set_input_files(str(files[0]))
    editor_frame = None
    # 上传完成后编辑器由独立 iframe 异步装载；按实际出现条件等待，避免网络
    # 波动时把“尚在转码/加载”误判为字段不支持。
    for _ in range(20):
        for frame in page.frames:
            if await frame.locator("div.input-editor").count():
                editor_frame = frame
                break
        if editor_frame:
            break
        await page.wait_for_timeout(1_000)
    if editor_frame is None:
        raise PreflightError("视频号视频编辑器未加载完成")

    description_input = editor_frame.locator("div.input-editor").first
    if not await description_input.count():
        raise PreflightError("视频号视频描述编辑器未加载完成")
    await description_input.evaluate(
        """(element, value) => {
            element.focus();
            element.textContent = value;
            element.dispatchEvent(new InputEvent('input', {
                bubbles: true, inputType: 'insertText', data: value,
            }));
            element.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
        description,
    )
    await page.wait_for_timeout(300)
    description_ok = " ".join((await description_input.inner_text()).split()) == " ".join(description.split())
    title_input = editor_frame.locator('input[placeholder*="短标题"]').first
    if not await title_input.count():
        raise PreflightError("视频号短标题输入框未加载完成")
    await _set_dom_value(title_input, title)
    await page.wait_for_timeout(300)
    title_ok = " ".join((await title_input.input_value()).split()) == " ".join(title.split())
    if not description_ok or not title_ok:
        missing = "视频描述" if not description_ok else "短标题"
        raise PreflightError(f"视频号{missing}字段未能回读测试值")
    # 安全边界：绝不定位或点击发表、预览、存草稿等按钮。
    return "视频号视频素材已上传，视频描述和短标题已回读；未保存草稿、未预览、未发表"


async def _video_channel_graphic_preflight(page, payload: dict) -> str:
    """视频号图文预检：图片序列、标题和描述都只填到发表前页面。"""

    files = [Path(str(item)).resolve() for item in payload.get("fileList") or []]
    if not files or not all(path.is_file() for path in files):
        raise PreflightError("视频号图文预检缺少可读取的图片素材")
    if len(files) > 18:
        raise PreflightError("视频号图文一次最多可上传 18 张图片")
    title, description = _payload_text(payload, "视频号图文预检")
    title = title[:22]

    await page.goto(_VIDEO_CHANNEL_HOME_URL, wait_until="domcontentloaded", timeout=45_000)
    await page.wait_for_timeout(4_500)
    # 视频号将菜单作为自定义折叠导航渲染；通过真实菜单事件进入图文页，
    # 不能直接访问目标 URL，否则官方会重定向回首页。
    opened = await page.evaluate(
        """() => {
            const menu = Array.from(document.querySelectorAll('a.finder-ui-desktop-menu__sub__link'))
              .find(element => (element.innerText || '').trim() === '内容管理');
            if (!menu) return false;
            menu.click();
            return true;
        }"""
    )
    if not opened:
        raise PreflightError("视频号首页未加载内容管理入口")
    await page.wait_for_timeout(350)
    selected = await page.evaluate(
        """() => {
            const item = Array.from(document.querySelectorAll('*')).find(element => {
                const rect = element.getBoundingClientRect();
                return element.children.length === 0
                  && (element.innerText || '').trim() === '图文'
                  && rect.width > 0 && rect.height > 0;
            });
            if (!item) return false;
            item.click();
            return true;
        }"""
    )
    if not selected:
        raise PreflightError("当前视频号账号未显示图文发布入口")
    await page.wait_for_timeout(2_000)
    try:
        await page.get_by_text("发表图文", exact=True).click(timeout=10_000)
    except Exception as exc:
        raise PreflightError("视频号图文管理页未加载“发表图文”入口") from exc
    await page.wait_for_timeout(1_000)
    upload = page.locator("input[type=file]").first
    try:
        await upload.wait_for(state="attached", timeout=12_000)
        await upload.set_input_files([str(path) for path in files])
    except Exception as exc:
        raise PreflightError("视频号图文发表页未加载图片上传控件") from exc

    editor_frame = None
    for _ in range(20):
        for frame in page.frames:
            if await frame.locator("div.input-editor").count():
                editor_frame = frame
                break
        if editor_frame:
            break
        await page.wait_for_timeout(1_000)
    if editor_frame is None:
        raise PreflightError("视频号图文编辑器未加载完成")

    title_input = editor_frame.locator('input[placeholder*="填写标题"]').first
    description_input = editor_frame.locator("div.input-editor").first
    if not await title_input.count() or not await description_input.count():
        raise PreflightError("视频号图文标题或描述字段未加载完成")
    await _set_dom_value(title_input, title)
    await description_input.evaluate(
        """(element, value) => {
            element.focus();
            element.textContent = value;
            element.dispatchEvent(new InputEvent('input', {
                bubbles: true, inputType: 'insertText', data: value,
            }));
            element.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
        description,
    )
    await page.wait_for_timeout(300)
    if " ".join((await title_input.input_value()).split()) != " ".join(title.split()):
        raise PreflightError("视频号图文标题字段未能回读测试值")
    if " ".join((await description_input.inner_text()).split()) != " ".join(description.split()):
        raise PreflightError("视频号图文描述字段未能回读测试值")
    # 安全边界：不定位或点击发表、预览、草稿等会产生平台内容结果的控件。
    return f"视频号图文已上传 {len(files)} 张图片，标题和描述已回读；未保存草稿、未预览、未发表"


async def _douyin_fill_title_and_description(
    page,
    title: str,
    description: str,
    *,
    title_placeholder: str,
    label: str,
) -> None:
    """填写抖音创作页的标题和描述，并在页面内回读。"""

    title_input = page.locator(f'input[placeholder="{title_placeholder}"]').first
    await title_input.wait_for(state="visible", timeout=15_000)
    await _set_dom_value(title_input, title)
    editor = page.locator('[contenteditable="true"]').first
    await editor.wait_for(state="visible", timeout=10_000)
    await editor.fill(description, timeout=10_000)
    await page.wait_for_timeout(300)
    title_ok = _normalized_page_text(await title_input.input_value()) == _normalized_page_text(title)
    description_ok = _normalized_page_text(await editor.inner_text()) == _normalized_page_text(description)
    if not title_ok or not description_ok:
        missing = "标题" if not title_ok else "描述"
        raise PreflightError(f"抖音{label}{missing}字段未能回读测试值")


async def _douyin_first_visible_nodes(root, selectors: tuple[str, ...]) -> list:
    """按选择器优先级返回第一组可见节点，不混用隐藏模板节点。"""

    for selector in selectors:
        matches = root.locator(selector)
        visible = []
        for index in range(await matches.count()):
            node = matches.nth(index)
            try:
                in_viewport = await node.evaluate(
                    """element => {
                        const style = getComputedStyle(element);
                        const rect = element.getBoundingClientRect();
                        const width = Math.max(
                            0,
                            Math.min(rect.right, innerWidth) - Math.max(rect.left, 0)
                        );
                        const height = Math.max(
                            0,
                            Math.min(rect.bottom, innerHeight) - Math.max(rect.top, 0)
                        );
                        return !element.hidden
                            && element.getAttribute('aria-hidden') !== 'true'
                            && style.display !== 'none'
                            && style.visibility !== 'hidden'
                            && Number(style.opacity || 1) > 0.01
                            && width > 0
                            && height > 0;
                    }"""
                )
                if await node.is_visible() and await node.is_enabled() and in_viewport:
                    visible.append(node)
            except Exception:
                continue
        if visible:
            return visible
    return []


async def _douyin_wait_visible_nodes(
    root,
    selectors: tuple[str, ...],
    page,
    *,
    attempts: int = 20,
) -> list:
    """在有上限的时间内等待地点搜索弹层，防止无界等待。"""

    for _ in range(attempts):
        nodes = await _douyin_first_visible_nodes(root, selectors)
        if nodes:
            return nodes
        await page.wait_for_timeout(250)
    return []


async def _douyin_location_option_name(option) -> str:
    """优先读取地点选项的结构化名称，再回退到选项首行。"""

    for attribute in ("data-label", "title", "aria-label"):
        try:
            value = _normalized_page_text(await option.get_attribute(attribute))
        except Exception:
            value = ""
        if value:
            return value
    for selector in (
        '[class*="name"]',
        '[class*="title"]',
        '[data-testid*="name"]',
    ):
        nodes = option.locator(selector)
        for index in range(await nodes.count()):
            node = nodes.nth(index)
            try:
                if await node.is_visible():
                    value = _douyin_location_candidate_name(await node.inner_text())
                    if value:
                        return value
            except Exception:
                continue
    return _douyin_location_candidate_name(await option.inner_text())


async def _douyin_location_option_identity(option) -> dict[str, str]:
    """读取平台候选可见的 POI 身份；不从页面导出会话或凭据。"""

    poi_id = ""
    for attribute in (
        "data-poi-id",
        "data-poiid",
        "data-id",
        "data-value",
        "value",
    ):
        try:
            value = _normalized_page_text(await option.get_attribute(attribute))
        except Exception:
            value = ""
        if value and len(value) <= 128 and not any(mark in value for mark in "{}[]"):
            poi_id = value
            break

    name = await _douyin_location_option_name(option)
    address = ""
    for selector in (
        '[class*="address"]',
        '[class*="addr"]',
        '[data-testid*="address"]',
        '[class*="description"]',
    ):
        nodes = option.locator(selector)
        for index in range(await nodes.count()):
            node = nodes.nth(index)
            try:
                if await node.is_visible():
                    value = _normalized_page_text(await node.inner_text())
                    if value and value != name:
                        address = value
                        break
            except Exception:
                continue
        if address:
            break
    if not address:
        try:
            lines = [
                _normalized_page_text(line)
                for line in (await option.inner_text()).splitlines()
                if _normalized_page_text(line)
            ]
        except Exception:
            lines = []
        address = " ".join(line for line in lines[1:] if line != name)
    return {"poiId": poi_id, "name": name, "address": address}


async def _douyin_set_location(page, payload: dict) -> str:
    """
    使用抖音官方发布页的地点搜索并回读。

    不做坐标伪造，不调用地图签名或隐式接口；只在页面返回唯一同名候选时
    才选择，否则安全停止。留空表示明确不添加定位。
    """

    selected_poi = payload.get("locationPoi")
    if selected_poi and not isinstance(selected_poi, dict):
        raise PreflightError("抖音发布定位数据无效，已安全停止")
    selected_poi = selected_poi if isinstance(selected_poi, dict) else {}
    keyword = _normalized_page_text(
        selected_poi.get("name") or payload.get("locationKeyword")
    )
    if not keyword:
        return ""
    if not selected_poi:
        raise PreflightError("抖音任务只包含地点关键词，缺少已选择的官方 POI，已安全停止")

    triggers = await _douyin_wait_visible_nodes(
        page,
        _DOUYIN_LOCATION_TRIGGER_SELECTORS,
        page,
    )
    if len(triggers) != 1:
        raise PreflightError(
            "抖音发布页未找到唯一可用的“发布定位”入口，已停止以避免误操作"
        )
    trigger = triggers[0]
    selection_container = trigger.locator(
        'xpath=ancestor::div[contains(@class,"semi-select")][1]'
    )
    if await selection_container.count() != 1:
        raise PreflightError("抖音发布定位控件结构已变化，已安全停止")
    await trigger.click(timeout=8_000)

    inputs = await _douyin_wait_visible_nodes(
        page,
        _DOUYIN_LOCATION_INPUT_SELECTORS,
        page,
    )
    if len(inputs) != 1:
        raise PreflightError("抖音地点搜索输入框未唯一显示，已安全停止")
    await inputs[0].fill(keyword, timeout=8_000)

    options = await _douyin_wait_visible_nodes(
        page,
        _DOUYIN_LOCATION_OPTION_SELECTORS,
        page,
        attempts=24,
    )
    if not options:
        raise PreflightError(f"抖音未返回地点“{keyword}”的可选结果")
    candidates = [await _douyin_location_option_identity(option) for option in options]
    matched_indexes = _douyin_location_match_indexes(selected_poi, candidates)
    if len(matched_indexes) != 1:
        matched = "、".join(
            " / ".join(
                part
                for part in (candidate.get("name"), candidate.get("address"))
                if part
            )
            for candidate in candidates[:5]
        ) or "无可读名称"
        raise PreflightError(
            f"抖音地点“{keyword}”没有唯一一致的 POI（当前：{matched}），已停止且未猜选"
        )
    selected_name = candidates[matched_indexes[0]]["name"]
    await options[matched_indexes[0]].click(timeout=8_000)
    await page.wait_for_timeout(300)

    try:
        readback = _normalized_page_text(await selection_container.inner_text())
    except Exception as exc:
        raise PreflightError("抖音定位选择后无法回读控件值") from exc
    if selected_name.casefold() not in readback.casefold():
        raise PreflightError(
            f"抖音定位回读不一致：期望“{selected_name}”，页面显示“{readback or '空'}”"
        )
    return selected_name


async def _douyin_video_preflight(page, payload: dict) -> str:
    """抖音视频预检：上传、填写、回读，严格结束在发布动作之前。"""

    files = [Path(str(item)).resolve() for item in payload.get("fileList") or []]
    if not files or not files[0].is_file():
        raise PreflightError("抖音视频预检缺少可读取的视频素材")
    title, description = _payload_text(payload, "抖音视频预检")
    await page.goto(_DOUYIN_UPLOAD_URL, wait_until="domcontentloaded", timeout=45_000)
    upload = page.locator('input[type=file]').first
    await upload.wait_for(state="attached", timeout=15_000)
    await upload.set_input_files(str(files[0]))
    await page.wait_for_url("**/creator-micro/content/post/video*", timeout=30_000)
    await _douyin_fill_title_and_description(
        page, title, description,
        title_placeholder="填写作品标题，为作品获得更多流量", label="视频",
    )
    location_name = await _douyin_set_location(page, payload)
    # 安全边界：绝不定位或点击“发布”“发布暂存离开”“预览”等按钮。
    location_note = f"，定位“{location_name}”已回读" if location_name else "，未添加定位"
    return f"抖音视频素材已上传，标题和描述已回读{location_note}；未保存草稿、未预览、未发布"


async def _douyin_graphic_preflight(page, payload: dict) -> str:
    """抖音图文预检：上传图片序列、填写、回读，不创建草稿。"""

    files = [Path(str(item)).resolve() for item in payload.get("fileList") or []]
    if not files or not all(path.is_file() for path in files):
        raise PreflightError("抖音图文预检缺少可读取的图片素材")
    if len(files) > 35:
        raise PreflightError("抖音图文一次最多可上传 35 张图片")
    title, description = _payload_text(payload, "抖音图文预检")
    await page.goto(f"{_DOUYIN_UPLOAD_URL}?default-tab=3", wait_until="domcontentloaded", timeout=45_000)
    upload = page.locator('input[type=file]').first
    await upload.wait_for(state="attached", timeout=15_000)
    await upload.set_input_files([str(path) for path in files])
    await page.wait_for_url("**/creator-micro/content/post/image*", timeout=30_000)
    await _douyin_fill_title_and_description(
        page, title, description, title_placeholder="添加作品标题", label="图文",
    )
    location_name = await _douyin_set_location(page, payload)
    # 安全边界：不定位或点击预览、暂存、发布等会产生平台内容结果的控件。
    location_note = f"，定位“{location_name}”已回读" if location_name else "，未添加定位"
    return f"抖音图文已上传 {len(files)} 张图片，标题和描述已回读{location_note}；未保存草稿、未预览、未发布"


async def _douyin_text_preflight(page, payload: dict) -> str:
    """抖音文章预检：填写文章、上传必填封面并回读，停在发布前。"""

    cover = Path(str(payload.get("coverPath") or "")).resolve()
    if not cover.is_file():
        raise PreflightError("抖音文字预检需要选择本地封面图片")
    title, description = _payload_text(payload, "抖音文字预检")
    title = title[:30]
    summary = " ".join(description.split())[:30]
    await page.goto(f"{_DOUYIN_UPLOAD_URL}?default-tab=5", wait_until="domcontentloaded", timeout=45_000)
    await page.get_by_text("我要发文", exact=True).click(timeout=12_000)
    title_input = page.locator('input[placeholder*="文章标题"]').first
    await title_input.wait_for(state="visible", timeout=20_000)
    summary_input = page.locator('input[placeholder*="内容摘要"]').first
    editor = page.locator('div.ProseMirror[contenteditable="true"]').first
    await summary_input.fill(summary, timeout=10_000)
    await _set_dom_value(title_input, title)
    await editor.fill(description, timeout=10_000)
    # 抖音文章封面由点击控件动态创建 file chooser，页面没有常驻 file input。
    async with page.expect_file_chooser(timeout=10_000) as chooser_info:
        await page.get_by_text("点击上传封面图", exact=True).click(timeout=10_000)
    await (await chooser_info.value).set_files(str(cover))
    await page.wait_for_timeout(1_000)
    title_ok = _normalized_page_text(await title_input.input_value()) == _normalized_page_text(title)
    summary_ok = _normalized_page_text(await summary_input.input_value()) == _normalized_page_text(summary)
    body_ok = _normalized_page_text(await editor.inner_text()) == _normalized_page_text(description)
    if not title_ok or not summary_ok or not body_ok:
        missing = "标题" if not title_ok else "摘要" if not summary_ok else "正文"
        raise PreflightError(f"抖音文字{missing}字段未能回读测试值")
    location_name = await _douyin_set_location(page, payload)
    location_note = f"，定位“{location_name}”已回读" if location_name else "，未添加定位"
    return f"抖音文章封面已上传，标题、摘要和正文已回读{location_note}；未保存草稿、未预览、未发布"


async def _kuaishou_video_preflight(page, payload: dict) -> str:
    """快手视频预检：上传并回读作品描述，结束在发布前。"""

    files = [Path(str(item)).resolve() for item in payload.get("fileList") or []]
    if not files or not files[0].is_file():
        raise PreflightError("快手视频预检缺少可读取的视频素材")
    title, description = _payload_text(payload, "快手视频预检")
    # 快手以“作品描述”作为视频标题、文案和话题的统一编辑字段。
    work_description = f"{title}\n{description}"[:1000]
    await page.goto(_KUAISHOU_VIDEO_URL, wait_until="domcontentloaded", timeout=45_000)
    upload = page.locator('input[type=file]').first
    await upload.wait_for(state="attached", timeout=15_000)
    await upload.set_input_files(str(files[0]))
    editor = page.locator("#work-description-edit").first
    await editor.wait_for(state="visible", timeout=60_000)
    await editor.fill(work_description, timeout=12_000)
    await page.wait_for_timeout(300)
    if _normalized_page_text(await editor.inner_text()) != _normalized_page_text(work_description):
        raise PreflightError("快手视频作品描述字段未能回读测试值")
    # 安全边界：绝不定位或点击发布、取消、预览等控件。
    return "快手视频素材已上传，作品描述已回读；未保存草稿、未预览、未发布"


async def _kuaishou_graphic_preflight(page, payload: dict) -> str:
    """快手图文预检：经官方上传按钮上传图片序列，再回读作品描述。"""

    files = [Path(str(item)).resolve() for item in payload.get("fileList") or []]
    if not files or not all(path.is_file() for path in files):
        raise PreflightError("快手图文预检缺少可读取的图片素材")
    if len(files) > 31:
        raise PreflightError("快手图文一次最多可上传 31 张图片")
    title, description = _payload_text(payload, "快手图文预检")
    work_description = f"{title}\n{description}"[:500]
    await page.goto(_KUAISHOU_VIDEO_URL, wait_until="domcontentloaded", timeout=45_000)
    # 首屏框架与 tab 会异步加载，使用语义角色而非首页同名文案避免误命中。
    graphic_tab = page.locator('[role="tab"]').filter(has_text="上传图文").first
    await graphic_tab.wait_for(state="visible", timeout=25_000)
    await graphic_tab.click(timeout=12_000)
    # 该页切换 tab 后会替换隐藏 input；通过平台“上传图片”按钮获得当前 chooser，
    # 不复用视频 tab 中已经失效的 input。
    async with page.expect_file_chooser(timeout=12_000) as chooser_info:
        await page.get_by_text("上传图片", exact=True).click(timeout=10_000)
    await (await chooser_info.value).set_files([str(path) for path in files])
    editor = page.locator("#work-description-edit").first
    await editor.wait_for(state="visible", timeout=60_000)
    await editor.fill(work_description, timeout=12_000)
    await page.wait_for_timeout(300)
    if _normalized_page_text(await editor.inner_text()) != _normalized_page_text(work_description):
        raise PreflightError("快手图文作品描述字段未能回读测试值")
    # 安全边界：不定位或点击发布、取消、预览等控件。
    return f"快手图文已上传 {len(files)} 张图片，作品描述已回读；未保存草稿、未预览、未发布"


async def _bilibili_video_preflight(page, payload: dict) -> str:
    """B站视频预检：上传、标题和简介回读，严格停在投稿前。"""

    files = [Path(str(item)).resolve() for item in payload.get("fileList") or []]
    if not files or not files[0].is_file():
        raise PreflightError("B站视频预检缺少可读取的视频素材")
    title, description = _payload_text(payload, "B站视频预检")
    await page.goto(_BILIBILI_VIDEO_URL, wait_until="domcontentloaded", timeout=45_000)
    upload = page.locator('input[type="file"][accept*=".mp4"]').first
    await upload.wait_for(state="attached", timeout=15_000)
    await upload.set_input_files(str(files[0]))

    title_input = page.locator('input[placeholder="请输入稿件标题"]').first
    try:
        await title_input.wait_for(state="visible", timeout=90_000)
    except Exception as exc:
        raise PreflightError("B站视频上传后未进入稿件信息页") from exc
    await title_input.fill(title[:80], timeout=10_000)
    description_editor = page.locator('.ql-editor[contenteditable="true"]').first
    await description_editor.wait_for(state="visible", timeout=12_000)
    await description_editor.fill(description[:2_000], timeout=10_000)
    if _normalized_page_text(await title_input.input_value()) != _normalized_page_text(title[:80]):
        raise PreflightError("B站视频标题字段未能回读测试值")
    if _normalized_page_text(description) not in _normalized_page_text(await description_editor.inner_text()):
        raise PreflightError("B站视频简介字段未能回读测试值")
    # 安全边界：绝不定位或点击“立即投稿”“保存草稿”“预览”。
    return "B站视频素材已上传，标题和简介已回读；未保存草稿、未预览、未投稿"


async def _bilibili_article_frame(page):
    """等待 B站专栏内嵌编辑器；未出现则不误判为平台不支持。"""

    for _ in range(30):
        for frame in page.frames:
            if "member.bilibili.com/york/read-editor" in str(frame.url):
                title = frame.locator('textarea[placeholder*="请输入标题"]').first
                if await title.count():
                    return frame
        await page.wait_for_timeout(500)
    raise PreflightError("B站专栏编辑器未加载完成")


async def _bilibili_article_preflight(page, payload: dict) -> str:
    """B站专栏预检：填写标题正文，图文额外上传图片序列，不保存草稿。"""

    content_type = str(payload.get("contentType") or "")
    files = [Path(str(item)).resolve() for item in payload.get("fileList") or []]
    if content_type == "article" and (not files or not all(path.is_file() for path in files)):
        raise PreflightError("B站图文预检缺少可读取的图片素材")
    title, description = _payload_text(payload, "B站专栏预检")
    await page.goto(_BILIBILI_ARTICLE_URL, wait_until="domcontentloaded", timeout=45_000)
    frame = await _bilibili_article_frame(page)
    title_input = frame.locator('textarea[placeholder*="请输入标题"]').first
    editor = frame.locator('.tiptap.ProseMirror[contenteditable="true"]').first
    await title_input.fill(title[:50], timeout=10_000)
    await editor.fill(description[:100_000], timeout=10_000)
    if _normalized_page_text(await title_input.input_value()) != _normalized_page_text(title[:50]):
        raise PreflightError("B站专栏标题字段未能回读测试值")
    if _normalized_page_text(description) not in _normalized_page_text(await editor.inner_text()):
        raise PreflightError("B站专栏正文字段未能回读测试值")

    if content_type == "article":
        image_tool = frame.locator("eva3-toolbar-image").first
        await image_tool.wait_for(state="visible", timeout=8_000)
        async with page.expect_file_chooser(timeout=10_000) as chooser_info:
            await image_tool.click()
        chooser = await chooser_info.value
        await chooser.set_files([str(path) for path in files])
        images = frame.locator('.tiptap.ProseMirror img')
        try:
            await images.first.wait_for(state="visible", timeout=30_000)
        except Exception as exc:
            raise PreflightError("B站图文图片上传后未回显到专栏正文") from exc
        if await images.count() < len(files):
            raise PreflightError("B站图文图片未完整回显到专栏正文")
        return f"B站图文已上传 {len(files)} 张图片，标题和正文已回读；未保存草稿、未预览、未投稿"

    # B站专栏在未选自定义封面时会按正文自动生成封面；当前页面明确标为可选。
    return "B站文字专栏标题和正文已回读；平台封面为可选，未保存草稿、未预览、未投稿"


async def run_preflight(payload: dict) -> dict:
    """运行单平台预检，返回可安全写入任务记录的结果。"""

    if str(payload.get("runtimeMode") or "preflight") != "preflight" or not payload.get("debugDryRun", True):
        raise PreflightError("一键发当前测试执行器只允许预发布检查，正式发布保持人工确认")
    account = _account_for_payload(payload)
    platform_type = int(account["type"])
    if platform_type not in {1, 2, 3, 4, 5, 10}:
        raise PreflightError("当前真实预检仅接入小红书、视频号、抖音、快手、B站与公众号")
    from playwright.async_api import async_playwright

    playwright = await async_playwright().start()
    browser = None
    context = None
    try:
        browser = await playwright.chromium.launch(headless=bool(payload.get("backgroundMode", True)))
        context = await browser.new_context(storage_state=str(_storage_state(account)))
        page = await context.new_page()
        if platform_type == 1:
            message = await _xhs_preflight(page, payload)
        elif platform_type == 2:
            content_type = str(payload.get("contentType") or "")
            if content_type == "video":
                message = await _video_channel_video_preflight(page, payload)
            elif content_type == "article":
                message = await _video_channel_graphic_preflight(page, payload)
            else:
                raise PreflightError("视频号当前不支持纯文字预检")
        elif platform_type == 3:
            content_type = str(payload.get("contentType") or "")
            if content_type == "video":
                message = await _douyin_video_preflight(page, payload)
            elif content_type == "article":
                message = await _douyin_graphic_preflight(page, payload)
            elif content_type == "text":
                message = await _douyin_text_preflight(page, payload)
            else:
                raise PreflightError("抖音不支持当前内容类型的预检")
        elif platform_type == 4:
            content_type = str(payload.get("contentType") or "")
            if content_type == "video":
                message = await _kuaishou_video_preflight(page, payload)
            elif content_type == "article":
                message = await _kuaishou_graphic_preflight(page, payload)
            else:
                raise PreflightError("快手不支持纯文字发布")
        elif platform_type == 5:
            content_type = str(payload.get("contentType") or "")
            if content_type == "video":
                message = await _bilibili_video_preflight(page, payload)
            elif content_type in {"article", "text"}:
                message = await _bilibili_article_preflight(page, payload)
            else:
                raise PreflightError("B站不支持当前内容类型的预检")
        else:
            operation = str(payload.get("preflightOperation") or "")
            if operation == _WECHAT_AUTHOR_ONLY_OPERATION:
                message = await _wechat_author_only_preflight(page, payload)
            elif operation == _WECHAT_COVER_ONLY_OPERATION:
                message = await _wechat_cover_only_preflight(page, payload)
            elif not operation:
                message = await _wechat_preflight(page, payload, account=account)
            else:
                raise PreflightError(f"公众号预检操作不支持：{operation}")
        return {"type": platform_type, "ok": True, "message": message}
    except Exception as exc:
        return {"type": platform_type, "ok": False, "message": f"预检失败：{type(exc).__name__}：{exc}"}
    finally:
        if context:
            await context.close()
        if browser:
            await browser.close()
        await playwright.stop()


def run_preflight_sync(payload: dict) -> dict:
    """供桌面任务线程调用的同步包装。"""

    return asyncio.run(run_preflight(payload))


def run_wechat_author_preflight_sync(payload: dict) -> dict:
    """受控的公众号仅作者入口；调用方必须显式声明 dry-run 和作者操作。"""

    _validate_wechat_author_payload(payload)
    return asyncio.run(run_preflight(dict(payload)))


def run_wechat_cover_preflight_sync(payload: dict) -> dict:
    """受控的公众号仅封面入口；不填写其他内容，也不触碰提交类控件。"""

    _validate_wechat_cover_payload(payload)
    return asyncio.run(run_preflight(dict(payload)))
