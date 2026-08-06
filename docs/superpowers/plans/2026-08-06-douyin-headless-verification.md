# 抖音带货无头验证对话框实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在抖音带货正式提交中，以原生客户端对话框完成短信验证码或扫码验证，同时浏览器始终保持无头并继续同一会话。

**Architecture:** 新增一个只保存内存状态的抖音验证协调器。后台 Playwright 执行器识别唯一验证挑战、创建请求并轮询协调器；Qt 页面按当前任务号显示对话框，短信代码由 UI 暂存、再由原执行器在原异步循环中取走填写。二维码只以图像字节在内存中展示，任何不唯一或无法回读的验证状态都停止任务。

**Tech Stack:** Python 3.12、PyQt6、Playwright、unittest、现有 `BackgroundTaskRunner`。

## Global Constraints

- 基线提交为 `2346ffc`；当前工作在其上的本地设计文档提交不得改变业务基线。
- 抖音带货浏览器始终 `headless=True`；不得调用 `reveal_page_window`、`bring_to_front`、`open` 或其他前台浏览器入口。
- 仅处理用户已确认最终提交后的平台短信/扫码验证；不新增登录、短信发送、草稿保存、预检绕过或自动发表。
- 验证码、二维码、Cookie、手机号、原始 DOM、请求参数和 storage state 不进入任务事件、日志、数据库、发布包、截图或 Git。
- 平台最终作品管理页或定时状态回读仍是 `scheduled`/`published` 的唯一依据。

---

### Task 1: 建立内存验证协调器与离线安全契约

**Files:**
- Create: `app_core/douyin_verification.py`
- Create: `test_douyin_verification.py`

**Interfaces:**
- Produces: `DouyinVerificationBroker`、`DouyinVerificationError`、`VerificationChallenge`、`verification_broker`。
- Produces: `create_sms(task_id, message) -> str`、`create_qr(task_id, qr_image, expires_in_seconds) -> str`、`submit_code(request_id, code) -> None`、`consume_code(request_id) -> str | None`、`snapshot(request_id) -> dict`、`succeed/fail/cancel/clear`。
- Consumes: 当前任务 ID；二维码仅为 `bytes`。

- [ ] **Step 1: 写短信验证码暂存的失败测试**

```python
def test_sms_code_is_consumed_once_and_never_exposed_by_snapshot(self):
    broker = DouyinVerificationBroker()
    request_id = broker.create_sms(task_id=41, message="需要短信验证")
    broker.submit_code(request_id, "123456")
    self.assertEqual(broker.consume_code(request_id), "123456")
    self.assertIsNone(broker.consume_code(request_id))
    self.assertNotIn("123456", str(broker.snapshot(request_id)))
```

- [ ] **Step 2: 运行失败测试并确认是缺少协调器而非测试错误**

Run: `.venv/bin/python -m unittest test_douyin_verification.DouyinVerificationBrokerTests.test_sms_code_is_consumed_once_and_never_exposed_by_snapshot -v`

Expected: FAIL，原因是 `app_core.douyin_verification` 或目标接口不存在。

- [ ] **Step 3: 实现最小内存请求模型**

```python
@dataclass
class VerificationRequest:
    request_id: str
    task_id: int
    kind: Literal["sms", "qr"]
    message: str
    expires_at: float
    qr_image: bytes = b""
    pending_code: str = field(default="", repr=False)
    state: str = "waiting"
```

`submit_code` 仅接受 4 至 8 位数字并以 `Condition` 唤醒等待方；`consume_code` 取出后立即清空。快照只返回请求 ID、任务 ID、类型、状态、说明、剩余秒数和是否有二维码。

- [ ] **Step 4: 扩充并运行协调器测试**

增加二维码空白/损坏拒绝、重复任务拒绝、取消、超时、错误状态和二维码字节不出现在快照中的用例。

Run: `.venv/bin/python -m unittest test_douyin_verification -v`

Expected: PASS，所有验证状态均为本机内存状态。

- [ ] **Step 5: 提交本任务**

```bash
git add app_core/douyin_verification.py test_douyin_verification.py
git commit -m "feat: 增加抖音无头验证协调器"
```

### Task 2: 在同一无头 Playwright 会话内识别并处理验证挑战

**Files:**
- Modify: `uploader/douyin_uploader/main.py:655-694`
- Modify: `app_core/douyin_commerce_session.py:947-1020`
- Modify: `test_douyin_publish_executor.py`
- Modify: `test_douyin_commerce_service.py`

