# 批量任务结果表格 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将抖音带货批量任务明细改为可筛选的逐视频结果表格，让用户一眼识别每条视频的成功、失败、地点、定时时间及具体原因。

**Architecture:** 仅调整 `TaskDetailDialog` 的展示层，不改数据库、任务执行、发布和重试逻辑。批量任务按 `workflow=douyin-commerce-batch` 走专用六列表格；其他任务继续使用现有十列通用执行项表格。数据仍由 `task_service.get_task()` 返回的 `items` 提供，并按 `batchItemIndex` 排序。

**Tech Stack:** Python 3、PyQt6、unittest、SQLite（只读验收）、现有 `task_service`。

## Global Constraints

- 不触发任何视频重新发布、失败项重试或平台写操作。
- 不修改任务状态、数据表结构或历史任务数据。
- 成功与失败必须来自每条 item 的实际状态，不能从任务总数反推。
- 表格显示短文件名，完整路径和原始结果保留在单元格工具提示中。
- `outputs/` 中的诊断文件和截图不进入 Git。
- 所有新增代码和测试保持 UTF-8。

---

### Task 1: 建立批量任务专用结果表格

**Files:**
- Create: `test_task_page.py`
- Modify: `ui/task_page.py`

- [ ] **Step 1: 写批量任务表格失败测试**

在 `test_task_page.py` 创建最小 `QApplication`，构造一个 `workflow="douyin-commerce-batch"` 的任务，包含一条成功和一条失败 item。断言：

```python
self.assertEqual(
    dialog.findChild(QLabel, "batchResultSummary").text(),
    "共 2 条 · 成功 1 · 失败 1 · 未完成 0",
)

table = dialog.findChild(QTableWidget, "batchResultTable")
self.assertEqual(table.columnCount(), 6)
self.assertEqual(
    [table.horizontalHeaderItem(i).text() for i in range(6)],
    ["序号", "视频", "地点", "定时时间", "状态", "结果说明"],
)
self.assertEqual(table.item(0, 1).text(), "测试24.mp4")
self.assertEqual(table.item(1, 1).text(), "测试13.mp4")
self.assertIn("b24dad24-", table.item(1, 1).toolTip())
```

测试数据需使用乱序 item，验证表格最终按 `batchItemIndex` 升序显示。成功结果说明预期为“平台已回读”，失败结果说明显示 item 的实际 `message`。

- [ ] **Step 2: 运行测试并确认红灯**

Run:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest test_task_page.py -v
```

Expected: FAIL，原因是 `batchResultSummary`、`batchResultTable` 和六列批量表格尚不存在。

- [ ] **Step 3: 实现批量任务展示分支**

在 `TaskDetailDialog` 增加并调用以下小函数：

```python
def _is_batch_task(self) -> bool: ...
def _batch_result_summary(self) -> str: ...
def _batch_video_name(self, row: dict) -> str: ...
def _batch_result_text(self, row: dict) -> str: ...
def _ordered_batch_items(self) -> list[dict]: ...
def _batch_items_tab(self) -> QWidget: ...
def _render_batch_items(self, rows: list[dict]) -> None: ...
```

实现要求：

- 批量任务顶部不再渲染完整 `commerceSummary`，改为对象名 `batchResultSummary` 的单行统计。
- 页签容器对象名为 `taskDetailTabs`，批量首个页签标题为 `视频结果（N）`。
- 表格对象名为 `batchResultTable`，列顺序固定为六列。
- 文件名移除任务生成的 UUID 前缀；完整文件路径或原始文件名写入工具提示。
- 地点和定时时间只读取 item 已保存的 `locationSummary` 与 `scheduleSummary`；字段缺失时显示 `—`，不从其他文本猜测平台状态。
- 成功项结果说明显示“平台已回读”，工具提示保留原始 message；失败和未完成项直接显示实际原因。

- [ ] **Step 4: 运行测试并确认绿灯**

Run:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest test_task_page.py -v
```

Expected: PASS。

- [ ] **Step 5: 提交第一阶段**

```bash
git add ui/task_page.py test_task_page.py
git commit -m "feat: 增加批量任务结果表格"
```

---

### Task 2: 增加状态筛选、失败高亮和自动定位

**Files:**
- Modify: `test_task_page.py`
- Modify: `ui/task_page.py`

- [ ] **Step 1: 写筛选与失败定位失败测试**

补充以下断言：

```python
status_filter = dialog.findChild(QComboBox, "batchStatusFilter")
self.assertEqual(status_filter.itemText(0), "全部（2）")
self.assertIn("失败（1）", [status_filter.itemText(i) for i in range(status_filter.count())])
self.assertEqual(table.currentRow(), 1)
self.assertEqual(table.item(1, 4).foreground().color().name().upper(), "#B42318")
self.assertEqual(table.item(1, 4).background().color().name().upper(), "#FEF3F2")

status_filter.setCurrentText("失败（1）")
self.assertEqual(table.rowCount(), 1)
self.assertEqual(table.item(0, 1).text(), "测试13.mp4")
```

