# 抖音带货批量发布工作台 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让同一抖音账号可安全地一次发布 1 至 20 条带货视频，共用内容与平台设置、逐条配置官方地点和立即/定时发布，并只以逐条平台最终回执记为成功。

**Architecture:** 在现有单视频 `DouyinCommerceSessionManager` 之上新增独立的批量契约、地点预设与批量执行协调器；协调器始终串行地只持有一个无头编辑器会话。桌面端 `DouyinCommercePage` 改为三步批量工作台，复用现有验证对话框和任务表，但扩展任务条目回填以表达每条视频的结果。

**Tech Stack:** Python 3、PyQt6、SQLite、Playwright、unittest。

## Global Constraints

- 只使用 UTF-8 与中文用户文案、注释和文档。
- 一个批次只允许一个已登录、状态正常的抖音账号，视频数必须为 1 至 20。
- 标题、文案、标签、收藏音乐、作品内容声明为全批次共享字段；地点和发布时间为逐条字段。
- 批量默认立即发布；定时只接受 Asia/Shanghai 的未来时间，支持起始时间加间隔与逐条覆盖。
- 浏览器默认真正无头运行；二维码、短信验证码、登录、验证码、未知合规或风控提示必须暂停，不得绕过。
- 短信验证在客户端等待用户输入；重发必须遵守 60 秒倒计时；验证成功后从当前条继续。
- 不使用官方 API、签名参数、私有接口或第三方地图代替抖音官方地点。
- 官方地点预设仅保存本机可见的稳定标识、名称、完整地址、范围和账号关联；不保存 Cookie、二维码、验证码、手机号、HTML、原始请求或凭据。
- 编辑器填写、上传、本地草稿或任务创建不是平台发布/定时成功；仅最终平台回执和回读可写入成功状态。
- 不实现多账号批量、图文/文字带货、团购商品/套餐/门店绑定或自动绕过验证。

---

## 文件结构与责任边界

| 文件 | 责任 |
| --- | --- |
| `app_core/database.py` | 创建批量草稿与地点预设 SQLite 表；为现有任务条目添加批次序号和非敏感地点/时间摘要列。 |
| `app_core/douyin_commerce_batch_draft_service.py` | 规范化、保存、恢复本地批次草稿；只处理本机批次配置。 |
| `app_core/douyin_location_preset_service.py` | 按账号持久化地点预设，并从编辑器候选中做精确匹配。 |
| `app_core/douyin_commerce_batch_service.py` | 批量载荷契约、间隔排期、逐条覆盖、单条执行载荷生成。 |
| `app_core/douyin_commerce_batch_executor.py` | 串行协调单视频会话：上传、共享字段、逐条地点/时间、预检/提交、状态和验证暂停。 |
| `app_core/task_service.py` | 创建和回填逐视频任务条目，显示“抖音带货批量”与地点/时间摘要。 |
| `ui/douyin_commerce_page.py` | 三步批量工作台与进度展示；不直接写 Playwright。 |
| `ui/douyin_verification_dialog.py` | 显示当前批次视频序号，复用现有短信/二维码内存验证通道。 |
| `docs/DOUYIN_COMMERCE_WORKFLOW.md` | 更新单条即批量一条、默认立即、地点预设、批量确认与验证规则。 |

## Task 1: 本地批次草稿与地点预设

**Files:**
- Modify: `app_core/database.py:ensure_schema`
- Create: `app_core/douyin_commerce_batch_draft_service.py`
- Create: `app_core/douyin_location_preset_service.py`
- Test: `test_douyin_commerce_batch_draft_service.py`
- Test: `test_douyin_location_preset_service.py`

**Interfaces:**
- Consumes: `app_core.database.connect()`、`app_core.douyin_location_service.normalize_location_candidate()`。
- Produces: `normalize_batch_draft(payload) -> dict`、`save_batch_draft(payload) -> dict`、`load_batch_draft() -> dict | None`、`save_location_preset(account_id, location, scope) -> dict`、`list_location_presets(account_id) -> list[dict]`、`match_location_preset(preset, candidates) -> dict`。