**Interfaces:**
- Produces: `DouYinVideo.detect_publish_verification(page) -> VerificationChallenge | None`。
- Produces: `DouYinVideo.apply_sms_verification_code(page, challenge, code) -> None`。
- Changes: `DouYinVideo._wait_formal_publish_result(page, on_verification)`，回调在原 Playwright 事件循环中执行。
- Changes: `DouyinCommerceSessionManager.submit(session_id, payload, task_id)`；`task_id` 只用于安全事件和协调器索引。

- [ ] **Step 1: 写“后台验证不前置浏览器”的失败测试**

```python
def test_headless_sms_challenge_waits_for_native_code_without_revealing_page(self):
    page = SmsChallengePage()
    video = make_video()
    with patch("utils.base_social_media.reveal_page_window") as reveal:
        receipt = asyncio.run(video._wait_formal_publish_result(
            page, on_verification=submit_native_sms_code,
        ))
    self.assertEqual(receipt["status"], "published")
    reveal.assert_not_called()
    self.assertEqual(page.applied_code, "123456")
```

测试替身必须模拟：唯一验证码输入框、唯一验证按钮、验证后 URL 转为作品管理页。另写一例多个可见输入框时断言抛出“无法唯一确认”，且未填写任何值。

- [ ] **Step 2: 运行失败测试确认当前实现仍在后台模式直接报错**

Run: `.venv/bin/python -m unittest test_douyin_publish_executor.DouyinPublishPayloadTests.test_headless_sms_challenge_waits_for_native_code_without_revealing_page -v`

Expected: FAIL，当前 `_wait_formal_publish_result` 在后台模式抛出“后台模式无法完成人工验证”。

- [ ] **Step 3: 实现挑战识别与同会话处理**

`detect_publish_verification` 先判断发布成功 URL；再只在可见节点中区分：

1. 短信：唯一输入框 + 唯一确认按钮；
2. 扫码：唯一可解码二维码图片；
3. 其他：返回不可处理错误。

`_wait_formal_publish_result` 在发现挑战后调用 `on_verification(challenge)`；回调成功才继续轮询。短信填写发生在 `apply_sms_verification_code` 中，填入后必须回读输入值、点击唯一按钮并等待页面脱离验证态。不得记录选择器、验证码或二维码 URL。

`DouyinCommerceSessionManager` 以 `task_id` 创建 Broker 请求：短信循环检查 `consume_code`，二维码循环检查页面状态与 Broker 取消状态。两者总等待时限为 10 分钟，超时安全失败。保持 `_commerce_browser_launch_options` 固定返回 `{"headless": True, "hide_until_ready": False}`，并移除提交路径的前置浏览器分支。

- [ ] **Step 4: 运行定向测试与带货会话回归**

Run:

```bash
.venv/bin/python -m unittest test_douyin_publish_executor -v
.venv/bin/python -m unittest test_douyin_commerce_service.DouyinCommerceSessionContractTests -v
```

Expected: PASS；断言后台配置保持无头、验证码错误/取消/超时没有平台成功回执，且没有任何前置浏览器调用。

- [ ] **Step 5: 提交本任务**

```bash
git add uploader/douyin_uploader/main.py app_core/douyin_commerce_session.py \
  test_douyin_publish_executor.py test_douyin_commerce_service.py
git commit -m "feat: 支持抖音无头会话短信与扫码验证"
```

### Task 3: 添加原生抖音验证对话框并接入带货提交页

**Files:**
- Create: `ui/douyin_verification_dialog.py`
- Create: `test_douyin_verification_dialog.py`
- Modify: `ui/douyin_commerce_page.py:3217-3260`
- Modify: `test_douyin_commerce_service.py`

**Interfaces:**
- Produces: `DouyinVerificationDialog(request_id, broker=verification_broker, parent=...)`。
- Consumes: Broker `snapshot`、`qr_image`、`submit_code`、`cancel`。
- Changes: `DouyinCommercePage` 增加仅在最终提交运行期间工作的验证轮询定时器和按任务 ID 去重的活动对话框。

- [ ] **Step 1: 写短信对话框的失败测试**

```python
def test_sms_dialog_submits_digits_to_broker_without_showing_sensitive_history(self):
    request_id = self.broker.create_sms(task_id=51, message="请输入短信验证码")
    dialog = DouyinVerificationDialog(request_id, broker=self.broker)
    dialog.code_input.setText("123456")
    dialog.submit_button.click()
    self.assertEqual(self.broker.consume_code(request_id), "123456")
    self.assertNotIn("123456", dialog.status_label.text())
```

