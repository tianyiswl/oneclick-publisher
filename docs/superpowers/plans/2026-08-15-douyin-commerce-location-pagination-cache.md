# 抖音带货地点分页加载与本地缓存 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为抖音带货地点搜索增加“设置页手动分页、正式发布自动分页、账号隔离本地缓存和失败后无感校对”。

**Architecture:** 新建独立 SQLite 缓存服务保存公开 POI 与校对状态；地点 DOM 服务提供首次搜索和单次加载更多原语；会话及采集器维持同一搜索轮次并拒绝迟到结果；Qt 页面先分页读取缓存，耗尽后才调用平台；批量执行器正式发布时自动逐页查找并把地点类结果回写缓存。缓存不持有浏览器对象，DOM 层不写缓存，正式发布不信任缓存。

**Tech Stack:** Python 3.12、PyQt6、Playwright async API、SQLite、`unittest`/`unittest.mock`。

## Global Constraints

- 设置页首次最多显示 10 条缓存，每次点击最多追加下一批 10 条。
- 设置页平台分页必须由用户点击触发；正式发布允许自动分页。
- 单轮平台分页最多点击 10 次或累计读取 100 个有效地址。
- 连续两次没有新增候选、平台无更多、超时或加载控件不唯一时安全停止。
- 候选身份固定为 `POI ID + 名称 + 完整地址 + 返佣类型`。
- 已手动选择的视频不得被自动填入覆盖；改回“请选择地点”后恢复自动填入资格。
- 缓存按抖音账号隔离，7 天未校对或地点类失败后在下次对应搜索中无感校对。
- 正式发布必须重新从平台逐页查找和严格回读完整 POI。
- 不保存 Cookie、验证码、HTML、DOM、浏览器对象或账号凭据。
- 测试遵循 RED → 最小 GREEN → 相关模块；最后收尾时才跑一次全量。

---

## 文件结构

- Create `app_core/douyin_location_cache.py`：缓存表、严格归一化、分页、合并、状态和淘汰。
- Modify `app_core/database.py`：初始化地点缓存表和索引。
- Modify `app_core/douyin_commerce_service.py`：识别加载更多、执行单次分页、正式发布自动分页。
- Modify `app_core/douyin_commerce_session.py`：保存当前地点搜索上下文并暴露单次加载更多。
- Modify `app_core/douyin_commerce_collectors.py`：在采集器动作队列中路由加载更多。
- Modify `ui/douyin_commerce_page.py`：缓存优先、本地分页、平台加载按钮、进度和自动填入保护。
- Modify `app_core/douyin_commerce_batch_executor.py`：地点结果写回缓存和新错误码文案。
- Test `test_douyin_location_cache.py`：缓存纯服务契约。
- Test `test_douyin_commerce_service.py`：DOM、会话、采集器、UI 和执行器契约。
- Modify `docs/DOUYIN_COMMERCE_WORKFLOW.md`：用户可见工作流说明。

---

### Task 1: 账号隔离地点缓存服务

**Files:**
- Create: `app_core/douyin_location_cache.py`
- Modify: `app_core/database.py`
- Create: `test_douyin_location_cache.py`

**Interfaces:**
- Produces: `LocationCacheQuery(account_id, scope, keyword, commission_filter)`。
- Produces: `get_cached_locations(query, *, offset=0, limit=10, now=None) -> dict[str, object]`。
- Produces: `merge_platform_locations(query, candidates, *, verified_at=None) -> dict[str, object]`。
- Produces: `record_location_publish_result(account_id, candidate, *, success, error_code="", occurred_at=None) -> None`。
- Produces: `location_requires_revalidation(row, *, now=None) -> bool`。

- [ ] **Step 1: 写缓存隔离、分页和状态 RED 测试**

```python
def test_cache_is_account_scoped_and_pages_ten_rows(self):
    merge_platform_locations(query("account-a"), candidates(25))
    merge_platform_locations(query("account-b"), candidates(3))
    assert ids(get_cached_locations(query("account-a"), offset=0, limit=10)) == ids_1_to_10
    assert ids(get_cached_locations(query("account-a"), offset=10, limit=10)) == ids_11_to_20
    assert len(get_cached_locations(query("account-b"))["candidates"]) == 3

def test_location_failure_marks_only_target_for_revalidation(self):
    record_location_publish_result("account-a", target, success=False,
                                   error_code="publish_location_readback_mismatch")
    assert cached(target)["status"] == "needs_revalidation"
    assert cached(other)["status"] == "reusable"
```

