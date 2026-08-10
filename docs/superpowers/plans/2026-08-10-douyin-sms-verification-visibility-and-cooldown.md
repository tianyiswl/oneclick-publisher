# 抖音短信验证码明文显示与跨视频冷却自动续发 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让抖音短信验证码在本机原生弹窗中明文可核对，并让同账号后续视频在上一条实际触发短信后的 60 秒冷却结束时自动且唯一地继续最终提交。

**Architecture:** 新增不依赖 Qt、数据库或浏览器的 `DouyinSmsCooldownGate`，以单调时钟按账号维护运行期冷却；批量执行器在真实 SMS challenge 回调中登记起点，在每条视频预检后、`submit` 前等待。任务服务只保存无敏感内容的 UTC 起点和固定期限，用于同一任务及续发子任务恢复；UI 只负责明文输入、受控倒计时投影和退出收束。

**Tech Stack:** Python 3、PyQt6、`threading.Event`、SQLite、`unittest`、现有 `BackgroundTaskRunner`。

## Global Constraints

- 验证码输入框使用 `QLineEdit.EchoMode.Normal`，但仍只允许 4 至 8 位数字。
- 验证码、手机号、Cookie、账号文件路径和平台原始页面文本不得写入数据库、任务事件、日志、诊断、剪贴板自动操作或对象 `repr`。
- 冷却固定为 60 秒，只从真实 `VerificationChallenge(kind="sms")` 出现时开始。
- 冷却门只能位于预检之后、最终 `submit` 之前；上传、音乐、声明、地点、定时和预检继续执行。
- 冷却归零后每条视频只能调用一次 `submit`；不得重传视频、重复写表单、重复点击发布或主动重发验证码。
- 冷却等待最多每 1 秒检查一次退出状态；客户端关闭后不得产生迟到提交。
- UI 可逐秒刷新；数据库和普通日志只记录冷却开始与结束。
- 全部实施和测试默认离线，不访问真实抖音、不读取真实账号、不启动浏览器、不上传、不预检、不保存草稿、不发布。

---

## File Structure

- Create `app_core/douyin_sms_cooldown.py`：纯冷却状态机。
- Create `test_douyin_sms_cooldown.py`：边界、账号隔离、取消和固定错误码。
- Modify `ui/douyin_verification_dialog.py`、`test_douyin_verification_dialog.py`：验证码明文显示。
- Modify `app_core/task_service.py`、`test_task_service.py`：无敏感冷却事件、恢复和退出前回退。
- Modify `app_core/douyin_commerce_batch_executor.py`、`test_douyin_commerce_batch_executor.py`：真实触发、预提交等待和唯一提交。
- Modify `ui/douyin_commerce_page.py`、`test_douyin_commerce_service.py`、`test_main_window.py`：倒计时、日志降噪和关闭屏障。

---

### Task 1: 验证码明文显示且不扩大敏感数据边界

**Files:**
- Modify: `ui/douyin_verification_dialog.py:105-126`
- Test: `test_douyin_verification_dialog.py:28-95`

**Interfaces:**
- Consumes: `DouyinVerificationBroker.submit_code(request_id: str, code: str)`。
- Produces: `DouyinVerificationDialog.code_input.echoMode() == QLineEdit.EchoMode.Normal`。

- [ ] **Step 1: 写失败测试**

```python
def test_sms_code_is_plaintext_visible_but_never_copied_to_status(self) -> None:
    request_id = self.broker.create_sms(task_id=60, message="需要短信验证")
    dialog = DouyinVerificationDialog(request_id, broker=self.broker)
    dialog.code_input.setText("123456")

    self.assertEqual(dialog.code_input.echoMode(), QLineEdit.EchoMode.Normal)
    self.assertEqual(dialog.code_input.displayText(), "123456")
    self.assertIn("明文显示", dialog.description_label.text())
    self.assertNotIn("123456", dialog.status_label.text())
    self.assertNotIn("123456", repr(dialog))
    dialog.close()
```

