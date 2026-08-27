# Douyin Graphic Matrix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an independent Douyin graphic-matrix workflow that publishes one shared 1–35 image set to 1–20 saved Douyin accounts with per-account content and schedule overrides, one account session at a time, auditable receipts, pause/resume, and failed-account-only retry.

**Architecture:** Add a matrix-specific contract and task creator above the existing publish service, then route both optional platform preflight and formal execution through one shared Douyin graphic editor adapter. The default path performs a local-only batch check, binds a short-lived one-time authorization to the immutable matrix snapshot, and executes each account sequentially with a just-in-time page check and conditional submit in the same browser session. Desktop UI, CLI, MCP, task records, and content projects all call the same service and task store.

**Tech Stack:** Python 3, PyQt6, SQLite, Playwright, existing `uploader.douyin_uploader.DouYinVideo` topic/verification helpers, `unittest`, local stdio MCP.

**Spec:** `docs/superpowers/specs/2026-08-26-douyin-graphic-matrix-design.md`

## Global Constraints

- Scope is domestic Douyin graphic publishing only; overseas modules and existing Douyin commerce behavior must remain unchanged.
- `schemaVersion` is exactly `oneclick-douyin-graphic-matrix/v1` and `workflow` is exactly `douyin-graphic-matrix`.
- A batch contains 1–35 shared images and 1–20 distinct saved Douyin accounts.
- The default check is local-only and must not open Douyin; optional full platform preflight must never locate or click the final submit control.
- Formal execution is serial, one saved account profile at a time, and each account gets only one platform session containing page check, fill/readback, and conditional submit.
- Formal execution requires a short-lived single-use authorization bound to the immutable account/content/image/schedule snapshot.
- Do not accept or expose Cookie, password, verification code, QR-code payload, or account session path in UI/CLI/MCP request JSON, task projections, or logs.
- Only official selected topic entities count as tags; plain `#text` fallback is a failure.
- A platform receipt, work ID, or scheduled-time readback is required before an item is successful.
- One account failure must not block later accounts; retry tasks include only failed or unstarted accounts and never resend successful accounts.
- Default schedule date is Beijing current date plus one day; schedule timezone is always `Asia/Shanghai`.
- Use the project test ladder: new failing test, closest unit tests, affected modules, then full suite and packaged-client smoke tests.
- Implementation release version is `0.5.15`; version changes only after the affected suite passes.

---

## File Structure

### New files

- `app_core/douyin_graphic_matrix_service.py` — validates the matrix envelope, resolves common/override fields, calculates schedules, builds immutable snapshots, and derives retry matrices.
- `app_core/douyin_graphic_editor.py` — the only Douyin graphic-page adapter for image upload, title/body/topic/schedule readback, final submit, and receipt readback.
- `app_core/douyin_graphic_matrix_executor.py` — serial per-account orchestration, browser lifetime, verification pauses, heartbeat, and terminal result handling.
- `ui/douyin_graphic_matrix_table.py` — account row widgets and per-field override state without platform operations.
- `ui/douyin_graphic_matrix_page.py` — three-step workflow, local check, batch confirmation, start/pause/resume, and result presentation.
- `test_douyin_graphic_matrix_service.py` — contract, inheritance, schedule, snapshot, and retry unit tests.
- `test_douyin_graphic_editor.py` — fake-page tests for shared preflight/formal field and topic-entity readback.
- `test_douyin_graphic_matrix_executor.py` — serial execution, verification, failure isolation, pause, receipt, and lease tests.
- `test_douyin_graphic_matrix_page.py` — offscreen UI tests for the three-step page and account override table.

### Modified files

- `app_core/database.py` — additive task-item columns for account ID, stable error code, receipt JSON, and authorization snapshot hash.
- `app_core/task_service.py` — one-item-per-account task creation, item transitions, pause/retry helpers, task labels, and stale-worker reconciliation.
- `app_core/oneclick_preflight.py` — replace the old Douyin graphic preflight implementation with the shared editor adapter.
- `app_core/publish_service.py` — route matrix local check, optional platform preflight, and formal execution without entering generic file×account logic.
- `app_core/controlled_publish.py` — matrix request parsing, immutable fingerprint authorization, safe task projection, and generic authorization routing.
- `app_core/content_project_gateway.py` — content-project matrix request methods.
- `app_core/oneclick_mcp_server.py` — matrix local-check, authorization, and formal-publish tools.
- `desktop_native_app.py` — allow the existing controlled CLI `authorize` action to authorize either a successful generic preflight or a successful matrix local check.
- `ui/main_window.py` — add the independent “抖音图文矩阵” navigation page and task retry/resume routing.
- `ui/task_page.py` — matrix-specific account result table, failure filter, receipt details, and retry action.
- `app_core/branding.py` — bump the implementation release from `0.5.14` to `0.5.15` after tests pass.
- `SOURCE_OF_TRUTH.md` — record only verified implementation, test, package, and real-platform evidence.
- Existing test files `test_task_service.py`, `test_publish_service.py`, `test_controlled_publish.py`, `test_content_project_gateway.py`, `test_oneclick_mcp_server.py`, `test_task_page.py`, `test_account_detection_ui.py`, `test_macos_build.py`, and `test_windows_build.py` — compatibility and integration coverage.

---

### Task 1: Matrix Contract, Overrides, and Schedule Calculation

**Files:**
- Create: `app_core/douyin_graphic_matrix_service.py`
- Create: `test_douyin_graphic_matrix_service.py`

**Interfaces:**
- Produces: `DouyinGraphicMatrixError(error_code: str, message: str)`.
- Produces: `prepare_matrix(raw: Mapping[str, Any], *, accounts: Sequence[Mapping[str, Any]], now: datetime | None = None) -> dict[str, Any]`.
- Produces: `effective_item_payload(matrix: Mapping[str, Any], target: Mapping[str, Any]) -> dict[str, Any]`.
- Produces: `matrix_scope_fingerprint(matrix: Mapping[str, Any]) -> str`.
- Produces: `retry_matrix(matrix: Mapping[str, Any], item_indexes: Sequence[int]) -> dict[str, Any]`.

- [ ] **Step 1: Write the failing contract and override tests**

