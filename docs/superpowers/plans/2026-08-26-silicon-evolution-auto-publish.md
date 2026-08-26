# Silicon Evolution Auto Publish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically preflight and formally submit a verified V1.2 frozen WeChat article for project `silicon-evolution` through local OneClick, then store evidence-only receipts.

**Architecture:** OneClick gets a V1.2-specific loader and a dedicated local route; it never coerces the frozen package into `oneclick-content/v1`. The content operator hands off article ID, package path, and package hash only, then consumes a hash-matched receipt. OneClick remains the only owner of sessions, account mapping, browser execution, and one-use authorization.

**Tech Stack:** Python 3, SQLite, unittest, OneClick local stdio/MCP process, existing WeChat Playwright executor.

**Spec:** `docs/superpowers/specs/2026-08-26-silicon-evolution-auto-publish-design.md`

## Global Constraints

- Preserve every V1.2 package byte; `publish_allowed=false` remains unchanged.
- Bind automatic publication only to `silicon-evolution` and one verified `微信公众号` account.
- Always send `wechatGroupNotification=false` and `enableTimer=false`.
- Stop with no retry on QR code, risk control, unknown dialog, account/hash/field mismatch, duplicate article, missing AI declaration, or ambiguous result.
- Never persist cookies, QR images, tokens, credentials, full article text, or HTML in profiles, handoffs, receipts, or logs.
- Task acceptance, final-button click, platform receipt, public URL, and performance data remain different evidence layers.

---

### Task 1: Verify a V1.2 package for direct publication

**Files:**
- Create: `app_core/silicon_evolution_publish_package.py`
- Create: `test_silicon_evolution_publish_package.py`
- Reuse: `app_core/silicon_evolution_draft_package.py`

**Interfaces:**
- `FrozenWechatPublishPackage(article_id, package_sha256, title, digest, content_html, cover_path, body_image_paths, ai_disclosure)`.
- `load_frozen_wechat_publish_package(package_dir: Path, *, expected_sha256: str)`.
- `FrozenWechatPublishPackageError` for every contract failure.

- [ ] **Step 1: Write the failing test**
  - Add `test_loads_hash_verified_release_with_explicit_ai_image_declaration` to call `load_frozen_wechat_publish_package(package, expected_sha256=digest)` and assert article ID `WX-20260826-001` plus the first body image.
  - Add `test_rejects_missing_quality_pass_or_ai_declaration` and expect `FrozenWechatPublishPackageError` containing `AI`.
- [ ] **Step 2: Verify RED**
  - Run `.venv/bin/python -m unittest -v test_silicon_evolution_publish_package`.
  - Expect module import failure because the direct loader does not exist.
- [ ] **Step 3: Write minimal implementation**
  - Call `load_frozen_wechat_draft_package(package_dir, expected_sha256=expected_sha256)` first.
  - Verify `quality-receipt.json` and `publish-disclosure.json` against the existing `release-manifest.json` file records.
  - Require a passing quality receipt and exact AI image paths matching all `body_image_paths`; return the new dataclass.
  - Do not provide a fallback for existing packages missing `publish-disclosure.json`.
- [ ] **Step 4: Verify GREEN**
  - Run `.venv/bin/python -m unittest -v test_silicon_evolution_publish_package test_silicon_evolution_draft_package`.
  - Expect both suites to pass.
- [ ] **Step 5: Commit**
  - Run `git add app_core/silicon_evolution_publish_package.py test_silicon_evolution_publish_package.py`.
  - Run `git commit -m "feat: verify silicon evolution publish packages"`.

### Task 2: Add OneClick’s account-bound automatic route

**Files:**
- Create: `app_core/silicon_evolution_auto_publish.py`
- Modify: `app_core/controlled_publish.py`
- Modify: `app_core/oneclick_mcp_server.py`
- Create: `test_silicon_evolution_auto_publish.py`
- Modify: `test_controlled_publish.py`

**Interfaces:**
- `AutoPublishProfile(project_id, account_id, account_display_name, enabled)`.
- `build_silicon_evolution_payload(package, profile, account_id, mode) -> dict[str, Any]`.
- MCP commands `oneclick_preflight_silicon_evolution_release` and `oneclick_auto_publish_silicon_evolution_release`.
- Receipt keys: `articleId`, `packageSha256`, `accountId`, `phase`, `status`, `platformPostId`, `postUrl`, `occurredAt`, and `errorCode`.

