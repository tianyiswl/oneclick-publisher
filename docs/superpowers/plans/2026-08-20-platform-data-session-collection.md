# 一键发平台数据会话采集 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 建立统一的平台指标快照与同步记录，并交付抖音 Cookie 直连优先、短时静默官方签名兜底的数据监测页面。

**Architecture:** 数据库和 UI 只接触白名单规范化指标；抖音采集器先用受限 requests.Session 复用 storage_state，业务状态拒绝时才启动独立无窗口 Chromium 捕获官方页面自身的数据响应。任何空信封、内层未登录、验证页或资源关闭不完整都按固定错误码失败，旧成功快照不被清空。

**Tech Stack:** Python 3、SQLite、requests 2.34、Playwright 1.60、PyQt6 6.11、unittest

**Spec:** docs/superpowers/specs/2026-08-20-platform-data-session-collection-design.md

## Global Constraints

- V1 只实现平台类型 3 的抖音账号级指标，不实现其他平台真实适配器。
- 只读用户本人已授权可见的数据，不发布、不修改、不删除平台内容。
- 不逆向签名、不执行页面内 fetch、不绕过验证码或风控。
- Cookie、请求头、完整响应、DOM、HTML、签名参数和账号身份不得进入数据库、日志或 UI 载荷。
- HTTP 200、外层成功和空数组均不能单独证明同步成功。
- 缺失指标保持缺失，UI 显示 —，不得补零。
- 浏览器兜底必须无窗口、单次、严格关闭。
- 保留已有未跟踪目录 .superpowers/brainstorm/ 与 outputs/，不得纳入提交。

---

### Task 1: 指标模型、数据库和原子快照服务

**Files:**
- Create: app_core/platform_data_models.py
- Create: app_core/platform_data_service.py
- Modify: app_core/database.py
- Create: test_platform_data_service.py

**Interfaces:**
- Produces: MetricPoint、CollectionBatch、CollectionFailure
- Produces: record_successful_sync(account_id, batch) -> dict
- Produces: record_failed_sync(account_id, platform_type, source_mode, error_code) -> dict
- Produces: account_data_summary(account_id) -> dict

- [ ] **Step 1: Write the failing model and persistence tests**

Add tests that construct only public fields and assert:

~~~python
point = MetricPoint(
    entity_type="account",
    entity_key="account:12",
    metric_key="views",
    raw_metric_key="play",
    metric_value=125,
    metric_unit="count",
    observed_at="2026-08-20T12:00:00+08:00",
)
batch = CollectionBatch(
    platform_type=3,
    source_mode="direct_session",
    metrics=(point,),
)
saved = platform_data_service.record_successful_sync(12, batch)
self.assertEqual(saved["status"], "success")
self.assertEqual(platform_data_service.account_data_summary(12)["metrics"]["views"], 125)
self.assertNotIn("likes", platform_data_service.account_data_summary(12)["metrics"])
~~~

Also assert a forced snapshot insert failure rolls back the run and metrics, a failed run leaves prior successful metrics visible, unsupported metric keys are rejected, and dataclass fields do not accept cookie/header/response payloads.

- [ ] **Step 2: Run the Task 1 tests and verify RED**

Run:

    python -m unittest -v test_platform_data_service

Expected: import failure because platform_data_models and platform_data_service do not exist.

- [ ] **Step 3: Add schema and immutable public models**

Create frozen dataclasses with exact validation:

~~~python
ALLOWED_METRIC_KEYS = frozenset({
    "views", "likes", "comments", "shares",
    "followers_total", "followers_net", "profile_visits",
})
ALLOWED_SOURCE_MODES = frozenset({"direct_session", "browser_signed"})

@dataclass(frozen=True, slots=True)
class MetricPoint:
    entity_type: str
    entity_key: str
    metric_key: str
    raw_metric_key: str
    metric_value: int | float
    metric_unit: str
    observed_at: str

@dataclass(frozen=True, slots=True)
class CollectionBatch:
    platform_type: int
    source_mode: str
    metrics: tuple[MetricPoint, ...]
~~~

Add platform_data_sync_runs and platform_metric_snapshots exactly as specified, including foreign keys, unique run/entity/metric index, and account/time indexes.