```python
class DouyinGraphicMatrixServiceTests(unittest.TestCase):
    def test_prepare_matrix_keeps_images_once_and_resolves_each_account(self):
        prepared = prepare_matrix(
            {
                "schemaVersion": "oneclick-douyin-graphic-matrix/v1",
                "workflow": "douyin-graphic-matrix",
                "runtimeMode": "local_check",
                "content": {
                    "images": [str(self.image_a), str(self.image_b)],
                    "common": {"title": "通用标题", "body": "通用正文", "tags": ["矩阵发布"]},
                },
                "targets": [
                    {"itemIndex": 1, "accountId": 31, "overrides": {}, "scheduleTime": "2026-08-27 18:00", "timezone": "Asia/Shanghai"},
                    {"itemIndex": 2, "accountId": 32, "overrides": {"title": "账号二标题"}, "scheduleTime": "2026-08-27 18:30", "timezone": "Asia/Shanghai"},
                ],
            },
            accounts=self.accounts,
            now=datetime(2026, 8, 26, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
        self.assertEqual(prepared["content"]["images"], [str(self.image_a), str(self.image_b)])
        self.assertEqual(prepared["targets"][0]["effective"]["title"], "通用标题")
        self.assertEqual(prepared["targets"][1]["effective"]["title"], "账号二标题")
        self.assertNotIn("filePath", json.dumps(prepared["targets"], ensure_ascii=False))

    def test_default_schedule_is_beijing_tomorrow_and_only_unmodified_rows_reflow(self):
        prepared = prepare_matrix(self.unscheduled_matrix(), accounts=self.accounts, now=self.now)
        self.assertEqual(prepared["targets"][0]["scheduleTime"], "2026-08-27 18:00")
        self.assertEqual(prepared["targets"][1]["scheduleTime"], "2026-08-27 18:30")
        self.assertEqual(prepared["targets"][2]["scheduleTime"], "2026-08-28 09:15")
        self.assertTrue(prepared["targets"][2]["scheduleOverridden"])

    def test_matrix_rejects_duplicate_accounts_and_more_than_limits(self):
        with self.assertRaises(DouyinGraphicMatrixError) as raised:
            prepare_matrix(self.matrix_with_duplicate_account(), accounts=self.accounts, now=self.now)
        self.assertEqual(raised.exception.error_code, "douyin_graphic_matrix_invalid")
```

- [ ] **Step 2: Run the focused tests and confirm the missing-module failure**

Run: `.venv/bin/python -m unittest -v test_douyin_graphic_matrix_service`

Expected: FAIL with `ModuleNotFoundError: app_core.douyin_graphic_matrix_service`.

- [ ] **Step 3: Implement the normalized contract and fingerprint**

```python
SCHEMA_VERSION = "oneclick-douyin-graphic-matrix/v1"
WORKFLOW = "douyin-graphic-matrix"
SHANGHAI = ZoneInfo("Asia/Shanghai")

class DouyinGraphicMatrixError(ValueError):
    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = str(error_code)
        self.public_message = str(message)
        super().__init__(message)

def effective_item_payload(matrix: Mapping[str, Any], target: Mapping[str, Any]) -> dict[str, Any]:
    common = dict(matrix["content"]["common"])
    overrides = dict(target.get("overrides") or {})
    return {
        "type": 3,
        "contentType": "article",
        "workflow": WORKFLOW,
        "title": str(overrides.get("title") if overrides.get("title") is not None else common["title"]),
        "description": str(overrides.get("body") if overrides.get("body") is not None else common["body"]),
        "tags": list(overrides.get("tags") if overrides.get("tags") is not None else common["tags"]),
        "fileList": list(matrix["content"]["images"]),
        "accountIds": [int(target["accountId"])],
        "enableTimer": bool(target.get("scheduleTime")),
        "scheduleTime": str(target.get("scheduleTime") or "") or None,
        "scheduleTimezone": "Asia/Shanghai",
        "backgroundMode": False,
    }

def matrix_scope_fingerprint(matrix: Mapping[str, Any]) -> str:
    canonical = {
        "workflow": WORKFLOW,
        "images": list(matrix["content"]["imageHashes"]),
        "targets": [
            {
                "itemIndex": int(target["itemIndex"]),
                "accountId": int(target["accountId"]),
                "effective": dict(target["effective"]),
                "scheduleTime": str(target.get("scheduleTime") or ""),
                "timezone": "Asia/Shanghai",
            }
            for target in matrix["targets"]
        ],
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
```

`prepare_matrix` must normalize whitespace, require existing image files, hash image bytes, enforce image/account limits, resolve each `accountId` to a type-3 account, reject duplicate accounts, calculate tomorrow’s Beijing date, and store `effective` content per target. It must never copy `filePath` from account rows into the public matrix snapshot.

- [ ] **Step 4: Run the focused contract tests**

Run: `.venv/bin/python -m unittest -v test_douyin_graphic_matrix_service`

Expected: PASS for image sharing, field inheritance, schedule reflow, account isolation, fingerprint changes, and retry subset tests.

- [ ] **Step 5: Commit the contract**

```bash
git add app_core/douyin_graphic_matrix_service.py test_douyin_graphic_matrix_service.py
git commit -m "feat: add douyin graphic matrix contract"
```

---

### Task 2: One-Item-Per-Account Persistence and Terminal Results

**Files:**
- Modify: `app_core/database.py:469-570`
- Modify: `app_core/task_service.py:20-225,491-760,1355-1775`
- Modify: `test_task_service.py`

**Interfaces:**
- Consumes: `prepare_matrix`, `matrix_scope_fingerprint` from Task 1.
- Produces: `create_douyin_graphic_matrix_task(matrix: dict, *, mode: str, revision_source_task_id: int | None = None) -> dict`.
- Produces: `matrix_item_for_index(task_id: int, item_index: int) -> dict`.
- Produces: `start_matrix_item(task_id: int, item_id: int) -> None`.
- Produces: `finish_matrix_item(task_id: int, item_id: int, *, ok: bool, message: str, error_code: str = "", receipt: Mapping[str, Any] | None = None) -> None`.
- Produces: `close_matrix_parent(task_id: int) -> None`.

