# 抖音带货连续三栏工作台 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use `- [ ]` syntax for tracking.

**Goal:** 将一键发开发版的“抖音带货”页面改造成连续、可恢复、并严格保留现有平台安全门槛的三栏桌面工作台。

**Architecture:** 保留 DouyinCommercePage 现有任务载荷、BackgroundTaskRunner 与抖音编辑会话调用；页面层新增一个由已验证状态推导出的“当前平台步骤”，用 QStackedWidget 只呈现本时刻的主操作。地点候选改为独立内存状态，确认后显示名称与完整地址卡，避免依赖隐藏列表的选中行。douyin_commerce_draft_service 继续只保存本机内容准备，不保存会话或平台数据。

**Tech Stack:** Python、PyQt6、SQLite、本项目现有 unittest 离线测试、BackgroundTaskRunner。

## Global Constraints

- 所有用户可见文案、源码注释与新增文档使用 UTF-8 中文。
- 仅调整 ui/douyin_commerce_page.py、ui/common.py 与对应离线/UI 测试；不修改抖音真实选择器、账号登录、上传、草稿、预检或发表执行器。
- 首版继续仅支持一个状态正常的抖音账号和一条本地视频；不新增门店、商品、图文、文字、批量账号或其他平台能力。
- 音乐、地点、作品内容声明均必须由用户选择并由当前抖音编辑页回读；不得默认选择或猜测。
- 地点范围默认“国内”，可切换“本地”；地点范围或关键词变化必须清空旧地点与声明确认。
- 定时为可选项；开启时必须继续使用 Asia/Shanghai 的未来时间校验，未开启时保持立即发表。
- 本地保存只能保存账号引用、视频引用、标题、文案、标签和更新时间；不得保存 Cookie、二维码、平台临时会话、平台原始请求或凭据。
- 本轮验证不执行真实上传、保存草稿、预检或发表；开发版验收只打开本地页面与离线状态。
- 工作区已有未提交改动；每次提交前通过 git add -p 仅暂存本计划产生的代码块，不能把既有改动、运行产物、数据库、storage state、截图或临时文件纳入提交。

---

## 文件职责

| 文件 | 责任 |
| --- | --- |
| ui/douyin_commerce_page.py | 三栏布局、派生步骤状态、地点选择/确认状态、错误提示、按钮门槛与本地恢复交互。 |
| ui/common.py | 抖音带货三栏卡片、会话清单、阶段错误卡、定位确认卡的纯视觉样式。 |
| test_douyin_commerce_service.py | 不启动平台浏览器的 PyQt 页面状态、交互门槛、完整地址、错误脱敏与检查页回归。 |
| test_douyin_commerce_draft_service.py | 本地保存/恢复与标签历史不含平台会话或凭据的边界回归。 |

不修改 app_core/douyin_commerce_draft_service.py：当前 normalize_content_draft() 已只接受账号、视频、标题、文案、标签，满足本轮保存边界；本计划不扩大持久化字段。

## 共享接口约定

以下成员定义在 DouyinCommercePage，并由本计划的后续任务复用：

~~~
_PLATFORM_STAGE_ORDER = ("blocked", "music", "location", "declaration", "schedule")

def _current_platform_stage(self) -> str:
    """依据已回读状态返回 blocked、music、location、declaration 或 schedule。"""

def _sync_workbench(self) -> None:
    """刷新三栏卡片、阶段堆栈、会话清单、操作栏和步骤状态，不访问平台。"""

def _set_stage_error(self, stage: str, diagnostic: str) -> None:
    """记录本机诊断，并在对应卡片显示脱敏原因与可执行下一步。"""

def _clear_stage_error(self, stage: str) -> None:
    """清空指定阶段的用户提示，不清空平台已回读状态。"""

def _selected_location(self) -> dict[str, str] | None:
    """返回独立保存的地点候选，而非依赖 QListWidget 是否可见。"""
~~~

_selected_location_data: dict[str, str] | None 只保存当前任务的 poiId、名称、完整地址和距离。_stage_error_labels: dict[str, QLabel] 的键固定为 content、music、location、declaration、schedule。

### Task 1: 建立可测试的派生步骤状态与安全错误呈现

**Files:**
- Modify: ui/douyin_commerce_page.py:1-180, 850-1591
- Modify: test_douyin_commerce_service.py:1189-1554

**Interfaces:**
- Consumes: 既有 _session_id、_selected_music、_location_applied、_declaration_applied、_content_dirty_after_upload、_busy()。
- Produces: _current_platform_stage()、_sync_workbench()、_set_stage_error()、_clear_stage_error()、_selected_location_data。

- [ ] **Step 1: 写出派生阶段与错误脱敏的失败测试**

在 DouyinCommerceUiTests 中加入下列测试；只改变页面内存状态，不调用 runner.run()：

~~~
def test_platform_stage_follows_verified_readbacks(self) -> None:
    self.page._session_id = "session-demo"
    self.assertEqual(self.page._current_platform_stage(), "music")

    self.page._selected_music = {
        "musicId": "music-001", "title": "出埃及记",
        "creator": "石Yuchi", "duration": "01:08",
    }
    self.assertEqual(self.page._current_platform_stage(), "location")

    self.page._selected_location_data = dict(self.location_a)
    self.page._location_applied = True
    self.assertEqual(self.page._current_platform_stage(), "declaration")

    self.page.declaration_combo.setCurrentIndex(1)
    self.page._declaration_applied = True
    self.assertEqual(self.page._current_platform_stage(), "schedule")


def test_stage_error_hides_automation_detail_and_keeps_retry_action(self) -> None:
    diagnostic = "Locator.click: Timeout 5000ms exceeded"
    self.page._set_stage_error("declaration", diagnostic)

    text = self.page._stage_error_labels["declaration"].text()
    self.assertIn("失败原因：", text)
    self.assertIn("下一步：", text)
    self.assertNotIn("Locator.click", text)
~~~

同时把现有 test_declaration_error_stays_in_current_window 改为断言 QMessageBox.warning 未被调用，声明卡包含“失败原因：”与“下一步：”，且不包含传入的诊断字符串；错误必须留在触发它的阶段卡内。

