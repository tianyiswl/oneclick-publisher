# Douyin Commerce Return-to-Edit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让抖音带货批量任务在全部失败或用户手动暂停后，把明确未成功的视频安全带回内容准备／平台设置修改，同时保持原任务与成功回执不可变。

**Architecture:** 任务服务从来源任务生成只读修改快照，Qt 页面在严格关闭屏障成功后把快照载入现有批量编辑控件；再次确认时才创建带 `revisionSourceTaskId` 的新任务。已成功视频使用 `mediaId`、旧任务使用规范化 `mediaPath` 建立禁止列表，待核对或非用户暂停任务整体拒绝恢复。

**Tech Stack:** Python 3、PyQt6、SQLite、`unittest`、现有 `BackgroundTaskRunner`、抖音带货批量服务与任务服务。

## Global Constraints

- 原任务、原逐条状态、平台回执、失败原因与完成时间不得修改。
- 只允许已结束的 `failed`／`partial_failed` 任务，或 `pauseReasonCode=user_request` 的 `paused` 任务进入修改流程。
- 只复制 `failed` 与 `pending` 条目；任何 `running`、待核对、登录／验证码处理中或系统安全暂停都必须整体拒绝。
- 返回修改时不访问账号、不启动浏览器、不上传、不预检、不保存平台草稿、不提交发布。
- 返回前必须同时验证 collector `closed=true`、`aliveCollectorCount` 为严格整数 `0`；若存在编辑会话，还要验证 session `closed=true`、`aliveSessionCount` 为严格整数 `0` 且 `status.active=false`。
- 运行中的批量 worker 与返回修改互斥；Qt 主线程不得同步等待浏览器资源关闭。
- 所有公开错误使用固定中文说明，不透传底层异常、路径、账号文件或页面文本。
- 测试节奏固定为：发现失败立即停止组合测试，修复后只跑对应单项；相关模块收尾只跑一次；全部任务完成后全量只跑一次。
- 保留仓库既有未跟踪目录 `.superpowers/brainstorm/` 与 `outputs/`，不得加入提交。

---

### Task 1: Preserve Stable Media Identity in Batch Payloads

**Files:**
- Modify: `app_core/douyin_commerce_batch_service.py:158-194`
- Modify: `app_core/douyin_commerce_batch_service.py:314-340`
- Modify: `ui/douyin_commerce_page.py:4472-4530`
- Modify: `ui/douyin_commerce_page.py:4533-4580`
- Test: `test_douyin_commerce_batch_service.py`
- Test: `test_douyin_commerce_service.py`

**Interfaces:**
- Consumes: 素材字典中的 `id: int` 与 `storedPath: str`。
- Produces: 每条批次 item 的 `mediaId: int | None`、`mediaPath: str`；`item_publish_payload()` 将 `mediaId` 保留到任务 `payloadJson`。

- [ ] **Step 1: Write the failing batch-service test**

```python
def test_batch_items_preserve_builtin_positive_media_identity(self) -> None:
    from copy import deepcopy

    raw = deepcopy(self.batch)
    raw["items"][0]["mediaId"] = 71
    prepared = prepare_batch_for_execution(raw, now=self.shanghai_now)
    payload = item_publish_payload(prepared, prepared["items"][0])
    self.assertEqual(prepared["items"][0]["mediaId"], 71)
    self.assertEqual(payload["mediaId"], 71)

    for invalid in (True, 0, -1, "71"):
        with self.subTest(invalid=invalid):
            raw["items"][0]["mediaId"] = invalid
            prepared = prepare_batch_for_execution(raw, now=self.shanghai_now)
            self.assertIsNone(prepared["items"][0]["mediaId"])
```

- [ ] **Step 2: Run the single test and confirm RED**

Run:

```bash
.venv/bin/python -m unittest -v \
  test_douyin_commerce_batch_service.DouyinCommerceBatchServiceTests.test_batch_items_preserve_builtin_positive_media_identity
```

Expected: FAIL because normalized items and publish payloads currently omit `mediaId`.

- [ ] **Step 3: Add strict media identity normalization**

