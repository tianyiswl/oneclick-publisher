# -*- coding: utf-8 -*-
"""抖音带货分步编辑会话。

抖音的收藏音乐、发布定位和作品内容声明都只有在视频上传完成后的编辑页才会
出现。本模块将这一临时编辑页保持在一个专用的内存会话中：上传只做一次，
用户在客户端选择音乐后，按平台实际的“位置 → 带货模式”流程选择发布定位和
声明，再继续定时与预检。

安全边界：

* 不保存草稿、不点击预览、不点击发表，除非桌面端已完成独立最终确认后显式
  调用 ``submit``；
* 不写入 storage state，不导出 Cookie、二维码、DOM 或平台原始请求；
* 音乐、门店候选只来自当前会话的可见平台控件，瞬态 DOM 标记不会返回给 UI
  或写入任务；
* 会话关闭/改选素材时关闭临时浏览器，平台上不会留下草稿。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import threading
from typing import Any, Mapping
from uuid import uuid4

from . import (
    douyin_commerce_service,
    douyin_music_service,
    douyin_publish_executor,
)
from .oneclick_preflight import _account_for_payload, _storage_state


class DouyinCommerceSessionError(RuntimeError):
    """分步编辑会话无法继续时抛出。"""


def _normalized(value: object) -> str:
    return " ".join(str(value or "").replace("\u200b", " ").split())


def _public_music(value: Mapping[str, Any]) -> dict[str, str]:
    music = douyin_music_service.normalize_music_readback(value)
    if not music:
        raise DouyinCommerceSessionError("抖音收藏音乐缺少可验证的回读字段")
    return music


def _same_music(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return all(
        _normalized(left.get(key)) == _normalized(right.get(key))
        for key in ("musicId", "title", "creator", "duration")
    )


def _same_location(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return all(
        _normalized(left.get(key)) == _normalized(right.get(key))
        for key in ("poiId", "name", "address")
    )


def _same_store(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return all(
        _normalized(left.get(key)) == _normalized(right.get(key))
        for key in ("storeId", "name", "address", "poiId")
    )


@dataclass
class _CommerceEditorSession:
    """只在进程内存在的受控抖音编辑器状态。"""

    session_id: str
    upload_payload: dict[str, Any]
    account_name: str
    browser: Any
    context: Any
    page: Any
    playwright: Any
    uploader: Any
    stage: str = "uploaded"
    music_picker_page: Any | None = None
    music_dialog: Any | None = None
    music_candidates: list[dict[str, str]] = field(default_factory=list)
    selected_music: dict[str, str] | None = None
    commerce_location_candidates: list[dict[str, Any]] = field(default_factory=list)
    location: dict[str, str] | None = None
    location_scope: str = ""
    selected_declaration: str = ""
    # 历史字段仅用于安全清理旧会话，不再写入新任务或驱动门店绑定。
    stores: list[dict[str, str]] = field(default_factory=list)
    selected_store: dict[str, str] | None = None
    preflight_fingerprint: str = ""
    schedule_time: str = ""


class DouyinCommerceSessionManager:
    """在固定 asyncio 线程中持有一个抖音带货编辑会话。

    Qt 的每次按钮回调都会进入不同的后台线程；Playwright 对象不能跨这些
    线程/事件循环使用。因此本管理器自己持有一个单独事件循环，外部只调用
    同步的安全入口。这样既能让客户端不冻结，又能避免重复上传视频。
    """

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._thread_lock = threading.RLock()
        self._session: _CommerceEditorSession | None = None

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._thread_lock:
            if self._thread is None or not self._thread.is_alive():
                self._ready.clear()
                self._thread = threading.Thread(
                    target=self._loop_main,
                    name="oneclick-douyin-commerce-session",
                    daemon=True,
                )
                self._thread.start()
        if not self._ready.wait(timeout=5):
            raise DouyinCommerceSessionError("抖音带货临时会话未能启动")
        if self._loop is None:
            raise DouyinCommerceSessionError("抖音带货临时会话事件循环不可用")
        return self._loop

    def _loop_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        loop.run_forever()

    def _call(self, coroutine):
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        return future.result()

    @staticmethod
    def _upload_fingerprint(payload: Mapping[str, Any]) -> str:
        return "|".join(
            (
                _normalized((payload.get("accountList") or [""])[0]),
                _normalized((payload.get("fileList") or [""])[0]),
                _normalized(payload.get("title")),
                _normalized(payload.get("description")),
                ",".join(_normalized(item) for item in payload.get("tags") or []),
            )
        )

    @staticmethod
    def _preflight_fingerprint(payload: Mapping[str, Any]) -> str:
        music = payload.get("selectedMusic") if isinstance(payload.get("selectedMusic"), Mapping) else {}
        poi = payload.get("locationPoi") if isinstance(payload.get("locationPoi"), Mapping) else {}
        return "|".join(
            (
                DouyinCommerceSessionManager._upload_fingerprint(payload),
                _normalized(music.get("musicId")),
                _normalized(music.get("title")),
                _normalized(poi.get("poiId")),
                _normalized(payload.get("locationScope")),
                _normalized(payload.get("contentDeclaration")),
                _normalized(payload.get("scheduleTime")),
            )
        )

    def start_upload(self, payload: Mapping[str, Any]) -> dict[str, str]:
        """后台上传一次视频并停在同一编辑会话，尚不选音乐、地点或声明。"""

        checked = douyin_commerce_service.validate_douyin_commerce_upload_payload(
            payload
        )
        return self._call(self._start_upload(dict(checked)))

    def load_favorite_music(self, session_id: str) -> list[dict[str, str]]:
        """读取当前编辑页收藏音乐候选，供用户在客户端选择。"""

        return self._call(self._load_favorite_music(session_id))

    def select_favorite_music(self, session_id: str, music_id: str) -> dict[str, str]:
        """选择用户指定的当前收藏音乐并回读。"""

        return self._call(self._select_favorite_music(session_id, music_id))

    def search_locations(
        self,
        session_id: str,
        keyword: object,
        scope: object,
    ) -> list[dict[str, Any]]:
        """在当前已上传编辑页搜索发布定位候选，不另开浏览器或使用私有请求。"""

        return self._call(self._search_locations(session_id, keyword, scope))

    def apply_location(
        self,
        session_id: str,
        location: Mapping[str, Any],
    ) -> dict[str, Any]:
        """只选择并回读当前带货页返回的发布定位，不绑定门店。"""

        normalized = douyin_commerce_service.normalize_commerce_location_candidate(location)
        if not normalized:
            raise DouyinCommerceSessionError("请选择一键发返回的完整发布定位候选")
        return self._call(self._apply_location(session_id, dict(normalized)))

    def select_content_declaration(self, session_id: str, declaration: object) -> str:
        """选择用户明确指定的作品内容声明，并从当前编辑页回读。"""

        normalized = douyin_commerce_service.normalize_content_declaration(declaration)
        return self._call(self._select_content_declaration(session_id, normalized))

    def load_stores(self, session_id: str) -> list[dict[str, str]]:
        """从当前编辑页读取可绑定的带货门店，不创建本地门店库。"""

        return self._call(self._load_stores(session_id))

    def select_store(self, session_id: str, store_id: str) -> dict[str, str]:
        """绑定用户指定的当前平台门店并回读。"""

        return self._call(self._select_store(session_id, store_id))

    def preflight(self, session_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """在已上传会话内回读全部字段及定时，不点击发表。"""

        checked = douyin_commerce_service.validate_douyin_commerce_payload(payload)
        if str(checked.get("runtimeMode") or "") != "preflight" or checked.get("debugDryRun") is not True:
            raise DouyinCommerceSessionError("抖音带货预检必须保持 dry-run 模式")
        return self._call(self._preflight(session_id, dict(checked)))

    def submit(self, session_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """在已预检的同一会话中执行明确确认后的最终定时提交。"""

        checked = douyin_commerce_service.validate_douyin_commerce_payload(payload)
        if str(checked.get("runtimeMode") or "") != "publish" or checked.get("debugDryRun") is not False:
            raise DouyinCommerceSessionError("抖音带货最终提交必须明确 runtimeMode=publish")
        return self._call(self._submit(session_id, dict(checked)))

    def close(self, session_id: str | None = None) -> None:
        """放弃本次临时编辑页，不保存草稿或任何会话状态。"""

        try:
            self._call(self._close(session_id))
        except DouyinCommerceSessionError:
            # 页面销毁或应用退出时的重复关闭无需打断 UI。
            return

    def status(self) -> dict[str, str]:
        return self._call(self._status())

    async def _current(self, session_id: str) -> _CommerceEditorSession:
        session = self._session
        if session is None:
            raise DouyinCommerceSessionError("当前没有可继续的抖音带货上传会话")
        if _normalized(session_id) != session.session_id:
            raise DouyinCommerceSessionError("抖音带货会话已更新，请从当前步骤重新开始")
        if session.page is None or session.page.is_closed():
            raise DouyinCommerceSessionError("抖音临时编辑页已关闭，请重新上传视频")
        return session

    async def _start_upload(self, payload: dict[str, Any]) -> dict[str, str]:
        await self._close(None)
        from playwright.async_api import async_playwright
        from uploader.douyin_uploader.main import DouYinVideo
        from utils.base_social_media import launch_publish_browser, new_publish_context, set_init_script
        from utils.publish_observer import publish_context

        account = _account_for_payload(payload)
        expected_account = _normalized(account.get("profileName") or account.get("userName"))
        if not expected_account:
            raise DouyinCommerceSessionError("抖音账号缺少可回读的账号名，请先在账号管理中重新绑定")
        storage_state = _storage_state(account)
        playwright = await async_playwright().start()
        browser = context = page = None
        success = False
        try:
            with publish_context(
                mode="douyin_commerce_upload",
                background_mode=False,
                platform_type=3,
                platform_name="抖音",
            ):
                browser = await launch_publish_browser(playwright)
                context = await new_publish_context(browser, storage_state=str(storage_state))
                context = await set_init_script(context)
                page = await context.new_page()
                actual_account = await douyin_publish_executor._readback_douyin_session_identity(
                    page,
                    expected_account,
                    reveal=False,
                )
                uploader = DouYinVideo(
                    title=str(payload["title"]),
                    file_path=str(payload["fileList"][0]),
                    tags=list(payload.get("tags") or []),
                    publish_date=0,
                    account_file=storage_state,
                    category=payload.get("category"),
                    thumbnail_path=None,
                    thumbnail_paths={},
                    dry_run=True,
                    dry_run_hold_browser=False,
                    save_draft_only=False,
                    description=str(payload["description"]),
                )
                # 带货流程不能误入独立封面裁剪；音乐与地点会由后续用户步骤
                # 显式处理，当前仅完成视频、标题、文案和话题。
                uploader.skip_thumbnail = True
                uploader.external_page = page
                uploader.external_context = context
                uploader.external_browser = browser
                await uploader.prepare_uploaded_video_editor(page, reveal_editor=False)

            session = _CommerceEditorSession(
                session_id=uuid4().hex,
                upload_payload=dict(payload),
                account_name=actual_account,
                browser=browser,
                context=context,
                page=page,
                playwright=playwright,
                uploader=uploader,
            )
            self._session = session
            success = True
            return {
                "sessionId": session.session_id,
                "account": actual_account,
                "video": Path(str(payload["fileList"][0])).name,
                "message": "视频已上传并回读标题、文案；尚未选择音乐、地点、声明或定时。",
            }
        except Exception as exc:
            raise DouyinCommerceSessionError(
                f"抖音带货后台上传未完成：{_normalized(str(exc))[:260]}"
            ) from exc
        finally:
            if not success:
                await self._close_resources(
                    context=context,
                    browser=browser,
                    playwright=playwright,
                )

    async def _load_favorite_music(self, session_id: str) -> list[dict[str, str]]:
        session = await self._current(session_id)
        if session.stage == "music_candidates_loaded":
            return [_public_music(item) for item in session.music_candidates]
        if session.stage != "uploaded":
            raise DouyinCommerceSessionError("请在上传完成后、选择地点前读取收藏音乐")
        try:
            picker_page, dialog, candidates = await douyin_music_service.open_favorite_music_choices(
                session.page
            )
        except douyin_music_service.DouyinMusicError as exc:
            raise DouyinCommerceSessionError(f"读取抖音收藏音乐失败：{exc}") from exc
        session.music_picker_page = picker_page
        session.music_dialog = dialog
        session.music_candidates = [dict(item) for item in candidates]
        session.stage = "music_candidates_loaded"
        return [_public_music(item) for item in session.music_candidates]

    async def _select_favorite_music(
        self,
        session_id: str,
        music_id: str,
    ) -> dict[str, str]:
        session = await self._current(session_id)
        if session.stage != "music_candidates_loaded":
            raise DouyinCommerceSessionError("请先读取当前账号的收藏音乐，再选择其中一首")
        candidates = [
            item
            for item in session.music_candidates
            if _normalized(item.get("musicId")) == _normalized(music_id)
        ]
        if len(candidates) != 1:
            raise DouyinCommerceSessionError("所选音乐不是当前收藏列表中的唯一候选，请重新读取")
        try:
            selected = await douyin_music_service.select_favorite_music_choice(
                session.page,
                session.music_picker_page,
                session.music_dialog,
                candidates[0],
            )
        except douyin_music_service.DouyinMusicError as exc:
            raise DouyinCommerceSessionError(f"抖音收藏音乐未能选择并回读：{exc}") from exc
        session.selected_music = _public_music(selected)
        session.music_picker_page = None
        session.music_dialog = None
        # 瞬态 marker 仅在刚才的点击中存在，选择完成后立即清除。
        session.music_candidates = []
        session.stage = "music_selected"
        return dict(session.selected_music)

    async def _search_locations(
        self,
        session_id: str,
        keyword: object,
        scope: object,
    ) -> list[dict[str, Any]]:
        session = await self._current(session_id)
        if session.stage not in {
            "music_selected",
            "location_selected",
            "declaration_selected",
            "stores_loaded",
            "store_selected",
            "preflighted",
        }:
            raise DouyinCommerceSessionError("请先由用户选择并确认收藏音乐")
        try:
            selected_scope = douyin_commerce_service.normalize_commerce_location_scope(scope)
            candidates = await douyin_commerce_service.search_commerce_location_store_candidates(
                session.page,
                keyword,
                scope=selected_scope,
            )
        except Exception as exc:
            raise DouyinCommerceSessionError(
                f"抖音带货位置搜索失败：{_normalized(str(exc))[:260]}"
            ) from exc
        # 新搜索结果会改变发布定位。任何此前的门店选择、预检或定时回读都
        # 必须失效，避免误把旧门店用于新地点。
        session.commerce_location_candidates = [dict(item) for item in candidates]
        session.location = None
        session.location_scope = selected_scope
        session.selected_declaration = ""
        session.stores = []
        session.selected_store = None
        session.preflight_fingerprint = ""
        session.schedule_time = ""
        session.stage = "music_selected"
        return [dict(item) for item in session.commerce_location_candidates]

    async def _apply_location(
        self,
        session_id: str,
        candidate: dict[str, Any],
    ) -> dict[str, Any]:
        session = await self._current(session_id)
        if session.stage not in {
            "music_selected",
            "location_selected",
            "declaration_selected",
            "stores_loaded",
            "store_selected",
            "preflighted",
        }:
            raise DouyinCommerceSessionError("请先由用户选择并确认收藏音乐")
        normalized_candidate = douyin_commerce_service.normalize_commerce_location_candidate(
            candidate
        )
        if not normalized_candidate:
            raise DouyinCommerceSessionError("所选发布定位候选缺少完整地址或稳定身份")
        matching_candidates = [
            item
            for item in session.commerce_location_candidates
            if _same_location(item, normalized_candidate)
        ]
        if len(matching_candidates) != 1:
            raise DouyinCommerceSessionError(
                "所选位置不是当前抖音带货搜索结果中的唯一候选，请重新搜索"
            )
        try:
            result = await douyin_commerce_service.apply_commerce_location_to_page(
                session.page,
                matching_candidates[0],
            )
        except Exception as exc:
            raise DouyinCommerceSessionError(
                f"抖音发布定位未能选择并回读：{_normalized(str(exc))[:260]}"
            ) from exc
        location = result.get("location") if isinstance(result, Mapping) else None
        if not isinstance(location, Mapping):
            raise DouyinCommerceSessionError("抖音发布定位未返回完整页面回读")
        if not _same_location(location, normalized_candidate):
            raise DouyinCommerceSessionError("抖音发布定位回读与所选候选不一致")
        # 发布定位只写入地点回读。门店必须由后续单独步骤读取、选择并验证，
        # 不能把同一控件中展示的商品摘要误记录成已绑定门店。
        session.location = {key: _normalized(location.get(key)) for key in ("poiId", "name", "address", "distance")}
        session.selected_declaration = ""
        session.stores = []
        session.selected_store = None
        if session.uploader is not None:
            session.uploader.location_verification = session.location["name"]
        session.preflight_fingerprint = ""
        session.schedule_time = ""
        session.stage = "location_selected"
        return {"location": dict(session.location)}

    async def _select_content_declaration(
        self,
        session_id: str,
        declaration: str,
    ) -> str:
        session = await self._current(session_id)
        if session.stage not in {"location_selected", "declaration_selected", "preflighted"}:
            raise DouyinCommerceSessionError("请先选择并回读发布定位")
        selected = douyin_commerce_service.normalize_content_declaration(declaration)
        try:
            actual = await session.uploader.set_content_declaration(session.page, selected)
        except Exception as exc:
            raise DouyinCommerceSessionError(
                f"抖音作品内容声明未能选择并回读：{_normalized(str(exc))[:260]}"
            ) from exc
        actual_value = douyin_commerce_service.normalize_content_declaration(actual)
        if actual_value != selected:
            raise DouyinCommerceSessionError("抖音作品内容声明回读与用户所选项不一致")
        session.selected_declaration = actual_value
        session.preflight_fingerprint = ""
        session.schedule_time = ""
        session.stage = "declaration_selected"
        return actual_value

    async def _load_stores(self, session_id: str) -> list[dict[str, str]]:
        session = await self._current(session_id)
        if session.location is None or session.stage not in {
            "location_selected",
            "stores_loaded",
            "store_selected",
            "preflighted",
        }:
            raise DouyinCommerceSessionError("请先在当前编辑页选择并回读发布定位")
        try:
            candidates = await douyin_commerce_service.read_commerce_store_candidates(
                session.page,
                session.location,
            )
        except Exception as exc:
            raise DouyinCommerceSessionError(
                f"抖音带货门店候选未能唯一读取：{_normalized(str(exc))[:260]}"
            ) from exc
        # 候选读取后故意保留这个已经唯一确认的平台下拉。下一步绑定会复用
        # 同一列表，避免为了“关闭菜单”再次点击控件而误触或被浮层拦截。
        session.stores = [dict(item) for item in candidates]
        session.selected_store = None
        session.preflight_fingerprint = ""
        session.schedule_time = ""
        session.stage = "stores_loaded"
        return [dict(item) for item in session.stores]

    async def _select_store(self, session_id: str, store_id: str) -> dict[str, str]:
        session = await self._current(session_id)
        if session.location is None or session.stage != "stores_loaded":
            raise DouyinCommerceSessionError("请先读取当前地点下可绑定的门店")
        matches = [
            item
            for item in session.stores
            if _normalized(item.get("storeId")) == _normalized(store_id)
        ]
        if len(matches) != 1:
            raise DouyinCommerceSessionError("所选门店不是当前地点的唯一平台候选")
        try:
            selected = await douyin_commerce_service.apply_commerce_store_to_page(
                session.page,
                matches[0],
                session.location,
            )
        except Exception as exc:
            raise DouyinCommerceSessionError(
                f"抖音带货门店未能绑定并回读：{_normalized(str(exc))[:260]}"
            ) from exc
        # 平台在选中门店后可能自行收起下拉，也可能保留它；两种状态都不影响
        # 已完成的身份回读。这里不再为“强制关闭”二次点击控件，避免浮层拦截
        # 或页面保留层导致客户端把成功绑定误报为失败。
        if not _same_store(selected, matches[0]):
            raise DouyinCommerceSessionError("抖音带货门店回读与所选候选不一致")
        session.selected_store = dict(selected)
        session.preflight_fingerprint = ""
        session.schedule_time = ""
        session.stage = "store_selected"
        return dict(session.selected_store)

    async def _preflight(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        session = await self._current(session_id)
        self._assert_payload_matches_session(session, payload)
        if (
            session.selected_music is None
            or session.location is None
            or not session.selected_declaration
        ):
            raise DouyinCommerceSessionError("音乐、地点和作品内容声明均完成平台回读后才能开始预检")
        target_schedule = douyin_publish_executor._scheduled_time(payload)
        if target_schedule is None and session.schedule_time:
            # 同一临时编辑页此前已被写入过定时。新版控件没有可靠的“清空定时”
            # 回读路径时，不能把客户端切到“立即发表”就假定平台也已切回；要求
            # 用户重新上传，避免意外沿用旧定时。
            raise DouyinCommerceSessionError(
                "当前编辑页此前已写入定时；如需改为立即发表，请重新上传视频后再预检"
            )
        try:
            if target_schedule is not None:
                await session.uploader.set_schedule_time_douyin(session.page, target_schedule)
            form = await session.uploader.verify_prepublish_form(
                session.page,
                require_covers=False,
            )
        except Exception as exc:
            raise DouyinCommerceSessionError(
                f"抖音带货预检未能完成字段回读：{_normalized(str(exc))[:260]}"
            ) from exc
        if target_schedule is not None:
            schedule_value = _normalized(getattr(session.uploader, "schedule_verification", ""))
            if not session.uploader._schedule_time_matches(
                schedule_value,
                target_schedule.strftime("%Y-%m-%d %H:%M"),
            ):
                raise DouyinCommerceSessionError("抖音定时时间未能回读为指定的北京时间")
        if _normalized(session.uploader.location_verification) != session.location["name"]:
            raise DouyinCommerceSessionError("抖音带货位置最终回读不一致")
        session.schedule_time = (
            target_schedule.strftime("%Y-%m-%d %H:%M")
            if target_schedule is not None
            else ""
        )
        session.preflight_fingerprint = self._preflight_fingerprint(payload)
        session.stage = "preflighted"
        return {
            "ok": True,
            "dryRun": True,
            "message": (
                "抖音带货预检已在同一编辑会话回读账号、标题、文案、用户所选收藏音乐、"
                "发布定位、作品内容声明与"
                f"{'定时' if target_schedule is not None else '立即发表'}状态；尚未保存草稿或提交发布。"
            ),
            "account": session.account_name,
            "form": dict(form or {}),
            "music": dict(session.selected_music),
            "location": dict(session.location),
            "contentDeclaration": session.selected_declaration,
            "scheduled": target_schedule is not None,
            "scheduledAt": session.schedule_time or None,
        }

    async def _submit(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        session = await self._current(session_id)
        self._assert_payload_matches_session(session, payload)
        expected_fingerprint = self._preflight_fingerprint(payload)
        if session.stage != "preflighted" or session.preflight_fingerprint != expected_fingerprint:
            raise DouyinCommerceSessionError("账号、内容、音乐、地点、声明或定时已变化，请先重新预检")
        target_schedule = douyin_publish_executor._scheduled_time(payload)
        if target_schedule is None and session.schedule_time:
            raise DouyinCommerceSessionError(
                "当前编辑页仍保留已回读的定时；不能安全改为立即发表，请重新上传并预检"
            )
        try:
            # 定时提交前再次写入并回读；立即发表则保持上传后默认的立即状态，
            # 不猜测点击任何“取消定时”控件。
            if target_schedule is not None:
                await session.uploader.set_schedule_time_douyin(session.page, target_schedule)
                schedule_value = _normalized(getattr(session.uploader, "schedule_verification", ""))
                if not session.uploader._schedule_time_matches(
                    schedule_value,
                    target_schedule.strftime("%Y-%m-%d %H:%M"),
                ):
                    raise DouyinCommerceSessionError("抖音最终提交前定时时间回读不一致")
            from utils.base_social_media import reveal_page_window

            # 最终不可逆步骤前明确前置受控页面；二维码、验证码和新提示都由
            # 用户在这个窗口处理，程序不会尝试绕过。
            await reveal_page_window(session.page)
            publish_button = await session.uploader.wait_publish_button_ready(session.page)
            await publish_button.click(timeout=10_000)
            receipt = await session.uploader._wait_formal_publish_result(session.page)
            scheduled = None
            if target_schedule is not None:
                scheduled = await douyin_publish_executor._scheduled_submission_readback(
                    session.page,
                    title=str(payload["title"]),
                    target=target_schedule,
                )
        except DouyinCommerceSessionError:
            raise
        except Exception as exc:
            raise DouyinCommerceSessionError(
                f"抖音带货最终提交未能获得平台回执：{_normalized(str(exc))[:260]}"
            ) from exc
        finally:
            # 成功或失败后都不把临时编辑会话留成可误复用状态。若平台要求扫码
            # /验证码，``_wait_formal_publish_result`` 会在前台等待用户处理后才
            # 返回或报错，因此不会在用户操作前走到这里。
            await self._close(session_id)
        return {
            "ok": True,
            "message": (
                (
                    "抖音带货定时提交已由作品管理页回读："
                    f"{scheduled['scheduledAt']}；"
                    if scheduled is not None
                    else "抖音带货立即发表已获得平台回执："
                )
                + f"账号={session.account_name}；地点={session.location['name']}；"
                f"声明={session.selected_declaration}；音乐={session.selected_music['title']}"
            ),
            "account": session.account_name,
            "music": dict(session.selected_music),
            "location": dict(session.location),
            "contentDeclaration": session.selected_declaration,
            "scheduledAt": scheduled["scheduledAt"] if scheduled is not None else None,
            "scheduledReadback": dict(scheduled) if scheduled is not None else None,
            "platformReceipt": dict(receipt or {}),
            "scheduled": scheduled is not None,
        }

    def _assert_payload_matches_session(
        self,
        session: _CommerceEditorSession,
        payload: Mapping[str, Any],
    ) -> None:
        if self._upload_fingerprint(payload) != self._upload_fingerprint(session.upload_payload):
            raise DouyinCommerceSessionError("账号、视频、标题、文案或话题已变更，请重新上传")
        selected_music = payload.get("selectedMusic") if isinstance(payload.get("selectedMusic"), Mapping) else {}
        if session.selected_music is None or not _same_music(selected_music, session.selected_music):
            raise DouyinCommerceSessionError("任务中的音乐与当前编辑页用户所选音乐不一致")
        location = payload.get("locationPoi") if isinstance(payload.get("locationPoi"), Mapping) else {}
        if session.location is None or not _same_location(location, session.location):
            raise DouyinCommerceSessionError("任务中的地点与当前编辑页回读地点不一致")
        try:
            payload_scope = douyin_commerce_service.normalize_commerce_location_scope(
                payload.get("locationScope")
            )
        except douyin_commerce_service.DouyinCommerceError as exc:
            raise DouyinCommerceSessionError(str(exc)) from exc
        if payload_scope != session.location_scope:
            raise DouyinCommerceSessionError("任务中的地点搜索范围与当前编辑页选择不一致")
        try:
            declaration = douyin_commerce_service.normalize_content_declaration(
                payload.get("contentDeclaration")
            )
        except douyin_commerce_service.DouyinCommerceError as exc:
            raise DouyinCommerceSessionError(str(exc)) from exc
        if not session.selected_declaration or declaration != session.selected_declaration:
            raise DouyinCommerceSessionError("任务中的作品内容声明与当前编辑页回读不一致")

    async def _status(self) -> dict[str, str]:
        session = self._session
        if session is None:
            return {"active": "false", "stage": ""}
        return {
            "active": "true",
            "sessionId": session.session_id,
            "stage": session.stage,
        }

    async def _close(self, session_id: str | None) -> None:
        session = self._session
        if session is None:
            return
        if session_id and _normalized(session_id) != session.session_id:
            raise DouyinCommerceSessionError("无法关闭已替换的抖音带货会话")
        self._session = None
        await self._close_resources(
            context=session.context,
            browser=session.browser,
            playwright=session.playwright,
        )

    @staticmethod
    async def _close_resources(*, context, browser, playwright) -> None:
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
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                pass


commerce_session_manager = DouyinCommerceSessionManager()