- [ ] **Step 2: 运行测试确认当前实现尚无接口**

Run:

~~~bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest \
  test_douyin_commerce_service.DouyinCommerceUiTests.test_platform_stage_follows_verified_readbacks \
  test_douyin_commerce_service.DouyinCommerceUiTests.test_stage_error_hides_automation_detail_and_keeps_retry_action
~~~

Expected: FAIL，提示 _current_platform_stage、_set_stage_error 或 _stage_error_labels 不存在。

- [ ] **Step 3: 加入统一的状态和错误投影实现**

在模块顶部加入日志器，在 __init__ 中初始化候选地点与阶段错误容器：

~~~
import logging

LOGGER = logging.getLogger(__name__)

# __init__ 内
self._selected_location_data: dict[str, str] | None = None
self._stage_error_labels: dict[str, QLabel] = {}
~~~

在 DouyinCommercePage 中实现以下方法。用户可见错误只使用固定中文，原始 diagnostic 只写入本机诊断日志：

~~~
def _current_platform_stage(self) -> str:
    if not self._session_id or self._content_dirty_after_upload:
        return "blocked"
    if self._selected_music is None:
        return "music"
    if not self._location_applied:
        return "location"
    if not self._declaration_applied:
        return "declaration"
    return "schedule"

def _set_stage_error(self, stage: str, diagnostic: str) -> None:
    copy = {
        "content": ("本机内容尚未形成可上传条件", "核对账号、视频、标题和文案后重新上传。"),
        "music": ("平台未返回可确认的收藏音乐", "回到音乐步骤，重新读取并手动选择一首音乐。"),
        "location": ("平台未返回可唯一确认的完整地点", "核对范围和关键词后重新搜索，再确认完整地址。"),
        "declaration": ("平台未回读与所选值一致的作品内容声明", "关闭当前说明浮层后重新选择并确认声明。"),
        "schedule": ("发布时间未满足当前设置条件", "选择立即发表，或填写未来的北京时间。"),
    }
    reason, next_action = copy[stage]
    LOGGER.warning("douyin-commerce %s failed: %s", stage, diagnostic)
    self._stage_error_labels[stage].setText(f"失败原因：{reason}\n下一步：{next_action}")
    self._stage_error_labels[stage].setVisible(True)

def _clear_stage_error(self, stage: str) -> None:
    self._stage_error_labels[stage].clear()
    self._stage_error_labels[stage].setVisible(False)

def _stage_error_label(self, stage: str) -> QLabel:
    label = QLabel()
    label.setObjectName("douyinCommerceStageError")
    label.setWordWrap(True)
    label.setVisible(False)
    self._stage_error_labels[stage] = label
    return label
~~~

在内容、音乐、地点、声明和定时卡各自的布局末尾调用 _stage_error_label("<阶段名>") 并 addWidget 返回的标签；这使测试构造页面后每个阶段都有固定错误容器。将 _selected_location() 改为优先复制 self._selected_location_data；在 _show_locations()、_location_scope_changed()、_location_keyword_edited()、_abandon_session() 清空它，在 _location_selected() 与 _location_applied_success() 写入标准化候选。把 _sync_view() 的布局/状态刷新部分移入 _sync_workbench()，并让 _sync_view() 最后调用它，保留现有信号调用入口。

- [ ] **Step 4: 将平台操作错误接到阶段错误卡**

新增下列包装，并让上传、音乐、定位、声明的 on_error 使用对应阶段键：

~~~
def _platform_action_error(self, stage: str, diagnostic: str) -> None:
    self._set_stage_error(stage, diagnostic)
    self._sync_view()

# 声明回调示例
on_error=lambda diagnostic: self._platform_action_error(
    "declaration", diagnostic
)
~~~

预检和最终提交保持既有任务记录和平台回执逻辑，不把它们改成 UI 错误卡。

- [ ] **Step 5: 运行状态与声明错误回归**

Run:

~~~bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest \
  test_douyin_commerce_service.DouyinCommerceUiTests
~~~

Expected: PASS；声明错误只留在声明阶段卡，用户可见错误不包含 Locator.click、Timeout 或 DOM 选择器文本。

- [ ] **Step 6: 仅暂存本任务变更并建立可回退提交**

Run:

~~~bash
git add -p ui/douyin_commerce_page.py test_douyin_commerce_service.py
git diff --cached --check
git diff --cached --stat
git commit -m "feat: 收束抖音带货页面状态与错误反馈"
~~~

Expected: 暂存区只包含本任务的派生状态、地点内存状态和错误脱敏测试；既有未提交块仍留在工作区。

### Task 2: 重构内容准备为稳定的三栏与本地保存入口

**Files:**
- Modify: ui/douyin_commerce_page.py:179-466, 795-1167, 1331-1591
- Modify: ui/common.py:697-968
- Modify: test_douyin_commerce_service.py:1351-1554
- Modify: test_douyin_commerce_draft_service.py:25-77

**Interfaces:**
- Consumes: Task 1 的 _sync_workbench()、_set_stage_error()；既有 save_content()、restore_saved_content()、_saved_content_payload()、list_tag_history()。
- Produces: 内容页对象名 douyinCommerceContentAccountColumn、douyinCommerceContentBodyColumn、douyinCommerceContentVideoColumn；douyinCommerceRestoreContent 和 douyinCommerceSaveContent 位于各自三栏；操作栏只保留上传主操作；视频卡明确展示素材管理已提供的时长/尺寸或“未提供”。

- [ ] **Step 1: 写出三栏内真实操作位置和保存边界的失败测试**

替换“固定操作栏包含恢复/保存”的旧断言，加入：

~~~
def test_content_workbench_places_restore_save_and_upload_in_separate_roles(self) -> None:
    self.page.pages.setCurrentIndex(0)
    self.page._sync_view()

    account_column = self.page.findChild(QFrame, "douyinCommerceContentAccountColumn")
    content_column = self.page.findChild(QFrame, "douyinCommerceContentBodyColumn")
    video_column = self.page.findChild(QFrame, "douyinCommerceContentVideoColumn")
    self.assertIsNotNone(account_column)
    self.assertIsNotNone(content_column)
    self.assertIsNotNone(video_column)
    self.assertTrue(account_column.isAncestorOf(self.page.restore_content_button))
    self.assertTrue(content_column.isAncestorOf(self.page.save_content_button))
    self.assertIs(self.page.operation_dock_upload.parent(), self.page.operation_dock)
    self.assertFalse(self.page.operation_dock.isHidden())


