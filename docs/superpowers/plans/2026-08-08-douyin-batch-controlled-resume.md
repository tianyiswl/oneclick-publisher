# 抖音带货批量受控续发 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为用户手动暂停的抖音带货批量任务创建可审计的续发子任务，并且只在用户当次确认后重新提交原任务未开始的视频。

**Architecture:** 续发不修改来源任务，而是从其保存的逐视频 payload 快照重建一个仅含 `pending` 视频的批次信封，创建 `oneclick_resume` 子任务并写入来源关联。服务层负责资格、素材、账号和北京时间排期校验；任务详情只提出续发请求，主窗口把请求路由至抖音带货页的独立确认弹窗，确认后才创建子任务并复用现有批量执行器。

**Tech Stack:** Python 3、SQLite、PyQt6、unittest、现有 `app_core.douyin_commerce_batch_service` 与 `DouyinCommerceBatchExecutor`。

## Global Constraints

- 只允许 `workflow == "douyin-commerce-batch"`、`status == "paused"`、`pauseReasonCode == "user_request"` 的任务续发；缺少暂停代码的历史任务一律拒绝。
- 只续发来源任务中 `publish_task_items.status == "pending"` 的视频；不得自动重试 `failed`、验证中或回执待核对的视频。
- 续发必须新建 `mode == "oneclick_resume"` 子任务，写入 `resumeSourceTaskId`；来源任务的状态、计数、条目和错误信息不可改写。
- 每条待续发视频必须重新通过本地素材、地点、音乐、声明、账号和北京时间排期校验；定时时间过期即拒绝，绝不自动改期。
- 任务详情不启动浏览器或执行器；只有抖音带货页的“确认继续发布 N 条”动作才能创建子任务并启动既有批量执行器。
- 开发验证仅使用本地数据库、离屏 Qt 和模拟执行器；不调用任何真实平台最终提交。
- 所有新增用户文案使用中文，所有新增代码注释使用中文；不写入账号凭据、Cookie 或平台会话。

---

## 文件结构与职责

- `app_core/database.py`：为既有 SQLite 数据库增加可向前迁移的 `pauseReasonCode` 与 `resumeSourceTaskId` 列。
- `app_core/task_service.py`：定义暂停原因常量、恢复准备/创建服务、来源与子任务审计事件，以及批次原序号保留接口。
- `app_core/douyin_commerce_batch_executor.py`：为每个既有暂停分支写入明确的暂停原因代码。
- `ui/task_page.py`：仅在可续发来源任务详情中显示“继续未开始的 N 条”，并向上层发出任务 ID。
- `ui/main_window.py`：把任务详情的续发请求切换并路由至抖音带货页，不在任务页拥有执行权限。
- `ui/douyin_commerce_page.py`：展示续发确认对话框、在确认后创建子任务并交给既有执行器。
- `test_task_service.py`：覆盖数据库迁移、资格拒绝、纯准备、子任务创建、原序号和来源不可变性。
- `test_douyin_commerce_batch_executor.py`：回归所有暂停分支写入的原因代码。
- `test_task_page.py`：验证详情按钮的可见性和仅发出请求的边界。
- `test_douyin_commerce_service.py`：验证续发确认前无创建/无执行，确认后才创建并调用现有启动方法。
- `test_main_window.py`：验证主窗口将任务 ID 路由至抖音带货页。

### Task 1: 任务数据迁移与受控续发服务

**Files:**
- Modify: `app_core/database.py:200-280`
- Modify: `app_core/task_service.py:1-560`
- Modify: `test_task_service.py`

