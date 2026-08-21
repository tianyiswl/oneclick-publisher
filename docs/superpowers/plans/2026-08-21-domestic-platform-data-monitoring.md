# 国内四平台数据监测 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有抖音、小红书数据监测基础上，按公众号、视频号、B站、快手的顺序接入官方创作后台只读数据。

**Architecture:** 保留 `platform_data_sync.py` 的统一编排和 `platform_data_service.py` 的原子写入，每个平台使用独立采集器，只解析已经通过真实页面观察确认的官方 HTTPS JSON 响应。采集器统一输出 `CollectionBatch`，数据库事务完成后必须读回本批次；任一平台失败不影响其他平台已保存数据。

**Tech Stack:** Python 3、PyQt5、Playwright async API、SQLite、`unittest`

**Spec:** `docs/superpowers/specs/2026-08-21-domestic-platform-data-monitoring-design.md`

## Global Constraints

- 实施顺序固定为：公众号 → 视频号 → B站 → 快手。
- 只使用账号管理已保存的本地登录态读取官方创作者后台，不输出 Cookie、请求头、响应正文、HTML、验证码或异常原文。
- 不主动调用未经审核的隐藏接口；域名、路径、页面阶段、字段类型和内容 ID 都要校验。
- 平台未提供的字段保持不可得，不写成 `0`；原始字段名写入 `raw_metric_key`。
- 每次同步使用短会话，必须有超时、请求数、响应数、单响应大小和累计大小上限，并在成功与失败路径关闭所有资源。
- 没有真实账号时只能标记“代码与离线合同完成，等待真实验收”。
- 真实验收必须包含同一登录态连续两次同步、SQLite 读回和页面核对。

---

### Task 1: 抽出国内平台共用的安全采集骨架

**Files:**
- Create: `app_core/domestic_data_collector.py`
- Modify: `app_core/platform_data_collection_errors.py`
- Test: `test_domestic_data_collector.py`

**Interfaces:**
- Consumes: `CollectionBatch`, `CleanupReceipt`, `PlatformDataCollectionError`
- Produces: `DomesticCollectorConfig`, `CapturedJson`, `DomesticBrowserCollector.collect(account: dict) -> CollectionBatch`

- [ ] **Step 1: Write failing contract tests**

```python
def test_collector_rejects_unreviewed_host():
    collector = DomesticBrowserCollector(config(), browser_factory=fake_browser(
        response_url="https://evil.example/metrics", payload={"data": {}}
    ))
    with self.assertRaises(PlatformDataCollectionError) as raised:
        collector.collect(account())
    self.assertEqual(raised.exception.error_code, "metric_payload_empty")

def test_collector_closes_browser_after_parser_failure():
    browser = fake_browser(payload={"unexpected": True})
    collector = DomesticBrowserCollector(config(), browser_factory=lambda: browser)
    with self.assertRaises(PlatformDataCollectionError):
        collector.collect(account())
    self.assertEqual(browser.alive_resource_count, 0)
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `python3 -m unittest -v test_domestic_data_collector`

Expected: FAIL because `app_core.domestic_data_collector` does not exist.

- [ ] **Step 3: Implement the shared immutable configuration and bounded capture**

```python
@dataclass(frozen=True, slots=True)
class DomesticCollectorConfig:
    platform_type: int
    home_url: str
    allowed_hosts: frozenset[str]
    endpoint_by_path: Mapping[str, str]
    phase_by_path: Mapping[str, str]
    total_timeout_seconds: float = 60.0
    max_response_bytes: int = 1_048_576
    max_total_response_bytes: int = 4_194_304
    max_responses: int = 100
    max_requests: int = 400

@dataclass(frozen=True, slots=True)
class CapturedJson:
    endpoint: str
    phase: str
    path: str
    payload: Mapping[str, object]
