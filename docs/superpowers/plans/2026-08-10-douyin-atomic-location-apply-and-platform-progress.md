# 抖音地点原子应用与平台进度条实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 正式批量发布在同一地点面板内完成搜索、唯一匹配、点击和回读，并在平台设置底部为所有耗时采集动作显示可见进度。

**Architecture:** DOM 服务层提供一个不跨面板的地点原子动作，正式会话管理器将它暴露为单一同步入口，批量执行器只调用该入口。Qt 页面使用一个带代际/采集器/action token 所有权的进度状态，旧回调不能收口新动作。

**Tech Stack:** Python 3.12、PyQt6、Playwright async API、SQLite、`unittest`、`unittest.mock`

## Global Constraints

- 不恢复、改写或自动续发任务 `T08100006-D236`。
- 不使用店名近似、候选序号或“第一条”降级地点匹配。
- 地点未唯一匹配、点击失败或回读不一致时，排期、预检和最终提交都不得执行。
- 每条视频仍创建全新正式 Session，且在 `finally` 中关闭。
- 进度条使用 Qt 不确定模式，只显示当前动作与已等待秒数，不伪造百分比。
- 进度文案和任务错误只使用固定阶段/错误码，不显示 Cookie、账号路径、DOM、HTML 或原始异常。
- 离线测试、离屏 UI 和默认验证命令不得读真实账号、打开真实浏览器、上传、保存草稿或发布。

---

## File Structure

- `app_core/douyin_commerce_service.py`：持有地点面板 DOM 原子动作；候选节点不离开该层。
- `app_core/douyin_commerce_session.py`：校验公开入参，在固定 asyncio 线程中调用原子动作，更新当前正式会话回读。
- `app_core/douyin_commerce_batch_executor.py`：传入有界关键词，将原有搜索+应用两步替换为单一入口，保持提交门禁。
- `ui/douyin_commerce_page.py`：持有平台采集进度所有者、定时器、底栏控件和生命周期收口。
- `ui/common.py`：只增加平台采集进度控件的定向样式。
- `test_douyin_commerce_service.py`：DOM 原子性、会话合约和 Qt 进度生命周期。
- `test_douyin_commerce_batch_executor.py`：逐条执行顺序、原子地点入口和最终提交断言。

---

### Task 1: 建立同面板地点原子 DOM 动作

**Files:**
- Modify: `app_core/douyin_commerce_service.py:60-65, 2301-2345`
- Test: `test_douyin_commerce_service.py:176-1843`

**Interfaces:**
- Consumes: `match_location_preset(preset: object, candidates: object) -> dict[str, str]`
- Produces: `apply_saved_commerce_location_to_page(page, preset: Mapping[str, Any], scope: object, keywords: list[str]) -> dict[str, Any]`
- Produces: `_apply_open_commerce_location_to_page(page, listbox, candidate: Mapping[str, Any]) -> dict[str, Any]`
- Error codes: `publish_location_candidate_missing`、`publish_location_candidate_ambiguous`、`publish_location_click_failed`、`publish_location_readback_mismatch`、`publish_location_cleanup_incomplete`

- [ ] **Step 1: 写“关闭后重开快照丢失”的失败测试**

在 `DouyinCommercePayloadTests` 中新增两项异步调用测试。第一项证明新入口在搜索得到候选后，使用同一 `listbox` 点击，不再调用 `_open_store_selector`；第二项证明第一关键词无匹配后收口，第二关键词可成功。

