# 硅基进化公众号自动草稿桥接 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让一键发把硅基进化已冻结的 V1.2 内容包保存为公众号草稿，并以后台草稿列表回读作为唯一成功证据。

**Architecture:** 内容项目将冻结包的文章 ID、绝对路径和包哈希原子写入本地收件箱；一键发在桌面端明确启用后读取它，重新校验 V1.2 包，再走独立的公众号草稿执行器。执行器永远不调用正式发表路径，保存草稿后按标题和时间回读草稿列表，并把不含凭据的回执交回内容项目。

**Tech Stack:** Python 3.12、PySide6、Playwright、SQLite、`unittest`、现有 `wechat_operator` CLI。

**Spec:** `docs/superpowers/specs/2026-08-25-silicon-evolution-wechat-draft-bridge-design.md`

## Global Constraints

- 草稿链路只能保存草稿；不得发表、群发、定时发表或点击最终发表确认。
- 交接和回执只存文章 ID、包路径、哈希、账号显示名、状态、时间和安全错误码；不得存 Cookie、Token、二维码或正文副本。
- 只接受 V1.2 `release_ready` 包，且 `publish_allowed=false`；一键发必须重新验证包内文件哈希。
- 公众号编辑页和草稿列表都必须回读硅基进化账号；账号、标题、控件或草稿结果不唯一即停止。
- 草稿回读是独立协作证据，不改变 `delivered_unverified`、`manually_published_user_confirmed` 或 `manually_published`。
- 一键发共享核心仅在 `integration/oneclick-0.5.0` 修改；内容项目的现有未提交台账与 `tools/wechat_operator` 子模块变更不得混入提交。

---

## File Structure

### 一键发集成工作区

- Create: `app_core/silicon_evolution_draft_package.py` — 校验 V1.2 冻结包并生成只草稿载荷。
- Create: `app_core/wechat_draft_queue.py` — 原子读取交接记录、幂等锁和安全回执写入。
- Create: `app_core/wechat_draft_executor.py` — 填写编辑器、保存草稿、草稿列表回读。
- Modify: `app_core/publish_service.py` — 只为 type 10 增加独立 `wechat_draft` 运行模式与执行路由。
- Modify: `ui/publish_page.py` — 导入 V1.2 包、显示“保存硅基进化草稿”明确动作和结果。
- Modify: `desktop_native_app.py` — 仅在用户在界面启用后启动本地草稿队列轮询。
- Create: `test_silicon_evolution_draft_package.py`、`test_wechat_draft_queue.py`、`test_wechat_draft_executor.py`。
- Modify: `test_wechat_publish_executor.py`、`test_content_bundle.py`、`test_publish_service.py`（若不存在则创建）与离屏 UI 测试。

### 硅基进化内容项目

- Create: `tools/wechat_operator/src/wechat_operator/draft_bridge.py` — 写入交接记录并只接受匹配的草稿回执。
- Modify: `tools/wechat_operator/src/wechat_operator/cli.py` — 增加 `enqueue-wechat-draft`、`record-wechat-draft-receipt`。
- Modify: `tools/wechat_operator/src/wechat_operator/store.py` — 在 `run_events` 中登记草稿回读，不改变文章发表状态。
- Create: `tools/wechat_operator/tests/test_draft_bridge.py` 与 `tools/wechat_operator/tests/test_cli_draft_bridge.py`。
- Modify: `tools/wechat_operator/prompts/daily_content_orchestrator.md` — 只在本地包成为 `delivered_unverified` 后写交接记录；不等待草稿结果阻塞次日生产。

## Wire Contract

交接文件路径固定为：`~/Library/Application Support/一键发/wechat-draft-bridge/inbox/<article_id>.json`；回执目录为同级 `receipts/`。文件一律先写入同目录 `.tmp`，再以原子替换完成。

```json
{
  "schema_version": "silicon-evolution-wechat-draft-handoff/v1",
  "article_id": "WX-20260825-001",
  "package_path": "/absolute/path/to/WX-20260825-001",
  "package_sha256": "64 lowercase hex characters",
  "target_account_name": "硅基进化",
  "intent": "wechat_draft_only",
  "created_at": "2026-08-25T08:00:00+08:00"
}
```

```json
{
  "schema_version": "silicon-evolution-wechat-draft-receipt/v1",
  "article_id": "WX-20260825-001",
  "package_sha256": "64 lowercase hex characters",
  "account_name": "硅基进化",
  "status": "draft_readback_confirmed",
  "recorded_at": "2026-08-25T08:03:00+08:00",
  "error_code": null
}
```

