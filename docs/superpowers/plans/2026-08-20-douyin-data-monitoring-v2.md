# 一键发抖音数据监测 V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将当前语义不明的抖音账号单点数据页，升级为可区分账号/作品、日增量/累计值、昨日/7 天/30 天口径的可追溯数据监测页。

**Architecture:** 保留现有“会话直连优先、官方签名响应兜底”的只读采集路线，先在公开模型中锁定指标对象、口径和时间区间，再分别归一化账号日趋势和作品累计值。同步结果在单一 SQLite 事务中写入运行、指标和作品表；查询服务只输出白名单业务字段，Qt 页面只做本地的 1/7/30 天切换与展示。

**Tech Stack:** Python 3.11+ standard library, SQLite, requests, Playwright async API, PyQt6, unittest.

**Spec:** `docs/superpowers/specs/2026-08-20-douyin-data-monitoring-v2-design.md`

## Global Constraints

- 只读取用户本人已在一键发登录的抖音账号数据。
- 继续使用“官方会话直连优先，官方页面签名响应兜底”，不重放 fetch，不自行组合签名。
- 不采集评论正文、私信、粉丝明细，不保存 Cookie、完整平台响应或原始异常。
- 所有数值必须带对象、时间区间、日增量/累计口径、观察时间和数据来源；无法证明口径的数值不入公开模型。
- 平台未返回的数据显示“暂未取得”，不补 0、不推算、不使用第三方估算。
- 作品只使用平台稳定 ID 定位；无稳定 ID 不落库，不用标题或封面猜测。
- 作品列表单次同步最多 20 页、500 个作品；达到上限时明确标记 `content_list_truncated`。
- 时间范围只允许 1、7、30 天，默认 7 天；切换时只查本地数据，不重新访问平台。
- 同一账号只允许一个同步任务；客户端关闭仍使用 5 秒收束屏障。
- 测试顺序为命名单项 RED/GREEN、受影响模块、收尾时唯一一次全量；发现首个失败即停止并回到单项修复。

---

## File Structure

- `app_core/platform_data_models.py`: 维护指标、作品和采集批次的不可变公开契约。
- `app_core/database.py`: 只负责 SQLite 表、列、索引与兼容迁移。
- `app_core/douyin_data_collector.py`: 只负责抖音官方响应的请求、白名单和归一化。
- `app_core/platform_data_service.py`: 只负责原子写入与本地白名单查询。
- `app_core/platform_data_sync.py`: 只负责同步阶段、直连/兜底编排和公开结果。
- `ui/data_monitor_page.py`: 只负责 Qt 展示、本地时间范围切换和同步任务生命周期。
- `test_platform_data_service.py`: 模型、迁移、事务和查询契约。
- `test_douyin_data_collector.py`: 账号趋势、作品分页、白名单与脱敏契约。
- `test_platform_data_sync.py`: 成功、部分成功、失败和进度阶段契约。
- `test_data_monitor_page.py`: 页面口径、趋势、作品表、去重与关闭屏障。

### Task 1: 扩展公开模型与数据库迁移

**Files:**
- Modify: `app_core/platform_data_models.py`
- Modify: `app_core/database.py`
- Modify: `test_platform_data_service.py`

**Interfaces:**
- Produces: `MetricPoint` with required `metric_scope: str`, `period_start: str`, and `period_end: str` fields in addition to the existing fields.
- Produces: `ContentRecord(content_id: str, title: str, cover_url: str, published_at: str, content_status: str, content_type: str)`
- Produces: `CollectionBatch` with `platform_type: int`, `source_mode: str`, `metrics: tuple[MetricPoint, ...]`, `contents: tuple[ContentRecord, ...]`, `account_metrics_available: bool`, `content_data_available: bool`, `platform_observed_at: str`, and `warning_code: str = ""`.
- Produces: migrated `platform_metric_snapshots` and new `platform_contents` schema.