- [ ] **Step 1: Write the failing test**
  - Add `test_auto_payload_disables_notification_and_timer`: build formal payload and assert `payload["wechatGroupNotification"] is False` and `payload["enableTimer"] is False`.
  - Add `test_formal_requires_matching_successful_preflight_and_unconsumed_grant`: call the formal route without a matched preflight and expect `SiliconEvolutionAutoPublishError` containing `预检`.
- [ ] **Step 2: Verify RED**
  - Run `.venv/bin/python -m unittest -v test_silicon_evolution_auto_publish test_controlled_publish`.
  - Expect a missing module or route method.
- [ ] **Step 3: Write minimal implementation**
  - Verify project ID, enabled profile, account ID, platform type 10, and account display name before creating a payload.
  - Build a payload with `type=10`, `contentType="article"`, frozen HTML description, cover/body image paths, `wechatGroupNotification=False`, `enableTimer=False`, exact AI disclosure, article ID, and package hash.
  - Create and consume a ten-minute one-use grant internally only after exactly one same-hash preflight reaches a successful terminal state; never return this grant to the caller.
  - Normalize task output to only the stated receipt keys and omit absent URL or platform ID values.
- [ ] **Step 4: Verify GREEN**
  - Run `.venv/bin/python -m unittest -v test_silicon_evolution_auto_publish test_controlled_publish test_controlled_publish_resilience test_content_project_gateway`.
  - Expect all suites to pass; duplicate and ambiguous calls must return stable errors with no retry.
- [ ] **Step 5: Commit**
  - Run `git add app_core/silicon_evolution_auto_publish.py app_core/controlled_publish.py app_core/oneclick_mcp_server.py test_silicon_evolution_auto_publish.py test_controlled_publish.py`.
  - Run `git commit -m "feat: add silicon evolution auto publish route"`.

### Task 3: Require final executor option and account readback

**Files:**
- Modify: `app_core/wechat_publish_executor.py`
- Modify: `app_core/wechat_publish_policy.py`
- Modify: `test_wechat_publish_executor.py`
- Modify: `test_wechat_publish_policy.py`

**Interfaces:**
- `validate_silicon_evolution_auto_publish_readback(payload, page_state) -> dict[str, Any]`.
- It requires article ID, frozen hash, account, exact fields, `groupNotification=false`, and `scheduledPublish=false` before a final click.

- [ ] **Step 1: Write the failing test**
  - Add `test_auto_publish_stops_when_group_notification_is_on` with a readable `groupNotification.enabled=True` state and assert `allowed is False` plus error code `wechat_publish_options_mismatch`.
- [ ] **Step 2: Verify RED**
  - Run `.venv/bin/python -m unittest -v test_wechat_publish_executor test_wechat_publish_policy`.
  - Expect a missing validator.
- [ ] **Step 3: Write minimal implementation**
  - Invoke the current option readback with `wechatGroupNotification=false` and `enableTimer=false`.
  - Reject before a final click if title, account, cover, HTML/image field, article ID, hash, or dialog set differs.
  - Return a stable result receipt only from an actual platform result; never equate it to public access.
- [ ] **Step 4: Verify GREEN and commit**
  - Run `.venv/bin/python -m unittest -v test_wechat_publish_executor test_wechat_publish_policy test_publish_service`.
  - Run `git add app_core/wechat_publish_executor.py app_core/wechat_publish_policy.py test_wechat_publish_executor.py test_wechat_publish_policy.py`.
  - Run `git commit -m "feat: guard silicon evolution auto publish readback"`.

### Task 4: Add content-operator local handoff and evidence ledger

**Files:**
- Create: `/Users/andy/Documents/AI/codex/Projects/内容策略/硅基进化公众号/tools/wechat_operator/src/wechat_operator/adapters/oneclick_local.py`
- Modify: `/Users/andy/Documents/AI/codex/Projects/内容策略/硅基进化公众号/tools/wechat_operator/src/wechat_operator/models.py`
- Modify: `/Users/andy/Documents/AI/codex/Projects/内容策略/硅基进化公众号/tools/wechat_operator/src/wechat_operator/store.py`
- Create: `/Users/andy/Documents/AI/codex/Projects/内容策略/硅基进化公众号/tools/wechat_operator/tests/test_oneclick_local.py`
- Modify: `/Users/andy/Documents/AI/codex/Projects/内容策略/硅基进化公众号/tools/wechat_operator/tests/test_store.py`