- [ ] **Step 1: Add failing migration and item-count tests**

```python
def test_matrix_task_creates_one_item_per_account_not_per_image(self):
    task = task_service.create_douyin_graphic_matrix_task(self.matrix, mode="oneclick_matrix_local_check")
    saved = task_service.get_task(task["id"])
    self.assertEqual(saved["itemCount"], 2)
    self.assertEqual([item["accountId"] for item in saved["items"]], [31, 32])
    self.assertEqual([item["batchItemIndex"] for item in saved["items"]], [1, 2])
    self.assertTrue(all(item["filePath"] == str(self.image_a) for item in saved["items"]))

def test_matrix_item_stores_stable_error_and_receipt_json(self):
    task_service.finish_matrix_item(
        self.task_id, self.item_id, ok=False,
        message="账号登录失效", error_code="douyin_graphic_account_session_expired",
        receipt=None,
    )
    item = task_service.get_task(self.task_id)["items"][0]
    self.assertEqual(item["status"], "failed")
    self.assertEqual(item["errorCode"], "douyin_graphic_account_session_expired")
    self.assertEqual(item["receiptJson"], "")
```

- [ ] **Step 2: Run the task-service tests and confirm schema/function failures**

Run: `.venv/bin/python -m unittest -v test_task_service`

Expected: FAIL because the new columns and matrix task functions do not exist.

- [ ] **Step 3: Add the migration and matrix-specific task writer**

Add these columns through `_add_columns`:

```python
(
    ("accountId", "INTEGER"),
    ("errorCode", "TEXT NOT NULL DEFAULT ''"),
    ("receiptJson", "TEXT NOT NULL DEFAULT ''"),
    ("authorizationSnapshotHash", "TEXT NOT NULL DEFAULT ''"),
)
```

`create_douyin_graphic_matrix_task` must insert the normalized matrix as a one-element `payloadJson` list, create exactly one `publish_task_items` row per target, set `batchItemIndex`, `accountId`, `accountLabel`, `scheduleSummary`, and the matrix fingerprint, and use mode `oneclick_matrix_local_check`, `oneclick_matrix_preflight`, or `oneclick_matrix_publish` only.

`finish_matrix_item` must JSON-project only `platformPostId`, `postUrl`, `publishedAt`, `scheduledAt`, `scheduleTime`, and `timezone`; it must calculate parent status as `running`, `success`, `partial_failed`, or `failed` from all item rows and must never convert an already successful item back to running or failed.

- [ ] **Step 4: Run persistence and existing task regressions**

Run: `.venv/bin/python -m unittest -v test_task_service test_task_page test_controlled_publish_resilience`

Expected: PASS, including old Douyin commerce batch task creation and stale-worker cleanup.

- [ ] **Step 5: Commit persistence**

```bash
git add app_core/database.py app_core/task_service.py test_task_service.py
git commit -m "feat: persist douyin graphic matrix account results"
```

---

### Task 3: Local-Only Check and Matrix Authorization

**Files:**
- Modify: `app_core/douyin_graphic_matrix_service.py`
- Modify: `app_core/publish_service.py:57-159,727-792`
- Modify: `app_core/controlled_publish.py:20-215,222-530`
- Modify: `desktop_native_app.py:192-258`
- Modify: `test_publish_service.py`
- Modify: `test_controlled_publish.py`
- Modify: `test_controlled_publish_process.py`

**Interfaces:**
- Consumes: normalized matrix and task creator from Tasks 1–2.
- Produces: `run_local_check(matrix: Mapping[str, Any]) -> list[dict[str, Any]]` with one result per account and no browser calls.
- Produces: `start_douyin_graphic_matrix(matrix: Mapping[str, Any]) -> dict` in `publish_service`.
- Produces: `authorize_completed_check(task_id: int, *, ttl_seconds: int = 600) -> dict[str, Any]` in `controlled_publish` while keeping `authorize_completed_preflight` as a compatibility wrapper.
- Produces: `consume_matrix_authorization(authorization_id: str, checked_task_id: int, matrix: Mapping[str, Any]) -> None`.

- [ ] **Step 1: Write failing safety and authorization tests**

```python
def test_matrix_local_check_never_calls_platform_executor(self):
    with patch("app_core.publish_service.douyin_graphic_matrix_executor") as executor:
        task = publish_service.start_douyin_graphic_matrix(self.local_check_matrix)
        self.wait_for_terminal(task["id"])
    executor.run_sync.assert_not_called()
    self.assertEqual(task_service.get_task(task["id"])["status"], "success")

def test_matrix_authorization_binds_exact_snapshot_and_is_single_use(self):
    authorization = authorize_completed_check(self.local_check_task_id, ttl_seconds=600)
    consume_matrix_authorization(
        authorization["authorizationId"],
        self.local_check_task_id,
        self.matrix,
    )
    with self.assertRaises(ControlledPublishError) as consumed:
        consume_matrix_authorization(
            authorization["authorizationId"],
            self.local_check_task_id,
            self.matrix,
        )
    self.assertEqual(consumed.exception.error_code, "controlled_authorization_consumed")
```

- [ ] **Step 2: Run focused service and authorization tests**

Run: `.venv/bin/python -m unittest -v test_publish_service test_controlled_publish test_controlled_publish_process`

Expected: FAIL because matrix routing and generic check authorization do not exist.

- [ ] **Step 3: Implement matrix routing without weakening existing workflows**

Add an early branch before generic `_validate_payloads`:

```python
def start_douyin_graphic_matrix(matrix: Mapping[str, Any]) -> dict:
    prepared = prepare_matrix(matrix, accounts=account_service.list_accounts())
    runtime_mode = str(prepared["runtimeMode"])
    if runtime_mode != "local_check":
        raise ValueError("抖音图文矩阵平台预检和正式执行器尚未接入")
    mode = "oneclick_matrix_local_check"
    task = task_service.create_douyin_graphic_matrix_task(prepared, mode=mode)
    worker = threading.Thread(
        target=_run_matrix_local_check,
        args=(task, prepared), daemon=True,
        name=f"oneclick-douyin-graphic-matrix-{runtime_mode}-{task['id']}",
    )
    _active_threads[int(task["id"])] = worker
    worker.start()
    return task
```