- [ ] **Step 2: 运行 Task 1 单测并确认因模块不存在而失败**

Run: `../../.venv/bin/python -m unittest -v test_douyin_location_cache`

Expected: FAIL with `ModuleNotFoundError: app_core.douyin_location_cache`。

- [ ] **Step 3: 实现严格公开模型和数据库表**

```python
@dataclass(frozen=True)
class LocationCacheQuery:
    account_id: str
    scope: str
    keyword: str
    commission_filter: str

LOCATION_STATUS_REUSABLE = "reusable"
LOCATION_STATUS_NEEDS_REVALIDATION = "needs_revalidation"
LOCATION_STATUS_INVALID = "invalid"
LOCATION_CACHE_PAGE_SIZE = 10
LOCATION_CACHE_CAPACITY = 100
LOCATION_REVALIDATE_AFTER = timedelta(days=7)
```

在 `database.py` 建表，唯一键为 `account_id, scope, poi_id, name, address, commission_type`；关键词使用独立关联表，避免同一 POI 因多个关键词复制。所有 upsert、状态更新和容量淘汰放在同一个 `with connect()` 事务中。

- [ ] **Step 4: 实现分页、原子合并、淘汰和状态转换**

只接受 built-in `int` 的 offset/limit；仅返回 `reusable` 且未过期记录。地点类固定错误码进入待校对，连续两次平台校对找不到进入失效；非地点错误不得调用状态更新。

- [ ] **Step 5: 运行 Task 1 单测**

Run: `../../.venv/bin/python -m unittest -v test_douyin_location_cache`

Expected: PASS。

- [ ] **Step 6: 提交 Task 1**

```bash
git add app_core/database.py app_core/douyin_location_cache.py test_douyin_location_cache.py
git commit -m "增加抖音地点本地缓存"
```

---

### Task 2: 平台地点单次加载更多原语

**Files:**
- Modify: `app_core/douyin_commerce_service.py`
- Modify: `test_douyin_commerce_service.py`

**Interfaces:**
- Produces: `load_more_commerce_location_candidates(page, *, previous_candidates, commission_filter="all", timeout_ms=30000) -> dict[str, object]`。
- Return keys: `platformResultCount`, `candidates`, `newCandidateCount`, `hasMore`, `stopReason`。
- Produces fixed errors: `publish_location_load_more_failed`。

- [ ] **Step 1: 写唯一按钮、增长等待和零新增 RED 测试**

```python
def test_load_more_clicks_once_and_returns_only_public_metadata(self):
    result = asyncio.run(load_more_commerce_location_candidates(
        page, previous_candidates=first_page, commission_filter="commission"))
    self.assertEqual(load_more.click_count, 1)
    self.assertEqual(result["newCandidateCount"], 8)
    self.assertTrue(result["hasMore"])
    self.assertNotIn("html", repr(result))

def test_load_more_rejects_multiple_visible_controls(self):
    with self.assertRaisesRegex(DouyinCommerceError,
                                "publish_location_load_more_failed"):
        asyncio.run(load_more_commerce_location_candidates(
            page,
            previous_candidates=first_page,
            commission_filter="all",
        ))
```

- [ ] **Step 2: 运行两个命名测试并确认 RED**

Run: `../../.venv/bin/python -m unittest -v test_douyin_commerce_service.DouyinCommerceLocationDomTests.test_load_more_clicks_once_and_returns_only_public_metadata test_douyin_commerce_service.DouyinCommerceLocationDomTests.test_load_more_rejects_multiple_visible_controls`

Expected: FAIL because function is absent。

- [ ] **Step 3: 实现可见“点击加载更多”唯一识别**

匹配可见按钮/文本控件，要求规范化文本精确包含“点击加载更多”或“加载更多”；排除 hidden、`aria-hidden=true`、`display:none`、累计 opacity 0 和不可点击节点。0 个表示 `hasMore=False`；多于 1 个固定失败。

- [ ] **Step 4: 实现单击、候选增长和去重**

点击恰好一次；轮询候选签名直到新增候选稳定三次。返回累计公开候选，不返回 locator、HTML 或 DOM。连续无增长由上层轮次累计，本函数只返回 `newCandidateCount=0`。