另写二维码对话框测试：从内存字节生成 `QPixmap`，没有号码/验证码输入框；取消后 Broker 为 `cancelled`。再写页面去重测试：同一 `task_id` 连续两次轮询只创建一个对话框。

- [ ] **Step 2: 运行失败测试确认对话框尚未实现**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest test_douyin_verification_dialog -v`

Expected: FAIL，原因是 `ui.douyin_verification_dialog` 不存在。

- [ ] **Step 3: 实现最小 Qt 对话框与页面轮询**

短信对话框使用 `QLineEdit` 数字输入、验证与取消按钮；扫码对话框使用 `QLabel` 显示由内存字节构造的 `QPixmap`。状态由 `QTimer` 每 250ms 轮询，成功自动关闭，失败/取消保留原因。

在 `DouyinCommercePage.open_submit_confirmation` 创建后台提交任务后启动验证轮询；在 `_submit_succeeded`、`_submit_failed` 与 `_submit_finished` 中停止轮询、关闭对话框并清理请求。轮询只根据 Broker 请求打开原生对话框，不调用任何浏览器控制函数。

- [ ] **Step 4: 运行 UI 与页面回归测试**

Run:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest test_douyin_verification_dialog -v
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest test_douyin_commerce_service -v
QT_QPA_PLATFORM=offscreen .venv/bin/python desktop_native_app.py --ui-test
```

Expected: PASS；对话框仅在 Broker 请求时显示，验证码不出现在 UI 历史/任务事件，正常提交路径不显示对话框。

- [ ] **Step 5: 提交本任务**

```bash
git add ui/douyin_verification_dialog.py ui/douyin_commerce_page.py \
  test_douyin_verification_dialog.py test_douyin_commerce_service.py
git commit -m "feat: 增加抖音原生验证对话框"
```

### Task 4: 全量离线回归与开发版安全验收

**Files:**
- Modify: `README.md`（仅补充一行：抖音带货无头验证由客户端原生弹窗承接，浏览器不前置）

**Interfaces:**
- Consumes: Tasks 1 至 3 的 Broker、执行器、页面和离线测试。
- Produces: 可复现的本地验证记录；不产生真实平台任务。

- [ ] **Step 1: 写 README 断言或文档检查测试**

```python
def test_readme_states_douyin_verification_stays_in_native_client(self):
    content = Path("README.md").read_text(encoding="utf-8")
    self.assertIn("无头", content)
    self.assertIn("原生验证", content)
```

- [ ] **Step 2: 运行失败测试**

Run: `.venv/bin/python -m unittest test_douyin_verification.ReadmeContractTests.test_readme_states_douyin_verification_stays_in_native_client -v`

Expected: FAIL，README 尚未包含该明确边界。

- [ ] **Step 3: 补充说明并执行完整离线回归**

Run:

```bash
.venv/bin/python -m unittest test_douyin_verification test_douyin_verification_dialog test_douyin_publish_executor test_douyin_commerce_service -v
QT_QPA_PLATFORM=offscreen .venv/bin/python desktop_native_app.py --ui-test
git diff --check
git status --short
```

Expected: 全部测试通过；离屏客户端自检通过；仅预期源码/测试/说明文件有改动；无数据库、会话、截图、素材或运行日志进入暂存区。

- [ ] **Step 4: 启动开发版本地模拟验收**

Run: `.venv/bin/python -u desktop_native_app.py --page commerce`

验收：本地模拟短信和二维码验证对话框可展示与取消；不启动真实抖音页面、不上传、不保存草稿、不预检、不提交。

- [ ] **Step 5: 提交本任务并按分支收尾流程处理**

```bash
git add README.md
git commit -m "docs: 说明抖音无头验证边界"
```

完成后调用 `superpowers:finishing-a-development-branch`，由用户选择保留本地提交、合并或推送；真实平台验证必须等待单独明确授权。

## 计划自检

- 设计覆盖：短信输入、二维码显示、无头约束、同会话回读、取消/超时、敏感数据、UI 去重、任务状态与离线验证均有独立任务。
- 无占位项：每个任务列明了文件、接口、失败测试、运行命令与提交范围。
- 接口一致：`DouyinCommercePage` 仅向 `DouyinCommerceSessionManager.submit` 传递任务 ID；页面不直接调用 Playwright；验证码仅由 Broker 向原执行器一次性传递。