- [ ] **Step 2: 确认 RED**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest test_douyin_verification_dialog.DouyinVerificationDialogTests.test_sms_code_is_plaintext_visible_but_never_copied_to_status -v`

Expected: FAIL，当前仍为 `Password`。

- [ ] **Step 3: 做最小实现**

```python
self.description_label.setText(
    "需要短信验证，请输入收到的数字验证码。验证码会明文显示，仅在本机内存中临时处理。"
)
self.code_input.setEchoMode(QLineEdit.EchoMode.Normal)
```

保留现有数字 Validator、8 位上限、Broker 4 至 8 位校验、二维码分支和不回显状态文案。

- [ ] **Step 4: 运行弹窗完整回归**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest test_douyin_verification_dialog -v`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add ui/douyin_verification_dialog.py test_douyin_verification_dialog.py
git commit -m "显示抖音短信验证码明文"
```

---

### Task 2: 建立纯单调时钟冷却门

**Files:**
- Create: `app_core/douyin_sms_cooldown.py`
- Create: `test_douyin_sms_cooldown.py`

**Interfaces:**
- Produces: `DouyinSmsCooldownGate.record_trigger(account_key: str) -> None`。
- Produces: `restore_remaining(account_key: str, remaining_seconds: float) -> None`。
- Produces: `remaining_seconds(account_key: str) -> int`。
- Produces: `wait_until_ready(account_key: str, *, on_tick, cancelled) -> bool`。
- Produces: 固定异常 `verification_cooldown_state_invalid`、`verification_cooldown_failed`。

- [ ] **Step 1: 写边界、隔离和取消测试**

```python
class DouyinSmsCooldownGateTests(unittest.TestCase):
    def test_waits_only_remaining_time_and_releases_at_60_seconds(self) -> None:
        now = [100.0]
        ticks: list[int] = []
        gate = DouyinSmsCooldownGate(
            clock=lambda: now[0],
            waiter=lambda seconds: now.__setitem__(0, now[0] + seconds),
        )
        gate.record_trigger("account-a")
        now[0] = 133.0
        self.assertTrue(gate.wait_until_ready(
            "account-a", on_tick=ticks.append, cancelled=lambda: False
        ))
        self.assertEqual((ticks[0], ticks[-1], now[0]), (27, 1, 160.0))

    def test_boundary_accounts_restore_and_cancel(self) -> None:
        now = [10.0]
        waits: list[float] = []
        gate = DouyinSmsCooldownGate(clock=lambda: now[0], waiter=waits.append)
        gate.record_trigger("account-a")
        gate.restore_remaining("account-b", 8.0)
        now[0] = 69.999
        self.assertEqual(gate.remaining_seconds("account-a"), 1)
        self.assertEqual(gate.remaining_seconds("account-b"), 0)
        self.assertFalse(gate.wait_until_ready(
            "account-a", on_tick=lambda _: None, cancelled=lambda: True
        ))
        self.assertEqual(waits, [])
        now[0] = 70.0
        self.assertEqual(gate.remaining_seconds("account-a"), 0)
```

另加测试：空账号、NaN 时钟、负数或大于 60 的恢复值固定抛 `verification_cooldown_state_invalid`；waiter 抛含敏感原文异常时只抛 `verification_cooldown_failed` 且 `__cause__ is None`。

- [ ] **Step 2: 确认 RED**

Run: `.venv/bin/python -m unittest test_douyin_sms_cooldown -v`

Expected: ERROR，模块不存在。

- [ ] **Step 3: 实现冷却门**

```python
SMS_COOLDOWN_SECONDS = 60.0

class DouyinSmsCooldownError(RuntimeError):
    pass

