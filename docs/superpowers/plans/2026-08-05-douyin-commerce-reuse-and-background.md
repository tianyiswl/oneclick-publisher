# 抖音带货内容复用与后台会话 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 让抖音带货在同一有效编辑会话中只同步标题、文案和标签；仅账号或视频变更才重新上传，并将上传浏览器默认保持在后台。

**Architecture:** DouyinCommerceSessionManager 继续拥有唯一的内存编辑会话，并新增内容同步入口；DouYinVideo 负责将内容字段写回同一页面并回读。DouyinCommercePage 比较“上传身份”和“可同步内容”两份快照，决定无动作、同步或重新上传，不再使用一个笼统的脏标记阻断全部流程。

**Tech Stack:** Python 3、PyQt6、Playwright、现有 BackgroundTaskRunner、unittest。

## Global Constraints

- 所有新增注释和用户文案使用 UTF-8 中文。
- 不使用官方 API、签名参数、私有接口或本地门店库。
- 不保存草稿、不预览、不发表；本计划的自动验证只运行离线/本地 Qt 测试。
- 内容同步、音乐、定位和声明的成功都以当前编辑页回读为准；本地控件值不等于平台状态。
- 上传与内容同步默认后台运行；登录失效时返回现有“到账号管理重新登录”状态，不在抖音带货页代替用户登录。
- 最终提交前台展示与二维码/验证码处理逻辑不在本计划中改变。
- 平台设置页不保留固定说明句，也不显示“已由当前抖音编辑页回读确认”“已确认一首收藏音乐”等重复文案。
- 工作区已有未提交改动；每次提交只允许精确暂存本计划新增 hunk，绝不暂存 storage state、数据库、日志、截图或无关改动。

## File Structure

- app_core/douyin_commerce_session.py：管理器负责会话身份校验、内容同步调用、预检指纹失效和后台浏览器上下文。
- uploader/douyin_uploader/main.py：唯一负责标题、文案和标签向同一抖音编辑页写入及回读。
- ui/douyin_commerce_page.py：负责内容差异投影、后台任务分派、三种主操作和平台设置双栏展示。
- test_douyin_commerce_service.py：覆盖会话契约、UI 差异分流、布局文案与后台模式，不连接真实平台。

---

### Task 1: 为同一编辑会话增加内容同步契约

**Files:**

- Modify: app_core/douyin_commerce_session.py:146-174,210-260,373-455,760-825
- Modify: uploader/douyin_uploader/main.py:292-370
- Test: test_douyin_commerce_service.py:2259-2645

**Interfaces:**

- Consumes: DouYinVideo.clear_platform_title(page)、DouYinVideo.fill_description_and_topics(page)、DouyinCommerceSessionManager._current(session_id)。
- Produces: DouYinVideo.sync_uploaded_editor_content(page, *, title, description, tags) -> dict[str, Any]。
- Produces: DouyinCommerceSessionManager.synchronize_content(session_id, payload, *, on_progress=None) -> dict[str, Any]。
- Produces: _session_identity_fingerprint(payload) -> str 和 _content_fingerprint(payload) -> str；前者只含账号与视频，后者只含标题、文案和标签。

- [x] **Step 1: 写会话内同步的失败测试**

~~~python
def test_content_sync_updates_fields_without_creating_a_new_upload_session(self) -> None:
    class OpenPage:
        def is_closed(self) -> bool:
            return False

    class Uploader:
        sync_uploaded_editor_content = AsyncMock(
            return_value={
                "title": "修改后的标题",
                "description": "修改后的文案",
                "tags": ["北海", "团购"],
                "form": {"title_confirmed": True},
            }
        )

    manager = douyin_commerce_session.DouyinCommerceSessionManager()
    uploader = Uploader()
    manager._session = douyin_commerce_session._CommerceEditorSession(
        session_id="session-demo",
        upload_payload={
            "accountList": ["oneclick_3_demo.json"],
            "fileList": ["/tmp/demo.mp4"],
            "title": "原标题",
            "description": "原文案",
            "tags": ["北海"],
        },
        account_name="测试账号",
        browser=None,
        context=None,
        page=OpenPage(),
        playwright=None,
        uploader=uploader,
    )
    payload = {
        **manager._session.upload_payload,
        "title": "修改后的标题",
        "description": "修改后的文案",
        "tags": ["北海", "团购"],
    }

    result = asyncio.run(manager._synchronize_content("session-demo", payload))

    uploader.sync_uploaded_editor_content.assert_awaited_once()
    self.assertEqual(result["status"], "synced")
    self.assertEqual(manager._session.upload_payload["title"], "修改后的标题")
    self.assertEqual(manager._session.preflight_fingerprint, "")