Extend controlled requests only when `workflow == "douyin-graphic-matrix"`; do not add matrix-only keys to generic target parsing. `authorize_completed_check` accepts either a successful `oneclick_preflight` task or a successful `oneclick_matrix_local_check` task and fingerprints the stored immutable payload. Existing generic and commerce authorization tests must remain unchanged.

- [ ] **Step 4: Run local-check and authorization regressions**

Run: `.venv/bin/python -m unittest -v test_douyin_graphic_matrix_service test_publish_service test_controlled_publish test_controlled_publish_process test_douyin_commerce_batch_service`

Expected: PASS and zero calls to Playwright for `runtimeMode=local_check`.

- [ ] **Step 5: Commit local check and authorization**

```bash
git add app_core/douyin_graphic_matrix_service.py app_core/publish_service.py app_core/controlled_publish.py desktop_native_app.py test_publish_service.py test_controlled_publish.py test_controlled_publish_process.py
git commit -m "feat: add local checks for douyin graphic matrix"
```

---

### Task 4: Shared Douyin Graphic Editor Adapter

**Files:**
- Create: `app_core/douyin_graphic_editor.py`
- Create: `test_douyin_graphic_editor.py`
- Modify: `app_core/oneclick_preflight.py:2671-2720,2894-2908`
- Modify: `test_publish_preflight_copy.py`

**Interfaces:**
- Consumes: effective per-account payloads from Task 1.
- Produces: `DouyinGraphicEditorError(error_code: str, message: str)`.
- Produces: `DouyinGraphicReadback(image_count: int, title: str, body: str, tags: tuple[str, ...], scheduled_at: str)`.
- Produces: `DouyinGraphicEditor.prepare(page: Any, payload: Mapping[str, Any]) -> Awaitable[DouyinGraphicReadback]`.
- Produces: `DouyinGraphicEditor.submit_and_read_receipt(page: Any, payload: Mapping[str, Any]) -> Awaitable[dict[str, Any]]`.

- [ ] **Step 1: Write fake-page tests for field order, topics, and no-submit preflight**

```python
def test_prepare_uploads_all_images_and_requires_official_topic_entities(self):
    readback = asyncio.run(self.editor.prepare(self.page, self.payload))
    self.assertEqual(self.page.uploaded_files, self.payload["fileList"])
    self.assertEqual(readback.image_count, 3)
    self.assertEqual(readback.tags, ("图文矩阵", "门店经营"))
    self.assertEqual(self.page.submit_clicks, 0)

def test_plain_hash_text_is_not_accepted_as_a_topic_entity(self):
    self.page.official_topics = {"图文矩阵"}
    with self.assertRaises(DouyinGraphicEditorError) as raised:
        asyncio.run(self.editor.prepare(self.page, self.payload))
    self.assertEqual(raised.exception.error_code, "douyin_topic_entity_missing")

def test_submit_requires_platform_receipt(self):
    self.page.receipt = None
    with self.assertRaises(DouyinGraphicEditorError) as raised:
        asyncio.run(self.editor.submit_and_read_receipt(self.page, self.payload))
    self.assertEqual(raised.exception.error_code, "douyin_graphic_submit_receipt_missing")
```

- [ ] **Step 2: Run editor tests and confirm the missing adapter**

Run: `.venv/bin/python -m unittest -v test_douyin_graphic_editor`

Expected: FAIL with `ModuleNotFoundError: app_core.douyin_graphic_editor`.

- [ ] **Step 3: Implement one adapter and route preflight through it**

```python
@dataclass(frozen=True)
class DouyinGraphicReadback:
    image_count: int
    title: str
    body: str
    tags: tuple[str, ...]
    scheduled_at: str

class DouyinGraphicEditor:
    async def prepare(self, page, payload: Mapping[str, Any]) -> DouyinGraphicReadback:
        files = [str(Path(path).resolve()) for path in payload.get("fileList") or []]
        await page.goto(f"{DOUYIN_UPLOAD_URL}?default-tab=3", wait_until="domcontentloaded", timeout=45_000)
        await self._upload_images(page, files)
        image_count = await self._read_image_count(page)
        if image_count != len(files):
            raise DouyinGraphicEditorError("douyin_graphic_image_upload_failed", "抖音图文图片数量回读不一致")
        title, body, tags = await self._fill_and_readback_content(page, payload)
        scheduled_at = await self._set_and_read_schedule(page, payload)
        return DouyinGraphicReadback(image_count, title, body, tuple(tags), scheduled_at)

    async def submit_and_read_receipt(self, page, payload: Mapping[str, Any]) -> dict[str, Any]:
        await self._click_unique_submit(page)
        receipt = await self._read_management_receipt(page, payload)
        if not receipt:
            raise DouyinGraphicEditorError("douyin_graphic_submit_receipt_missing", "抖音图文提交后没有取得平台回执")
        return receipt
```

Move the current image-entry navigation and title/body locators from `_douyin_graphic_preflight` into this adapter. Reuse `DouYinVideo.sync_uploaded_editor_content` for official topic candidate selection and entity readback; do not copy its selectors. `_douyin_graphic_preflight` must call `prepare` and return a message that explicitly says no final submit occurred.

- [ ] **Step 4: Run editor and existing Douyin preflight tests**

Run: `.venv/bin/python -m unittest -v test_douyin_graphic_editor test_publish_preflight_copy test_douyin_publish_executor`

Expected: PASS, including the existing video topic/verification contract.

- [ ] **Step 5: Commit the shared adapter**

```bash
git add app_core/douyin_graphic_editor.py app_core/oneclick_preflight.py test_douyin_graphic_editor.py test_publish_preflight_copy.py
git commit -m "feat: share douyin graphic editor readback"
```

---

### Task 5: Sequential Formal Executor and Failure Isolation

**Files:**
- Create: `app_core/douyin_graphic_matrix_executor.py`
- Create: `test_douyin_graphic_matrix_executor.py`
- Modify: `app_core/publish_service.py:417-679,727-792`
- Modify: `app_core/task_service.py:1355-1775`

