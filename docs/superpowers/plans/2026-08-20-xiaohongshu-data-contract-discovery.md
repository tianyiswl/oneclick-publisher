# 小红书数据合同只读探测 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不猜测端点、不保存账号隐私且不执行平台写操作的前提下，取得小红书账号趋势、作品列表和单作品累计指标的真实官方请求与字段合同，为下一份生产适配器实施计划提供事实输入。

**Architecture:** 新增默认零平台动作的开发者验证器；显式 `--execute` 时只允许选择唯一正常小红书账号，使用独立短生命周期 Playwright 上下文打开官方创作服务平台，并被动监听页面自身产生的官方 JSON。探测结果仅保存 URL 路径、请求方法、结构键、字段类型、分页结构和资源清理状态，不保存 Cookie、查询串、响应值、作品内容或账号身份。

**Tech Stack:** Python 3.12、Playwright async API、SQLite 只读查询、`unittest`、JSON 白名单投影。

**Spec:** `docs/superpowers/specs/2026-08-20-domestic-platform-data-monitoring-design.md`

## Global Constraints

- 本计划只处理小红书数据合同探测，不实现生产适配器，不修改数据监测页面。
- 默认模式不得读取账号、数据库或会话文件，不得启动浏览器，不得访问网络。
- 显式 `--execute` 只允许平台类型 `1`，且必须恰好存在一个 `status=1` 的小红书账号；否则固定失败为 `xiaohongshu_account_selection_required`。
- 浏览器只打开 `https://creator.xiaohongshu.com/creator/home`，只被动监听页面自身请求，不点击、不填写、不调用页面内 `fetch`、不上传、不发布。
- 只接纳 HTTPS 且主机精确为 `creator.xiaohongshu.com` 的 JSON 响应；查询串、片段和响应值不得进入报告。
- 报告不得包含 Cookie、Token、Authorization、Set-Cookie、手机号、昵称、作品标题、作品正文、作品 ID、封面 URL、响应原文、HTML 或 DOM。
- 验证码、登录页、安全验证、超时和资源关闭不完整必须固定失败并停止。
- 进程控制异常 `KeyboardInterrupt`、`SystemExit` 保持传播，但 `finally` 仍必须尝试关闭 page、context、browser 和 Playwright。
- 实际平台探测属于只读运行验收；离线测试不能冒充探测成功。

---

### Task 1: 零动作验证器外壳与报告净化

**Files:**
- Create: `tools/verify_xiaohongshu_data_contract.py`
- Create: `test_xiaohongshu_data_contract_verifier.py`

**Interfaces:**
- Produces: `build_plan() -> dict[str, object]`
- Produces: `sanitize_probe_report(value: object) -> dict[str, object]`
- Produces: `main(argv: list[str] | None = None, *, stdout=sys.stdout) -> int`
- Report schema: `schemaVersion`, `platformType`, `mode`, `phases`, `status`, `errorCode`, `observedAt`, `responses`, `cleanup`

- [ ] **Step 1: Write failing default-mode and sanitizer tests**

```python
class XiaohongshuDataContractVerifierTests(unittest.TestCase):
    def test_default_mode_is_zero_action_plan(self):
        with patch("tools.verify_xiaohongshu_data_contract._execute") as execute:
            output = io.StringIO()
            code = verifier.main([], stdout=output)
        self.assertEqual(code, 0)
        execute.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["mode"], "plan")
        self.assertEqual(payload["phases"], [
            "account_overview", "content_list", "content_lifetime"
        ])

    def test_report_sanitizer_drops_values_and_sensitive_keys(self):
        report = verifier.sanitize_probe_report({
            "responses": [{
                "url": "https://creator.xiaohongshu.com/api/data?token=secret",
                "headers": {"Cookie": "secret"},
                "keys": ["data", "note_id", "title"],
                "sample": {"title": "private work"},
            }]
        })
        encoded = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("secret", encoded)
        self.assertNotIn("private work", encoded)
        self.assertEqual(report["responses"][0]["path"], "/api/data")
```

- [ ] **Step 2: Run the two tests and verify RED**

Run:

```bash
.venv/bin/python -m unittest \
  test_xiaohongshu_data_contract_verifier.XiaohongshuDataContractVerifierTests.test_default_mode_is_zero_action_plan \
  test_xiaohongshu_data_contract_verifier.XiaohongshuDataContractVerifierTests.test_report_sanitizer_drops_values_and_sensitive_keys -v
```