失败回执的 `status` 为 `stopped`，必须有下列之一的 `error_code`：`desktop_not_enabled`、`account_mismatch`、`login_required`、`unknown_dialog`、`save_control_ambiguous`、`field_readback_mismatch`、`draft_readback_missing`、`draft_readback_ambiguous`。

### Task 1: 内容侧草稿交接和回执登记

**Files:**
- Create: `tools/wechat_operator/src/wechat_operator/draft_bridge.py`
- Modify: `tools/wechat_operator/src/wechat_operator/cli.py`
- Modify: `tools/wechat_operator/src/wechat_operator/store.py`
- Test: `tools/wechat_operator/tests/test_draft_bridge.py`
- Test: `tools/wechat_operator/tests/test_cli_draft_bridge.py`

**Interfaces:**
- Consumes: `OperatorStore.get_article(article_id)`, `verify_release_package(package_dir)`, `release_package_sha256(package_dir)`.
- Produces: `enqueue_wechat_draft(store, article_id, inbox_dir) -> Path` and `record_wechat_draft_receipt(store, receipt_path, run_id) -> dict[str, str]`.

- [ ] **Step 1: Write failing contract tests**

```python
def test_enqueue_writes_only_a_frozen_delivered_package(tmp_path):
    path = enqueue_wechat_draft(store, "WX-20260825-001", tmp_path / "inbox")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["intent"] == "wechat_draft_only"
    assert payload["package_sha256"] == frozen_sha256
    assert "body" not in payload

def test_receipt_requires_matching_article_and_sha(tmp_path):
    receipt = write_receipt(tmp_path, article_id="WX-other", package_sha256=frozen_sha256)
    with pytest.raises(DraftBridgeError, match="文章 ID"):
        record_wechat_draft_receipt(store, receipt, run_id="draft-readback:test")
```

- [ ] **Step 2: Run the two tests and verify failure**

Run: `tools/wechat_operator/.venv/bin/python -m pytest tools/wechat_operator/tests/test_draft_bridge.py -q`

Expected: import failure because `draft_bridge` does not exist.

- [ ] **Step 3: Implement strict JSON records and idempotency**

```python
def enqueue_wechat_draft(store: OperatorStore, article_id: str, inbox_dir: Path) -> Path:
    article = store.get_article(article_id)
    if article.state is not ArticleState.DELIVERED_UNVERIFIED:
        raise DraftBridgeError("只有当前本地交付稿可以进入公众号草稿队列")
    package = Path(article.package_path or "")
    verify_release_package(package)
    package_sha256 = release_package_sha256(package)
    if package_sha256 != article.package_sha256:
        raise DraftBridgeError("冻结包哈希不一致")
    return _atomic_json(inbox_dir / f"{article_id}.json", _handoff_payload(article, package_sha256))
```

`record_wechat_draft_receipt()` 只接受精确 schema、`draft_readback_confirmed`、文章 ID 和包哈希匹配的回执；它调用 `store.record_run_event()` 写 `wechat_draft_readback_confirmed`，不得调用 `transition_article()`。

- [ ] **Step 4: Add exact CLI commands and tests**

```text
wechat-operator --state-db data/runtime/v12-state.db enqueue-wechat-draft \
  --article-id WX-20260825-001 --inbox-dir "$HOME/Library/Application Support/一键发/wechat-draft-bridge/inbox" --json

wechat-operator --state-db data/runtime/v12-state.db record-wechat-draft-receipt \
  --receipt /absolute/path/receipts/WX-20260825-001.json \
  --run-id draft-readback:WX-20260825-001 --json
```

- [ ] **Step 5: Run tests and commit only content-operator files**

Run: `tools/wechat_operator/.venv/bin/python -m pytest tools/wechat_operator/tests/test_draft_bridge.py tools/wechat_operator/tests/test_cli_draft_bridge.py -q`

Expected: PASS. Commit from the `tools/wechat_operator` repository only with `feat: add wechat draft bridge receipts`.

### Task 2: 一键发 V1.2 冻结包导入器

**Files:**
- Create: `app_core/silicon_evolution_draft_package.py`
- Test: `test_silicon_evolution_draft_package.py`

**Interfaces:**
- Consumes: handoff `package_path`, `package_sha256`; V1.2 `release-manifest.json` and `publication.json`.
- Produces: `FrozenWechatDraftPackage` and `load_frozen_wechat_draft_package(package_dir: Path, expected_sha256: str) -> FrozenWechatDraftPackage`.