```

Implement `DomesticBrowserCollector` so it validates the account state path, creates a fresh Playwright/context/page, captures only configured hosts and paths, enforces all limits, delegates parsing to `parse_captures(account_id, captures)`, and always returns a `CleanupReceipt`. Convert uncontrolled exceptions to the fixed `PlatformDataCollectionError` enums only.

- [ ] **Step 4: Run the focused tests**

Run: `python3 -m unittest -v test_domestic_data_collector`

Expected: PASS, including login redirect, verification page, response size, request limit, total timeout and cleanup tests.

- [ ] **Step 5: Commit**

```bash
git add app_core/domestic_data_collector.py app_core/platform_data_collection_errors.py test_domestic_data_collector.py
git commit -m "新增国内平台安全采集骨架"
```

### Task 2: 接入公众号数据采集器

**Files:**
- Create: `app_core/wechat_data_collector.py`
- Create: `test_wechat_data_collector.py`
- Modify: `app_core/platform_data_collectors.py`
- Modify: `test_platform_data_sync.py`

**Interfaces:**
- Consumes: `DomesticBrowserCollector`, `CapturedJson`, `MetricPoint`, `ContentRecord`, `CollectionBatch`
- Produces: `WechatDataCollector.collect(account: dict) -> CollectionBatch`; registry entry `platform_type=10`

- [ ] **Step 1: Add parser and registry tests using sanitized captured shapes**

```python
def test_wechat_parser_maps_account_and_article_metrics():
    batch = WechatDataCollector.parse_captures(
        account_id=5,
        captures=wechat_captures(
            followers_total=120,
            followers_net=3,
            article_id="article-1",
            title="sample",
            views=80,
            likes=4,
            comments=2,
            shares=1,
        ),
    )
    self.assertEqual(batch.platform_type, 10)
    self.assertIn("followers_total", {p.metric_key for p in batch.metrics})
    self.assertEqual(batch.contents[0].content_id, "article-1")

def test_registry_builds_wechat_collector():
    self.assertIn(10, registered_platform_types())
```

- [ ] **Step 2: Run and verify the tests fail**

Run: `python3 -m unittest -v test_wechat_data_collector test_platform_data_sync`

Expected: FAIL because the collector and type 10 registry entry do not exist.

- [ ] **Step 3: Implement only reviewed official response contracts**

Create `WECHAT_CONFIG` with the official creator host, reviewed response paths and phase mapping observed from the existing logged-in creator page. Implement strict helpers for numeric values, Beijing dates, account metrics and article records. Return `warning_code="content_list_unavailable"` when only account data was observed; never manufacture article rows.

- [ ] **Step 4: Register type 10 and run focused tests**

Run: `python3 -m unittest -v test_wechat_data_collector test_platform_data_sync test_platform_data_service`

Expected: PASS. The demo account must still be rejected as `session_state_missing`; registration alone must not make demo data look real.

- [ ] **Step 5: Commit**

```bash
git add app_core/wechat_data_collector.py app_core/platform_data_collectors.py test_wechat_data_collector.py test_platform_data_sync.py
git commit -m "接入公众号只读数据采集"
```

### Task 3: 接入视频号数据采集器

**Files:**
- Create: `app_core/channels_data_collector.py`
- Create: `test_channels_data_collector.py`
- Modify: `app_core/platform_data_collectors.py`
- Modify: `test_platform_data_sync.py`

**Interfaces:**
- Consumes: shared domestic collector contract from Task 1
- Produces: `ChannelsDataCollector.collect(account: dict) -> CollectionBatch`; registry entry `platform_type=2`

- [ ] **Step 1: Write strict mapping, phase binding and cleanup tests**

```python
def test_channels_parser_keeps_video_metrics_attached_to_video_id():
    batch = ChannelsDataCollector.parse_captures(7, channels_captures(
        content_id="finder-1", views=91, likes=8, comments=2, shares=3
    ))
    points = [p for p in batch.metrics if p.entity_type == "content"]
    self.assertEqual({p.entity_key for p in points}, {"finder-1"})

def test_channels_rejects_list_response_seen_during_account_phase():
    with self.assertRaises(PlatformDataCollectionError):
        ChannelsDataCollector.parse_captures(7, misplaced_channels_captures())
```

- [ ] **Step 2: Run and verify failure**

Run: `python3 -m unittest -v test_channels_data_collector`

Expected: FAIL because `channels_data_collector.py` does not exist.

- [ ] **Step 3: Implement the type 2 adapter and registry entry**

Use only the official 视频号助手 host and the paths observed during the real account session. Account summaries, content lists and content metrics must be separate phases. Map unsupported fields to absence, preserve official raw keys, and reject duplicate content IDs.

- [ ] **Step 4: Run affected tests**

Run: `python3 -m unittest -v test_channels_data_collector test_platform_data_sync test_platform_data_service`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app_core/channels_data_collector.py app_core/platform_data_collectors.py test_channels_data_collector.py test_platform_data_sync.py
git commit -m "接入视频号只读数据采集"
```

### Task 4: 接入 B站数据采集器

