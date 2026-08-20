# Xiaohongshu Data Contract Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Obtain value-free evidence for the Xiaohongshu account overview, content list, and content lifetime contracts so the production adapter can be planned without guessing endpoints or metric meanings.

**Architecture:** Extend the existing passive Playwright verifier with a separate ephemeral schema-review projection. Persistent reports remain aggressively sanitized; review output contains only bounded official path templates and key/type structure. Real execution remains a separate authorization gate and may legitimately finish with an incomplete contract.

**Tech Stack:** Python 3, `unittest`, Playwright async API, JSON, existing account storage state and report writer.

**Spec:** `docs/superpowers/specs/2026-08-20-xiaohongshu-data-monitoring-adapter-design.md`

## Global Constraints

- This plan changes only the verifier, its tests, and evidence documents. It does not register a Xiaohongshu collector or alter Data Monitoring UI.
- Permit only navigation to `https://creator.xiaohongshu.com` and passive observation of naturally emitted JSON.
- Forbid `fetch`, `requests`, request replay, DOM extraction, clicks, Cookie export, and every platform write action.
- Never persist response values, query strings, headers, cookies, HTML, profile data, note titles, or full content IDs.
- `KeyboardInterrupt` and `SystemExit` propagate only after all cleanup attempts.
- Completion requires `cleanup.closed is True` and built-in integer `cleanup.aliveResourceCount == 0`.
- A real probe needs explicit authorization in its execution turn; design approval is not authorization.
- If any required contract is missing, stop. Do not invent a parser or production mapping.
- Preserve untracked `.superpowers/brainstorm/` and `outputs/`.

## Scope split

The approved design contains two dependent deliverables. This plan completes contract evidence. A second production-adapter plan is written only after the exact endpoints, key types, metric scopes, pagination semantics, and cleanup result are known. Combining both now would force placeholders and violate the fail-closed design.

## File map

- Modify `tools/verify_xiaohongshu_data_contract.py`: ephemeral review projection, CLI boundary, controlled navigation evidence.
- Modify `test_xiaohongshu_data_contract_verifier.py`: deterministic security, CLI, navigation, budget, and cleanup tests.
- Update `docs/superpowers/reports/2026-08-20-xiaohongshu-data-contract-report.md`: value-free real evidence.
- Update local `.superpowers/sdd/2026-08-20-xiaohongshu-data-contract-discovery/`: probe and execution ledger.
- Create after complete evidence: `docs/superpowers/plans/2026-08-20-xiaohongshu-data-monitoring-adapter.md`.

---

### Task 1: Ephemeral schema-review trust boundary

**Files:**
- Modify: `tools/verify_xiaohongshu_data_contract.py`
- Test: `test_xiaohongshu_data_contract_verifier.py`

**Interfaces:**
- Consumes: sanitized shapes from `_async_response_shape()`.
- Produces: `_review_schema(report: object) -> dict[str, object]`; CLI `--execute --review-schema`.

- [ ] **Step 1: Write failing review-boundary tests**

Add exact tests proving safe static path/key names survive while IDs, query, values, sensitive names, subclasses, and custom containers do not:

```python
def test_schema_review_retains_static_names_and_templates_ids(self):
    source = {"responses": [{
        "url": ("https://creator.xiaohongshu.com/api/galaxy/creator/"
                "datacenter/note/67b196bf000000001d0368f8?token=secret"),
        "method": "GET", "status": 200,
        "contentType": "application/json",
        "keyPaths": ["data.note_list", "data.note_list[].note_id"],
        "fieldTypes": {"data.note_list": "list",
                       "data.note_list[].note_id": "str"},
        "listLengths": {"data.note_list": 1},
        "paginationKeys": ["data.total"],
        "sample": {"title": "private title", "cookie": "secret"},
    }]}
    review = verifier._review_schema(source)
    encoded = json.dumps(review, ensure_ascii=False)
    self.assertEqual(review["responses"][0]["path"],
                     "/api/galaxy/creator/datacenter/note/:id")
    self.assertIn("data.note_list[].note_id", encoded)
    for forbidden in ("67b196bf000000001d0368f8", "private title", "secret"):
        self.assertNotIn(forbidden, encoded)


def test_schema_review_rejects_sensitive_keys_and_non_builtin_containers(self):
    class UntrustedDict(dict):
        pass
    source = {"responses": [{
        "path": "/api/data",
        "keyPaths": ["data.cookie", "data.authorization", "data.safe_key"],
        "fieldTypes": UntrustedDict({"data.safe_key": "int"}),
    }]}
    encoded = json.dumps(verifier._review_schema(source), ensure_ascii=False)
    self.assertNotIn("cookie", encoded.lower())
    self.assertNotIn("authorization", encoded.lower())
    self.assertNotIn("safe_key", encoded)
```