- [ ] **Step 1: 写入批次草稿与地点预设的失败测试**

```python
def test_batch_draft_keeps_shared_fields_and_item_specific_location_only():
    saved = save_batch_draft({
        "accountId": 7,
        "accountFile": "douyin.json",
        "shared": {"title": "统一标题", "description": "统一文案", "tags": ["北海"]},
        "items": [{"mediaPath": "/tmp/a.mp4", "locationPresetId": "p1", "enableTimer": False}],
        "cookie": "must-not-persist",
    })
    self.assertEqual(saved["payload"]["items"][0]["locationPresetId"], "p1")
    self.assertNotIn("cookie", saved["payload"])

def test_location_preset_is_account_scoped_and_requires_full_identity():
    preset = save_location_preset(7, LOCATION, "domestic")
    self.assertEqual(list_location_presets(7), [preset])
    self.assertEqual(list_location_presets(8), [])
    with self.assertRaisesRegex(DouyinLocationPresetError, "完整地址"):
        save_location_preset(7, {"poiId": "p", "name": "银滩"}, "domestic")
```

- [ ] **Step 2: 运行测试，确认实现尚不存在**

Run: `.venv/bin/python -m unittest -v test_douyin_commerce_batch_draft_service test_douyin_location_preset_service`  
Expected: FAIL，提示模块或接口不存在。

- [ ] **Step 3: 以最小 SQLite 结构实现本地持久化**

在 `database.ensure_schema()` 增加以下两张表，不迁移或读取 Cookie/会话数据：

```sql
CREATE TABLE IF NOT EXISTS douyin_commerce_batch_drafts (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  payloadJson TEXT NOT NULL,
  updatedAt TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS douyin_location_presets (
  id TEXT PRIMARY KEY,
  accountId INTEGER NOT NULL,
  poiId TEXT NOT NULL,
  name TEXT NOT NULL,
  address TEXT NOT NULL,
  scope TEXT NOT NULL,
  verifiedAt TEXT NOT NULL,
  UNIQUE(accountId, poiId)
);
```

实现 `normalize_batch_draft()`：只接受一个账号、共享内容和 1 至 20 个条目；丢弃未知敏感字段；每个条目仅保存本地媒体路径、预设 ID、`enableTimer` 与 `scheduleTimeOverride`。实现 `match_location_preset()`：只在候选中寻找 `poiId` 相同且名称、完整地址均相同的一条，零条或多条均抛出明确错误。

- [ ] **Step 4: 运行新增离线测试**

Run: `.venv/bin/python -m unittest -v test_douyin_commerce_batch_draft_service test_douyin_location_preset_service`  
Expected: PASS。

- [ ] **Step 5: 提交本任务**

```bash
git add app_core/database.py app_core/douyin_commerce_batch_draft_service.py app_core/douyin_location_preset_service.py test_douyin_commerce_batch_draft_service.py test_douyin_location_preset_service.py
git commit -m "feat: 添加抖音带货批次草稿与地点预设"
```

## Task 2: 批量载荷契约与排期生成

**Files:**
- Create: `app_core/douyin_commerce_batch_service.py`
- Test: `test_douyin_commerce_batch_service.py`

**Interfaces:**
- Consumes: `douyin_commerce_service.normalize_content_declaration()`、`douyin_music_service.normalize_music_readback()`、`zoneinfo.ZoneInfo("Asia/Shanghai")`。
- Produces: `validate_batch_payload(payload) -> dict`、`apply_interval_schedule(payload, now) -> dict`、`item_publish_payload(batch, item) -> dict`。

`validate_batch_payload()` 的批次信封使用 `workflow="douyin-commerce-batch"`；`item_publish_payload()` 生成给既有单视频 `DouyinCommerceSessionManager` 的内部操作载荷时必须使用 `workflow="douyin-commerce"`，同时添加 `batchWorkflow="douyin-commerce-batch"`。这样不改变既有单视频校验器，也不会把批次任务误标成单条任务。

