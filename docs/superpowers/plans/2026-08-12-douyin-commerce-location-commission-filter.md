# 抖音带货地点返佣筛选 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在抖音带货平台设置中新增默认“返佣”的“全部／返佣／无佣”筛选，并在正式发布时重新核对每条视频保存的返佣要求。

**Architecture:** 新增一个纯函数模块负责返佣摘要解析、筛选值规范化和候选过滤；现有页面服务只负责从官方可见候选行提取公开摘要。UI 保存每条视频的筛选意图，批次契约、草稿、任务和正式发布原子地点恢复全链路传递该意图；正式发布仍在同一打开面板内先筛选，再按 POI、名称和完整地址唯一匹配、点击和回读。

**Tech Stack:** Python 3.12、PyQt6、Playwright、SQLite、本项目原生 `unittest`、Git

## Global Constraints

- 筛选文案固定为 `全部／返佣／无佣`，新批次默认 `返佣`。
- 明确返佣数量大于 0 为 `commission`；无商品／返佣标识或明确 0 件返佣为 `no_commission`；含商业摘要但无法稳定判断时为 `unknown`。
- 自动填入只处理“请选择地点”的视频，已有地点不覆盖；候选不足或为 0 时不回退到全部、不默认第一项、不模糊匹配。
- 每条视频保存独立 `commissionFilter`；切换顶部筛选不改写已有选择。
- 账号级地点预设数据库不新增返佣字段；返佣是本批次实时观察，不是地点永久属性。
- 旧任务或旧草稿缺少返佣筛选时按 `all` 兼容；新批次必须保存有效筛选值。
- 正式发布返佣状态不满足时使用固定错误码 `publish_location_commission_mismatch`，只失败当前视频并继续后续视频。
- 只保留公开结构化字段，不保存原始 DOM、HTML、Cookie、账号路径、验证码或异常原文。
- 严格遵守 TDD：每个行为先取得 RED；发现失败立即停下修复，只重跑对应单项；全部任务收尾时才运行一次完整回归。
- 自动测试不得访问抖音、启动真实账号会话、上传、预检、保存平台草稿或正式发布。
- 从 `main@d2f8980` 创建隔离 worktree 实施，保留主仓库既有未跟踪目录 `.superpowers/brainstorm/` 与 `outputs/`。

---

## 文件结构与职责

- Create: `app_core/douyin_commerce_location_commission.py` — 返佣筛选常量、摘要解析、字段白名单、候选过滤和显示文案。
- Modify: `app_core/douyin_commerce_service.py` — 从可见官方候选项提取 `commerceInfo`，调用纯函数规范化；正式搜索接收返佣筛选。
- Modify: `app_core/douyin_commerce_session.py` — `apply_saved_location` 传递返佣筛选并允许固定 mismatch 错误码。
- Modify: `app_core/douyin_commerce_batch_service.py` — 批次地点快照和逐视频发布载荷保存返佣意图。
- Modify: `app_core/douyin_commerce_batch_draft_service.py` — 草稿 schema 升级和旧草稿兼容。
- Modify: `app_core/task_service.py` — 续发重建保留筛选意图，任务地点摘要展示返佣／无佣。
- Modify: `app_core/douyin_commerce_batch_executor.py` — 正式发布把筛选意图传给原子地点恢复，并投影固定公开错误说明。
- Modify: `ui/douyin_commerce_page.py` — 新增筛选控件、默认状态、候选过滤、自动填入、状态反馈和生命周期重置。
- Verify: `ui/task_page.py` — 继续使用现有地点列展示包含返佣标签的 `locationSummary`，不新增宽列或改变表格结构。
- Modify tests: `test_douyin_commerce_service.py`、`test_douyin_commerce_batch_service.py`、`test_douyin_commerce_batch_draft_service.py`、`test_douyin_commerce_batch_executor.py`、`test_task_service.py`、`test_task_page.py`。

---

### Task 1: 返佣摘要解析与候选过滤纯函数

**Files:**
- Create: `app_core/douyin_commerce_location_commission.py`
- Modify: `test_douyin_commerce_service.py`（`DouyinCommercePayloadTests`）