- [ ] **Step 5: 运行地点 DOM 相关测试**

Run: `../../.venv/bin/python -m unittest -v test_douyin_commerce_service.DouyinCommerceLocationDomTests`

Expected: PASS。

- [ ] **Step 6: 提交 Task 2**

```bash
git add app_core/douyin_commerce_service.py test_douyin_commerce_service.py
git commit -m "支持抖音地点单次加载更多"
```

---

### Task 3: 会话与采集器分页动作

**Files:**
- Modify: `app_core/douyin_commerce_session.py`
- Modify: `app_core/douyin_commerce_collectors.py`
- Modify: `test_douyin_commerce_service.py`

**Interfaces:**
- Produces: `commerce_session_manager.load_more_locations(session_id, keyword, scope, *, commission_filter, previous_candidates) -> dict[str, object]`。
- Produces: `commerce_collector_manager.load_more_locations(generation_id, keyword, scope, *, commission_filter, previous_candidates) -> dict[str, object]`。
- Consumes Task 2 public metadata envelope。

- [ ] **Step 1: 写同搜索上下文、迟到实例和动作串行 RED 测试**

```python
def test_load_more_requires_same_keyword_scope_and_filter(self):
    manager.search_locations(generation, "夜南香", "domestic",
                             commission_filter="commission")
    with self.assertRaisesRegex(DouyinCommerceCollectorError,
                                "collector_search_context_mismatch"):
        manager.load_more_locations(generation, "新关键词", "domestic",
                                    commission_filter="commission",
                                    previous_candidates=[])
```

另测旧 generation/instance 的迟到结果被丢弃，不能覆盖新搜索。

- [ ] **Step 2: 运行三个命名测试并确认 RED**

Run: `../../.venv/bin/python -m unittest -v test_douyin_commerce_collectors.DouyinCommerceCollectorManagerTests.test_load_more_requires_same_keyword_scope_and_filter test_douyin_commerce_service.DouyinCommerceSessionContractTests.test_load_more_returns_public_metadata test_douyin_commerce_collectors.DouyinCommerceCollectorManagerTests.test_late_load_more_is_discarded`

Expected: FAIL because APIs are absent。

- [ ] **Step 3: 在 session 保存受控搜索上下文**

```python
@dataclass
class _LocationSearchContext:
    keyword: str
    scope: str
    commission_filter: str
    candidates: list[dict[str, Any]]
    load_more_count: int = 0
    zero_growth_count: int = 0
```

新搜索替换上下文；准备发布设置、切换搜索、放弃或关闭 session 时清空。加载更多先做三字段一致性校验，再调用 Task 2 并更新累计候选。

- [ ] **Step 4: 在 collector action queue 增加加载更多动作**

捕获不可变 generation、collector type、instance id、关键词、范围、筛选和 previous candidates。成功及失败 envelope 均沿用现有 generation/type/instance 三重门禁；异常只投影固定 errorCode。

- [ ] **Step 5: 运行会话与采集器相关测试**

Run: `../../.venv/bin/python -m unittest -v test_douyin_commerce_service.DouyinCommerceSessionContractTests test_douyin_commerce_collectors.DouyinCommerceCollectorManagerTests`

Expected: PASS。

- [ ] **Step 6: 提交 Task 3**

```bash
git add app_core/douyin_commerce_session.py app_core/douyin_commerce_collectors.py test_douyin_commerce_service.py
git commit -m "贯通抖音地点分页采集动作"
```

---

### Task 4: 设置页缓存优先和手动分页

**Files:**
- Modify: `ui/douyin_commerce_page.py`
- Modify: `test_douyin_commerce_service.py`

**Interfaces:**
- Consumes Task 1 cache query/page/merge APIs。
- Consumes Task 3 collector/session load-more APIs。
- Produces UI state keys: `cacheTotal`, `cacheOffset`, `platformLoadCount`, `zeroGrowthCount`, `hasMore`, `source`。

- [ ] **Step 1: 写缓存首屏、本地分页和平台接力 RED 测试**

```python
def test_search_shows_first_ten_cached_without_platform_call(self):
    seed_cache(100)
    page.batch_location_search_button.click()
    self.assertEqual(len(page._batch_location_state()["candidates"]), 10)
    platform_search.assert_not_called()

def test_load_more_uses_cache_then_platform_once(self):
    seed_cache(20)
    page._load_more_batch_locations()  # 10 -> 20, local only
    page._load_more_batch_locations()  # cache exhausted, platform once
    self.assertEqual(platform_load_more.call_count, 1)
```