Expected: `ModuleNotFoundError` for `tools.verify_xiaohongshu_data_contract`.

- [ ] **Step 3: Implement the minimal plan and whitelist sanitizer**

```python
_ALLOWED_REPORT_KEYS = frozenset({
    "schemaVersion", "platformType", "mode", "phases", "status",
    "errorCode", "observedAt", "responses", "cleanup",
})
_ALLOWED_RESPONSE_KEYS = frozenset({
    "method", "path", "status", "contentType", "keyPaths",
    "fieldTypes", "listLengths", "paginationKeys",
})

def build_plan() -> dict[str, object]:
    return {
        "schemaVersion": "xiaohongshu-data-contract-probe/v1",
        "platformType": 1,
        "mode": "plan",
        "phases": ["account_overview", "content_list", "content_lifetime"],
        "status": "planned",
        "errorCode": "",
        "observedAt": "",
        "responses": [],
        "cleanup": {"closed": True, "aliveResourceCount": 0},
    }
```

`sanitize_probe_report()` must rebuild exact built-in dictionaries and lists, use `urlsplit()` to retain only `.path`, reject non-built-in strings and integers, cap `responses` at 100, `keyPaths` at 300 per response and text at 120 characters, and replace any invalid value with a fixed safe default. It must never call `str()` on untrusted objects.

- [ ] **Step 4: Run the Task 1 test class**

Run: `.venv/bin/python -m unittest test_xiaohongshu_data_contract_verifier.XiaohongshuDataContractVerifierTests -v`

Expected: all Task 1 tests pass; no database or browser import occurs in default mode.

- [ ] **Step 5: Commit Task 1**

```bash
git add tools/verify_xiaohongshu_data_contract.py test_xiaohongshu_data_contract_verifier.py
git commit -m "增加小红书数据合同零动作验证器"
```

### Task 2: 唯一账号选择与只读启动门禁

**Files:**
- Modify: `tools/verify_xiaohongshu_data_contract.py`
- Modify: `test_xiaohongshu_data_contract_verifier.py`

**Interfaces:**
- Produces: `_select_single_eligible_account() -> dict[str, object]`
- Produces: `_execute(*, browser_factory, utc_now) -> dict[str, object]`
- Produces: `ProbeFailure(error_code: str)` whose public message is the exact fixed code
- Consumes: `app_core.account_service.list_accounts() -> list[dict]`

- [ ] **Step 1: Write failing account-boundary tests**

```python
def test_execute_requires_exactly_one_normal_xiaohongshu_account(self):
    cases = (
        [],
        [{"id": 1, "type": 1, "status": 0}],
        [{"id": 1, "type": 1, "status": 1},
         {"id": 2, "type": 1, "status": 1}],
        [{"id": 1, "type": True, "status": 1}],
    )
    for accounts in cases:
        with self.subTest(accounts=accounts), patch(
            "app_core.account_service.list_accounts", return_value=accounts
        ):
            with self.assertRaisesRegex(
                verifier.ProbeFailure,
                "^xiaohongshu_account_selection_required$",
            ):
                verifier._select_single_eligible_account()

def test_execute_accepts_only_platform_type_one_and_builtin_ids(self):
    account = {"id": 7, "type": 1, "status": 1, "filePath": "state.json"}
    with patch("app_core.account_service.list_accounts", return_value=[account]):
        selected = verifier._select_single_eligible_account()
    self.assertEqual(selected, account)
```

- [ ] **Step 2: Run the account-boundary tests and verify RED**

Run: `.venv/bin/python -m unittest test_xiaohongshu_data_contract_verifier.XiaohongshuDataContractVerifierTests.test_execute_requires_exactly_one_normal_xiaohongshu_account test_xiaohongshu_data_contract_verifier.XiaohongshuDataContractVerifierTests.test_execute_accepts_only_platform_type_one_and_builtin_ids -v`

Expected: fail because selection and `ProbeFailure` do not exist.

- [ ] **Step 3: Implement strict selection and lazy imports**