```python
def test_saved_location_is_clicked_in_the_same_open_panel(self) -> None:
    page = object()
    listbox = object()
    row = {
        "poiId": "visible-poi:target",
        "name": "夜南香北京烤鸭",
        "address": "陕西省安康市汉滨区江北办富民街2号",
    }
    events: list[str] = []

    async def search(*_args, **_kwargs):
        events.append("search")
        return [dict(row)]

    async def current_listbox(_page):
        events.append("listbox")
        return listbox

    async def apply_open(_page, actual_listbox, candidate):
        self.assertIs(actual_listbox, listbox)
        self.assertEqual(candidate, row)
        events.append("click")
        return {"location": dict(row)}

    with patch.object(
        douyin_commerce_service,
        "search_commerce_location_store_candidates",
        side_effect=search,
    ), patch.object(
        douyin_commerce_service,
        "_visible_store_listbox",
        side_effect=current_listbox,
    ), patch.object(
        douyin_commerce_service,
        "_apply_open_commerce_location_to_page",
        side_effect=apply_open,
    ), patch.object(
        douyin_commerce_service,
        "close_commerce_store_selector",
        new_callable=AsyncMock,
    ), patch.object(
        douyin_commerce_service,
        "_open_store_selector",
        new_callable=AsyncMock,
    ) as reopen:
        result = asyncio.run(
            douyin_commerce_service.apply_saved_commerce_location_to_page(
                page, row, "domestic", [row["address"]]
            )
        )

    self.assertEqual(events, ["search", "listbox", "click"])
    self.assertEqual(result["matchedKeyword"], row["address"])
    reopen.assert_not_awaited()
```

- [ ] **Step 2: 运行单项测试并确认 RED**

Run:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest \
  test_douyin_commerce_service.DouyinCommercePayloadTests.test_saved_location_is_clicked_in_the_same_open_panel
