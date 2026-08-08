# 抖音带货地点 DOM 作用域修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复抖音带货地点 portal 把发布页 9 个可编辑字段误算成地点输入框的问题，并对动态“位置”入口增加失败关闭的有限等待。

**Architecture:** 地点输入框由“本地/国内”标签对反向确定最小地点弹层，再要求该弹层内只有一个可编辑字段；不再从全页每个输入框向上寻找共享祖先。动态位置入口只增加有限只读重试，始终只在唯一候选时点击。

**Tech Stack:** Python 3.12、`unittest`、Playwright 1.60、异步 DOM evaluate、PyQt6 离屏自检。

## Global Constraints

- 只修改 `app_core/douyin_commerce_service.py` 与 `test_douyin_commerce_service.py` 的相关逻辑，不做无关重构。
- 当前两个目标文件已有未提交改动，实施时必须保留；不得把这些既有改动一起提交或覆盖。
- 无法唯一确认地点输入框或位置入口时继续安全停止，禁止按序号、placeholder、CSS 瞬态类名或全页候选打分猜测。
- 不启动真实抖音提交，不重新执行 19 条批次，不创建平台草稿或定时任务。
- 新测试必须先在旧代码上出现预期失败，再写生产修复。
- 设计依据：`docs/superpowers/specs/2026-08-08-douyin-location-dom-scope-fix-design.md`。

---

### Task 1: 用真实 DOM 锁定唯一地点 portal

**Files:**
- Modify: `test_douyin_commerce_service.py:970-988`
- Modify: `app_core/douyin_commerce_service.py:985-1070`

**Interfaces:**
- Consumes: `douyin_commerce_service._visible_commerce_search_input(page) -> Locator`
- Produces: 只标记 `[data-oneclick-commerce-search-input="active"]` 的唯一地点字段；歧义时抛出 `DouyinCommerceError`。

- [ ] **Step 1: 写入真实 DOM 失败测试**

在 `test_douyin_commerce_service.py` 导入 `async_playwright`，新增 `DouyinCommerceLocationDomTests(unittest.IsolatedAsyncioTestCase)`。第一项测试包含 8 个无关发布字段及 1 个 portal 地点字段：

```python
from playwright.async_api import async_playwright


class DouyinCommerceLocationDomTests(unittest.IsolatedAsyncioTestCase):
    async def test_location_portal_ignores_eight_other_editor_fields(self) -> None:
        html = """
        <main id="editor-root">
          <input id="title"><textarea id="description"></textarea>
          <input id="tag"><input id="schedule-date"><input id="schedule-time">
          <input id="collaboration"><input id="cover"><input id="other">
          <div data-oneclick-commerce-store="active">输入地理位置</div>
          <section id="location-portal">
            <nav><button>本地</button><button>国内</button></nav>
            <input id="location-input">
          </section>
        </main>
        """
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(html)
                locator = await douyin_commerce_service._visible_commerce_search_input(page)
                self.assertEqual(await locator.get_attribute("id"), "location-input")
                self.assertEqual(
                    await page.locator('[data-oneclick-commerce-search-input="active"]').count(),
                    1,
                )
            finally:
                await browser.close()
```

第二项把 portal 内改为两个输入框，并断言 `_visible_commerce_search_input()` 抛出包含“实际 2 个”的 `DouyinCommerceError`。测试期望值来自手写 DOM，不复用生产筛选逻辑。

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```bash
.venv/bin/python -m unittest -v \
  test_douyin_commerce_service.DouyinCommerceLocationDomTests
```

Expected: 第一项因旧算法报“实际 9 个”而失败；不得因导入、浏览器缺失或测试拼写报错。

- [ ] **Step 3: 最小化改写 portal 选择算法**

在 `_visible_commerce_search_input()` 中删除 `allFields.map(field => panelFor(field))`。保留现有 `visible()`、`editable()` 和 marker，新增以下流程：