再构造一个非批量任务，断言仍显示 `执行项`、十列表格，且不存在 `batchStatusFilter`。

- [ ] **Step 2: 运行测试并确认红灯**

Run:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest test_task_page.py -v
```

Expected: FAIL，原因是筛选、失败配色和自动定位尚未实现。

- [ ] **Step 3: 实现筛选和失败视觉层级**

在 `TaskDetailDialog` 增加：

```python
def _batch_status_counts(self) -> dict[str, int]: ...
def _populate_batch_status_filter(self, combo: QComboBox) -> None: ...
def _apply_batch_status_filter(self) -> None: ...
def _focus_first_failed(self) -> None: ...
```

实现要求：

- 筛选项为 `全部 / 成功 / 失败 / 执行中 / 未开始`，标签附带实际数量。
- 切换筛选时从保存的完整有序 items 重新渲染，不能修改原始数据。
- 失败行使用浅红背景 `#FEF3F2`；状态和结果说明使用深红 `#B42318`。
- 首次打开时自动选择并滚动到第一条失败项；没有失败项时定位第一行。
- 成功、执行中和未开始保持可读的现有视觉风格，不新增动画。
- 非批量任务完全保留现有表格和交互。

- [ ] **Step 4: 运行针对性测试并确认绿灯**

Run:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest test_task_page.py -v
```

Expected: PASS。

- [ ] **Step 5: 运行现有任务页相关测试**

Run:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest test_task_page.py test_task_detail_dialog.py -v
```

若 `test_task_detail_dialog.py` 不存在，则先用 `rg --files -g 'test*task*py'` 找到并运行仓库中现有任务页测试，不创建无意义占位测试。

Expected: PASS。

- [ ] **Step 6: 提交第二阶段**

```bash
git add ui/task_page.py test_task_page.py
git commit -m "feat: 突出批量任务失败明细"
```

---

### Task 3: 全量回归、历史任务验收与交付

**Files:**
- Modify: `/Users/andy/Documents/AI/知识库/06_项目记忆/代码迁移优化/00_项目记忆.md`
- Create or Modify: `/Users/andy/Documents/AI/知识库/06_项目记忆/代码迁移优化/会话摘要/2026-08-08_2242_一键发批量任务结果表格设计.md`
- Optional local-only artifact: `outputs/batch-task-result-table-task-9.png`

- [ ] **Step 1: 运行全量自动化测试**

Run:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest
```

Expected: 全部 PASS；若仓库测试入口不同，使用项目现有 README/脚本中的正式测试命令并记录实际结果。

- [ ] **Step 2: 运行静态与项目级检查**

Run:

```bash
.venv/bin/python -m py_compile ui/task_page.py test_task_page.py
./scripts/ui-test.sh
git diff --check
git status --short --branch
```

Expected: 编译、UI 测试和 diff 检查通过；`outputs/` 仍保持未跟踪且不暂存。

- [ ] **Step 3: 对历史任务 9 做只读验收**

使用 `task_service.get_task(9)` 构建 `TaskDetailDialog`，不调用任何修改或发布接口。断言：

```python
self.assertEqual(table.rowCount(), 19)
self.assertEqual(table.item(11, 0).text(), "12")
self.assertEqual(table.item(11, 1).text(), "测试13.mp4")
self.assertEqual(table.item(11, 4).text(), "失败")
self.assertIn("地点", table.item(11, 5).text())
```

并确认顶部统计为 19 条、成功 18、失败 1、未完成 0。验收过程只能读取数据库。

- [ ] **Step 4: 生成本地截图并人工检查**

在离屏或本机窗口中打开历史任务 9，保存截图到 `outputs/batch-task-result-table-task-9.png`。检查：

- 首屏能看到统计、筛选器和表格。
- 第 12 条 `测试13.mp4` 自动可见并高亮。
- 失败原因不被截断到无法识别；完整内容可通过工具提示查看。
- 其余 18 条成功项逐条可见。

截图只作本地验收，不加入 Git。

- [ ] **Step 5: 安全重启客户端验证**

先确认没有正在运行的 Playwright/Chromium 发布会话；只有空闲时才重启客户端。重启后再次打开任务 `T08082217-665B`，确认显示仍为 `18 成功 / 1 失败 / 19 总数`，且未触发重试或发布。

- [ ] **Step 6: 更新项目记忆和会话摘要**

记录实际测试结果、界面改动、历史任务 9 的只读验收结果、截图路径、Git 提交和剩余风险。不得写入凭据、Cookie 或平台账号敏感信息。

- [ ] **Step 7: 推送分支并更新现有 PR**

Run:

```bash
git status --short --branch
git log --oneline -5
git push origin codex/douyin-commerce-batch
```

Expected: 推送成功，现有 PR #2 自动包含新提交。再次确认 `outputs/` 未被提交。
