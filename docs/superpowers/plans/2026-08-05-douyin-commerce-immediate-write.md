# 抖音带货即时写入交互 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 让抖音带货上传后的音乐、定位和声明选中即写入并回读，定时可独立填写但只在检查流程写入平台。

**Architecture:** 客户端保持固定双栏和单一临时编辑会话。所有会修改同一编辑页的即时写入共用一个后台任务键，完成前禁用其余平台控件；成功才更新已确认状态，失败恢复到最近一次平台回读值。定时只更新本次任务载荷，避免用户改时间时打开抖音发表弹窗。

**Tech Stack:** Python 3、PyQt6、现有 BackgroundTaskRunner、DouyinCommerceSessionManager、unittest。

## Global Constraints

- 所有新增文案和注释使用 UTF-8 中文。
- 只使用现有受控抖音编辑会话；不使用 API、签名参数或私有请求。
- 不保存草稿、不预览、不发表；本轮只运行离线/UI 验证。
- 音乐、定位、声明成功都必须由会话执行器回读；客户端暂选不等于平台确认。
- 登录失效、二维码、验证码、控件变化、回读不一致或未知提示均安全停止。
- 定时不在用户选择时打开抖音发表弹窗；只在“检查并继续”流程设置和回读。
- 工作区已有用户未提交改动；每次提交只可精确暂存本计划的新增 hunk。

---

### Task 1: 共享即时写入任务与失败恢复

**Files:**
- Modify: ui/douyin_commerce_page.py:229-290,2019-2170,2444-2675
- Test: test_douyin_commerce_service.py:1201-2070

**Interfaces:**
- Consumes: BackgroundTaskRunner.run()、select_favorite_music()、apply_location()、select_content_declaration()。
- Produces: _start_immediate_write(kind, work, on_success, on_error) 和 _IMMEDIATE_WRITE_KEY = "douyin_commerce_immediate_write"。

- [ ] **Step 1: 写失败测试**

~~~python
def test_immediate_write_uses_one_shared_task_key_and_disables_other_controls(self) -> None:
    self.page._session_id = "session-demo"
    with patch.object(self.page.runner, "run", return_value=True) as run:
        self.page._start_music_write({"musicId": "music-new", "title": "新音乐"})
    self.assertEqual(run.call_args.args[0], "douyin_commerce_immediate_write")
    self.assertFalse(self.page.location_search_button.isEnabled())

def test_failed_music_write_restores_last_confirmed_value(self) -> None:
    self.page._selected_music = {"musicId": "music-old", "title": "旧音乐"}
    self.page._pending_music = {"musicId": "music-new", "title": "新音乐"}
    self.page._immediate_music_failed("平台回读不一致")
    self.assertEqual(self.page.music_combo.currentData()["musicId"], "music-old")
~~~

- [ ] **Step 2: 运行失败测试**

~~~bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest   test_douyin_commerce_service.DouyinCommerceUiTests.test_immediate_write_uses_one_shared_task_key_and_disables_other_controls   test_douyin_commerce_service.DouyinCommerceUiTests.test_failed_music_write_restores_last_confirmed_value
~~~

Expected: FAIL，提示即时写入方法或音乐下拉框不存在。

- [ ] **Step 3: 实现最小共享入口**

~~~python
_IMMEDIATE_WRITE_KEY = "douyin_commerce_immediate_write"

def _start_immediate_write(self, kind, work, on_success, on_error) -> bool:
    if not self._session_id or self.runner.is_running(self._IMMEDIATE_WRITE_KEY):
        return False
    self._set_immediate_busy(kind, True)
    return self.runner.run(
        self._IMMEDIATE_WRITE_KEY,
        work,
        on_success=on_success,
        on_error=on_error,
        on_finished=lambda: self._finish_immediate_write(kind),
    )
~~~

_set_immediate_busy() 禁用音乐下拉框、地点搜索与候选、声明单选；_finish_immediate_write() 按已确认状态恢复控件。失败回调只恢复所属字段并调用既有 _platform_action_error() 保存本机诊断。

- [ ] **Step 4: 验证共享入口**