Implement in `_items()`:

```python
media_id = raw.get("mediaId")
media_id = media_id if type(media_id) is int and media_id > 0 else None
result.append(
    {
        "mediaId": media_id,
        "mediaPath": media_path,
        "locationPreset": _location_preset(raw.get("locationPreset"), index=index),
        "scheduleTimeOverride": override,
    }
)
```

Add to `item_publish_payload()`:

```python
"mediaId": item.get("mediaId"),
```

Add `"mediaId": video.get("id")` to both `collect_batch_payload()` and `_batch_draft_payload()` item dictionaries.

- [ ] **Step 4: Extend the existing UI payload contract test and run both single tests**

In the existing
`DouyinCommerceBatchUiTests.test_collect_batch_payload_generates_explicit_item_timer_fields_before_ui_task_creation`, add:

```python
self.assertEqual(payload["items"][0]["mediaId"], 1)
```

Run only the two named tests. Expected: PASS.

- [ ] **Step 5: Commit Task 1**

```bash
git add app_core/douyin_commerce_batch_service.py ui/douyin_commerce_page.py \
  test_douyin_commerce_batch_service.py test_douyin_commerce_service.py
git commit -m "保留抖音批量视频媒体身份"
```

---

### Task 2: Build an Immutable Revision Snapshot in Task Service

**Files:**
- Modify: `app_core/database.py:200-242`
- Modify: `app_core/task_service.py:297-345`
- Modify: `app_core/task_service.py:400-590`
- Modify: `app_core/task_service.py:600-875`
- Test: `test_task_service.py`

**Interfaces:**
- Consumes: `prepare_douyin_batch_revision(task_id: int) -> dict[str, object]` 的来源任务 ID。
- Produces: `revisionAllowed`, `blockedReason`, `sourceTaskId`, `sourceTaskNo`, `revisionItemIndexes`, `successfulMediaKeys`, `draft`。
- Produces: `create_douyin_batch_task(..., revision_source_task_id: int | None = None)`；新任务公开字段 `revisionSourceTaskId` 与 `revisionSourceTaskNo`。

- [ ] **Step 1: Write eligibility and immutability RED tests**

Add focused tests to `DouyinCommerceBatchTaskTests`:

First extend `_create_paused_douyin_batch_source()` so each source item contains
`"mediaId": index`. Add this test-only helper directly below it:

```python
def _set_revision_source_states(
    self,
    task_id: int,
    statuses: list[str],
    *,
    task_status: str,
    pause_reason: str | None,
) -> None:
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT id FROM publish_task_items WHERE taskId = ? ORDER BY id",
            (int(task_id),),
        ).fetchall()
        self.assertEqual(len(rows), len(statuses))
        for row, status in zip(rows, statuses):
            conn.execute(
                "UPDATE publish_task_items SET status = ? WHERE id = ?",
                (status, int(row["id"])),
            )
        conn.execute(
            "UPDATE publish_tasks SET status = ?, pauseReasonCode = ? WHERE id = ?",
            (task_status, pause_reason, int(task_id)),
        )
        conn.commit()
```

```python
def test_prepare_revision_keeps_only_failed_and_pending_items(self) -> None:
    source = self._create_paused_douyin_batch_source()
    self._set_revision_source_states(
        source["id"],
        ["success", "failed", "pending"],
        task_status="paused",
        pause_reason=task_service.PAUSE_REASON_USER_REQUEST,
    )
    before = task_service.get_task(source["id"])
    plan = task_service.prepare_douyin_batch_revision(source["id"])
    after = task_service.get_task(source["id"])
    self.assertTrue(plan["revisionAllowed"])
    self.assertEqual(plan["revisionItemIndexes"], [2, 3])
    self.assertEqual(len(plan["draft"]["items"]), 2)
    self.assertEqual(plan["successfulMediaKeys"], ["media:1"])
    self.assertEqual(after, before)

def test_prepare_revision_rejects_ambiguous_and_nonmanual_pauses(self) -> None:
    for reason in (
        task_service.PAUSE_REASON_RECEIPT_AMBIGUOUS,
        task_service.PAUSE_REASON_WAITING_LOGIN,
        task_service.PAUSE_REASON_WAITING_VERIFICATION,
        task_service.PAUSE_REASON_AUTO_FAILURE,
        task_service.PAUSE_REASON_CLIENT_SHUTDOWN,
        task_service.PAUSE_REASON_CLEANUP_INCOMPLETE,
    ):
        source = self._create_paused_douyin_batch_source()
        self._set_revision_source_states(
            source["id"],
            ["success", "pending", "pending"],
            task_status="paused",
            pause_reason=reason,
        )
        self.assertFalse(
            task_service.prepare_douyin_batch_revision(source["id"])["revisionAllowed"]
        )
```