- [ ] **Step 1: 写入批量契约的失败测试**

```python
def test_interval_schedule_defaults_to_shanghai_and_allows_item_override():
    batch = validate_batch_payload(BATCH_WITH_3_ITEMS)
    result = apply_interval_schedule(batch, now=SHANGHAI_NOW)
    self.assertEqual(result["items"][0]["scheduleTime"], "2026-08-07 09:00")
    self.assertEqual(result["items"][1]["scheduleTime"], "2026-08-07 09:30")
    self.assertEqual(result["items"][2]["scheduleTime"], "2026-08-07 15:00")

def test_batch_rejects_more_than_twenty_items_or_more_than_one_account():
    with self.assertRaisesRegex(DouyinCommerceBatchError, "1 至 20"):
        validate_batch_payload({**BATCH_WITH_3_ITEMS, "items": BATCH_WITH_3_ITEMS["items"] * 7})
    with self.assertRaisesRegex(DouyinCommerceBatchError, "一个"):
        validate_batch_payload({**BATCH_WITH_3_ITEMS, "accountList": ["a.json", "b.json"]})
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `.venv/bin/python -m unittest -v test_douyin_commerce_batch_service`  
Expected: FAIL，提示 `douyin_commerce_batch_service` 不存在。

- [ ] **Step 3: 实现可审计的批量载荷**

实现以下不可变约定：

```python
def item_publish_payload(batch: Mapping[str, Any], item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": 3,
        "workflow": "douyin-commerce",
        "batchWorkflow": "douyin-commerce-batch",
        "commerceMode": "local-group-buy",
        "contentType": "video",
        "accountList": [batch["accountFile"]],
        "fileList": [item["mediaPath"]],
        "title": batch["shared"]["title"],
        "description": batch["shared"]["description"],
        "tags": list(batch["shared"]["tags"]),
        "selectedMusic": dict(batch["shared"]["selectedMusic"]),
        "contentDeclaration": batch["shared"]["contentDeclaration"],
        "locationPoi": dict(item["locationPreset"]),
        "locationKeyword": item["locationPreset"]["name"],
        "locationScope": item["locationPreset"]["scope"],
        "enableTimer": item["enableTimer"],
        "scheduleTime": item.get("scheduleTime", ""),
    }
```

立即发布项目必须没有 `scheduleTime`；定时项目必须为未来的上海时间。禁止把 `commerceStore`、Cookie、验证码或浏览器字段放进契约。

- [ ] **Step 4: 运行批量服务测试**

Run: `.venv/bin/python -m unittest -v test_douyin_commerce_batch_service`  
Expected: PASS。

- [ ] **Step 5: 提交本任务**

```bash
git add app_core/douyin_commerce_batch_service.py test_douyin_commerce_batch_service.py
git commit -m "feat: 定义抖音带货批量任务契约"
```

## Task 3: 逐视频任务记录与平台回执状态

**Files:**
- Modify: `app_core/database.py:ensure_schema`
- Modify: `app_core/task_service.py:WORKFLOW_LABELS,create_pending_task,mark_platform_result`
- Test: `test_task_service.py`

**Interfaces:**
- Consumes: `item_publish_payload()` 生成的每条单视频载荷。
- Produces: `create_douyin_batch_task(batch) -> dict`、`mark_batch_item_result(task_id, item_id, ok, message, event_type, readback) -> None`、`commerce_summary_from_payload_json()` 的批量摘要。

- [ ] **Step 1: 写入逐条任务记录的失败测试**

```python
def test_batch_task_creates_one_publish_item_per_video_and_keeps_location_time():
    task = create_douyin_batch_task(BATCH_PAYLOAD)
    detail = get_task(task["id"])
    self.assertEqual(detail["workflowLabel"], "抖音带货批量")
    self.assertEqual(len(detail["items"]), 3)
    self.assertIn("北海银滩景区", detail["commerceSummary"])
    self.assertIn("立即发布", detail["commerceSummary"])