**Interfaces:**
- Produces: `PAUSE_REASON_USER_REQUEST = "user_request"`、`PAUSE_REASON_WAITING_LOGIN = "waiting_login"`、`PAUSE_REASON_WAITING_VERIFICATION = "waiting_verification"`、`PAUSE_REASON_RECEIPT_AMBIGUOUS = "receipt_ambiguous"`、`PAUSE_REASON_AUTO_FAILURE = "auto_failure"`。
- Produces: `mark_task_paused(task_id: int, message: str, *, pause_reason_code: str) -> None`，对未知代码抛出 `ValueError("未知的批量暂停原因")`。
- Produces: `prepare_douyin_batch_resume(task_id: int, *, now: datetime) -> dict[str, object]`，始终返回 `resumeAllowed: bool`、`blockedReason: str`、`pendingCount: int`、`sourceTaskId: int`、`sourceTaskNo: str`、`itemIndexes: list[int]`；可续发时额外返回 `batch: dict[str, object]`。
- Produces: `create_douyin_batch_resume(task_id: int, *, now: datetime) -> dict[str, object]`；成功返回 `{"task": dict, "batch": dict, "sourceTaskNo": str, "itemIndexes": list[int]}`，拒绝时抛出 `ValueError(blockedReason)`。
- Produces: `create_douyin_batch_task(batch: dict, mode: str = "oneclick_publish", *, schedule_now=None, resume_source_task_id: int | None = None, batch_item_indexes: list[int] | None = None) -> dict`；当传入 `batch_item_indexes` 时长度必须等于 `items`、元素为互异正整数，并用其写入子任务 `batchItemIndex`。

- [ ] **Step 1: 为迁移、资格和子任务不可变性写失败测试**

在 `test_task_service.py` 增加 `from datetime import datetime` 与 `from zoneinfo import ZoneInfo`，并使用临时目录真实 `.mp4` 文件的 `_create_paused_douyin_batch_source()`。先调用 `account_service.save_oneclick_authorized_account(3, "续发测试主体", "resume-account.json", display_name="续发测试账号")` 写入可用账号，再创建包含三个视频、有效 `locationPoi`、`selectedMusic`、`contentDeclaration`、`accountFile == "resume-account.json"` 和未来 `scheduleTime` 的批次；把第 1 条标为 `success`、第 2、3 条保留 `pending`，并通过 `mark_task_paused(source["id"], "用户主动暂停", pause_reason_code=PAUSE_REASON_USER_REQUEST)` 写入暂停。

```python
def test_prepare_douyin_batch_resume_only_includes_pending_source_items(self):
    source = self._create_paused_douyin_batch_source()

    prepared = task_service.prepare_douyin_batch_resume(
        source["id"], now=datetime(2026, 8, 8, 21, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    )

    self.assertTrue(prepared["resumeAllowed"])
    self.assertEqual(prepared["itemIndexes"], [2, 3])
    self.assertEqual([item["filePath"] for item in prepared["batch"]["items"]], self.media_paths[1:])
    self.assertEqual([item["scheduleTime"] for item in prepared["batch"]["items"]], ["2026-08-09 16:30", "2026-08-09 17:00"])

def test_create_douyin_batch_resume_keeps_source_unchanged_and_preserves_indexes(self):
    source = self._create_paused_douyin_batch_source()
    source_before = task_service.get_task(source["id"])

    created = task_service.create_douyin_batch_resume(
        source["id"], now=datetime(2026, 8, 8, 21, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    )

    child = task_service.get_task(created["task"]["id"])
    self.assertEqual(child["mode"], "oneclick_resume")
    self.assertEqual(child["resumeSourceTaskId"], source["id"])
    self.assertEqual([item["batchItemIndex"] for item in child["items"]], [2, 3])
    self.assertEqual(task_service.get_task(source["id"])["items"], source_before["items"])
```

同一测试类加入参数化子测试，分别写入 `waiting_login`、`waiting_verification`、`receipt_ambiguous`、`auto_failure` 和空暂停代码；断言 `prepare["resumeAllowed"] is False`、`create_douyin_batch_resume` 抛出，且 `list_tasks()` 数量不变。再加入“无 pending 条目”“任一待续发素材被删除”“任一定时等于或早于 `now`”三个拒绝测试。

- [ ] **Step 2: 运行失败测试确认服务尚不存在**

Run: `python -m unittest test_task_service.DouyinCommerceBatchTaskTests.test_prepare_douyin_batch_resume_only_includes_pending_source_items test_task_service.DouyinCommerceBatchTaskTests.test_create_douyin_batch_resume_keeps_source_unchanged_and_preserves_indexes -v`

Expected: FAIL，提示 `prepare_douyin_batch_resume` 或 `create_douyin_batch_resume` 未定义，或缺少新数据库列。

- [ ] **Step 3: 实现数据库列和暂停原因契约**