`main()` must import `app_core.account_service`, account paths and Playwright only inside the `--execute` branch. `_select_single_eligible_account()` accepts only exact built-in `int` values for `id/type/status`, `id > 0`, `type == 1`, `status == 1`, and exactly one match. The selected public account dictionary may retain only `id`, `type`, `status`, and `filePath`; it must not be written to the report.

- [ ] **Step 4: Run Task 2 tests and the independent default CLI**

Run:

```bash
.venv/bin/python -m unittest test_xiaohongshu_data_contract_verifier.XiaohongshuDataContractVerifierTests -v
.venv/bin/python tools/verify_xiaohongshu_data_contract.py
```

Expected: tests pass; CLI prints only the plan JSON and does not touch `demo-runtime`.

- [ ] **Step 5: Commit Task 2**

```bash
git add tools/verify_xiaohongshu_data_contract.py test_xiaohongshu_data_contract_verifier.py
git commit -m "收紧小红书数据探测账号边界"
```

### Task 3: 被动响应结构采集与严格资源关闭

**Files:**
- Modify: `tools/verify_xiaohongshu_data_contract.py`
- Modify: `test_xiaohongshu_data_contract_verifier.py`

**Interfaces:**
- Produces: `_probe_with_browser(account: dict, *, playwright_factory, monotonic, utc_now) -> dict[str, object]`
- Produces: `_response_shape(response) -> dict[str, object] | None`
- Consumes: saved Playwright storage-state path from the selected account

- [ ] **Step 1: Write failing passive-capture tests**

```python
def test_probe_only_accepts_creator_https_json_and_never_fetches(self):
    fake = FakePlaywright(responses=[
        FakeResponse("https://evil.example/data", "application/json", {"x": 1}),
        FakeResponse("http://creator.xiaohongshu.com/api", "application/json", {"x": 1}),
        FakeResponse("https://creator.xiaohongshu.com/api/data?q=secret", "text/html", "<html>"),
        FakeResponse("https://creator.xiaohongshu.com/api/data?q=secret", "application/json", {
            "data": {"items": [{"note_id": "private", "view_count": 3}]},
            "cursor": "private-cursor",
        }),
    ])
    result = verifier._probe_with_browser(
        eligible_account(), playwright_factory=lambda: fake,
        monotonic=FakeClock(), utc_now=fixed_now,
    )
    self.assertEqual(len(result["responses"]), 1)
    self.assertEqual(result["responses"][0]["path"], "/api/data")
    self.assertIn("data.items[].view_count", result["responses"][0]["keyPaths"])
    self.assertNotIn("private", json.dumps(result))
    self.assertEqual(fake.page.fetch_calls, 0)
    self.assertEqual(fake.page.click_calls, 0)

def test_probe_closes_all_resources_on_timeout_and_base_exception(self):
    for failure in (TimeoutError("secret"), KeyboardInterrupt(), SystemExit()):
        fake = FakePlaywright(failure=failure)
        with self.subTest(failure=type(failure).__name__):
            if isinstance(failure, (KeyboardInterrupt, SystemExit)):
                with self.assertRaises(type(failure)):
                    verifier._probe_with_browser(
                        eligible_account(), playwright_factory=lambda: fake,
                        monotonic=FakeClock(), utc_now=fixed_now,
                    )
            else:
                result = verifier._probe_with_browser(
                    eligible_account(), playwright_factory=lambda: fake,
                    monotonic=FakeClock(), utc_now=fixed_now,
                )
                self.assertEqual(result["errorCode"], "xiaohongshu_probe_timeout")
            self.assertEqual(fake.close_order, ["page", "context", "browser", "playwright"])
```

- [ ] **Step 2: Run the passive-capture tests and verify RED**

Run the two named tests with `-v`.

Expected: fail because browser probe and response-shape functions do not exist.

- [ ] **Step 3: Implement passive capture**

The probe must:

1. validate and load the saved storage state without exposing its path;
2. create one Playwright instance, one bundled Chromium browser, one context and one page;
3. register a `response` listener before navigation;
4. navigate only to `https://creator.xiaohongshu.com/creator/home`;
5. wait at most 30 seconds for network quiet and at most 60 seconds total;
6. inspect only HTTPS `creator.xiaohongshu.com` responses whose normalized content type is `application/json`;
7. rebuild structural key paths and built-in type names in memory without retaining scalar values;
8. classify shapes into `account_overview`, `content_list`, `content_lifetime`, or `unclassified` based only on key names and nesting;
9. close resources in reverse order in `finally` and report exact built-in `aliveResourceCount`.