def test_only_platform_receipt_marks_a_batch_item_success():
    mark_batch_item_result(TASK_ID, ITEM_ID, ok=True, message="编辑页填写完成", event_type="editor_written", readback={})
    self.assertEqual(get_task(TASK_ID)["items"][0]["status"], "running")
    mark_batch_item_result(TASK_ID, ITEM_ID, ok=True, message="平台已定时", event_type="platform_scheduled_receipt", readback={"scheduleTime": "2026-08-07 09:00"})
    self.assertEqual(get_task(TASK_ID)["items"][0]["status"], "success")
```

- [ ] **Step 2: 运行任务服务测试，确认失败**

Run: `.venv/bin/python -m unittest -v test_task_service`  
Expected: FAIL，提示批量任务接口不存在。

- [ ] **Step 3: 实现逐视频而非逐平台的回填**

为 `publish_task_items` 增加可选列 `batchItemIndex INTEGER`、`locationSummary TEXT`、`scheduleSummary TEXT`，并增加按 `itemId` 更新的 `mark_batch_item_result()`。`editor_written`、`verification_waiting` 与 `preflight_readback` 只追加事件并保持 `running`；仅 `platform_publish_receipt` 或 `platform_scheduled_receipt` 更新为 `success`。失败事件只标记当前条；其余条保留 `pending`。

将 `WORKFLOW_LABELS["douyin-commerce-batch"]` 设为“抖音带货批量”，并让摘要逐行输出“视频、地点完整地址、立即发布/北京时间定时”。

- [ ] **Step 4: 运行任务服务与现有带货回归测试**

Run: `.venv/bin/python -m unittest -v test_task_service test_douyin_commerce_service`  
Expected: PASS。

- [ ] **Step 5: 提交本任务**

```bash
git add app_core/database.py app_core/task_service.py test_task_service.py
git commit -m "feat: 记录抖音带货批量逐视频回执"
```

## Task 4: 串行无头批量执行器与验证暂停

**Files:**
- Create: `app_core/douyin_commerce_batch_executor.py`
- Modify: `app_core/douyin_commerce_session.py:DouyinCommerceSessionManager`
- Modify: `app_core/douyin_verification.py:VerificationChallenge`
- Test: `test_douyin_commerce_batch_executor.py`
- Test: `test_douyin_verification.py`

**Interfaces:**
- Consumes: `validate_batch_payload()`、`item_publish_payload()`、`location_preset_service.match_location_preset()`、`commerce_session_manager`、`task_service.mark_batch_item_result()`、`verification_broker`。
- Produces: `DouyinCommerceBatchExecutor.run_preflight(batch, task_id, progress) -> list[dict]`、`DouyinCommerceBatchExecutor.run_publish(batch, task_id, progress) -> list[dict]`、`BatchProgressEvent(index, total, phase, message)`。

- [ ] **Step 1: 写入串行执行、单条失败继续与验证暂停的失败测试**

```python
def test_executor_processes_items_serially_and_continues_after_item_failure():
    manager = FakeCommerceSessionManager(fail_item_indexes={1})
    result = DouyinCommerceBatchExecutor(manager).run_publish(BATCH, task_id=41, progress=events.append)
    self.assertEqual([row["status"] for row in result], ["published", "failed", "published"])
    self.assertEqual(manager.max_open_sessions, 1)
    self.assertNotIn("submit:1", manager.calls)

def test_login_or_verification_pauses_batch_without_submitting_later_items():
    manager = FakeCommerceSessionManager(challenge_on_index=1)
    result = DouyinCommerceBatchExecutor(manager).run_publish(BATCH, task_id=41)
    self.assertEqual(result[1]["status"], "waiting_verification")
    self.assertEqual(result[2]["status"], "pending")