**Files:**
- Create: `app_core/bilibili_data_collector.py`
- Create: `test_bilibili_data_collector.py`
- Modify: `app_core/platform_data_collectors.py`
- Modify: `test_platform_data_sync.py`

**Interfaces:**
- Consumes: shared domestic collector contract from Task 1
- Produces: `BilibiliDataCollector.collect(account: dict) -> CollectionBatch`; registry entry `platform_type=5`

- [ ] **Step 1: Write tests for account totals, submissions and unavailable fields**

```python
def test_bilibili_parser_maps_archive_metrics_without_zero_filling():
    batch = BilibiliDataCollector.parse_captures(8, bilibili_captures(
        content_id="BV1TEST", views=300, likes=12, comments=None, favorites=6
    ))
    keys = {p.metric_key for p in batch.metrics if p.entity_key == "BV1TEST"}
    self.assertEqual(keys, {"views", "likes", "favorites"})
```

- [ ] **Step 2: Run and verify failure**

Run: `python3 -m unittest -v test_bilibili_data_collector`

Expected: FAIL because the B站 adapter does not exist.

- [ ] **Step 3: Implement the type 5 adapter and registry entry**

Accept only reviewed responses from the B站创作中心 identity/data/submission pages. Normalize AV/BV identifiers to the exact stable identifier returned by the official response; do not infer IDs from titles. Keep exposure-only or platform-specific fields out of public cards unless they map exactly to the existing metric contract.

- [ ] **Step 4: Run affected tests**

Run: `python3 -m unittest -v test_bilibili_data_collector test_platform_data_sync test_platform_data_service`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app_core/bilibili_data_collector.py app_core/platform_data_collectors.py test_bilibili_data_collector.py test_platform_data_sync.py
git commit -m "接入B站只读数据采集"
```

### Task 5: 接入快手数据采集器

**Files:**
- Create: `app_core/kuaishou_data_collector.py`
- Create: `test_kuaishou_data_collector.py`
- Modify: `app_core/platform_data_collectors.py`
- Modify: `test_platform_data_sync.py`

**Interfaces:**
- Consumes: shared domestic collector contract from Task 1
- Produces: `KuaishouDataCollector.collect(account: dict) -> CollectionBatch`; registry entry `platform_type=4`

- [ ] **Step 1: Write tests for work identifiers, values and verification handling**

```python
def test_kuaishou_parser_maps_work_metrics():
    batch = KuaishouDataCollector.parse_captures(9, kuaishou_captures(
        content_id="work-1", views=66, likes=7, comments=1, shares=2
    ))
    self.assertEqual(batch.platform_type, 4)
    self.assertEqual(batch.contents[0].content_id, "work-1")

def test_kuaishou_verification_page_returns_fixed_code():
    with self.assertRaises(PlatformDataCollectionError) as raised:
        collector_for_verification_page().collect(account())
    self.assertEqual(raised.exception.error_code, "verification_required")
```

- [ ] **Step 2: Run and verify failure**

Run: `python3 -m unittest -v test_kuaishou_data_collector`

Expected: FAIL because the 快手 adapter does not exist.

- [ ] **Step 3: Implement the type 4 adapter and registry entry**

Use only the official 快手创作者服务平台 host and reviewed paths. Treat verification as a terminal, retryable fixed error; never retry in a loop inside one sync. Preserve absent share/favorite fields as absent.

- [ ] **Step 4: Run affected tests**

Run: `python3 -m unittest -v test_kuaishou_data_collector test_platform_data_sync test_platform_data_service`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app_core/kuaishou_data_collector.py app_core/platform_data_collectors.py test_kuaishou_data_collector.py test_platform_data_sync.py
git commit -m "接入快手只读数据采集"
```

### Task 6: 完成页面选择、状态隔离和用户可读失败提示

**Files:**
- Modify: `ui/data_monitor_page.py`
- Modify: `test_data_monitor_page.py`
- Modify: `app_core/platform_data_collection_errors.py`

**Interfaces:**
- Consumes: `registered_platform_types()`, `sync_account_data(account_id, report=...)`
- Produces: platform/account/range selection and stale-result-safe rendering for types 2, 4, 5 and 10

- [ ] **Step 1: Add UI regression tests**

