# 抖音收藏音乐本地缓存 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让抖音带货按账号本地缓存用户已手动同步的收藏音乐，并在每次新上传后立即展示缓存，同时仍以当前无头编辑页的唯一选择和回读作为真实写入证据。

**Architecture:** 新增独立的本地缓存服务，负责 SQLite 表、账号隔离、候选净化和失效判定。分步编辑会话只负责把当前编辑页返回的候选交给缓存服务，并在用户点选缓存条目时重新打开当前编辑页的收藏音乐抽屉，以相同 `musicId` 精确匹配后选择和回读。界面优先展示缓存，用户点“刷新收藏音乐”才访问当前无头编辑页。

**Tech Stack:** Python 3、SQLite、PySide6、Playwright、unittest。

## Global Constraints

- 仅适用于抖音带货的收藏音乐；不影响其他平台与普通发布。
- 缓存按 `user_info.id` 的抖音账号 ID 隔离，禁止跨账号复用。
- 仅保存 `musicId`、标题、作者、时长和本机同步时间；禁止保存 marker、Cookie、二维码、页面 HTML、请求、封面 URL 或凭据。
- 没有平台稳定 `musicId` 的当前会话候选不进入长期缓存，但仍可在该会话中按既有临时 marker 选择。
- 用户点击“刷新收藏音乐”时才读平台；不自动打开浏览器、登录、上传、保存草稿、预览或发布。
- 从缓存选中不等于平台已写入：必须在当前无头编辑页重新匹配唯一 `musicId`、点击并回读；未匹配时停止并提示刷新。
- 不默认选择第一首音乐，不做音乐搜索、下载、上传、版权判断或收藏夹管理。

---

### Task 1: 本地缓存数据边界

**Files:**
- Modify: `app_core/database.py:130-170`
- Create: `app_core/douyin_favorite_music_cache.py`
- Create: `test_douyin_favorite_music_cache.py`

**Interfaces:**
- Produces: `replace_cached_favorite_music(account_id: int, rows: object) -> list[dict[str, str]]`
- Produces: `list_cached_favorite_music(account_id: int, *, limit: int = 200) -> list[dict[str, str]]`
- Produces: `cached_music_ids(rows: object) -> set[str]`
- Consumes: `app_core.database.connect()` and `douyin_music_service.normalize_music_readback()`.

- [ ] **Step 1: Write the failing cache-isolation test**

```python
def test_cache_isolated_by_account_and_keeps_only_safe_music_fields(self):
    first = replace_cached_favorite_music(3, [{
        "musicId": "music-a", "title": "歌曲 A", "creator": "作者 A",
        "duration": "03:21", "marker": "transient", "cookie": "never-store",
    }])
    second = replace_cached_favorite_music(4, [{
        "musicId": "music-b", "title": "歌曲 B", "creator": "作者 B",
        "duration": "02:18",
    }])

    assert list_cached_favorite_music(3) == first
    assert list_cached_favorite_music(4) == second
    assert "marker" not in first[0]
    assert "cookie" not in first[0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m unittest -v test_douyin_favorite_music_cache`

Expected: FAIL because `app_core.douyin_favorite_music_cache` does not exist.

- [ ] **Step 3: Write the failing stale/unstable-row test**

```python
def test_replace_ignores_rows_without_stable_platform_music_id(self):
    saved = replace_cached_favorite_music(3, [{
        "musicId": "favorite-index:1", "title": "临时行", "creator": "作者",
        "duration": "00:31", "marker": "only-this-session",
    }])

    assert saved == []
    assert list_cached_favorite_music(3) == []
```

- [ ] **Step 4: Run stale/unstable-row test to verify it fails**

Run: `.venv/bin/python -m unittest -v test_douyin_favorite_music_cache.DouyinFavoriteMusicCacheTests.test_replace_ignores_rows_without_stable_platform_music_id`

Expected: FAIL because the cache module and identity filter do not exist.

- [ ] **Step 5: Implement schema and cache service**

Add the following table to `database.ensure_schema()`:

```sql
CREATE TABLE IF NOT EXISTS douyin_favorite_music_cache (
    accountId INTEGER NOT NULL,
    musicId TEXT NOT NULL,
    title TEXT NOT NULL,
    creator TEXT NOT NULL,
    duration TEXT NOT NULL,
    syncedAt TEXT NOT NULL,
    PRIMARY KEY(accountId, musicId)
)
```

Implement `douyin_favorite_music_cache.py` with a `_normalize_cache_row()` helper that accepts only a normalized music row with a non-empty `musicId` that does not contain `favorite-index:`. `replace_cached_favorite_music()` must delete and insert one account’s rows in the same SQLite transaction. `list_cached_favorite_music()` must order by `syncedAt DESC, title COLLATE NOCASE, musicId`, cap `limit` to `1..200`, and return only the five approved fields: `musicId`、`title`、`creator`、`duration`、`syncedAt`.