class DouyinSmsCooldownGate:
    def __init__(self, *, clock=time.monotonic, waiter=time.sleep) -> None:
        self._clock = clock
        self._waiter = waiter
        self._lock = threading.RLock()
        self._deadlines: dict[str, float] = {}

    @staticmethod
    def _validated_key(value: object) -> str:
        if type(value) is not str or not value.strip():
            raise DouyinSmsCooldownError("verification_cooldown_state_invalid")
        return value.strip()

    def _safe_now(self) -> float:
        try:
            value = float(self._clock())
        except (TypeError, ValueError, OverflowError):
            raise DouyinSmsCooldownError(
                "verification_cooldown_state_invalid"
            ) from None
        if not math.isfinite(value):
            raise DouyinSmsCooldownError("verification_cooldown_state_invalid")
        return value

    @staticmethod
    def _validated_remaining(value: object) -> float:
        try:
            remaining = float(value)
        except (TypeError, ValueError, OverflowError):
            raise DouyinSmsCooldownError(
                "verification_cooldown_state_invalid"
            ) from None
        if not math.isfinite(remaining) or not 0 <= remaining <= 60:
            raise DouyinSmsCooldownError("verification_cooldown_state_invalid")
        return remaining

    def record_trigger(self, account_key: str) -> None:
        key = self._validated_key(account_key)
        deadline = self._safe_now() + SMS_COOLDOWN_SECONDS
        with self._lock:
            self._deadlines[key] = deadline

    def restore_remaining(self, account_key: str, remaining_seconds: float) -> None:
        key = self._validated_key(account_key)
        remaining = self._validated_remaining(remaining_seconds)
        restored = self._safe_now() + remaining
        with self._lock:
            self._deadlines[key] = max(self._deadlines.get(key, restored), restored)

    def remaining_seconds(self, account_key: str) -> int:
        return math.ceil(self._remaining(account_key))

    def _remaining(self, account_key: str) -> float:
        key = self._validated_key(account_key)
        now = self._safe_now()
        with self._lock:
            deadline = self._deadlines.get(key, now)
        return max(0.0, deadline - now)

    def wait_until_ready(self, account_key: str, *, on_tick, cancelled) -> bool:
        try:
            while True:
                remaining = self._remaining(account_key)
                if remaining <= 0:
                    return True
                if cancelled():
                    return False
                on_tick(math.ceil(remaining))
                self._waiter(min(1.0, remaining))
        except DouyinSmsCooldownError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            raise DouyinSmsCooldownError("verification_cooldown_failed") from None
```

上述辅助方法必须保持精确类型和有限数检查；错误均固定脱敏。

- [ ] **Step 4: 运行新模块测试**

Run: `.venv/bin/python -m unittest test_douyin_sms_cooldown -v`

Expected: PASS，测试不真实等待。

- [ ] **Step 5: 提交**

```bash
git add app_core/douyin_sms_cooldown.py test_douyin_sms_cooldown.py
git commit -m "增加抖音短信提交冷却门"
```

---

### Task 3: 保存无敏感冷却时间并支持退出续发

**Files:**
- Modify: `app_core/task_service.py:14-24,44-94,534-784,935-1025`
- Test: `test_task_service.py:18-310`

**Interfaces:**
- Produces: `record_douyin_sms_cooldown(task_id: int, item_id: int, *, triggered_at_utc: datetime) -> None`。
- Produces: `load_douyin_sms_cooldown_remaining(task_id: int, *, now_utc: datetime) -> float`。
- Produces: `pause_douyin_batch_before_submit(task_id: int, item_id: int, message: str) -> None`。
- Produces: `PAUSE_REASON_CLIENT_SHUTDOWN = "client_shutdown"`。

- [ ] **Step 1: 写保存、来源任务恢复和损坏事件测试**

```python
def test_sms_cooldown_restores_for_source_and_resume_child(self) -> None:
    source = task_service.create_douyin_batch_task(self.batch)
    item_id = task_service.get_task(source["id"])["items"][0]["id"]
    triggered = datetime(2026, 8, 10, 1, 0, tzinfo=ZoneInfo("UTC"))
    task_service.record_douyin_sms_cooldown(
        source["id"], item_id, triggered_at_utc=triggered
    )
    child = task_service.create_douyin_batch_task(
        self.batch, resume_source_task_id=source["id"]
    )
    now = triggered + __import__("datetime").timedelta(seconds=33)
    self.assertEqual(
        task_service.load_douyin_sms_cooldown_remaining(source["id"], now_utc=now),
        27.0,
    )
    self.assertEqual(
        task_service.load_douyin_sms_cooldown_remaining(child["id"], now_utc=now),
        27.0,
    )
    rendered = str(task_service.get_task(source["id"])["events"])
    self.assertNotIn("oneclick_3_offline.json", rendered)
    self.assertNotIn("123456", rendered)