- [ ] **Step 4: Implement atomic service writes and public reads**

Use one database transaction for successful run plus all snapshots. Validate built-in numeric account/platform IDs, source mode, non-empty metrics and finite numeric values before opening the transaction. account_data_summary returns only:

~~~python
{
    "accountId": 12,
    "latestSuccessAt": "...",
    "latestRun": {
        "status": "failed",
        "sourceMode": "direct_session",
        "errorCode": "login_required",
        "finishedAt": "...",
    },
    "metrics": {"views": 125},
    "observedAt": {"views": "..."},
}
~~~

- [ ] **Step 5: Run Task 1 GREEN and commit**

Run:

    python -m unittest -v test_platform_data_service

Expected: all Task 1 tests PASS.

Commit:

    git add app_core/platform_data_models.py app_core/platform_data_service.py app_core/database.py test_platform_data_service.py
    git commit -m "增加平台数据指标快照底座"

---

### Task 2: 抖音受限直连会话采集器

**Files:**
- Create: app_core/platform_data_collectors.py
- Create: app_core/douyin_data_collector.py
- Create: test_douyin_data_collector.py

**Interfaces:**
- Consumes: CollectionBatch、MetricPoint
- Produces: PlatformDataCollector protocol
- Produces: collector_for_platform(platform_type, **dependencies) -> PlatformDataCollector
- Produces: DouyinDataCollector.collect_direct(account) -> CollectionBatch
- Produces: DouyinDataCollectionError(error_code, fallback_allowed)

- [ ] **Step 1: Write direct-session RED tests**

Use a temporary storage-state JSON and a fake requests.Session. Assert:

1. only cookies whose domain is douyin.com or a subdomain are installed;
2. endpoint and method are fixed by the collector, not accepted from account input;
3. HTTP 200 with inner status_code=8 raises login_required and writes nothing;
4. outer success plus empty metrics raises metric_payload_empty;
5. malformed metric values raise metric_payload_invalid;
6. valid platform metrics map only to the seven allowed standard keys;
7. unknown fields are ignored and never appear in CollectionBatch;
8. exception text containing Cookie, URL query, HTML or account path is not exposed.

Representative assertion:

~~~python
with self.assertRaises(DouyinDataCollectionError) as raised:
    collector.collect_direct(account)
self.assertEqual(raised.exception.error_code, "login_required")
self.assertTrue(raised.exception.fallback_allowed)
self.assertIsNone(raised.exception.__cause__)
~~~

- [ ] **Step 2: Run Task 2 tests and verify RED**

Run:

    python -m unittest -v test_douyin_data_collector

Expected: import failure because the collector modules do not exist.

- [ ] **Step 3: Implement the registry and endpoint allowlist**

Registry behavior:

~~~python
def collector_for_platform(platform_type: int, **dependencies):
    if type(platform_type) is not int:
        raise CollectionFailure("collector_not_available")
    if platform_type != 3:
        raise CollectionFailure("collector_not_available")
    return DouyinDataCollector(**dependencies)
~~~

The collector owns the endpoint definition. No public method accepts a URL, headers or arbitrary request body.

- [ ] **Step 4: Implement storage-state cookie loading and semantic parsing**

Load the account state through COOKIE_DIR plus basename only. Build the CookieJar without emitting a Cookie header in logs or subprocess arguments. Apply a 20-second request timeout.

Parser requirements:

- validate exact built-in int status codes;
- inspect nested metric statuses before reading values;
- accept only finite int/float values, excluding bool and numeric strings;
- use one observed timestamp for a batch;
- reject zero trustworthy metrics;
- convert all untrusted exceptions to fixed DouyinDataCollectionError from None.

- [ ] **Step 5: Run Task 2 GREEN and commit**

Run:

    python -m unittest -v test_douyin_data_collector

Expected: all Task 2 tests PASS.

Commit:

    git add app_core/platform_data_collectors.py app_core/douyin_data_collector.py test_douyin_data_collector.py
    git commit -m "增加抖音登录会话只读取数"

---

### Task 3: 短时静默官方签名兜底与同步编排

