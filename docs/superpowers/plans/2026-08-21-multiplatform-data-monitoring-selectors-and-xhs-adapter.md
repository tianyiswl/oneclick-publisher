# Multi-platform Data Monitoring Selectors and Xiaohongshu Adapter Implementation Plan

> Command note: examples use `.venv/bin/python` from the repository root. From the isolated `.worktrees/<name>` checkout, use the shared interpreter at `../../.venv/bin/python`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add separate platform and subject selectors to Data Monitoring and connect the already verified Xiaohongshu read-only contracts to the production synchronization pipeline.

**Architecture:** `DataMonitorPage` selects a registered platform first and filters subjects by strict `platformType`; synchronization resolves a platform-specific collector through an explicit registry. `XiaohongshuDataCollector` opens one short-lived saved-session browser, accepts only the four reviewed official JSON paths, converts proven fields into the existing `CollectionBatch`, and writes through the existing atomic platform-data service before the UI rereads local data.

**Tech Stack:** Python 3.12, PyQt6, Playwright sync API, SQLite, `unittest`, existing `BackgroundTaskRunner` and platform data models.

**Spec:** `docs/superpowers/specs/2026-08-20-xiaohongshu-data-monitoring-adapter-design.md`

## Global Constraints

- The Data Monitoring header must contain two selectors in this order: `平台` then `主体`.
- The platform selector lists only platform types registered in the production collector registry; initial values are Xiaohongshu `1` and Douyin `3`.
- The subject selector displays only accounts belonging to the selected platform; each option stores one built-in positive integer `accountId`.
- Switching platform or subject clears the previous cards, trend and content rows before reading the new account.
- Never export, log, persist or display cookies, tokens, signatures, headers, raw bodies, complete URLs, HTML, QR data or verification data.
- Xiaohongshu collection may navigate only to reviewed `https://creator.xiaohongshu.com` pages and may passively consume only the four exact reviewed JSON paths.
- Never replay captured requests with `requests`, `fetch` or a copied Cookie.
- Unknown fields and unproved metric meanings are ignored; unavailable values display `—`, never fabricated `0`.
- A run is not successful unless cleanup reports `closed is True` and `type(aliveResourceCount) is int` with value `0`.
- `KeyboardInterrupt` and `SystemExit` propagate only after cleanup attempts complete.
- Preserve the existing untracked `.superpowers/brainstorm/`, `outputs/` and Archify visual-check files.
- Do not run a real Xiaohongshu browser synchronization until Andy explicitly authorizes that exact run after offline tests pass.

## File Map

- Create `app_core/platform_data_collection_errors.py`: platform-neutral collection exception and public fixed-code mapping.
- Modify `app_core/douyin_data_collector.py`: inherit the neutral collection exception without changing Douyin behavior.
- Modify `app_core/platform_data_collectors.py`: explicit collector registry and supported-platform query.
- Create `app_core/xiaohongshu_data_contract.py`: exact-path, built-in-only parsers for the reviewed Xiaohongshu payloads.
- Modify `app_core/platform_data_models.py`: permit explicitly unavailable optional content metadata without inventing values.
- Create `app_core/xiaohongshu_data_collector.py`: bounded short-session collector and strict cleanup.
- Modify `app_core/platform_data_sync.py`: catch platform-neutral errors and emit platform-neutral progress.
- Modify `ui/data_monitor_page.py`: platform selector, subject selector, per-platform last selection and stale-view clearing.
- Modify `ui/main_window.py`: no new API; verify existing page refresh continues to call `DataMonitorPage.refresh()`.
- Create `test_xiaohongshu_data_collector.py`: parser, browser lifecycle and model conversion tests.
- Modify `test_platform_data_sync.py`: Xiaohongshu routing, persistence and fixed-error tests.
- Modify `test_data_monitor_page.py`: two-selector behavior, switching isolation and progress tests.
- Update `docs/superpowers/reports/2026-08-20-xiaohongshu-data-contract-report.md`: production-adapter and real-run evidence after completion.

---

### Task 1: Platform-neutral collector contract and explicit registry

**Files:**
- Create: `app_core/platform_data_collection_errors.py`
- Modify: `app_core/douyin_data_collector.py`
- Modify: `app_core/platform_data_collectors.py`
- Test: `test_douyin_data_collector.py`
- Test: `test_platform_data_sync.py`