**Interfaces:**
- Produces: `normalize_commission_filter(value: object, *, default: str = "all") -> str`
- Produces: `normalize_observed_commission_type(value: object, *, default: str = "unknown") -> str`
- Produces: `parse_commission_summary(value: object) -> dict[str, object]`
- Produces: `filter_location_candidates(rows: object, commission_filter: object) -> list[dict[str, object]]`
- Produces constants: `COMMISSION_FILTER_ALL`、`COMMISSION_FILTER_COMMISSION`、`COMMISSION_FILTER_NO_COMMISSION`、`DEFAULT_COMMISSION_FILTER`

- [ ] **Step 1: 写返佣分类与过滤 RED 测试**

在 `DouyinCommercePayloadTests` 新增表驱动测试，锁定全部边界：

```python
def test_commission_summary_classification_is_strict_and_public(self) -> None:
    cases = [
        ("15件商品 · 15件返佣", "commission", 15, 15, "返佣"),
        ("13件商品 · 0件返佣", "no_commission", 13, 0, "无佣"),
        ("", "no_commission", None, None, "无佣"),
        ("13件商品", "unknown", 13, None, "待确认"),
        ("返佣活动", "unknown", None, None, "待确认"),
    ]
    for raw, expected_type, products, commission_products, label in cases:
        with self.subTest(raw=raw):
            parsed = commission.parse_commission_summary(raw)
            self.assertEqual(parsed["commissionType"], expected_type)
            self.assertEqual(parsed["productCount"], products)
            self.assertEqual(parsed["commissionProductCount"], commission_products)
            self.assertEqual(parsed["commissionLabel"], label)
            self.assertNotIn("commerceInfo", parsed)

def test_commission_filter_does_not_fallback_or_hide_unknown_from_all(self) -> None:
    rows = [
        {"poiId": "p1", "commissionType": "commission"},
        {"poiId": "p2", "commissionType": "no_commission"},
        {"poiId": "p3", "commissionType": "unknown"},
    ]
    self.assertEqual(
        [row["poiId"] for row in commission.filter_location_candidates(rows, "commission")],
        ["p1"],
    )
    self.assertEqual(
        [row["poiId"] for row in commission.filter_location_candidates(rows, "no_commission")],
        ["p2"],
    )
    self.assertEqual(
        [row["poiId"] for row in commission.filter_location_candidates(rows, "all")],
        ["p1", "p2", "p3"],
    )
```

- [ ] **Step 2: 运行单项并确认 RED**

Run:

```bash
.venv/bin/python -m unittest \
  test_douyin_commerce_service.DouyinCommercePayloadTests.test_commission_summary_classification_is_strict_and_public \
  test_douyin_commerce_service.DouyinCommercePayloadTests.test_commission_filter_does_not_fallback_or_hide_unknown_from_all -v
```

Expected: `ModuleNotFoundError` 或缺少上述函数；不得出现平台访问。

- [ ] **Step 3: 实现最小纯函数模块**

核心实现必须先拒绝 `bool` 等伪整数，只接受可见中文摘要的精确数字结构：

```python
COMMISSION_FILTER_ALL = "all"
COMMISSION_FILTER_COMMISSION = "commission"
COMMISSION_FILTER_NO_COMMISSION = "no_commission"
DEFAULT_COMMISSION_FILTER = COMMISSION_FILTER_COMMISSION

_PRODUCT_RE = re.compile(r"(?P<count>\d+)\s*件商品")
_COMMISSION_RE = re.compile(r"(?P<count>\d+)\s*件返佣")

def parse_commission_summary(value: object) -> dict[str, object]:
    text = _text(value)
    product_match = _PRODUCT_RE.search(text)
    commission_match = _COMMISSION_RE.search(text)
    product_count = int(product_match.group("count")) if product_match else None
    commission_count = int(commission_match.group("count")) if commission_match else None
    if commission_count is not None:
        kind = "commission" if commission_count > 0 else "no_commission"
    elif not text:
        kind = "no_commission"
    else:
        kind = "unknown"
    return {
        "commissionType": kind,
        "productCount": product_count,
        "commissionProductCount": commission_count,
        "commissionLabel": {
            "commission": "返佣", "no_commission": "无佣", "unknown": "待确认"
        }[kind],
    }
```

`normalize_commission_filter` 只接受三个固定值；外部新批次调用显式使用默认 `commission`，旧数据兼容调用显式使用默认 `all`。`normalize_observed_commission_type` 只接受 `commission／no_commission／unknown`，缺失旧数据返回调用方显式给定的 `unknown`，其它值抛固定 `ValueError`。