```

再用 SQL 把最新 `douyin_sms_cooldown_started.detailJson` 改成 `{"cooldownSeconds":"bad"}`，断言固定 `ValueError("verification_cooldown_state_invalid")`；把 `now_utc` 推到第 60 秒，断言返回 `0.0`。

- [ ] **Step 2: 写退出前回退测试**

```python
def test_client_shutdown_before_submit_returns_current_item_to_pending(self) -> None:
    source = task_service.create_douyin_batch_task(self.batch)
    item_id = task_service.get_task(source["id"])["items"][0]["id"]
    task_service.mark_batch_item_result(
        source["id"], item_id, ok=True,
        message="预检完成", event_type="preflight_readback", readback={},
    )
    task_service.pause_douyin_batch_before_submit(
        source["id"], item_id, "客户端退出，最终提交尚未发生"
    )
    saved = task_service.get_task(source["id"])
    self.assertEqual(saved["status"], "paused")
    self.assertEqual(saved["pauseReasonCode"], "client_shutdown")
    self.assertEqual(saved["items"][0]["status"], "pending")
```

- [ ] **Step 3: 确认 RED**

Run: `.venv/bin/python -m unittest test_task_service.DouyinCommerceBatchTaskTests.test_sms_cooldown_restores_for_source_and_resume_child test_task_service.DouyinCommerceBatchTaskTests.test_client_shutdown_before_submit_returns_current_item_to_pending -v`

Expected: ERROR，新函数不存在。

- [ ] **Step 4: 实现事件白名单和恢复**

把 `cooldownStartedAt`、`cooldownSeconds` 加入 `_BATCH_READBACK_FIELDS`。记录函数使用：

```python
mark_batch_item_result(
    task_id,
    item_id,
    ok=True,
    event_type="douyin_sms_cooldown_started",
    message="本条已实际触发短信验证，后续最终提交遵守 60 秒冷却",
    readback={
        "cooldownStartedAt": triggered_at_utc.astimezone(ZoneInfo("UTC")).isoformat(),
        "cooldownSeconds": "60",
    },
)
```

恢复函数依次检查当前任务和 `resumeSourceTaskId` 来源任务的最新匹配事件，严格解析带时区 ISO 时间和字符串 `"60"`，返回 `max(0.0, 60.0 - elapsed)`；匹配事件存在但结构损坏时不得当作无冷却放行。

实现保持以下控制流，内部 `_latest_sms_cooldown_event()` 只按事件 ID 逆序返回最新一条：

```python
def _latest_sms_cooldown_event(task_ids: list[int]) -> dict | None:
    normalized = [int(value) for value in task_ids if int(value) > 0]
    placeholders = ",".join("?" for _ in normalized)
    with connect() as conn:
        row = conn.execute(
            f"SELECT * FROM publish_task_events WHERE taskId IN ({placeholders}) AND eventType = 'douyin_sms_cooldown_started' ORDER BY id DESC LIMIT 1",
            tuple(normalized),
        ).fetchone()
    return dict(row) if row is not None else None

def load_douyin_sms_cooldown_remaining(
    task_id: int, *, now_utc: datetime
) -> float:
    if now_utc.tzinfo is None:
        raise ValueError("verification_cooldown_state_invalid")
    task = get_task(int(task_id))
    if not isinstance(task, dict):
        raise ValueError("verification_cooldown_state_invalid")
    task_ids = [int(task_id)]
    source_id = task.get("resumeSourceTaskId")
    if type(source_id) is int and source_id > 0:
        task_ids.append(source_id)
    event = _latest_sms_cooldown_event(task_ids)
    if event is None:
        return 0.0
    try:
        detail = json.loads(str(event["detailJson"]))
        if detail.get("cooldownSeconds") != "60":
            raise ValueError
        started = datetime.fromisoformat(detail["cooldownStartedAt"])
        if started.tzinfo is None:
            raise ValueError
        elapsed = (
            now_utc.astimezone(ZoneInfo("UTC"))
            - started.astimezone(ZoneInfo("UTC"))
        ).total_seconds()
        if elapsed < 0:
            raise ValueError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise ValueError("verification_cooldown_state_invalid") from None
    return max(0.0, 60.0 - elapsed)