**Interfaces:**
- Produces: `PlatformDataCollectionError(error_code: str, *, fallback_allowed: bool = False, retryable: bool = False)`.
- Produces: `registered_platform_types() -> tuple[int, ...]` returning `(1, 3)` after Task 3 registers Xiaohongshu.
- Produces: `collector_for_platform(platform_type: int, **dependencies) -> PlatformDataCollector`.
- Preserves: `DouyinDataCollectionError` remains import-compatible and is a subclass of `PlatformDataCollectionError`.

- [ ] **Step 1: Write failing registry and neutral-error tests**

Add these tests before changing production code:

```python
def test_registry_exposes_only_explicit_platforms(self):
    self.assertEqual(registered_platform_types(), (3,))
    with self.assertRaises(CollectionFailure) as caught:
        collector_for_platform(True)
    self.assertEqual(caught.exception.error_code, "collector_not_available")

def test_douyin_error_is_platform_neutral_compatible(self):
    error = DouyinDataCollectionError("login_required", fallback_allowed=False)
    self.assertIsInstance(error, PlatformDataCollectionError)
    self.assertEqual(error.error_code, "login_required")
    self.assertFalse(error.fallback_allowed)
```

- [ ] **Step 2: Run the two named tests and verify RED**

Run:

```bash
.venv/bin/python -m unittest -v \
  test_douyin_data_collector.DouyinDataCollectorTests.test_registry_exposes_only_explicit_platforms \
  test_douyin_data_collector.DouyinDataCollectorTests.test_douyin_error_is_platform_neutral_compatible
```

Expected: imports or assertions fail because the neutral error and registry query do not exist.

- [ ] **Step 3: Implement the neutral error and registry**

Create the exact exception boundary:

```python
from .platform_data_models import CollectionFailure

class PlatformDataCollectionError(CollectionFailure):
    def __init__(
        self,
        error_code: str,
        *,
        fallback_allowed: bool = False,
        retryable: bool = False,
    ) -> None:
        self.fallback_allowed = type(fallback_allowed) is bool and fallback_allowed
        super().__init__(error_code, retryable=retryable)
```

In `douyin_data_collector.py`, make `DouyinDataCollectionError` inherit this type while preserving its constructor and all current call sites. In `platform_data_collectors.py`, replace the branch with a private built-in dictionary `_COLLECTOR_FACTORIES`; validate with `type(platform_type) is int`, return sorted keys from `registered_platform_types()`, and raise `CollectionFailure("collector_not_available") from None` for unsupported values.

- [ ] **Step 4: Run the named tests and current Douyin collector module**

```bash
.venv/bin/python -m unittest -v \
  test_douyin_data_collector.DouyinDataCollectorTests.test_registry_exposes_only_explicit_platforms \
  test_douyin_data_collector.DouyinDataCollectorTests.test_douyin_error_is_platform_neutral_compatible
.venv/bin/python -m unittest -v test_douyin_data_collector test_platform_data_sync
```

Expected: named tests and modules finish `OK`; registry is still `(3,)` until Task 3.

- [ ] **Step 5: Commit**

```bash
git add app_core/platform_data_collection_errors.py app_core/douyin_data_collector.py app_core/platform_data_collectors.py test_douyin_data_collector.py test_platform_data_sync.py
git commit -m "统一平台数据采集器协议"
```

---

### Task 2: Separate platform and subject selectors

**Files:**
- Modify: `ui/data_monitor_page.py`
- Modify: `test_data_monitor_page.py`

**Interfaces:**
- Consumes: `registered_platform_types() -> tuple[int, ...]` from Task 1.
- Produces UI fields: `platform_combo: QComboBox`, `account_combo: QComboBox` where `account_combo` is the subject selector kept for backward test compatibility.
- Produces: `_refresh_subjects(*, preferred_account_id: int | None = None) -> None`.
- Produces: `_clear_account_view(message: str) -> None`.

- [ ] **Step 1: Write failing two-selector tests**

Use accounts with two platforms and two Xiaohongshu subjects:

```python
XHS_ACCOUNT = {
    **ACCOUNT,
    "id": 21,
    "type": 1,
    "platformName": "小红书",
    "profileName": "硅基探索",
    "userName": "硅基探索",
}

def test_platform_then_subject_selectors_filter_and_restore_per_platform(self):
    page = self._page(accounts=[ACCOUNT, ACCOUNT_B, XHS_ACCOUNT])
    self.assertEqual([page.platform_combo.itemData(i) for i in range(page.platform_combo.count())], [3, 1])
    self.assertEqual(page.account_combo.count(), 2)
    page.account_combo.setCurrentIndex(page.account_combo.findData(14))
    page.platform_combo.setCurrentIndex(page.platform_combo.findData(1))
    self.assertEqual(page.account_combo.count(), 1)
    self.assertEqual(page.account_combo.currentData(), 21)
    self.assertEqual(page.account_combo.currentText(), "硅基探索｜硅基探索")
    page.platform_combo.setCurrentIndex(page.platform_combo.findData(3))
    self.assertEqual(page.account_combo.currentData(), 14)

def test_switch_platform_clears_previous_rows_before_new_account_readback(self):
    page = self._page(accounts=[ACCOUNT, XHS_ACCOUNT], contents=available_contents())
    self.assertGreater(page.content_table.rowCount(), 0)
    with patch.object(page, "_render_data") as render:
        page.platform_combo.setCurrentIndex(page.platform_combo.findData(1))
        self.assertEqual(page.content_table.rowCount(), 0)
        render.assert_called_once_with(include_contents=True)
```

Add a no-account test asserting subject count `0`, disabled sync button, and status `请先在账号管理中添加小红书账号`.

- [ ] **Step 2: Run only the new UI tests and verify RED**

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest -v \
  test_data_monitor_page.DataMonitorPageTests.test_platform_then_subject_selectors_filter_and_restore_per_platform \
  test_data_monitor_page.DataMonitorPageTests.test_switch_platform_clears_previous_rows_before_new_account_readback \
  test_data_monitor_page.DataMonitorPageTests.test_platform_without_subject_disables_sync
```

Expected: fail because `platform_combo` does not exist and `refresh()` hard-codes type `3`.

- [ ] **Step 3: Implement platform-first filtering**

In `_build_ui()`, insert `platform_combo` before `account_combo`, connect it to `_platform_changed`, and retain `account_combo` as the subject selector. Add private state:

```python
self._accounts_by_platform: dict[int, tuple[dict, ...]] = {}
self._last_account_by_platform: dict[int, int] = {}
```

`refresh()` must snapshot the current strict platform/account IDs, rebuild only registered platforms that have at least one account, then call `_refresh_subjects()`. `_platform_changed()` must save the previous account for its previous platform, call `_clear_account_view()`, rebuild subjects, and render the new account. Subject labels are built only from exact strings using `profileName` / `userName`, falling back to `未命名主体` / `账号 {id}`.

- [ ] **Step 4: Run the new tests, then the Data Monitoring UI module**

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest -v \
  test_data_monitor_page.DataMonitorPageTests.test_platform_then_subject_selectors_filter_and_restore_per_platform \
  test_data_monitor_page.DataMonitorPageTests.test_switch_platform_clears_previous_rows_before_new_account_readback \
  test_data_monitor_page.DataMonitorPageTests.test_platform_without_subject_disables_sync
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest -v test_data_monitor_page
```

Expected: all finish `OK`; update the old `test_only_douyin_accounts_are_listed...` contract to assert registered-platform filtering rather than Douyin-only filtering.

- [ ] **Step 5: Commit**

```bash
git add ui/data_monitor_page.py test_data_monitor_page.py
git commit -m "拆分数据监测平台与主体选择"
```

---

### Task 3: Exact Xiaohongshu payload parsers

**Files:**
- Create: `app_core/xiaohongshu_data_contract.py`
- Modify: `app_core/platform_data_models.py`
- Create: `test_xiaohongshu_data_collector.py`