- [ ] **Step 1: Write failing fixture tests**

```python
def test_loads_v12_package_and_checks_every_manifest_file(tmp_path):
    package = write_v12_package(tmp_path, article_id="WX-20260825-001")
    loaded = load_frozen_wechat_draft_package(package, expected_sha256=PACKAGE_SHA)
    self.assertEqual(loaded.article_id, "WX-20260825-001")
    self.assertEqual(loaded.title, "测试标题")
    self.assertEqual(loaded.cover_path.name, "cover-master.png")

def test_rejects_changed_asset_and_publish_enabled_manifest(tmp_path):
    package = write_v12_package(tmp_path)
    (package / "assets" / "01.png").write_bytes(b"changed")
    with self.assertRaisesRegex(FrozenWechatDraftPackageError, "哈希"):
        load_frozen_wechat_draft_package(package, expected_sha256=PACKAGE_SHA)
```

- [ ] **Step 2: Run test and verify failure**

Run: `.venv/bin/python -m unittest -v test_silicon_evolution_draft_package`

Expected: import failure because the V1.2 importer does not exist.

- [ ] **Step 3: Implement isolated importer**

```python
@dataclass(frozen=True)
class FrozenWechatDraftPackage:
    article_id: str
    package_sha256: str
    title: str
    digest: str
    content_html: str
    cover_path: Path
    body_image_paths: tuple[Path, ...]

def load_frozen_wechat_draft_package(package_dir: Path, expected_sha256: str) -> FrozenWechatDraftPackage:
    manifest = _load_and_verify_release_manifest(package_dir)
    if manifest["state"] != "release_ready" or manifest["publish_allowed"] is not False:
        raise FrozenWechatDraftPackageError("只接受未授权发表的冻结发布包")
    _require_sha256(expected_sha256, "package_sha256")
    return _build_draft_package(package_dir, manifest, expected_sha256)
```

The loader verifies every manifest-listed file digest, requires `content.html`, `cover-master.png`, `publication.json`, and any `assets/` file before returning paths. It rejects parent traversal and never mutates the package.

- [ ] **Step 4: Run focused tests and commit**

Run: `.venv/bin/python -m unittest -v test_silicon_evolution_draft_package test_content_bundle`

Expected: PASS. Commit: `feat: validate silicon evolution frozen packages`.

### Task 3: 独立公众号草稿执行器和服务路由

**Files:**
- Create: `app_core/wechat_draft_executor.py`
- Modify: `app_core/publish_service.py`
- Test: `test_wechat_draft_executor.py`
- Modify: `test_wechat_publish_executor.py`
- Create: `test_publish_service.py`

**Interfaces:**
- Consumes: `FrozenWechatDraftPackage`, existing `_wechat_preflight(page, payload, account)`, `_account_for_payload(payload)`, `_storage_state(account)`.
- Produces: `run_wechat_draft_sync(payload: dict[str, Any], *, task_id: int) -> dict[str, Any]`; it returns only `{"ok": bool, "message": str, "draftTitle": str | None, "errorCode": str | None}`.

- [ ] **Step 1: Write failing boundary tests**

```python
def test_draft_executor_rejects_publish_controls():
    payload = draft_payload(runtimeMode="wechat_draft", debugDryRun=True)
    self.assertFalse(payload["wechatGroupNotification"])
    self.assertFalse(payload["enableTimer"])
    with self.assertRaisesRegex(WechatDraftError, "发表"):
        validate_draft_payload({**payload, "runtimeMode": "publish"})

async def test_draft_readback_requires_one_matching_title_and_time(page):
    result = await _readback_saved_draft(page, "测试标题", started_at=NOW)
    self.assertTrue(result["ok"])
    self.assertEqual(result["title"], "测试标题")
```

- [ ] **Step 2: Run tests and verify failure**

Run: `.venv/bin/python -m unittest -v test_wechat_draft_executor test_publish_service`

Expected: import failure because no draft executor or `wechat_draft` route exists.

- [ ] **Step 3: Implement the no-publish execution boundary**

```python
async def run_wechat_draft(payload: dict[str, Any], *, task_id: int) -> dict[str, Any]:
    _validate_wechat_draft_payload(payload)
    account = _account_for_payload(payload)
    async with _open_wechat_context(account, payload) as page:
        await _wechat_preflight(page, _preflight_payload(payload), account=account)
        _require_editor_account(await account_service._detect_display_name(page, 10), account)
        button = await _exact_visible_enabled_button(page, "保存草稿")
        await button.click(timeout=10_000)
        return await _readback_saved_draft(page, str(payload["title"]), started_at=_now())
```