- [ ] **Step 1: Write failing model and migration tests**

Add tests that construct one account daily point and one content lifetime point, reject ambiguous scopes/dates, reject duplicate `(entity, metric, scope, period)` identities, and run `database.ensure_schema()` twice against both a fresh database and a legacy database. Assert legacy rows keep empty scope/range and cannot masquerade as V2 data.

```python
daily = MetricPoint(
    entity_type="account",
    entity_key=f"account:{self.account_id}",
    metric_key="views",
    raw_metric_key="play_cnt",
    metric_value=125,
    metric_unit="count",
    metric_scope="daily_increment",
    period_start="2026-08-19",
    period_end="2026-08-19",
    observed_at="2026-08-20T12:00:00+08:00",
)
content = ContentRecord(
    content_id="aweme-1",
    title="作品一",
    cover_url="https://creator.douyin.com/cover/1.jpg",
    published_at="2026-08-19T10:00:00+08:00",
    content_status="published",
    content_type="video",
)
self.assertEqual(daily.metric_scope, "daily_increment")
self.assertEqual(content.content_id, "aweme-1")
```

- [ ] **Step 2: Run the named tests and verify RED**

Run:

```bash
.venv/bin/python -m unittest -v \
  test_platform_data_service.PlatformDataServiceTests.test_v2_models_require_explicit_scope_and_period \
  test_platform_data_service.PlatformDataServiceTests.test_schema_migrates_legacy_metrics_without_inventing_scope \
  test_platform_data_service.PlatformDataServiceTests.test_platform_contents_schema_is_idempotent
```

Expected: FAIL because the new fields, model and table do not exist.

- [ ] **Step 3: Implement strict public models**

Add exact allowlists and strict date parsing. Preserve built-in numeric validation and reject booleans.

```python
ALLOWED_ENTITY_TYPES = frozenset({"account", "content"})
ALLOWED_METRIC_SCOPES = frozenset({"daily_increment", "lifetime_total"})
ALLOWED_CONTENT_STATUSES = frozenset({"published", "scheduled", "private", "unavailable"})
ALLOWED_CONTENT_TYPES = frozenset({"video", "image"})

@dataclass(frozen=True, slots=True)
class ContentRecord:
    content_id: str
    title: str
    cover_url: str
    published_at: str
    content_status: str
    content_type: str
```

Extend `ALLOWED_METRIC_KEYS` with `favorites` and `downloads`; those keys are emitted only when the official payload contains explicit numeric fields. Define date-only validation with `datetime.strptime(value, "%Y-%m-%d")`, require `period_start <= period_end`, require account points to use `daily_increment` or proven follower totals, and require content points to use `lifetime_total`.

- [ ] **Step 4: Add idempotent schema migration**

Use `PRAGMA table_info(platform_metric_snapshots)` to add `metricScope`, `periodStart`, and `periodEnd` as `TEXT NOT NULL DEFAULT ''` only when absent. Create `platform_contents` with `UNIQUE(accountId, platformType, contentId)`, account/publish-time index, and account foreign key. Add a V2 metric query index over account/entity/scope/period.

- [ ] **Step 5: Run Task 1 tests and verify GREEN**

Run the three named tests from Step 2, then:

```bash
.venv/bin/python -m unittest -v test_platform_data_service.PlatformDataServiceTests
```

Expected: all Task 1 and existing service tests PASS.

- [ ] **Step 6: Commit Task 1**

```bash
git add app_core/platform_data_models.py app_core/database.py test_platform_data_service.py
git commit -m "扩展平台数据语义与存储模型"
```

### Task 2: 归一化抖音账号日趋势

**Files:**
- Modify: `app_core/douyin_data_collector.py`
- Modify: `test_douyin_data_collector.py`

**Interfaces:**
- Consumes: Task 1 `MetricPoint` and `CollectionBatch`.
- Produces: `_parse_daily_metric(metric: Mapping, account_id: int) -> tuple[MetricPoint, ...]`.
- Produces: direct and browser-signed account batches containing every valid natural-day point.