- [ ] **Step 4: 重跑两个单项并确认 GREEN**

使用 Step 2 原命令，Expected: `2 tests OK`。

- [ ] **Step 5: 提交 Task 1**

```bash
git add app_core/douyin_commerce_location_commission.py test_douyin_commerce_service.py
git commit -m "解析抖音地点返佣摘要"
```

---

### Task 2: 平台可见候选保留返佣结构

**Files:**
- Modify: `app_core/douyin_commerce_service.py:206-258, 2160-2410, 2534-2638, 2703-2750`
- Modify: `test_douyin_commerce_service.py`（`DouyinCommercePayloadTests`、`DouyinCommerceLocationDomTests`）

**Interfaces:**
- Consumes: `parse_commission_summary`、`filter_location_candidates`、`normalize_commission_filter`
- Changes: `normalize_commerce_location_candidate(value) -> dict` 新增四个返佣公开字段
- Changes: `search_commerce_location_store_candidates(..., commission_filter: object = "all")`
- Changes: `apply_saved_commerce_location_to_page(..., commission_filter: object = "all")`

- [ ] **Step 1: 写候选提取、过滤后去重和同面板复核 RED 测试**

新增三项：

```python
def test_visible_location_candidate_keeps_structured_commission_only(self) -> None:
    candidate = douyin_commerce_service.normalize_commerce_location_candidate({
        "name": "夜南香北京烤鸭",
        "address": "陕西省安康市汉滨区江北办富民街2号",
        "commerceInfo": "15件商品 · 15件返佣",
        "text": "must-not-persist",
    })
    self.assertEqual(candidate["commissionType"], "commission")
    self.assertEqual(candidate["productCount"], 15)
    self.assertEqual(candidate["commissionProductCount"], 15)
    self.assertNotIn("commerceInfo", candidate)
    self.assertNotIn("text", candidate)

def test_same_location_commission_variants_are_filtered_before_ambiguity(self) -> None:
    rows = [
        {"name": "同名店", "address": "北京市朝阳区测试路1号", "commerceInfo": "1件商品 · 1件返佣"},
        {"name": "同名店", "address": "北京市朝阳区测试路1号", "commerceInfo": ""},
    ]
    commission_rows = douyin_commerce_service.normalize_commerce_location_candidates(
        rows, commission_filter="commission"
    )
    self.assertEqual(len(commission_rows), 1)
    self.assertEqual(commission_rows[0]["commissionType"], "commission")
    with self.assertRaisesRegex(douyin_commerce_service.DouyinCommerceError, "重复"):
        douyin_commerce_service.normalize_commerce_location_candidates(
            rows, commission_filter="all"
        )
```

在现有 `apply_saved_commerce_location_to_page` 原子测试旁新增：保存要求为返佣、当前同地点只有无佣时抛出 `publish_location_commission_mismatch`，且 `_apply_open_commerce_location_to_page` 未调用。

- [ ] **Step 2: 分别运行三个单项并确认 RED，遇首个失败即停**

Run:

```bash
.venv/bin/python -m unittest \
  test_douyin_commerce_service.DouyinCommercePayloadTests.test_visible_location_candidate_keeps_structured_commission_only -v
```

修复前预期：缺少 `commissionType`。之后再依次运行另外两个新单项，不把失败堆积后一起处理。

- [ ] **Step 3: 在 DOM 出口和候选归一化接入结构字段**

保留 `_store_option_descriptors()` 当前读取的 `commerceInfo`，但在 `normalize_commerce_location_candidate()` 中立即调用解析器并仅返回：

```python
return {
    **location,
    **parse_commission_summary(value.get("commerceInfo")),
    "source": "douyin-visible-commerce-location",
}
```

`normalize_commerce_location_candidates(rows, *, commission_filter="all")` 必须按以下顺序执行：逐项规范化 → 按筛选过滤 → 以 POI 身份检查重复。不能先因返佣／无佣同名项报重复，再过滤。

- [ ] **Step 4: 正式搜索与稳定等待传入筛选要求**

