# -*- coding: utf-8 -*-
"""一键发抖音视频正式发布执行器。

此模块把一键发账号、任务和新版“发布定位”预检逻辑接到恢复的抖音
浏览器上传能力上。它不使用外部客户端、接口签名或 Cookie 导出；所有
页面写入与最终发布均发生在一键发保存的本机浏览器会话内。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from . import account_service, douyin_commerce_service, douyin_music_service, task_service
from .douyin_location_service import normalize_location_candidate
from .oneclick_preflight import _account_for_payload, _storage_state


_DOUYIN_EDITOR_URL = (
    "https://creator.douyin.com/creator-micro/content/post/video?enter_from=publish_page"
)
_DOUYIN_HOME_URL = "https://creator.douyin.com/"
_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


class DouyinPublishError(RuntimeError):
    """抖音正式发布不能安全继续时抛出。"""


def _is_commerce_workflow(payload: Mapping[str, Any]) -> bool:
    return str(payload.get("workflow") or "") == douyin_commerce_service.DOUYIN_COMMERCE_WORKFLOW


def _normalized(value: object) -> str:
    return " ".join(str(value or "").replace("\u200b", " ").split())


def _expected_account_name(account: Mapping[str, Any]) -> str:
    return _normalized(account.get("profileName") or account.get("userName"))


def _scheduled_time(payload: Mapping[str, Any]) -> datetime | None:
    """读取并校验抖音定时发布时间，统一按北京时间解释。"""

    enabled = payload.get("enableTimer") is True
    raw = _normalized(payload.get("scheduleTime")).replace("T", " ")
    if not enabled:
        if raw:
            raise DouyinPublishError("未开启抖音定时发布，但载荷中存在定时时间")
        return None
    if not raw:
        raise DouyinPublishError("抖音已开启定时发布，但缺少发布时间")
    try:
        target = datetime.strptime(raw, "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise DouyinPublishError(
            "抖音定时发布时间必须为 YYYY-MM-DD HH:mm（北京时间）"
        ) from exc
    now = datetime.now(_SHANGHAI_TZ).replace(tzinfo=None)
    if target <= now:
        raise DouyinPublishError("抖音定时发布时间必须晚于当前北京时间")
    return target


def _schedule_text_matches(text: object, target: datetime) -> bool:
    """判断管理页是否回读了指定定时日和时，不依赖固定 UI 文案。"""

    normalized = _normalized(text)
    if not normalized:
        return False
    time_text = target.strftime("%H:%M")
    if time_text not in normalized:
        return False
    date_variants = (
        target.strftime("%Y-%m-%d"),
        target.strftime("%Y/%m/%d"),
        target.strftime("%Y年%m月%d日"),
        target.strftime("%m-%d"),
        target.strftime("%m/%d"),
    )
    if any(value in normalized for value in date_variants):
        return True
    today = datetime.now(_SHANGHAI_TZ).date()
    return target.date() == today and "今天" in normalized


async def _scheduled_submission_readback(
    page,
    *,
    title: str,
    target: datetime,
    attempts: int = 90,
) -> dict[str, str]:
    """在最终提交后从抖音管理页回读该作品与定时时间。"""

    expected_title = _normalized(title)
    for attempt in range(attempts):
        if page.is_closed():
            break
        try:
            body = await page.locator("body").inner_text(timeout=1_500)
        except Exception:
            body = ""
        if expected_title and expected_title in _normalized(body) and _schedule_text_matches(
            body, target
        ):
            return {
                "title": expected_title,
                "scheduledAt": target.strftime("%Y-%m-%d %H:%M"),
                "url": str(page.url or ""),
            }
        # 最终提交后的管理页会先进入空壳，再异步刷新作品卡片。每十秒仅
        # 刷新一次只读管理页，避免把“跳转成功但列表尚未同步”误判为失败。
        if attempt and attempt % 10 == 0:
            try:
                await page.reload(wait_until="domcontentloaded", timeout=20_000)
            except Exception:
                pass
        await page.wait_for_timeout(1_000)
    raise DouyinPublishError(
        "抖音定时提交后未能从作品管理页回读标题和指定时间，未记录为定时成功"
    )


def validate_douyin_publish_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """校验抖音正式发布的最小可审计边界，不触发浏览器或平台动作。"""

    checked = dict(payload)
    if _is_commerce_workflow(checked):
        try:
            checked = douyin_commerce_service.validate_douyin_commerce_payload(checked)
        except douyin_commerce_service.DouyinCommerceError as exc:
            raise DouyinPublishError(str(exc)) from exc
    if int(checked.get("type") or 0) != 3:
        raise DouyinPublishError("抖音正式发布执行器只接受抖音目标")
    if str(checked.get("runtimeMode") or "") != "publish":
        raise DouyinPublishError("抖音正式发布必须明确 runtimeMode=publish")
    if checked.get("debugDryRun") is not False:
        raise DouyinPublishError("抖音正式发布必须明确 debugDryRun=false")
    if str(checked.get("contentType") or "") != "video":
        raise DouyinPublishError("抖音正式发布当前只接入视频类型")

    accounts = [str(item).strip() for item in checked.get("accountList") or [] if str(item).strip()]
    if len(accounts) != 1:
        raise DouyinPublishError("抖音正式发布一次只能选择一个已登录账号")
    checked["accountList"] = accounts

    files = [str(item).strip() for item in checked.get("fileList") or [] if str(item).strip()]
    if len(files) != 1 or not Path(files[0]).is_file():
        raise DouyinPublishError("抖音正式发布需要且只允许一条可读取的视频素材")
    checked["fileList"] = files

    if not _normalized(checked.get("title")):
        raise DouyinPublishError("抖音正式发布缺少标题")
    if not _normalized(checked.get("description")):
        raise DouyinPublishError("抖音正式发布缺少作品描述")

    keyword = _normalized(checked.get("locationKeyword"))
    location = normalize_location_candidate(checked.get("locationPoi"))
    if keyword:
        if not location or location["name"] != keyword:
            raise DouyinPublishError(
                "抖音发布定位必须来自一键发已选择的官方 POI，不能只传关键词"
            )
        checked["locationKeyword"] = location["name"]
        checked["locationPoi"] = location
    else:
        checked["locationKeyword"] = ""
        checked["locationPoi"] = {}

    # 正式发布必须以前台浏览器执行，二维码、验证码和未知提示才有机会被用户处理。
    checked["backgroundMode"] = False
    schedule_time = _scheduled_time(checked)
    checked["enableTimer"] = schedule_time is not None
    checked["scheduleTime"] = (
        schedule_time.strftime("%Y-%m-%d %H:%M") if schedule_time else None
    )
    return checked


def _commerce_readback(
    expected_store: Mapping[str, Any],
    actual_store: object,
) -> dict[str, str]:
    """要求已绑定门店由稳定身份、名称和关联 POI 三项共同回读。"""

    if not isinstance(actual_store, Mapping):
        raise DouyinPublishError("抖音带货门店没有返回可验证的页面回读")
    expected = douyin_commerce_service.normalize_commerce_store(expected_store)
    actual = douyin_commerce_service.normalize_commerce_store(actual_store)
    if not expected or not actual:
        raise DouyinPublishError("抖音带货门店回读缺少稳定身份，未记录为定时成功")
    if any(
        actual.get(key) != expected.get(key)
        for key in ("storeId", "name", "poiId")
    ):
        raise DouyinPublishError("抖音带货门店最终回读与任务指定门店不一致，未记录为定时成功")
    return actual


def _music_readback(actual_music: object) -> dict[str, str]:
    """要求带货流程已从可见收藏列表回读一条实际选择的音乐。"""

    music = douyin_music_service.normalize_music_readback(actual_music)
    if not music:
        raise DouyinPublishError("抖音带货音乐没有返回可验证的页面回读，已安全停止")
    return music


def _configure_commerce_editor(
    app: object,
    checked: Mapping[str, Any],
    *,
    discovery: bool = False,
) -> None:
    """把带货专属编辑器规则集中写入恢复上传器。

    带货视频不走封面裁剪；上传完成后必须走收藏音乐步骤。发布定位和作品
    内容声明均由用户明确选择并页面回读，不建立或绑定本地门店记录。
    """

    if checked.get("musicMode") == douyin_music_service.FAVORITE_MANUAL_MUSIC_MODE:
        raise DouyinPublishError(
            "用户自选收藏音乐必须复用抖音带货分步编辑会话，禁止重新上传后猜测选择"
        )
    setattr(app, "skip_thumbnail", True)
    setattr(app, "music_payload", {"mode": checked["musicMode"]})
    setattr(app, "content_declaration", str(checked["contentDeclaration"]))
    if discovery:
        return


def _verified_douyin_identity(expected_name: str, actual_name: object) -> str:
    """验证官方身份回执与任务指定账号完全一致。"""

    actual = _normalized(actual_name)
    if not actual:
        raise DouyinPublishError("抖音官方身份回执未包含可用账号名，已停止发布")
    if actual != expected_name:
        raise DouyinPublishError(
            f"抖音官方身份回执与任务账号不一致：期望“{expected_name}”，实际“{actual}”"
        )
    return actual


async def _readback_douyin_session_identity(
    page,
    expected_name: str,
    *,
    reveal: bool = True,
) -> str:
    """通过官方首页自然加载的身份回执确认当前会话账号。

    新版视频编辑页的顶部仅显示头像，页面文字“网址”等导航项不能作为
    账号昵称。这里不自行构造接口请求：只监听打开官方首页时页面自然发起
    的 ``media/user/info`` 回执，并只保留昵称用于本次一致性判断。
    """

    from .oneclick_authorization import (
        identity_response_display_name,
        login_response_confirms,
    )
    from utils.base_social_media import goto_and_reveal

    identity: dict[str, str] = {"name": ""}
    received = asyncio.Event()

    async def inspect(response) -> None:
        try:
            response_url = str(response.url or "")
            if "media/user/info" not in response_url:
                return
            body = await response.json()
            if not login_response_confirms(3, response_url, body):
                return
            name = _normalized(identity_response_display_name(body))
            if name:
                identity["name"] = name
                received.set()
        except Exception:
            return

    def observe(response) -> None:
        asyncio.create_task(inspect(response))

    page.on("response", observe)
    try:
        if reveal:
            await goto_and_reveal(page, _DOUYIN_HOME_URL, timeout=45_000)
        else:
            # 分步带货向导的首次上传保持后台执行；若会话无效，调用方会
            # 明确前置浏览器并停止，不会在隐藏窗口里继续尝试登录。
            await page.goto(
                _DOUYIN_HOME_URL,
                wait_until="domcontentloaded",
                timeout=45_000,
            )
        try:
            await asyncio.wait_for(received.wait(), timeout=10_000 / 1000)
        except asyncio.TimeoutError:
            pass
    finally:
        try:
            page.remove_listener("response", observe)
        except Exception:
            pass
    return _verified_douyin_identity(expected_name, identity["name"])


def _requires_foreground_hold(message: object) -> bool:
    """仅在必须由用户处理的验证、登录或平台确认场景保留前台页面。

    选择器不匹配、音乐/门店回读失败等可诊断代码错误不能在这里无限等待，
    否则客户端不会收到失败回调，用户只会看到编辑页“停住”。
    """

    text = _normalized(message)
    markers = (
        "二次安全验证",
        "短信验证码",
        "原设备扫码",
        "扫码",
        "二维码",
        "验证码",
        "登录失效",
        "重新登录",
        "请登录",
        "风控",
        "合规",
        "点击发布后未确认跳转",
        "正在等待二次安全验证",
        "定时提交后未能从作品管理页回读",
    )
    return any(marker in text for marker in markers)


async def _hold_frontend_for_user(
    page,
    browser,
    *,
    task_id: int | None,
    reason: str,
) -> None:
    """未知确认或安全验证时不关闭浏览器，等待用户自行处理或关闭。"""

    try:
        # 发布浏览器默认先放到屏幕外，只有确实需要人工处理时才前置显示。
        # 单纯 bring_to_front 不能把 -32000 坐标的窗口带回可见屏幕。
        from utils.base_social_media import reveal_page_window

        await reveal_page_window(page)
        await page.bring_to_front()
    except Exception:
        pass
    if task_id is not None:
        task_service.record_task_event(
            int(task_id),
            "douyin_intervention_required",
            "抖音发布需要用户在前台浏览器处理验证或确认；一键发已停止自动操作。"
            f"原因：{reason}",
            level="warning",
        )
    while browser is not None and browser.is_connected() and not page.is_closed():
        await asyncio.sleep(0.5)


async def run_douyin_publish(payload: Mapping[str, Any], *, task_id: int) -> dict[str, Any]:
    """在一键发受控会话内完成抖音视频发布并要求平台管理页回执。"""

    checked = validate_douyin_publish_payload(payload)
    target_schedule = _scheduled_time(checked)
    account = _account_for_payload(checked)
    if int(account.get("type") or 0) != 3:
        raise DouyinPublishError("选中的账号不是抖音账号")
    expected_account = _expected_account_name(account)
    if not expected_account:
        raise DouyinPublishError("抖音账号缺少可回读的账号名，已停止发布")
    storage_state = _storage_state(account)

    # 延迟导入浏览器与恢复上传器，使离线载荷测试不依赖 Playwright 环境。
    from playwright.async_api import async_playwright
    from uploader.douyin_uploader.main import DouYinVideo
    from utils.base_social_media import (
        goto_and_reveal,
        launch_publish_browser,
        new_publish_context,
        set_init_script,
    )
    from utils.publish_observer import publish_context

    playwright = await async_playwright().start()
    browser = None
    context = None
    page = None
    try:
        browser = await launch_publish_browser(playwright)
        context = await new_publish_context(browser, storage_state=str(storage_state))
        context = await set_init_script(context)
        page = await context.new_page()
        editor_account = await _readback_douyin_session_identity(
            page, expected_account
        )
        task_service.record_task_event(
            int(task_id),
            "douyin_session_identity_readback",
            f"抖音官方身份回执已回读：{editor_account}",
        )

        cover_paths = checked.get("coverPaths")
        if not isinstance(cover_paths, dict):
            cover_paths = {}
        is_commerce_workflow = _is_commerce_workflow(checked)
        app = DouYinVideo(
            title=str(checked["title"]),
            file_path=str(checked["fileList"][0]),
            tags=list(checked.get("tags") or []),
            publish_date=target_schedule or 0,
            account_file=storage_state,
            category=checked.get("category"),
            # 带货视频不把发布中心遗留的封面字段交给上传器；普通抖音发布
            # 保持原有封面能力。
            thumbnail_path=(
                None if is_commerce_workflow else str(checked.get("coverPath") or "")
            ),
            thumbnail_paths=(
                {}
                if is_commerce_workflow
                else {str(key): str(value) for key, value in cover_paths.items() if value}
            ),
            dry_run=False,
            dry_run_hold_browser=False,
            save_draft_only=False,
            description=str(checked["description"]),
        )
        app.ai_generated = False
        app.content_declaration = str(checked.get("contentDeclaration") or "")
        app.sync_to_toutiao = checked.get("syncToToutiao") is True
        app.location_payload = {
            "locationKeyword": checked["locationKeyword"],
            "locationPoi": dict(checked["locationPoi"]),
        }
        if is_commerce_workflow:
            _configure_commerce_editor(app, checked)
        app.external_page = page
        app.external_context = context
        app.external_browser = browser

        with publish_context(
            task_id=int(task_id),
            mode="single",
            background_mode=False,
            platform_type=3,
            platform_name="抖音",
        ):
            receipt = await app.upload(playwright)
        if not isinstance(receipt, dict) or receipt.get("status") != "published":
            raise DouyinPublishError("抖音未返回可验证的平台发布回执")

        location_name = _normalized(getattr(app, "location_verification", ""))
        if checked["locationKeyword"] and location_name != checked["locationKeyword"]:
            raise DouyinPublishError("抖音发布定位最终回读不一致，未记录为发布成功")

        content_declaration = ""
        music = None
        if _is_commerce_workflow(checked):
            music = _music_readback(getattr(app, "music_verification", None))
            content_declaration = douyin_commerce_service.normalize_content_declaration(
                getattr(app, "content_declaration_verification", "")
            )
            if content_declaration != checked["contentDeclaration"]:
                raise DouyinPublishError("抖音作品内容声明最终回读不一致，未记录为发布成功")
            task_service.record_task_event(
                int(task_id),
                "douyin_commerce_music_readback",
                "抖音带货已从收藏列表选择并回读音乐："
                f"{music['title']}（{music.get('creator') or '平台未返回作者'}，{music['duration']}）",
            )
            task_service.record_task_event(
                int(task_id),
                "douyin_commerce_declaration_readback",
                f"抖音作品内容声明已由编辑页回读：{content_declaration}",
            )

        scheduled_readback = None
        if target_schedule is not None:
            scheduled_readback = await _scheduled_submission_readback(
                page,
                title=str(checked["title"]),
                target=target_schedule,
            )
            message = (
                "抖音定时提交已由作品管理页回读："
                f"{scheduled_readback['scheduledAt']}；账号={editor_account}"
            )
        else:
            message = f"抖音已进入作品管理页，平台发布回执已确认：{editor_account}"
        if location_name:
            message += f"；发布定位“{location_name}”已回读"
        if content_declaration:
            message += f"；作品内容声明“{content_declaration}”已回读"
        if music:
            message += f"；收藏音乐“{music['title']}”已回读"
        task_service.record_task_event(
            int(task_id),
            "douyin_publish_receipt",
            message,
        )
        return {
            "ok": True,
            "message": message,
            "account": editor_account,
            "location": location_name,
            "contentDeclaration": content_declaration,
            "music": music,
            "scheduled": target_schedule is not None,
            "scheduledAt": (
                target_schedule.strftime("%Y-%m-%d %H:%M")
                if target_schedule is not None
                else None
            ),
            "scheduledReadback": scheduled_readback,
            "platformReceipt": dict(receipt),
        }
    except Exception as exc:
        if page is not None and browser is not None and _requires_foreground_hold(str(exc)):
            await _hold_frontend_for_user(
                page,
                browser,
                task_id=int(task_id),
                reason=_normalized(str(exc))[:280],
            )
        raise
    finally:
        # 正常完成或用户关闭前台验证页后清理一键发临时浏览器与连接。
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


async def run_douyin_commerce_preflight(
    payload: Mapping[str, Any], *, task_id: int
) -> dict[str, Any]:
    """执行抖音带货发布前预检，只填写/回读，绝不点击最终发表。"""

    if str(payload.get("runtimeMode") or "") != "preflight":
        raise DouyinPublishError("抖音带货预检必须明确 runtimeMode=preflight")
    if payload.get("debugDryRun") is not True:
        raise DouyinPublishError("抖音带货预检必须明确 debugDryRun=true")
    try:
        checked = douyin_commerce_service.validate_douyin_commerce_payload(payload)
    except douyin_commerce_service.DouyinCommerceError as exc:
        raise DouyinPublishError(str(exc)) from exc
    target_schedule = _scheduled_time(checked)

    account = _account_for_payload(checked)
    if int(account.get("type") or 0) != 3:
        raise DouyinPublishError("选中的账号不是抖音账号")
    expected_account = _expected_account_name(account)
    if not expected_account:
        raise DouyinPublishError("抖音账号缺少可回读的账号名，已停止预检")
    storage_state = _storage_state(account)

    from playwright.async_api import async_playwright
    from uploader.douyin_uploader.main import DouYinVideo
    from utils.base_social_media import (
        launch_publish_browser,
        new_publish_context,
        set_init_script,
    )
    from utils.publish_observer import publish_context

    playwright = await async_playwright().start()
    browser = None
    context = None
    page = None
    try:
        # 预检同样保持前台：若出现二维码、验证码或页面控件变化，用户可以
        # 直接看到当前状态；执行器不会替用户处理验证。
        browser = await launch_publish_browser(playwright)
        context = await new_publish_context(browser, storage_state=str(storage_state))
        context = await set_init_script(context)
        page = await context.new_page()
        editor_account = await _readback_douyin_session_identity(page, expected_account)
        task_service.record_task_event(
            int(task_id),
            "douyin_commerce_session_identity_readback",
            f"抖音带货账号已由官方身份回执确认：{editor_account}",
        )
        app = DouYinVideo(
            title=str(checked["title"]),
            file_path=str(checked["fileList"][0]),
            tags=list(checked.get("tags") or []),
            publish_date=target_schedule or 0,
            account_file=storage_state,
            category=checked.get("category"),
            # 抖音带货预检完全跳过独立封面：不打开、不上传、不确认封面。
            thumbnail_path=None,
            thumbnail_paths={},
            dry_run=True,
            dry_run_hold_browser=False,
            save_draft_only=False,
            description=str(checked["description"]),
        )
        # 带货流程的作品内容声明必须由用户选择，不能由载荷中的 AI 标记推断。
        app.ai_generated = False
        app.content_declaration = str(checked["contentDeclaration"])
        app.sync_to_toutiao = False
        app.location_payload = {
            "locationKeyword": checked["locationKeyword"],
            "locationPoi": dict(checked["locationPoi"]),
        }
        _configure_commerce_editor(app, checked)
        app.external_page = page
        app.external_context = context
        app.external_browser = browser
        with publish_context(
            task_id=int(task_id),
            mode="preflight",
            background_mode=False,
            platform_type=3,
            platform_name="抖音",
        ):
            await app.upload(playwright)

        location_name = _normalized(getattr(app, "location_verification", ""))
        if location_name != checked["locationKeyword"]:
            raise DouyinPublishError("抖音带货预检的官方地点回读不一致")
        content_declaration = douyin_commerce_service.normalize_content_declaration(
            getattr(app, "content_declaration_verification", "")
        )
        if content_declaration != checked["contentDeclaration"]:
            raise DouyinPublishError("抖音带货预检的作品内容声明回读不一致")
        music = _music_readback(getattr(app, "music_verification", None))
        if target_schedule is not None:
            schedule_value = _normalized(getattr(app, "schedule_verification", ""))
            if not DouYinVideo._schedule_time_matches(
                schedule_value, target_schedule.strftime("%Y-%m-%d %H:%M")
            ):
                raise DouyinPublishError(
                    "抖音带货预检未能回读指定的北京时间定时，未进入提交确认"
                )
            publish_mode = f"定时={target_schedule:%Y-%m-%d %H:%M}（北京时间）"
        else:
            publish_mode = "立即发表（最终仍需人工确认）"
        message = (
            "抖音带货预检已回读账号、官方地点、作品内容声明和发布方式："
            f"账号={editor_account}；地点={location_name}；"
            f"声明={content_declaration}；收藏音乐={music['title']}；"
            f"{publish_mode}"
        )
        task_service.record_task_event(
            int(task_id), "douyin_commerce_preflight_readback", message
        )
        return {
            "ok": True,
            "message": message,
            "account": editor_account,
            "location": location_name,
            "contentDeclaration": content_declaration,
            "music": music,
            "scheduled": target_schedule is not None,
            "scheduledAt": (
                target_schedule.strftime("%Y-%m-%d %H:%M")
                if target_schedule is not None
                else None
            ),
            "dryRun": True,
        }
    except Exception as exc:
        # 只有验证码、扫码、登录/风控等外部人工关口才保持前台。可诊断的
        # 页面/选择器失败必须立即回传给客户端，避免“浏览器看似停住”。
        if (
            page is not None
            and browser is not None
            and _requires_foreground_hold(str(exc))
        ):
            await _hold_frontend_for_user(
                page,
                browser,
                task_id=int(task_id),
                reason=_normalized(str(exc))[:280],
            )
        raise
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


async def run_douyin_commerce_store_discovery(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """在临时编辑页读取本地团购门店候选，不保存、不发表、不绑定门店。

    抖音仅在视频、文案与地点已经进入编辑页后显示带货相关控件。因此这里
    复用已受控的上传器完成必要的临时字段填写，再只读取平台可见候选。该
    阶段没有 ``commerceStore``、没有定时，也不会点击草稿或最终发表。
    """

    try:
        checked = douyin_commerce_service.validate_douyin_commerce_discovery_payload(
            payload
        )
    except douyin_commerce_service.DouyinCommerceError as exc:
        raise DouyinPublishError(str(exc)) from exc
    account = _account_for_payload(checked)
    if int(account.get("type") or 0) != 3:
        raise DouyinPublishError("选中的账号不是抖音账号")
    expected_account = _expected_account_name(account)
    if not expected_account:
        raise DouyinPublishError("抖音账号缺少可回读的账号名，已停止门店读取")
    storage_state = _storage_state(account)

    from playwright.async_api import async_playwright
    from uploader.douyin_uploader.main import DouYinVideo
    from utils.base_social_media import (
        launch_publish_browser,
        new_publish_context,
        set_init_script,
    )
    from utils.publish_observer import publish_context

    playwright = await async_playwright().start()
    browser = None
    context = None
    page = None
    try:
        # 读取门店前的临时编辑预检保持前台可见；普通成功读取后关闭，若出现
        # 验证、登录失效或未知控件则保持页面，绝不继续猜测。
        browser = await launch_publish_browser(playwright)
        context = await new_publish_context(browser, storage_state=str(storage_state))
        context = await set_init_script(context)
        page = await context.new_page()
        editor_account = await _readback_douyin_session_identity(page, expected_account)
        app = DouYinVideo(
            title=str(checked["title"]),
            file_path=str(checked["fileList"][0]),
            tags=list(checked.get("tags") or []),
            publish_date=0,
            account_file=storage_state,
            category=checked.get("category"),
            # 门店读取阶段是临时预检，也不继承任何封面字段。
            thumbnail_path=None,
            thumbnail_paths={},
            dry_run=True,
            dry_run_hold_browser=False,
            save_draft_only=False,
            description=str(checked["description"]),
        )
        app.location_payload = {
            "locationKeyword": checked["locationKeyword"],
            "locationPoi": dict(checked["locationPoi"]),
        }
        _configure_commerce_editor(app, checked, discovery=True)
        app.external_page = page
        app.external_context = context
        app.external_browser = browser
        with publish_context(
            mode="commerce_store_discovery",
            background_mode=False,
            platform_type=3,
            platform_name="抖音",
        ):
            await app.upload(playwright)

        location_name = _normalized(getattr(app, "location_verification", ""))
        if location_name != checked["locationKeyword"]:
            raise DouyinPublishError("读取门店时官方地点回读不一致")
        stores = [dict(item) for item in getattr(app, "commerce_candidates", [])]
        if not stores:
            raise DouyinPublishError("抖音未返回可唯一回读的团购门店候选")
        music = _music_readback(getattr(app, "music_verification", None))
        return {
            "ok": True,
            "account": editor_account,
            "location": location_name,
            "stores": stores,
            "music": music,
            "message": (
                f"已读取 {len(stores)} 个可绑定团购门店，并回读收藏音乐“{music['title']}”；"
                "未保存草稿或发表。"
            ),
            "dryRun": True,
        }
    except Exception as exc:
        # 门店读取也遵守同一规则：只有真的需要用户扫码、登录或处理平台
        # 风控时才保留浏览器；音乐/定位/门店控件错误直接返回桌面客户端。
        if (
            page is not None
            and browser is not None
            and _requires_foreground_hold(str(exc))
        ):
            await _hold_frontend_for_user(
                page,
                browser,
                task_id=None,
                reason=_normalized(str(exc))[:280],
            )
        raise
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


def run_douyin_publish_sync(payload: Mapping[str, Any], *, task_id: int) -> dict[str, Any]:
    """供桌面任务线程调用的同步入口。"""

    return asyncio.run(run_douyin_publish(payload, task_id=task_id))


def run_douyin_commerce_preflight_sync(
    payload: Mapping[str, Any], *, task_id: int
) -> dict[str, Any]:
    """供桌面任务线程调用的抖音带货预检同步入口。"""

    return asyncio.run(run_douyin_commerce_preflight(payload, task_id=task_id))


def run_douyin_commerce_store_discovery_sync(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """供带货页面后台线程读取可绑定门店的同步入口。"""

    return asyncio.run(run_douyin_commerce_store_discovery(payload))