def test_location_preset_must_exactly_match_current_editor_candidates():
    manager = FakeCommerceSessionManager(location_candidates=[DIFFERENT_ADDRESS])
    result = DouyinCommerceBatchExecutor(manager).run_preflight(BATCH, task_id=41)
    self.assertEqual(result[0]["status"], "failed")
    self.assertNotIn("apply_location", manager.calls)
```

- [ ] **Step 2: 运行执行器测试，确认失败**

Run: `.venv/bin/python -m unittest -v test_douyin_commerce_batch_executor`  
Expected: FAIL，提示执行器不存在。

- [ ] **Step 3: 实现单会话串行协调器**

`DouyinCommerceBatchExecutor` 对每条只执行以下固定顺序：`start_upload` → `synchronize_content` → `select_cached_favorite_music` → `select_content_declaration` → `search_locations` → `match_location_preset` → `apply_location` → `sync_schedule` → `preflight` →（仅 publish 模式）`submit` → `close`。

`run_preflight()` 永远使用 `runtimeMode="preflight"` 与 `debugDryRun=True`，不提交。`run_publish()` 只有调用方已完成总确认后才能使用 `runtimeMode="publish"` 与 `debugDryRun=False`。

对 `DouyinCommerceSessionManager` 增加 `status()` 可返回非敏感当前阶段，且每次 `close()` 后清空临时编辑器引用。验证挑战需要额外携带 `item_index` 与 `item_label`，仅用于内存中的客户端提示；不写入事件详情或数据库。收到 `DouyinVerificationError`、登录失效、二维码、短信验证或未知提示时，批量状态设为 `waiting_verification` 并中止循环；验证码成功后从该条未完成阶段继续，不重新提交已成功条目。

- [ ] **Step 4: 运行批量执行器、验证与既有会话回归**

Run: `.venv/bin/python -m unittest -v test_douyin_commerce_batch_executor test_douyin_verification test_douyin_commerce_service`  
Expected: PASS。

- [ ] **Step 5: 提交本任务**

```bash
git add app_core/douyin_commerce_batch_executor.py app_core/douyin_commerce_session.py app_core/douyin_verification.py test_douyin_commerce_batch_executor.py test_douyin_verification.py
git commit -m "feat: 添加抖音带货串行批量执行器"
```

## Task 5: 把抖音带货页替换为三步批量工作台

**Files:**
- Modify: `ui/douyin_commerce_page.py`
- Modify: `ui/douyin_verification_dialog.py`
- Test: `test_douyin_commerce_service.py`
- Test: `test_douyin_verification_dialog.py`

**Interfaces:**
- Consumes: `batch_draft_service`、`location_preset_service`、`batch_service`、`DouyinCommerceBatchExecutor`、`verification_broker`。
- Produces: `DouyinCommercePage.collect_batch_payload()`、`save_batch_content()`、`restore_batch_content()`、`start_batch_preflight()`、`open_batch_submit_confirmation()`、`start_batch_publish()`。

- [ ] **Step 1: 写入桌面交互的失败测试**

```python
def test_batch_page_allows_multiple_videos_and_shows_account_identity(qtbot):
    page = DouyinCommercePage()
    page.refresh()
    page.select_video_indexes([0, 1, 2])
    self.assertEqual(page.selected_video_count(), 3)
    self.assertTrue(page.account_avatar_label.pixmap())
    self.assertIn("主体", page.account_identity_label.text())

def test_page_defaults_to_immediate_and_generates_interval_times_only_when_enabled(qtbot):
    page = prepared_batch_page(qtbot, item_count=3)
    self.assertFalse(page.batch_timer_enabled.isChecked())
    page.batch_timer_enabled.setChecked(True)
    page.interval_minutes.setValue(30)
    self.assertEqual(page.item_schedule_text(1), "09:30")
    page.set_item_schedule_override(2, "2026-08-07 15:00")
    self.assertEqual(page.item_schedule_text(2), "15:00")