给 `_wait_for_fresh_commerce_location_results`、`search_commerce_location_store_candidates` 和 `apply_saved_commerce_location_to_page` 增加 `commission_filter`。稳定等待判断目标出现时，使用过滤后的候选；若未过滤的列表存在目标、过滤后不存在，最终固定抛：

```python
raise DouyinCommerceError("publish_location_commission_mismatch")
```

筛选为 `all` 时保持原有地点身份行为。

- [ ] **Step 5: 重跑本任务新增单项和既有地点 DOM 专项**

Run:

```bash
.venv/bin/python -m unittest \
  test_douyin_commerce_service.DouyinCommercePayloadTests \
  test_douyin_commerce_service.DouyinCommerceLocationDomTests -v
```

Expected: 全部通过；若失败，停下修复该模块，不继续下一任务。

- [ ] **Step 6: 提交 Task 2**

```bash
git add app_core/douyin_commerce_service.py test_douyin_commerce_service.py
git commit -m "保留抖音地点返佣候选结构"
```

---

### Task 3: 批次载荷、草稿、续发和任务摘要传递筛选意图

**Files:**
- Modify: `app_core/douyin_commerce_batch_service.py:116-176, 291-326`
- Modify: `app_core/douyin_commerce_batch_draft_service.py:66-174`
- Modify: `app_core/task_service.py:154-190, 513-735`
- Modify: `test_douyin_commerce_batch_service.py`
- Modify: `test_douyin_commerce_batch_draft_service.py`
- Modify: `test_task_service.py`

**Interfaces:**
- Consumes: `normalize_commission_filter`
- Produces per-location fields: `commissionFilter`、`observedCommissionType`、`productCount`、`commissionProductCount`
- Changes draft: `schemaVersion = 4`
- Produces single-item payload: top-level `locationCommissionFilter`

- [ ] **Step 1: 写批次白名单与旧任务兼容 RED 测试**

在批次服务测试的地点预设中加入：

```python
"commissionFilter": "commission",
"observedCommissionType": "commission",
"productCount": 15,
"commissionProductCount": 15,
"commerceInfo": "must-not-survive",
```

断言 `validate_batch_payload` 保留四个结构字段、丢弃 `commerceInfo`；`item_publish_payload` 输出：

```python
self.assertEqual(payload["locationCommissionFilter"], "commission")
self.assertEqual(payload["locationPoi"]["observedCommissionType"], "commission")
```

另测旧地点无字段时 `locationCommissionFilter == "all"`。

- [ ] **Step 2: 运行批次服务新增单项并确认 RED**

Run: `.venv/bin/python -m unittest test_douyin_commerce_batch_service -v`

Expected: 新断言失败；只修本模块后再继续。

- [ ] **Step 3: 实现批次地点白名单与逐条载荷**

`_location_preset()` 对新数据使用严格三值规范化，对缺失字段使用兼容 `all`：

```python
result["commissionFilter"] = normalize_commission_filter(
    value.get("commissionFilter"), default="all"
)
result["observedCommissionType"] = normalize_observed_commission_type(
    value.get("observedCommissionType"), default="unknown"
)
```

数量只接受 `type(value) is int and value >= 0`，否则存为 `None`。`item_publish_payload()` 将筛选值复制到顶层 `locationCommissionFilter`，方便执行器和会话明确消费。

- [ ] **Step 4: 写草稿 v4 与 v3 兼容 RED 测试**

验证：v4 round-trip 保留筛选字段；v3 缺字段升级后为 `all`；原始 `commerceInfo`、DOM 标记和未知字段不落盘；`lastLocationSearch` 增加并恢复 `commissionFilter`，旧草稿默认为 `commission` 只用于新搜索控件，不改写旧逐视频地点的 `all`。

- [ ] **Step 5: 实现草稿 schema v4**

`normalize_batch_draft()` 输出 `schemaVersion: 4`。`_last_location_search()` 返回：

```python
{
    "scope": scope,
    "keyword": keyword,
    "commissionFilter": normalize_commission_filter(
        raw.get("commissionFilter"), default=DEFAULT_COMMISSION_FILTER
    ),
}
```

地点快照使用与批次契约相同的公开字段白名单。

- [ ] **Step 6: 写续发与任务摘要 RED 测试**