在 `app_core/database.py` 的 `publish_tasks` 初始建表 SQL 中加入两列，并在既有 `_add_columns` 迁移映射中加入相同列，保证旧任务不会丢失：

```python
pauseReasonCode TEXT,
resumeSourceTaskId INTEGER,
```

在 `app_core/task_service.py` 顶部定义不可变集合：

```python
PAUSE_REASON_USER_REQUEST = "user_request"
PAUSE_REASON_WAITING_LOGIN = "waiting_login"
PAUSE_REASON_WAITING_VERIFICATION = "waiting_verification"
PAUSE_REASON_RECEIPT_AMBIGUOUS = "receipt_ambiguous"
PAUSE_REASON_AUTO_FAILURE = "auto_failure"
DOUYIN_BATCH_PAUSE_REASONS = frozenset({
    PAUSE_REASON_USER_REQUEST,
    PAUSE_REASON_WAITING_LOGIN,
    PAUSE_REASON_WAITING_VERIFICATION,
    PAUSE_REASON_RECEIPT_AMBIGUOUS,
    PAUSE_REASON_AUTO_FAILURE,
})
```

把 `mark_task_paused` 改为先验证 `pause_reason_code in DOUYIN_BATCH_PAUSE_REASONS`，再用单条 SQL 同时写 `status = 'paused'` 和 `pauseReasonCode = ?`，并把事件消息保留为人可读文本；不要用旧事件文案反推原因。

- [ ] **Step 4: 实现纯准备函数和子任务创建函数**

在 `task_service.py` 紧邻 `create_douyin_batch_task` 新增两个内部帮助函数：`_load_douyin_batch_resume_source(task_id: int) -> tuple[dict, list[dict]]` 和 `_build_douyin_batch_from_pending_payloads(source: dict, pending_items: list[dict], now: datetime) -> tuple[dict, list[int]]`。同时使用下列固定的拒绝结果构造函数：

```python
def _resume_blocked(task_id: int, reason: str) -> dict[str, object]:
    return {"resumeAllowed": False, "blockedReason": reason, "pendingCount": 0,
            "sourceTaskId": int(task_id), "sourceTaskNo": "", "itemIndexes": []}
```

`_load_douyin_batch_resume_source` 必须从 `get_task(task_id)` 读取来源任务与条目，解析 `payloadJson` 的逐视频 payload 列表，并按条目 `batchItemIndex` 关联对应快照。它只接受 `workflow == "douyin-commerce-batch"`、`status == "paused"` 和 `pauseReasonCode == PAUSE_REASON_USER_REQUEST`。在 `task_service.py` 导入 `from . import account_service`；对每个 `pending` 条目检查：文件存在、`locationPoi` 为含 `name` 的字典、`musicMode`/`selectedMusic`/`contentDeclaration` 非空、`enableTimer is True` 时 `scheduleTime` 严格晚于 `now`；账号则以 `account_service.list_accounts()` 中 `type == 3`、`status == 1` 且 `filePath` 等于 payload `accountFile` 的记录确认。

`_build_douyin_batch_from_pending_payloads` 从第一个 pending 快照复制共享字段 `type`、`workflow`、`batchWorkflow`、`commerceMode`、`contentType`、`accountList`、`title`、`description`、`tags`、`selectedMusic`、`musicMode`、`contentDeclaration`，只把 pending 快照中的 `fileList`、地点和原始定时构造成 `items`。它调用 `prepare_batch_for_execution(batch, now=now)`，并返回其中的 `items` 原 `batchItemIndex` 列表；任何验证异常都转成 `resumeAllowed=False` 的明确中文 `blockedReason`。

`prepare_douyin_batch_resume` 只能读取与构造字典，不能调用 `create_pending_task`、`connect().execute`、浏览器或执行器。`create_douyin_batch_resume` 必须重新调用 `prepare_douyin_batch_resume`；允许时调用：

```python
child = create_douyin_batch_task(
    prepared["batch"],
    mode="oneclick_resume",
    schedule_now=now,
    resume_source_task_id=int(prepared["sourceTaskId"]),
    batch_item_indexes=list(prepared["itemIndexes"]),
)
```