The executor must not import `wechat_publish_policy`, call `_prepare_final_options`, locate `发表`, or create `runtimeMode=publish`. Its only accepted runtime mode is `wechat_draft`; the payload requires `debugDryRun=true`, `wechatGroupNotification=false`, `enableTimer=false`, `scheduleTime=None`, and `originalDeclaration` to be explicit.

- [ ] **Step 4: Route only type 10 drafts to the new executor**

```python
if runtime_mode == "wechat_draft":
    if any(int(item["type"]) != 10 for item in prepared):
        raise ValueError("公众号草稿任务只能包含一个公众号账号")
    worker_target = _run_wechat_draft
elif runtime_mode == "draft":
    worker_target = _run_draft
```

`_run_wechat_draft()` records `platform_draft` events and never calls `_run_publish()`.

- [ ] **Step 5: Run targeted tests and commit**

Run: `.venv/bin/python -m unittest -v test_wechat_draft_executor test_wechat_publish_executor test_wechat_preflight test_publish_service`

Expected: PASS. Commit: `feat: add verified wechat draft executor`.

### Task 4: 桌面队列、明确启用和安全回执

**Files:**
- Create: `app_core/wechat_draft_queue.py`
- Modify: `ui/publish_page.py`
- Modify: `desktop_native_app.py`
- Test: `test_wechat_draft_queue.py`
- Test: `test_publish_page.py`

**Interfaces:**
- Consumes: handoff JSON from Task 1, `load_frozen_wechat_draft_package()` and `publish_service.start_desktop_publish()` with `runtimeMode="wechat_draft"`.
- Produces: `process_wechat_draft_inbox(inbox_dir: Path, receipts_dir: Path) -> list[DraftQueueResult]` and atomic receipt files.

- [ ] **Step 1: Write failing queue and UI tests**

```python
def test_queue_is_inert_until_user_enabled(tmp_path):
    result = process_wechat_draft_inbox(tmp_path / "inbox", tmp_path / "receipts", enabled=False)
    self.assertEqual(result, [])

def test_queue_writes_stopped_receipt_without_retry_for_bad_account(tmp_path):
    write_handoff(tmp_path / "inbox", account="别的账号")
    result = process_wechat_draft_inbox(tmp_path / "inbox", tmp_path / "receipts", enabled=True)
    self.assertEqual(result[0].error_code, "account_mismatch")
    self.assertTrue((tmp_path / "receipts" / "WX-20260825-001.json").is_file())
```

- [ ] **Step 2: Run test and verify failure**

Run: `.venv/bin/python -m unittest -v test_wechat_draft_queue test_publish_page`

Expected: import failure because the queue and opt-in control do not exist.

- [ ] **Step 3: Implement atomic queue processing**

```python
def process_wechat_draft_inbox(inbox_dir: Path, receipts_dir: Path, *, enabled: bool) -> list[DraftQueueResult]:
    if not enabled:
        return []
    handoffs = sorted(inbox_dir.glob("WX-*.json"))
    return [_process_one_handoff(path, receipts_dir) for path in handoffs if not _has_terminal_receipt(path, receipts_dir)]
```

`_process_one_handoff()` validates the exact schema, checks the current selected account display name equals `硅基进化`, starts one `wechat_draft` task, and writes exactly one terminal receipt. It never retries a terminal receipt or starts a second task for the same `article_id + package_sha256`.

- [ ] **Step 4: Add an explicit desktop opt-in**

Add a checkbox labelled `启用硅基进化自动保存草稿（不会发表）`, unchecked by default. The first enable dialog must state that the app will only save drafts, and that unknown prompts, scan verification, login loss and mismatched fields stop the task. The queue timer starts only after acceptance and stops when the checkbox is cleared or the application exits.

- [ ] **Step 5: Run UI and queue tests, then commit**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest -v test_wechat_draft_queue test_publish_page`

Expected: PASS. Commit: `feat: add opt-in wechat draft queue`.

### Task 5: 内容自动任务接线、回执消费和操作文档

**Files:**
- Modify: `tools/wechat_operator/prompts/daily_content_orchestrator.md`
- Modify: `docs/运营流程/日常运营工作流.md`
- Modify: `README.md`
- Test: `tools/wechat_operator/tests/test_draft_bridge.py`
- Test: `test_wechat_draft_queue.py`

**Interfaces:**
- Consumes: successful `daily-prepare` output and Task 4 receipt JSON.
- Produces: one CLI handoff invocation after a unique `delivered_unverified`, and one content-side `wechat_draft_readback_confirmed` event after a matching receipt.

- [ ] **Step 1: Write failing end-to-end contract test**

```python
def test_matching_queue_receipt_records_draft_evidence_without_publication_state(tmp_path):
    handoff = enqueue_wechat_draft(store, article_id, tmp_path / "inbox")
    receipt = confirmed_receipt_for(handoff, tmp_path / "receipts")
    outcome = record_wechat_draft_receipt(store, receipt, run_id="draft-readback:test")
    self.assertEqual(outcome["evidence"], "wechat_draft_readback_confirmed")
    self.assertEqual(store.get_article(article_id).state.value, "delivered_unverified")