- [ ] **Step 2: Run the two tests and verify RED**

```bash
../../.venv/bin/python -m unittest -v \
  test_xiaohongshu_data_contract_verifier.XiaohongshuDataContractVerifierTests.test_schema_review_retains_static_names_and_templates_ids \
  test_xiaohongshu_data_contract_verifier.XiaohongshuDataContractVerifierTests.test_schema_review_rejects_sensitive_keys_and_non_builtin_containers
```

Expected: both fail because `_review_schema` is absent.

- [ ] **Step 3: Implement minimal built-in-only projection**

Add these interfaces and constants:

```python
_REVIEW_SAFE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_REVIEW_ID_SEGMENT = re.compile(
    r"^(?:[0-9]+|[0-9a-f]{24}|[0-9a-f]{8}-[0-9a-f-]{27,})$", re.I)
_REVIEW_SENSITIVE_NAMES = frozenset({
    "authorization", "cookie", "cookies", "token", "ticket", "session",
    "sessionid", "password", "passwd", "secret", "phone", "mobile", "email",
})

def _review_path(value: object) -> str:
    if type(value) is not str:
        return ""
    if value.startswith("/") and "?" not in value and "#" not in value:
        raw_path = value
    else:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.hostname != _CREATOR_HOST:
            return ""
        raw_path = parsed.path
    output = []
    for segment in raw_path.split("/"):
        if not segment:
            continue
        lowered = segment.lower()
        if _REVIEW_ID_SEGMENT.fullmatch(lowered):
            output.append(":id")
        elif _REVIEW_SAFE_NAME.fullmatch(lowered) and lowered not in _REVIEW_SENSITIVE_NAMES:
            output.append(lowered)
        else:
            output.append(":segment")
    return "/" + "/".join(output)


def _review_key_path(value: object) -> str:
    if type(value) is not str or len(value) > 192:
        return ""
    parts = value.replace("[]", ".[]").split(".")
    rebuilt = []
    for part in parts:
        if part == "[]":
            if not rebuilt:
                return ""
            rebuilt[-1] += "[]"
        elif _REVIEW_SAFE_NAME.fullmatch(part) and part not in _REVIEW_SENSITIVE_NAMES:
            rebuilt.append(part)
        else:
            return ""
    return ".".join(rebuilt)


def _review_schema(report: object) -> dict[str, object]:
    if type(report) is not dict or type(report.get("responses")) is not list:
        return {"responses": []}
    reviewed = []
    for source in report["responses"][:_RESPONSE_LIMIT]:
        if type(source) is not dict:
            continue
        path = _review_path(source.get("url") or source.get("path"))
        if not path:
            continue
        row = {"path": path}
        # Rebuild each allowed structural field using exact built-in types only.
        # Drop empty/invalid members rather than coercing them.
        reviewed.append(row)
    return {"responses": reviewed}
```

Rebuild only `path`, `method`, `status`, `contentType`, `keyPaths`, `fieldTypes`, `listLengths`, and `paginationKeys`. Accept exact built-in containers/scalars only. Cap responses and nodes using existing verifier limits. Never call `str()`, `int()`, custom iteration, or object attributes.

- [ ] **Step 4: Run Step 2 again and verify GREEN**

Expected: the runner reports 2 tests and terminal status `OK`.

- [ ] **Step 5: Write the CLI isolation test**

```python
def test_review_cli_is_execute_only_and_persisted_report_stays_sanitized(self):
    payload = verifier.build_plan()
    payload.update({"mode": "execute", "status": "failed",
                    "errorCode": "xiaohongshu_contracts_unobserved",
                    "responses": [{"path": "/api/galaxy/creator/datacenter/list",
                                   "keyPaths": ["data.note_list"],
                                   "fieldTypes": {"data.note_list": "list"}}]})
    output = io.StringIO()
    with patch.object(verifier, "_execute", return_value=payload), \
         patch.object(verifier, "_write_report") as writer:
        code = verifier.main(["--execute", "--review-schema"], stdout=output)
    self.assertEqual(code, 1)
    self.assertIn("schemaReview", json.loads(output.getvalue()))
    writer.assert_not_called()
    invalid = io.StringIO()
    self.assertEqual(verifier.main(["--review-schema"], stdout=invalid), 2)
```