然后在同一数据库事务中：更新子任务 `resumeSourceTaskId`，给来源任务写 `eventType='batch_resume_created'` 事件（含子任务号和条数），给子任务写同名事件（含来源任务号及 `2、3` 形式的原序号）。不要更新来源任务任何字段。

- [ ] **Step 5: 运行任务服务测试确认通过**

Run: `python -m unittest test_task_service -v`

Expected: PASS，新增测试证明只复制 pending 项、拒绝四类非手动暂停和历史任务、不会自动改期，且来源任务逐项数据保持完全相同。

- [ ] **Step 6: 提交任务服务变更**

```bash
git add app_core/database.py app_core/task_service.py test_task_service.py
git commit -m "feat: 增加抖音批量受控续发服务"
```

### Task 2: 为既有暂停分支写入准确的暂停原因

**Files:**
- Modify: `app_core/douyin_commerce_batch_executor.py:450-535`
- Modify: `test_douyin_commerce_batch_executor.py:430-575`

**Interfaces:**
- Consumes: Task 1 的五个 `PAUSE_REASON_*` 常量与强制关键字参数的 `mark_task_paused`。
- Produces: 每次批量执行暂停时，任务记录均有与实际分支相符的 `pauseReasonCode`。

- [ ] **Step 1: 为五个分支补充失败断言**

在 `test_douyin_commerce_batch_executor.py` 为现有手动暂停、`waiting_login`、`waiting_verification`/`verification_failed`、`receipt_ambiguous` 和连续五条 `failed` 测试各加一条读取任务后的断言：

```python
task = task_service.get_task(task_id)
self.assertEqual(task["status"], "paused")
self.assertEqual(task["pauseReasonCode"], task_service.PAUSE_REASON_USER_REQUEST)
```

对验证失败用 `PAUSE_REASON_WAITING_VERIFICATION`，对回执不明用 `PAUSE_REASON_RECEIPT_AMBIGUOUS`，对连续失败用 `PAUSE_REASON_AUTO_FAILURE`。等待登录或验证的分支还应断言后续条目保持 `pending`。

- [ ] **Step 2: 运行失败测试确认现有分支未分类**

Run: `python -m unittest test_douyin_commerce_batch_executor -v`

Expected: FAIL，等待登录/验证分支未落库为 `paused` 或 `pauseReasonCode` 为空。

- [ ] **Step 3: 以最小改动给每个分支传入常量**

在执行器导入 `task_service` 常量或通过已注入的 `self._task_store` 常量使用；每个 `mark_task_paused` 调用都传入对应关键字。对当前仅设置内存 `paused=True` 的等待登录、等待验证和验证失败分支，在设置 `paused_result_status = "pending"` 的同一分支调用：

```python
self._task_store.mark_task_paused(
    task_id,
    "等待用户处理验证或登录，未开始后续视频",
    pause_reason_code=task_service.PAUSE_REASON_WAITING_VERIFICATION,
)
```

等待登录改为 `PAUSE_REASON_WAITING_LOGIN`；用户点击暂停为 `PAUSE_REASON_USER_REQUEST`；回执不明为 `PAUSE_REASON_RECEIPT_AMBIGUOUS`；连续失败为 `PAUSE_REASON_AUTO_FAILURE`。不要改变结果条目的既有 `waiting_*`、`receipt_ambiguous`、`pending` 或 `paused` 状态语义。

- [ ] **Step 4: 运行执行器回归测试确认通过**

Run: `python -m unittest test_douyin_commerce_batch_executor -v`

Expected: PASS，所有暂停来源都留有可读事件和可机器判断的原因代码。

- [ ] **Step 5: 提交执行器暂停分类**

```bash
git add app_core/douyin_commerce_batch_executor.py test_douyin_commerce_batch_executor.py
git commit -m "fix: 记录抖音批量暂停原因"
```

### Task 3: 任务详情的资格提示和无执行权限请求

**Files:**
- Modify: `ui/task_page.py:1-330, 650-700`
- Create: `test_main_window.py`
- Modify: `test_task_page.py`
- Modify: `ui/main_window.py:345-375`

