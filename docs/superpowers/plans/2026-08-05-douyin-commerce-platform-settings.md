# 抖音带货平台设置与后台进度 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 将抖音带货上传后的平台设置改为“左侧配乐与定位、右侧声明与定时”的紧凑双栏页，并让上传全过程在隐藏浏览器中运行、由客户端显示安全的阶段进度和登录失效提示。

**Architecture:** 保留现有 DouyinCommerceSessionManager 的同一临时编辑会话和单次上传边界；会话层输出内存进度事件和受控登录失效结果，UI 线程只投影这些状态。页面不再把平台配置拆进单一 QStackedWidget 卡片，而是始终展示双栏卡片，并按已回读状态启用控件。

**Tech Stack:** Python 3、PyQt6、Playwright、现有 unittest 离线/UI 测试、现有抖音受控编辑会话。

## Global Constraints

- 所有用户可见文案、源码注释与新增文档使用 UTF-8 中文。
- 正常上传使用屏幕外的有界面浏览器窗口；不改为纯无头模式，不自动前置浏览器。
- 登录失效、二维码、验证码或重新认证需求必须安全停止本次临时会话，只提示“登录已失效，请到账号管理重新登录”；不得在抖音带货页打开或控制登录。
- 不保存 Cookie、二维码、验证码、平台原始请求、DOM、会话或账号凭据；本地草稿边界保持不变。
- 本轮不执行真实上传、保存草稿、预检、定时提交或公开发布；只运行离线/UI 模拟验收。
- 音乐、地点与作品声明仍需用户明确选择且必须经当前编辑页回读；定时为可选项，开启时必须是未来 Asia/Shanghai 时间。
- 工作区已有用户未提交改动；每次提交前只暂存本计划本轮新增的代码和测试，不能带入数据库、storage state、截图、日志或无关改动。

---

## 文件职责

| 文件 | 责任 |
| --- | --- |
| app_core/douyin_commerce_session.py | 输出非敏感上传进度、把已识别的登录失效转为安全结果、继续持有同一编辑会话。 |
| uploader/douyin_uploader/main.py | 在带货上传的既有页面操作前后发出阶段进度，不改变字段写入和封面跳过逻辑。 |
| ui/background_task.py | 支持将后台任务的进度事件安全投递到 GUI 线程，同时保持旧零参数任务调用兼容。 |
| ui/douyin_commerce_page.py | 双栏平台设置、紧凑进度卡、登录失效提示/账号管理导航、控件门槛和回读状态。 |
| ui/main_window.py | 接收带货页的账号管理导航请求，只切换主页面，不触发登录或检测。 |
| ui/common.py | 双栏、折叠候选列表、进度卡和登录提示的纯视觉样式。 |
| test_douyin_commerce_service.py | 会话事件、后台任务进度、双栏 UI、空列表不占位、登录失效安全停止与按钮门槛回归。 |

## 共享接口约定

~~~python
@dataclass(frozen=True)
class CommerceProgressEvent:
    phase: str
    label: str
    state: str = "running"

    def to_public_dict(self) -> dict[str, str]: ...


class DouyinCommerceSessionManager:
    def start_upload(
        self,
        payload: Mapping[str, Any],
        *,
        on_progress: Callable[[dict[str, str]], None] | None = None,
    ) -> dict[str, str]: ...