def test_content_sync_rejects_changed_account_or_video_before_editor_write(self) -> None:
    class OpenPage:
        def is_closed(self) -> bool:
            return False

    class Uploader:
        sync_uploaded_editor_content = AsyncMock()

    upload_payload = {
        "accountList": ["oneclick_3_demo.json"],
        "fileList": ["/tmp/demo.mp4"],
        "title": "原标题",
        "description": "原文案",
        "tags": [],
    }
    manager = douyin_commerce_session.DouyinCommerceSessionManager()
    uploader = Uploader()
    manager._session = douyin_commerce_session._CommerceEditorSession(
        session_id="session-demo",
        upload_payload=upload_payload,
        account_name="测试账号",
        browser=None,
        context=None,
        page=OpenPage(),
        playwright=None,
        uploader=uploader,
    )
    changed = dict(upload_payload, accountList=["oneclick_3_other.json"])
    with self.assertRaisesRegex(
        douyin_commerce_session.DouyinCommerceSessionError, "账号或视频"
    ):
        asyncio.run(manager._synchronize_content("session-demo", changed))
    uploader.sync_uploaded_editor_content.assert_not_awaited()
~~~

- [x] **Step 2: 运行失败测试，确认接口尚不存在**

~~~bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest \
  test_douyin_commerce_service.DouyinCommerceSessionContractTests.test_content_sync_updates_fields_without_creating_a_new_upload_session \
  test_douyin_commerce_service.DouyinCommerceSessionContractTests.test_content_sync_rejects_changed_account_or_video_before_editor_write
~~~

Expected: FAIL，提示 synchronize_content 或 _synchronize_content 不存在。

- [x] **Step 3: 在上传器中封装字段同步和回读**

~~~python
async def sync_uploaded_editor_content(
    self,
    page: Page,
    *,
    title: str,
    description: str,
    tags: list[str],
) -> dict[str, Any]:
    self.title = str(title).strip()
    self.description = str(description).strip()
    self.tags = [str(tag).strip().lstrip("#") for tag in tags if str(tag).strip()]
    await self.clear_platform_title(page)
    await self.fill_description_and_topics(page)
    form = await self.verify_prepublish_form(page, require_covers=False)
    return {
        "title": self.title,
        "description": self.description,
        "tags": list(self.tags),
        "form": dict(form or {}),
    }
~~~

此方法不得调用上传 input、封面、音乐、定位、声明、定时、草稿、预览或发表。

- [x] **Step 4: 在会话管理器中拆分身份与内容指纹**

~~~python
@staticmethod
def _session_identity_fingerprint(payload: Mapping[str, Any]) -> str:
    return "|".join((
        _normalized((payload.get("accountList") or [""])[0]),
        _normalized((payload.get("fileList") or [""])[0]),
    ))

@staticmethod
def _content_fingerprint(payload: Mapping[str, Any]) -> str:
    return "|".join((
        _normalized(payload.get("title")),
        _normalized(payload.get("description")),
        ",".join(_normalized(tag) for tag in payload.get("tags") or []),
    ))
~~~

实现同步入口时先用 validate_douyin_commerce_upload_payload() 规范化载荷，再比较身份指纹；不一致立即抛出“账号或视频已变化，请重新上传”。一致时调用上传器同步方法，写回 session.upload_payload，清空 session.preflight_fingerprint，保留已回读的音乐、地点、声明和会话标识。

- [x] **Step 5: 更新预检与提交的会话匹配错误分类**

~~~python
if self._session_identity_fingerprint(payload) != self._session_identity_fingerprint(session.upload_payload):
    raise DouyinCommerceSessionError("账号或视频已变化，请重新上传")
if self._content_fingerprint(payload) != self._content_fingerprint(session.upload_payload):
    raise DouyinCommerceSessionError("标题、文案或标签已变化，请先同步内容")
~~~

这样预检/提交不会把未同步的本地内容伪装成当前编辑页内容。

- [x] **Step 6: 运行会话契约回归测试**

~~~bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest \
  test_douyin_commerce_service.DouyinCommerceSessionContractTests
~~~

Expected: PASS；同步测试不调用上传方法、内容不同步不能通过预检匹配。

- [ ] **Step 7: 精确提交本任务代码**

~~~bash
git add -p uploader/douyin_uploader/main.py app_core/douyin_commerce_session.py test_douyin_commerce_service.py
git diff --cached --check
git commit -m "feat: 支持抖音带货同会话内容同步"
~~~

---