**Interfaces:**
- Consumes: `DouyinGraphicEditor`, normalized matrix, and matrix task item functions.
- Produces: `run_matrix(matrix: Mapping[str, Any], *, task_id: int, editor_factory: Callable[..., DouyinGraphicEditor] = DouyinGraphicEditor, browser_launcher: Callable[..., AsyncContextManager[Any]] = open_matrix_account_page) -> Awaitable[list[dict[str, Any]]]`.
- Produces: `run_matrix_sync(matrix: Mapping[str, Any], *, task_id: int, editor_factory: Callable[..., DouyinGraphicEditor] = DouyinGraphicEditor, browser_launcher: Callable[..., AsyncContextManager[Any]] = open_matrix_account_page) -> list[dict[str, Any]]`.
- Produces: `run_matrix_preflight` and `run_matrix_preflight_sync` with the same dependency-injection keywords; these call `prepare` but never call `submit_and_read_receipt`.
- Produces private helpers `_matrix_account(account_id: int) -> dict`, `open_matrix_account_page(account: Mapping[str, Any]) -> AsyncContextManager[Any]`, `public_error_code(exc: BaseException) -> str`, and `public_result(target: Mapping[str, Any], status: str, **detail: Any) -> dict[str, Any]`.
- Produces: one terminal result per account with `itemIndex`, `accountId`, `status`, `errorCode`, `errorText`, and `receipt`.

- [ ] **Step 1: Write executor tests for one-session-per-account and continuation after failure**

```python
def test_accounts_run_serially_once_and_failure_does_not_block_next_account(self):
    editor = FakeEditor(fail_account_ids={32})
    launcher = FakeAccountLauncher()
    results = run_matrix_sync(
        self.matrix, task_id=self.task_id,
        editor_factory=lambda: editor,
        browser_launcher=launcher,
    )
    self.assertEqual(launcher.active_peak, 1)
    self.assertEqual(launcher.opened_account_ids, [31, 32, 33])
    self.assertEqual([row["status"] for row in results], ["success", "failed", "success"])
    self.assertEqual(editor.prepare_calls, [31, 32, 33])
    self.assertEqual(editor.submit_calls, [31, 33])

def test_success_requires_receipt_and_never_resubmits_a_terminal_item(self):
    results = run_matrix_sync(self.matrix, task_id=self.task_id, editor_factory=self.no_receipt_editor)
    self.assertEqual(results[0]["errorCode"], "douyin_graphic_submit_receipt_missing")
    run_matrix_sync(self.matrix, task_id=self.task_id, editor_factory=self.editor)
    self.assertEqual(self.editor.submit_count_for(31), 0)

def test_optional_platform_preflight_uses_same_editor_without_submit(self):
    results = run_matrix_preflight_sync(self.matrix, task_id=self.preflight_task_id, editor_factory=self.editor)
    self.assertTrue(all(row["status"] == "success" for row in results))
    self.assertEqual(self.editor.prepare_calls, [31, 32, 33])
    self.assertEqual(self.editor.submit_calls, [])
```

- [ ] **Step 2: Run executor tests and confirm the missing executor**

Run: `.venv/bin/python -m unittest -v test_douyin_graphic_matrix_executor`

Expected: FAIL with `ModuleNotFoundError: app_core.douyin_graphic_matrix_executor`.

- [ ] **Step 3: Implement serial account execution with terminal writes in `finally`**

```python
async def run_matrix(matrix: Mapping[str, Any], *, task_id: int) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for target in matrix["targets"]:
        item = task_service.matrix_item_for_index(task_id, int(target["itemIndex"]))
        if item["status"] == "success":
            continue
        task_service.start_matrix_item(task_id, int(item["id"]))
        try:
            payload = effective_item_payload(matrix, target)
            account = matrix_account(int(target["accountId"]))
            async with open_matrix_account_page(account) as page:
                editor = DouyinGraphicEditor(task_id=task_id, item_id=int(item["id"]))
                readback = await editor.prepare(page, payload)
                receipt = await editor.submit_and_read_receipt(page, payload)
            task_service.finish_matrix_item(task_id, int(item["id"]), ok=True, message="抖音图文平台回执已确认", receipt=receipt)
            results.append(public_result(target, "success", receipt=receipt))
        except Exception as exc:
            code = public_error_code(exc)
            task_service.finish_matrix_item(task_id, int(item["id"]), ok=False, message=str(exc), error_code=code)
            results.append(public_result(target, "failed", error_code=code, error_text=str(exc)))
        finally:
            task_service.touch_task_heartbeat(task_id)
    task_service.close_matrix_parent(task_id)
    return results
```

The real browser launcher must use the saved account’s independent storage state and existing fixed bundled Chromium helpers. Catch account/session errors per item, close context/browser before starting the next account, and let process-level cleanup call `fail_active_task` only for still-running or pending items.

At this task, extend `start_douyin_graphic_matrix` to route `platform_preflight` to `run_matrix_preflight_sync` and `publish` to `run_matrix_sync`. Before starting `publish`, consume the authorization bound to `confirmedCheckTaskId` and the normalized matrix fingerprint. The fixed public error mapping must cover `douyin_graphic_account_session_expired`, `douyin_graphic_entry_missing`, `douyin_graphic_image_upload_failed`, `douyin_graphic_title_readback_mismatch`, `douyin_graphic_body_readback_mismatch`, `douyin_topic_entity_missing`, `douyin_graphic_schedule_readback_mismatch`, `douyin_graphic_verification_cancelled`, and `douyin_graphic_submit_receipt_missing`.

- [ ] **Step 4: Run executor, task, and publish-service regressions**

Run: `.venv/bin/python -m unittest -v test_douyin_graphic_matrix_executor test_douyin_graphic_editor test_task_service test_publish_service test_douyin_verification`

Expected: PASS with maximum concurrent account sessions equal to one.

- [ ] **Step 5: Commit the formal executor**

```bash
git add app_core/douyin_graphic_matrix_executor.py app_core/publish_service.py app_core/task_service.py test_douyin_graphic_matrix_executor.py
git commit -m "feat: execute douyin graphic matrix serially"
```

---

### Task 6: Verification, Pause/Resume, Lease Recovery, and Failed-Only Retry