另测已选视频不覆盖、改回“请选择地点”可重新填入、不同账号隔离和新搜索令牌拒绝迟到回调。

- [ ] **Step 2: 运行 Task 4 命名测试并确认 RED**

Run: `../../.venv/bin/python -m unittest -v test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_search_shows_first_ten_cached_without_platform_call test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_load_more_uses_cache_then_platform_once`

Expected: FAIL because load-more UI/state is absent。

- [ ] **Step 3: 增加按钮和受控进度文案**

在地点卡底部新增 object name `douyinCommerceBatchLoadMoreLocations`。按钮仅在搜索轮次存在且 `hasMore` 时可用；运行中显示“正在加载更多…”。状态文案使用缓存已显示数、缓存总数、平台批次和累计候选数，不显示内部路径或异常原文。

- [ ] **Step 4: 实现缓存优先搜索和分页**

搜索先构造当前账号 query：有可复用缓存则显示前 10 条并不调用平台；没有缓存、存在待校对或 7 天过期才启动平台搜索。加载更多先取本地下一个 offset；本地耗尽才调用 Task 3。平台结果合并缓存后再投影当前筛选。

- [ ] **Step 5: 保护选择并清理轮次**

只对 combo 当前为 placeholder 的视频调用自动填入。新关键词、范围、筛选、账号、视频批次、放弃和整批完成均递增搜索 token 并清分页状态；放弃和完成不得删除缓存。

- [ ] **Step 6: 运行批量 UI 测试**

Run: `QT_QPA_PLATFORM=offscreen ../../.venv/bin/python -m unittest -v test_douyin_commerce_service.DouyinCommerceBatchUiTests`

Expected: PASS。

- [ ] **Step 7: 提交 Task 4**

```bash
git add ui/douyin_commerce_page.py test_douyin_commerce_service.py
git commit -m "增加抖音地点缓存分页交互"
```

---

### Task 5: 正式发布自动分页和缓存结果回写

**Files:**
- Modify: `app_core/douyin_commerce_service.py`
- Modify: `app_core/douyin_commerce_session.py`
- Modify: `app_core/douyin_commerce_batch_executor.py`
- Modify: `test_douyin_commerce_service.py`

**Interfaces:**
- Extends: `apply_saved_commerce_location_to_page(page, preset, scope, keywords, commission_filter="all", *, max_load_more_clicks=10, max_candidates=100)`。
- Consumes Task 1 `record_location_publish_result`。
- Produces fixed errors defined in the design。

- [ ] **Step 1: 写目标在后续批次和安全停止 RED 测试**

```python
def test_publish_loads_until_saved_poi_is_found(self):
    result = asyncio.run(apply_saved_commerce_location_to_page(
        page, preset, "domestic", ["夜南香"], "commission"))
    self.assertEqual(load_more.click_count, 2)
    self.assertEqual(result["location"]["poiId"], preset["poiId"])

def test_publish_stops_after_ten_clicks(self):
    with self.assertRaisesRegex(DouyinCommerceError,
                                "publish_location_load_more_limit"):
        asyncio.run(apply_saved_commerce_location_to_page(
            page,
            preset,
            "domestic",
            ["夜南香"],
            "commission",
            max_load_more_clicks=10,
            max_candidates=100,
        ))
```

另测 100 条、连续两次零新增、无更多、按钮歧义、返佣变化和找到后不再点击。

- [ ] **Step 2: 运行正式发布分页命名测试并确认 RED**

Run: `../../.venv/bin/python -m unittest -v test_douyin_commerce_service.DouyinCommerceLocationDomTests.test_publish_loads_until_saved_poi_is_found test_douyin_commerce_service.DouyinCommerceLocationDomTests.test_publish_stops_after_ten_clicks`

Expected: FAIL because publish search only reads first stable page。

- [ ] **Step 3: 实现正式发布有界自动分页**

每次关键词搜索先匹配首屏；未找到则在同一面板循环调用 Task 2。每轮记录 click count、累计有效数、新增数；找到唯一完整 POI 立即 break。所有 exit path 继续使用严格 selector cleanup。