```

- [ ] **Step 5: 实现原子退出回退**

在一个 SQLite 事务内确认条目属于任务且尚未成功，把当前条改回 `pending`，把任务改为 `paused`/`pauseReasonCode=client_shutdown`，写固定事件 `batch_paused_client_shutdown`。把该原因加入 `DOUYIN_BATCH_PAUSE_REASONS`，并让续发来源校验允许 `user_request` 或 `client_shutdown`，继续拒绝登录、验证、待核对和连续失败暂停。

原子更新的 SQL 必须保持以下状态边界：

```python
def pause_douyin_batch_before_submit(
    task_id: int, item_id: int, message: str
) -> None:
    now = _now()
    with connect() as conn:
        item = conn.execute(
            "SELECT status FROM publish_task_items WHERE id = ? AND taskId = ?",
            (int(item_id), int(task_id)),
        ).fetchone()
        if item is None or item["status"] == "success":
            raise ValueError("最终提交前条目状态无法安全回退")
        conn.execute(
            "UPDATE publish_task_items SET status = 'pending', message = ?, finishedAt = NULL WHERE id = ?",
            (message, int(item_id)),
        )
        conn.execute(
            "UPDATE publish_tasks SET status = 'paused', pauseReasonCode = ? WHERE id = ?",
            (PAUSE_REASON_CLIENT_SHUTDOWN, int(task_id)),
        )
        conn.execute(
            "INSERT INTO publish_task_events (taskId, itemId, level, eventType, message, createdAt) VALUES (?, ?, 'warning', 'batch_paused_client_shutdown', ?, ?)",
            (int(task_id), int(item_id), message, now),
        )
        conn.commit()
```

- [ ] **Step 6: 运行任务服务回归并提交**

Run: `.venv/bin/python -m unittest test_task_service -v`

Expected: PASS。

```bash
git add app_core/task_service.py test_task_service.py
git commit -m "保存抖音短信冷却恢复时间"
```

---

### Task 4: 接入真实 SMS 回调与最终提交前门禁

**Files:**
- Modify: `app_core/douyin_commerce_batch_executor.py:58-74,195-269,311-443,445-748`
- Test: `test_douyin_commerce_batch_executor.py:20-190,630-850`

**Interfaces:**
- Consumes: Tasks 2-3 的 Gate 和任务服务函数。
- Produces: `BatchProgressEvent.remaining_seconds: int | None`，公开键 `remainingSeconds`。
- Produces: `reset_shutdown() -> int`、`request_shutdown(source: str = "") -> bool`。
- Produces: 每次 `reset_shutdown()` 生成新的整数运行代际；旧代际倒计时不能提交新代际会话。
- Produces: 进度阶段 `verification_cooldown`。

- [ ] **Step 1: 写“下一条预检后等待、归零后只提交一次”的失败测试**

```python
def test_sms_cooldown_waits_after_next_preflight_then_auto_submits_once(self) -> None:
    now = [100.0]
    broker = DouyinVerificationBroker()
    manager = FakeCommerceSessionManager(
        challenge_on_index=0,
        verification_mode="active_success",
        verification_broker=broker,
    )
    gate = DouyinSmsCooldownGate(
        clock=lambda: now[0],
        waiter=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )
    events: list[BatchProgressEvent] = []
    executor = DouyinCommerceBatchExecutor(
        manager, verification_broker=broker, cooldown_gate=gate,
        utc_now=lambda: datetime(2026, 8, 10, 1, 0, tzinfo=timezone.utc),
    )
    result = executor.run_publish(
        self.batch, task_id=self.task["id"], confirmed=True, progress=events.append
    )
    self.assertEqual([row["status"] for row in result], ["published"] * 3)
    self.assertEqual(manager.calls.count("submit:0"), 1)
    self.assertEqual(manager.calls.count("submit:1"), 1)
    self.assertLess(
        manager.ordered_calls.index(("preflight", "session-2")),
        manager.ordered_calls.index(("submit", "session-2")),
    )
    cooldown = [row for row in events if row.phase == "verification_cooldown"]
    self.assertEqual((cooldown[0].remaining_seconds, cooldown[-1].remaining_seconds), (60, 1))
```

- [ ] **Step 2: 写暂停和退出竞态测试**

```python
def test_user_pause_during_cooldown_finishes_current_item_then_stops_next(self) -> None:
    now = [100.0]
    broker = DouyinVerificationBroker()
    manager = FakeCommerceSessionManager(
        challenge_on_index=0, verification_mode="active_success",
        verification_broker=broker,
    )
    executor: DouyinCommerceBatchExecutor
    first_wait = [True]

    def waiter(seconds: float) -> None:
        if first_wait[0]:
            first_wait[0] = False
            executor.request_pause(source="user_confirmed")
        now[0] += seconds

    executor = DouyinCommerceBatchExecutor(
        manager, verification_broker=broker,
        cooldown_gate=DouyinSmsCooldownGate(clock=lambda: now[0], waiter=waiter),
        utc_now=lambda: datetime(2026, 8, 10, 1, 0, tzinfo=timezone.utc),
    )
    result = executor.run_publish(self.batch, task_id=self.task["id"], confirmed=True)
    self.assertEqual([row["status"] for row in result], ["published", "published", "paused"])
    self.assertEqual(manager.calls.count("submit:1"), 1)
    self.assertNotIn("start_upload:2", manager.calls)