The implementation must not call `page.evaluate`, `page.request`, `context.request`, `requests`, or any page fetch API.

The test file must define local fakes with these exact observable members so the tests do not import or start Playwright:

```python
class FakeResponse:
    def __init__(self, url, content_type, payload):
        self.url = url
        self.headers = {"content-type": content_type}
        self._payload = payload

    def json(self):
        return self._payload

class FakePage:
    fetch_calls = 0
    click_calls = 0

    def on(self, event, callback):
        self.response_callback = callback

    def goto(self, url, **_kwargs):
        self.goto_url = url

class FakePlaywright:
    def __init__(self, responses=(), failure=None):
        self.responses = responses
        self.failure = failure
        self.close_order = []
        self.page = FakePage()
```

Complete the fake browser/context objects only with `new_context()`, `new_page()` and `close()` methods needed by `_probe_with_browser`; every close method appends its resource name to the shared `close_order` list.

- [ ] **Step 4: Run the full verifier module**

Run: `.venv/bin/python -m unittest test_xiaohongshu_data_contract_verifier -v`

Expected: all tests pass, including timeout and process-control cleanup.

- [ ] **Step 5: Commit Task 3**

```bash
git add tools/verify_xiaohongshu_data_contract.py test_xiaohongshu_data_contract_verifier.py
git commit -m "增加小红书官方响应结构探测"
```

### Task 4: 探测报告持久化边界与零动作回归

**Files:**
- Modify: `tools/verify_xiaohongshu_data_contract.py`
- Modify: `test_xiaohongshu_data_contract_verifier.py`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `_write_report(path: Path, report: object) -> None`
- Report output directory: `.superpowers/sdd/2026-08-20-xiaohongshu-data-contract-discovery/`

- [ ] **Step 1: Write failing persistence-injection tests**

```python
def test_persisted_report_is_finally_sanitized(self):
    injected = {
        "status": "success",
        "account": {"nickname": "private", "phone": "13800000000"},
        "responses": [{
            "url": "https://creator.xiaohongshu.com/api?cookie=secret",
            "keyPaths": ["data.items[].note_id"],
            "raw": {"note_id": "private-id", "title": "private-title"},
        }],
        "cleanup": {"closed": True, "aliveResourceCount": 0, "unknown": "secret"},
    }
    verifier._write_report(self.report_path, injected)
    text = self.report_path.read_text("utf-8")
    for forbidden in ("13800000000", "private-id", "private-title", "cookie=", "secret"):
        self.assertNotIn(forbidden, text)
    self.assertEqual(json.loads(text)["responses"][0]["path"], "/api")
```

- [ ] **Step 2: Run the persistence test and verify RED**

Expected: fail because `_write_report` does not exist.

- [ ] **Step 3: Implement final-boundary sanitization and ignore runtime reports**

`_write_report()` must sanitize again immediately before UTF-8 JSON serialization, reject paths outside the exact SDD directory, create parents, and write through a same-directory temporary file followed by `replace()`. Add only the generated `probe-report.json` path to `.gitignore`; do not ignore the whole SDD directory.

- [ ] **Step 4: Run verifier tests, py_compile and forbidden-pattern scan**

Run:

```bash
.venv/bin/python -m unittest test_xiaohongshu_data_contract_verifier -v
.venv/bin/python -m py_compile tools/verify_xiaohongshu_data_contract.py test_xiaohongshu_data_contract_verifier.py
! rg -n "Cookie|Authorization|Set-Cookie|response\.text|response\.body|page\.evaluate|context\.request" tools/verify_xiaohongshu_data_contract.py
git diff --check
```

Expected: tests and compile pass; forbidden scan has no matches; diff check exits 0.

- [ ] **Step 5: Commit Task 4**

```bash
git add .gitignore tools/verify_xiaohongshu_data_contract.py test_xiaohongshu_data_contract_verifier.py
git commit -m "加固小红书探测报告隐私边界"
```

### Task 5: 真实只读探测与事实合同

**Files:**
- Create: `docs/superpowers/reports/2026-08-20-xiaohongshu-data-contract-report.md`
- Runtime-only ignored output: `.superpowers/sdd/2026-08-20-xiaohongshu-data-contract-discovery/probe-report.json`