def test_save_restore_never_creates_platform_session(self) -> None:
    self.page._session_id = ""
    saved = {"payload": {}, "updatedAt": "2026-08-05 12:00"}
    with patch("ui.douyin_commerce_page.douyin_commerce_draft_service.save_content_draft", return_value=saved), \
         patch("ui.douyin_commerce_page.douyin_commerce_draft_service.load_content_draft", return_value=None), \
         patch("ui.douyin_commerce_page.douyin_commerce_draft_service.remember_tag_history", return_value=[]), \
         patch.object(self.page.runner, "run") as run:
        self.page.save_content()
        self.page.restore_saved_content()
    run.assert_not_called()
    self.assertEqual(self.page._session_id, "")


def test_video_card_shows_metadata_or_explicit_missing_values(self) -> None:
    self.page.video_combo.addItem(
        "团购短片.mp4",
        {"storedPath": "/tmp/demo.mp4", "filename": "团购短片.mp4", "durationText": "00:32", "resolution": "1080 × 1920"},
    )
    self.page.video_combo.setCurrentIndex(self.page.video_combo.count() - 1)
    self.page._sync_content_cards()
    self.assertIn("时长：00:32", self.page.video_card.text())
    self.assertIn("尺寸：1080 × 1920", self.page.video_card.text())
~~~

在 DouyinCommerceContentDraftTests 中补充本地字段约束：

~~~
def test_normalize_content_draft_never_adds_platform_stage_fields(self) -> None:
    draft = douyin_commerce_draft_service.normalize_content_draft(
        {"title": "本地内容", "tags": ["北海"], "scheduleTime": "2026-08-06 09:00"}
    )
    self.assertEqual(set(draft), {
        "schemaVersion", "accountId", "accountFile", "mediaId", "mediaPath",
        "title", "description", "tags",
    })
~~~

- [ ] **Step 2: 运行测试确认当前操作栏布局不符合新规则**

Run:

~~~bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest \
  test_douyin_commerce_service.DouyinCommerceUiTests.test_content_workbench_places_restore_save_and_upload_in_separate_roles \
  test_douyin_commerce_service.DouyinCommerceUiTests.test_save_restore_never_creates_platform_session \
  test_douyin_commerce_service.DouyinCommerceUiTests.test_video_card_shows_metadata_or_explicit_missing_values \
  test_douyin_commerce_draft_service.DouyinCommerceContentDraftTests.test_normalize_content_draft_never_adds_platform_stage_fields
~~~

Expected: 第一项 FAIL，因为恢复与保存按钮仍位于 douyinCommerceOperationDock；本地草稿字段测试必须 PASS。

- [ ] **Step 3: 将恢复、保存和标签历史放回对应三栏**

在 _build_operation_dock() 中只创建状态、说明和 self.operation_dock_upload。在 _build_content_page() 中按以下结构创建控件，不复制同一个 QPushButton 到多个布局：

~~~
# 左栏：账号、保存状态、恢复与历史标签
account_panel.setObjectName("douyinCommerceContentAccountColumn")
self.content_save_status = QLabel("尚未保存本地内容")
self.content_save_status.setObjectName("douyinCommerceContentSaveStatus")
self.content_save_status.setWordWrap(True)
self.tag_history_host = QFrame()
self.tag_history_host.setObjectName("douyinCommerceTagHistoryHost")
self.tag_history_layout = QHBoxLayout(self.tag_history_host)
self.tag_history_layout.setContentsMargins(8, 6, 8, 6)
self.tag_history_layout.setSpacing(6)
self.restore_content_button = button("恢复已保存内容", variant="secondary", compact=True)
self.restore_content_button.setObjectName("douyinCommerceRestoreContent")
self.restore_content_button.clicked.connect(self.restore_saved_content)
account_layout.addWidget(self.content_save_status)
account_layout.addWidget(self.restore_content_button)
account_layout.addWidget(self.tag_history_host)

# 中栏：正文填写、当前标签与本地保存
content_panel.setObjectName("douyinCommerceContentBodyColumn")
self.save_content_button = button("保存本地内容", variant="secondary", compact=True)
self.save_content_button.setObjectName("douyinCommerceSaveContent")
self.save_content_button.clicked.connect(self.save_content)
content_layout.addWidget(self.save_content_button)

# 右栏：唯一视频与不设置独立封面的边界
video_panel.setObjectName("douyinCommerceContentVideoColumn")
self.video_card = self._selection_card("尚未选择视频素材")
self.video_card.setObjectName("douyinCommerceVideoCard")
video_layout.addWidget(self.video_card)
~~~

新增 _video_display(media: dict) -> str，并让 _sync_content_cards() 在每次刷新时调用它写入 self.video_card 与 self.platform_video_card：

~~~python
def _video_display(self, media: dict) -> str:
    if not media:
        return "尚未选择视频素材"
    filename = _normalized(media.get("filename")) or "未命名视频"
    duration = _normalized(media.get("durationText") or media.get("duration")) or "素材管理未提供时长"
    resolution = _normalized(media.get("resolution"))
    if not resolution:
        width = _normalized(media.get("width"))
        height = _normalized(media.get("height"))
        resolution = f"{width} × {height}" if width and height else "素材管理未提供尺寸"
    return f"{filename}\n时长：{duration}\n尺寸：{resolution}"
~~~

该方法不得扫描、上传或改写视频。让 _sync_workbench() 继续控制恢复按钮可用性：有会话时禁用并显示“请先放弃本次上传，再恢复已保存内容”；无会话且有保存内容时可用。save_content()、restore_saved_content() 与标签历史服务调用保持不变。

- [ ] **Step 4: 为三栏与状态卡增加纯样式标识**