### Task 2: 让内容页按差异选择继续、同步或重新上传

**Files:**

- Modify: ui/douyin_commerce_page.py:304-322,531-535,568-703,1715-1744,1995-2080,2293-2425,2468-2500
- Test: test_douyin_commerce_service.py:1201-2230

**Interfaces:**

- Consumes: DouyinCommerceSessionManager.synchronize_content(session_id, payload, on_progress)、DouyinCommerceSessionManager.start_upload(payload, on_progress)。
- Produces: _content_change_kind() -> str，返回 "none"、"sync"、"reupload"；continue_after_content() 作为内容页唯一底部主操作。

- [x] **Step 1: 写 UI 差异分流失败测试**

~~~python
def test_unchanged_content_returns_to_platform_settings_without_runner_work(self) -> None:
    self.page._session_id = "session-demo"
    self.page._uploaded_editor_payload = self.page.collect_upload_payload()
    with patch.object(self.page.runner, "run") as run:
        self.page.continue_after_content()
    run.assert_not_called()
    self.assertEqual(self.page.pages.currentIndex(), 1)

def test_text_only_change_uses_content_sync_not_video_upload(self) -> None:
    self.page._session_id = "session-demo"
    self.page._uploaded_editor_payload = self.page.collect_upload_payload()
    self.page.title_input.setText("仅修改标题")
    with patch.object(self.page.runner, "run", return_value=True) as run:
        self.page.continue_after_content()
    self.assertEqual(run.call_args.args[0], "douyin_commerce_sync_content")

def test_account_or_video_change_requires_full_upload(self) -> None:
    self.page._session_id = "session-demo"
    self.page._uploaded_editor_payload = self.page.collect_upload_payload()
    self.page.video_combo.setCurrentIndex(self.page.video_combo.currentIndex() + 1)
    self.assertEqual(self.page._content_change_kind(), "reupload")
~~~

- [x] **Step 2: 运行失败测试**

~~~bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest \
  test_douyin_commerce_service.DouyinCommerceUiTests.test_unchanged_content_returns_to_platform_settings_without_runner_work \
  test_douyin_commerce_service.DouyinCommerceUiTests.test_text_only_change_uses_content_sync_not_video_upload \
  test_douyin_commerce_service.DouyinCommerceUiTests.test_account_or_video_change_requires_full_upload
~~~

Expected: FAIL，提示 continue_after_content 或 _uploaded_editor_payload 不存在。

- [x] **Step 3: 用快照替代内容全量脏标记**

~~~python
def _content_change_kind(self) -> str:
    if not self._session_id or not self._uploaded_editor_payload:
        return "reupload"
    current = self.collect_upload_payload()
    if self._upload_identity(current) != self._upload_identity(self._uploaded_editor_payload):
        return "reupload"
    if self._content_fields(current) != self._content_fields(self._uploaded_editor_payload):
        return "sync"
    return "none"
~~~

内容变更回调只清空预检指纹并刷新投影；不得再直接把任意字段改动设成“需重新上传”。上传成功回调使用启动时保存的上传载荷写入 _uploaded_editor_payload；若上传期间用户改了字段，则上传完成后立即重新计算差异，不覆盖用户后续输入。

- [x] **Step 4: 将底部按钮改为分流入口**

~~~python
def continue_after_content(self) -> None:
    change_kind = self._content_change_kind()
    if change_kind == "none":
        self._go_to_step(1)
        return
    if change_kind == "sync":
        self._start_content_sync(self.collect_upload_payload())
        return
    self.start_upload()
~~~

将原 operation_dock_upload.clicked.connect(self.start_upload) 改为连接此入口。按钮文字分别为“上传视频并继续”“同步内容并继续”“重新上传视频并继续”“继续平台设置”；状态只使用这些短文本，不再显示“内容已变更，需要重新上传”的固定长句。

- [x] **Step 5: 实现内容同步回调与失败回退**

~~~python
def _start_content_sync(self, payload: dict[str, Any]) -> None:
    session_id = self._session_id
    self.runner.run(
        "douyin_commerce_sync_content",
        with_progress=lambda report: commerce_session_manager.synchronize_content(
            session_id, payload, on_progress=report
        ),
        on_progress=self._set_commerce_progress,
        on_success=lambda result: self._content_sync_succeeded(payload, result),
        on_error=self._content_sync_failed,
        on_finished=self._sync_view,
    )
~~~

成功时更新 _uploaded_editor_payload、清空 _preflight_fingerprint、保留音乐/定位/声明/定时状态并进入平台设置。失败时保留用户表单，保持在内容页；若错误说明会话已关闭或已失效，则将状态投影为“重新上传视频并继续”，不自动启动上传。