- [ ] **Step 6: Run cache tests to verify they pass**

Run: `.venv/bin/python -m unittest -v test_douyin_favorite_music_cache`

Expected: PASS; account 3 never sees account 4’s rows, and transient identifiers/fields never persist.

- [ ] **Step 7: Commit Task 1**

```bash
git add app_core/database.py app_core/douyin_favorite_music_cache.py test_douyin_favorite_music_cache.py
git commit -m "feat: 缓存抖音收藏音乐候选"
```

### Task 2: 当前编辑会话的缓存刷新与精确重选

**Files:**
- Modify: `app_core/douyin_commerce_session.py:95-145, 275-305, 630-680`
- Modify: `app_core/douyin_music_service.py:677-765`
- Modify: `test_douyin_commerce_service.py`

**Interfaces:**
- Consumes: `list_cached_favorite_music(account_id)` and `replace_cached_favorite_music(account_id, rows)`.
- Produces: `CommerceSessionManager.cached_favorite_music(session_id: str) -> list[dict[str, str]]`
- Produces: `CommerceSessionManager.refresh_favorite_music(session_id: str) -> list[dict[str, str]]`
- Produces: `CommerceSessionManager.select_cached_favorite_music(session_id: str, music_id: str) -> dict[str, str]`
- Produces: `douyin_music_service.find_favorite_music_by_id(candidates: list[dict[str, str]], music_id: str) -> dict[str, str]`

- [ ] **Step 1: Write the failing cache-display test**

```python
def test_cached_music_returns_only_current_editor_account_rows(self):
    manager = CommerceSessionManager()
    session = self._session(account_id=3, account_name="账号甲")
    manager._session = session
    replace_cached_favorite_music(3, [self._music("m-1")])
    replace_cached_favorite_music(4, [self._music("m-2")])

    assert manager.cached_favorite_music(session.session_id) == [
        {"musicId": "m-1", "title": "歌曲", "creator": "作者", "duration": "03:21", "syncedAt": mock.ANY}
    ]
```

- [ ] **Step 2: Run cache-display test to verify it fails**

Run: `.venv/bin/python -m unittest -v test_douyin_commerce_service.DouyinCommerceSessionTests.test_cached_music_returns_only_current_editor_account_rows`

Expected: FAIL because the session does not retain `account_id` and has no cache API.

- [ ] **Step 3: Write the failing stale-cache selection test**

```python
def test_cached_choice_stops_when_current_platform_list_lacks_same_music_id(self):
    manager = self._manager_with_uploaded_session(account_id=3)
    with patch.object(douyin_music_service, "open_favorite_music_choices", return_value=(page, dialog, [self._music("m-other")])):
        with self.assertRaisesRegex(DouyinCommerceSessionError, "缓存已过期"):
            asyncio.run(manager._select_cached_favorite_music(session_id, "m-cached"))
```

- [ ] **Step 4: Run stale-cache selection test to verify it fails**

Run: `.venv/bin/python -m unittest -v test_douyin_commerce_service.DouyinCommerceSessionTests.test_cached_choice_stops_when_current_platform_list_lacks_same_music_id`

Expected: FAIL because cache-specific selection does not exist.

- [ ] **Step 5: Implement session APIs and exact matching**

Extend `_CommerceEditorSession` with `account_id: int`; populate it from the already-resolved `user_info` row in `_start_upload()`. Implement public thread-safe wrappers and coroutine implementations for cached list, refresh and cached select.

`refresh_favorite_music()` must call the existing `open_favorite_music_choices()` exactly once, retain the returned transient marker only in the in-memory session, cache only stable-ID rows, and return public cached-safe rows. If all returned rows lack stable IDs, return an empty cache list while preserving the session candidates for this one session’s immediate manual selection.

`select_cached_favorite_music()` must open the current editor’s music drawer if it is not already open, collect current candidates, call `find_favorite_music_by_id()`, require exactly one match, then call existing `select_favorite_music_choice()` and validate `_same_music()` against the selected cache identity. If absent, duplicated, markerless, or mismatched, clear transient candidates, close no additional dialogs by guesswork, and raise `DouyinCommerceSessionError("收藏音乐缓存已过期，请刷新后重新选择")`.

Implement `find_favorite_music_by_id()` as exact normalized ID matching only; it must reject an empty ID or anything other than exactly one current candidate.

- [ ] **Step 6: Run session/music tests to verify they pass**

Run: `.venv/bin/python -m unittest -v test_douyin_commerce_service test_douyin_publish_executor`

Expected: PASS; cached candidates show immediately, current-page mismatch stops safely, and existing user-selected music behavior stays green.

- [ ] **Step 7: Commit Task 2**

```bash
git add app_core/douyin_commerce_session.py app_core/douyin_music_service.py test_douyin_commerce_service.py
git commit -m "feat: 支持抖音收藏音乐缓存选择"
```