~~~bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest   test_douyin_commerce_service.DouyinCommerceUiTests.test_immediate_write_uses_one_shared_task_key_and_disables_other_controls   test_douyin_commerce_service.DouyinCommerceUiTests.test_failed_music_write_restores_last_confirmed_value
~~~

Expected: PASS。

### Task 2: 收藏音乐下拉直选

**Files:**
- Modify: ui/douyin_commerce_page.py:958-991,2444-2528
- Modify: ui/common.py:820-900
- Test: test_douyin_commerce_service.py:1320-1510

**Interfaces:**
- Consumes: DouyinCommerceSessionManager.load_favorite_music()、select_favorite_music(session_id, music_id)。
- Produces: music_combo、_load_favorite_music_candidates()、_start_music_write(candidate)。

- [ ] **Step 1: 写失败测试**

~~~python
def test_upload_success_reads_favorite_music_without_selecting_one(self) -> None:
    with patch.object(self.page, "_load_favorite_music_candidates") as load:
        self.page._upload_succeeded({"sessionId": "session-demo", "message": "已上传"})
    load.assert_called_once()
    self.assertIsNone(self.page._selected_music)

def test_music_combo_change_immediately_starts_platform_write(self) -> None:
    self.page._session_id = "session-demo"
    self.page._show_music_candidates([{"musicId": "m-1", "title": "收藏音乐"}])
    with patch.object(self.page, "_start_music_write") as write:
        self.page.music_combo.setCurrentIndex(1)
    write.assert_called_once_with({"musicId": "m-1", "title": "收藏音乐"})
~~~

- [ ] **Step 2: 运行失败测试**

~~~bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest   test_douyin_commerce_service.DouyinCommerceUiTests.test_upload_success_reads_favorite_music_without_selecting_one   test_douyin_commerce_service.DouyinCommerceUiTests.test_music_combo_change_immediately_starts_platform_write
~~~

Expected: FAIL，提示 music_combo 或自动读取入口不存在。

- [ ] **Step 3: 实现下拉直选**

在 _build_music_stage() 中用单个 QComboBox 替换候选列表和“确认使用”按钮。上传成功进入平台设置后自动读取收藏音乐，但不自动选择。下拉变更忽略空值和信号阻塞期，其余候选直接调用 _start_music_write()；成功后显示回读值，失败后恢复已确认值。

- [ ] **Step 4: 验证音乐回归**

~~~bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest   test_douyin_commerce_service.DouyinCommerceUiTests
~~~

Expected: PASS；不存在“确认使用这首音乐”按钮。

### Task 3: 定位和声明选中即写入

**Files:**
- Modify: ui/douyin_commerce_page.py:992-1145,1712-1765,2555-2675
- Modify: ui/common.py:820-940
- Test: test_douyin_commerce_service.py:1450-2050

**Interfaces:**
- Consumes: apply_location()、select_content_declaration()。
- Produces: _start_location_write(location)、declaration_group、declaration_buttons、_start_declaration_write(declaration)。

- [ ] **Step 1: 写失败测试**

~~~python
def test_location_click_immediately_starts_platform_write(self) -> None:
    self.page._session_id = "session-demo"
    self.page._show_locations([self.location_a])
    with patch.object(self.page, "_start_location_write") as write:
        self.page.location_result_list.setCurrentRow(0)
    write.assert_called_once_with(self.location_a)

def test_declaration_defaults_to_none_and_click_starts_write(self) -> None:
    self.page._session_id = "session-demo"
    default = "无需添加自主声明"
    self.assertTrue(self.page.declaration_buttons[default].isChecked())
    with patch.object(self.page, "_start_declaration_write") as write:
        self.page.declaration_buttons["内容由AI生成"].click()
    write.assert_called_once_with("内容由AI生成")
~~~

- [ ] **Step 2: 运行失败测试**

~~~bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest   test_douyin_commerce_service.DouyinCommerceUiTests.test_location_click_immediately_starts_platform_write   test_douyin_commerce_service.DouyinCommerceUiTests.test_declaration_defaults_to_none_and_click_starts_write
~~~

Expected: FAIL，提示即时写入入口或声明单选集合不存在。

- [ ] **Step 3: 实现地点立即写入**

_location_selected() 直接调用 _start_location_write(location)，不再显示第二个确认按钮。成功后显示完整已确认地址并收起候选；失败时清除暂选并保留搜索结果供重新选择。