**Interfaces:**
- Consumes: `task_service.prepare_douyin_batch_resume(task_id, now=current_shanghai_time())` 的 `resumeAllowed`、`pendingCount` 和 `blockedReason`；`current_shanghai_time` 从 `app_core.douyin_commerce_batch_service` 导入。
- Produces: `TaskDetailDialog.resume_douyin_batch_requested = pyqtSignal(int)`；`TaskPage.resume_douyin_batch_requested = pyqtSignal(int)`；`MainWindow._open_douyin_batch_resume(task_id: int) -> None`。

- [ ] **Step 1: 为按钮可见性和路由写失败测试**

在 `test_task_page.py` 使用 `unittest.mock.patch("ui.task_page.task_service.prepare_douyin_batch_resume")`，令批次任务分别得到允许和拒绝结果：

```python
allowed.return_value = {"resumeAllowed": True, "pendingCount": 2, "blockedReason": ""}
dialog = TaskDetailDialog(_batch_task())
self.assertTrue(dialog.resume_batch_button.isVisible())
self.assertEqual(dialog.resume_batch_button.text(), "继续未开始的 2 条")

blocked.return_value = {"resumeAllowed": False, "pendingCount": 0, "blockedReason": "仅支持用户主动暂停的批次"}
dialog = TaskDetailDialog(_batch_task())
self.assertFalse(dialog.resume_batch_button.isVisible())
```

再用 `QSignalSpy` 或列表回调点击允许按钮，断言只收到来源任务 ID，且 `task_service.create_douyin_batch_resume` 和任何 `DouyinCommerceBatchExecutor.run_publish` 未被调用。新增 `test_main_window.py`，用 mock 的 `TaskPage` 信号触发后断言主窗口当前页为“抖音带货”，且只调用 `douyin_commerce.open_batch_resume(task_id)` 一次。

- [ ] **Step 2: 运行失败测试确认控件与路由尚不存在**

Run: `python -m unittest test_task_page test_main_window -v`

Expected: FAIL，缺少 `resume_batch_button`、续发信号或主窗口路由方法。

- [ ] **Step 3: 实现任务详情按钮、信号和主窗口路由**

在 `ui/task_page.py` 把 `from PyQt6.QtCore import Qt` 改为 `from PyQt6.QtCore import Qt, pyqtSignal`。在 `TaskDetailDialog` 声明 `resume_douyin_batch_requested = pyqtSignal(int)`。从 `app_core.douyin_commerce_batch_service` 导入 `current_shanghai_time`，构造批次详情后调用只读准备服务；当且仅当 `resumeAllowed is True` 且 `pendingCount > 0` 时创建：

```python
self.resume_batch_button = button(f"继续未开始的 {pending_count} 条", variant="primary")
self.resume_batch_button.setObjectName("douyinCommerceResumeBatch")
self.resume_batch_button.clicked.connect(
    lambda: self.resume_douyin_batch_requested.emit(int(self.task["id"]))
)
```

把按钮放在明细底部关闭按钮左侧。`TaskPage` 同样声明该信号；在 `open_detail_by_id` 创建对话框后连接 `dialog.resume_douyin_batch_requested` 到 `self.resume_douyin_batch_requested.emit`，再 `exec()`。`TaskDetailDialog` 不导入执行器、不调用创建服务、也不切换页面。

在 `MainWindow.__init__` 连接：

```python
self.tasks.resume_douyin_batch_requested.connect(self._open_douyin_batch_resume)
```

实现 `_open_douyin_batch_resume`：查找 `self.page_definitions` 中标签为 `"抖音带货"` 的索引并调用 `_set_current_page(index)`，随后调用 `self.douyin_commerce.open_batch_resume(int(task_id))`。该方法不得创建任务。

- [ ] **Step 4: 运行任务详情和路由测试确认通过**

Run: `python -m unittest test_task_page test_main_window -v`

Expected: PASS，只有服务明确允许时可见按钮，点击只携带任务 ID 进入抖音带货页。

- [ ] **Step 5: 提交任务详情路由**

```bash
git add ui/task_page.py ui/main_window.py test_task_page.py test_main_window.py
git commit -m "feat: 增加抖音批量续发入口"
```

### Task 4: 抖音带货页二次确认与子任务启动

**Files:**
- Modify: `ui/douyin_commerce_page.py:280-360, 4110-4160`
- Modify: `test_douyin_commerce_service.py`