- [ ] **Step 2: Run only the two tests and confirm RED**

Expected: FAIL because `prepare_douyin_batch_revision` does not exist.

- [ ] **Step 3: Add the revision source column and task creation parameter**

Add to `publish_tasks` creation and `_add_columns()`:

```sql
revisionSourceTaskId INTEGER
```

Add a non-unique lookup index:

```sql
CREATE INDEX IF NOT EXISTS idx_publish_tasks_revision_source
ON publish_tasks(revisionSourceTaskId)
WHERE revisionSourceTaskId IS NOT NULL
```

Extend signatures:

```python
def create_pending_task(
    payloads: list[dict],
    mode: str = "desktop",
    *,
    resume_source_task_id: int | None = None,
    revision_source_task_id: int | None = None,
) -> dict:
```

```python
def create_douyin_batch_task(
    batch: dict,
    mode: str = "oneclick_publish",
    *,
    schedule_now=None,
    resume_source_task_id: int | None = None,
    revision_source_task_id: int | None = None,
    batch_item_indexes: list[int] | None = None,
) -> dict:
```

Pass `revision_source_task_id` into the INSERT without changing the unique `resumeSourceTaskId` rule.

- [ ] **Step 4: Implement the pure read-only revision planner**

Use fixed public rejection shape:

```python
def _revision_blocked(task_id: int, reason: str, source: dict | None = None) -> dict[str, object]:
    return {
        "revisionAllowed": False,
        "blockedReason": reason,
        "sourceTaskId": int(task_id),
        "sourceTaskNo": str((source or {}).get("taskNo") or ""),
        "revisionItemIndexes": [],
        "successfulMediaKeys": [],
    }
```

Implement `prepare_douyin_batch_revision()` with these exact gates:

```python
if source["workflow"] != "douyin-commerce-batch":
    return _revision_blocked(task_id, "仅支持抖音带货批量任务返回修改", source)
if source["status"] == "paused":
    if source.get("pauseReasonCode") != PAUSE_REASON_USER_REQUEST:
        return _revision_blocked(task_id, "当前暂停原因不能返回修改", source)
elif source["status"] not in {"failed", "partial_failed"}:
    return _revision_blocked(task_id, "当前任务尚未结束或没有明确失败结果", source)
if any(item.get("status") == "running" for item in items):
    return _revision_blocked(task_id, "当前任务仍有视频正在处理", source)
```

Require `len(items) == len(payloads)`, accept only `failed` and `pending`, reject damaged payloads and missing media, and build a draft compatible with `_batch_draft_payload()` rather than an execution-ready publish batch. Preserve shared fields, per-item location snapshot, `mediaId`, `mediaPath`, schedule override and last search intent. Do not rewrite expired schedule here; the existing UI restore rule will replace today／past dates with the Beijing-time next-day default.

Media keys must be deterministic:

```python
def _batch_media_key(payload: dict) -> str:
    media_id = payload.get("mediaId")
    if type(media_id) is int and media_id > 0:
        return f"media:{media_id}"
    file_list = payload.get("fileList")
    path = str(file_list[0] if isinstance(file_list, list) and file_list else "").strip()
    return f"path:{Path(path).resolve(strict=False)}" if path else ""
```

- [ ] **Step 5: Add relation readback and source immutability test**

When `get_task()` reads a task with `revisionSourceTaskId`, resolve only its public task number into `revisionSourceTaskNo`.