**Interfaces:**
- Produces: `parse_account_overview(payload: object, observed_at: str, platform_day: str) -> tuple[MetricPoint, ...]`.
- Produces: `parse_content_list(payload: object) -> tuple[XhsContentIdentity, ...]`.
- Produces: `parse_content_lifetime(payload: object, identity: XhsContentIdentity, observed_at: str, platform_day: str) -> tuple[ContentRecord, tuple[MetricPoint, ...]]`.
- Produces: immutable `XhsContentIdentity(content_id: str)`; no title/date/type is invented at this boundary.
- Updates: `ContentRecord` accepts exact empty strings for unobserved `title`, `cover_url` and `published_at`, plus `content_status="unavailable"` and `content_type="unavailable"`.
- Preserves: `registered_platform_types() == (3,)` until Task 4 creates and registers the real collector.

- [ ] **Step 1: Write failing built-in-only parser tests**

Add fixtures that mirror only reviewed fields:

```python
def test_parse_account_overview_uses_only_proven_fans_total(self):
    points = parse_account_overview(
        {"data": {"fans_count": 3199}},
        observed_at="2026-08-21T12:00:00+08:00",
        platform_day="2026-08-21",
    )
    self.assertEqual([(p.metric_key, p.metric_value, p.metric_scope) for p in points], [("followers_total", 3199, "lifetime_total")])

def test_parse_content_list_accepts_exact_ids_and_rejects_bool_counts(self):
    rows = parse_content_list({"data": {"note_infos": [{"id": "6a0ffca800000000080033f8"}], "total": 1}})
    self.assertEqual(rows[0].content_id, "6a0ffca800000000080033f8")
    with self.assertRaises(CollectionFailure):
        parse_content_list({"data": {"note_infos": [{"id": "6a0ffca800000000080033f8"}], "total": True}})

def test_parse_lifetime_maps_only_reviewed_metrics_and_marks_unknown_text(self):
    content, points = parse_content_lifetime(
        {"data": {"note_info": {"id": "6a0ffca800000000080033f8", "view_count": 148, "like_count": 5, "comment_count": 9}, "collect_count": 2, "share_count": 4}},
        XhsContentIdentity("6a0ffca800000000080033f8"),
        observed_at="2026-08-21T12:00:00+08:00",
        platform_day="2026-08-21",
    )
    self.assertEqual(content.title, "")
    self.assertEqual(content.published_at, "")
    self.assertEqual(content.content_type, "unavailable")
    self.assertEqual({p.metric_key for p in points}, {"views", "likes", "comments", "favorites", "shares"})
```

Also test duplicate IDs, mismatched lifetime ID, subclasses, strings for numeric fields, NaN, unknown containers and more than 50 items. Every failure must expose only `metric_payload_invalid` or `content_list_truncated`, with no exception cause containing payload values.

- [ ] **Step 2: Run the parser tests and verify RED**

```bash
.venv/bin/python -m unittest -v \
  test_xiaohongshu_data_collector.XiaohongshuDataContractTests
```

Expected: import fails because `xiaohongshu_data_contract.py` does not exist.

- [ ] **Step 3: Implement exact parsers**

Use `type(value) is dict/list/str/int/float`; never call coercive `str()` or `int()` on payload fields. Accept only IDs matching `^[0-9a-f]{24}$`. Map keys exactly:

```python
_CONTENT_METRICS = {
    "view_count": "views",
    "like_count": "likes",
    "comment_count": "comments",
    "share_count": "shares",
    "collect_count": "favorites",
}
```

Extend `ContentRecord` narrowly so unobserved optional metadata stays absent rather than fabricated: accept only exact built-in strings; `title`, `cover_url` and `published_at` may be `""`; add `"unavailable"` to `ALLOWED_CONTENT_TYPES`; require `content_status="unavailable"` whenever any of those metadata fields is empty. Existing Douyin records remain subject to their current non-empty contract. The Xiaohongshu parser creates an empty-metadata record tied only to the proven content ID and lifetime metrics. Do not persist a derived title, fake URL, observation time as publication time, or guessed image/video type.

- [ ] **Step 4: Keep platform 1 unavailable until the collector exists**

Do not add a Xiaohongshu factory or type `1` to the production registry in this task. Keep the registry test asserting `(3,)` and rejecting type `1`, booleans and strings. Task 4 creates the collector and registers it in the same change.

- [ ] **Step 5: Run parser and current-registry tests**