```

- [ ] **Step 2: 运行 UI 测试，确认失败**

Run: `.venv/bin/python -m unittest -v test_douyin_commerce_service.DouyinCommercePageTests`  
Expected: FAIL，提示批量页面接口不存在。

- [ ] **Step 3: 用确认样稿的布局替换单视频表单结构**

保留类名 `DouyinCommercePage` 与侧边栏路由，避免改动启动入口。删除单视频/门店控件和强制顺序的 `_PLATFORM_STAGE_ORDER` 依赖，改为：

1. 内容准备页为左（账号下拉和身份卡）中（共享标题、文案、标签）右（多视频缩略图选择器）三栏；文件名必须截断，视频项显示封面、时长、尺寸；保存按钮放在右列底部。
2. 平台设置页左列为收藏音乐下拉和全部声明单选列表；右列为视频行表格，地点预设下拉显示完整地址，默认立即发布，开启按间隔定时后显示起始时间、间隔和每行覆盖编辑。
3. 检查与提交页显示可滚动逐条摘要；未分配地点时禁用继续按钮；提交确认弹窗必须列出全部视频、地点完整地址和每行发布方式。

音乐、声明、地点选择后的异步写入使用后台 `BackgroundTaskRunner`，只显示“正在写入/已回读/失败”状态，不显示原始 Playwright 报错。内容只改共享字段时调用当前会话的 `synchronize_content()`；账号或视频变化才使受影响条目需要重新上传。

验证码对话框增加“第 N/共 M 条：文件名”但不得展示手机号、验证码、二维码原文或浏览器内容。恢复本地批次草稿时直接填充控件，不弹成功提示。

- [ ] **Step 4: 运行 UI、验证对话框与无头策略回归**

Run: `.venv/bin/python -m unittest -v test_douyin_commerce_service test_douyin_verification_dialog`  
Expected: PASS。

- [ ] **Step 5: 运行桌面无界面自检**

Run: `.venv/bin/python desktop_native_app.py --ui-test`  
Expected: 输出 `NATIVE_DESKTOP_UI_OK`。

- [ ] **Step 6: 提交本任务**

```bash
git add ui/douyin_commerce_page.py ui/douyin_verification_dialog.py test_douyin_commerce_service.py test_douyin_verification_dialog.py
git commit -m "feat: 重构抖音带货批量工作台"
```

## Task 6: 集成路由、文档与回归验证

**Files:**
- Modify: `app_core/publish_service.py`
- Modify: `app_core/douyin_publish_executor.py`
- Modify: `docs/DOUYIN_COMMERCE_WORKFLOW.md`
- Modify: `README.md`
- Test: `test_douyin_publish_executor.py`
- Test: `test_douyin_commerce_service.py`

**Interfaces:**
- Consumes: `DouyinCommerceBatchExecutor.run_preflight/run_publish`、`create_douyin_batch_task`。
- Produces: 发布服务对 `workflow="douyin-commerce-batch"` 的显式路由；用户可读的工作流说明。

- [ ] **Step 1: 写入路由与安全边界失败测试**

```python
def test_publish_service_routes_batch_preflight_without_final_submit():
    with patch("app_core.douyin_commerce_batch_executor.batch_executor.run_preflight") as preflight:
        publish_service._run_preflight({"id": 41, "dryRun": 1}, [BATCH_PAYLOAD])
    preflight.assert_called_once()

def test_batch_publish_requires_explicit_confirmed_flag():
    with self.assertRaisesRegex(ValueError, "批量确认"):
        publish_service._run_publish({"id": 41, "dryRun": 0}, [{**BATCH_PAYLOAD, "batchConfirmed": False}])