- [ ] **Step 1: Write failing 30-day normalization tests**

Create a direct response with 30 ordered `trends` entries and assert all 30 survive with `daily_increment`, exact Beijing dates, and unique identities. Add separate cases for invalid dates, duplicate dates, booleans and numeric strings; each must raise fixed `metric_payload_invalid` without a cause.

```python
points = [point for point in batch.metrics if point.metric_key == "views"]
self.assertEqual(len(points), 30)
self.assertEqual(points[0].period_start, "2026-07-22")
self.assertEqual(points[-1].period_end, "2026-08-20")
self.assertTrue(all(point.metric_scope == "daily_increment" for point in points))
```

Add browser `option_list` coverage. Only accept `current_count` as `followers_total/lifetime_total` when the raw key and response structure prove that it is a current follower total.

- [ ] **Step 2: Run named account-trend tests and verify RED**

```bash
.venv/bin/python -m unittest -v \
  test_douyin_data_collector.DouyinDirectCollectorTests.test_all_daily_trend_points_are_preserved \
  test_douyin_data_collector.DouyinDirectCollectorTests.test_daily_trend_rejects_invalid_or_duplicate_dates \
  test_douyin_data_collector.DouyinBrowserCollectorTests.test_browser_option_list_preserves_daily_scope_and_proven_total
```

Expected: FAIL because the collector still reads only the final point.

- [ ] **Step 3: Replace final-point parsing with strict daily parsing**

Implement a helper that snapshots the untrusted list once, validates every mapping, parses `%Y%m%d` or explicitly allowlisted browser date formats into Beijing date strings, and emits one point per date. Use a per-`metric_key/date/scope` identity set; a duplicate is invalid rather than last-write-wins.

```python
def _daily_point_identity(point: MetricPoint) -> tuple[str, str, str]:
    return point.metric_key, point.metric_scope, point.period_start
```

Do not fall back to `datetime.now()` for missing dates. A point without a provable natural day must fail closed.

- [ ] **Step 4: Update both collection paths**

Make `_parse_payload()` and `_parse_current_overview()` build V2 batches with `account_metrics_available=True`. Preserve direct/browser source modes and existing cookie-domain isolation. Ensure `platform_observed_at` is a controlled local observation timestamp and each metric retains its platform date.

- [ ] **Step 5: Run Task 2 tests and collector regression**

```bash
.venv/bin/python -m unittest -v test_douyin_data_collector
```

Expected: all collector tests PASS; prior tests are updated to assert explicit scope/date rather than only `(key, value)`.

- [ ] **Step 6: Commit Task 2**

```bash
git add app_core/douyin_data_collector.py test_douyin_data_collector.py
git commit -m "保留抖音账号每日趋势"
```

### Task 3: 采集作品列表与作品累计指标

**Files:**
- Modify: `app_core/douyin_data_collector.py`
- Modify: `test_douyin_data_collector.py`

**Interfaces:**
- Consumes: Task 1 `ContentRecord`, `MetricPoint`, `CollectionBatch`.
- Produces: `_collect_direct_contents(session: object, account_id: int) -> tuple[tuple[ContentRecord, ...], tuple[MetricPoint, ...], str]` where the third item is `""` or `content_list_truncated`.
- Produces: allowlisted browser response normalization for the same content contract.

- [ ] **Step 1: Write failing pagination and identity tests**

Extend `FakeSession` to route responses by official URL and cursor. Test two pages, stable content IDs, metadata, and cumulative metrics. Assert missing IDs are skipped, duplicate IDs are rejected, unsupported cover hosts are blanked, and missing numeric fields remain absent.

```python
self.assertEqual([item.content_id for item in batch.contents], ["aweme-2", "aweme-1"])
self.assertEqual(
    {(point.entity_key, point.metric_key, point.metric_scope) for point in batch.metrics if point.entity_type == "content"},
    {("aweme-1", "views", "lifetime_total"), ("aweme-1", "likes", "lifetime_total")},
)
```