**Interfaces:**
- Consumes: Task 1 的 `prepare_douyin_batch_resume` 和 `create_douyin_batch_resume` 返回值；Task 3 的 `MainWindow._open_douyin_batch_resume` 路由。
- Produces: `DouyinCommerceBatchResumeConfirmDialog(plan: dict, parent=None)` 与 `DouyinCommercePage.open_batch_resume(task_id: int) -> None`。

- [ ] **Step 1: 写确认前零副作用和确认后启动的失败测试**

在 `test_douyin_commerce_service.py` patch `task_service.prepare_douyin_batch_resume` 返回：

```python
prepared = {
    "resumeAllowed": True, "blockedReason": "", "pendingCount": 2,
    "sourceTaskId": 41, "sourceTaskNo": "T0808-0041", "itemIndexes": [2, 3],
    "batch": {"workflow": "douyin-commerce-batch", "items": [{"scheduleTime": "2026-08-09 16:30"}, {"scheduleTime": "2026-08-09 17:00"}]},
}
```

先让 `DouyinCommerceBatchResumeConfirmDialog.exec` 返回 `QDialog.DialogCode.Rejected`：

```python
page.open_batch_resume(41)
create_resume.assert_not_called()
start_batch_publish.assert_not_called()
```

再让 `exec` 返回 `Accepted`，并让 `create_douyin_batch_resume` 返回 `{"task": {"id": 99, "mode": "oneclick_resume"}, "batch": prepared["batch"], "sourceTaskNo": "T0808-0041", "itemIndexes": [2, 3]}`；断言只调用一次 `create_douyin_batch_resume(41, now=ANY)`，随后只调用一次 `start_batch_publish(prepared["batch"], created["task"])`。第三个测试让准备服务返回 `resumeAllowed=False`，断言显示本地警告、不会展示确认弹窗、不会创建/启动。

- [ ] **Step 2: 运行失败测试确认续发 UI 不存在**

Run: `python -m unittest test_douyin_commerce_service -v`

Expected: FAIL，`open_batch_resume` 或 `DouyinCommerceBatchResumeConfirmDialog` 未定义。

- [ ] **Step 3: 实现专用确认对话框和受控页面入口**

在 `DouyinCommerceBatchConfirmDialog` 附近新建 `DouyinCommerceBatchResumeConfirmDialog`，不复用普通新建批次的标题与确认文字。它必须展示：

```python
QLabel(f"来源任务：{plan['sourceTaskNo']}")
QLabel(f"仅继续原批次第 {'、'.join(map(str, plan['itemIndexes']))} 条，共 {plan['pendingCount']} 条")
QLabel("将重新打开浏览器并重新校验音乐、地点、声明和定时；失败视频不会重试。")
QCheckBox("我已核对待续发视频及原定时时间")
```

复选框未选中时禁用确认按钮；确认按钮文本必须为 `确认继续发布 {pendingCount} 条`。每条列表行显示文件名和 `scheduleTime`；没有失败项行。

新增 `DouyinCommercePage.open_batch_resume`：从 `app_core.douyin_commerce_batch_service` 导入并在入口取得一次 `resume_now = current_shanghai_time()`；先调用 `task_service.prepare_douyin_batch_resume(task_id, now=resume_now)`。不允许时 `QMessageBox.warning(self, "继续发布", blockedReason)` 并返回。允许时弹出专用对话框；用户取消立即返回。只有 Accepted 后才调用 `task_service.create_douyin_batch_resume(task_id, now=resume_now)`。创建异常以 `QMessageBox.warning(self, "继续发布", str(exc))` 呈现且返回。成功后调用既有 `start_batch_publish(created["batch"], created["task"])`；不直接调用 `run_publish`，以保留既有异步执行、进度与平台回执门。

- [ ] **Step 4: 运行抖音带货页单元测试确认通过**

Run: `python -m unittest test_douyin_commerce_service -v`

Expected: PASS，取消与拒绝均零副作用，确认才创建 `oneclick_resume` 子任务并走既有启动路径。

- [ ] **Step 5: 提交确认对话框与启动入口**

```bash
git add ui/douyin_commerce_page.py test_douyin_commerce_service.py
git commit -m "feat: 增加抖音批量续发确认"
```

### Task 5: 全量回归、离屏界面验证和交接