在 `test_task_service.py` 验证：暂停任务续发后，每条 `payloadJson` 仍有原 `locationCommissionFilter`；任务的 `locationSummary` 根据当次 `observedCommissionType` 分别追加 `【返佣】`、`【无佣】`；筛选为 `all` 但当次观察明确时仍显示真实观察标签，旧任务没有观察字段时不显示错误标签。

- [ ] **Step 7: 实现任务创建和续发保留**

`_build_douyin_batch_from_pending_payloads()` 重建地点预设时从 `payload["locationCommissionFilter"]` 和 `locationPoi` 复制公开字段。任务摘要使用当次观察类型的固定映射，不把筛选为“全部”误显示成未知：

```python
commission_suffix = {
    "commission": "【返佣】",
    "no_commission": "【无佣】",
}.get(location.get("observedCommissionType"), "")
location_summary = f"{location_name}{commission_suffix}（{location_address}）"
```

- [ ] **Step 8: 运行三模块专项并确认 GREEN**

Run:

```bash
.venv/bin/python -m unittest \
  test_douyin_commerce_batch_service \
  test_douyin_commerce_batch_draft_service \
  test_task_service.DouyinCommerceBatchTaskTests -v
```

Expected: 全部通过。

- [ ] **Step 9: 提交 Task 3**

```bash
git add app_core/douyin_commerce_batch_service.py \
  app_core/douyin_commerce_batch_draft_service.py app_core/task_service.py \
  test_douyin_commerce_batch_service.py test_douyin_commerce_batch_draft_service.py \
  test_task_service.py
git commit -m "保存抖音地点返佣筛选意图"
```

---

### Task 4: 设置页三档筛选、默认返佣和受控自动填入

**Files:**
- Modify: `ui/douyin_commerce_page.py:1361-1400, 1538-1845, 3040-3060, 4138-4250, 4259-4365, 5360-5430, 6680-6710`
- Modify: `test_douyin_commerce_service.py`（`DouyinCommerceBatchUiTests`）

**Interfaces:**
- Consumes: `DEFAULT_COMMISSION_FILTER`、`filter_location_candidates`、`normalize_commission_filter`
- Produces widget: `batch_location_commission_combo` objectName `douyinCommerceBatchCommissionFilter`
- Search state adds: `commissionFilter`、`platformResultCount`

- [ ] **Step 1: 写 UI 默认值、顺序和新批次重置 RED 测试**

```python
def test_batch_location_commission_filter_precedes_scope_and_defaults_to_commission(self) -> None:
    combo = self.page.findChild(QComboBox, "douyinCommerceBatchCommissionFilter")
    self.assertIsNotNone(combo)
    self.assertEqual([combo.itemText(i) for i in range(combo.count())], ["全部", "返佣", "无佣"])
    self.assertEqual(combo.currentData(), "commission")
    self.assertLess(
        self.page.batch_location_title_row.indexOf(combo),
        self.page.batch_location_title_row.indexOf(self.page.batch_location_scope_combo),
    )
```

现有“放弃本次上传重置平台设置”测试增加默认返佣断言。

- [ ] **Step 2: 运行这两个单项并确认 RED**

Run:

```bash
.venv/bin/python -m unittest \
  test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_batch_location_commission_filter_precedes_scope_and_defaults_to_commission -v
```

Expected: 找不到新控件。

- [ ] **Step 3: 创建控件和状态字段**

在地点范围控件前创建：

```python
self.batch_location_commission_combo = QComboBox()
self.batch_location_commission_combo.setObjectName("douyinCommerceBatchCommissionFilter")
self.batch_location_commission_combo.addItem("全部", "all")
self.batch_location_commission_combo.addItem("返佣", "commission")
self.batch_location_commission_combo.addItem("无佣", "no_commission")
self.batch_location_commission_combo.setCurrentIndex(
    self.batch_location_commission_combo.findData(DEFAULT_COMMISSION_FILTER)
)
```

把 `title_row` 保存为 `self.batch_location_title_row` 供稳定 UI 测试。`_batch_location_state()`、草稿保存／恢复、放弃、新批次和整批完成清理同步维护 `commissionFilter`，但普通切换范围不重置它。

- [ ] **Step 4: 写筛选、状态文案和自动填入 RED 测试**

用三条候选（返佣、无佣、unknown）验证：默认只向空视频填入返佣候选；切换无佣后只填用户清空的那条；已有选择始终不变；全部显示三条；筛选为空时没有保存调用，状态包含“平台返回 3 个，但没有符合‘返佣’条件”。

