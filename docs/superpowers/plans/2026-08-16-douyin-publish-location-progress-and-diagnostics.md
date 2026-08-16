# Douyin Publish Location Progress and Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让抖音正式发布地点分页在持续取得新候选时不被整段 30 秒期限提前截断，并把真实停止边界写入逐视频任务明细。

**Architecture:** 地点服务把“整段绝对期限”改成“每次平台动作独立 30 秒”，同时继续共享 100 个地点、10 次加载和 3 个关键词硬上限。服务层通过带白名单诊断的受控异常传递失败上下文，会话层原样收敛，批量执行器将用户提示写入 item message，将结构化诊断写入 event detailJson。

**Tech Stack:** Python 3.12、asyncio、Playwright 异步 DOM 操作、SQLite、PyQt6、unittest/IsolatedAsyncioTestCase。

## Global Constraints

- 每次搜索、滚动、加载或回读连续 30 秒未完成才判定单次操作超时。
- 所有关键词仍共享最多 100 个不同地点、10 次实际加载和最多 3 个关键词。
- 完整 POI 身份、返佣校验和点击后回读标准不放宽。
- 新任务明细只保存公开白名单诊断，不保存候选列表、DOM、Cookie、账号文件路径或平台原始异常。
- 第一条失败后第二条继续执行；进程控制异常和会话严格关闭边界保持不变。
- 旧 `publish_location_load_more_limit` 记录继续可读，不迁移历史数据库。

---

### Task 1: 地点分页改为进展驱动的单次操作期限

**Files:**
- Modify: `app_core/douyin_commerce_service.py:72-110, 162-170, 3665-3895`
- Test: `test_douyin_commerce_service.py:4783-5605`

**Interfaces:**
- Produces: `DouyinCommerceError(code: str, diagnostic: Mapping[str, object] | None = None)`，其 `str(error)` 仍只返回固定错误码，`error.diagnostic` 只含受控白名单字段。
- Produces: `apply_saved_commerce_location_to_page(...) -> {"location": dict, "matchedKeyword": str}`；成功接口保持兼容。
- Consumes: `search_commerce_location_store_candidates(...)` 与 `load_more_commerce_location_candidates(...)` 现有返回契约。

- [ ] **Step 1: 写持续进展超过 30 秒仍成功的失败测试**

在 `DouyinCommerceLocationDomTests` 新增测试，使用受控 `monotonic` 和 7 个分页回包：每轮耗时少于 30 秒，但累计耗时超过 30 秒，目标在第 7 次加载出现。

```python
async def test_publish_progressive_pages_do_not_share_one_thirty_second_deadline(self):
    # search 返回首批；每次 load_more 都新增唯一候选；第 7 次加入 preset。
    # monotonic 模拟总耗时超过 30 秒，但任一动作都未超过 30 秒。
    result = await douyin_commerce_service.apply_saved_commerce_location_to_page(...)
    self.assertEqual(load_more.await_count, 7)
    self.assertEqual(result["location"]["poiId"], preset["poiId"])
```

- [ ] **Step 2: 运行单项确认 RED**

Run: `.venv/bin/python -m unittest test_douyin_commerce_service.DouyinCommerceLocationDomTests.test_publish_progressive_pages_do_not_share_one_thirty_second_deadline -v`  
Expected: FAIL，旧代码在第 7 次加载前抛出 `publish_location_load_more_limit`。

- [ ] **Step 3: 写三个明确边界的失败测试**

```python
async def test_publish_reports_action_timeout_with_safe_diagnostic(self):
    with self.assertRaises(douyin_commerce_service.DouyinCommerceError) as caught:
        await douyin_commerce_service.apply_saved_commerce_location_to_page(...)
    self.assertEqual(str(caught.exception), "publish_location_action_timeout")
    self.assertEqual(caught.exception.diagnostic["stage"], "load_more")

async def test_publish_reports_click_limit_with_safe_diagnostic(self):
    # 10 次均有新增，第 11 次前停止。
    self.assertEqual(str(caught.exception), "publish_location_click_limit")
    self.assertEqual(caught.exception.diagnostic["loadMoreClicks"], 10)

async def test_publish_reports_candidate_limit_with_safe_diagnostic(self):
    # 达到 100 个唯一身份后停止。
    self.assertEqual(str(caught.exception), "publish_location_candidate_limit")
    self.assertEqual(caught.exception.diagnostic["candidateCount"], 100)
```