Accept only `--execute --review-schema` and `--execute --review-schema --report PATH`. `_write_report()` receives only `sanitize_probe_report(payload)`; append `schemaReview` only to the stdout object after the report write.

- [ ] **Step 6: Run CLI test and verifier module**

```bash
../../.venv/bin/python -m unittest -v \
  test_xiaohongshu_data_contract_verifier.XiaohongshuDataContractVerifierTests.test_review_cli_is_execute_only_and_persisted_report_stays_sanitized
../../.venv/bin/python -m unittest -v test_xiaohongshu_data_contract_verifier
```

Expected: named test passes; module ends `OK`.

- [ ] **Step 7: Commit**

```bash
git add tools/verify_xiaohongshu_data_contract.py test_xiaohongshu_data_contract_verifier.py
git commit -m "增加小红书合同临时结构审查"
```

---

### Task 2: Controlled passive navigation and cleanup evidence

**Files:**
- Modify: `tools/verify_xiaohongshu_data_contract.py`
- Test: `test_xiaohongshu_data_contract_verifier.py`

**Interfaces:**
- Consumes: Task 1 review projection and existing response listener.
- Produces: ephemeral `navigation: list[dict[str, str]]` with controlled phase and sanitized path only.

- [ ] **Step 1: Write a failing navigation test using existing fake Playwright objects**

```python
def test_review_probe_reports_navigation_and_closes_every_resource(self):
    response = FakeResponse(
        "https://creator.xiaohongshu.com/api/galaxy/creator/datacenter/overview",
        "application/json", {"data": {"views": 12}},
    )
    fake = FakePlaywright(responses=(response,))
    result = verifier._probe_with_browser(
        eligible_account(), playwright_factory=lambda: fake,
        monotonic=FakeClock(), utc_now=fixed_now,
    )
    review = verifier._review_schema(result)
    self.assertEqual(review["navigation"], [
        {"phase": "account_home", "path": "/creator/home"},
        {"phase": "data_analysis", "path": "/statistics/data-analysis"},
    ])
    self.assertTrue(result["cleanup"]["closed"])
    self.assertIs(type(result["cleanup"]["aliveResourceCount"]), int)
    self.assertEqual(result["cleanup"]["aliveResourceCount"], 0)
    self.assertEqual(fake.page.fetch_calls, 0)
    self.assertEqual(fake.page.click_calls, 0)
```

- [ ] **Step 2: Run the named test and verify RED**

```bash
../../.venv/bin/python -m unittest -v \
  test_xiaohongshu_data_contract_verifier.XiaohongshuDataContractVerifierTests.test_review_probe_reports_navigation_and_closes_every_resource
```

Expected: missing controlled navigation evidence.

- [ ] **Step 3: Record navigation only after successful bounded goto**

Implement:

```python
navigation: list[dict[str, str]] = []

async def navigate(phase: str, url: str) -> None:
    response = await _await_with_deadline(
        page.goto(url, wait_until="domcontentloaded",
                  timeout=_NETWORK_QUIET_TIMEOUT_MS),
        deadline=work_deadline, monotonic=monotonic)
    error_code = _navigation_error_code(response)
    if error_code is not None:
        raise ProbeFailure(error_code)
    navigation.append({"phase": phase, "path": _review_path(url)})
    await _await_with_deadline(page.wait_for_timeout(_PASSIVE_CAPTURE_WAIT_MS),
                               deadline=work_deadline, monotonic=monotonic)
```

Use phases `account_home`, `data_analysis`, and only when a strictly validated transient note ID exists, `content_lifetime`. Add navigation only to the in-memory result and review projection, not the persistent sanitizer.

- [ ] **Step 4: Run navigation and cleanup tests**

```bash
../../.venv/bin/python -m unittest -v \
  test_xiaohongshu_data_contract_verifier.XiaohongshuDataContractVerifierTests.test_review_probe_reports_navigation_and_closes_every_resource \
  test_xiaohongshu_data_contract_verifier.XiaohongshuDataContractVerifierTests.test_probe_closes_all_resources_on_timeout_and_base_exception \
  test_xiaohongshu_data_contract_verifier.XiaohongshuDataContractVerifierTests.test_probe_total_timeout_reports_exact_cleanup_failure_count
```