Add a 20-page/500-item boundary test that expects `warning_code == "content_list_truncated"` and never issues page 21.

- [ ] **Step 2: Write failing browser allowlist tests**

Feed one official content-list response and one lookalike/unapproved path into the fake browser. Assert only the exact official host and normalized allowlisted path are accepted; query parameters never widen the allowlist.

- [ ] **Step 3: Run Task 3 named tests and verify RED**

```bash
.venv/bin/python -m unittest -v \
  test_douyin_data_collector.DouyinDirectCollectorTests.test_content_pages_use_stable_ids_and_lifetime_metrics \
  test_douyin_data_collector.DouyinDirectCollectorTests.test_content_pagination_stops_at_twenty_pages_or_five_hundred_items \
  test_douyin_data_collector.DouyinBrowserCollectorTests.test_content_response_requires_exact_official_allowlist
```

Expected: FAIL because content collection is absent.

- [ ] **Step 4: Add exact content endpoint constants and parser**

Add only endpoints observed and verified on the official creator page to separate direct and browser allowlists. Normalize the response into `ContentRecord` and content `MetricPoint` objects. Sort metadata by `published_at` descending after deduplication; never use title, cover or timestamp as an identity.

The direct pagination loop must obey both hard limits:

```python
while has_more and page_count < 20 and len(contents) < 500:
    payload = _request_content_page(session, cursor)
    page_count += 1
    cursor, has_more = _normalize_cursor(payload)
```

If the verified runtime has not yet revealed an official content endpoint, define no speculative endpoint: return account metrics with `content_data_available=False` and fixed `content_list_unavailable`.

- [ ] **Step 5: Integrate direct and signed batches**

Merge account metrics, content metadata and content metrics into one immutable batch. A content failure must not erase a complete account batch; set `content_data_available=False` and `warning_code="content_list_unavailable"`. A malformed content payload uses `content_payload_invalid`. No raw response or URL enters the exception.

- [ ] **Step 6: Run Task 3 tests and collector regression**

```bash
.venv/bin/python -m unittest -v test_douyin_data_collector
```

Expected: all direct, browser, pagination, allowlist and legacy security tests PASS.

- [ ] **Step 7: Commit Task 3**

```bash
git add app_core/douyin_data_collector.py test_douyin_data_collector.py
git commit -m "采集抖音作品与累计指标"
```

### Task 4: 原子持久化与 1/7/30 天白名单查询

**Files:**
- Modify: `app_core/platform_data_service.py`
- Modify: `test_platform_data_service.py`

**Interfaces:**
- Consumes: Task 1 V2 batch models and schema.
- Produces: `record_collection_sync(account_id: int, batch: CollectionBatch) -> dict`.
- Produces: `account_period_summary(account_id: int, days: int) -> dict`.
- Produces: `account_daily_trends(account_id: int, days: int) -> dict`.
- Produces: `account_contents(account_id: int, limit: int = 50, offset: int = 0) -> dict`.

- [ ] **Step 1: Write failing atomic persistence tests**

Persist a batch containing account days, one content row and content totals. Assert one run owns all snapshots and the content row. Inject failures at the second metric, content upsert and run finalization; after each failure assert runs/snapshots/contents remain unchanged.

```python
with database.connect() as conn:
    counts = tuple(
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("platform_data_sync_runs", "platform_metric_snapshots", "platform_contents")
    )
self.assertEqual(counts, (0, 0, 0))
```

- [ ] **Step 2: Write failing period-query tests**

Seed sparse daily points across 30 days and follower lifetime totals. Assert 1/7/30 summaries use Beijing natural-day boundaries, sum only `daily_increment`, choose the final available `lifetime_total`, and return per-metric `complete/partial/missing`. Assert legacy blank-scope rows are excluded.