还要断言保存到 `_batch_locations[path]` 的是该视频选择时的 `commissionFilter` 和 `observedCommissionType`，后续切换顶部筛选不改变它。

- [ ] **Step 5: 实现 UI 过滤和候选显示**

`_batch_location_search_succeeded()` 先保存平台原始结构化候选和 `platformResultCount`，再调用纯函数过滤。状态文案使用实际数字：

```python
filtered = filter_location_candidates(raw_candidates, commission_filter)
auto_filled, remaining = self._auto_fill_batch_location_candidates(
    scope, filtered, commission_filter=commission_filter
)
```

下拉项文案追加：

```python
suffix = {
    "commission": "【返佣】",
    "no_commission": "【无佣】",
    "unknown": "【待确认】",
}[candidate["commissionType"]]
```

`_save_batch_location_candidate()` 仍先调用账号级 `save_location_preset()` 保存稳定身份，再只在本批内合并返佣字段；不得修改地点预设数据库 schema。

- [ ] **Step 6: 写“请选择地点”重选与筛选切换回归**

扩展既有 `test_batch_location_placeholder_clears_old_binding_then_next_search_refills`：旧返佣地点清空后切换“无佣”，下一次搜索自动填入无佣地点，并确认另一条已选返佣地点未被覆盖。

- [ ] **Step 7: 运行批量 UI 专项并确认 GREEN**

Run:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest \
  test_douyin_commerce_service.DouyinCommerceBatchUiTests -v
```

Expected: 全部通过；不得启动真实浏览器。

- [ ] **Step 8: 提交 Task 4**

```bash
git add ui/douyin_commerce_page.py test_douyin_commerce_service.py
git commit -m "增加抖音地点返佣筛选界面"
```

---

### Task 5: 正式发布返佣复核、固定诊断与任务明细

**Files:**
- Modify: `app_core/douyin_commerce_session.py:332-390, 1020-1115`
- Modify: `app_core/douyin_commerce_batch_executor.py:130-160, 905-970`
- Verify: `ui/task_page.py:198-320`
- Modify: `test_douyin_commerce_service.py`（`DouyinCommerceSessionContractTests`）
- Modify: `test_douyin_commerce_batch_executor.py`
- Modify: `test_task_page.py`

**Interfaces:**
- Changes: `DouyinCommerceSessionManager.apply_saved_location(session_id, preset, scope, keywords, commission_filter)`
- Consumes top-level: `locationCommissionFilter`
- Allows fixed error: `publish_location_commission_mismatch`

- [ ] **Step 1: 写会话接口与错误码 RED 测试**

在 session contract 测试中传 `commission_filter="commission"`，断言底层 `apply_saved_commerce_location_to_page` 收到同值；底层抛 `publish_location_commission_mismatch` 时，公开异常原样保留固定码且无原始 cause。

- [ ] **Step 2: 运行两个会话单项并确认 RED**

Run:

```bash
.venv/bin/python -m unittest \
  test_douyin_commerce_service.DouyinCommerceSessionContractTests.test_apply_saved_location_updates_session_only_after_atomic_readback -v
```

Expected: 旧接口不接收筛选参数或 mock 未收到参数。

- [ ] **Step 3: 扩展会话接口**

公开与异步内部接口都增加 `commission_filter`，入口严格规范化；调用底层时传入关键字参数。允许码集合增加：

```python
"publish_location_commission_mismatch"
```

成功回读继续只以地点身份写入 session；返佣观察值留在本条任务载荷，不变成长期会话身份。

- [ ] **Step 4: 写批执行器“当前视频失败、下一条继续”RED 测试**

Fake manager 第一条在 `apply_saved_location` 抛 mismatch，第二条返回成功。断言：第一条 `failed`、诊断为固定中文说明并含固定码；第一条未执行 `sync_schedule/preflight/submit`；第二条进入正常提交；两个 session 均在 finally 关闭；每次调用收到对应视频的筛选值。

- [ ] **Step 5: 实现执行器传递与公开诊断**

调用改为：

```python
applied = self._manager.apply_saved_location(
    session_id,
    location,
    scope,
    location_keywords,
    payload.get("locationCommissionFilter", "all"),
)
```

`_PUBLIC_BATCH_DIAGNOSTICS` 增加：

```python
"publish_location_commission_mismatch": (
    "发布定位恢复失败：地点存在，但当前返佣状态与设置时不一致"
    "（错误码 publish_location_commission_mismatch）"
),
```

- [ ] **Step 6: 写任务明细返佣标签 RED 测试**

在 `test_task_page.py` 的批量任务样本把两条 `locationSummary` 设为 `地点【返佣】（地址）` 与 `地点【无佣】（地址）`，断言地点列完整显示且 tooltip 一致，表格仍为六列；不得为返佣新增单独宽列。

- [ ] **Step 7: 运行会话、执行器和任务明细专项**

Run:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest \
  test_douyin_commerce_service.DouyinCommerceSessionContractTests \
  test_douyin_commerce_batch_executor \
  test_task_page -v
```