**Files:**
- Modify: app_core/douyin_data_collector.py
- Create: app_core/platform_data_sync.py
- Modify: test_douyin_data_collector.py
- Create: test_platform_data_sync.py

**Interfaces:**
- Consumes: DouyinDataCollector.collect_direct(account)
- Produces: DouyinDataCollector.collect_browser_signed(account, report) -> CollectionBatch
- Produces: sync_account_data(account_id, report=lambda event: None) -> dict

- [ ] **Step 1: Write browser fallback RED tests**

Create fake Playwright/browser/context/page/response objects that expose real lifecycle calls. Assert:

- direct success never starts Playwright;
- only fallback_allowed starts browser_signed;
- the page is opened only on the fixed creator data URL;
- only allowlisted response paths are parsed;
- no page.evaluate, page.click, page.fill or request.fetch method is called;
- login/verification pages raise login_required;
- timeout raises browser_signature_timeout;
- page, context, browser and Playwright close exactly once on success and failure;
- any cleanup failure overrides apparent data success with browser_cleanup_incomplete;
- progress events contain only stage and fixed text.

- [ ] **Step 2: Run the browser tests and verify RED**

Run:

    python -m unittest -v \
      test_douyin_data_collector.DouyinBrowserSignedCollectorTests \
      test_platform_data_sync

Expected: missing collect_browser_signed and sync_account_data failures.

- [ ] **Step 3: Implement browser-signed capture**

Use async_playwright from a synchronous adapter boundary via asyncio.run. Launch with existing fixed bundled Chromium launch helpers. Register response listeners before navigating. The listener may only accept exact creator.douyin.com host and approved data path prefixes.

Use an asyncio Future for the first semantically valid JSON response. Race it against page/login state and a 30-second deadline. Do not retain response objects after normalization.

- [ ] **Step 4: Implement sync orchestration**

sync_account_data must:

1. fetch a browser-auth account by numeric ID;
2. reject non-Douyin accounts as collector_not_available;
3. report direct_session;
4. fall back only when the direct error says fallback_allowed;
5. persist exactly one final success or failure run;
6. return only accountId, status, sourceMode, errorCode and metricCount;
7. preserve KeyboardInterrupt and SystemExit while still closing browser resources.

- [ ] **Step 5: Run Task 3 GREEN and commit**

Run:

    python -m unittest -v test_douyin_data_collector test_platform_data_sync

Expected: all Task 2 and Task 3 tests PASS.

Commit:

    git add app_core/douyin_data_collector.py app_core/platform_data_sync.py test_douyin_data_collector.py test_platform_data_sync.py
    git commit -m "接入抖音静默签名数据兜底"

---

### Task 4: 数据监测页面与主窗口路由

**Files:**
- Create: ui/data_monitor_page.py
- Modify: ui/main_window.py
- Modify: ui/common.py
- Create: test_data_monitor_page.py
- Modify: test_main_window.py

**Interfaces:**
- Consumes: account_service.list_accounts()
- Consumes: platform_data_service.account_data_summary(account_id)
- Consumes: platform_data_sync.sync_account_data(account_id, report)
- Produces: DataMonitorPage.refresh()
- Produces: DataMonitorPage.shutdown() -> bool

- [ ] **Step 1: Write UI RED tests**

Using QT_QPA_PLATFORM=offscreen and a controlled BackgroundTaskRunner pool, assert:

- “数据监测” is a real MainWindow page, not coming_soon;
- account combo contains only Douyin browser accounts;
- no account renders — rather than 0;
- clicking sync starts a background task and leaves the Qt event loop responsive;
- second click while the same account key is active is rejected;
- progress text contains no URL, Cookie, path or exception text;
- success refreshes cards and shows source mode;
- failure preserves prior metrics and shows the fixed error text;
- login_required exposes the existing account-management route;
- shutdown cancels queued work, waits at most five seconds for running work and returns False if it cannot prove completion.

- [ ] **Step 2: Run UI tests and verify RED**

Run:

    QT_QPA_PLATFORM=offscreen python -m unittest -v \
      test_data_monitor_page \
      test_main_window

Expected: DataMonitorPage import or route assertions fail.