- [ ] **Step 4: 运行三个单项确认 RED**

Run: `.venv/bin/python -m unittest test_douyin_commerce_service.DouyinCommerceLocationDomTests.test_publish_reports_action_timeout_with_safe_diagnostic test_douyin_commerce_service.DouyinCommerceLocationDomTests.test_publish_reports_click_limit_with_safe_diagnostic test_douyin_commerce_service.DouyinCommerceLocationDomTests.test_publish_reports_candidate_limit_with_safe_diagnostic -v`  
Expected: FAIL，旧实现均返回模糊的 `publish_location_load_more_limit`，且无 `diagnostic`。

- [ ] **Step 5: 实现最小受控异常与单次动作期限**

```python
class DouyinCommerceError(RuntimeError):
    def __init__(self, code: str, diagnostic: Mapping[str, object] | None = None):
        super().__init__(code)
        self.code = code
        self.diagnostic = _public_location_diagnostic(diagnostic)

def _location_failure(code, *, stage, keyword, clicks, candidates):
    return DouyinCommerceError(code, {
        "errorCode": code,
        "stage": stage,
        "keyword": keyword,
        "loadMoreClicks": clicks,
        "candidateCount": candidates,
        "candidateLimit": max_candidates,
        "clickLimit": max_load_more_clicks,
        "operationTimeoutSeconds": 30,
    })
```

移除 `apply_saved_commerce_location_to_page` 的单一 `wait_deadline`。每次搜索和每次加载开始时创建独立 30 秒 deadline；成功得到新批次后，下一轮重新创建 deadline。候选数和点击次数继续在所有关键词间共享。

- [ ] **Step 6: 更新旧边界测试的错误码期望**

将只验证旧模糊码的现有测试分别改为断言：

- 10 次：`publish_location_click_limit`
- 100 个候选：`publish_location_candidate_limit`
- 单次动作超时：`publish_location_action_timeout`
- 平台明确穷尽：`publish_location_not_found_after_all_pages`

保留一个 `_public_batch_diagnostic("publish_location_load_more_limit")` 兼容旧任务的测试。

- [ ] **Step 7: 运行 Task 1 定向测试确认 GREEN**

Run: `.venv/bin/python -m unittest test_douyin_commerce_service.DouyinCommerceLocationDomTests -v`  
Expected: 全部 PASS。

- [ ] **Step 8: 提交 Task 1**

```bash
git add app_core/douyin_commerce_service.py test_douyin_commerce_service.py
git commit -m "修复抖音正式发布地点分页提前停止"
```

---

### Task 2: 将安全诊断贯穿会话和批量执行器

**Files:**
- Modify: `app_core/douyin_commerce_session.py:40-52, 1450-1490`
- Modify: `app_core/douyin_commerce_batch_executor.py:150-205, 537-552, 1070-1145, 1310-1360`
- Test: `test_douyin_commerce_service.py:9541-13490`
- Test: `test_douyin_commerce_batch_executor.py:270-900`

**Interfaces:**
- Consumes: `DouyinCommerceError.diagnostic: dict[str, object]` from Task 1.
- Produces: `DouyinCommerceSessionError(code, diagnostic=None)` with the same fixed string/error-diagnostic boundary.
- Produces: `DouyinCommerceBatchExecutor._record_progress(..., readback: Mapping[str, object] | None = None)`.

- [ ] **Step 1: 写会话层诊断保留的失败测试**

```python
def test_apply_saved_location_preserves_public_failure_diagnostic(self):
    error = DouyinCommerceError("publish_location_candidate_limit", {...})
    with self.assertRaises(DouyinCommerceSessionError) as caught:
        manager.apply_saved_location(...)
    self.assertEqual(str(caught.exception), "publish_location_candidate_limit")
    self.assertEqual(caught.exception.diagnostic["candidateCount"], 100)
```

- [ ] **Step 2: 运行单项确认 RED**