```bash
.venv/bin/python -m unittest -v \
  test_xiaohongshu_data_collector.XiaohongshuDataContractTests \
  test_douyin_data_collector.DouyinDataCollectorTests.test_registry_exposes_only_explicit_platforms
```

Expected: all finish `OK`.

- [ ] **Step 6: Commit**

```bash
git add app_core/xiaohongshu_data_contract.py app_core/platform_data_models.py app_core/platform_data_collectors.py test_xiaohongshu_data_collector.py test_douyin_data_collector.py test_platform_data_service.py
git commit -m "增加小红书数据合同转换"
```

---

### Task 4: Bounded Xiaohongshu short-session collector

**Files:**
- Create: `app_core/xiaohongshu_data_collector.py`
- Modify: `app_core/xiaohongshu_data_contract.py`
- Modify: `app_core/platform_data_collectors.py`
- Test: `test_xiaohongshu_data_collector.py`

**Interfaces:**
- Produces: `XiaohongshuDataCollector.collect_direct(account: dict) -> CollectionBatch`, which raises a fallback-allowed neutral error without opening a browser.
- Produces: `XiaohongshuDataCollector.collect_browser_signed(account: dict) -> CollectionBatch`.
- Updates: registers type `1` with a lazy Xiaohongshu factory only after this collector exists, making `registered_platform_types()` return `(1, 3)`.
- Constructor dependencies: `browser_factory`, `utc_now`, `monotonic`, with production defaults and fake injection for tests.
- Consumes exact paths:
  - `/api/galaxy/v2/creator/datacenter/account/base`
  - `/api/galaxy/creator/home/personal_info`
  - `/api/galaxy/creator/datacenter/note/analyze/list`
  - `/api/galaxy/creator/datacenter/note/base`

- [ ] **Step 1: Write failing lifecycle tests**

Create fakes that emit the four responses and record navigation/cleanup. Tests must prove:

```python
def test_browser_signed_collects_one_standard_batch_and_closes_all_resources(self):
    collector, fake = collector_with_responses(reviewed_success_responses())
    batch = collector.collect_browser_signed(xhs_account())
    self.assertEqual(batch.platform_type, 1)
    self.assertEqual(batch.source_mode, "browser_signed")
    self.assertTrue(batch.account_metrics_available)
    self.assertTrue(batch.content_data_available)
    self.assertEqual(len(batch.contents), 1)
    self.assertEqual(fake.goto_urls, [CREATOR_HOME, DATA_ANALYSIS_URL, expected_detail_url])
    self.assertEqual(fake.cleanup, ["page", "context", "browser", "playwright"])

def test_cleanup_failure_never_returns_success(self):
    collector, fake = collector_with_responses(reviewed_success_responses(), context_close_error=RuntimeError("private"))
    with self.assertRaises(PlatformDataCollectionError) as caught:
        collector.collect_browser_signed(xhs_account())
    self.assertEqual(caught.exception.error_code, "browser_cleanup_incomplete")
    self.assertIsNone(caught.exception.__cause__)
    self.assertIn("browser", fake.cleanup)
    self.assertIn("playwright", fake.cleanup)
```

Add tests for missing state file, login redirect, verification page, response over 1 MiB, cumulative response bytes over 4 MiB, total timeout, late response phase binding, duplicate response, partial content failure and `KeyboardInterrupt` / `SystemExit` cleanup propagation.

- [ ] **Step 2: Run only lifecycle tests and verify RED**

```bash
.venv/bin/python -m unittest -v \
  test_xiaohongshu_data_collector.XiaohongshuDataCollectorTests.test_browser_signed_collects_one_standard_batch_and_closes_all_resources \
  test_xiaohongshu_data_collector.XiaohongshuDataCollectorTests.test_cleanup_failure_never_returns_success
```

Expected: fail because `XiaohongshuDataCollector` is absent.

- [ ] **Step 3: Implement the bounded collector**

Reuse the proven verifier limits: 60-second total run, reserve up to 5 seconds for cleanup, 1 MiB per response, 4 MiB cumulative, maximum 50 list items, exact creator host, and request-object identity for response-phase binding. Load only `COOKIE_DIR / Path(account["filePath"]).name`; reject missing, symlinked or non-file state as `session_state_missing`.