```python
summary = platform_data_service.account_period_summary(self.account_id, 7)
self.assertEqual(summary["days"], 7)
self.assertEqual(summary["metrics"]["views"]["scope"], "daily_increment")
self.assertEqual(summary["metrics"]["views"]["availability"], "partial")
self.assertNotIn("rawMetricKey", repr(summary))
```

Reject `True`, strings, floats, 0, 2 and 31 as `days`; reject invalid `limit/offset` with fixed `metric_payload_invalid`.

- [ ] **Step 3: Run Task 4 named tests and verify RED**

```bash
.venv/bin/python -m unittest -v \
  test_platform_data_service.PlatformDataServiceTests.test_v2_batch_persists_atomically \
  test_platform_data_service.PlatformDataServiceTests.test_period_summary_distinguishes_complete_partial_and_missing \
  test_platform_data_service.PlatformDataServiceTests.test_content_query_returns_latest_totals_without_internal_fields
```

Expected: FAIL because V2 persistence and queries do not exist.

- [ ] **Step 4: Implement one-transaction persistence**

Insert the run initially as a controlled in-progress state, insert all scoped metrics, upsert content metadata, then finalize the same run as `success` or `partial_success` inside one `with database.connect()` block. Use the batch flags and warning code; do not infer availability from empty tuples.

- [ ] **Step 5: Implement strict local queries**

Create small private helpers for built-in integers, Beijing date ranges and public numeric conversion. `account_period_summary` returns:

```python
{
    "accountId": account,
    "days": days,
    "periodStart": start,
    "periodEnd": end,
    "metrics": {
        "views": {
            "value": 125,
            "scope": "daily_increment",
            "unit": "count",
            "availability": "partial",
            "observedDays": 5,
        }
    },
    "latestRun": public_run,
    "platformObservedAt": observed_at,
    "localSyncedAt": finished_at,
}
```

`account_daily_trends` returns dates only when stored; `account_contents` returns metadata plus latest `lifetime_total` values, ordered by publish time, with `total/limit/offset/items`.

- [ ] **Step 6: Run Task 4 tests and service regression**

```bash
.venv/bin/python -m unittest -v test_platform_data_service
```

Expected: all service tests PASS; public payload scans find no cookie, session, URL query, raw response, SQL text or local path.

- [ ] **Step 7: Commit Task 4**

```bash
git add app_core/platform_data_service.py test_platform_data_service.py
git commit -m "原子保存并查询抖音数据"
```

### Task 5: 编排部分成功、进度与固定错误

**Files:**
- Modify: `app_core/platform_data_sync.py`
- Modify: `app_core/platform_data_service.py`
- Modify: `test_platform_data_sync.py`

**Interfaces:**
- Consumes: Task 4 `record_collection_sync`.
- Produces: `sync_account_data(account_id: int, report: Callable[[dict], None] | None = None) -> dict` returning only accountId/status/sourceMode/errorCode/metricCount/contentCount.
- Produces fixed progress stages: `direct_session`, `browser_signed`, `account_metrics`, `content_list`, `content_metrics`, `persisting`, `completed`, `partial`, `failed`.

- [ ] **Step 1: Write failing partial-success and progress tests**

Create collectors that return full, account-only partial, and total failure batches. Assert partial data is persisted as `partial_success`, emits `partial`, and never displays or records success. Assert a content truncation warning survives as a fixed code. Assert callback exceptions are swallowed and `KeyboardInterrupt/SystemExit` retain existing behavior.

```python
result = platform_data_sync.sync_account_data(self.account_id, report=events.append)
self.assertEqual(result["status"], "partial_success")
self.assertEqual(result["errorCode"], "content_list_unavailable")
self.assertEqual(events[-1]["stage"], "partial")
```

- [ ] **Step 2: Write failing error allowlist tests**

