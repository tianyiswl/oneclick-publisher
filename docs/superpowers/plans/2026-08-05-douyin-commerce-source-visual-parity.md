# 抖音带货源码视觉一致性 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将开发版“抖音带货”的内容准备页重构为用户已确认的三栏视觉合同，而不是只保留相似的功能布局。

**Architecture:** 保留 `DouyinCommercePage` 的账号、素材、本机保存和上传调用接口；只替换内容准备页的原生 PyQt 组合结构与局部 QSS。页面由透明标题区、三段进度、阶段切换条、账号/内容/视频三张等高卡片和底部唯一上传操作栏组成；不把 HTML 原型嵌入客户端，也不触碰抖音执行器。

**Tech Stack:** Python、PyQt6、QSS、unittest、离屏 Qt 截图。

## Global Constraints

- 以用户提供的图 1 为唯一视觉参考，不能用仓库内另一套 HTML 原型替代。
- 仅修改 `ui/douyin_commerce_page.py`、`ui/common.py`、`test_douyin_commerce_service.py` 与本计划；不修改账号、上传、音乐、定位、声明、预检或发布执行器。
- 继续保持单账号、单视频、本机保存与恢复、用户选择标签的原有安全边界。
- 内容准备页不启动浏览器、不上传、不保存平台草稿、不预检、不发布。
- 任意“视觉完成”结论必须同时具备离线断言和同尺寸源码截图证据。

---

### Task 1: 为视觉合同写出失败的源码组件测试

**Files:**
- Modify: `test_douyin_commerce_service.py`

**Interfaces:**
- Consumes: `DouyinCommercePage` 现有 `pages`、`account_card`、`video_card`、`operation_dock_upload`。
- Produces: `douyinCommerceReferenceHeader`、`douyinCommerceStageSwitcher`、`douyinCommerceAccountIdentityCard`、`douyinCommerceVideoPreview`、`douyinCommerceVideoReplace`、`douyinCommerceReferenceFooter`。

- [ ] **Step 1: 写失败测试**

```python
def test_content_page_exposes_the_approved_visual_regions(self) -> None:
    self.page.pages.setCurrentIndex(0)
    self.page._sync_view()

    self.assertIsNotNone(self.page.findChild(QFrame, "douyinCommerceReferenceHeader"))
    self.assertIsNotNone(self.page.findChild(QFrame, "douyinCommerceStageSwitcher"))
    self.assertIsNotNone(self.page.findChild(QFrame, "douyinCommerceAccountIdentityCard"))
    self.assertIsNotNone(self.page.findChild(QFrame, "douyinCommerceVideoPreview"))
    self.assertIsNotNone(self.page.findChild(QFrame, "douyinCommerceVideoReplace"))
    self.assertIsNotNone(self.page.findChild(QFrame, "douyinCommerceReferenceFooter"))
    self.assertIsNone(self.page.findChild(QFrame, "douyinCommerceCommandBar"))
```

- [ ] **Step 2: 运行失败测试**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest test_douyin_commerce_service.DouyinCommerceUiTests.test_content_page_exposes_the_approved_visual_regions`

Expected: FAIL，因为当前页面仍创建旧的 `douyinCommerceCommandBar`，且没有参考图中的独立视觉区域。

### Task 2: 原生重建内容准备页视觉骨架

**Files:**
- Modify: `ui/douyin_commerce_page.py`
- Modify: `ui/common.py`

**Interfaces:**
- Consumes: `_section()`、`_selection_card()`、`_sync_content_cards()`、`save_content()`、`restore_saved_content()`、`start_upload()`。
- Produces: `_build_reference_header()`、`_build_content_stage_switcher()`、`_build_reference_account_card()`、`_build_reference_video_preview()`；这些方法只创建原生控件，原有业务信号保持连接。

- [ ] **Step 1: 替换旧顶栏**

删除 `_build_ui()` 中 `douyinCommerceCommandBar` 和单独的流程边界条，改为 `douyinCommerceReferenceHeader`：左侧固定显示“抖音带货”和“一个视频，一次上传；平台设置在上传后的同一编辑会话完成。”，右侧只显示本机保存状态徽标和会话放弃按钮（仅会话存在时显示）。

- [ ] **Step 2: 组装参考图的三栏卡片**

保留现有账号、标题、文案、标签和视频控件，分别放入等高的账号、内容、视频列；账号列增加头像占位和账号状态卡，视频列增加深色预览块、文件名/时长/尺寸、虚线“更换视频”入口和上传后设置提示。所有视觉部件复用现有的数据同步方法，不新增本地字段或平台操作。

- [ ] **Step 3: 收束步骤和底栏**

将三步进度改为参考图的编号卡；新增只表达当前阶段的 `douyinCommerceStageSwitcher`。底部操作栏改为 `douyinCommerceReferenceFooter`，其中唯一高强调按钮继续指向现有 `start_upload()`；保存和恢复保留在各自栏内。

- [ ] **Step 4: 运行同一测试转绿**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest test_douyin_commerce_service.DouyinCommerceUiTests.test_content_page_exposes_the_approved_visual_regions`

Expected: PASS，且旧命令栏不再存在。

### Task 3: 增加尺寸和内容同步回归，并做源码截图验证

**Files:**
- Modify: `test_douyin_commerce_service.py`

**Interfaces:**
- Consumes: Task 2 的各视觉对象名和现有 `_sync_content_cards()`。
- Produces: 1600×900 的离线几何断言和本地源码截图命令。

- [ ] **Step 1: 写失败的几何与同步测试**

```python
def test_content_cards_share_a_stable_three_column_geometry(self) -> None:
    self.page.resize(1600, 900)
    self.page.show()
    QApplication.processEvents()
    cards = [
        self.page.findChild(QFrame, name)
        for name in (
            "douyinCommerceContentAccountColumn",
            "douyinCommerceContentBodyColumn",
            "douyinCommerceContentVideoColumn",
        )
    ]
    self.assertTrue(all(card is not None and card.height() >= 420 for card in cards))
    self.assertLess(max(card.y() for card in cards) - min(card.y() for card in cards), 4)
    self.assertGreater(cards[1].width(), cards[0].width())
```

- [ ] **Step 2: 运行失败测试并只调整布局伸缩与最小尺寸**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest test_douyin_commerce_service.DouyinCommerceUiTests.test_content_cards_share_a_stable_three_column_geometry`

Expected: FAIL，直到内容页在 1600×900 下不再因旧顶栏和滚动布局压缩卡片。

- [ ] **Step 3: 运行完整离线验证并生成源码截图**

Run:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest test_douyin_commerce_draft_service test_douyin_commerce_service
QT_QPA_PLATFORM=offscreen .venv/bin/python desktop_native_app.py --ui-test
```

随后只用离屏 Qt 构造 `DouyinCommercePage`、设置 1600×900、保存 `grab()` 截图到 `/tmp`，人工对照用户图 1。该截图不访问账号、浏览器或平台。