Run: `.venv/bin/python -m unittest test_douyin_commerce_service.DouyinCommerceSessionContractTests.test_apply_saved_location_preserves_public_failure_diagnostic -v`  
Expected: FAIL，当前会话异常只保留字符串。

- [ ] **Step 3: 实现会话层固定码与诊断投影**

扩展 `DouyinCommerceSessionError`，允许携带已由服务层净化的诊断；`apply_saved_location` 仅允许新的四个固定码和现有固定码，未知错误仍映射到 `publish_location_click_failed`，不得保留原始 cause。

- [ ] **Step 4: 写批量执行器提示和 readback 的失败测试**

```python
def test_location_limit_failure_persists_exact_public_diagnostic_and_continues(self):
    # 第一条抛带 diagnostic 的 session error，第二条成功。
    self.assertEqual([row["status"] for row in result], ["failed", "published"])
    failed = task_service.get_task(task_id)
    self.assertIn("累计 100 个地点", failed["items"][0]["message"])
    self.assertEqual(json.loads(failed["events"][...]["detailJson"])["errorCode"],
                     "publish_location_candidate_limit")
```

- [ ] **Step 5: 运行单项确认 RED**

Run: `.venv/bin/python -m unittest test_douyin_commerce_batch_executor.DouyinCommerceBatchExecutorTests.test_location_limit_failure_persists_exact_public_diagnostic_and_continues -v`  
Expected: FAIL，当前 `_record_progress` 固定写入 `{}`。

- [ ] **Step 6: 实现批量提示格式化和诊断传递**

新增公开格式化器，按错误码生成明确文案，例如：

```python
"发布定位恢复失败：累计检查 100 个不同地点、加载 7 次仍未命中目标（错误码 publish_location_candidate_limit）"
```

执行器捕获异常时只读取 `type(diagnostic) is dict` 的白名单字段；item message 使用公开格式化文本，event readback 使用结构化诊断。无诊断的旧异常继续使用 `_PUBLIC_BATCH_DIAGNOSTICS`。

- [ ] **Step 7: 运行 Task 2 定向测试确认 GREEN**

Run: `.venv/bin/python -m unittest test_douyin_commerce_service.DouyinCommerceSessionContractTests test_douyin_commerce_batch_executor.DouyinCommerceBatchExecutorTests -v`  
Expected: 全部 PASS。

- [ ] **Step 8: 提交 Task 2**

```bash
git add app_core/douyin_commerce_session.py app_core/douyin_commerce_batch_executor.py test_douyin_commerce_service.py test_douyin_commerce_batch_executor.py
git commit -m "贯穿抖音地点失败诊断"
```

---

### Task 3: 任务数据库白名单与详情验收

**Files:**
- Modify: `app_core/task_service.py:35-102, 1440-1525`
- Modify: `ui/task_page.py:195-215, 335-368` only if structured details need readable formatting
- Test: `test_task_service.py:1200-1405`
- Test: `test_task_page.py:17-180`

**Interfaces:**
- Consumes: Task 2 的 `readback` 结构化诊断。
- Produces: `_batch_readback_projection(readback) -> dict[str, str | int]`，只允许设计中的 9 个诊断字段和既有平台回执字段。
- Produces: `TaskDetailDialog` 继续用逐视频 `message` 显示结果，并在事件详情中显示净化后的 JSON。

- [ ] **Step 1: 写诊断白名单落库的失败测试**

```python
def test_location_failure_diagnostic_is_whitelisted_before_persistence(self):
    task_service.mark_batch_item_result(..., ok=False, readback={
        "errorCode": "publish_location_candidate_limit",
        "stage": "load_more",
        "keyword": "夜南香",
        "loadMoreClicks": 7,
        "candidateCount": 100,
        "cookie": "must-not-store",
        "dom": "must-not-store",
    })
    detail = json.loads(task_service.get_task(task_id)["events"][-1]["detailJson"])
    self.assertEqual(detail["candidateCount"], 100)
    self.assertNotIn("cookie", detail)
    self.assertNotIn("dom", detail)
```

- [ ] **Step 2: 运行单项确认 RED**