Test all four new public codes and an attacker-controlled exception/code. The attacker value must map to `metric_payload_invalid`, have no cause, and never enter the database result or progress messages.

- [ ] **Step 3: Run Task 5 tests and verify RED**

```bash
.venv/bin/python -m unittest -v test_platform_data_sync
```

Expected: FAIL on V2 result shape, partial status and stages.

- [ ] **Step 4: Implement V2 orchestration**

Call the collector once per account, emit only fixed stage/message pairs, and pass the immutable batch to `record_collection_sync`. Preserve fallback only for explicitly fallback-allowed direct errors. Update the service status/error allowlists with `partial_success` and the four new codes.

- [ ] **Step 5: Run Task 5 tests and affected backend modules**

```bash
.venv/bin/python -m unittest -v \
  test_platform_data_sync \
  test_platform_data_service \
  test_douyin_data_collector
```

Expected: all affected backend tests PASS.

- [ ] **Step 6: Commit Task 5**

```bash
git add app_core/platform_data_sync.py app_core/platform_data_service.py test_platform_data_sync.py
git commit -m "编排抖音数据部分成功状态"
```

### Task 6: 升级数据监测页并完成最终验收

**Files:**
- Modify: `ui/data_monitor_page.py`
- Modify: `test_data_monitor_page.py`
- Modify if shutdown contract changes: `ui/main_window.py`
- Modify if shutdown contract changes: `test_main_window.py`
- Modify: `docs/superpowers/specs/2026-08-20-douyin-data-monitoring-v2-design.md` only to record verified endpoint/acceptance facts, without weakening requirements.

**Interfaces:**
- Consumes: Task 4 summary/trend/content queries and Task 5 sync result/stages.
- Produces: `DataMonitorPage` with account selector, 1/7/30 range selector, semantic cards, local trend chart, paged content table, partial-success status and existing shutdown barrier.

- [ ] **Step 1: Write failing semantic-card and local-range tests**

Patch the three query methods and build a real offscreen `DataMonitorPage`. Assert default 7 days, explicit labels such as `账号区间新增播放`, complete/partial/missing captions, source and both timestamps. Change 7 to 30 and assert only local query mocks are called; `sync_account_data` remains untouched.

```python
self.assertEqual(page.range_combo.currentData(), 7)
self.assertIn("账号区间新增播放", page.metric_titles["views"].text())
page.range_combo.setCurrentIndex(page.range_combo.findData(30))
sync_mock.assert_not_called()
period_mock.assert_called_with(12, 30)
```

- [ ] **Step 2: Write failing trend and content-table tests**

Provide sparse trend dates and two content rows. Assert the custom Qt trend widget preserves a gap rather than inserting zero, the table orders by published time, missing metrics show `—`, and page navigation changes only `limit/offset` local queries.

The trend widget exposes a deterministic read-only testing projection:

```python
self.assertEqual(
    page.trend_chart.series_for_test("views"),
    (("2026-08-18", 10), ("2026-08-20", 12)),
)
self.assertNotIn(("2026-08-19", 0), page.trend_chart.series_for_test("views"))
```

- [ ] **Step 3: Write failing status, dedupe and shutdown tests**

Assert partial success text says account trends updated/content unavailable, fixed errors route login when required, repeated clicks enqueue one task, progress uses only fixed stage text, and a worker that cannot finish within the existing 5-second deadline makes `shutdown()` return `False`.

- [ ] **Step 4: Run Task 6 named tests and verify RED**

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest -v \
  test_data_monitor_page.DataMonitorPageTests.test_v2_cards_explain_scope_range_and_freshness \
  test_data_monitor_page.DataMonitorPageTests.test_range_switch_queries_local_data_without_sync \
  test_data_monitor_page.DataMonitorPageTests.test_sparse_trend_and_content_table_preserve_missing_values \
  test_data_monitor_page.DataMonitorPageTests.test_partial_success_is_not_rendered_as_complete