def test_client_shutdown_during_cooldown_never_submits_current_item(self) -> None:
    now = [100.0]
    broker = DouyinVerificationBroker()
    manager = FakeCommerceSessionManager(
        challenge_on_index=0, verification_mode="active_success",
        verification_broker=broker,
    )
    executor: DouyinCommerceBatchExecutor
    first_wait = [True]

    def waiter(seconds: float) -> None:
        if first_wait[0]:
            first_wait[0] = False
            executor.request_shutdown(source="client_shutdown")
        now[0] += seconds

    executor = DouyinCommerceBatchExecutor(
        manager, verification_broker=broker,
        cooldown_gate=DouyinSmsCooldownGate(clock=lambda: now[0], waiter=waiter),
        utc_now=lambda: datetime(2026, 8, 10, 1, 0, tzinfo=timezone.utc),
    )
    result = executor.run_publish(self.batch, task_id=self.task["id"], confirmed=True)
    self.assertEqual([row["status"] for row in result], ["published", "client_shutdown", "pending"])
    self.assertNotIn("submit:1", manager.calls)
    self.assertIn("close:1", manager.calls)
    saved = task_service.get_task(self.task["id"])
    self.assertEqual((saved["status"], saved["pauseReasonCode"]), ("paused", "client_shutdown"))
    self.assertEqual(saved["items"][1]["status"], "pending")
```

再加 `test_replaced_run_generation_drops_old_cooldown_callback`：旧 waiter 第一次执行时调用 `executor.reset_shutdown()` 建立新代际；旧 `run_publish()` 必须按 `client_shutdown` 收束，旧条没有 `submit`，且旧 tick 不能改变新代际进度。

- [ ] **Step 3: 确认 RED**

Run: `.venv/bin/python -m unittest test_douyin_commerce_batch_executor.DouyinCommerceBatchExecutorTests.test_sms_cooldown_waits_after_next_preflight_then_auto_submits_once -v`

Expected: ERROR，构造函数和进度字段尚不存在。

- [ ] **Step 4: 扩展进度和执行器依赖**

```python
@dataclass(frozen=True)
class BatchProgressEvent:
    index: int
    total: int
    phase: str
    message: str
    remaining_seconds: int | None = None

    def to_public_dict(self) -> dict[str, object]:
        result = {"index": self.index, "total": self.total,
                  "phase": self.phase, "message": self.message}
        if type(self.remaining_seconds) is int:
            result["remainingSeconds"] = self.remaining_seconds
        return result
```

构造函数增加 `cooldown_gate`、`utc_now`、`_shutdown_requested`、`_run_lock` 和 `_run_generation = 0`。`reset_shutdown()` 在锁内递增代际并清退出标志，返回新代际整数；UI 只在新 worker 入队前调用。`run_publish()` 在入口捕获当前代际并把它传到 `_run()`/`_run_item()`；冷却的 `cancelled` 同时检查退出 Event 与代际相等。`request_shutdown()` 只接受 `client_shutdown`；`run_publish()` 不自动清退出标志，避免排队退出竞态。

- [ ] **Step 5: 只在真实 SMS challenge 登记**

给 `_verification_progress_callback()` 增加 `account_key`，并加入：

```python
if (
    isinstance(challenge, VerificationChallenge)
    and challenge.kind == "sms"
    and self._has_active_verification(task_id)
):
    self._cooldown_gate.record_trigger(account_key)
    self._task_store.record_douyin_sms_cooldown(
        task_id, item_id, triggered_at_utc=self._utc_now()
    )