**Files:**
- Modify: `app_core/douyin_graphic_matrix_executor.py`
- Modify: `app_core/task_service.py:627-1307,1355-1545`
- Modify: `app_core/controlled_publish.py:361-487`
- Modify: `test_douyin_graphic_matrix_executor.py`
- Modify: `test_task_service.py`
- Modify: `test_controlled_publish_resilience.py`

**Interfaces:**
- Produces: `pause_douyin_graphic_matrix(task_id: int) -> None`.
- Produces: `prepare_douyin_graphic_matrix_retry(task_id: int) -> dict[str, Any]`.
- Produces: `create_douyin_graphic_matrix_retry(task_id: int) -> dict[str, Any]`.
- Produces: matrix-aware `project_task` output using stored `accountId`, `errorCode`, `receiptJson`, and schedule.

- [ ] **Step 1: Add failing verification, shutdown, and retry tests**

```python
def test_sms_verification_waits_in_current_account_and_continues_after_cooldown(self):
    broker = DouyinVerificationBroker()
    results = self.run_with_sms_challenge(broker, submitted_code="123456")
    self.assertEqual(results[0]["status"], "success")
    self.assertEqual(results[1]["status"], "success")
    self.assertEqual(self.launcher.opened_account_ids, [31, 32])

def test_retry_contains_failed_and_pending_accounts_only(self):
    prepared = task_service.prepare_douyin_graphic_matrix_retry(self.partial_task_id)
    self.assertEqual(prepared["itemIndexes"], [2, 4])
    self.assertEqual([row["accountId"] for row in prepared["matrix"]["targets"]], [32, 34])

def test_stale_matrix_worker_never_remains_running(self):
    reconciled = task_service.reconcile_stale_controlled_task(self.task_id, lease_seconds=30, now=self.later)
    self.assertTrue(reconciled)
    self.assertIn(task_service.get_task(self.task_id)["status"], {"failed", "partial_failed"})
```

- [ ] **Step 2: Run focused resilience tests**

Run: `.venv/bin/python -m unittest -v test_douyin_graphic_matrix_executor test_task_service test_controlled_publish_resilience`

Expected: FAIL until matrix modes, verification wait, and retry eligibility are implemented.

- [ ] **Step 3: Implement recoverable states without automatic silent submit**

Reuse `app_core.douyin_verification.verification_broker` and `DouyinSmsCooldownGate`. The executor may auto-continue after a completed verification or the official 60-second resend cooldown, but a user-requested pause or client shutdown must stop before the next final submit and require an explicit resume/retry action.

Extend stale reconciliation mode allowlist with `oneclick_matrix_local_check`, `oneclick_matrix_preflight`, and `oneclick_matrix_publish`. `prepare_douyin_graphic_matrix_retry` must reject active/source-ineligible tasks, preserve original `batchItemIndex`, bind `revisionSourceTaskId`, and select only `failed` or `pending` items.

When a matrix worker PID is gone and its heartbeat exceeds the lease, use the existing stable error code `controlled_worker_lease_expired`; close every still-running or pending item and calculate a terminal parent status instead of leaving the task active.

Matrix projection must read stored columns directly:

```python
{
    "platform": "抖音",
    "platformType": 3,
    "accountId": int(item.get("accountId") or 0),
    "account": str(item.get("accountLabel") or ""),
    "status": str(item.get("status") or "pending"),
    "errorCode": str(item.get("errorCode") or ""),
    "errorText": str(item.get("message") or "") if item.get("status") == "failed" else "",
    "receipt": json.loads(item["receiptJson"]) if item.get("receiptJson") else None,
    "scheduledAt": str(item.get("scheduleSummary") or ""),
}
```

- [ ] **Step 4: Run resilience and existing commerce resume tests**

Run: `.venv/bin/python -m unittest -v test_douyin_graphic_matrix_executor test_task_service test_controlled_publish_resilience test_douyin_commerce_batch_executor test_douyin_verification test_douyin_verification_dialog`

Expected: PASS; existing commerce pause/resume and verification behavior remains unchanged.

- [ ] **Step 5: Commit recovery and retry**

```bash
git add app_core/douyin_graphic_matrix_executor.py app_core/task_service.py app_core/controlled_publish.py test_douyin_graphic_matrix_executor.py test_task_service.py test_controlled_publish_resilience.py
git commit -m "feat: recover and retry douyin graphic matrix tasks"
```

---

### Task 7: Independent Three-Step Desktop UI

**Files:**
- Create: `ui/douyin_graphic_matrix_table.py`
- Create: `ui/douyin_graphic_matrix_page.py`
- Create: `test_douyin_graphic_matrix_page.py`
- Modify: `ui/main_window.py:339-510`
- Modify: `test_account_detection_ui.py`

**Interfaces:**
- Consumes: `prepare_matrix`, `start_douyin_graphic_matrix`, saved Douyin accounts, and task status.
- Produces: `DouyinGraphicAccountTable(QWidget)` with `set_accounts`, `set_common_fields`, `set_schedule`, `restore_common_field`, and `targets`.
- Produces: `DouyinGraphicMatrixPage(QWidget)` with three steps and signals `request_account_management`, `task_created(int)`, and `open_task_detail(int)`.

- [ ] **Step 1: Write offscreen UI tests for account limits, overrides, and local-check copy**

```python
def test_account_field_follows_common_until_overridden_then_can_restore(self):
    self.page.set_common_content("通用标题", "通用正文", ["矩阵发布"])
    self.page.account_table.edit_title(32, "账号二标题")
    self.page.set_common_content("新通用标题", "新通用正文", ["新话题"])
    self.assertEqual(self.page.account_table.row_for(31).title(), "新通用标题")
    self.assertEqual(self.page.account_table.row_for(32).title(), "账号二标题")
    self.page.account_table.restore_common_field(32, "title")
    self.assertEqual(self.page.account_table.row_for(32).title(), "新通用标题")

def test_local_check_button_does_not_claim_platform_preflight(self):
    labels = [button.text() for button in self.page.findChildren(QPushButton)]
    self.assertIn("本地批量检查", labels)
    self.assertNotIn("平台预检成功", " ".join(label.text() for label in self.page.findChildren(QLabel)))
```