```javascript
const editableSelector = 'input, textarea, [contenteditable="true"][role="textbox"]';
const fieldsWithin = panel => Array.from(panel.querySelectorAll(editableSelector)).filter(editable);
const lowestCommonAncestor = (left, right) => {
    const ancestors = new Set();
    for (let current = left; current && current !== document.body; current = current.parentElement) {
        ancestors.add(current);
    }
    for (let current = right; current && current !== document.body; current = current.parentElement) {
        if (ancestors.has(current)) return current;
    }
    return null;
};
if (root.matches(editableSelector) && editable(root)) {
    root.dataset.oneclickCommerceSearchInput = 'active';
    return { count: 1, source: 'direct' };
}
const portalMatches = [];
let observedPortalFieldCount = 0;
for (const local of labelLeaves(document, '本地')) {
    for (const domestic of labelLeaves(document, '国内')) {
        let panel = lowestCommonAncestor(local, domestic);
        for (let depth = 0; panel && panel !== document.body && depth < 8;
            panel = panel.parentElement, depth += 1) {
            if (labelLeaves(panel, '本地').length !== 1
                || labelLeaves(panel, '国内').length !== 1) continue;
            const fields = fieldsWithin(panel);
            if (fields.length > 0 && observedPortalFieldCount === 0) {
                observedPortalFieldCount = fields.length;
            }
            if (fields.length === 1) {
                if (!portalMatches.some(item => item.field === fields[0])) {
                    portalMatches.push({ field: fields[0], panel });
                }
                break;
            }
            if (fields.length > 1) break;
        }
    }
}
if (portalMatches.length > 1) return { count: portalMatches.length };
let matches = portalMatches;
if (matches.length === 0 && observedPortalFieldCount > 0) {
    return { count: observedPortalFieldCount };
}
```

只有没有 portal 候选且没有观察到 portal 字段时才进入原 `anchor-item` 后备；成功后继续设置地点输入框和地点面板 marker。

- [ ] **Step 4: 运行专项测试并确认 GREEN**

Run:

```bash
.venv/bin/python -m unittest -v \
  test_douyin_commerce_service.DouyinCommerceLocationDomTests \
  test_douyin_commerce_service.DouyinCommercePayloadTests.test_location_search_opens_dynamic_input_before_waiting \
  test_douyin_commerce_service.DouyinCommercePayloadTests.test_location_search_reuses_existing_dynamic_input_without_clicking
```

Expected: 4 项全部 `ok`，退出码为 0。

- [ ] **Step 5: 检查差异，不自动提交脏文件**

Run:

```bash
git diff --check -- app_core/douyin_commerce_service.py test_douyin_commerce_service.py
git diff -- app_core/douyin_commerce_service.py test_douyin_commerce_service.py
```

Expected: 无空白错误；目标文件已有实施前改动，因此本任务不执行 `git add` 或 `git commit`，避免把既有改动混入提交。

---

### Task 2: 对动态位置入口增加有限只读等待

**Files:**
- Modify: `test_douyin_commerce_service.py:900-970`
- Modify: `app_core/douyin_commerce_service.py:739-767`

**Interfaces:**
- Consumes: `_mark_unique_position_tag_select(page) -> Locator | None`
- Produces: `_wait_unique_position_tag_select(page, *, attempts: int = 4, interval_ms: int = 150) -> Locator | None`

- [ ] **Step 1: 写入动态入口失败测试**

```python
def test_position_tag_waits_for_unique_dynamic_entry_before_opening_add_tag(self) -> None:
    page = MagicMock()
    page.wait_for_timeout = AsyncMock()
    tag_select = MagicMock()
    position_option = MagicMock()
    position_option.click = AsyncMock()
    missing_anchor = douyin_commerce_service.DouyinCommerceError(
        "抖音页面未找到唯一可用的带货模式控件组（实际 0 个），已安全停止"
    )
    with patch.object(
        douyin_commerce_service, "_anchor_controls", new_callable=AsyncMock,
        side_effect=[missing_anchor, (object(), object(), "带货模式", "")],
    ), patch.object(
        douyin_commerce_service, "_mark_unique_position_tag_select",
        new_callable=AsyncMock, side_effect=[None, None, tag_select],
    ), patch.object(
        douyin_commerce_service, "_open_unique_add_tag", new_callable=AsyncMock,
    ) as open_add_tag, patch.object(
        douyin_commerce_service, "_open_exact_select", new_callable=AsyncMock,
    ), patch.object(
        douyin_commerce_service, "_wait_position_tag_option",
        new_callable=AsyncMock, return_value=position_option,
    ):
        asyncio.run(douyin_commerce_service._ensure_position_tag(page))
    open_add_tag.assert_not_awaited()
    self.assertEqual(page.wait_for_timeout.await_args_list[:2], [call(150), call(150)])
    position_option.click.assert_awaited_once_with(timeout=8_000)
```

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```bash
.venv/bin/python -m unittest -v \
  test_douyin_commerce_service.DouyinCommercePayloadTests.test_position_tag_waits_for_unique_dynamic_entry_before_opening_add_tag
```