Cache each bounded response body while its page is alive, parse after the required phases finish, and keep response values only in function-local memory. In `finally`, attempt page/context/browser/playwright cleanup independently. If cleanup is incomplete, override ordinary collection success/failure with `browser_cleanup_incomplete`; then propagate `KeyboardInterrupt` or `SystemExit` only after cleanup.

- [ ] **Step 4: Run lifecycle tests, then the full new collector module**

```bash
.venv/bin/python -m unittest -v \
  test_xiaohongshu_data_collector.XiaohongshuDataCollectorTests.test_browser_signed_collects_one_standard_batch_and_closes_all_resources \
  test_xiaohongshu_data_collector.XiaohongshuDataCollectorTests.test_cleanup_failure_never_returns_success
.venv/bin/python -m unittest -v test_xiaohongshu_data_collector
```

Expected: all finish `OK`; no test opens a real browser or reads a real account.

- [ ] **Step 5: Commit**

```bash
git add app_core/xiaohongshu_data_collector.py app_core/xiaohongshu_data_contract.py app_core/platform_data_collectors.py test_xiaohongshu_data_collector.py
git commit -m "接入小红书只读数据会话"
```

---

### Task 5: Synchronization, persistence and platform-neutral UI feedback

**Files:**
- Modify: `app_core/platform_data_sync.py`
- Modify: `app_core/platform_data_service.py`
- Modify: `ui/data_monitor_page.py`
- Modify: `test_platform_data_sync.py`
- Modify: `test_platform_data_service.py`
- Modify: `test_data_monitor_page.py`

**Interfaces:**
- Consumes: platform-neutral `PlatformDataCollectionError` and registered collectors.
- Preserves: `sync_account_data(account_id: int, report: Callable[[dict], None] | None = None) -> dict`.
- Produces platform-neutral progress stage copy; no stage mentions Douyin for Xiaohongshu.
- Produces UI content metadata caption `平台未提供标题与发布时间` when empty optional metadata is displayed.

- [ ] **Step 1: Write failing real-chain tests**

```python
def test_xhs_sync_routes_collector_persists_and_returns_public_result(self):
    batch = xhs_collection_batch()
    collector = Mock()
    collector.collect_direct.side_effect = PlatformDataCollectionError("direct_request_rejected", fallback_allowed=True)
    collector.collect_browser_signed.return_value = batch
    with patch("app_core.platform_data_sync.account_service.list_accounts", return_value=[xhs_account()]), \
         patch("app_core.platform_data_sync.collector_for_platform", return_value=collector):
        result = platform_data_sync.sync_account_data(21)
    self.assertEqual(result["status"], "success")
    self.assertEqual(result["sourceMode"], "browser_signed")
    self.assertEqual(result["contentCount"], 1)

def test_xhs_progress_and_ui_never_claim_douyin(self):
    progress = []
    # invoke the same chain with the Xiaohongshu account
    self.assertNotIn("抖音", " ".join(item["message"] for item in progress))
```

Add an SQLite integration assertion that account `21` writes platform type `1`, account `12` remains untouched, and rerunning account `21` updates its own latest snapshot atomically. Add failure tests for login, verification, payload invalid and cleanup incomplete; UI must display fixed Chinese copy and never exception text.

- [ ] **Step 2: Run the new chain tests and verify RED**

```bash
.venv/bin/python -m unittest -v \
  test_platform_data_sync.PlatformDataSyncTests.test_xhs_sync_routes_collector_persists_and_returns_public_result \
  test_platform_data_sync.PlatformDataSyncTests.test_xhs_progress_and_ui_never_claim_douyin
```

Expected: the sync layer only catches `DouyinDataCollectionError` or emits Douyin-specific progress.

- [ ] **Step 3: Generalize the orchestration boundary**

Catch `PlatformDataCollectionError`, retain direct-to-browser fallback semantics, and map only `platform_data_service.ALLOWED_ERROR_CODES`. Change `browser_signed` progress to `正在读取平台官方数据…`. Add `browser_cleanup_incomplete`, `session_state_missing` and `content_list_truncated` to the existing public code whitelist only when each has a fixed UI message and persistence test.