在 ui/common.py 的抖音带货样式块中新增以下选择器，并删除 _selection_card() 的内联 setStyleSheet()，改为给卡片设置 douyinCommerceSelectionCard=True：

~~~css
QFrame#douyinCommerceContentAccountColumn,
QFrame#douyinCommerceContentBodyColumn,
QFrame#douyinCommerceContentVideoColumn {
    background: #FFFFFF;
    border: 1px solid #DDE6EB;
    border-radius: 12px;
}
QLabel[douyinCommerceSelectionCard="true"] {
    color: #344054;
    background: #F8FAFB;
    border: 1px solid #DCE3EA;
    border-radius: 8px;
    padding: 11px 13px;
}
QLabel#douyinCommerceContentSaveStatus {
    color: #526174;
    background: #F0F7F6;
    border-left: 3px solid #89B9B3;
    padding: 8px 10px;
}
QLabel#douyinCommerceStep[stepState="active"] {
    color: #175CD3;
    background: #EFF6FF;
    border-color: #B2CCFF;
    font-weight: 700;
}
~~~

保留 complete 状态的绿色与 pending 状态的中性灰，使顶部状态明确为“完成绿 / 当前蓝 / 待完成灰”；不引入网页动画、渐变或新的依赖。

- [ ] **Step 5: 运行内容恢复、标签与布局回归**

Run:

~~~bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest \
  test_douyin_commerce_draft_service.DouyinCommerceContentDraftTests \
  test_douyin_commerce_service.DouyinCommerceUiTests.test_content_workbench_places_restore_save_and_upload_in_separate_roles \
  test_douyin_commerce_service.DouyinCommerceUiTests.test_content_page_uses_three_columns_and_tag_chips \
  test_douyin_commerce_service.DouyinCommerceUiTests.test_restore_saved_content_restores_local_fields_without_uploading
~~~

Expected: PASS；保存/恢复只触及本机 SQLite，历史标签仍可回填，页面底部只存在一个“上传视频并继续”主操作。

- [ ] **Step 6: 仅暂存本任务变更并建立可回退提交**

Run:

~~~bash
git add -p ui/douyin_commerce_page.py ui/common.py test_douyin_commerce_service.py test_douyin_commerce_draft_service.py
git diff --cached --check
git diff --cached --stat
git commit -m "feat: 重构抖音带货内容准备工作台"
~~~

Expected: 提交只包含内容页三栏、样式和本地保存回归；没有数据库文件、素材、截图或会话文件。

### Task 3: 用阶段堆栈实现上传后的连续配置与地点确认闭环

**Files:**
- Modify: ui/douyin_commerce_page.py:467-718, 1188-1591, 1761-1976
- Modify: ui/common.py:761-968
- Modify: test_douyin_commerce_service.py:1276-1489

**Interfaces:**
- Consumes: Task 1 的 _current_platform_stage()、_selected_location_data、_set_stage_error()；Task 2 的三栏对象与恢复按钮。
- Produces: self.platform_stage_stack: QStackedWidget、self.platform_session_checklist: dict[str, QLabel]、self.location_view_stack: QStackedWidget、_sync_platform_workspace() -> None。

- [ ] **Step 1: 写出“同一时刻一个主操作”和完整地点确认的失败测试**

在 DouyinCommerceUiTests 中加入：

~~~
def test_platform_workspace_uses_one_active_stage_and_a_session_checklist(self) -> None:
    self.page._saved_content_available = True
    self.page._session_id = "session-demo"
    self.page.pages.setCurrentIndex(1)
    self.page._sync_view()

    self.assertEqual(self.page.platform_stage_stack.currentIndex(), 1)
    self.assertIs(self.page.platform_stage_stack.currentWidget(), self.page.music_stage)
    self.assertFalse(self.page.platform_restore_button.isEnabled())
    self.assertEqual(self.page.platform_session_checklist["location"].text(), "待完成")

    self.page._selected_music = {
        "musicId": "music-001", "title": "出埃及记",
        "creator": "石Yuchi", "duration": "01:08",
    }
    self.page._sync_view()
    self.assertEqual(self.page.platform_stage_stack.currentIndex(), 2)
    self.assertIs(self.page.platform_stage_stack.currentWidget(), self.page.location_stage)


def test_location_confirmation_replaces_candidates_with_full_address_card(self) -> None:
    self.page._session_id = "session-demo"
    self.page._selected_music = {
        "musicId": "music-001", "title": "出埃及记",
        "creator": "石Yuchi", "duration": "01:08",
    }
    self.page._show_locations([self.location_a, self.location_b])
    self.page.location_result_list.setCurrentRow(0)

    self.assertEqual(self.page.location_view_stack.currentIndex(), 1)
    self.assertIn(self.location_a["address"], self.page.location_candidate_card.text())
    self.assertTrue(self.page.apply_location_button.isEnabled())

    self.page._location_applied_success({"location": self.location_a})
    self.assertEqual(self.page.location_view_stack.currentIndex(), 2)
    self.assertIn(self.location_a["address"], self.page.location_applied_card.text())
    self.assertTrue(self.page.change_location_button.isVisible())


def test_location_keyword_change_clears_confirmed_location_and_declaration(self) -> None:
    self.page._selected_location_data = dict(self.location_a)
    self.page._location_applied = True
    self.page.declaration_combo.setCurrentIndex(1)
    self.page._declaration_applied = True

    self.page.location_keyword.setText("北海老街")
    self.page._location_keyword_edited()

    self.assertIsNone(self.page._selected_location_data)
    self.assertFalse(self.page._location_applied)
    self.assertFalse(self.page._declaration_applied)
    self.assertEqual(self.page.location_view_stack.currentIndex(), 0)
~~~

- [ ] **Step 2: 运行测试确认旧平台页仍是散列布局**

Run:

~~~bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest \
  test_douyin_commerce_service.DouyinCommerceUiTests.test_platform_workspace_uses_one_active_stage_and_a_session_checklist \
  test_douyin_commerce_service.DouyinCommerceUiTests.test_location_confirmation_replaces_candidates_with_full_address_card
~~~

Expected: FAIL，platform_stage_stack、阶段卡和会话清单尚不存在。