```

- [ ] **Step 2: Run test and verify failure until the task-1 interface is wired**

Run: `tools/wechat_operator/.venv/bin/python -m pytest tools/wechat_operator/tests/test_draft_bridge.py -q`

Expected: FAIL until the daily orchestrator and receipt commands use the Task 1 interfaces.

- [ ] **Step 3: Wire the automation sequence**

After `daily-prepare` returns the unique `delivered_unverified` card, invoke `enqueue-wechat-draft` once. On later runs, consume any matching receipt before allocating the next day’s content. Missing, stopped, or mismatched receipts must be reported as draft-bridge state and must not block content production or cause a second handoff.

- [ ] **Step 4: Document precise operator states**

Update both READMEs to say: `draft_readback_confirmed` proves only a backend draft exists; it is neither `manually_published_user_confirmed` nor `manually_published`. Document the one required user action for scan verification: scan only in the one-click native dialog, then wait for the same task to resume or stop.

- [ ] **Step 5: Run cross-project checks and commit separately**

Run in content project: `tools/wechat_operator/.venv/bin/python -m pytest tools/wechat_operator/tests/test_draft_bridge.py tools/wechat_operator/tests/test_cli_draft_bridge.py -q`

Run in one-click integration worktree: `.venv/bin/python -m unittest -v test_silicon_evolution_draft_package test_wechat_draft_queue test_wechat_draft_executor test_wechat_publish_executor test_wechat_preflight test_publish_service`

Expected: PASS. Commit documentation and content-side code in their respective repositories; do not include existing ledger changes, generated reports, or unrelated untracked files.

### Task 6: 集成回归与首次真实草稿验收

**Files:**
- Modify: `SOURCE_OF_TRUTH.md`
- Modify: `QUALITY_GATES.md`
- Create: `docs/verification/wechat-draft-bridge-2026-08-25.md`

**Interfaces:**
- Consumes: passing Task 1–5 tests and a next-day `delivered_unverified` package.
- Produces: current evidence statement distinguishing local tests, desktop queue execution, backend draft readback and publication.

- [ ] **Step 1: Run full shared-core regression before live work**

Run: `.venv/bin/python -m unittest discover -p 'test_*.py'`

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python desktop_native_app.py --ui-test`

Run: `git diff --check`

Expected: all tests pass, UI test exits 0, and no whitespace errors.

- [ ] **Step 2: Perform the first real draft-only run**

Use the next normal V1.2 package after it has reached `delivered_unverified`. Enable the new desktop checkbox, verify the selected account label is `硅基进化`, then let the queue create exactly one draft. If a QR dialog appears, pause for the user scan in the native dialog; do not capture its contents.

- [ ] **Step 3: Read back and record only the evidence actually present**

Require the same title, cover and current run time in the backend draft list before producing `draft_readback_confirmed`. Save only the redacted receipt and verification outcome. If the list cannot prove the draft uniquely, write `draft_readback_ambiguous` and stop; do not retry or publish.

- [ ] **Step 4: Update the two authorities and commit**

`SOURCE_OF_TRUTH.md` must say whether the result is only local tests, desktop execution, or backend draft readback. `QUALITY_GATES.md` must preserve the rule that a draft is not publication. Commit only after the exact readback result is documented: `docs: record wechat draft bridge verification`.

## Plan Self-Review

- Spec coverage: Tasks 1–2 implement the frozen-package and handoff contract; Tasks 3–4 implement independent draft execution, opt-in queue, stop conditions and receipts; Task 5 consumes only matching receipts; Task 6 separates local, desktop and platform evidence.
- Placeholder scan: no unfinished markers, deferred implementation wording or unspecified test steps remain.
- Type consistency: both repositories use the same `article_id`, `package_sha256`, `draft_readback_confirmed`, `wechat_draft_only` and the exact receipt schemas defined above.