- [x] **Step 6: 调整检查、步骤条与恢复按钮的判断**

所有旧的“上传后内容有改动”判断改为基于 _content_change_kind() == "none" 与同步任务是否运行。内容同步尚未完成时禁用检查；已同步后恢复原有预检条件。恢复本地内容仍不得创建会话或后台任务。

- [x] **Step 7: 运行内容页回归测试**

~~~bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest \
  test_douyin_commerce_draft_service \
  test_douyin_commerce_service.DouyinCommerceUiTests
~~~

Expected: PASS；无变化不启动任务，文字改动只启动同步任务，账号/视频改动只走上传路径。

- [ ] **Step 8: 精确提交本任务代码**

~~~bash
git add -p ui/douyin_commerce_page.py test_douyin_commerce_service.py
git diff --cached --check
git commit -m "feat: 按内容差异复用抖音带货会话"
~~~

---

### Task 3: 重排平台设置并移除重复状态文案

**Files:**

- Modify: ui/douyin_commerce_page.py:889-1185,2110-2225
- Test: test_douyin_commerce_service.py:1566-1705,2030-2180

**Interfaces:**

- Consumes: 已有 music_panel、location_panel、declaration_panel、schedule_panel 和即时写入回调。
- Produces: 左栏顺序“音乐、声明”，右栏顺序“定位、定时”；动态状态标签只在处理中或失败时可见。

- [x] **Step 1: 写布局与精简文案失败测试**

~~~python
def test_platform_workspace_orders_music_and_declaration_on_left(self) -> None:
    left_layout = self.page.platform_left_column.layout()
    right_layout = self.page.platform_right_column.layout()
    self.assertIs(left_layout.itemAt(0).widget(), self.page.music_panel)
    self.assertIs(left_layout.itemAt(1).widget(), self.page.declaration_panel)
    self.assertIs(right_layout.itemAt(0).widget(), self.page.location_panel)
    self.assertIs(right_layout.itemAt(1).widget(), self.page.schedule_panel)

def test_platform_workspace_hides_redundant_readback_copy_when_idle(self) -> None:
    self.page._session_id = "session-demo"
    self.page._selected_music = {"musicId": "m-1", "title": "收藏音乐"}
    self.page._declaration_applied = True
    self.page._sync_view()
    visible_text = "\n".join(
        label.text() for label in self.page.findChildren(QLabel) if not label.isHidden()
    )
    self.assertNotIn("已由当前抖音编辑页回读确认", visible_text)
    self.assertNotIn("已确认一首收藏音乐", visible_text)
~~~

- [x] **Step 2: 运行失败测试**

~~~bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest \
  test_douyin_commerce_service.DouyinCommerceUiTests.test_platform_workspace_orders_music_and_declaration_on_left \
  test_douyin_commerce_service.DouyinCommerceUiTests.test_platform_workspace_hides_redundant_readback_copy_when_idle
~~~

Expected: FAIL，当前左栏第二项是定位，且仍存在重复成功文案。

- [x] **Step 3: 重排四个区域，不新增固定介绍段落**

~~~python
left_layout.addWidget(self.music_stage)
left_layout.addWidget(self.declaration_stage)
right_layout.addWidget(self.location_stage)
right_layout.addWidget(self.schedule_stage)
~~~

不添加“可任意顺序设置，选择后自动同步”或替代说明。保留步骤条、区域标题、表单控件、完整地址和底部“检查并继续”。

- [x] **Step 4: 删除静态回读复述，只保留操作值和异常状态**

音乐由下拉框显示当前选项；声明由单选项显示当前选项；地点完成后只保留名称、完整地址和“更换定位”；定时只保留开关和日期时间。删除 music_card、declaration_card 及其成功状态文本，正常空闲时隐藏 music_status、declaration_status、platform_session_status；同步中和失败时复用现有短状态/阶段错误标签。

- [x] **Step 5: 运行平台设置 UI 回归测试**

~~~bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest \
  test_douyin_commerce_service.DouyinCommerceUiTests
~~~

Expected: PASS；四项仍可独立配置，已选地点完整地址仍可见，无重复回读文案。

- [ ] **Step 6: 精确提交本任务代码**

~~~bash
git add -p ui/douyin_commerce_page.py test_douyin_commerce_service.py
git diff --cached --check
git commit -m "feat: 精简抖音带货平台设置界面"
~~~

---

### Task 4: 修正上传会话的后台浏览器策略并完成离线验收

**Files:**