- [ ] **Step 3: Implement DataMonitorPage**

Build a native page with:

- account QComboBox;
- “立即同步” secondary/primary button;
- status and last-success labels;
- seven metric cards using a fixed Chinese label map;
- a QTableWidget for observed times;
- fixed error text map;
- BackgroundTaskRunner key platform-data-sync:{accountId}.

Store no response payload on widgets. On success and failure, re-read platform_data_service rather than trusting the worker payload for metric values.

- [ ] **Step 4: Wire navigation and shutdown**

Move 数据监测 from coming_soon_definitions into page_definitions. Add it to the main stack and refresh routing. In MainWindow.closeEvent call data_monitor.shutdown before global browser cleanup; if it returns False, ignore the close event and stop further cleanup.

- [ ] **Step 5: Run Task 4 GREEN and commit**

Run:

    QT_QPA_PLATFORM=offscreen python -m unittest -v \
      test_data_monitor_page \
      test_main_window

Expected: all Task 4 tests PASS.

Commit:

    git add ui/data_monitor_page.py ui/main_window.py ui/common.py test_data_monitor_page.py test_main_window.py
    git commit -m "增加平台数据监测页面"

---

### Task 5: 受影响回归、真实只读验收和交付说明

**Files:**
- Modify: README.md
- Create: docs/PLATFORM_DATA_COLLECTION.md
- Modify tests only if a real regression exposes an obsolete exact expectation

**Interfaces:**
- Consumes all Task 1-4 interfaces
- Produces operator-facing documentation and final verification evidence

- [ ] **Step 1: Document the user-visible and security contract**

Document:

- supported platform: Douyin only;
- missing value versus zero;
- direct_session and browser_signed meanings;
- login_required recovery;
- local-only storage;
- no write actions;
- exact location of data monitor in navigation;
- offline tests do not prove live platform data.

- [ ] **Step 2: Run the affected module suite once**

Run:

    QT_QPA_PLATFORM=offscreen python -m unittest -v \
      test_platform_data_service \
      test_douyin_data_collector \
      test_platform_data_sync \
      test_data_monitor_page \
      test_main_window \
      test_oneclick_authorization \
      test_account_detection_ui

Expected: all affected tests PASS. If the first failure appears, stop this run, fix that failure with a focused RED/GREEN test, then rerun this affected suite once.

- [ ] **Step 3: Run one final full suite**

Run:

    QT_QPA_PLATFORM=offscreen python -m unittest discover -v

Expected: all tests PASS. Do not rerun the full suite unless production code changes after this result.

- [ ] **Step 4: Run static and sensitive-data gates**

Run:

    python -m py_compile app_core/platform_data_models.py app_core/platform_data_service.py app_core/platform_data_collectors.py app_core/douyin_data_collector.py app_core/platform_data_sync.py ui/data_monitor_page.py ui/main_window.py
    git diff --check
    rg -n "Cookie:|sessionid|passport_csrf|responseBody|a_bogus|msToken" app_core/platform_data_* app_core/douyin_data_collector.py ui/data_monitor_page.py

Expected:

- py_compile exits 0;
- diff check exits 0;
- any sensitive-term matches occur only in defensive allow/deny constants or tests, never logs, database writes or UI payloads.

- [ ] **Step 5: Perform the authorized live read-only acceptance**

From the source client, select one already logged-in Douyin account and click 立即同步 once. Acceptance evidence:

- no visible browser window;
- UI reports direct_session or browser_signed;
- at least one non-empty trustworthy metric is displayed;
- the same metric is manually readable on the official creator dashboard at that time;
- no Chromium/Playwright process owned by the sync remains after completion;
- no publish/preflight/save/delete request occurs.

If login_required or metric_payload_empty occurs, report the exact fixed state and stop. Do not claim live success from offline tests.

- [ ] **Step 6: Commit docs and final report**

Commit:

    git add README.md docs/PLATFORM_DATA_COLLECTION.md
    git commit -m "记录平台数据采集使用边界"

Then record final SHAs, affected/full test counts, live acceptance state and remaining unsupported platforms. Do not stage .superpowers/brainstorm/ or outputs/.