Run: `.venv/bin/python -m unittest test_task_service.DouyinCommerceBatchTaskTests.test_location_failure_diagnostic_is_whitelisted_before_persistence -v`  
Expected: FAIL，当前投影丢弃所有诊断字段和整数。

- [ ] **Step 3: 实现严格类型白名单**

字符串字段仅接受非空内建 `str`；计数字段仅接受非负内建 `int`，拒绝 `bool`、浮点数、字符串数字、自定义对象和未知字段。平台成功回执原有规则不改变。

- [ ] **Step 4: 写任务详情的失败提示测试**

```python
def test_batch_failure_row_and_event_show_exact_location_limit(self):
    dialog = TaskDetailDialog(task_with_location_diagnostic)
    self.assertIn("累计检查 100 个不同地点", dialog.batch_result_table.item(0, 5).text())
    event_table = dialog.findChildren(QTableWidget)[...]
    self.assertIn("publish_location_candidate_limit", event_table.item(0, 4).text())
```

- [ ] **Step 5: 运行单项确认 RED 或确认现有 UI 已满足**

Run: `.venv/bin/python -m unittest test_task_page.TaskDetailDialogTests.test_batch_failure_row_and_event_show_exact_location_limit -v`  
Expected: 若现有控件已直接展示新的 message/detailJson，则测试可直接 PASS；此时不修改 `ui/task_page.py`。若 FAIL，只做使固定文案可见的最小 UI 修改。

- [ ] **Step 6: 运行 Task 3 定向测试确认 GREEN**

Run: `.venv/bin/python -m unittest test_task_service.DouyinCommerceBatchTaskTests test_task_page.TaskDetailDialogTests -v`  
Expected: 全部 PASS。

- [ ] **Step 7: 提交 Task 3**

```bash
git add app_core/task_service.py ui/task_page.py test_task_service.py test_task_page.py
git commit -m "完善抖音地点失败任务明细"
```

---

### Task 4: 受影响模块验证与交付检查

**Files:**
- Verify only: `app_core/douyin_commerce_service.py`
- Verify only: `app_core/douyin_commerce_session.py`
- Verify only: `app_core/douyin_commerce_batch_executor.py`
- Verify only: `app_core/task_service.py`
- Verify only: `ui/task_page.py`

**Interfaces:**
- Consumes: Tasks 1–3 全部接口。
- Produces: 可提交的本地交付证据；不产生真实平台发布证据。

- [ ] **Step 1: 运行地点、会话、执行器、任务记录相关组合**

Run:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest \
  test_douyin_commerce_service.DouyinCommerceLocationDomTests \
  test_douyin_commerce_service.DouyinCommerceSessionContractTests \
  test_douyin_commerce_batch_executor.DouyinCommerceBatchExecutorTests \
  test_task_service.DouyinCommerceBatchTaskTests \
  test_task_page.TaskDetailDialogTests -v
```

Expected: 全部 PASS，退出码 0。发现首个失败立即停止，先修该单项，再重新运行该组合。

- [ ] **Step 2: 编译与差异检查**

Run:

```bash
.venv/bin/python -m py_compile \
  app_core/douyin_commerce_service.py \
  app_core/douyin_commerce_session.py \
  app_core/douyin_commerce_batch_executor.py \
  app_core/task_service.py ui/task_page.py
git diff --check
git status --short
```

Expected: 编译和 diff check 退出码 0；只包含本轮文件与用户原有 `.superpowers/brainstorm/`、`outputs/` 未跟踪目录。

- [ ] **Step 3: 运行最终必要回归**

根据相关组合结果和改动风险，运行一次：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest \
  test_douyin_commerce_service \
  test_douyin_commerce_batch_executor \
  test_task_service test_task_page -v
```

Expected: 全部 PASS，退出码 0。该证据仅证明受影响模块，不声称真实抖音平台已验证。

- [ ] **Step 4: 最终提交**

```bash
git add app_core/douyin_commerce_service.py app_core/douyin_commerce_session.py \
  app_core/douyin_commerce_batch_executor.py app_core/task_service.py ui/task_page.py \
  test_douyin_commerce_service.py test_douyin_commerce_batch_executor.py \
  test_task_service.py test_task_page.py
git commit -m "修复抖音地点分页并记录具体失败原因"
```