```python
def test_revision_child_records_source_without_mutating_it(self) -> None:
    source = self._create_paused_douyin_batch_source()
    self._set_revision_source_states(
        source["id"],
        ["failed", "failed", "failed"],
        task_status="failed",
        pause_reason=None,
    )
    before = task_service.get_task(source["id"])
    child = task_service.create_douyin_batch_task(
        self.batch,
        revision_source_task_id=source["id"],
    )
    detail = task_service.get_task(child["id"])
    self.assertEqual(detail["revisionSourceTaskId"], source["id"])
    self.assertEqual(detail["revisionSourceTaskNo"], source["taskNo"])
    self.assertEqual(task_service.get_task(source["id"]), before)
```

Run only Task 2 named tests. Expected: PASS.

- [ ] **Step 6: Commit Task 2**

```bash
git add app_core/database.py app_core/task_service.py test_task_service.py
git commit -m "增加抖音失败批次修改快照"
```

---

### Task 3: Load Revision Draft and Block Successful Videos in UI

**Files:**
- Modify: `ui/douyin_commerce_page.py:480-605`
- Modify: `ui/douyin_commerce_page.py:2617-2745`
- Modify: `ui/douyin_commerce_page.py:4595-4745`
- Modify: `ui/douyin_commerce_page.py:6960-7205`
- Test: `test_douyin_commerce_service.py`

**Interfaces:**
- Consumes: Task 2 plan fields `draft`, `sourceTaskId`, `sourceTaskNo`, `successfulMediaKeys`。
- Produces: `_apply_batch_editable_payload(payload: Mapping[str, object]) -> None` shared by saved-draft restore and revision restore.
- Produces: `_batch_revision_source_task_id: int | None` and `_batch_revision_blocked_media_keys: set[str]`.

Add this reusable fixture helper to `DouyinCommerceBatchUiTests` before the
Task 3 tests; later Task 4 tests reuse its returned plan:

```python
def _load_revision_ui_fixture(self) -> dict[str, object]:
    tempdir = tempfile.TemporaryDirectory()
    self.addCleanup(tempdir.cleanup)
    root = Path(tempdir.name)
    videos = []
    for media_id, name in enumerate(("done.mp4", "failed.mp4", "pending.mp4"), start=1):
        path = root / name
        path.write_bytes(b"offline-video")
        videos.append(
            {"id": media_id, "typeText": "视频", "storedPath": str(path), "filename": name}
        )
    account = {
        "id": 71,
        "type": 3,
        "status": 1,
        "filePath": "douyin-71.json",
        "profileName": "测试主体",
        "userName": "测试账号",
    }
    with patch(
        "ui.douyin_commerce_page.account_service.list_accounts", return_value=[account]
    ), patch(
        "ui.douyin_commerce_page.media_service.list_media", return_value=videos
    ):
        self.page.refresh()
    location = {
        "poiId": "poi-1",
        "name": "测试地点",
        "address": "北京市朝阳区测试路1号",
        "scope": "domestic",
        "commissionFilter": "commission",
        "observedCommissionType": "commission",
    }
    draft = {
        "accountId": 71,
        "accountFile": "douyin-71.json",
        "shared": {
            "title": "修改后的标题",
            "description": "修改未完成视频",
            "tags": ["测试"],
            "selectedMusic": {
                "musicId": "music-1", "title": "测试音乐", "creator": "测试", "duration": "00:30"
            },
            "contentDeclaration": "无需添加自主声明",
        },
        "lastLocationSearch": {
            "scope": "domestic", "keyword": "测试地点", "commissionFilter": "commission"
        },
        "publishMode": "interval-schedule",
        "schedule": {"timezone": "Asia/Shanghai", "startTime": "2099-08-13 16:00", "intervalMinutes": 30},
        "items": [
            {"mediaId": row["id"], "mediaPath": row["storedPath"], "locationPreset": location, "enableTimer": True, "scheduleTimeOverride": ""}
            for row in videos[1:]
        ],
    }
    return {
        "revisionAllowed": True,
        "sourceTaskId": 41,
        "sourceTaskNo": "T08122117-665B",
        "revisionItemIndexes": [2, 3],
        "successfulMediaKeys": ["media:1"],
        "draft": draft,
    }
```