- [ ] **Step 3: 重建平台设置页为左中右连续工作台**

将 _build_platform_settings_page() 改成三个固定栏：左栏为账号、已保存内容提示、禁用的恢复按钮与“返回内容准备”；中栏为单个阶段堆栈；右栏为“本次上传会话”清单。阶段堆栈索引固定：

~~~
self.platform_stage_stack = QStackedWidget()
self.platform_stage_stack.setObjectName("douyinCommercePlatformStageStack")
self.blocked_stage = self._build_blocked_stage()
self.music_stage = self._build_music_stage()
self.location_stage = self._build_location_stage()
self.declaration_stage = self._build_declaration_stage()
self.schedule_stage = self._build_schedule_stage()
for stage in (
    self.blocked_stage, self.music_stage, self.location_stage,
    self.declaration_stage, self.schedule_stage,
):
    self.platform_stage_stack.addWidget(stage)

self.platform_restore_button = button("恢复已保存内容", variant="secondary", compact=True)
self.platform_restore_button.setObjectName("douyinCommercePlatformRestoreContent")
self.platform_restore_button.clicked.connect(self.restore_saved_content)
self.platform_back_button = button("返回内容准备", variant="secondary", compact=True)
self.platform_back_button.setObjectName("douyinCommerceBackToContent")
self.platform_back_button.clicked.connect(lambda: self._go_to_step(0))

self.platform_session_checklist = {
    "video": self._checklist_value("视频"),
    "music": self._checklist_value("音乐"),
    "location": self._checklist_value("定位"),
    "declaration": self._checklist_value("声明"),
    "schedule": self._checklist_value("发布方式"),
}

def _checklist_value(self, label_text: str) -> QLabel:
    value = QLabel("待完成")
    value.setObjectName("douyinCommerceSessionChecklistValue")
    value.setAccessibleName(f"{label_text}状态")
    value.setWordWrap(True)
    return value
~~~

所有阶段对象名称固定为 douyinCommerceBlockedStage、douyinCommerceMusicStage、douyinCommerceLocationStage、douyinCommerceDeclarationStage、douyinCommerceScheduleStage。左栏对象名称为 douyinCommercePlatformContextColumn，右栏为 douyinCommerceSessionColumn。

保留 self.platform_account_card 和 self.platform_video_card 作为左栏已选对象摘要；删除 self.platform_session_card。同步修改 _sync_content_cards()：只更新账号、视频、self.platform_account_card、self.platform_video_card 与本机保存说明；会话文字只由 _sync_platform_workspace() 填入右栏五项清单，避免同一会话状态在两个区域重复出现。

将原有控件移动到下列构造器中，保持既有 objectName 和信号连接，不重复创建第二套业务控件：

~~~python
def _build_blocked_stage(self) -> QFrame:
    panel, layout = self._section("等待上传会话", "返回内容准备并完成上传后，才能在同一抖音编辑页设置音乐、地点和声明。")
    panel.setObjectName("douyinCommerceBlockedStage")
    self.blocked_stage_message = QLabel("当前没有可继续配置的抖音编辑会话")
    self.blocked_stage_message.setWordWrap(True)
    layout.addWidget(self.blocked_stage_message)
    return panel

def _build_music_stage(self) -> QFrame:
    panel, layout = self._section("选择收藏音乐", "仅读取当前编辑页可见的收藏音乐；不会默认选第一首。")
    panel.setObjectName("douyinCommerceMusicStage")
    self.music_panel = panel
    self.load_music_button = button("读取收藏音乐", variant="primary")
    self.use_music_button = button("确认使用这首音乐", variant="secondary")
    self.music_status = QLabel("视频上传完成后可读取收藏音乐")
    self.music_status.setObjectName("douyinCommerceInlineNotice")
    self.music_status.setWordWrap(True)
    self.music_list = QListWidget()
    self.music_list.setObjectName("douyinCommerceMusicCandidates")
    self.music_card = self._selection_card("尚未选择收藏音乐")
    self.load_music_button.clicked.connect(self.load_music)
    self.use_music_button.clicked.connect(self.use_selected_music)
    self.music_list.itemSelectionChanged.connect(self._music_candidate_changed)
    layout.addWidget(self.load_music_button)
    layout.addWidget(self.use_music_button)
    layout.addWidget(self.music_status)
    layout.addWidget(self.music_list)
    layout.addWidget(self.music_card)
    layout.addWidget(self._stage_error_label("music"))
    return panel

def _build_location_stage(self) -> QFrame:
    panel, layout = self._section("选择发布定位", "范围默认国内；确认后保留完整地址，点击“更换定位”才重新展开搜索。")
    panel.setObjectName("douyinCommerceLocationStage")
    self.location_panel = panel
    self.location_view_stack = QStackedWidget()
    self.location_view_stack.addWidget(self._build_location_search_view())
    self.location_view_stack.addWidget(self._build_location_candidate_view())
    self.location_view_stack.addWidget(self._build_location_applied_view())
    layout.addWidget(self.location_view_stack)
    layout.addWidget(self._stage_error_label("location"))
    return panel

def _build_declaration_stage(self) -> QFrame:
    panel, layout = self._section("作品内容声明", "只写入你明确选择的声明，且必须回读一致。")
    panel.setObjectName("douyinCommerceDeclarationStage")
    self.declaration_panel = panel
    self.declaration_combo = QComboBox()
    self.declaration_combo.setObjectName("douyinCommerceContentDeclaration")
    self.declaration_combo.addItem("请选择作品内容声明", "")
    for value in douyin_commerce_service.CONTENT_DECLARATION_OPTIONS:
        self.declaration_combo.addItem(value, value)
    self.apply_declaration_button = button("确认写入声明", variant="primary")
    self.declaration_card = self._selection_card("尚未选择作品内容声明")
    self.declaration_card.setObjectName("douyinCommerceDeclarationCard")
    self.declaration_status = QLabel("先完成音乐与发布定位")
    self.declaration_status.setWordWrap(True)
    self.declaration_combo.currentIndexChanged.connect(self._declaration_changed)
    self.apply_declaration_button.clicked.connect(self.apply_selected_declaration)
    layout.addWidget(self.declaration_combo)
    layout.addWidget(self.apply_declaration_button)
    layout.addWidget(self.declaration_status)
    layout.addWidget(self.declaration_card)
    layout.addWidget(self._stage_error_label("declaration"))
    return panel