```

Expected: FAIL because the V2 controls and projections do not exist.

- [ ] **Step 5: Implement focused Qt presentation components**

Keep `DataMonitorPage` as orchestrator and add small private widgets in the same module:

```python
class _TrendChart(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._series: dict[str, tuple[tuple[str, int | float], ...]] = {}

    def set_series(self, series: dict[str, tuple[tuple[str, int | float], ...]]) -> None:
        self._series = {key: tuple(points) for key, points in series.items()}
        self.update()

    def series_for_test(self, key: str) -> tuple[tuple[str, int | float], ...]:
        return self._series.get(key, ())

class _ContentTable(QTableWidget):
    def set_payload(self, payload: dict) -> None:
        items = payload.get("items") if type(payload) is dict else None
        rows = items if type(items) is list else []
        self.setRowCount(len(rows))
        for row_index, item in enumerate(rows):
            if type(item) is not dict:
                continue
            self.setItem(row_index, 0, QTableWidgetItem(str(item.get("title") or "—")))
```

Implement the chart with `QPainter`; derive x positions from real dates and start a new path after missing dates. Do not add web views or external chart packages. Add `range_combo`, metric title/status labels, freshness labels, trend selector, table and local pagination controls. Preserve the real `BackgroundTaskRunner` key `platform-data-sync:<accountId>`.

- [ ] **Step 6: Run Task 6 UI tests**

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest -v test_data_monitor_page
```

Expected: all DataMonitorPage tests PASS.

- [ ] **Step 7: Run affected-module verification once**

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest -v \
  test_platform_data_service \
  test_douyin_data_collector \
  test_platform_data_sync \
  test_data_monitor_page \
  test_main_window
```

Expected: all affected tests PASS. If the first failure appears, stop this command's investigation, reproduce that named test alone, fix it RED/GREEN, then rerun this affected set once.

- [ ] **Step 8: Run static checks**

```bash
.venv/bin/python -m py_compile \
  app_core/platform_data_models.py \
  app_core/database.py \
  app_core/douyin_data_collector.py \
  app_core/platform_data_service.py \
  app_core/platform_data_sync.py \
  ui/data_monitor_page.py
git diff --check
rg -n "Cookie|sessionid|responseBody|Authorization|Bearer " \
  app_core/platform_data_models.py \
  app_core/douyin_data_collector.py \
  app_core/platform_data_service.py \
  app_core/platform_data_sync.py \
  ui/data_monitor_page.py
```

Expected: compilation and diff checks exit 0. Any credential-pattern match must be an allowlisted test fixture or static header name; no value, raw response, path or query string may enter public output or persistence.

- [ ] **Step 9: Run the single final full suite**

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -v
```

Expected: all tests PASS with exit 0. Record the exact count and duration; do not rerun full discovery unless production code changes after this evidence.

- [ ] **Step 10: Perform real read-only acceptance**

Launch the source client, select the already logged-in Douyin account, click sync once, and verify:

1. Yesterday, 7-day and 30-day cards show explicit account/date/scope labels.
2. Yesterday's account playback matches the same date in the official creator overview.
3. At least one content row matches official title, publish time, play, like, comment and share totals.
4. Unavailable completion/traffic metrics remain `—` or `暂未取得`.
5. Closing the client leaves no Chromium or Playwright session.

If no allowlisted official content response is observed, record the acceptance as `账号趋势完成，作品数据未取得`; do not broaden the endpoint allowlist or claim full completion.

- [ ] **Step 11: Commit Task 6**

```bash
git add \
  ui/data_monitor_page.py \
  test_data_monitor_page.py \
  ui/main_window.py \
  test_main_window.py \
  docs/superpowers/specs/2026-08-20-douyin-data-monitoring-v2-design.md
git commit -m "升级抖音数据监测页"
```

Only add `ui/main_window.py` and `test_main_window.py` if the shutdown contract required a real change. Before committing, use `git diff --name-only` to avoid staging unrelated user files.
