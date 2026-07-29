# -*- coding: utf-8 -*-
"""一键发的真实平台预检执行器。

此模块只使用一键发保存的官方会话：上传测试素材、填写表单并核对页面。
它明确禁止点击“发表”“发布”“保存草稿”“预览”等会产生内容结果的按钮。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
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


class PreflightError(RuntimeError):
    """平台预检无法停在最终发布前时抛出。"""


def _account_for_payload(payload: dict) -> dict:
    platform_type = int(payload.get("type") or 0)
    files = {Path(str(value)).name for value in payload.get("accountList") or []}
    for account in account_service.list_accounts():
        if int(account.get("type") or 0) == platform_type and Path(str(account.get("filePath") or "")).name in files:
            return account
    raise PreflightError("未找到一键发已登录账号，请先在账号管理中完成登录")


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
    files = [Path(str(item)).resolve() for item in payload.get("fileList") or []]
    if not files or not all(path.is_file() for path in files):
        raise PreflightError("小红书预检缺少可读取的本地素材")
    content_type = str(payload.get("contentType") or "video")
    title, description = _payload_text(payload, "小红书预检")
    await page.goto(_XHS_PUBLISH_URL, wait_until="domcontentloaded", timeout=45_000)
    await page.wait_for_timeout(800)
    if content_type == "article":
        # 小红书页面会同时保留离屏标题节点；优先使用实际绑定点击事件的 tab。
        selector = page.locator(".creator-tab[data-hp-kind='creator-tab-上传图文']").first
        if not await selector.count():
            selector = page.locator(".creator-tab").filter(has_text="上传图文").last
        await selector.click(timeout=8_000, force=True)
        await page.wait_for_timeout(600)
        upload = page.locator("input[type=file]").first
        await upload.set_input_files([str(path) for path in files])
        label = "图文"
    elif content_type == "video":
        upload = page.locator("input.upload-input[type=file]").first
        await upload.set_input_files(str(files[0]))
        label = "视频"
    else:
        raise PreflightError("小红书当前不支持纯文字预检")
    await page.wait_for_timeout(7_000)
    title_input = page.locator('input[placeholder="填写标题会有更多赞哦"]').first
    await title_input.wait_for(state="attached", timeout=15_000)
    await _set_dom_value(title_input, title)
    editor = page.locator(".tiptap.ProseMirror").first
    await editor.wait_for(state="attached", timeout=8_000)
    await editor.evaluate(
        """(element, value) => {
            element.textContent = value;
            element.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: value }));
        }""",
        description,
    )
    if await title_input.input_value() != title:
        raise PreflightError("小红书标题字段未能回读测试值")
    # 安全边界：本函数到此结束，绝不定位或点击预览、草稿、发布按钮。
    return f"小红书{label}素材已上传，标题和正文已回读；未保存草稿、未预览、未发布"


async def _wechat_preflight(page, payload: dict) -> str:
    title, description = _payload_text(payload, "公众号预检")
    cover = Path(str(payload.get("coverPath") or "")).resolve()
    if not cover.is_file():
        raise PreflightError("公众号文章预检需要本地封面图片")
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
    # 封面上传会触发编辑器状态刷新，因此先上传，再写入标题和正文。
    await page.locator("input[type=file]").first.set_input_files(str(cover))
    await page.wait_for_timeout(4_000)
    # 新版公众号将标题和正文都实现为 ProseMirror：第一个是标题，第二个是正文。
    editors = page.locator("div.ProseMirror")
    if await editors.count() < 2:
        raise PreflightError("公众号编辑器字段未加载完成")
    title_editor = editors.nth(0)
    editor = editors.nth(1)
    await title_editor.fill(title, force=True, timeout=10_000)
    await editor.fill(description, force=True, timeout=10_000)
    if title not in " ".join((await title_editor.inner_text()).split()):
        raise PreflightError("公众号标题字段未能回读测试值")
    if not " ".join((await editor.inner_text()).split()):
        raise PreflightError("公众号正文字段未能回读测试值")
    content_label = "图文" if str(payload.get("contentType")) == "article" else "文字"
    # 安全边界：不点击“保存为草稿”“预览”“发表”。
    return f"公众号{content_label}封面已上传，标题和正文已回读；未保存草稿、未预览、未发表"


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
    # 安全边界：绝不定位或点击“发布”“发布暂存离开”“预览”等按钮。
    return "抖音视频素材已上传，标题和描述已回读；未保存草稿、未预览、未发布"


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
    # 安全边界：不定位或点击预览、暂存、发布等会产生平台内容结果的控件。
    return f"抖音图文已上传 {len(files)} 张图片，标题和描述已回读；未保存草稿、未预览、未发布"


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
    # 安全边界：不定位或点击“发布”“发布暂存离开”“预览”等按钮。
    return "抖音文字标题、摘要、正文与封面已填写并回读；未保存草稿、未预览、未发布"


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
            message = await _wechat_preflight(page, payload)
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