def _build_schedule_stage(self) -> QFrame:
    panel, layout = self._section("发布方式", "默认立即发表；开启定时时，只接受未来的北京时间。")
    panel.setObjectName("douyinCommerceScheduleStage")
    self.schedule_panel = panel
    self.timer_enabled = QCheckBox("开启定时发表")
    self.timer_enabled.toggled.connect(self._timer_enabled_changed)
    self.schedule_date = QDateEdit()
    self.schedule_date.setObjectName("douyinCommerceScheduleDate")
    self.schedule_date.setCalendarPopup(True)
    tomorrow = datetime.now(_SHANGHAI_TZ).date() + timedelta(days=1)
    self.schedule_date.setDate(QDate(tomorrow.year, tomorrow.month, tomorrow.day))
    self.schedule_date.dateChanged.connect(self._schedule_changed)
    self.schedule_time = QTimeEdit()
    self.schedule_time.setObjectName("douyinCommerceScheduleTime")
    self.schedule_time.setDisplayFormat("HH:mm")
    self.schedule_time.setTime(QTime(9, 0))
    self.schedule_time.timeChanged.connect(self._schedule_changed)
    self.to_review_button = button("进入检查", variant="primary")
    self.to_review_button.setObjectName("douyinCommerceToReview")
    self.to_review_button.clicked.connect(lambda: self._go_to_step(2))
    layout.addWidget(self.timer_enabled)
    layout.addWidget(self.schedule_date)
    layout.addWidget(self.schedule_time)
    layout.addWidget(self.to_review_button)
    layout.addWidget(self._stage_error_label("schedule"))
    return panel
~~~

地点三视图使用以下具体构造；搜索视图只保留本地/国内范围、关键词与候选，候选/已确认视图不再显示下拉列表：

~~~python
def _build_location_search_view(self) -> QWidget:
    view = QWidget()
    layout = QVBoxLayout(view)
    row = QHBoxLayout()
    self.location_scope_combo = QComboBox()
    self.location_scope_combo.setObjectName("douyinCommerceLocationScope")
    self.location_scope_combo.addItem("选择范围：本地或国内", "")
    self.location_scope_combo.addItem("本地", douyin_commerce_service.LOCATION_SCOPE_LOCAL)
    self.location_scope_combo.addItem("国内", douyin_commerce_service.LOCATION_SCOPE_DOMESTIC)
    self.location_scope_combo.setCurrentIndex(2)
    self.location_keyword = QLineEdit()
    self.location_keyword.setObjectName("douyinCommerceLocationKeyword")
    self.location_keyword.setPlaceholderText("输入地点，例如：北海夜南香")
    self.location_search_button = button("搜索发布定位", variant="primary")
    self.location_search_button.setObjectName("douyinCommerceSearchLocation")
    self.location_scope_combo.currentIndexChanged.connect(self._location_scope_changed)
    self.location_keyword.textEdited.connect(self._location_keyword_edited)
    self.location_keyword.returnPressed.connect(self.search_locations)
    self.location_search_button.clicked.connect(self.search_locations)
    row.addWidget(self.location_scope_combo)
    row.addWidget(self.location_keyword, 1)
    row.addWidget(self.location_search_button)
    self.location_status = QLabel("完成音乐选择后，可从当前抖音编辑页搜索发布定位（默认范围：国内）")
    self.location_status.setObjectName("douyinCommerceLocationStatus")
    self.location_status.setWordWrap(True)
    self.location_result_list = QListWidget()
    self.location_result_list.setObjectName("douyinCommerceLocationResults")
    self.location_result_list.setWordWrap(True)
    self.location_result_list.setTextElideMode(Qt.TextElideMode.ElideNone)
    self.location_result_list.itemSelectionChanged.connect(self._location_selected)
    layout.addLayout(row)
    layout.addWidget(self.location_status)
    layout.addWidget(self.location_result_list)
    return view

def _build_location_candidate_view(self) -> QWidget:
    view = QWidget()
    layout = QVBoxLayout(view)
    self.location_candidate_card = self._selection_card("请选择一个平台发布定位候选")
    self.apply_location_button = button("确认写入平台", variant="primary")
    self.apply_location_button.setObjectName("douyinCommerceApplyLocation")
    self.apply_location_button.clicked.connect(self.apply_selected_location)
    layout.addWidget(self.location_candidate_card)
    layout.addWidget(self.apply_location_button)
    return view

def _build_location_applied_view(self) -> QWidget:
    view = QWidget()
    layout = QVBoxLayout(view)
    self.location_applied_card = self._selection_card("尚未选择发布定位")
    self.location_applied_card.setObjectName("douyinCommerceLocationConfirmedCard")
    self.change_location_button = button("更换定位", variant="secondary", compact=True)
    self.change_location_button.setObjectName("douyinCommerceChangeLocation")
    self.change_location_button.clicked.connect(self.change_location_selection)
    layout.addWidget(self.location_applied_card)
    layout.addWidget(self.change_location_button)
    return view
~~~

保留现有 location_status 标签作为搜索视图内的状态说明，并把它的文字更新留在现有 _show_locations()、_location_scope_changed()、_location_keyword_edited() 中；三个视图内不创建新的地点列表或重复按钮。

- [ ] **Step 4: 实现阶段堆栈和会话清单刷新**

实现 _sync_platform_workspace()，只读取页面内存状态并设置可见性，不调用 runner.run()：

~~~
def _sync_platform_workspace(self) -> None:
    stage_index = {
        "blocked": 0, "music": 1, "location": 2,
        "declaration": 3, "schedule": 4,
    }[self._current_platform_stage()]
    self.platform_stage_stack.setCurrentIndex(stage_index)
    self.platform_session_checklist["video"].setText(
        "已建立同一编辑会话" if self._session_id else "待上传"
    )
    self.platform_session_checklist["music"].setText(
        "已由平台回读确认" if self._selected_music else "待完成"
    )
    self.platform_session_checklist["location"].setText(
        "已由平台回读确认" if self._location_applied else "待完成"
    )
    self.platform_session_checklist["declaration"].setText(
        "已由平台回读确认" if self._declaration_applied else "待完成"
    )
    self.platform_session_checklist["schedule"].setText(
        "北京时间定时" if self.timer_enabled.isChecked() else "立即发表"
    )