Expected: FAIL；旧代码在第一次 `None` 后立即调用 `_open_unique_add_tag()`。

- [ ] **Step 3: 实现有限等待并接入**

```python
async def _wait_unique_position_tag_select(
    page,
    *,
    attempts: int = 4,
    interval_ms: int = 150,
) -> Any | None:
    """短时等待动态位置入口，只返回页面唯一确认的控件。"""

    max_attempts = max(1, attempts)
    for attempt in range(max_attempts):
        tag_select = await _mark_unique_position_tag_select(page)
        if tag_select is not None:
            return tag_select
        if attempt + 1 < max_attempts:
            await page.wait_for_timeout(interval_ms)
    return None
```

首次读取改成 `_wait_unique_position_tag_select(page)`；打开唯一“添加标签”入口后改成 `_wait_unique_position_tag_select(page, attempts=5, interval_ms=200)`。保留所有既有错误、点击和最终锚点回读。

- [ ] **Step 4: 运行动态入口专项并确认 GREEN**

Run:

```bash
.venv/bin/python -m unittest -v \
  test_douyin_commerce_service.DouyinCommercePayloadTests.test_position_tag_waits_for_unique_dynamic_entry_before_opening_add_tag \
  test_douyin_commerce_service.DouyinCommercePayloadTests.test_location_search_opens_dynamic_input_before_waiting
```

Expected: 2 项全部 `ok`，退出码为 0。

- [ ] **Step 5: 运行差异检查**

Run: `git diff --check -- app_core/douyin_commerce_service.py test_douyin_commerce_service.py`

Expected: 退出码为 0；不自动提交既有脏文件。

---

### Task 3: 完整离线回归与安全边界确认

**Files:**
- Verify: `app_core/douyin_commerce_service.py`
- Verify: `test_douyin_commerce_service.py`
- Verify: `test_douyin_commerce_batch_executor.py`
- Verify: `test_douyin_publish_executor.py`
- Verify: `desktop_native_app.py`

**Interfaces:**
- Consumes: Task 1 的地点 portal 唯一选择和 Task 2 的动态入口有限等待。
- Produces: 可交付的离线验证证据；不产生平台侧状态。

- [ ] **Step 1: 运行抖音地点与批量完整回归**

Run:

```bash
.venv/bin/python -m unittest -v \
  test_douyin_commerce_service \
  test_douyin_commerce_batch_executor \
  test_douyin_publish_executor
```

Expected: 全部测试 `OK`，0 failure、0 error。

- [ ] **Step 2: 运行桌面离屏界面自检**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python desktop_native_app.py --ui-test`

Expected: 退出码为 0，输出包含 `NATIVE_DESKTOP_UI_OK`。

- [ ] **Step 3: 运行语法与差异检查**

```bash
.venv/bin/python -m py_compile app_core/douyin_commerce_service.py test_douyin_commerce_service.py
git diff --check
git status --short
```

Expected: 编译与差异检查退出码为 0；不出现新的数据库、账号资料、Cookie、日志或素材。

- [ ] **Step 4: 核对没有平台提交动作**

只读核对本轮命令与任务数据库修改时间。本计划不启动 `desktop_native_app.py --page commerce`、批量执行器、真实抖音页面或任何 `submit` 方法。完成说明必须明确“仅离线修复，未重试 8 条”。

- [ ] **Step 5: 更新项目记忆与会话摘要**

更新 `/Users/andy/Documents/AI/知识库/06_项目记忆/代码迁移优化/00_项目记忆.md`，并在 `会话摘要/` 新增本轮摘要，记录 RED/GREEN 命令、回归结果、修改文件、未重试边界和唯一下一步。

由于生产与测试文件包含实施前既有未提交改动，最终不自动提交这两个文件；将精确差异、验证证据和建议提交信息交给 Andy 审阅。