**Files:**
- Create: `outputs/douyin-batch-resume-task-detail.png`（仅本地验收截图，不加入 Git）
- Modify: `/Users/andy/Documents/AI/知识库/06_项目记忆/代码迁移优化/00_项目记忆.md`
- Create: `/Users/andy/Documents/AI/知识库/06_项目记忆/代码迁移优化/会话摘要/2026-08-08_抖音带货受控续发实施.md`

**Interfaces:**
- Consumes: Tasks 1-4 的完整本地功能。
- Produces: 559+ 单元测试、Python 编译检查、离屏 Qt 截图和清晰的“本地验证完成，未做真实平台提交”交接记录。

- [ ] **Step 1: 运行聚焦测试并保存命令输出**

Run: `python -m unittest test_task_service test_douyin_commerce_batch_executor test_task_page test_main_window test_douyin_commerce_service -v`

Expected: PASS，包含来源不可变、暂停分类、按钮路由、取消零副作用和确认启动测试。

- [ ] **Step 2: 运行完整回归与编译检查**

Run: `python -m unittest discover -v`

Expected: PASS，测试数不得少于改动前通过的 559 项，新增测试全部纳入发现。

Run: `python -m py_compile app_core/database.py app_core/task_service.py app_core/douyin_commerce_batch_executor.py ui/task_page.py ui/main_window.py ui/douyin_commerce_page.py`

Expected: exit code 0。

- [ ] **Step 3: 做一次离屏 UI 冒烟检查，不执行平台提交**

以 `QT_QPA_PLATFORM=offscreen` 启动现有原生界面测试或一个仅构造 `TaskDetailDialog` 的测试夹具，加载用户手动暂停、两条 pending 的本地任务，确认截图中存在 `继续未开始的 2 条`；点击前后断言任务总数不变。将截图保存到 `outputs/douyin-batch-resume-task-detail.png`，不运行 `open_batch_resume` 的确认动作，不打开真实浏览器。

- [ ] **Step 4: 更新项目记忆与会话交接**

在项目记忆新增一条：受控续发仅适用于新产生的 `user_request` 暂停批次，子任务使用 `oneclick_resume` 和 `resumeSourceTaskId`，本轮只有本地/离屏验证，没有平台续发证明。会话摘要记录目标、实现文件、测试命令与结果、风险（需由 Andy 在单条用户手动暂停测试中点确认）、下一步（如需真实验证由 Andy 明确发起）。不得写入 Cookie、账号信息或素材绝对路径。

- [ ] **Step 5: 审查工作树并完成交接**

Run: `git status --short && git diff --check && git log --oneline -6`

Expected: `outputs/` 截图保持未跟踪，代码和测试已按前四个任务提交；无空白错误。

不要推送远程或触发 Windows 打包；只有 Andy 再次明确要求上传或构建时才执行外部动作。

## 计划自审

### 规格覆盖

- 新字段、暂停代码、历史任务拒绝和来源/子任务关系由 Task 1 实现。
- 既有运行路径的所有暂停原因由 Task 2 分类，避免只有新功能的任务可审计。
- 任务详情按钮只在资格允许时显示、且没有执行权限，由 Task 3 实现。
- 二次确认、取消零副作用、确认后才创建子任务并复用现有执行器，由 Task 4 实现。
- 不自动改期、素材/账号/地点/音乐/声明检查以及不触发真实提交，由 Task 1、Task 4 和 Task 5 共同覆盖。
- 本地、离屏 UI、完整回归、项目记忆和未推送约束由 Task 5 覆盖。

### 占位符扫描

已检查本文不含任何待补充占位语或笼统的错误处理要求；每个代码步骤给出了文件、接口、验证命令与最小实现形状。

### 类型一致性

`prepare_douyin_batch_resume` 在 Tasks 1、3、4 均使用 `task_id: int` 与 `now: datetime`，并返回相同的 `resumeAllowed`、`blockedReason`、`pendingCount`、`batch`、`sourceTaskNo`、`itemIndexes` 字段。`create_douyin_batch_resume` 在 Task 1 定义、Task 4 使用，返回相同的 `task` 与 `batch` 字段。任务详情和主窗口都仅传递 `int task_id`，未绕过服务层。