- [ ] **Step 4: 实现声明默认单选**

使用 QButtonGroup 和 QRadioButton 展示全部 CONTENT_DECLARATION_OPTIONS。默认勾选“无需添加自主声明”；上传会话成功后立即写入并回读该默认项。点击其他单选直接调用 _start_declaration_write()；失败时恢复上次已确认值。

- [ ] **Step 5: 验证定位与声明回归**

~~~bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest   test_douyin_commerce_service.DouyinCommerceUiTests
~~~

Expected: PASS；定位无第二次确认，声明不是下拉框。

### Task 4: 定时独立可编辑，检查时才写入平台

**Files:**
- Modify: ui/douyin_commerce_page.py:1124-1165,2019-2170
- Test: test_douyin_commerce_service.py:1390-1530,1980-2050

**Interfaces:**
- Consumes: _future_schedule_text()、timer_enabled、schedule_date、schedule_time。
- Produces: 立即可编辑的定时控件；不改 collect_payload() 和最终检查执行器边界。

- [ ] **Step 1: 写失败测试**

~~~python
def test_schedule_controls_are_available_before_other_platform_readbacks(self) -> None:
    self.page._session_id = "session-demo"
    self.page._sync_view()
    self.assertTrue(self.page.timer_enabled.isEnabled())
    self.page.timer_enabled.setChecked(True)
    self.assertTrue(self.page.schedule_date.isEnabled())
    self.assertTrue(self.page.schedule_time.isEnabled())
~~~

- [ ] **Step 2: 运行失败测试**

~~~bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest   test_douyin_commerce_service.DouyinCommerceUiTests.test_schedule_controls_are_available_before_other_platform_readbacks
~~~

Expected: FAIL，当前实现需要声明回读才启用定时。

- [ ] **Step 3: 解除定时界面锁定**

将 can_configure_schedule 改为 bool(self._session_id) and not self._content_dirty_after_upload and not busy。保留 _can_review() 对音乐、定位、声明回读和未来 Asia/Shanghai 时间的检查。_timer_enabled_changed() 与 _schedule_changed() 不得调用会话执行器。

- [ ] **Step 4: 验证定时边界**

~~~bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest   test_douyin_commerce_service.DouyinCommerceUiTests   test_douyin_commerce_service.DouyinCommerceTaskPresentationTests
~~~

Expected: PASS；定时可先填，检查前不触发平台发表设置。

### Task 5: 完整离线验收与精确提交

**Files:**
- Modify: test_douyin_commerce_service.py

**Interfaces:**
- Consumes: 前四任务的即时写入和定时边界。
- Produces: 完整离线证据，不生成平台草稿、上传或发布记录。

- [ ] **Step 1: 运行完整验证**

~~~bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest
QT_QPA_PLATFORM=offscreen .venv/bin/python desktop_native_app.py --ui-test
.venv/bin/python -m py_compile ui/douyin_commerce_page.py ui/background_task.py app_core/douyin_commerce_session.py uploader/douyin_uploader/main.py
git diff --check
~~~

Expected: 全部 PASS、NATIVE_DESKTOP_UI_OK；没有启动真实抖音浏览器、上传、草稿、预检或发表。

- [ ] **Step 2: 审查并提交本轮 hunk**

~~~bash
git add -p ui/douyin_commerce_page.py ui/common.py test_douyin_commerce_service.py
git diff --cached --check
git commit -m "feat: 优化抖音带货即时设置"
~~~

只暂存本计划新增 hunk；不得将数据库、storage state、截图、日志或既有用户未提交改动纳入提交。

## 自审结果

- **规格覆盖：** Task 2 覆盖音乐下拉直选；Task 3 覆盖定位单击和声明默认单选；Task 4 覆盖定时独立可编辑；Task 1 覆盖串行写入和失败恢复。
- **安全边界：** 所有平台修改仍经现有会话执行器和回读；计划不新增 API、浏览器前置、草稿或发表动作。
- **占位扫描：** 本计划没有 TODO、TBD 或未定义的后续处理。
- **接口一致性：** 音乐、定位和声明都通过 Task 1 的共享任务键调用现有会话接口；定时不调用会话接口。