**Interfaces:**
- Consumes: `tools/verify_xiaohongshu_data_contract.py --execute`
- Produces: an evidence-backed checked-in contract report containing only public endpoint paths, request methods, structural field names, pagination keys, observation time, availability verdicts and cleanup result

- [ ] **Step 1: Run default mode once and prove zero action**

Run:

```bash
.venv/bin/python tools/verify_xiaohongshu_data_contract.py
```

Expected: JSON mode is `plan`; no account selection, browser process or runtime report is created.

- [ ] **Step 2: Execute one real read-only probe**

Run:

```bash
.venv/bin/python tools/verify_xiaohongshu_data_contract.py \
  --execute \
  --report .superpowers/sdd/2026-08-20-xiaohongshu-data-contract-discovery/probe-report.json
```

Expected: one of these truthful terminal states:

- `success`: all three response classes observed and cleanup is zero;
- `partial_success`: at least one trusted class observed, missing classes listed by fixed code;
- `login_required` or `verification_required`: no response contract claimed;
- `failed`: fixed failure code and no leaked exception text.

If there is no eligible account, the command must stop at `xiaohongshu_account_selection_required`; ask Andy to log in through the existing account-management UI and do not start a browser. If there is more than one eligible account, stop at the same fixed code and do not guess an account.

- [ ] **Step 3: Verify process cleanup and report purity**

Check that the verifier process and any Chromium child it created have exited. Scan the report for forbidden credential, query, personal and content fields. Expected: zero matches; `cleanup.closed is true`; `aliveResourceCount == 0` using exact built-in integer semantics.

- [ ] **Step 4: Write the checked-in factual contract report**

The report must contain:

- execution date and platform type only, no account ID or name;
- each observed official endpoint path without query string;
- method and response structural key paths;
- classification as account overview, content list or content lifetime;
- pagination key names and whether full traversal is technically possible;
- explicit missing classes and fixed failure codes;
- browser cleanup evidence;
- a verdict: `adapter_contract_ready` only if all three required classes are observed, otherwise `adapter_contract_incomplete`.

Do not write metric meanings that cannot be proven from official labels or stable field names. Do not include example values.

- [ ] **Step 5: Run final offline verification after writing the report**

Run:

```bash
.venv/bin/python -m unittest test_xiaohongshu_data_contract_verifier -v
.venv/bin/python -m unittest test_douyin_data_collector test_platform_data_sync test_platform_data_service test_data_monitor_page -v
git diff --check
```

Expected: all tests pass; existing Douyin V2 behavior is unchanged.

- [ ] **Step 6: Commit the factual contract report**

```bash
git add docs/superpowers/reports/2026-08-20-xiaohongshu-data-contract-report.md
git commit -m "记录小红书数据只读合同"
```

### Task 6: 合同复审与下一计划停点

**Files:**
- Review: `docs/superpowers/reports/2026-08-20-xiaohongshu-data-contract-report.md`
- Review: `tools/verify_xiaohongshu_data_contract.py`
- Review: `test_xiaohongshu_data_contract_verifier.py`

**Interfaces:**
- Produces one terminal decision: `adapter_contract_ready` or `adapter_contract_incomplete`

- [ ] **Step 1: Request a read-only security and evidence review**

Reviewer must verify:

- default mode is genuinely zero action;
- execute mode cannot select other platforms or ambiguous accounts;
- browser performs no clicks, form input, upload, publish or request replay;
- persisted report is sanitized at the final write boundary;
- all resources close on success, ordinary failure, timeout and process-control exception;
- report claims are directly supported by observed structural evidence.

- [ ] **Step 2: Resolve every Critical or Important finding with independent RED/GREEN**

For each finding, create one deterministic named regression test, run it to observe failure, implement the smallest fix, then rerun only that test. After all findings are green, run the verifier module once and the affected V2 modules once.

- [ ] **Step 3: Stop at the contract gate**

If verdict is `adapter_contract_ready`, invoke `superpowers:writing-plans` to create `docs/superpowers/plans/2026-08-20-xiaohongshu-data-adapter.md` using the now-known endpoint paths and fields. If verdict is incomplete, report the exact missing evidence and do not implement a speculative production collector.