- [ ] **Step 1: Write the revision-loading RED test**

```python
def test_revision_plan_restores_only_unfinished_items_and_all_editable_fields(self) -> None:
    plan = self._load_revision_ui_fixture()
    self.page._apply_batch_revision_plan(plan)
    self.assertEqual(self.page.selected_video_count(), 2)
    self.assertEqual(self.page._batch_revision_source_task_id, 41)
    self.assertEqual(self.page.pages.currentIndex(), 1)
    self.assertEqual(self.page.title_input.text(), "修改后的标题")
    self.assertEqual(self.page._selected_music["musicId"], "music-1")
    self.assertEqual(len(self.page._batch_locations), 2)
    self.assertEqual(self.page.batch_publish_mode.currentData(), "interval-schedule")
```

- [ ] **Step 2: Run the single test and confirm RED**

Expected: FAIL because revision state and loader do not exist.

- [ ] **Step 3: Extract the existing saved-draft renderer**

Move the side-effect-free control restoration part of `restore_batch_content()` into:

```python
def _apply_batch_editable_payload(self, payload: Mapping[str, object]) -> None:
    """把已校验的本地可编辑快照投射到现有控件；不访问平台。"""
```

`restore_batch_content()` must still load from `douyin_commerce_batch_draft_service` and then call this helper. The helper restores account, video indexes, shared content, music, declaration, location search intent, per-video locations, schedule overrides and publish mode exactly once.

Add revision state in `__init__`:

```python
self._batch_revision_source_task_id: int | None = None
self._batch_revision_source_task_no = ""
self._batch_revision_blocked_media_keys: set[str] = set()
```

Implement:

```python
def _apply_batch_revision_plan(self, plan: Mapping[str, object]) -> None:
    self._batch_revision_source_task_id = int(plan["sourceTaskId"])
    self._batch_revision_source_task_no = _normalized(plan.get("sourceTaskNo"))
    self._batch_revision_blocked_media_keys = {
        str(value) for value in plan.get("successfulMediaKeys", []) if isinstance(value, str)
    }
    self._apply_batch_editable_payload(dict(plan["draft"]))
    self.pages.setCurrentIndex(1)
    self._sync_view()
```

- [ ] **Step 4: Write and fix the successful-video re-selection RED tests**

```python
def test_revision_blocks_successful_video_from_checkbox_and_programmatic_selection(self) -> None:
    self._load_revision_ui_fixture()
    self.page._batch_revision_blocked_media_keys = {"media:1"}
    with patch("ui.douyin_commerce_page.QMessageBox.warning") as warning:
        self.page.select_video_indexes([1, 2])
    self.assertEqual(self.page.selected_video_count(), 1)
    self.assertEqual(self.page._selected_videos()[0]["id"], 2)
    warning.assert_called_once()
```

Add one UI media-key helper that matches Task 2:

```python
@staticmethod
def _media_key(media: object) -> str:
    if not isinstance(media, Mapping):
        return ""
    media_id = media.get("id")
    if type(media_id) is int and media_id > 0:
        return f"media:{media_id}"
    path = _normalized(media.get("storedPath"))
    return f"path:{Path(path).resolve(strict=False)}" if path else ""
```

Apply the guard in `_batch_video_item_changed()`, `select_video_indexes()` and `select_all_batch_videos()`. A blocked item must remain unchecked; one fixed warning is enough per user action.

- [ ] **Step 5: Reset revision state only at real workflow boundaries**

Clear revision state in `_reset_platform_settings_after_abandon()`, `_clear_current_batch_platform_choices()` after complete success, and `start_new_content()`. Do not clear it when task creation fails, so the edited batch stays recoverable.

Run only the Task 3 named tests. Expected: PASS.

- [ ] **Step 6: Commit Task 3**

```bash
git add ui/douyin_commerce_page.py test_douyin_commerce_service.py
git commit -m "恢复抖音未完成视频到可编辑批次"
```

---