```

Expected: `ERROR`/`FAIL`，因为 `apply_saved_commerce_location_to_page` 尚不存在。

- [ ] **Step 3: 拆出已打开面板点击帮助器**

将 `apply_commerce_location_to_page()` 中从“读取当前 listbox 候选”到“点击并回读”的逻辑移入下列帮助器，旧入口仅负责打开面板后调用它：

```python
async def _apply_open_commerce_location_to_page(
    page,
    listbox,
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    normalized = normalize_commerce_location_candidate(candidate)
    if not normalized:
        raise DouyinCommerceError("publish_location_candidate_missing")
    location = {
        key: _normalized(normalized.get(key))
        for key in ("poiId", "name", "address", "distance")
    }
    visible_locations = normalize_commerce_location_candidates(
        await _store_option_descriptors(listbox)
    )
    matched_locations = [
        row
        for row in visible_locations
        if all(
            _normalized(row.get(field)) == location[field]
            for field in ("poiId", "name", "address")
        )
    ]
    if len(matched_locations) != 1:
        code = (
            "publish_location_candidate_ambiguous"
            if len(matched_locations) > 1
            else "publish_location_candidate_missing"
        )
        raise DouyinCommerceError(code)
    targets = await _location_option_targets(listbox, location)
    if len(targets) != 1:
        raise DouyinCommerceError("publish_location_click_failed")
    try:
        await targets[0].scroll_into_view_if_needed(timeout=5_000)
        await targets[0].click(timeout=8_000)
    except Exception as exc:
        raise DouyinCommerceError("publish_location_click_failed") from exc
    await page.wait_for_timeout(450)
    _, _, mode_value, selected_name = await _anchor_controls(page)
    if mode_value != _COMMERCE_MODE_TEXT or selected_name != location["name"]:
        raise DouyinCommerceError("publish_location_readback_mismatch")
    return {"location": location}
```

- [ ] **Step 4: 实现有界关键词的原子搜索与应用**

`apply_saved_commerce_location_to_page()` 只接受 1—3 个非空关键词。每个关键词先收口旧面板，调用现有搜索服务，用 `match_location_preset()` 唯一匹配，然后立即从当前可见 listbox 点击。匹配失败才进入下一关键词；点击失败不得换关键词掩盖。

```python
async def _close_commerce_store_selector_strict(page) -> None:
    try:
        await close_commerce_store_selector(page)
    except Exception:
        raise DouyinCommerceError("publish_location_cleanup_incomplete") from None


async def apply_saved_commerce_location_to_page(
    page,
    preset: Mapping[str, Any],
    scope: object,
    keywords: list[str],
) -> dict[str, Any]:
    selected_scope = normalize_commerce_location_scope(scope)
    bounded_keywords = list(dict.fromkeys(
        _normalized(value) for value in keywords if _normalized(value)
    ))[:3]
    if not bounded_keywords:
        raise DouyinCommerceError("publish_location_candidate_missing")
    for keyword in bounded_keywords:
        await _close_commerce_store_selector_strict(page)
        panel_may_be_open = False
        try:
            panel_may_be_open = True
            try:
                candidates = await search_commerce_location_store_candidates(
                    page, keyword, scope=selected_scope
                )
            except DouyinCommerceError:
                continue
            try:
                matched = match_location_preset(preset, candidates)
            except DouyinLocationPresetError as exc:
                if "存在多个" in str(exc):
                    raise DouyinCommerceError(
                        "publish_location_candidate_ambiguous"
                    ) from None
                matched = None
            if matched is None:
                continue
            listbox = await _visible_store_listbox(page)
            if listbox is None:
                raise DouyinCommerceError("publish_location_click_failed")
            result = await _apply_open_commerce_location_to_page(
                page, listbox, matched
            )
            return {**result, "matchedKeyword": keyword}
        except DouyinCommerceError:
            raise
        except Exception:
            raise DouyinCommerceError("publish_location_click_failed") from None
        finally:
            if panel_may_be_open:
                await _close_commerce_store_selector_strict(page)
    raise DouyinCommerceError("publish_location_candidate_missing")
```

在文件顶部显式导入 `DouyinLocationPresetError` 和 `match_location_preset`。若正常错误路径再次收口面板时失败，`publish_location_cleanup_incomplete` 覆盖原错误，因为此时不能证明浏览器已恢复干净基线。所有新增对外异常都使用 `from None`，不保留底层 cause 原文。

- [ ] **Step 5: 运行 DOM 定向回归**

Run:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest \
  test_douyin_commerce_service.DouyinCommercePayloadTests \
  test_douyin_commerce_service.DouyinCommerceLocationDomTests
```

Expected: `OK`，且新增原子动作用例通过。

- [ ] **Step 6: 提交 Task 1**

```bash
git add app_core/douyin_commerce_service.py test_douyin_commerce_service.py
git commit -m "实现抖音地点同面板原子应用"
```

---

### Task 2: 将正式会话与批量执行器切换到单一地点入口

**Files:**
- Modify: `app_core/douyin_commerce_session.py:332-357, 930-1010`
- Modify: `app_core/douyin_commerce_batch_executor.py:609-637`
- Test: `test_douyin_commerce_service.py:4857-7454`
- Test: `test_douyin_commerce_batch_executor.py:30-820`

**Interfaces:**
- Consumes: `apply_saved_commerce_location_to_page(page, preset, scope, keywords)` from Task 1
- Produces: `DouyinCommerceSessionManager.apply_saved_location(session_id: str, preset: Mapping[str, Any], scope: object, keywords: list[str]) -> dict[str, Any]`
- Produces result: `{"location": dict[str, str], "matchedKeyword": str}`

- [ ] **Step 1: 改造内存替身并写执行器 RED 测试**

在 `FakeCommerceSessionManager` 增加单一入口，保留原 `search_locations`/`apply_location` 供非批量合约测试使用，但新批量执行器测试要断言它们不再被调用。

```python
def apply_saved_location(
    self,
    session_id: str,
    preset: dict,
    scope: str,
    keywords: list[str],
) -> dict:
    index = self._index_by_session[session_id]
    self.calls.append(f"apply_saved_location:{index}")
    self.ordered_calls.append(("apply_saved_location", session_id))
    self.atomic_location_requests.append(
        (session_id, dict(preset), scope, list(keywords))
    )
    location = dict(self.location_candidates[0]) if self.location_candidates else dict(preset)
    return {"location": location, "matchedKeyword": keywords[0]}
```

在替身的 `__init__` 中增加 `self.atomic_location_requests = []`，不把原子请求写入旧 `location_searches`，以便测试能分清新旧入口。

新增用例必须断言：

```python
def test_executor_uses_one_atomic_location_action_before_schedule(self) -> None:
    manager = FakeCommerceSessionManager()

    result = DouyinCommerceBatchExecutor(manager).run_preflight(
        self.batch, task_id=self.task["id"]
    )

    self.assertEqual([row["status"] for row in result], ["preflighted"] * 3)
    first_session_calls = [
        name for name, session_id in manager.ordered_calls if session_id == "session-1"
    ]
    self.assertEqual(
        first_session_calls,
        [
            "prepare_publish_settings",
            "select_cached_favorite_music",
            "select_content_declaration",
            "apply_saved_location",
            "sync_schedule",
            "preflight",
        ],
    )
    self.assertNotIn("search_locations:0", manager.calls)
    self.assertNotIn("apply_location:0", manager.calls)
```

另新增错误用例：`apply_saved_location` 抛出 `publish_location_candidate_missing`、`publish_location_click_failed`、`publish_location_readback_mismatch` 时，当前条目为 `failed`，`sync_schedule`、`preflight`、`submit` 均未调用，会话仍在 `finally` 关闭。

- [ ] **Step 2: 运行新执行器用例并确认 RED**

Run:

```bash
.venv/bin/python -m unittest \
  test_douyin_commerce_batch_executor.DouyinCommerceBatchExecutorTests.test_executor_uses_one_atomic_location_action_before_schedule
```

Expected: `FAIL`，旧执行器仍调用 `search_locations` 和 `apply_location`。

- [ ] **Step 3: 实现正式会话公开入口**

在公开方法中严格校验 session ID、完整地点、范围和 1—3 个非空关键词，再进入固定事件循环：

```python
def apply_saved_location(
    self,
    session_id: str,
    preset: Mapping[str, Any],
    scope: object,
    keywords: list[str],
) -> dict[str, Any]:
    normalized = douyin_commerce_service.normalize_commerce_location_candidate(preset)
    if not normalized:
        raise DouyinCommerceSessionError("publish_location_candidate_missing")
    selected_scope = douyin_commerce_service.normalize_commerce_location_scope(scope)
    bounded_keywords = list(dict.fromkeys(
        _normalized(value) for value in keywords if _normalized(value)
    ))[:3]
    if not bounded_keywords:
        raise DouyinCommerceSessionError("publish_location_candidate_missing")
    return self._call(
        self._apply_saved_location(
            session_id,
            dict(normalized),
            selected_scope,
            bounded_keywords,
        )
    )
```

私有协程调用 Task 1 原子动作，只在成功后写入 `session.commerce_location_candidates`、`session.location`、`session.location_scope`，并使预检指纹与排期失效。

```python
async def _apply_saved_location(
    self,
    session_id: str,
    preset: dict[str, Any],
    scope: str,
    keywords: list[str],
) -> dict[str, Any]:
    session = await self._current(session_id)
    self._ensure_editor_not_blocked_by_music_picker(session)
    result = await douyin_commerce_service.apply_saved_commerce_location_to_page(
        session.page, preset, scope, keywords
    )
    location = result.get("location") if isinstance(result, Mapping) else None
    normalized = douyin_commerce_service.normalize_commerce_location_candidate(location)
    if not normalized:
        raise DouyinCommerceSessionError("publish_location_readback_mismatch")
    session.commerce_location_candidates = [dict(normalized)]
    session.location = dict(normalized)
    session.location_scope = scope
    session.stores = []
    session.selected_store = None
    session.preflight_fingerprint = ""
    session.schedule_time = ""
    self._refresh_editor_stage(session)
    return {"location": dict(normalized), "matchedKeyword": _normalized(result.get("matchedKeyword"))}
```

- [ ] **Step 4: 用单一原子入口替换批量执行器的两步调用**

替换 `app_core/douyin_commerce_batch_executor.py:609-637`：

```python
location = payload["locationPoi"]
scope = _text(payload["locationScope"])
applied = self._manager.apply_saved_location(
    session_id,
    location,
    scope,
    _location_search_keywords(location),
)
applied_location = (
    applied.get("location")
    if isinstance(applied, Mapping) and isinstance(applied.get("location"), Mapping)
    else None
)
try:
    confirmed_location = match_location_preset(
        location,
        [dict(applied_location)] if isinstance(applied_location, Mapping) else [],
    )
except Exception as exc:
    raise DouyinCommerceBatchExecutorError(
        "publish_location_readback_mismatch"
    ) from exc
if any(
    _text(applied_location.get(field)) != _text(confirmed_location.get(field))
    for field in ("poiId", "name", "address")
):
    raise DouyinCommerceBatchExecutorError("publish_location_readback_mismatch")
```

批量失败文案保留视频序号与固定错误码，不把底层 cause 或 DOM 原文写入任务。

- [ ] **Step 5: 运行会话与执行器回归**

Run:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest \
  test_douyin_commerce_service.DouyinCommerceSessionContractTests \
  test_douyin_commerce_batch_executor.DouyinCommerceBatchExecutorTests
```

Expected: `OK`；新顺序为 `prepare -> music -> declaration -> apply_saved_location -> schedule -> preflight -> submit`。

- [ ] **Step 6: 提交 Task 2**

```bash
git add app_core/douyin_commerce_session.py app_core/douyin_commerce_batch_executor.py \
  test_douyin_commerce_service.py test_douyin_commerce_batch_executor.py
git commit -m "切换抖音批量发布地点原子链路"
```

---

### Task 3: 增加平台设置底部动态进度条

**Files:**
- Modify: `ui/douyin_commerce_page.py:1-70, 460-525, 1299-1315, 4932-5005, 5229-5305, 5674-5700, 5870-5910, 6350-6460`
- Modify: `ui/common.py:902-922`
- Test: `test_douyin_commerce_service.py:7455-9936`

**Interfaces:**
- Produces: `_start_platform_collector_progress(generation_id: str, collector_type: str, action_token: int, label: str) -> None`
- Produces: `_finish_platform_collector_progress(generation_id: str, collector_type: str, action_token: int) -> None`
- Produces: `_reset_platform_collector_progress() -> None`
- Produces: `_update_platform_collector_progress() -> None`

- [ ] **Step 1: 写进度可见性、耗时和迟到收口 RED 测试**

在 `DouyinCommerceBatchUiTests` 中新增四项用例：

```python
def test_platform_collector_progress_is_indeterminate_and_shows_elapsed_seconds(self) -> None:
    with patch("ui.douyin_commerce_page.time.monotonic", side_effect=[100.0, 107.4]):
        self.page._start_platform_collector_progress(
            "generation-a", "favorite_music", 1, "正在刷新收藏音乐"
        )
        self.page._update_platform_collector_progress()

    self.assertFalse(self.page.platform_collector_progress_frame.isHidden())
    self.assertEqual(self.page.platform_collector_progress.minimum(), 0)
    self.assertEqual(self.page.platform_collector_progress.maximum(), 0)
    self.assertEqual(
        self.page.platform_collector_progress_label.text(),
        "正在刷新收藏音乐 · 已等待 7 秒",
    )

def test_stale_collector_finish_cannot_hide_current_progress(self) -> None:
    self.page._start_platform_collector_progress(
        "generation-new", "local_location", 3, "正在搜索本地点"
    )

    self.page._finish_platform_collector_progress(
        "generation-old", "local_location", 2
    )

    self.assertFalse(self.page.platform_collector_progress_frame.isHidden())
    self.assertEqual(
        self.page._platform_collector_progress_owner,
        ("generation-new", "local_location", 3),
    )
```

其余两项通过可控 runner 断言：音乐刷新、国内/本地搜索和重试启动时显示对应文案；放弃/登录失效调用重置后进度所有者为 `None`、定时器停止、控件隐藏。

- [ ] **Step 2: 运行新 UI 用例并确认 RED**

Run:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest \
  test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_platform_collector_progress_is_indeterminate_and_shows_elapsed_seconds \
  test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_stale_collector_finish_cannot_hide_current_progress
```

Expected: `ERROR`，进度控件与状态方法尚不存在。

- [ ] **Step 3: 在平台底栏建立不确定进度控件**

在 `platform_review_status` 与底栏按钮之间新增：

```python
self.platform_collector_progress_frame = QFrame()
self.platform_collector_progress_frame.setObjectName(
    "douyinCommercePlatformCollectorProgress"
)
platform_progress_layout = QVBoxLayout(self.platform_collector_progress_frame)
platform_progress_layout.setContentsMargins(10, 7, 10, 7)
platform_progress_layout.setSpacing(4)
self.platform_collector_progress_label = QLabel("")
self.platform_collector_progress_label.setObjectName(
    "douyinCommercePlatformCollectorProgressLabel"
)
self.platform_collector_progress = QProgressBar()
self.platform_collector_progress.setObjectName(
    "douyinCommercePlatformCollectorProgressBar"
)
self.platform_collector_progress.setRange(0, 0)
self.platform_collector_progress.setTextVisible(False)
self.platform_collector_progress.setFixedWidth(180)
platform_progress_layout.addWidget(self.platform_collector_progress_label)
platform_progress_layout.addWidget(self.platform_collector_progress)
self.platform_collector_progress_frame.setVisible(False)
review_layout.addWidget(self.platform_collector_progress_frame)
```

`ui/common.py` 复用内容准备进度块的颜色和尺寸，但只使用上述三个专用 object name，不改全局 `QProgressBar`。

- [ ] **Step 4: 实现进度所有者与单调计时**

在页面初始化中增加 `import time`、所有者元组、动作文案、起始时间和 1 秒 `QTimer`。

```python
self._platform_collector_progress_owner: tuple[str, str, int] | None = None
self._platform_collector_progress_label_text = ""
self._platform_collector_progress_started = 0.0
self._platform_collector_progress_timer = QTimer(self)
self._platform_collector_progress_timer.setInterval(1_000)
self._platform_collector_progress_timer.timeout.connect(
    self._update_platform_collector_progress
)
```

四个方法必须使用精确所有权：

```python
def _start_platform_collector_progress(
    self,
    generation_id: str,
    collector_type: str,
    action_token: int,
    label: str,
) -> None:
    self._platform_collector_progress_owner = (
        _normalized(generation_id),
        _normalized(collector_type),
        int(action_token),
    )
    self._platform_collector_progress_label_text = _normalized(label)
    self._platform_collector_progress_started = time.monotonic()
    self.platform_collector_progress.setRange(0, 0)
    self.platform_collector_progress_frame.setVisible(True)
    self._update_platform_collector_progress()
    self._platform_collector_progress_timer.start()

def _update_platform_collector_progress(self) -> None:
    if self._platform_collector_progress_owner is None:
        return
    elapsed = max(
        0,
        int(time.monotonic() - self._platform_collector_progress_started),
    )
    self.platform_collector_progress_label.setText(
        f"{self._platform_collector_progress_label_text} · 已等待 {elapsed} 秒"
    )

def _finish_platform_collector_progress(
    self,
    generation_id: str,
    collector_type: str,
    action_token: int,
) -> None:
    owner = (_normalized(generation_id), _normalized(collector_type), int(action_token))
    if owner != self._platform_collector_progress_owner:
        return
    self._reset_platform_collector_progress()

def _reset_platform_collector_progress(self) -> None:
    self._platform_collector_progress_timer.stop()
    self._platform_collector_progress_owner = None
    self._platform_collector_progress_label_text = ""
    self._platform_collector_progress_started = 0.0
    self.platform_collector_progress_label.clear()
    self.platform_collector_progress_frame.setVisible(False)
```

- [ ] **Step 5: 接入国内启动、音乐、地点和重试动作**

`_start_setup_generation()` 使用当前代际值（首次为空字符串）、`domestic_location` 和 `start_token` 开始“正在准备国内地点采集”；`on_finished` 使用相同三元组收口。首次代际 ID 未返回时，空 ID + 唯一 `start_token` 仍是稳定所有者，不用伪造 ID。

`_run_collector_action()` 签名改为 `def _run_collector_action(self, collector_type: str, work, on_success, *, action_label: str) -> bool`。在 `runner.run()` 前开始进度，并把 `on_finished=self._sync_view` 改为：

```python
on_finished=lambda: self._collector_action_finished(
    generation_id, collector_type, action_token
)
```

```python
def _collector_action_finished(
    self,
    generation_id: str,
    collector_type: str,
    action_token: int,
) -> None:
    self._finish_platform_collector_progress(
        generation_id, collector_type, action_token
    )
    self._sync_view()
```

修改所有代际采集调用点，显式传入以下固定文案：

- `_refresh_favorite_music_candidates()`：`action_label="正在刷新收藏音乐"`
- `_search_batch_locations()` 和 `search_locations()` 的国内分支：`action_label="正在搜索国内地点"`
- 上述两处的本地分支：`action_label="正在搜索本地地点"`
- `_retry_last_failed_collector()`：收藏音乐为“正在重试收藏音乐”，国内地点为“正在重试国内地点”，本地地点为“正在重试本地地点”

这些文案只来自代码内固定字面量，不使用搜索关键词或底层错误拼接。国内初始采集发生在内容页跳转平台设置之前，因此同时保留现有内容底栏 `on_progress=self._set_commerce_progress`；新平台进度所有者不改变这一导航顺序。

- [ ] **Step 6: 接入放弃、登录、替换、关闭和退出重置**

在以下入口的状态清理前调用 `_reset_platform_collector_progress()`：

- `_reset_platform_settings_after_abandon()`
- `_clear_current_batch_platform_choices()`
- `_handle_login_required()`
- 新代际替换前
- `_setup_generation_close_succeeded()`
- `shutdown()` 或 `closeEvent()` 的页面收口路径

重置只操作 Qt 本地状态，不代替严格 `close_generation` 屏障。

- [ ] **Step 7: 运行 UI 专项回归**

Run:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest \
  test_douyin_commerce_service.DouyinCommerceUiTests \
  test_douyin_commerce_service.DouyinCommerceBatchUiTests
```

Expected: `OK`；进度条、迟到回调、放弃和登录重置用例全部通过。

- [ ] **Step 8: 提交 Task 3**

```bash
git add ui/douyin_commerce_page.py ui/common.py test_douyin_commerce_service.py
git commit -m "增加抖音平台采集动态进度"
```

---

### Task 4: 完整门禁回归、客户端自检与交付

**Files:**
- Modify when required by failed regression only: files already listed in Tasks 1-3
- Verify: `docs/superpowers/specs/2026-08-10-douyin-atomic-location-apply-and-platform-progress-design.md`
- Verify: `docs/superpowers/plans/2026-08-10-douyin-atomic-location-apply-and-platform-progress.md`

**Interfaces:**
- Consumes: all interfaces from Tasks 1-3
- Produces: no new production interface

- [ ] **Step 1: 运行联合定向回归**

Run:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest \
  test_douyin_commerce_service.DouyinCommercePayloadTests \
  test_douyin_commerce_service.DouyinCommerceLocationDomTests \
  test_douyin_commerce_service.DouyinCommerceSessionContractTests \
  test_douyin_commerce_service.DouyinCommerceUiTests \
  test_douyin_commerce_service.DouyinCommerceBatchUiTests \
  test_douyin_commerce_batch_executor.DouyinCommerceBatchExecutorTests
```

Expected: `OK`。

另外单独运行已有的人工清空回归，确保本次改造不会让“请选择地点”再次回填旧地址：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest \
  test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_batch_location_placeholder_clears_old_binding_then_next_search_refills
```

Expected: `OK`，且清空后 `_batch_locations` 不再包含该视频，下一次搜索可以自动填入新 POI。

- [ ] **Step 2: 运行完整离线回归**

Run:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -q
```

Expected: 测试数不少于基线 750，`failures=0`，`errors=0`。

- [ ] **Step 3: 运行编译、差异和离屏 UI 门禁**

Run:

```bash
.venv/bin/python -m py_compile \
  app_core/douyin_commerce_service.py \
  app_core/douyin_commerce_session.py \
  app_core/douyin_commerce_batch_executor.py \
  ui/douyin_commerce_page.py \
  test_douyin_commerce_service.py \
  test_douyin_commerce_batch_executor.py
git diff --check
QT_QPA_PLATFORM=offscreen .venv/bin/python desktop_native_app.py --ui-test
```

Expected: `py_compile` 退出 0，`git diff --check` 无输出，UI 输出 `NATIVE_DESKTOP_UI_OK`。

- [ ] **Step 4: 审计安全边界**

使用以下检查确认新代码没有私有请求、最终提交快捷路径或敏感诊断字段：

```bash
rg -n "cookie|authorization|localStorage|innerHTML|page\.content|request\.(get|post)|submit\(" \
  app_core/douyin_commerce_service.py \
  app_core/douyin_commerce_session.py \
  ui/douyin_commerce_page.py
```

逐条核对命中仅为现有合法边界；新增地点和进度方法不得新增上述能力。

- [ ] **Step 5: 检查任务 95 仍保持暂停**

Run:

```bash
sqlite3 -readonly -header -column demo-runtime/db/database.db \
  "SELECT id, taskNo, status, successCount, failedCount, pauseReasonCode FROM publish_tasks WHERE id=95;"
```

Expected: `status=paused`、`successCount=0`、`failedCount=5`、`pauseReasonCode=auto_failure`。不对数据库执行写操作。

- [ ] **Step 6: 重启源码客户端并验证进程来源**

只在所有离线门通过后，关闭当前源码客户端，从主仓库重新启动：

```bash
.venv/bin/python -u desktop_native_app.py --page commerce
```

用 `ps` 与 `lsof -d cwd` 确认 Python 进程的 cwd 为主仓库且命令行包含 `--page commerce`。这一步只打开本地 UI，不进入账号页、不触发采集或发布。

- [ ] **Step 7: 提交最终验证记录**

若 Task 4 未修改业务代码，不创建空提交；若回归暴露真实缺口，必须先写独立 RED、最小修复并重跑 Task 4 全部门，再使用：

```bash
git add app_core/douyin_commerce_service.py \
  app_core/douyin_commerce_session.py \
  app_core/douyin_commerce_batch_executor.py \
  ui/douyin_commerce_page.py ui/common.py \
  test_douyin_commerce_service.py test_douyin_commerce_batch_executor.py
git commit -m "补强抖音地点原子应用验证"
```

不将 `.superpowers/brainstorm/`、`outputs/`、数据库、日志、账号文件或诊断快照纳入 Git。

---

## Execution Stop Point

完成 Task 1—4 只表示离线代码与本地 UI 通过。不得因此恢复任务 95，也不得自动进行真实平台验收。如需实机验证，必须在实施完成后由 Andy 再次明确授权，仅使用 1 条测试视频和 1 个地点，停在预检/最终提交前并放弃临时页。