```python
def test_domestic_registered_platforms_appear_only_with_real_accounts(self):
    page = self.page(accounts=[wechat_real(), channels_real(), bilibili_real(), kuaishou_real()])
    self.assertEqual(self.platform_labels(page), ["公众号", "视频号", "B站", "快手"])

def test_late_result_from_previous_platform_does_not_replace_current_view(self):
    page, runner = self.blocking_page()
    page.select_subject(platform_type=2, account_id=20)
    page.start_sync()
    page.select_subject(platform_type=5, account_id=21)
    runner.finish_success(account_id=20)
    self.assertEqual(page.current_subject(), (5, 21))
```

- [ ] **Step 2: Run and verify at least the new failure**

Run: `QT_QPA_PLATFORM=offscreen python3 -m unittest -v test_data_monitor_page`

Expected: FAIL on the new four-platform selection/error-copy assertions.

- [ ] **Step 3: Implement minimal UI updates**

Keep the existing two-level platform/account selector. Add readable copies for login required, verification required, platform response changed, incomplete cleanup and local persistence/readback failure. Bind task completion to `(platform_type, account_id)` and discard late results for a different current subject.

- [ ] **Step 4: Run UI and sync tests**

Run: `QT_QPA_PLATFORM=offscreen python3 -m unittest -v test_data_monitor_page test_platform_data_sync`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/data_monitor_page.py app_core/platform_data_collection_errors.py test_data_monitor_page.py
git commit -m "完善国内平台数据监测交互"
```

### Task 7: 逐平台真实只读验收

**Files:**
- Create: `docs/verification/domestic-platform-data-monitoring-2026-08-21.md`
- Modify only if a real run exposes a defect: the affected collector and its focused test

**Interfaces:**
- Consumes: account rows and saved browser state from account management; `sync_account_data`
- Produces: sanitized acceptance table with run IDs, counts and cleanup receipts; no sensitive payloads

- [ ] **Step 1: Identify available real accounts without exposing session contents**

Run a read-only account listing that prints only `id`, `type`, display name and whether the state file exists. Do not print the state path contents.

- [ ] **Step 2: Validate each available platform twice in implementation order**

For each available real account, call `sync_account_data(account_id, report=...)` twice. Stop that platform on the first defect, add one focused regression test, fix it, and rerun only that platform. Do not continue repeated real requests after a known failure.

- [ ] **Step 3: Verify SQLite readback**

For each successful run, verify the new run ID, metric count, content count, `sourceMode=browser_signed`, non-empty platform observation time, and exact account/content ownership. Confirm the second run creates a distinct sync run and leaves no alive browser resources.

- [ ] **Step 4: Verify the real UI**

Open the source client, select the platform and account, run sync once, and confirm the selected platform remains unchanged while account cards and available content rows refresh. Record unavailable fields as `null/平台未提供`, never as a fabricated zero.

- [ ] **Step 5: Record honest status and commit**

The verification document must use one of: `真实连续两次通过`, `代码与离线合同通过，等待真实账号`, or `未通过：<固定错误类别>`.

```bash
git add docs/verification/domestic-platform-data-monitoring-2026-08-21.md
git commit -m "记录国内平台数据真实验收"
```

### Task 8: 受影响模块与全量回归

**Files:**
- Modify only for discovered regressions: affected source and focused test
- Verify: all repository `test_*.py`

**Interfaces:**
- Consumes: all deliverables from Tasks 1-7
- Produces: final regression evidence and a clean worktree

- [ ] **Step 1: Run the complete platform-data module set**

Run:

```bash
QT_QPA_PLATFORM=offscreen python3 -m unittest -v \
  test_domestic_data_collector \
  test_wechat_data_collector \
  test_channels_data_collector \
  test_bilibili_data_collector \
  test_kuaishou_data_collector \
  test_platform_data_sync \
  test_platform_data_service \
  test_data_monitor_page
```

Expected: PASS. If a test fails, stop, fix that specific defect, rerun its focused test, then rerun this module set.

- [ ] **Step 2: Run the full suite once**

Run: `QT_QPA_PLATFORM=offscreen python3 -m unittest discover -v`

Expected: all tests PASS. Record the exact passed/failed counts; do not describe a partial run as full regression.

- [ ] **Step 3: Review security and repository cleanliness**

Run:

```bash
rg -n "Cookie|Authorization|Set-Cookie|response\.text|response\.body" \
  app_core/*_data_collector.py docs/verification/domestic-platform-data-monitoring-2026-08-21.md
git status --short
```

Expected: no credential or raw response logging; only intended source/test/verification files changed.

- [ ] **Step 4: Commit final corrections if needed**

```bash
git add <only-the-files-fixed-in-this-task>
git commit -m "收尾国内平台数据监测"
```