### Task 4: Add the Review-Page Return State Machine and Close Barrier

**Files:**
- Modify: `ui/douyin_commerce_page.py:570-590`
- Modify: `ui/douyin_commerce_page.py:2460-2540`
- Modify: `ui/douyin_commerce_page.py:3921-3955`
- Modify: `ui/douyin_commerce_page.py:4840-5030`
- Modify: `ui/douyin_commerce_page.py:6960-7005`
- Test: `test_douyin_commerce_service.py`

**Interfaces:**
- Consumes: `task_service.prepare_douyin_batch_revision(task_id)` and Task 3 `_apply_batch_revision_plan()`.
- Produces: `_batch_result_task_id: int | None`, `_batch_revision_available: bool`, `_BATCH_REVISION_TASK_KEY` and `return_unfinished_batch_to_edit()`.

- [ ] **Step 1: Write button-state RED tests**

```python
def test_review_button_offers_revision_after_failed_or_manual_paused_batch(self) -> None:
    self.page.pages.setCurrentIndex(2)
    self.page._batch_result_task_id = 41
    self.page._batch_revision_available = True
    self.page._sync_view()
    self.assertEqual(self.page.review_back_button.text(), "返回修改未完成视频")
    self.assertTrue(self.page.review_back_button.isEnabled())

def test_review_button_never_offers_revision_after_all_success_or_ambiguous_receipt(self) -> None:
    self.page._batch_result_task_id = 41
    self.page._batch_revision_available = False
    self.page._sync_view()
    self.assertEqual(self.page.review_back_button.text(), "开始新内容")
```

- [ ] **Step 2: Run the two tests and confirm RED**

Expected: FAIL because the current projection only checks editor/setup session presence.

- [ ] **Step 3: Retain authoritative result task ID and compute availability after worker cleanup**

At batch start, set both active and result IDs:

```python
self._batch_task_id = int(task["id"])
self._batch_result_task_id = self._batch_task_id
self._batch_revision_available = False
```

In `_batch_operation_finished()`, capture `task_id` before clearing `_batch_task_id`, then call `prepare_douyin_batch_revision(task_id)` once to project availability. The click handler must call it again; the first call is display-only.

Update `_sync_workbench()`:

```python
if self._batch_revision_available and not self._busy():
    self.review_back_button.setText("返回修改未完成视频")
elif self._session_id or self._setup_generation_id:
    self.review_back_button.setText("返回平台设置")
else:
    self.review_back_button.setText("开始新内容")
```

- [ ] **Step 4: Write close-barrier and stale-state RED tests**

```python
def test_return_to_edit_rechecks_task_and_waits_for_zero_alive_barrier(self) -> None:
    plan = self._load_revision_ui_fixture()
    self.page.runner = self._InlineRunner()
    self.page._batch_result_task_id = 41
    with patch(
        "ui.douyin_commerce_page.task_service.prepare_douyin_batch_revision",
        return_value=plan,
    ) as prepare, patch.object(
        self.page, "_close_revision_resources",
        return_value={"closed": True, "aliveCollectorCount": 0, "aliveSessionCount": 0},
    ), patch.object(self.page, "_apply_batch_revision_plan") as apply:
        self.page.return_unfinished_batch_to_edit()
    self.assertGreaterEqual(prepare.call_count, 1)
    apply.assert_called_once_with(plan)

def test_return_to_edit_keeps_result_page_when_cleanup_is_incomplete(self) -> None:
    self.page.runner = self._InlineRunner()
    self.page.pages.setCurrentIndex(2)
    with patch.object(
        self.page, "_close_revision_resources",
        return_value={"closed": False, "aliveCollectorCount": 1, "aliveSessionCount": 0},
    ), patch.object(self.page, "_apply_batch_revision_plan") as apply:
        self.page.return_unfinished_batch_to_edit()
    self.assertEqual(self.page.pages.currentIndex(), 2)
    apply.assert_not_called()
```

- [ ] **Step 5: Implement asynchronous return and strict close projection**

Add a dedicated runner key. `return_from_review()` routes to `return_unfinished_batch_to_edit()` when a revision is available; otherwise it keeps existing single-session/new-content behavior.