class BackgroundTaskRunner(QObject):
    def run(
        self,
        key: str,
        fn: Callable[[], Any] | None = None,
        *,
        with_progress: Callable[[Callable[[object], None]], Any] | None = None,
        on_progress: Callable[[object], None] | None = None,
        on_started: Callable[[], None] | None = None,
        on_success: Callable[[Any], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_finished: Callable[[], None] | None = None,
    ) -> bool: ...


class DouyinCommercePage(QWidget):
    request_account_management = pyqtSignal()

    def _set_commerce_progress(self, event: Mapping[str, object]) -> None: ...
    def _clear_commerce_progress(self) -> None: ...
    def _handle_login_required(self) -> None: ...
~~~

CommerceProgressEvent.to_public_dict() 只能返回 phase、label、state。失败细节只写本机日志；UI 不显示选择器、请求、URL、Cookie、二维码或凭据。

### Task 1: 为隐藏上传会话建立可测试的进度与登录失效契约

**Files:**
- Modify: app_core/douyin_commerce_session.py:1-360
- Modify: uploader/douyin_uploader/main.py:844-940
- Test: test_douyin_commerce_service.py:2102-2505

**Interfaces:**
- Consumes: 现有 _readback_douyin_session_identity(..., reveal=False)、launch_publish_browser()、prepare_uploaded_video_editor(..., reveal_editor=False)。
- Produces: CommerceProgressEvent、_emit_progress()、_login_required_result() 与支持 on_progress 的 start_upload()。

- [ ] **Step 1: 写出进度字段和登录失效结果的失败测试**

在 DouyinCommerceSessionContractTests 中加入：

~~~python
def test_progress_event_only_exposes_phase_label_and_state(self) -> None:
    event = douyin_commerce_session.CommerceProgressEvent(
        phase="uploading_video", label="正在上传视频"
    )
    self.assertEqual(
        event.to_public_dict(),
        {"phase": "uploading_video", "label": "正在上传视频", "state": "running"},
    )


def test_login_required_result_has_no_session_or_raw_diagnostic(self) -> None:
    manager = douyin_commerce_session.DouyinCommerceSessionManager()
    result = manager._login_required_result()
    self.assertEqual(result["status"], "needs_login")
    self.assertEqual(result["message"], "登录已失效，请到账号管理重新登录")
    self.assertNotIn("sessionId", result)
    self.assertNotIn("Locator", str(result))
~~~

再用 AsyncMock 的上传器在 prepare_uploaded_video_editor() 前后调用其 progress_callback，断言依次至少出现 uploading_video、waiting_platform、reading_content，且不改变 skip_thumbnail=True 的封面边界。

- [ ] **Step 2: 运行测试确认当前实现尚无接口**

Run:

~~~bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest \
  test_douyin_commerce_service.DouyinCommerceSessionContractTests.test_progress_event_only_exposes_phase_label_and_state \
  test_douyin_commerce_service.DouyinCommerceSessionContractTests.test_login_required_result_has_no_session_or_raw_diagnostic
~~~

Expected: FAIL，提示 CommerceProgressEvent 或 _login_required_result 不存在。

- [ ] **Step 3: 实现只含公开字段的进度事件与隐藏上传回调**

在 app_core/douyin_commerce_session.py 加入：

~~~python
@dataclass(frozen=True)
class CommerceProgressEvent:
    phase: str
    label: str
    state: str = "running"

    def to_public_dict(self) -> dict[str, str]:
        return {"phase": self.phase, "label": self.label, "state": self.state}


def _emit_progress(callback, phase: str, label: str, state: str = "running") -> None:
    if callback is not None:
        callback(CommerceProgressEvent(phase, label, state).to_public_dict())
~~~

将 start_upload() 与 _start_upload() 增加关键字参数 on_progress。在账号身份回读前发出 checking_session，启动隐藏窗口前发出 opening_editor，会话创建成功后发出 ready。将回调赋给 uploader.progress_callback，不把它保存到数据库或任务载荷。

带货会话继续使用 publish_context(background_mode=False) 和 prepare_uploaded_video_editor(..., reveal_editor=False)；这会保留现有屏幕外有界面窗口，而非切换成纯无头浏览器。不得新增 reveal_page_window() 或 goto_and_reveal() 调用。

在 uploader/douyin_uploader/main.py 增加私有 _report_commerce_progress()，仅在 progress_callback 可调用时发送：文件选择前 uploading_video、首次进入上传等待循环时 waiting_platform、标题和文案写入后 reading_content。普通发布没有回调时必须保持原行为。

- [ ] **Step 4: 将明确登录页识别转为安全结果**

在会话层新增异步 _is_login_required_page(page)：只读取当前 page.url 与可见 body 短文本；仅匹配登录/扫码/验证码的精确登录语义，读取失败时返回 False，不得猜测。

账号身份回读失败后，只有 _is_login_required_page(page) 为 True 时才：

~~~python
def _login_required_result(self) -> dict[str, str]:
    return {
        "status": "needs_login",
        "message": "登录已失效，请到账号管理重新登录",
    }
~~~

并发出 needs_login 事件、让 finally 关闭 context、browser 和 playwright，不创建 _CommerceEditorSession。账号身份不一致、控件异常和上传失败保持为普通安全错误，不伪装成登录失效。

- [ ] **Step 5: 运行会话合同和上传器回归**

Run:

~~~bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest \
  test_douyin_commerce_service.DouyinCommerceSessionContractTests \
  test_douyin_commerce_service.DouyinCommerceMusicRuleTests
~~~

Expected: PASS；测试不启动 Playwright，不创建浏览器，也不写入平台。

- [ ] **Step 6: 建立独立提交**

~~~bash
git add -p app_core/douyin_commerce_session.py uploader/douyin_uploader/main.py test_douyin_commerce_service.py
git diff --cached --check
git commit -m "feat: 增加抖音带货隐藏上传进度"
~~~

### Task 2: 让后台任务把进度安全投递到 GUI 线程

**Files:**
- Modify: ui/background_task.py:12-88
- Test: test_douyin_commerce_service.py:1200-2073

**Interfaces:**
- Consumes: Task 1 的 on_progress 回调字典。
- Produces: TaskSignals.progressed 和 BackgroundTaskRunner.run(..., with_progress=..., on_progress=...)。

- [ ] **Step 1: 写出后台任务进度信号的失败测试**

在 UI 测试类中加入：

~~~python
def test_background_task_forwards_progress_before_success(self) -> None:
    events: list[dict[str, str]] = []
    task = BackgroundTask(
        lambda report: (
            report({"phase": "uploading_video", "label": "正在上传视频", "state": "running"}),
            {"ok": True},
        )[1]
    )
    task.signals.progressed.connect(events.append)
    task.run()
    self.assertEqual(events[0]["phase"], "uploading_video")
~~~

- [ ] **Step 2: 运行测试确认当前任务没有进度信号**

Run:

~~~bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest \
  test_douyin_commerce_service.DouyinCommerceUiTests.test_background_task_forwards_progress_before_success
~~~

Expected: FAIL，提示 TaskSignals.progressed 或带 report 参数的任务调用不存在。

- [ ] **Step 3: 实现兼容的 with_progress 执行入口**

将 TaskSignals 增加 progressed = pyqtSignal(object)。将 BackgroundTask 的函数类型改为 Callable[[Callable[[object], None]], Any]，并在 run() 内调用：

~~~python
self.signals.succeeded.emit(self.fn(self.signals.progressed.emit))
~~~

将 BackgroundTaskRunner.run() 改为同时支持既有 fn 和新增 with_progress：

~~~python
if fn is None and with_progress is None:
    raise ValueError("后台任务需要 fn 或 with_progress")
worker = with_progress or (lambda _report: fn())
task = BackgroundTask(worker)
~~~

若传入 on_progress，连接 task.signals.progressed.connect(on_progress)。所有已有 runner.run(key, fn, ...) 调用保持不变。

- [ ] **Step 4: 运行任务与现有页面测试**

Run:

~~~bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest \
  test_douyin_commerce_service.DouyinCommerceUiTests \
  test_douyin_commerce_service.DouyinCommerceTaskPresentationTests
~~~

Expected: PASS；旧的零参数后台任务仍可运行，进度回调可在 UI 线程消费。

- [ ] **Step 5: 建立独立提交**

~~~bash
git add ui/background_task.py test_douyin_commerce_service.py
git diff --cached --check
git commit -m "feat: 支持客户端后台任务进度反馈"
~~~

### Task 3: 将平台设置重构为配乐/定位与声明/定时双栏

**Files:**
- Modify: ui/douyin_commerce_page.py:820-1185,1888-2600
- Modify: ui/common.py:764-1030
- Test: test_douyin_commerce_service.py:1223-2030

**Interfaces:**
- Consumes: _selected_music、_selected_location()、_location_applied、_declaration_applied、timer_enabled、_busy()。
- Produces: platform_left_column、platform_right_column、platform_progress_strip、platform_review_dock；保留既有 music_panel、location_panel、declaration_panel、schedule_panel 供业务回调使用。

- [ ] **Step 1: 写出双栏可见性和懒加载高度的失败测试**

替换旧的“只显示当前平台步骤”断言，加入：

~~~python
def test_platform_settings_keeps_music_location_and_publish_settings_visible(self) -> None:
    self.page._session_id = "session-demo"
    self.page.pages.setCurrentIndex(1)
    self.page._sync_view()
    self.assertTrue(self.page.platform_left_column.isVisible())
    self.assertTrue(self.page.platform_right_column.isVisible())
    self.assertTrue(self.page.music_panel.isVisible())
    self.assertTrue(self.page.location_panel.isVisible())
    self.assertTrue(self.page.declaration_panel.isVisible())
    self.assertTrue(self.page.schedule_panel.isVisible())


def test_empty_music_list_does_not_reserve_platform_card_height(self) -> None:
    self.page._music_candidates = []
    self.page._sync_view()
    self.assertTrue(self.page.music_list.isHidden())
    self.assertEqual(self.page.music_list.minimumHeight(), 0)
~~~

再为地点确认增加断言：确认后 location_result_list 隐藏、确认卡中同时存在名称与完整地址、change_location_button 可见。

- [ ] **Step 2: 运行测试确认旧单阶段堆栈不满足新布局**

Run:

~~~bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest \
  test_douyin_commerce_service.DouyinCommerceUiTests.test_platform_settings_keeps_music_location_and_publish_settings_visible \
  test_douyin_commerce_service.DouyinCommerceUiTests.test_empty_music_list_does_not_reserve_platform_card_height
~~~

Expected: FAIL，提示 platform_left_column/platform_right_column 不存在或音乐卡因阶段堆栈隐藏。

- [ ] **Step 3: 重排平台页与底部唯一主操作**

在 _build_platform_settings_page() 删除常驻“本次任务”和“本次上传会话”大列，以及正常流程使用的 platform_stage_stack。使用一个 QGridLayout：

~~~python
self.platform_left_column = QFrame()
self.platform_left_column.setObjectName("douyinCommercePlatformLeftColumn")
self.platform_right_column = QFrame()
self.platform_right_column.setObjectName("douyinCommercePlatformRightColumn")

left_layout.addWidget(self.music_panel)
left_layout.addWidget(self.location_panel)
left_layout.addStretch(1)
right_layout.addWidget(self.declaration_panel)
right_layout.addWidget(self.schedule_panel)
right_layout.addStretch(1)
columns.addWidget(self.platform_left_column, 0, 0)
columns.addWidget(self.platform_right_column, 0, 1)
columns.setColumnStretch(0, 58)
columns.setColumnStretch(1, 42)
~~~

在双栏下方新增 platform_review_dock，将既有 to_review_button 移入其中，文本固定为“检查并继续”。左侧只显示短状态，例如“还需确认地点”。不要恢复长流程说明或重复的会话清单。

将未加载的 music_list 与 location_result_list 默认 setVisible(False) 和 setMinimumHeight(0)；只有实际候选存在时设置 setMaximumHeight(240) 并显示。保留地点的暂选→确认→完整地址回读→更换定位状态机。

- [ ] **Step 4: 调整控件门槛而非隐藏业务步骤**

修改 _sync_platform_workspace()：保留 _current_platform_stage() 仅用于计算前置条件，不再调用 platform_stage_stack.setCurrentIndex()。

必须满足下列规则：

~~~python
can_search_location = bool(self._session_id and self._selected_music) and not self._busy()
can_apply_declaration = bool(
    self._session_id and self._location_applied and self._selected_declaration()
) and not self._busy()
can_configure_schedule = bool(self._session_id and self._declaration_applied) and not self._busy()
can_review = self._can_review() and not self._busy()
~~~

相应卡片保持可见；未满足前置条件时显示短文案“完成配乐后可选择地点”“确认地点后可填写声明”“确认声明后可设置定时”。to_review_button 只在 can_review 时可点击。

- [ ] **Step 5: 增加双栏和紧凑状态样式并运行 UI 回归**

在 ui/common.py 增加 douyinCommercePlatformLeftColumn、douyinCommercePlatformRightColumn、douyinCommercePlatformProgress、douyinCommercePlatformReviewDock 的样式；保留现有浅色、低对比边框和完整地址换行，删除旧 PlatformContextColumn、SessionColumn、PlatformStageStack 对本页的布局依赖。

Run:

~~~bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest \
  test_douyin_commerce_service.DouyinCommerceUiTests
~~~

Expected: PASS；平台页双栏稳定可见，未读取音乐时不出现 224px 空白，地点确认后不保留候选下拉。

- [ ] **Step 6: 建立独立提交**

~~~bash
git add -p ui/douyin_commerce_page.py ui/common.py test_douyin_commerce_service.py
git diff --cached --check
git commit -m "feat: 重构抖音带货平台设置双栏"
~~~

### Task 4: 把进度和登录失效投影到客户端并接入账号管理导航

**Files:**
- Modify: ui/douyin_commerce_page.py:260-470,1888-2435
- Modify: ui/main_window.py:345-635
- Modify: ui/common.py:790-1030
- Test: test_douyin_commerce_service.py:1200-2073

**Interfaces:**
- Consumes: Task 1 CommerceProgressEvent 和结果 {"status": "needs_login"}；Task 2 with_progress/on_progress。
- Produces: 上传底部进度条、平台页紧凑进度条、登录失效卡及 request_account_management 信号。

- [ ] **Step 1: 写出客户端进度和登录失效的失败测试**

在 DouyinCommerceUiTests 中加入：

~~~python
def test_upload_progress_projects_safe_phase_into_content_dock(self) -> None:
    self.page._set_commerce_progress(
        {"phase": "uploading_video", "label": "正在上传视频", "state": "running"}
    )
    self.assertTrue(self.page.commerce_progress_frame.isVisible())
    self.assertIn("正在上传视频", self.page.commerce_progress_label.text())
    self.assertNotIn("Locator", self.page.commerce_progress_label.text())


def test_login_required_stops_current_session_and_requests_account_navigation(self) -> None:
    requested: list[bool] = []
    self.page.request_account_management.connect(lambda: requested.append(True))
    self.page._session_id = "session-demo"
    self.page._handle_login_required()
    self.assertEqual(self.page._session_id, "")
    self.assertIn("登录已失效", self.page.login_required_label.text())
    self.page.go_account_management_button.click()
    self.assertEqual(requested, [True])
~~~

- [ ] **Step 2: 运行测试确认当前 UI 没有受控进度和导航信号**

Run:

~~~bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest \
  test_douyin_commerce_service.DouyinCommerceUiTests.test_upload_progress_projects_safe_phase_into_content_dock \
  test_douyin_commerce_service.DouyinCommerceUiTests.test_login_required_stops_current_session_and_requests_account_navigation
~~~

Expected: FAIL，提示 _set_commerce_progress、commerce_progress_frame 或 request_account_management 不存在。

- [ ] **Step 3: 接入上传进度和登录失效结果**

在内容准备底部操作条加入隐藏的 commerce_progress_frame，其中包括 commerce_progress_label 和固定范围 0..6 的 QProgressBar。在平台页顶部加入相同文案的紧凑 platform_progress_label，不重复显示大段说明。

start_upload() 改用：

~~~python
self.runner.run(
    "douyin_commerce_upload",
    with_progress=lambda report: douyin_commerce_session.commerce_session_manager.start_upload(
        payload, on_progress=report
    ),
    on_progress=self._set_commerce_progress,
    on_success=self._handle_upload_result,
    on_error=lambda message: self._platform_action_error("content", message),
    on_finished=self._sync_view,
)
~~~

_handle_upload_result() 遇到 status == "needs_login" 时调用 _handle_login_required()，不得调用 _upload_succeeded()；否则走既有成功初始化流程。_handle_login_required() 清空 _session_id 和临时候选，保留本机保存内容，显示固定中文，并绝不调用浏览器、上传、重试或账号写入。

为页面增加 request_account_management = pyqtSignal()；“前往账号管理”按钮只 emit 该信号。MainWindow.__init__() 中将此信号连接到 lambda: self._set_current_page(1)，只切换到账号管理页，不调用登录或检测服务。

- [ ] **Step 4: 运行完整离线与编译验证**

Run:

~~~bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest \
  test_douyin_commerce_draft_service \
  test_douyin_commerce_service \
  test_douyin_location
.venv/bin/python -m py_compile \
  app_core/douyin_commerce_session.py \
  uploader/douyin_uploader/main.py \
  ui/background_task.py \
  ui/douyin_commerce_page.py \
  ui/main_window.py \
  ui/common.py
git diff --check
QT_QPA_PLATFORM=offscreen .venv/bin/python desktop_native_app.py --ui-test
~~~

Expected: 全部 PASS，NATIVE_DESKTOP_UI_OK；没有浏览器启动、上传、草稿、预检或发表。

- [ ] **Step 5: 启动开发版做本地模拟验收**

Run:

~~~bash
.venv/bin/python -u desktop_native_app.py --page commerce
~~~

验收：内容页出现紧凑进度占位；手动进入平台页时可见左侧配乐/定位和右侧声明/定时；候选区域为空时不占大块高度；使用模拟回调验证“登录已失效”只出现账号管理导航。

- [ ] **Step 6: 建立独立提交并记录验收边界**

~~~bash
git add -p ui/douyin_commerce_page.py ui/main_window.py ui/common.py test_douyin_commerce_service.py
git diff --cached --check
git commit -m "feat: 完善抖音带货后台进度与登录提示"
~~~

提交后在 docs/DOUYIN_COMMERCE_WORKFLOW.md 追加“平台设置与登录失效”说明：正常浏览器隐藏、登录失效只前往账号管理、进度为客户端状态而非平台提交回执。不得记录真实账号或会话信息。

## 自审结果

- **需求覆盖：** Task 3 覆盖左配乐/地点、右声明/定时和紧凑 UI；Task 1、2、4 覆盖隐藏浏览器、上传进度、登录失效不在当前页操作；各任务均保留单次上传与回读边界。
- **安全边界：** 所有测试使用 fake/离线对象；没有步骤调用最终提交、保存草稿或真实平台浏览器。
- **占位扫描：** 本计划没有 TODO、TBD 或“适当处理”等未定义实现步骤。
- **接口一致性：** 任务 1 的 on_progress 由任务 2 的 with_progress 注入，再由任务 4 的 _set_commerce_progress 消费；登录失效结果只由任务 4 处理。