Expected: the runner reports 3 tests and terminal status `OK`.

- [ ] **Step 5: Prove review mode reuses response identity/body budgets**

Add this test using the existing `CountingResponse` and `FakePlaywright`:

```python
def test_review_mode_keeps_response_identity_and_byte_budgets(self):
    response = CountingResponse(
        "https://creator.xiaohongshu.com/api/galaxy/creator/datacenter/overview",
        "application/json", {"data": {"views": 1}},
    )
    fake = FakePlaywright(responses=(response, response))
    result = verifier._probe_with_browser(
        eligible_account(), playwright_factory=lambda: fake,
        monotonic=FakeClock(), utc_now=fixed_now,
    )
    review = verifier._review_schema(result)
    self.assertEqual(len(review["responses"]), 1)
    self.assertEqual(response.instance_json_calls, 1)
```

Do not create a second response-retention path for review mode.

- [ ] **Step 6: Run that named test and verifier module once**

```bash
../../.venv/bin/python -m unittest -v \
  test_xiaohongshu_data_contract_verifier.XiaohongshuDataContractVerifierTests.test_review_mode_keeps_response_identity_and_byte_budgets
../../.venv/bin/python -m unittest -v test_xiaohongshu_data_contract_verifier
```

Expected: both commands end `OK`.

- [ ] **Step 7: Commit**

```bash
git add tools/verify_xiaohongshu_data_contract.py test_xiaohongshu_data_contract_verifier.py
git commit -m "约束小红书合同导航审查边界"
```

---

### Task 3: Offline safety checkpoint

**Files:**
- Verify: `tools/verify_xiaohongshu_data_contract.py`
- Verify: `test_xiaohongshu_data_contract_verifier.py`
- Create local: `.superpowers/sdd/2026-08-20-xiaohongshu-data-contract-discovery/contract-completion-report.md`

**Interfaces:**
- Consumes: Tasks 1–2.
- Produces: offline-ready evidence; no real account or browser action.

- [ ] **Step 1: Run the verifier module once**

```bash
../../.venv/bin/python -m unittest -v test_xiaohongshu_data_contract_verifier
```

Expected: all tests `OK`. On failure, stop, fix only the first failure with a named test, then rerun this module once.

- [ ] **Step 2: Compile**

```bash
../../.venv/bin/python -m py_compile tools/verify_xiaohongshu_data_contract.py test_xiaohongshu_data_contract_verifier.py
```

Expected: exit 0.

- [ ] **Step 3: Inspect forbidden-action matches**

```bash
rg -n "requests\.|httpx\.|urllib\.request|page\.evaluate|locator\(|inner_text|page\.content|\.click\(|Cookie|Authorization" tools/verify_xiaohongshu_data_contract.py
```

Expected: no request replay, DOM extraction, click, or secret output in the execution path.

- [ ] **Step 4: Prove default mode remains zero-action**

```bash
../../.venv/bin/python tools/verify_xiaohongshu_data_contract.py
```

Expected: one plan JSON, exit 0, no account read/browser/report write.

- [ ] **Step 5: Check scope and formatting**

```bash
git diff --check
git status --short
```

Expected: only intended changes plus preserved user-owned untracked directories.

- [ ] **Step 6: Write actual offline evidence**

Write `contract-completion-report.md` with actual test count, exit codes, zero-action result, sensitive scan result, and `下一安全停点：等待本次真实只读 probe 明确授权`. Do not estimate counts.

- [ ] **Step 7: Commit only if repository policy tracks the report**

If `.superpowers/sdd` is intentionally ignored, leave it local and report its path. Do not force-add ignored evidence.

---

### Task 4: One explicitly authorized real read-only probe

**Files:**
- Update: `docs/superpowers/reports/2026-08-20-xiaohongshu-data-contract-report.md`
- Update local: `.superpowers/sdd/2026-08-20-xiaohongshu-data-contract-discovery/probe-report.json`
- Update local: `.superpowers/sdd/2026-08-20-xiaohongshu-data-contract-discovery/contract-completion-report.md`

**Interfaces:**
- Consumes: exactly one active Xiaohongshu account and Tasks 1–3 passing.
- Produces: persistent sanitized evidence plus ephemeral stdout schema review; no production code.

- [ ] **Step 1: Stop for fresh authorization**