`return_unfinished_batch_to_edit()` must:

1. reject if `_busy()` or no result task ID;
2. re-read `prepare_douyin_batch_revision()`;
3. run `_close_revision_resources()` in `BackgroundTaskRunner`;
4. only apply the plan in `on_success` after strict close validation;
5. retain the result page and fixed feedback on any failure.

Implement worker-side close:

```python
def _close_revision_resources(self) -> dict[str, object]:
    collector = self._close_setup_generation("return_to_edit")
    if not self._collector_close_is_complete(collector):
        return {"closed": False, "aliveCollectorCount": 1, "aliveSessionCount": 0}
    session_id = self._session_id
    if session_id:
        session = douyin_commerce_session.commerce_session_manager.close_strict(session_id)
        alive_sessions = session.get("aliveSessionCount") if isinstance(session, Mapping) else None
        if not isinstance(session, Mapping) or session.get("closed") is not True \
                or type(alive_sessions) is not int or alive_sessions != 0:
            return {"closed": False, "aliveCollectorCount": 0, "aliveSessionCount": 1}
    return {"closed": True, "aliveCollectorCount": 0, "aliveSessionCount": 0}
```

The success callback must also require strict built-in integer zeros and then clear verification memory before applying the plan. Ordinary exceptions map to “返回修改前的临时会话未能完全关闭，请重试”；`KeyboardInterrupt` and `SystemExit` keep existing process-control semantics.

- [ ] **Step 6: Pass revision source into the next confirmed task**

Change `open_batch_submit_confirmation()`:

```python
task = task_service.create_douyin_batch_task(
    payload,
    mode="oneclick_publish",
    revision_source_task_id=self._batch_revision_source_task_id,
)
```

If creation raises, keep `_batch_revision_source_task_id`, selected videos and edited controls unchanged.

Run only Task 4 named tests. Expected: PASS.

- [ ] **Step 7: Commit Task 4**

```bash
git add ui/douyin_commerce_page.py test_douyin_commerce_service.py
git commit -m "增加抖音未完成视频返回修改入口"
```

---

### Task 5: Show Revision Source in Task Detail and Update Workflow Documentation

**Files:**
- Modify: `ui/task_page.py:70-110`
- Modify: `docs/DOUYIN_COMMERCE_WORKFLOW.md`
- Test: `test_douyin_commerce_service.py`

**Interfaces:**
- Consumes: `get_task()` fields `revisionSourceTaskId` and `revisionSourceTaskNo`.
- Produces: Task detail summary row `修改来源` with text `修改自 <taskNo>`.

- [ ] **Step 1: Write the task-detail RED test**

```python
def test_task_detail_shows_revision_source_task_number(self) -> None:
    task = task_service._attach_content_type(
        {
            "id": 99,
            "taskNo": "T08122130-NEW1",
            "status": "failed",
            "title": "修改任务",
            "payloadJson": json.dumps(
                [
                    {
                        "contentType": "video",
                        "workflow": "douyin-commerce",
                        "batchWorkflow": "douyin-commerce-batch",
                        "fileList": ["/tmp/failed.mp4"],
                        "enableTimer": False,
                    }
                ],
                ensure_ascii=False,
            ),
            "items": [],
            "events": [],
            "revisionSourceTaskId": 41,
            "revisionSourceTaskNo": "T08122117-665B",
        }
    )
    dialog = TaskDetailDialog(task)
    labels = [label.text() for label in dialog.findChildren(QLabel)]
    self.assertIn("修改来源", labels)
    self.assertIn("修改自 T08122117-665B", labels)
```

- [ ] **Step 2: Run the single test and confirm RED**

Expected: FAIL because the summary panel has no revision-source row.

- [ ] **Step 3: Add the source row without changing resume controls**

Refactor the optional rows to use a `next_summary_row` counter starting at `5`.
Append batch result／commerce summary first, revision source second, and failure
reason last. This prevents the new row from overlapping the existing batch result
or failure row. Only add the revision row when `revisionSourceTaskId` is a positive
built-in integer and `revisionSourceTaskNo` is non-empty:

```python
next_summary_row = 5
if self._is_batch_task():
    summary_values.append(("批量结果", self._batch_result_summary(), next_summary_row, 0, 5))
    next_summary_row += 1
elif task.get("commerceSummary"):
    summary_values.append(("带货信息", task.get("commerceSummary"), next_summary_row, 0, 5))
    next_summary_row += 1
if type(task.get("revisionSourceTaskId")) is int \
        and task["revisionSourceTaskId"] > 0 \
        and str(task.get("revisionSourceTaskNo") or "").strip():
    summary_values.append(
        ("修改来源", f"修改自 {task['revisionSourceTaskNo']}", next_summary_row, 0, 5)
    )
    next_summary_row += 1
if not self._is_batch_task() or task.get("lastError"):
    summary_values.append(("失败原因", task.get("lastError"), next_summary_row, 0, 5))
```

Keep “继续发布” qualification and `resumeSourceTaskId` behavior unchanged.

- [ ] **Step 4: Document the user flow and safety distinction**

Add to `docs/DOUYIN_COMMERCE_WORKFLOW.md`:

- all-success tasks start new content;
- explicit user pause／clear failure can create a local revision batch;
- success and ambiguous items cannot be copied;
- return-to-edit is not “continue publishing” and creates no task until reconfirmation;
- the original task remains immutable.

Run the Task 5 single test. Expected: PASS.

- [ ] **Step 5: Commit Task 5**

```bash
git add ui/task_page.py docs/DOUYIN_COMMERCE_WORKFLOW.md test_douyin_commerce_service.py
git commit -m "展示抖音修改批次来源"
```

---

### Task 6: Integration Verification and Final Evidence

**Files:**
- Create: `docs/superpowers/reports/2026-08-12-douyin-commerce-return-to-edit-report.md`
- Modify only if a failing targeted test proves a defect: files already listed in Tasks 1-5.

**Interfaces:**
- Consumes: all Task 1-5 public contracts.
- Produces: one implementation report with commit SHAs, RED／GREEN evidence, related-suite result, full-suite result and platform-action boundary.

- [ ] **Step 1: Run one related-module suite**

Run once:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest -v \
  test_douyin_commerce_batch_service \
  test_task_service \
  test_douyin_commerce_service.DouyinCommerceBatchUiTests \
  test_douyin_commerce_service.DouyinCommerceTaskPresentationTests
```

If it fails, stop immediately, fix only the first proven defect, run that one failing test, then rerun this related suite once.

- [ ] **Step 2: Run the final full suite exactly once after related tests are green**

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -v
```

Expected: all tests PASS. Do not repeat a green full suite.

- [ ] **Step 3: Run static and diff gates**

```bash
.venv/bin/python -m py_compile \
  app_core/database.py \
  app_core/douyin_commerce_batch_service.py \
  app_core/task_service.py \
  ui/douyin_commerce_page.py \
  ui/task_page.py \
  test_douyin_commerce_batch_service.py \
  test_task_service.py \
  test_douyin_commerce_service.py
git diff --check
git status --short
```

Verify status contains no account state, database, media, logs, `.superpowers/brainstorm/` contents or `outputs/` contents.

- [ ] **Step 4: Write the implementation report**

The report must state:

- exact commits for Tasks 1-5;
- each targeted RED reason and corresponding GREEN command;
- related and full suite totals and exit codes;
- original-task immutability evidence;
- zero-alive close-barrier evidence;
- no real account, browser, upload, preflight, platform draft, submit or publish action occurred;
- any remaining platform-DOM risk is unverified rather than passed.

- [ ] **Step 5: Commit final evidence**

```bash
git add docs/superpowers/reports/2026-08-12-douyin-commerce-return-to-edit-report.md
git commit -m "记录抖音返回修改功能验收"
```

- [ ] **Step 6: Request a focused code review**

Use `superpowers:requesting-code-review` against the implementation range. Block completion on any Critical or Important finding; fix each finding with a new single RED／GREEN test before refreshing only the affected related suite.