Do not special-case Xiaohongshu in `record_collection_sync`; persistence remains keyed by strict `accountId` and the batch's strict `platform_type`.

- [ ] **Step 4: Render platform-specific missing-data copy without changing metrics**

In `DataMonitorPage`, derive the selected platform only from `platform_combo.currentData()`. When Xiaohongshu contents contain empty optional metadata, the table renders `—` and the page displays `平台未提供标题与发布时间`; do not rewrite DB rows or replace missing values with zero. Login errors show `需要重新登录小红书` and expose the existing account-management action.

- [ ] **Step 5: Run sync, persistence and UI affected modules**

```bash
.venv/bin/python -m unittest -v test_platform_data_sync test_platform_data_service
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest -v test_data_monitor_page test_main_window
```

Expected: all finish `OK`.

- [ ] **Step 6: Commit**

```bash
git add app_core/platform_data_sync.py app_core/platform_data_service.py ui/data_monitor_page.py test_platform_data_sync.py test_platform_data_service.py test_data_monitor_page.py
git commit -m "贯通小红书数据同步与界面回读"
```

---

### Task 6: Offline completion gate and one explicitly authorized real sync

**Files:**
- Modify: `docs/superpowers/reports/2026-08-20-xiaohongshu-data-contract-report.md`
- Create local ignored evidence under: `.superpowers/sdd/2026-08-21-multiplatform-data-monitoring/`

**Interfaces:**
- Consumes the production UI and collector from Tasks 1–5.
- Produces an offline verification report before any real action.
- Produces one real-run report only after Andy authorizes the exact account and run.

- [ ] **Step 1: Run static and affected verification**

```bash
.venv/bin/python -m py_compile \
  app_core/platform_data_collection_errors.py \
  app_core/platform_data_collectors.py \
  app_core/platform_data_models.py \
  app_core/xiaohongshu_data_contract.py \
  app_core/xiaohongshu_data_collector.py \
  app_core/platform_data_sync.py \
  ui/data_monitor_page.py
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest -v \
  test_xiaohongshu_data_collector \
  test_douyin_data_collector \
  test_platform_data_sync \
  test_platform_data_service \
  test_data_monitor_page \
  test_main_window
git diff --check
```

Expected: every command exits `0` and unittest ends `OK`.

- [ ] **Step 2: Perform the security scan**

Use `rg` and AST checks on changed production files to prove there is no `requests`, `fetch`, Cookie output, header/body persistence, clipboard copy, upload, publish, submit, edit or delete call in the Xiaohongshu data collector. Record exact commands and exit codes in the local report.

- [ ] **Step 3: Run the full suite once after the last production change**

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -v
```

Expected: one fresh terminal summary with `OK` and exit code `0`. If it fails, stop at the first product failure, repair with a named test, then run one new final full suite and mark the older evidence historical.

- [ ] **Step 4: Stop at the real-action authorization gate**

Present the exact Xiaohongshu subject name and explain that the next action opens one official read-only browser session. Do not proceed on old or general authorization; obtain Andy's authorization for this run.

- [ ] **Step 5: Execute one production-path read-only synchronization**

After authorization, launch the source client from current `main`, select `小红书` then `硅基探索`, and click `同步数据` once. Do not retry automatically. Capture only controlled status, counts, fixed error code and cleanup result; do not capture Cookie or raw response values in the report.

- [ ] **Step 6: Verify local readback against the official visible page**

Confirm all of the following:

- platform selector is `小红书`;
- subject selector is `硅基探索`;
- local account ID and platform type are strict positive integers `accountId` and `1`;
- follower total and at least one content metric equal the official page values visible in the already logged-in session;
- missing metrics display `—`;
- latest sync belongs to the Xiaohongshu account and does not modify Douyin account `3199`;
- cleanup is `closed=True` and built-in integer `aliveResourceCount=0`.

If any item fails, report the exact fixed code and leave previous local snapshots untouched.

- [ ] **Step 7: Update report and commit**

```bash
git add docs/superpowers/reports/2026-08-20-xiaohongshu-data-contract-report.md
git commit -m "验收小红书数据监测正式链路"
```

The report must distinguish offline delivery, real client run and business usefulness. A successful one-time synchronization proves the runtime path, not that the data has already improved content decisions.