```

- [ ] **Step 2: 运行路由测试，确认失败**

Run: `.venv/bin/python -m unittest -v test_douyin_publish_executor test_douyin_commerce_service`  
Expected: FAIL，提示批量工作流未路由。

- [ ] **Step 3: 增加显式路由和文档**

在 `publish_service` 识别 `workflow="douyin-commerce-batch"`，分别路由到批量预检和提交；提交必须同时满足 `runtimeMode="publish"`、`debugDryRun=False`、`batchConfirmed=True`。现有 `douyin-commerce` 单视频任务继续可读可执行，不做破坏性迁移。

在 `douyin_publish_executor` 拒绝将批量载荷误送入旧的单条执行函数，明确报错“请使用抖音带货批量执行器”。文档说明默认立即发布、间隔定时、地点预设重新匹配、验证码 60 秒、单条失败继续与最终回执状态。

- [ ] **Step 4: 运行完整抖音离线回归**

Run: `.venv/bin/python -m unittest -v test_douyin_commerce_batch_draft_service test_douyin_location_preset_service test_douyin_commerce_batch_service test_task_service test_douyin_commerce_batch_executor test_douyin_commerce_service test_douyin_publish_executor test_douyin_verification test_douyin_verification_dialog`  
Expected: PASS。

- [ ] **Step 5: 提交本任务**

```bash
git add app_core/publish_service.py app_core/douyin_publish_executor.py docs/DOUYIN_COMMERCE_WORKFLOW.md README.md test_douyin_publish_executor.py test_douyin_commerce_service.py
git commit -m "docs: 完成抖音带货批量工作流接入"
```

## Task 7: 实机验收门槛

**Files:**
- Modify: `docs/DOUYIN_COMMERCE_WORKFLOW.md`

**Interfaces:**
- Consumes: 所有 Task 1 至 Task 6 的离线通过结果。
- Produces: 可执行的预检和正式提交验收清单。

- [ ] **Step 1: 在文档添加不可跳过的实机验收清单**

```markdown
1. 选一个已登录账号和 2 条测试视频，保存并恢复本地批次；确认没有平台草稿。
2. 为两条分别分配已验证的官方地点预设；在 dry-run 中回读完整地址。
3. 验证默认立即发布与“起始时间 + 间隔 + 单条覆盖”均能在页面和载荷中正确显示。
4. 触发或模拟短信验证；确认客户端等待、60 秒重发限制、输入后只从当前条继续。
5. 取得用户单独发布授权后，执行真实批量；每条只凭平台最终回执记为已发布/已定时。
```

- [ ] **Step 2: 运行文档引用的最终离线命令**

Run: `.venv/bin/python -m unittest -v test_douyin_commerce_batch_draft_service test_douyin_location_preset_service test_douyin_commerce_batch_service test_task_service test_douyin_commerce_batch_executor test_douyin_commerce_service test_douyin_publish_executor test_douyin_verification test_douyin_verification_dialog && .venv/bin/python desktop_native_app.py --ui-test`  
Expected: 全部 PASS，最后输出 `NATIVE_DESKTOP_UI_OK`。

- [ ] **Step 3: 提交验收清单**

```bash
git add docs/DOUYIN_COMMERCE_WORKFLOW.md
git commit -m "docs: 添加抖音带货批量验收清单"
```

## 计划自审

- **规格覆盖：** Task 1 至 Task 2 覆盖本地草稿、地点预设、1–20 限制、共享/逐条字段与排期；Task 3 覆盖任务记录；Task 4 覆盖无头串行执行、失败继续、验证暂停；Task 5 覆盖已确认界面；Task 6 至 Task 7 覆盖路由、文档、离线与实机验收。
- **安全边界：** 每个实际提交都要求总确认；二维码/验证码/未知提示暂停；任务成功仅由最终回执决定；没有任务会保存敏感会话信息。
- **占位检查：** 本计划没有 TBD、TODO、"类似于"、"适当处理" 或无测试的实现步骤。
- **类型一致性：** 批次信封统一使用 `workflow="douyin-commerce-batch"`，而供既有单视频会话执行的 `item_publish_payload()` 固定使用 `workflow="douyin-commerce"` 与 `batchWorkflow="douyin-commerce-batch"`；草稿/预设/执行器/任务服务使用相同的 `items`、`locationPresetId`、`scheduleTime` 名称。