- [ ] **Step 2: Run the new page tests and confirm the missing UI modules**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest -v test_douyin_graphic_matrix_page`

Expected: FAIL with missing UI modules.

- [ ] **Step 3: Implement the three-step page and navigation entry**

Build these visible steps:

1. `内容准备`: image picker/preview, common title, body, `TopicTagEditor`, and Douyin-only account selection capped at 20.
2. `账号设置`: one row per account, inherited/independent markers, per-field restore buttons, Beijing date+1 start date, start time, interval, and per-row time override.
3. `检查与提交`: final per-account snapshot table, `本地批量检查`, one batch confirmation dialog, and current task status.

The page must call service methods only. It must not import Playwright, `DouYinVideo`, or browser launch helpers. Add `("抖音图文矩阵", self.douyin_graphic_matrix, "ui/assets/nav-publish.svg")` to `MainWindow.page_definitions` immediately after “发布中心”.

- [ ] **Step 4: Run UI and main-window regressions**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest -v test_douyin_graphic_matrix_page test_account_detection_ui test_publish_page test_publish_topic_editor`

Expected: PASS at minimum window size 1180×720 and default 1600×900.

- [ ] **Step 5: Commit the desktop UI**

```bash
git add ui/douyin_graphic_matrix_table.py ui/douyin_graphic_matrix_page.py ui/main_window.py test_douyin_graphic_matrix_page.py test_account_detection_ui.py
git commit -m "feat: add douyin graphic matrix desktop page"
```

---

### Task 8: Account Result Table, Verification Dialog, and UI Retry Routing

**Files:**
- Modify: `ui/task_page.py:52-190,376-520`
- Modify: `ui/main_window.py:339-430`
- Modify: `ui/douyin_graphic_matrix_page.py`
- Modify: `ui/douyin_verification_dialog.py`
- Modify: `test_task_page.py`
- Modify: `test_douyin_verification_dialog.py`
- Modify: `test_douyin_graphic_matrix_page.py`

**Interfaces:**
- Consumes: matrix task projection and retry helpers from Task 6.
- Produces: `retry_douyin_graphic_matrix_requested = pyqtSignal(int)` in `TaskPage` and `TaskDetailDialog`.
- Produces: matrix result table columns `序号/账号/内容摘要/定时时间/阶段/状态/错误码/结果说明/平台回执`.

- [ ] **Step 1: Write failing task-detail and verification-context tests**

```python
def test_matrix_detail_identifies_exact_failed_account_and_receipt(self):
    dialog = TaskDetailDialog(self.matrix_task())
    table = dialog.findChild(QTableWidget, "douyinGraphicMatrixResultTable")
    self.assertEqual(table.columnCount(), 9)
    self.assertEqual(table.item(1, 1).text(), "账号二")
    self.assertEqual(table.item(1, 6).text(), "douyin_topic_entity_missing")
    self.assertIn("作品ID", table.item(0, 8).text())

def test_matrix_retry_button_emits_source_task_only(self):
    emitted = []
    dialog = TaskDetailDialog(self.partial_matrix_task())
    dialog.retry_douyin_graphic_matrix_requested.connect(emitted.append)
    dialog.retry_matrix_button.click()
    self.assertEqual(emitted, [77])
```

- [ ] **Step 2: Run the focused UI tests**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest -v test_task_page test_douyin_verification_dialog test_douyin_graphic_matrix_page`

Expected: FAIL until matrix table and retry signal exist.

- [ ] **Step 3: Implement matrix-specific display without changing commerce tables**

Add a matrix branch keyed by `workflow == "douyin-graphic-matrix"`; keep the existing commerce six-column result table and generic ten-column table unchanged. Show a status filter and focus the first failed account. Render receipt JSON as safe human-readable fields only.

Verification dialog text must include `第 X/Y 个账号` and the account display name. SMS codes remain visible. The dialog must not expose storage-state paths. Main window routes retry requests to `DouyinGraphicMatrixPage.open_failed_retry(task_id)` where the immutable subset is shown for a fresh confirmation.

- [ ] **Step 4: Run task, matrix UI, verification, and commerce table tests**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest -v test_task_page test_douyin_graphic_matrix_page test_douyin_verification_dialog test_douyin_commerce_service`

Expected: PASS with unchanged commerce task detail behavior.

- [ ] **Step 5: Commit result and retry UI**

```bash
git add ui/task_page.py ui/main_window.py ui/douyin_graphic_matrix_page.py ui/douyin_verification_dialog.py test_task_page.py test_douyin_verification_dialog.py test_douyin_graphic_matrix_page.py
git commit -m "feat: show douyin graphic matrix account results"
```

---

### Task 9: Controlled CLI, MCP, and Content-Project Matrix Gateway

**Files:**
- Modify: `app_core/content_project_gateway.py:115-340`
- Modify: `app_core/oneclick_mcp_server.py:31-150`
- Modify: `app_core/controlled_publish.py`
- Modify: `desktop_native_app.py:192-258,417-456`
- Modify: `test_content_project_gateway.py`
- Modify: `test_oneclick_mcp_server.py`
- Modify: `test_controlled_publish_process.py`
- Modify: `test_windows_build.py`
- Modify: `test_macos_build.py`

**Interfaces:**
- Produces: `ContentProjectGateway.check_douyin_graphic_matrix(manifest_path: str, targets: Sequence[Mapping[str, Any]]) -> dict`.
- Produces: `ContentProjectGateway.authorize_douyin_graphic_matrix(task_id: int) -> dict`.
- Produces: `ContentProjectGateway.publish_douyin_graphic_matrix(manifest_path: str, targets: Sequence[Mapping[str, Any]], *, confirmed_check_task_id: int, authorization_id: str) -> dict`.
- Produces MCP tools `oneclick_check_douyin_graphic_matrix`, `oneclick_authorize_douyin_graphic_matrix`, and `oneclick_publish_douyin_graphic_matrix`; existing `oneclick_task_status` is reused.

- [ ] **Step 1: Write failing safe-schema and gateway tests**