Expected: 全部通过。

- [ ] **Step 8: 提交 Task 5**

```bash
git add app_core/douyin_commerce_session.py app_core/douyin_commerce_batch_executor.py \
  test_douyin_commerce_service.py \
  test_douyin_commerce_batch_executor.py test_task_page.py
git commit -m "复核抖音发布地点返佣状态"
```

---

### Task 6: 设计覆盖审计与最终一次回归

**Files:**
- Modify only if review finds a concrete gap: files already listed above
- Verify: all touched production and test files

**Interfaces:**
- Consumes all prior task interfaces
- Produces final implementation report and a clean reviewable branch

- [ ] **Step 1: 对照设计逐条静态检查**

逐条确认：三档文案与默认值、无标识归无佣、unknown 只进全部、只填空行、已选不覆盖、每条独立筛选、旧任务 all、正式发布 mismatch、后续视频继续、账号级预设无返佣字段、无原始 DOM 持久化。

- [ ] **Step 2: 扫描敏感字段与占位实现**

Run:

```bash
rg -n "commerceInfo|innerHTML|outerHTML|cookie|verificationCode" \
  app_core/douyin_commerce_location_commission.py \
  app_core/douyin_commerce_batch_service.py \
  app_core/douyin_commerce_batch_draft_service.py \
  ui/douyin_commerce_page.py
```

Expected: `commerceInfo` 仅在当前 DOM 提取／解析入口短暂出现；Cookie、验证码不进入新地点字段和草稿。

- [ ] **Step 3: 运行一次完整回归**

这是整个实施唯一一次完整回归：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -v
```

Expected: 全部通过。若失败，立即停止，修复失败单项并只重跑该单项；修复完成后再运行一次最终完整回归以取得新鲜收尾证据。

- [ ] **Step 4: 编译和差异检查**

```bash
.venv/bin/python -m py_compile \
  app_core/douyin_commerce_location_commission.py \
  app_core/douyin_commerce_service.py \
  app_core/douyin_commerce_session.py \
  app_core/douyin_commerce_batch_service.py \
  app_core/douyin_commerce_batch_draft_service.py \
  app_core/douyin_commerce_batch_executor.py \
  app_core/task_service.py ui/douyin_commerce_page.py ui/task_page.py
git diff --check
```

- [ ] **Step 5: 提交收尾修复或报告**

若无代码修复，不制造空提交；写实施报告并提交：

```bash
git add docs/superpowers/reports/2026-08-12-douyin-commerce-location-commission-filter-report.md
git commit -m "记录抖音地点返佣筛选验收"
```

- [ ] **Step 6: 交付真实账号验证清单，不代替用户执行**

交付三次低风险设置页检查：默认返佣搜索、切无佣搜索、切全部搜索。真实验证只证明平台候选读取和自动填入；未经用户另行明确授权，不上传、不预检、不保存平台草稿、不正式提交。

---

## 实施完成定义

- 每个 Task 都有独立 RED、GREEN 和提交。
- 设置页新批次默认返佣，三档筛选和自动填入规则符合设计。
- 批次草稿、任务、续发和正式发布保持每条视频独立返佣意图。
- 正式发布 mismatch 只失败当前视频，后续继续。
- 旧数据兼容、敏感字段白名单和账号级预设边界通过测试。
- 最终完整回归、`py_compile`、`git diff --check` 通过。
- 未经用户实机授权，不产生任何平台副作用。