~~~

在 _sync_workbench() 中调用它，并将 self.platform_restore_button 与 self.restore_content_button 同步为相同的恢复门槛：有会话时两个按钮均禁用。保留 timer_enabled 的可选逻辑：只有进入 schedule 阶段才启用定时控件和“进入检查”；未开启定时时不要求填写日期时间。

- [ ] **Step 5: 建立地点的搜索、候选、确认三态**

在 _build_location_stage() 内使用 self.location_view_stack 建立固定索引：搜索为 0，候选确认是 1，平台已回读确认是 2。候选与回读卡均使用 _location_display()，完整地址允许复制：

~~~
def _sync_location_view(self) -> None:
    if self._location_applied and self._selected_location_data:
        self.location_view_stack.setCurrentIndex(2)
        self.location_applied_card.setText(
            f"{self._location_display(self._selected_location_data)}\n已由当前抖音编辑页回读确认"
        )
    elif self._selected_location_data:
        self.location_view_stack.setCurrentIndex(1)
        self.location_candidate_card.setText(
            f"{self._location_display(self._selected_location_data)}\n已选候选，尚未写入平台"
        )
    else:
        self.location_view_stack.setCurrentIndex(0)
~~~

_location_selected() 保存 self._selected_location_data 后调用 _sync_location_view()；change_location_selection()、_location_scope_changed()、_location_keyword_edited() 清空该状态并回到索引 0；_location_applied_success() 用平台回读的 location 覆盖候选，回到索引 2 并清空声明确认。地点确认前显示唯一主操作“确认写入平台”；确认后只保留次要的“更换定位”。

移除原有 self.location_card 的所有写入：_show_locations() 只更新 location_status、候选列表与 search 视图；_location_selected() 写入 location_candidate_card；apply_selected_location() 显示“正在由当前抖音编辑页确认发布定位”；_location_applied_success() 写入 location_applied_card。这样任何成功、失败或范围变化都不会尝试访问已删除的旧卡片。

- [ ] **Step 6: 清理阶段间的失效状态并保持安全门槛**

在 _music_selected() 中，如果音乐从已回读状态被更换，清空下游地点与声明，防止旧状态误复用：

~~~
if self._location_applied or self._declaration_applied:
    self._selected_location_data = None
    self._locations = []
    self.location_result_list.clear()
    self._location_applied = False
    self._clear_declaration("音乐已更换，请重新搜索定位并选择作品内容声明")
~~~

所有成功回调执行 _clear_stage_error(<stage>)，所有错误回调执行 Task 1 的 _platform_action_error()；二维码、登录、验证码、未知弹窗和无法唯一回读的情况仍由既有执行器安全停止，UI 不把它们标记为完成。

- [ ] **Step 7: 增加阶段卡、会话清单和错误卡样式**

在 ui/common.py 追加以下只影响抖音带货的样式：

~~~css
QFrame#douyinCommercePlatformContextColumn,
QFrame#douyinCommerceSessionColumn,
QStackedWidget#douyinCommercePlatformStageStack {
    background: #FFFFFF;
    border: 1px solid #DDE6EB;
    border-radius: 12px;
}
QFrame#douyinCommerceSessionChecklist {
    background: #F8FAFB;
    border: 1px solid #E2E8EE;
    border-radius: 10px;
}
QLabel#douyinCommerceStageError {
    color: #9A3412;
    background: #FFF7ED;
    border: 1px solid #FED7AA;
    border-left: 3px solid #EA580C;
    border-radius: 8px;
    padding: 9px 11px;
}
QLabel#douyinCommerceLocationConfirmedCard {
    color: #075F59;
    background: #EFF9F6;
    border: 1px solid #B7E5D7;
    border-radius: 10px;
    padding: 12px;
}
~~~

不在 ui/common.py 中加入平台业务判断、动态字符串或网络逻辑。

- [ ] **Step 8: 运行平台阶段、地点与定时回归**

Run:

~~~bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest \
  test_douyin_commerce_service.DouyinCommerceUiTests.test_platform_workspace_uses_one_active_stage_and_a_session_checklist \
  test_douyin_commerce_service.DouyinCommerceUiTests.test_location_confirmation_replaces_candidates_with_full_address_card \
  test_douyin_commerce_service.DouyinCommerceUiTests.test_location_keyword_change_clears_confirmed_location_and_declaration \
  test_douyin_commerce_service.DouyinCommerceUiTests.test_location_scope_change_clears_declaration_and_shows_full_address \
  test_douyin_commerce_service.DouyinCommerceUiTests.test_timer_is_optional_and_enters_review_after_required_platform_readbacks \
  test_douyin_commerce_service.DouyinCommerceUiTests.test_selected_declaration_cannot_enter_review_before_platform_readback
~~~

Expected: PASS；音乐、地点、声明、定时只在前一状态已回读后依次出现；候选确认后完整地址保留；立即发表仍可进入检查。

- [ ] **Step 9: 仅暂存本任务变更并建立可回退提交**

Run:

~~~bash
git add -p ui/douyin_commerce_page.py ui/common.py test_douyin_commerce_service.py
git diff --cached --check
git diff --cached --stat
git commit -m "feat: 完成抖音带货连续平台设置流程"
~~~

Expected: 提交只包含平台设置三栏、地点三态、会话清单和对应测试；真实平台执行器文件不在暂存区。

### Task 4: 对齐检查页、放弃恢复语义与开发版可视验收

**Files:**
- Modify: ui/douyin_commerce_page.py:719-783, 1331-1591, 1977-2171
- Modify: test_douyin_commerce_service.py:1392-1578
- Modify: test_douyin_commerce_draft_service.py:25-77