**Interfaces:**
- `OneClickLocalClient.preflight_release(article_id, package_path, package_sha256)` and `.auto_publish_release(...)`.
- `OneClickReceipt(article_id, package_sha256, phase, status, account_id, occurred_at, platform_post_id, post_url, error_code)`.
- `OperatorStore.record_oneclick_receipt(receipt, run_id)` writes a unique `(article_id, package_sha256, phase)` row and one evidence event.

- [ ] **Step 1: Write the failing test**
  - Add `test_client_rejects_receipt_with_a_different_frozen_hash`, return `packageSha256="f" * 64` from an injected invocation, and expect `OneClickLocalError` containing `哈希`.
  - Add `test_store_records_submission_without_marking_article_public`, record a receipt, and assert state stays `ArticleState.RELEASE_READY`.
- [ ] **Step 2: Verify RED**
  - Run `uv run python -m unittest -v tests.test_oneclick_local tests.test_store`.
  - Expect a missing adapter and store method.
- [ ] **Step 3: Write minimal implementation**
  - The client accepts only local stdio/MCP invocation, JSON results, exact receipt keys, and matching article/hash; reject HTTP URL, bearer token, cookie, or non-JSON input.
  - Add a foreign-keyed SQLite receipt table with exact SHA-256 checks and unique `(article_id, package_sha256, phase)`; include no HTML or credential column.
- [ ] **Step 4: Verify GREEN and commit**
  - Run `uv run python -m unittest -v tests.test_oneclick_local tests.test_store tests.test_transitions tests.test_secret_redaction`.
  - Run `git add src/wechat_operator/adapters/oneclick_local.py src/wechat_operator/models.py src/wechat_operator/store.py tests/test_oneclick_local.py tests/test_store.py`.
  - Run `git commit -m "feat: record local oneclick publish receipts"`.

### Task 5: Gate daily invocation and complete staged verification

**Files:**
- Modify: `/Users/andy/Documents/AI/codex/Projects/内容策略/硅基进化公众号/tools/wechat_operator/src/wechat_operator/daily.py`
- Modify: `/Users/andy/Documents/AI/codex/Projects/内容策略/硅基进化公众号/tools/wechat_operator/src/wechat_operator/cli.py`
- Modify: `/Users/andy/Documents/AI/codex/Projects/内容策略/硅基进化公众号/tools/wechat_operator/tests/test_daily.py`
- Modify: `/Users/andy/Documents/AI/codex/Projects/内容策略/硅基进化公众号/tools/wechat_operator/tests/test_cli.py`

**Interfaces:**
- CLI command `oneclick-auto-publish --article-id ID --run-id ID --json`.
- It only accepts `release_ready` with an unchanged registered hash and returns exit code `3` for stopped, failed, or ambiguous platform work.

- [ ] **Step 1: Write the failing test**
  - Add `test_auto_publish_refuses_delivered_or_changed_package`: transition `A001` to `DELIVERED_UNVERIFIED`, then expect `DailyInputError` containing `release_ready`.
  - Add `test_cli_returns_three_for_ambiguous_oneclick_result` and assert `main([...]) == 3`.
- [ ] **Step 2: Verify RED**
  - Run `uv run python -m unittest -v tests.test_daily tests.test_cli`.
  - Expect the command and helper to be missing.
- [ ] **Step 3: Write minimal implementation**
  - Run `release_package_sha256()` immediately before preflight and again immediately before formal submission.
  - Persist successful preflight evidence, make one formal call only, and record a submission event without promoting a public state unless a separately verified URL exists.
- [ ] **Step 4: Run full local checks**
  - Run `uv run python -m unittest discover -s tests -p 'test_*.py'` in `tools/wechat_operator`.
  - Run `.venv/bin/python -m unittest discover -p 'test_*.py'` in the OneClick integration worktree.
  - Run `QT_QPA_PLATFORM=offscreen .venv/bin/python desktop_native_app.py --ui-test` in that same OneClick worktree.
  - Run `git diff --check` in both worktrees.
- [ ] **Step 5: Commit and stage real validation**
  - Run `git add src/wechat_operator/daily.py src/wechat_operator/cli.py tests/test_daily.py tests/test_cli.py`.
  - Run `git commit -m "feat: gate silicon evolution auto publish"`.
  - Run exactly one real preflight and read back every field before updating `automation-3`.
  - Use a new frozen article, never `WX-20260826-001`, for the first formal submission and backend readback.