```python
def test_matrix_gateway_forwards_explicit_per_account_overrides(self):
    result = gateway.check_douyin_graphic_matrix(
        "/content/manifest.json",
        [
            {"accountId": 31, "title": None, "body": None, "tags": None, "schedule": {"localTime": "2026-08-27 18:00", "timezone": "Asia/Shanghai"}},
            {"accountId": 32, "title": "账号二标题", "body": None, "tags": ["账号二话题"], "schedule": {"localTime": "2026-08-27 18:30", "timezone": "Asia/Shanghai"}},
        ],
    )
    self.assertEqual(result["phase"], "local_check")
    self.assertEqual(submitted[0]["workflow"], "douyin-graphic-matrix")

def test_matrix_mcp_schemas_never_accept_sensitive_fields(self):
    tools = asyncio.run(create_server(_Gateway()).list_tools())
    schemas = json.dumps({tool.name: tool.input_schema for tool in tools}, ensure_ascii=False).lower()
    for forbidden in ("cookie", "password", "verification_code", "captcha", "storage_state"):
        self.assertNotIn(forbidden, schemas)
```

- [ ] **Step 2: Run gateway and MCP tests**

Run: `.venv/bin/python -m unittest -v test_content_project_gateway test_oneclick_mcp_server test_controlled_publish_process`

Expected: FAIL because the three matrix methods/tools do not exist.

- [ ] **Step 3: Add matrix tools that reuse the same service**

The local-check tool accepts a manifest path and an ordered list of account IDs plus optional title/body/tag/schedule overrides. The gateway loads the structured `oneclick-content/v1` bundle, requires `contentType=article`, builds the matrix envelope, and sends it to the existing controlled CLI subprocess. It never accepts account session files.

The authorization tool calls `authorize_completed_check`; the formal tool requires `confirmed_check_task_id` and `authorization_id`, rebuilds the exact matrix, and relies on fingerprint comparison and single-use consumption. Keep existing seven generic MCP tools unchanged and add the three matrix tools rather than changing their schemas.

- [ ] **Step 4: Run CLI/MCP and packaging contract tests**

Run: `.venv/bin/python -m unittest -v test_content_project_gateway test_oneclick_mcp_server test_controlled_publish test_controlled_publish_process test_windows_build test_macos_build`

Expected: PASS; the packaged entry point still exposes stdio MCP and no network listener.

- [ ] **Step 5: Commit the local interfaces**

```bash
git add app_core/content_project_gateway.py app_core/oneclick_mcp_server.py app_core/controlled_publish.py desktop_native_app.py test_content_project_gateway.py test_oneclick_mcp_server.py test_controlled_publish_process.py test_windows_build.py test_macos_build.py
git commit -m "feat: expose douyin graphic matrix local gateway"
```

---

### Task 10: Integrated Acceptance, Version 0.5.15, and Release Evidence

**Files:**
- Modify: `app_core/branding.py:10`
- Modify: `SOURCE_OF_TRUTH.md`
- Modify only if evidence requires correction: `QUALITY_GATES.md`
- Test: all new and affected test modules

**Interfaces:**
- Consumes: all Tasks 1–9.
- Produces: a verified source feature, a versioned Mac candidate, and Windows workflow-readiness evidence; a Windows binary is created only after a separate authorized push and GitHub workflow run. Real platform results remain separately recorded.

- [ ] **Step 1: Run the new feature tests and stop on the first failure**

Run:

```bash
.venv/bin/python -m unittest -v \
  test_douyin_graphic_matrix_service \
  test_douyin_graphic_editor \
  test_douyin_graphic_matrix_executor \
  test_douyin_graphic_matrix_page
```

Expected: PASS. If one module fails, stop, fix that module, and rerun only it before continuing.

- [ ] **Step 2: Run the affected domestic publishing suite**

Run:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest -v \
  test_publish_service test_controlled_publish test_controlled_publish_process \
  test_controlled_publish_resilience test_content_project_gateway \
  test_oneclick_mcp_server test_task_service test_task_page \
  test_douyin_publish_executor test_douyin_verification \
  test_douyin_verification_dialog test_douyin_commerce_batch_service \
  test_douyin_commerce_batch_executor test_douyin_commerce_service \
  test_account_detection_ui test_publish_page
```

Expected: PASS with no changes to existing commerce/video behavior.

- [ ] **Step 3: Bump the version and run the full suite**

Change only:

```python
APP_VERSION = "0.5.15"
```

Run: `.venv/bin/python -m unittest discover -p 'test_*.py'`

Expected: every test passes. Record the exact count; do not reuse the previous `2017/2017` figure.

- [ ] **Step 4: Run source-client and package smoke tests**

Run the project’s existing offscreen source startup check, then build the Mac candidate locally:

```bash
.venv/bin/python tools/build_macos.py --date 20260826
```

Verify the Mac archive name contains `0.5.15`, the embedded executable reports `0.5.15`, the MCP server lists all generic and matrix tools, and local matrix check creates exactly one task item per account without opening Douyin. On macOS, do not execute `tools/build_windows.py`; run `test_windows_build` as the local Windows-build contract. If the user separately asks for a Windows package, push the exact verified commit, dispatch `.github/workflows/build-windows.yml`, download the private artifact, and verify its SHA256 and embedded version before reporting it as built.

- [ ] **Step 5: Perform staged real-platform acceptance without claiming unverified publication**

Use an explicitly authorized test account and non-sensitive test content:

1. Run one-account optional platform preflight and confirm image count, title, body, official topic entities, and schedule readback with no final submit.
2. Obtain a fresh explicit user authorization for the exact immutable snapshot.
3. Run one-account formal scheduled submission and require a work ID or scheduled-time readback.
4. After that succeeds, run a small multi-account batch; do not start with 20 accounts.
5. Record local test, platform preflight, formal submit, scheduled receipt, and public availability as separate facts.

- [ ] **Step 6: Update the source of truth and commit release readiness**

Write only actual evidence to `SOURCE_OF_TRUTH.md`: commit, exact test counts, source startup, package paths/hashes, optional platform preflight result, formal result, and any remaining untested layer.

```bash
git add app_core/branding.py SOURCE_OF_TRUTH.md QUALITY_GATES.md
git commit -m "release: prepare oneclick 0.5.15"
```

Do not push, merge, install over the current client, or perform a real platform submit unless the user explicitly requests that separate action.