**Interfaces:**
- Consumes: Task 1 的统一状态与错误卡，Task 2 的本地草稿边界，Task 3 的会话清单、阶段堆栈和地点确认状态。
- Produces: 检查页完整摘要、放弃会话后仍可恢复本地内容的受控状态，以及最终离线/开发版验收记录。

- [ ] **Step 1: 写出检查页与放弃恢复语义的失败测试**

在 DouyinCommerceUiTests 中加入：

~~~
def test_review_shows_full_address_and_only_verified_platform_values(self) -> None:
    self.page._session_id = "session-demo"
    self.page._selected_music = {
        "musicId": "music-001", "title": "出埃及记",
        "creator": "石Yuchi", "duration": "01:08",
    }
    self.page._selected_location_data = dict(self.location_a)
    self.page._location_applied = True
    self.page.declaration_combo.setCurrentIndex(1)
    self.page._declaration_applied = True
    self.page._go_to_step(2)

    self.assertIn(self.location_a["address"], self.page.summary_values["location"].text())
    self.assertEqual(self.page.summary_values["declaration"].text(), "内容由AI生成")
    self.assertTrue(self.page.preflight_button.isEnabled())


def test_abandon_session_keeps_saved_content_recoverable(self) -> None:
    self.page._saved_content_available = True
    self.page._session_id = "session-demo"
    with patch("ui.douyin_commerce_page.douyin_commerce_session.commerce_session_manager.close") as close:
        self.page._abandon_session(silent=True)

    close.assert_called_once_with("session-demo")
    self.assertEqual(self.page._session_id, "")
    self.assertTrue(self.page._saved_content_available)
    self.assertTrue(self.page.restore_content_button.isEnabled())
~~~

- [ ] **Step 2: 运行测试确认检查摘要依赖旧的隐藏列表或恢复状态未完整同步**

Run:

~~~bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest \
  test_douyin_commerce_service.DouyinCommerceUiTests.test_review_shows_full_address_and_only_verified_platform_values \
  test_douyin_commerce_service.DouyinCommerceUiTests.test_abandon_session_keeps_saved_content_recoverable
~~~

Expected: FAIL，直到 _summary()、_sync_workbench() 与 _abandon_session() 统一使用 _selected_location_data 和保存状态。

- [ ] **Step 3: 将检查页和确认弹窗改为统一摘要来源**

只使用 _summary() 作为检查页与 DouyinCommerceConfirmDialog 的数据来源。_summary() 中地点字段使用 _selected_location_data，且只有 _location_applied 为真时显示“已由平台回读确认”的完整地址：

~~~
location = self._selected_location_data or {}
location_summary = (
    self._location_display(location)
    if self._location_applied and location
    else "待写入编辑页"
)
~~~

检查页新增“文案摘要”行，使用 self.description_input.toPlainText().strip() 的前 120 个字符并在更长时追加省略号；不改写原文或把文案提交到平台。预检和最终提交按钮继续仅由 _preflight_fingerprint、_can_review() 和平台回执控制。

- [ ] **Step 4: 固化放弃会话后的恢复状态**

在 _abandon_session() 中关闭临时会话后仅清空内存平台设置：_selected_music、_selected_location_data、地点候选、声明、预检指纹和上传后修改标识。不要调用 douyin_commerce_draft_service 的删除或覆盖接口。最后调用 _refresh_saved_content_status() 和 _sync_workbench()，使保存内容存在时恢复按钮立即重新可用。

- [ ] **Step 5: 运行完整离线回归和开发版无平台 UI 检查**

Run:

~~~bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest \
  test_douyin_commerce_draft_service \
  test_douyin_commerce_service

.venv/bin/python desktop_native_app.py --ui-test
~~~

Expected: 全部 PASS；--ui-test 只完成本地 Qt 自检，不打开抖音浏览器。

随后启动本地开发版页面，但不点击“上传视频并继续”、不读取音乐、不搜索地点、不预检、不提交：

~~~bash
.venv/bin/python -u desktop_native_app.py --page commerce
~~~

人工检查两个页面并保存到仅本机临时位置的截图：

1. 内容准备页：左栏有账号/恢复/历史标签，中栏有保存内容，右栏有视频，底部只有上传主操作。
2. 用测试内存状态进入平台设置页：左栏保持账号和恢复边界，中栏一次只显示一个阶段，右栏显示五项会话清单。
3. 用 location_a 演示候选、确认、已回读三个本地状态，确认完整地址可见且“更换定位”才展开搜索。

- [ ] **Step 6: 检查暂存内容并建立最终可回退提交**

Run:

~~~bash
git status --short
git add -p ui/douyin_commerce_page.py test_douyin_commerce_service.py test_douyin_commerce_draft_service.py
git diff --cached --check
git diff --cached --name-only
git commit -m "test: 验证抖音带货工作台恢复与检查流程"
~~~

Expected: 暂存文件仅为本计划明确列出的源码和测试；不含 *.sqlite3、storage_state、*.png、*.mp4、任务日志或浏览器数据。

## 验收映射

| 已锁定验收项 | 覆盖任务 | 证据 |
| --- | --- | --- |
| 保存并恢复账号、视频、标题、文案、标签，不触发平台动作 | Task 2 | 草稿服务与页面 runner.run 断言。 |
| 保存后可再次使用历史标签 | Task 2 | DouyinCommerceContentDraftTests 与标签芯片测试。 |
| 上传前后都保持三栏，右栏从素材过渡到会话 | Task 2、Task 3 | 页面对象名、Qt 可见性断言、开发版截图。 |
| 音乐、定位、声明、定时按回读顺序解锁 | Task 1、Task 3 | _current_platform_stage() 和阶段堆栈测试。 |
| 定位确认后显示完整地址与更换入口 | Task 3 | 三态堆栈和完整地址测试。 |
| 范围/关键词变化清空地点与声明 | Task 1、Task 3 | 范围变更测试扩展为内存地点状态断言。 |
| 放弃上传只关闭会话，不删除本地保存内容 | Task 4 | test_abandon_session_keeps_saved_content_recoverable。 |
| 不改变平台安全边界 | Task 4 | 全离线测试、--ui-test、开发版不执行上传/预检/提交。 |