```

二维码、无 active 请求、验证码成功回调和 UI 轮询不得登记起点。

- [ ] **Step 6: 恢复并在 submit 前等待**

`run_publish()` 规范化批次后加载剩余时间并调用 `restore_remaining(batch["accountFile"], remaining)`。`_manager.preflight()` 成功后调用私有等待方法：有剩余时只记录一次 `verification_cooldown_wait_started`，每秒发送固定 `verification_cooldown` 事件，Gate 返回 `True` 后只记录一次 `verification_cooldown_wait_finished`，随后才发送 `submitting` 并调用一次 `submit`。

Gate 返回 `False` 时调用 `pause_douyin_batch_before_submit()`，返回 `status="client_shutdown"` 并由 `finally` 关闭会话；余下视频保持 `pending`。Gate 异常只投影固定码，不带 `__cause__`。

- [ ] **Step 7: 更新旧验证成功测试并跑回归**

给既有 `test_active_sms_verification_keeps_current_session_and_continues_same_item_once` 注入瞬时推进的 Gate，继续断言同一验证条只上传一次、只提交一次、验证成功后处理后续条目；不得把生产冷却常量改成 0。

Run: `.venv/bin/python -m unittest test_douyin_commerce_batch_executor -v`

Expected: PASS，测试不增加真实 60 秒等待。

- [ ] **Step 8: 提交**

```bash
git add app_core/douyin_commerce_batch_executor.py test_douyin_commerce_batch_executor.py
git commit -m "接入抖音跨视频短信冷却"
```

---

### Task 5: UI 倒计时、日志降噪和关闭屏障

**Files:**
- Modify: `ui/douyin_commerce_page.py:4387-4402,4478-4525,6497-6534`
- Modify: `test_douyin_commerce_service.py:3245-3280` 及关闭测试区
- Modify: `test_main_window.py:30-80`

**Interfaces:**
- Consumes: `remainingSeconds`、`reset_shutdown()`、`request_shutdown(source="client_shutdown")`。
- Produces: “短信验证码冷却中，剩余 N 秒；到点自动继续第 X/Y 条”。
- Produces: `shutdown() -> bool` 仅在批任务、采集器和旧会话全部安全结束时为 `True`。

- [ ] **Step 1: 写倒计时和日志降噪测试**

```python
def test_batch_sms_cooldown_updates_countdown_without_per_second_log(self) -> None:
    with patch("ui.douyin_commerce_page._LOGGER.info") as info:
        for remaining in (27, 26):
            self.page._batch_progress({
                "index": 2, "total": 20,
                "phase": "verification_cooldown",
                "message": f"短信验证码冷却中，剩余 {remaining} 秒；到点自动继续第 3/20 条",
                "remainingSeconds": remaining,
            })
    self.assertIn("剩余 26 秒", self.page.validation_label.text())
    self.assertEqual(info.call_count, 0)
```

- [ ] **Step 2: 写批任务关闭测试**

```python
def test_shutdown_requests_batch_stop_and_refuses_exit_until_worker_finishes(self) -> None:
    with patch.object(
        self.page.runner, "is_running",
        side_effect=lambda key: key == "douyin_commerce_batch_run",
    ), patch.object(
        self.page.runner, "cancel_pending", return_value=False
    ), patch.object(
        self.page.runner, "wait_for_finished", return_value=False
    ), patch.object(
        self.page._batch_executor, "request_shutdown", return_value=True
    ) as request_shutdown, patch.object(
        self.page, "_close_setup_generation"
    ) as close_generation:
        result = self.page.shutdown()

    self.assertFalse(result)
    request_shutdown.assert_called_once_with(source="client_shutdown")
    close_generation.assert_not_called()

def test_shutdown_closes_collectors_after_batch_worker_finishes(self) -> None:
    with patch.object(
        self.page.runner, "is_running",
        side_effect=lambda key: key == "douyin_commerce_batch_run",
    ), patch.object(
        self.page.runner, "cancel_pending", return_value=False
    ), patch.object(
        self.page.runner, "wait_for_finished", return_value=True
    ), patch.object(
        self.page._batch_executor, "request_shutdown", return_value=True
    ), patch.object(
        self.page, "_close_setup_generation",
        return_value={"closed": True, "aliveCollectorCount": 0},
    ):
        self.assertTrue(self.page.shutdown())