- [ ] **Step 4: 增加固定错误码和批执行器文案**

新增：`publish_location_not_found_after_all_pages`、`publish_location_load_more_limit`、`publish_location_load_more_failed`。错误必须使用 `raise DouyinCommerceError(error_code) from None` 或经现有固定投影，不携带 DOM 原文。

- [ ] **Step 5: 回写对应地点状态**

批执行器在地点回读成功后调用 `record_location_publish_result(account_id, candidate, success=True, occurred_at=finished_at)`；只在设计列出的地点错误码发生时以同一账号和候选调用 `success=False`。上传、验证码、网络或定时失败不得调用缓存状态 API。

- [ ] **Step 6: 运行服务、session 和 batch executor 相关测试**

Run: `../../.venv/bin/python -m unittest -v test_douyin_commerce_service.DouyinCommerceLocationDomTests test_douyin_commerce_service.DouyinCommerceSessionContractTests test_douyin_commerce_batch_executor.DouyinCommerceBatchExecutorTests`

Expected: PASS。

- [ ] **Step 7: 提交 Task 5**

```bash
git add app_core/douyin_commerce_service.py app_core/douyin_commerce_session.py app_core/douyin_commerce_batch_executor.py test_douyin_commerce_service.py
git commit -m "支持抖音发布地点自动分页校对"
```

---

### Task 6: 工作流说明与最终验证

**Files:**
- Modify: `docs/DOUYIN_COMMERCE_WORKFLOW.md`
- Modify: `test_douyin_location_cache.py`
- Modify: `test_douyin_commerce_service.py`

**Interfaces:**
- Verifies all public interfaces from Tasks 1–5。

- [ ] **Step 1: 更新工作流说明**

写清：首次展示缓存 10 条、本地缓存分页优先、缓存耗尽后每次手动加载一批、正式发布自动最多 10 次/100 条、地点失败后下次无感校对，以及缓存不代表平台发布成功。

- [ ] **Step 2: 运行地点缓存与地点 DOM 专项**

Run: `../../.venv/bin/python -m unittest -v test_douyin_location_cache test_douyin_commerce_service.DouyinCommerceLocationDomTests`

Expected: PASS。

- [ ] **Step 3: 运行会话、采集器、UI 和执行器相关组合**

Run: `QT_QPA_PLATFORM=offscreen ../../.venv/bin/python -m unittest -v test_douyin_commerce_service.DouyinCommerceSessionContractTests test_douyin_commerce_collectors.DouyinCommerceCollectorManagerTests test_douyin_commerce_service.DouyinCommerceBatchUiTests test_douyin_commerce_batch_executor.DouyinCommerceBatchExecutorTests`

Expected: PASS。

- [ ] **Step 4: 只在收尾运行一次全量测试**

Run: `QT_QPA_PLATFORM=offscreen ../../.venv/bin/python -m unittest discover -v`

Expected: all tests PASS。若出现首个失败，立即停止，修复该单项后重新执行受影响组合；所有代码稳定后再刷新一次最终全量证据。

- [ ] **Step 5: 静态和安全检查**

```bash
../../.venv/bin/python -m py_compile \
  app_core/douyin_location_cache.py \
  app_core/douyin_commerce_service.py \
  app_core/douyin_commerce_session.py \
  app_core/douyin_commerce_collectors.py \
  app_core/douyin_commerce_batch_executor.py \
  ui/douyin_commerce_page.py
git diff --check
```

确认产品 diff 中没有 Cookie、验证码、HTML、DOM 快照、凭据样本、占位实现或范围外修改。

- [ ] **Step 6: 最终只读审查**

逐项核对：账号隔离、10/100 上限、迟到回调、缓存过期、选择不覆盖、正式发布严格回读、地点/非地点错误分类、所有关闭路径。

- [ ] **Step 7: 提交文档与最终修正**

```bash
git add docs/DOUYIN_COMMERCE_WORKFLOW.md test_douyin_location_cache.py test_douyin_commerce_service.py
git commit -m "完善抖音地点分页缓存验收"
```

---

## 实施停点

- 本计划只授权本地代码、测试、文档和 Git 提交。
- 不启动真实抖音账号、不读取真实账号数据、不上传视频、不预检、不保存平台草稿、不提交发布。
- 真实平台 DOM 验证需要大帅在代码验收后另行明确授权。