### Task 3: 平台设置界面与本地验收

**Files:**
- Modify: `ui/douyin_commerce_page.py:1000-1060, 2860-2960`
- Modify: `test_douyin_commerce_service.py`
- Modify: `docs/DOUYIN_COMMERCE_WORKFLOW.md`

**Interfaces:**
- Consumes: `commerce_session_manager.cached_favorite_music(session_id)`.
- Consumes: `commerce_session_manager.refresh_favorite_music(session_id)`.
- Consumes: `commerce_session_manager.select_cached_favorite_music(session_id, music_id)`.
- Produces: one local “刷新收藏音乐” action and an immediate cached music combo state.

- [ ] **Step 1: Write the failing UI state test**

```python
def test_music_combo_renders_cached_candidates_before_platform_refresh(self):
    page = self._commerce_page_with_uploaded_session()
    with patch.object(manager, "cached_favorite_music", return_value=[self._music("m-1")]):
        page._load_cached_music_candidates()

    assert page.music_combo.count() == 2
    assert page.music_combo.itemData(1)["musicId"] == "m-1"
    assert page.music_status.text() == ""
```

- [ ] **Step 2: Run UI state test to verify it fails**

Run: `.venv/bin/python -m unittest -v test_douyin_commerce_service.DouyinCommercePageTests.test_music_combo_renders_cached_candidates_before_platform_refresh`

Expected: FAIL because the UI always triggers platform candidate loading.

- [ ] **Step 3: Implement cache-first UI behavior**

On entering platform settings after upload, call `cached_favorite_music(session_id)` in the existing worker pathway and fill `music_combo` from returned safe rows. Add a compact `刷新收藏音乐` button beside the combo; it calls only `refresh_favorite_music(session_id)` and replaces the displayed cache list on success.

Change `_load_favorite_music_candidates()` so it first opens the cached combo when rows exist. When the user explicitly clicks refresh, show only `正在刷新收藏音乐…`; on failure keep existing cached rows visible and show the error below the control. Change `_start_music_write()` to call `select_cached_favorite_music()` for cached rows and retain existing `select_favorite_music()` only for session-only marker rows returned by a just-completed refresh.

Do not add success prose such as “已由当前抖音编辑页回读确认”; the selected music card is the visible outcome. Do not change location, declaration, scheduling, upload, verification, draft or submission actions.

- [ ] **Step 4: Run UI and regression tests to verify they pass**

Run: `.venv/bin/python -m unittest -v test_douyin_commerce_service test_douyin_commerce_draft_service test_douyin_favorite_music_cache test_douyin_publish_executor test_douyin_verification test_douyin_verification_dialog`

Expected: PASS; cached choice is instant, manual refresh replaces only that account’s cache, stale choice stops safely, and verification regressions remain green.

- [ ] **Step 5: Update workflow documentation**

Add one “收藏音乐缓存” subsection to `docs/DOUYIN_COMMERCE_WORKFLOW.md` that states: cache is local/account-scoped metadata, refresh is user-triggered after upload, a cached selection still needs current-editor exact write/readback, and cache never proves platform draft or publication.

- [ ] **Step 6: Commit Task 3**

```bash
git add ui/douyin_commerce_page.py test_douyin_commerce_service.py docs/DOUYIN_COMMERCE_WORKFLOW.md
git commit -m "feat: 在抖音带货显示收藏音乐缓存"
```

### Task 4: 开发版本地验收

**Files:**
- Modify: none
- Test: `desktop_native_app.py --page commerce`

**Interfaces:**
- Consumes: completed cache, session and UI APIs from Tasks 1–3.
- Produces: a source-development client observation only; no platform write.

- [ ] **Step 1: Start the source development client**

Run:

```bash
launchctl remove com.oneclick.dev.commerce 2>/dev/null || true
launchctl submit -l com.oneclick.dev.commerce -- \
  .venv/bin/python -u desktop_native_app.py --page commerce
```

- [ ] **Step 2: Verify local UI without platform action**

Check that an existing uploaded-session test fixture shows cached music in the dropdown before refresh; `刷新收藏音乐` is the only action that reads the platform page; selecting one cached item enters the existing background write progress state. Do not log in, upload, save a draft, preview, submit or publish.

- [ ] **Step 3: Record evidence and commit no runtime artifacts**

Run: `git status --short`

Expected: no database, storage state, screenshots, logs or temporary browser data are staged. The only commits are Tasks 1–3.

## Self-review

- Spec coverage: Task 1 covers local storage and data minimization; Task 2 covers session-bound platform write/readback and stale rejection; Task 3 covers cache-first UI, manual refresh and user-visible failure; Task 4 covers development-client verification without platform actions.
- 占位符扫描：没有未决标记、模糊“适当处理”或未明确的测试步骤。
- Type consistency: cache rows use `dict[str, str]`; session APIs take `session_id: str` and music IDs; only the cache service receives `account_id: int`.