- Modify: ui/douyin_commerce_page.py:2293-2360
- Modify: app_core/douyin_commerce_session.py:373-455
- Test: test_douyin_commerce_service.py:35-185,2259-2645

**Interfaces:**

- Consumes: utils.publish_observer.publish_context()、utils.base_social_media.launch_publish_browser()、既有 needs_login 返回值。
- Produces: DouyinCommerceSessionManager._background_upload_mode(payload) -> bool；上传和内容同步使用后台会话，最终提交不受影响。

- [x] **Step 1: 写后台模式失败测试**

~~~python
def test_upload_payload_defaults_to_background_mode(self) -> None:
    payload = self.page.collect_upload_payload()
    self.assertTrue(payload["backgroundMode"])

def test_session_manager_defaults_upload_context_to_background(self) -> None:
    manager = douyin_commerce_session.DouyinCommerceSessionManager()
    self.assertTrue(manager._background_upload_mode({}))
    self.assertTrue(manager._background_upload_mode({"backgroundMode": True}))
    self.assertFalse(manager._background_upload_mode({"backgroundMode": False}))
~~~

- [x] **Step 2: 运行失败测试**

~~~bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest \
  test_douyin_commerce_service.DouyinCommerceUiTests.test_upload_payload_defaults_to_background_mode \
  test_douyin_commerce_service.DouyinCommerceSessionContractTests.test_session_manager_defaults_upload_context_to_background
~~~

Expected: FAIL，当前载荷和上传上下文固定为 False。

- [x] **Step 3: 让上传与内容同步默认后台运行**

~~~python
@staticmethod
def _background_upload_mode(payload: Mapping[str, Any]) -> bool:
    return bool(payload.get("backgroundMode", True))

with publish_context(
    mode="douyin_commerce_upload",
    background_mode=self._background_upload_mode(payload),
    platform_type=3,
    platform_name="抖音",
):
    browser = await launch_publish_browser(playwright)
~~~

collect_upload_payload() 与 collect_payload() 统一写入 backgroundMode=True。内容同步不创建新浏览器，只复用现有后台会话。若后台页面判断为登录失效，保留已有 needs_login 返回值，关闭临时资源，并由客户端显示账号管理入口；不得在当前发布页打开登录窗口。

- [x] **Step 4: 确认进度和最终提交边界未被改动**

保留 _UPLOAD_PROGRESS、_set_commerce_progress() 和上传/同步按钮禁用状态；不修改 _submit() 中最终不可逆步骤前的 reveal_page_window()，不增加任何自动点击。

- [x] **Step 5: 运行完整离线验证**

~~~bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest
QT_QPA_PLATFORM=offscreen .venv/bin/python desktop_native_app.py --ui-test
.venv/bin/python -m py_compile \
  ui/douyin_commerce_page.py \
  app_core/douyin_commerce_session.py \
  uploader/douyin_uploader/main.py
git diff --check
~~~

Expected: 全部 PASS、输出 NATIVE_DESKTOP_UI_OK；不启动真实抖音浏览器，不上传、不保存草稿、不预检、不发表。

- [ ] **Step 6: 启动开发版进行人工 UI 验收**

~~~bash
pkill -f 'desktop_native_app.py --page commerce' || true
nohup .venv/bin/python desktop_native_app.py --page commerce \
  >/tmp/oneclick-douyin-commerce.log 2>&1 &
~~~

人工验收仅覆盖：平台设置双栏顺序、冗余文字消失、无修改的“继续平台设置”、标题改动后的“同步内容并继续”、账号/视频改动后的“重新上传视频并继续”。不点击上传、不访问平台、不创建草稿或发布。

- [ ] **Step 7: 精确提交本任务代码**

~~~bash
git add -p ui/douyin_commerce_page.py app_core/douyin_commerce_session.py test_douyin_commerce_service.py
git diff --cached --check
git commit -m "fix: 后台运行抖音带货上传会话"
~~~

## 自审结果

- **规格覆盖：** Task 1 实现同会话内容同步和身份安全边界；Task 2 实现无变化/内容变化/身份变化三种操作；Task 3 覆盖固定双栏与文案精简；Task 4 覆盖后台浏览器和本地验证。
- **安全边界：** 所有任务保持现有编辑会话、平台字段回读、账号管理登录入口和最终确认边界；没有新增草稿、预览、发表或私有接口。
- **占位扫描：** 没有占位词、遗漏接口或延后处理步骤。
- **类型一致性：** 会话层以 Mapping[str, Any] 接收载荷；上传器以显式 title/description/tags 同步字段；UI 只调用已定义的 synchronize_content()。