```

保留 `test_main_window.py` 契约：页面返回 `False` 时 `event.ignore()`，不停止账号检查、不全局关闭浏览器。

- [ ] **Step 3: 确认 RED**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_batch_sms_cooldown_updates_countdown_without_per_second_log test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_shutdown_requests_batch_stop_and_refuses_exit_until_worker_finishes test_main_window -v`

Expected: 两个新增 UI 测试 FAIL；当前每秒写日志且 `shutdown()` 未等待批任务。

- [ ] **Step 4: 实现 UI 投影**

`_batch_progress()` 严格读取 `phase` 和 `remainingSeconds`。冷却阶段使用受控模板更新 `_batch_progress_text`/`validation_label`，不逐秒调用 `_LOGGER.info`；其他阶段保持现有计数、日志和 `waiting_verification` 条目上下文。

- [ ] **Step 5: 实现启动与退出门禁**

`start_batch_publish()` 在 `runner.run()` 前调用 `self._batch_executor.reset_shutdown()`。`shutdown()` 设置页面退出 Event 后立即调用 `request_shutdown(source="client_shutdown")`；若 `_BATCH_RUN_KEY` 排队则 `cancel_pending`，已运行则有界 `wait_for_finished`。批任务未结束时立即返回 `False`；结束后才继续现有采集器零存活屏障和旧会话关闭。

- [ ] **Step 6: 运行 UI 回归并提交**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest test_douyin_commerce_service test_main_window test_background_task -v`

Expected: PASS。

```bash
git add ui/douyin_commerce_page.py test_douyin_commerce_service.py test_main_window.py
git commit -m "展示抖音短信冷却自动续发进度"
```

---

### Task 6: 完整离线验收

**Files:**
- Verify only: Tasks 1-5 的全部修改文件

**Interfaces:**
- Produces: 离线测试、编译、界面和 Git 清洁证据；不产生平台发布证据。

- [ ] **Step 1: 运行专项测试**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest test_douyin_sms_cooldown test_douyin_verification test_douyin_verification_dialog test_douyin_commerce_batch_executor test_task_service test_douyin_commerce_service test_main_window test_background_task -v`

Expected: PASS，无浏览器或平台访问日志。

- [ ] **Step 2: 连续跑冷却与执行器 5 轮**

Run: `for round in 1 2 3 4 5; do .venv/bin/python -m unittest test_douyin_sms_cooldown test_douyin_commerce_batch_executor || exit 1; done`

Expected: 五轮全部 PASS，无真实 60 秒等待、线程残留或随机失败。

- [ ] **Step 3: 运行完整回归**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -v`

Expected: PASS；交付时只能表述“完整离线回归通过”。

- [ ] **Step 4: 编译与差异检查**

Run: `.venv/bin/python -m py_compile app_core/douyin_sms_cooldown.py app_core/douyin_verification.py app_core/douyin_commerce_batch_executor.py app_core/task_service.py ui/douyin_verification_dialog.py ui/douyin_commerce_page.py && git diff --check && git status --short`

Expected: 退出 0；不触碰用户已有 `.superpowers/brainstorm/` 和 `outputs/`。

- [ ] **Step 5: 做产品代码敏感样本扫描**

Run: `rg -n "123456|13800138000|Cookie=|passport_csrf|ttwid" app_core/douyin_sms_cooldown.py app_core/douyin_commerce_batch_executor.py app_core/task_service.py ui/douyin_verification_dialog.py ui/douyin_commerce_page.py`

Expected: 产品代码无验证码、手机号或 Cookie 样本。

- [ ] **Step 6: 仅做源码客户端零平台 UI 冒烟**

启动源码客户端并打开抖音带货页面，确认无导入异常；不选择真实账号、不刷新音乐/地点、不上传、不预检、不提交。真实短信验证和 60 秒平台行为等待用户另行明确授权的单条实机验证。

---

## Completion Criteria

- 验证码输入明文可见，敏感数据边界不扩大。
- 真实 SMS challenge 后，同账号下一条只在最终提交前等待剩余时间。
- 59.999 秒不提交，60.000 秒自动继续且只提交一次。
- 用户暂停完成当前条；客户端退出取消自动提交并保留可续发状态。
- 同一任务及续发子任务恢复剩余时间；过期事件不等待；损坏事件安全停止。
- UI 逐秒显示倒计时，日志和数据库不逐秒刷屏。
- 全部证据明确标注为离线验证，不宣称真实抖音发布成功。
