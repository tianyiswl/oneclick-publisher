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
import logging
from pathlib import Path
import threading
from typing import Any, Callable, Mapping
from uuid import uuid4

from . import (
    douyin_commerce_service,
    douyin_music_service,
    douyin_publish_executor,
)
from .oneclick_preflight import _account_for_payload, _storage_state
from .douyin_verification import verification_broker


_LOGGER = logging.getLogger(__name__)


class DouyinCommerceSessionError(RuntimeError):
    """分步编辑会话无法继续时抛出。"""


@dataclass(frozen=True)
class CommerceProgressEvent:
    """仅供桌面端显示的非敏感后台处理进度。"""

    phase: str
    label: str
    state: str = "running"

    def to_public_dict(self) -> dict[str, str]:
        """返回可以跨线程传递的最小状态，不暴露平台诊断细节。"""

        return {
            "phase": self.phase,
            "label": self.label,
            "state": self.state,
        }


def _emit_progress(
    callback: Callable[[dict[str, str]], None] | None,
    phase: str,
    label: str,
    state: str = "running",
) -> None:
    """投递状态；界面刷新异常不得中断受控的上传会话。"""

    if callback is None:
        return
    try:
        callback(CommerceProgressEvent(phase, label, state).to_public_dict())
    except Exception:
        _LOGGER.debug("抖音带货进度回调失败", exc_info=True)


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
    def _session_identity_fingerprint(payload: Mapping[str, Any]) -> str:
        return "|".join(
            (
                _normalized((payload.get("accountList") or [""])[0]),
                _normalized((payload.get("fileList") or [""])[0]),
            )
        )

    @staticmethod
    def _content_fingerprint(payload: Mapping[str, Any]) -> str:
        return "|".join(
            (
                _normalized(payload.get("title")),
                _normalized(payload.get("description")),
                ",".join(_normalized(item) for item in payload.get("tags") or []),
            )
        )

    @classmethod
    def _upload_fingerprint(cls, payload: Mapping[str, Any]) -> str:
        return "|".join(
            (
                cls._session_identity_fingerprint(payload),
                cls._content_fingerprint(payload),
            )
        )

    @staticmethod
    def _background_upload_mode(payload: Mapping[str, Any]) -> bool:
        """上传默认后台运行；仅显式 false 保留受控兼容入口。"""

        if "backgroundMode" not in payload:
            return True
        return bool(payload.get("backgroundMode"))

    @classmethod
    def _commerce_browser_launch_options(cls, payload: Mapping[str, Any]) -> dict[str, bool]:
        """返回带货编辑会话的浏览器可见性策略。

        带货编辑与最终提交一律使用真正无头浏览器，不能依赖最小化或离屏窗口。
        登录失效会作为受控结果回到账号管理处理，不为验证或诊断打开前台浏览器。
        """

        del payload
        return {
            "headless": True,
            "hide_until_ready": False,
        }

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

    def start_upload(
        self,
        payload: Mapping[str, Any],
        *,
        on_progress: Callable[[dict[str, str]], None] | None = None,
    ) -> dict[str, str]:
        """后台上传一次视频并停在同一编辑会话，尚不选音乐、地点或声明。"""

        checked = douyin_commerce_service.validate_douyin_commerce_upload_payload(
            payload
        )
        return self._call(self._start_upload(dict(checked), on_progress=on_progress))

    def synchronize_content(
        self,
        session_id: str,
        payload: Mapping[str, Any],
        *,
        on_progress: Callable[[dict[str, str]], None] | None = None,
    ) -> dict[str, Any]:
        """在当前编辑会话中同步标题、文案和标签，不重新上传视频。"""

        checked = douyin_commerce_service.validate_douyin_commerce_upload_payload(
            payload
        )
        return self._call(
            self._synchronize_content(
                session_id,
                dict(checked),
                on_progress=on_progress,
            )
        )

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

    def sync_schedule(self, session_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """进入检查页前，读取并按需同步当前编辑页的定时设置。"""

        checked = douyin_commerce_service.validate_douyin_commerce_payload(payload)
        if (
            str(checked.get("runtimeMode") or "") != "preflight"
            or checked.get("debugDryRun") is not True
        ):
            raise DouyinCommerceSessionError("抖音带货定时同步必须保持预检模式")
        return self._call(self._sync_schedule(session_id, dict(checked)))

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

    def submit(
        self,
        session_id: str,
        payload: Mapping[str, Any],
        task_id: int | None = None,
    ) -> dict[str, Any]:
        """在已预检的同一会话中执行明确确认后的最终定时提交。"""

        checked = douyin_commerce_service.validate_douyin_commerce_payload(payload)
        if str(checked.get("runtimeMode") or "") != "publish" or checked.get("debugDryRun") is not False:
            raise DouyinCommerceSessionError("抖音带货最终提交必须明确 runtimeMode=publish")
        return self._call(self._submit(session_id, dict(checked), task_id=task_id))

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

    async def _synchronize_content(
        self,
        session_id: str,
        payload: Mapping[str, Any],
        *,
        on_progress: Callable[[dict[str, str]], None] | None = None,
    ) -> dict[str, Any]:
        """向同一编辑页写入内容字段，并用页面回读更新会话快照。"""

        session = await self._current(session_id)
        self._ensure_editor_not_blocked_by_music_picker(session)
        if self._session_identity_fingerprint(payload) != self._session_identity_fingerprint(
            session.upload_payload
        ):
            raise DouyinCommerceSessionError("账号或视频已变化，请重新上传")
        if session.uploader is None:
            raise DouyinCommerceSessionError("当前抖音编辑会话不可写入，请重新上传视频")

        _emit_progress(on_progress, "syncing_content", "正在同步内容")
        try:
            result = await session.uploader.sync_uploaded_editor_content(
                session.page,
                title=str(payload.get("title") or ""),
                description=str(payload.get("description") or ""),
                tags=list(payload.get("tags") or []),
            )
        except DouyinCommerceSessionError:
            raise
        except Exception as exc:
            _emit_progress(on_progress, "sync_failed", "内容同步未完成", "failed")
            raise DouyinCommerceSessionError(
                f"抖音带货内容同步未完成：{_normalized(str(exc))[:260]}"
            ) from exc

        confirmed = dict(result or {})
        session.upload_payload = {
            **dict(payload),
            "title": _normalized(confirmed.get("title") or payload.get("title")),
            "description": str(confirmed.get("description") or payload.get("description") or ""),
            "tags": [
                _normalized(tag).lstrip("#")
                for tag in confirmed.get("tags") or payload.get("tags") or []
                if _normalized(tag).lstrip("#")
            ],
        }
        session.preflight_fingerprint = ""
        self._refresh_editor_stage(session)
        _emit_progress(on_progress, "content_synced", "内容已同步", "succeeded")
        return {
            "status": "synced",
            "sessionId": session.session_id,
            "account": session.account_name,
            "title": session.upload_payload["title"],
            "description": session.upload_payload["description"],
            "tags": list(session.upload_payload["tags"]),
            "form": dict(confirmed.get("form") or {}),
        }

    @staticmethod
    def _refresh_editor_stage(session: _CommerceEditorSession) -> None:
        """用已回读字段生成展示阶段，不再把平台设置人为串成单一路径。"""

        if session.preflight_fingerprint:
            session.stage = "preflighted"
        elif session.music_picker_page is not None or session.music_dialog is not None:
            session.stage = "music_candidates_loaded"
        elif session.selected_declaration:
            session.stage = "declaration_selected"
        elif session.location is not None:
            session.stage = "location_selected"
        elif session.selected_music is not None:
            session.stage = "music_selected"
        else:
            session.stage = "uploaded"

    @staticmethod
    def _ensure_editor_not_blocked_by_music_picker(session: _CommerceEditorSession) -> None:
        """音乐抽屉在真实平台上打开时，只阻止并发操作，不制造长期顺序依赖。"""

        if session.music_picker_page is not None or session.music_dialog is not None:
            raise DouyinCommerceSessionError(
                "当前收藏音乐选择器仍打开，请先完成或取消音乐选择后再设置其他项"
            )

    @staticmethod
    def _login_required_result() -> dict[str, str]:
        """将明确登录页统一投影为客户端可处理的安全结果。"""

        return {
            "status": "needs_login",
            "message": "登录已失效，请到账号管理重新登录",
        }

    @staticmethod
    async def _is_login_required_page(page: Any) -> bool:
        """仅基于已加载页面的可见登录语义判断是否需要重新登录。"""

        try:
            url = _normalized(getattr(page, "url", "")).lower()
        except Exception:
            url = ""
        if any(marker in url for marker in ("/login", "passport", "scan_login")):
            return True

        try:
            text = _normalized(
                await page.locator("body").inner_text(timeout=1_200)
            )[:1_200]
        except Exception:
            return False
        return any(
            marker in text
            for marker in (
                "扫码登录",
                "请登录后继续",
                "手机号登录",
                "验证码登录",
                "请输入验证码",
                "请使用抖音扫码",
            )
        )

    async def _start_upload(
        self,
        payload: dict[str, Any],
        *,
        on_progress: Callable[[dict[str, str]], None] | None = None,
    ) -> dict[str, str]:
        await self._close(None)
        from playwright.async_api import async_playwright
        from uploader.douyin_uploader.main import DouYinVideo
        from utils.base_social_media import (
            launch_chromium_with_codecs,
            new_publish_context,
            set_init_script,
        )
        from utils.publish_observer import publish_context

        _emit_progress(on_progress, "checking_session", "正在核对账号会话")
        account = _account_for_payload(payload)
        # 账号主体编号不等于抖音昵称；与普通抖音发布保持同一身份回读规则。
        expected_account = douyin_publish_executor._expected_account_name(account)
        if not expected_account:
            raise DouyinCommerceSessionError("抖音账号缺少可回读的账号名，请先在账号管理中重新绑定")
        storage_state = _storage_state(account)
        _emit_progress(on_progress, "opening_editor", "正在打开后台编辑会话")
        playwright = await async_playwright().start()
        browser = context = page = None
        success = False
        try:
            browser_options = self._commerce_browser_launch_options(payload)
            background_mode = True
            with publish_context(
                mode="douyin_commerce_upload",
                background_mode=background_mode,
                platform_type=3,
                platform_name="抖音",
            ):
                browser = await launch_chromium_with_codecs(
                    playwright,
                    force_bundled=True,
                    **browser_options,
                )
                context = await new_publish_context(browser, storage_state=str(storage_state))
                context = await set_init_script(context)
                page = await context.new_page()
                try:
                    actual_account = await douyin_publish_executor._readback_douyin_session_identity(
                        page,
                        expected_account,
                        reveal=False,
                    )
                except Exception:
                    if await self._is_login_required_page(page):
                        _emit_progress(
                            on_progress,
                            "needs_login",
                            "登录已失效，请到账号管理重新登录",
                            "needs_login",
                        )
                        return self._login_required_result()
                    raise
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
                uploader.progress_callback = on_progress
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
            _emit_progress(on_progress, "ready", "已进入平台设置", "succeeded")
            return {
                "status": "ready",
                "sessionId": session.session_id,
                "account": actual_account,
                "video": Path(str(payload["fileList"][0])).name,
                "message": "视频已上传并回读标题、文案；尚未选择音乐、地点、声明或定时。",
            }
        except Exception as exc:
            if page is not None and await self._is_login_required_page(page):
                _emit_progress(
                    on_progress,
                    "needs_login",
                    "登录已失效，请到账号管理重新登录",
                    "needs_login",
                )
                return self._login_required_result()
            _emit_progress(on_progress, "failed", "上传未完成", "failed")
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
        if session.music_candidates and session.music_dialog is not None:
            return [_public_music(item) for item in session.music_candidates]
        self._ensure_editor_not_blocked_by_music_picker(session)
        try:
            picker_page, dialog, candidates = await douyin_music_service.open_favorite_music_choices(
                session.page
            )
        except douyin_music_service.DouyinMusicError as exc:
            raise DouyinCommerceSessionError(f"读取抖音收藏音乐失败：{exc}") from exc
        session.music_picker_page = picker_page
        session.music_dialog = dialog
        session.music_candidates = [dict(item) for item in candidates]
        self._refresh_editor_stage(session)
        return [_public_music(item) for item in session.music_candidates]

    async def _select_favorite_music(
        self,
        session_id: str,
        music_id: str,
    ) -> dict[str, str]:
        session = await self._current(session_id)
        if not session.music_candidates or session.music_dialog is None:
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
            # 收藏列表的 marker 依附于当前音乐弹窗。写入失败后不复用它，下一次
            # 选择必须重新打开并读取，避免将已重绘的条目误认成同一首音乐。
            session.music_picker_page = None
            session.music_dialog = None
            session.music_candidates = []
            self._refresh_editor_stage(session)
            raise DouyinCommerceSessionError(f"抖音收藏音乐未能选择并回读：{exc}") from exc
        session.selected_music = _public_music(selected)
        session.music_picker_page = None
        session.music_dialog = None
        # 瞬态 marker 仅在刚才的点击中存在，选择完成后立即清除。
        session.music_candidates = []
        session.preflight_fingerprint = ""
        session.schedule_time = ""
        self._refresh_editor_stage(session)
        return dict(session.selected_music)

    async def _search_locations(
        self,
        session_id: str,
        keyword: object,
        scope: object,
    ) -> list[dict[str, Any]]:
        session = await self._current(session_id)
        self._ensure_editor_not_blocked_by_music_picker(session)
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
        session.stores = []
        session.selected_store = None
        session.preflight_fingerprint = ""
        session.schedule_time = ""
        self._refresh_editor_stage(session)
        return [dict(item) for item in session.commerce_location_candidates]

    async def _apply_location(
        self,
        session_id: str,
        candidate: dict[str, Any],
    ) -> dict[str, Any]:
        session = await self._current(session_id)
        self._ensure_editor_not_blocked_by_music_picker(session)
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
        session.stores = []
        session.selected_store = None
        if session.uploader is not None:
            session.uploader.location_verification = session.location["name"]
        session.preflight_fingerprint = ""
        session.schedule_time = ""
        self._refresh_editor_stage(session)
        return {"location": dict(session.location)}

    async def _select_content_declaration(
        self,
        session_id: str,
        declaration: str,
    ) -> str:
        session = await self._current(session_id)
        self._ensure_editor_not_blocked_by_music_picker(session)
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
        self._refresh_editor_stage(session)
        return actual_value

    async def _sync_schedule(
        self,
        session_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """以当前编辑页为准核对定时；仅在不一致时写入新的未来时间。"""

        session = await self._current(session_id)
        self._ensure_editor_not_blocked_by_music_picker(session)
        self._assert_payload_matches_session(session, payload)
        return await self._sync_schedule_for_session(session, payload)

    async def _sync_schedule_for_session(
        self,
        session: _CommerceEditorSession,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """读取平台定时状态；客户端与页面不同才写入，并做第二次回读。"""

        if session.uploader is None:
            raise DouyinCommerceSessionError("当前抖音编辑会话不可读取定时，请重新上传视频")
        target_schedule = douyin_publish_executor._scheduled_time(payload)
        target_text = (
            target_schedule.strftime("%Y-%m-%d %H:%M")
            if target_schedule is not None
            else ""
        )
        try:
            actual_before = _normalized(
                await session.uploader.read_schedule_time_douyin(session.page)
            )
        except Exception as exc:
            raise DouyinCommerceSessionError(
                f"抖音定时状态未能回读：{_normalized(str(exc))[:260]}"
            ) from exc

        if target_schedule is None:
            if actual_before:
                try:
                    await session.uploader.clear_schedule_time_douyin(session.page)
                    actual_after = _normalized(
                        await session.uploader.read_schedule_time_douyin(session.page)
                    )
                except Exception as exc:
                    raise DouyinCommerceSessionError(
                        f"抖音取消定时未能同步：{_normalized(str(exc))[:260]}"
                    ) from exc
                if actual_after:
                    raise DouyinCommerceSessionError("抖音切回立即发表后仍回读到定时")
                session.schedule_time = ""
                session.preflight_fingerprint = ""
                self._refresh_editor_stage(session)
                return {"status": "updated", "scheduledAt": None}
            session.schedule_time = ""
            return {"status": "unchanged", "scheduledAt": None}

        if session.uploader._schedule_time_matches(actual_before, target_text):
            session.schedule_time = target_text
            return {"status": "unchanged", "scheduledAt": target_text}

        try:
            await session.uploader.set_schedule_time_douyin(session.page, target_schedule)
            actual_after = _normalized(
                await session.uploader.read_schedule_time_douyin(session.page)
            )
        except Exception as exc:
            raise DouyinCommerceSessionError(
                f"抖音定时时间未能同步：{_normalized(str(exc))[:260]}"
            ) from exc
        if not session.uploader._schedule_time_matches(actual_after, target_text):
            raise DouyinCommerceSessionError("抖音定时时间回读与指定北京时间不一致")

        session.schedule_time = target_text
        session.preflight_fingerprint = ""
        self._refresh_editor_stage(session)
        return {"status": "updated", "scheduledAt": target_text}

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
        try:
            schedule_result = await self._sync_schedule_for_session(session, payload)
            form = await session.uploader.verify_prepublish_form(
                session.page,
                require_covers=False,
            )
        except Exception as exc:
            raise DouyinCommerceSessionError(
                f"抖音带货预检未能完成字段回读：{_normalized(str(exc))[:260]}"
            ) from exc
        if _normalized(session.uploader.location_verification) != session.location["name"]:
            raise DouyinCommerceSessionError("抖音带货位置最终回读不一致")
        session.schedule_time = _normalized(schedule_result.get("scheduledAt"))
        session.preflight_fingerprint = self._preflight_fingerprint(payload)
        session.stage = "preflighted"
        return {
            "ok": True,
            "dryRun": True,
            "message": (
                "抖音带货预检已在同一编辑会话回读账号、标题、文案、用户所选收藏音乐、"
                "发布定位、作品内容声明与"
                f"{'定时' if session.schedule_time else '立即发表'}状态；尚未保存草稿或提交发布。"
            ),
            "account": session.account_name,
            "form": dict(form or {}),
            "music": dict(session.selected_music),
            "location": dict(session.location),
            "contentDeclaration": session.selected_declaration,
            "scheduled": bool(session.schedule_time),
            "scheduledAt": session.schedule_time or None,
        }

    async def _handle_publish_verification(
        self,
        session: _CommerceEditorSession,
        challenge,
        task_id: int | None,
    ) -> None:
        """在原 Playwright 事件循环内完成单次、内存态验证挑战。"""

        try:
            normalized_task_id = int(task_id) if task_id is not None else 0
        except (TypeError, ValueError):
            normalized_task_id = 0
        if normalized_task_id <= 0:
            raise DouyinCommerceSessionError("抖音验证缺少任务号，发布已安全停止")

        kind = getattr(challenge, "kind", "")
        if kind == "sms":
            request_id = verification_broker.create_sms(
                task_id=normalized_task_id,
                message="请在一键发客户端输入短信验证码",
            )
        elif kind == "qr":
            request_id = verification_broker.create_qr(
                task_id=normalized_task_id,
                qr_image=bytes(getattr(challenge, "qr_image", b"")),
                expires_in_seconds=600,
            )
        else:
            raise DouyinCommerceSessionError("抖音返回了无法处理的验证类型，发布已安全停止")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + 600
        try:
            while True:
                snapshot = verification_broker.snapshot(request_id)
                if snapshot.get("state") != "waiting":
                    raise DouyinCommerceSessionError("抖音验证已取消、失败或超时，发布已安全停止")
                if loop.time() >= deadline:
                    verification_broker.fail(request_id)
                    raise DouyinCommerceSessionError("等待抖音验证超时，发布已安全停止")

                if kind == "sms":
                    code = verification_broker.claim_code(request_id)
                    if code:
                        try:
                            verification_broker.ensure_processing(request_id)
                            await session.uploader.apply_sms_verification_code(
                                session.page,
                                challenge,
                                code,
                                before_submit=lambda: verification_broker.ensure_processing(
                                    request_id
                                ),
                            )
                        except Exception as exc:
                            verification_broker.fail(request_id)
                            raise DouyinCommerceSessionError(
                                "抖音短信验证未通过或页面状态无法确认，发布已安全停止"
                            ) from exc
                        verification_broker.succeed(request_id)
                        return
                else:
                    try:
                        current = await session.uploader.detect_publish_verification(
                            session.page
                        )
                    except Exception as exc:
                        verification_broker.fail(request_id)
                        raise DouyinCommerceSessionError(
                            "抖音扫码验证页面状态无法确认，发布已安全停止"
                        ) from exc
                    if current is None:
                        if "/creator-micro/content/manage" in str(session.page.url or ""):
                            verification_broker.succeed(request_id)
                            return
                        await session.page.wait_for_timeout(250)
                        continue
                    if getattr(current, "kind", "") != "qr":
                        verification_broker.fail(request_id)
                        raise DouyinCommerceSessionError(
                            "抖音扫码验证页面状态已变化，发布已安全停止"
                        )
                await session.page.wait_for_timeout(250)
        finally:
            verification_broker.clear(request_id)

    async def _submit(
        self,
        session_id: str,
        payload: dict[str, Any],
        task_id: int | None = None,
    ) -> dict[str, Any]:
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
        background_mode = True
        try:
            from utils.publish_observer import publish_context

            with publish_context(
                mode="douyin_commerce_submit",
                background_mode=background_mode,
                platform_type=3,
                platform_name="抖音",
            ):
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
                publish_button = await session.uploader.wait_publish_button_ready(session.page)
                await publish_button.click(timeout=10_000)
                receipt = await session.uploader._wait_formal_publish_result(
                    session.page,
                    on_verification=lambda challenge: self._handle_publish_verification(
                        session,
                        challenge,
                        task_id,
                    ),
                )
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
            # 成功或失败后都不把临时编辑会话留成可误复用状态。后台模式遇到
            # 二次验证会立即安全停止，不会假定用户能在隐藏浏览器中处理。
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
        if self._session_identity_fingerprint(payload) != self._session_identity_fingerprint(
            session.upload_payload
        ):
            raise DouyinCommerceSessionError("账号或视频已变化，请重新上传")
        if self._content_fingerprint(payload) != self._content_fingerprint(
            session.upload_payload
        ):
            raise DouyinCommerceSessionError("标题、文案或标签已变化，请先同步内容")
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