Tell the user exactly: `将读取唯一已登录的小红书账号，启动一次无头短会话，只导航官方数据页并被动监听 JSON；不会点击、上传、发布、重放请求或保存 Cookie。结束后严格关闭全部资源。`

- [ ] **Step 2: Execute exactly once after authorization**

```bash
../../.venv/bin/python tools/verify_xiaohongshu_data_contract.py \
  --execute --review-schema \
  --report .superpowers/sdd/2026-08-20-xiaohongshu-data-contract-discovery/probe-report.json
```

Capture stdout and exit code. Do not automatically rerun an incomplete result. Never print account dictionaries, IDs, profile names, or session paths.

- [ ] **Step 3: Verify cleanup before interpreting evidence**

Assert exact built-in types and values:

```python
report["cleanup"]["closed"] is True
type(report["cleanup"]["aliveResourceCount"]) is int
report["cleanup"]["aliveResourceCount"] == 0
```

Otherwise record `xiaohongshu_probe_cleanup_incomplete` and stop.

- [ ] **Step 4: Classify each required phase**

Classify `account_overview`, `content_list`, and `content_lifetime` as `observed`, `missing`, or `ambiguous`. `observed` requires a stable endpoint template, method/status, key paths/types, human-confirmed metric meaning/time scope, and no conflicting candidate. Do not copy values.

- [ ] **Step 5: Update tracked evidence report**

For each phase write only status, endpoint template, method, field path, built-in type, metric scope, pagination field names, and coverage semantics. State `adapter_contract_complete` only when all three are observed and cleanup is zero; otherwise state `adapter_contract_incomplete` with exact missing evidence.

- [ ] **Step 6: Sensitive scan and commit**

```bash
rg -n "cookie|authorization|token|secret|noteId=|[0-9a-fA-F]{24}|https://[^ ]+\?" docs/superpowers/reports/2026-08-20-xiaohongshu-data-contract-report.md
git diff --check
git add docs/superpowers/reports/2026-08-20-xiaohongshu-data-contract-report.md
git commit -m "记录小红书数据合同真实证据"
```

Expected: no sensitive/dynamic identifier match before commit. Do not commit raw stdout or the local probe report unless its sanitizer and repository policy explicitly permit it.

---

### Task 5: Production-adapter planning gate

**Files:**
- Read: `docs/superpowers/specs/2026-08-20-xiaohongshu-data-monitoring-adapter-design.md`
- Read: `docs/superpowers/reports/2026-08-20-xiaohongshu-data-contract-report.md`
- Create only if complete: `docs/superpowers/plans/2026-08-20-xiaohongshu-data-monitoring-adapter.md`

**Interfaces:**
- Consumes: Task 4 phase decisions and cleanup evidence.
- Produces: a safe stop or an exact second plan.

- [ ] **Step 1: Enforce the exact gate**

```python
required = {"account_overview", "content_list", "content_lifetime"}
ready = observed_phases == required and cleanup_closed and alive_count == 0
```

Partial success, inferred meanings, or unverified pagination do not pass.

- [ ] **Step 2A: Stop safely if false**

Report exact missing phase/evidence and confirm no production collector, registry, persistence, or UI code changed. Do not create an adapter plan with placeholders.

- [ ] **Step 2B: Invoke writing-plans again if true**

The second plan must contain literal reviewed endpoint templates, exact parser key paths/types, metric mappings/scopes, pagination termination, cover-domain evidence, unified collector errors, registry changes, UI account switching, atomic persistence, cleanup tests, real acceptance, and rollback.

It must define concrete tasks for:

```text
app_core/platform_data_models.py
app_core/platform_data_collectors.py
app_core/platform_data_sync.py
app_core/xiaohongshu_data_contracts.py
app_core/xiaohongshu_data_collector.py
app_core/platform_data_service.py
ui/data_monitor_page.py
test_xiaohongshu_data_contracts.py
test_xiaohongshu_data_collector.py
test_platform_data_sync.py
test_platform_data_service.py
test_data_monitor_page.py
```

- [ ] **Step 3: Self-review and commit the second plan**

Check spec coverage, placeholder absence, signature consistency, exact commands, unavailable-not-zero semantics, and real acceptance gate. Commit it separately before offering implementation.

---

## Completion evidence

This plan reaches contract-discovery delivery closure when offline gates pass, one authorized probe has a retained exit code, cleanup is strictly zero, evidence is value-free, and the report truthfully states complete or incomplete. It does not by itself reach production-adapter delivery or runtime closure.
