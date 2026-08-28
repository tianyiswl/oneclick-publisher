# TikTok System Browser Login Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 TikTok 首次绑定从会被 Google 拦截的 Chrome for Testing 改为“系统 Chrome 人工登录、TikTok-only 会话本地接收、唯一 handle 回读、清理后原子保存”的安全通道。

**Architecture:** `login_service` 只为 TikTok 路由新的系统浏览器会话；用户在一键发创建的临时 Chrome 资料夹内完成登录，登录期间不启动 Playwright 或远程调试。窗口关闭后再受控读取临时资料，过滤掉所有非 TikTok 状态，在空白浏览器中二次回读同一 handle；临时 Chrome 资料清理成功后，才写入会话文件和账号行。

**Tech Stack:** Python 3、PyQt6、SQLite、Playwright（仅用于登录后接收与回读）、系统 Google Chrome / Microsoft Edge、`unittest`、现有源码联调启动器。

**Spec:** `docs/superpowers/specs/2026-08-28-tiktok-system-browser-login-bridge-design.md`

## Global Constraints

- 登录阶段不得由 Playwright、Chrome DevTools 或其他自动化控制。
- 不读取用户日常 Chrome 资料夹，不保存 Google 或其他站点会话。
- 仅在 TikTok-only 会话和唯一公开 handle 均验证通过后，才能原子写入正式账号库。
- 成功、失败、取消、超时或客户端崩溃后，一键发自己创建的临时 Chrome 资料夹都必须进入可恢复的清理流程。
- 更新登录必须回读与旧记录相同的 handle；不允许覆盖为另一 TikTok 主体。
- 不从浏览器、命令行或内容包导入 Cookie、密码、验证码或二维码链接。
- 日志不记录 Cookie 名值、Google 邮箱、TikTok 登录标识、验证码、二维码或完整 storage-state。
- 首次真实验收只到“系统 Chrome 登录成功，重启后静默回读同一 handle”；不上传视频、不打开发布表单、不点击 `Post`。
- TikTok 专属文件先在当前功能分支提交；`app_core/account_service.py`、`app_core/login_service.py`、`ui/login_dialog.py` 和 `desktop_native_app.py` 必须单独作为共享提交，便于后续集成审查。
- 功能分支不升级版本号、不打包、不直接合并 `main`。

---

## File Structure

### New files

- `app_core/overseas_tiktok_session_scope.py` — TikTok Cookie/origin 白名单、storage-state 结构校验和纯函数过滤。
- `app_core/overseas_tiktok_system_login.py` — 系统浏览器解析、临时尝试目录、子进程生命周期、登录后接收、清理和会话文件提交。
- `test_overseas_tiktok_session_scope.py` — 域名边界、非 TikTok 状态剔除、空会话和输入不变性测试。
- `test_overseas_tiktok_system_login.py` — 浏览器命令、目录安全、取消/超时、二次回读、原子保存和崩溃清理测试。
- `test_tiktok_login_startup_wiring.py` — 证明只有正常 GUI 启动会执行 TikTok staging 恢复，CLI/MCP 不触发。
- `docs/superpowers/reports/2026-08-28-tiktok-system-browser-login-verification.md` — 离线测试、源码启动和首次真实登录边界证据。

### Modified files

- `app_core/account_service.py:1-450` — 新增 TikTok 公开 handle 绑定的条件写入，保证新绑定不重复、更新不串号。
- `app_core/login_service.py:20-123` — type 6 改走 `TikTokSystemBrowserLoginSession`，Meta 保持原恢复路由，YouTube 保持 OAuth 路由。
- `ui/login_dialog.py:161-353` — TikTok 系统 Chrome 文案、等待关窗、会话核验、清理和稳定错误码显示。
- `desktop_native_app.py:26-40,80-85` — 正常 GUI 创建主窗口前清理上次崩溃遗留的 TikTok staging。
- `test_overseas_integration.py:187-240` — 证明 TikTok 已不再调用 `_browser_cookie_gen(6)`，Meta/YouTube 不受影响。
- `test_account_detection_ui.py:150-190` — 系统 Chrome 提示、禁用手动保存、状态文案与错误码测试。
- `SOURCE_OF_TRUTH.md` — 只登记实际通过的离线测试与真实登录回读，不登记上传或发布成功。

---

### Task 1: Add the TikTok-only Session Scope Contract

**Files:**
- Create: `app_core/overseas_tiktok_session_scope.py`
- Create: `test_overseas_tiktok_session_scope.py`

**Interfaces:**
- Produces: `TikTokSessionScopeError(error_code: str, message: str)`.
- Produces: `is_tiktok_cookie_domain(value: object) -> bool`.
- Produces: `is_tiktok_https_origin(value: object) -> bool`.
- Produces: `sanitize_tiktok_storage_state(raw_state: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]`.
- Contract: return value contains only `cookies` and `origins`; input is never mutated; no TikTok cookie raises `tiktok_session_missing`; malformed or forbidden structure raises `tiktok_session_scope_invalid`.

- [ ] **Step 1: Write the failing filter tests**

```python
class TikTokSessionScopeTests(unittest.TestCase):
    def test_sanitize_keeps_only_tiktok_domains_and_origins(self):
        raw = {
            "cookies": [
                {"name": "sessionid", "value": "tt", "domain": ".tiktok.com", "path": "/"},
                {"name": "google", "value": "secret", "domain": ".google.com", "path": "/"},
                {"name": "lookalike", "value": "bad", "domain": "evil-tiktok.com", "path": "/"},
            ],
            "origins": [
                {"origin": "https://www.tiktok.com", "localStorage": [{"name": "tt", "value": "ok"}]},
                {"origin": "https://accounts.google.com", "localStorage": [{"name": "g", "value": "secret"}]},
            ],
        }
        result = sanitize_tiktok_storage_state(raw)
        self.assertEqual([item["domain"] for item in result["cookies"]], [".tiktok.com"])
        self.assertEqual([item["origin"] for item in result["origins"]], ["https://www.tiktok.com"])
        self.assertEqual(len(raw["cookies"]), 3)

    def test_empty_tiktok_cookie_set_is_not_a_login(self):
        with self.assertRaises(TikTokSessionScopeError) as raised:
            sanitize_tiktok_storage_state({"cookies": [], "origins": []})
        self.assertEqual(raised.exception.error_code, "tiktok_session_missing")
```

Add exact cases for `www.tiktok.com`, `.tiktok.com`, `shop.tiktok.com`, `tiktok.com.evil.test`, HTTP origins, credential-bearing origins, non-list `cookies`/`origins`, and a deep-copy assertion proving output mutation does not alter input.
Also add `https://www.tiktok.com:bad` and require a clean `False` result instead of leaking `urlsplit().port`'s `ValueError`.

- [ ] **Step 2: Run the test and prove the module is missing**

Run: `../../.venv/bin/python -m unittest -v test_overseas_tiktok_session_scope`

Expected: FAIL with `ModuleNotFoundError: app_core.overseas_tiktok_session_scope`.

- [ ] **Step 3: Implement the strict pure filter**

```python
class TikTokSessionScopeError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = str(error_code)
        self.public_message = str(message)
        super().__init__(self.public_message)


def is_tiktok_cookie_domain(value: object) -> bool:
    host = str(value or "").strip().lower().lstrip(".").rstrip(".")
    return host == "tiktok.com" or host.endswith(".tiktok.com")


def is_tiktok_https_origin(value: object) -> bool:
    parsed = urlsplit(str(value or ""))
    host = (parsed.hostname or "").lower().rstrip(".")
    try:
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and parsed.query == ""
        and parsed.fragment == ""
        and parsed.path in {"", "/"}
        and port in {None, 443}
        and (host == "tiktok.com" or host.endswith(".tiktok.com"))
    )
```

`sanitize_tiktok_storage_state` must accept only mappings whose `cookies` and `origins` are lists of mappings, use `copy.deepcopy` for retained entries, and return exactly `{"cookies": kept_cookies, "origins": kept_origins}`. It must never print or interpolate Cookie/localStorage values into an exception.

- [ ] **Step 4: Run the focused test**

Run: `../../.venv/bin/python -m unittest -v test_overseas_tiktok_session_scope`

Expected: PASS for every allowlist, rejection, missing-session and immutability case.

- [ ] **Step 5: Commit the overseas-only scope boundary**

```bash
git add app_core/overseas_tiktok_session_scope.py test_overseas_tiktok_session_scope.py
git commit -m "feat(overseas): isolate TikTok login session scope"
```

---

### Task 2: Build the System Browser Attempt and Safe Cleanup Lifecycle

**Files:**
- Create: `app_core/overseas_tiktok_system_login.py`
- Create: `test_overseas_tiktok_system_login.py`

**Interfaces:**
- Produces: `SystemBrowserSpec(name: str, executable: Path)`.
- Produces: `TikTokLoginAttempt(attempt_id: str, staging_root: Path, attempt_root: Path, profile_dir: Path)`.
- Produces: `TikTokSystemLoginError(error_code: str, message: str)`.
- Produces: `find_system_browser(*, platform: str = sys.platform, environ: Mapping[str, str] = os.environ, home: Path = Path.home()) -> SystemBrowserSpec`.
- Produces: `create_login_attempt(user_data_dir: Path = USER_DATA_DIR) -> TikTokLoginAttempt`.
- Produces: `build_system_browser_command(browser: SystemBrowserSpec, attempt: TikTokLoginAttempt) -> list[str]`.
- Produces: `wait_for_browser_exit(process, cancel_event: threading.Event, *, timeout_seconds: float, poll_seconds: float = 0.2) -> str` returning only `closed`, `cancelled`, or `timeout`.
- Produces: `wait_for_profile_release(attempt: TikTokLoginAttempt, *, timeout_seconds: float = 15.0, poll_seconds: float = 0.2) -> bool` without opening or reading profile contents.
- Produces: `remove_login_attempt(attempt_root: Path, staging_root: Path) -> None` and `recover_stale_tiktok_login_attempts(user_data_dir: Path = USER_DATA_DIR) -> list[str]`.

- [ ] **Step 1: Write failing command, path and process tests**

```python
def test_login_command_uses_only_the_dedicated_profile_and_no_automation_flags(self):
    browser = SystemBrowserSpec("Google Chrome", Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
    attempt = TikTokLoginAttempt("a" * 32, self.staging, self.staging / ("a" * 32), self.staging / ("a" * 32) / "chrome-profile")
    command = build_system_browser_command(browser, attempt)
    joined = " ".join(command)
    self.assertIn(f"--user-data-dir={attempt.profile_dir}", command)
    self.assertIn("https://www.tiktok.com/login", command)
    for forbidden in ("remote-debugging", "enable-automation", "playwright"):
        self.assertNotIn(forbidden, joined.lower())

def test_cleanup_rejects_any_target_outside_exact_staging_root(self):
    outside = self.root / "not-owned"
    outside.mkdir()
    with self.assertRaises(TikTokSystemLoginError) as raised:
        remove_login_attempt(outside, self.staging)
    self.assertEqual(raised.exception.error_code, "tiktok_login_cleanup_failed")
    self.assertTrue(outside.is_dir())
```

Add tests for generated attempt IDs, `0700` POSIX directory permissions, symlink attempts, exact child cleanup, stale-startup cleanup, normal close, cancellation, timeout, immediate profile release and bounded profile-release timeout. `FakeProcess` must record `terminate()`/`wait()` calls and must not invoke a real browser.

- [ ] **Step 2: Run the test and prove the symbols are missing**

Run: `../../.venv/bin/python -m unittest -v test_overseas_tiktok_system_login`

Expected: FAIL because the new module or named interfaces do not exist.

- [ ] **Step 3: Implement browser discovery and an allowlisted command**

```python
@dataclass(frozen=True, slots=True)
class SystemBrowserSpec:
    name: str
    executable: Path


@dataclass(frozen=True, slots=True)
class TikTokLoginAttempt:
    attempt_id: str
    staging_root: Path
    attempt_root: Path
    profile_dir: Path


def build_system_browser_command(browser: SystemBrowserSpec, attempt: TikTokLoginAttempt) -> list[str]:
    return [
        str(browser.executable),
        f"--user-data-dir={attempt.profile_dir}",
        "--profile-directory=Default",
        "--new-window",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",
        "https://www.tiktok.com/login",
    ]
```

Discovery order must be installed Google Chrome first, installed Microsoft Edge second. Use fixed macOS application paths, Windows `PROGRAMFILES`/`PROGRAMFILES(X86)`/`LOCALAPPDATA` paths and Linux `shutil.which` names; do not accept a browser path from CLI, account data or content packages.

- [ ] **Step 4: Implement owned-path cleanup and wait outcomes**

`create_login_attempt` must create `<USER_DATA_DIR>/login-staging/tiktok/<uuid>/chrome-profile`, reject symlinks, and set `0700` on POSIX. `remove_login_attempt` must resolve both paths, reject the staging root itself, require `attempt_root.parent == staging_root`, reject symlinks, and then remove only that exact attempt. `recover_stale_tiktok_login_attempts` may inspect only direct child names and filesystem metadata; it removes every safe orphan before the GUI creates a new login attempt and never reads browser history or session values.

`wait_for_browser_exit` must poll `process.poll()`, check cancellation before timeout, call `terminate()` only for its own process, and return a finite outcome. A timeout is fixed at `600.0` seconds in the session constructor but remains injectable in tests. After the process exits, `wait_for_profile_release` must perform only filesystem lock-existence checks inside the owned profile directory and wait a bounded 15 seconds; it must not force-delete lock files or inspect browser data.

- [ ] **Step 5: Run the focused lifecycle tests**

Run: `../../.venv/bin/python -m unittest -v test_overseas_tiktok_system_login`

Expected: PASS; no test starts Chrome or touches the production user-data directory.

- [ ] **Step 6: Commit the overseas-only attempt lifecycle**

```bash
git add app_core/overseas_tiktok_system_login.py test_overseas_tiktok_system_login.py
git commit -m "feat(overseas): add TikTok system browser login attempt"
```

---

### Task 3: Receive, Revalidate and Atomically Commit the TikTok Session

**Files:**
- Modify: `app_core/overseas_tiktok_system_login.py`
- Modify: `app_core/account_service.py:1-450`
- Modify: `test_overseas_tiktok_system_login.py`
- Modify: `test_overseas_tiktok_identity.py`

**Interfaces:**
- Consumes: `sanitize_tiktok_storage_state` from Task 1.
- Consumes: `read_tiktok_identity` and `validate_identity_binding` from `app_core.overseas_tiktok_identity`.
- Produces: `TikTokLoginCandidate(storage_state: dict[str, list[dict[str, Any]]], identity: TikTokIdentity)`.
- Produces: `collect_validated_tiktok_candidate(attempt: TikTokLoginAttempt, browser: SystemBrowserSpec, *, playwright_factory=async_playwright) -> Awaitable[TikTokLoginCandidate]`.
- Produces: `commit_tiktok_login_candidate(candidate: TikTokLoginCandidate, profile_name: str, *, record_id: int | None, existing_account: Mapping[str, Any] | None, cookie_dir: Path = COOKIE_DIR, account_saver=account_service.save_tiktok_browser_account) -> int`.
- Produces: `account_service.save_tiktok_browser_account(*, profile_name: str, storage_file_name: str, identity: TikTokIdentity, record_id: int | None = None, expected_account: Mapping[str, Any] | None = None) -> int`.
- Produces: `TikTokSystemBrowserLoginSession(profile_name: str, *, update_mode: bool = False, record_id: int | None = None, existing_account: Mapping[str, Any] | None = None, timeout_seconds: float = 600.0, user_data_dir: Path = USER_DATA_DIR, cookie_dir: Path = COOKIE_DIR, process_factory: Callable[..., subprocess.Popen] = subprocess.Popen)` with `queue`, `manual_save_supported = False`, `last_error_code: str | None`, `start()`, `cancel()` and `save()`.

- [ ] **Step 1: Write failing post-login intake tests**

```python
def test_candidate_is_revalidated_in_a_blank_context_with_sanitized_state(self):
    candidate = asyncio.run(collect_validated_tiktok_candidate(
        self.attempt,
        self.browser,
        playwright_factory=self.fake_playwright_factory,
    ))
    self.assertEqual(candidate.identity.handle, "expected.user")
    self.assertEqual(self.fake_blank_context.storage_state_input["cookies"][0]["domain"], ".tiktok.com")
    self.assertNotIn("google.com", repr(self.fake_blank_context.storage_state_input))
    self.assertEqual(self.fake_identity_reads, ["expected.user", "expected.user"])
```

Add failures for no TikTok Cookie, first/second handle mismatch, blank-context login rejection, profile lock, Playwright close failure, update-mode handle mismatch, ambiguous public handle and non-TikTok origin leakage. Fakes must expose only the minimal `launch_persistent_context`, `launch`, `new_context`, `new_page`, `goto`, `storage_state` and `close` methods. Existing `tiktok_account_identity_ambiguous` must be translated to the public session code `tiktok_account_invalid`, because the approved contract exposes one stable invalid-identity code.

- [ ] **Step 2: Write failing account commit and compensation tests**

```python
def test_account_saver_binds_verified_handle_and_rejects_duplicate_new_binding(self):
    account_id = account_service.save_tiktok_browser_account(
        profile_name="TikTok 测试",
        storage_file_name="first.json",
        identity=TikTokIdentity("expected.user", "Expected", "https://www.tiktok.com/@expected.user"),
    )
    self.assertGreater(account_id, 0)
    with self.assertRaises(TikTokIdentityError):
        account_service.save_tiktok_browser_account(
            profile_name="重复",
            storage_file_name="second.json",
            identity=TikTokIdentity("expected.user", "Expected", "https://www.tiktok.com/@expected.user"),
        )

def test_database_failure_removes_new_session_and_preserves_old_account(self):
    with self.assertRaises(TikTokSystemLoginError) as raised:
        commit_tiktok_login_candidate(
            self.candidate,
            "TikTok 测试",
            record_id=7,
            existing_account=self.old_account,
            cookie_dir=self.cookie_dir,
            account_saver=Mock(side_effect=RuntimeError("db unavailable")),
        )
    self.assertEqual(raised.exception.error_code, "tiktok_login_commit_failed")
    self.assertEqual(list(self.cookie_dir.glob("*.json")), [self.old_cookie])
```

Also test `0600` POSIX session permissions, JSON contains only sanitized `cookies`/`origins`, `os.replace` leaves no `.tmp`, concurrent update rejection preserves the old row/file, and successful update deletes the replaced session only after the account row references the new basename.

- [ ] **Step 3: Run focused tests and confirm the intake/commit failures**

Run:

```bash
../../.venv/bin/python -m unittest -v \
  test_overseas_tiktok_system_login \
  test_overseas_tiktok_identity
```

Expected: FAIL because candidate collection, account saver and commit interfaces are missing.

- [ ] **Step 4: Implement two-context intake**

```python
@dataclass(frozen=True, slots=True)
class TikTokLoginCandidate:
    storage_state: dict[str, list[dict[str, Any]]]
    identity: TikTokIdentity


async def collect_validated_tiktok_candidate(attempt, browser, *, playwright_factory=async_playwright):
    async with playwright_factory() as playwright:
        persistent = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(attempt.profile_dir),
            executable_path=str(browser.executable),
            headless=True,
            args=["--profile-directory=Default"],
        )
        first_page = await persistent.new_page()
        await first_page.goto(TIKTOK_STUDIO_URL, wait_until="domcontentloaded", timeout=45_000)
        first_identity = await read_tiktok_identity(first_page)
        sanitized = sanitize_tiktok_storage_state(await persistent.storage_state())
        await persistent.close()

        verifier = await playwright.chromium.launch(headless=True)
        blank = await verifier.new_context(storage_state=sanitized)
        second_page = await blank.new_page()
        await second_page.goto(TIKTOK_STUDIO_URL, wait_until="domcontentloaded", timeout=45_000)
        second_identity = await read_tiktok_identity(second_page)
        if first_identity.handle != second_identity.handle:
            raise TikTokSystemLoginError("tiktok_account_identity_mismatch", "TikTok 两次账号回读不一致")
        return TikTokLoginCandidate(sanitized, second_identity)
```

The implementation must close page/context/browser/Playwright resources in `finally`, translate a locked profile to `tiktok_login_profile_busy`, a missing/expired session to the matching stable code, and never include the underlying Cookie/state value in the public message. Candidate collection is deliberately account-agnostic: it proves only that the dedicated profile and the blank verification context return the same unique public handle.

- [ ] **Step 5: Implement conditional account persistence and atomic session replacement**

`save_tiktok_browser_account` must normalize the verified handle, reject another type-6 row with the same non-empty `accountReference`, and use one SQLite transaction. Update mode must compare `id`, `type`, `filePath`, `accountReference` and other overwritten fields from `expected_account`; `rowcount != 1` raises `tiktok_account_invalid` without changing the row.

`commit_tiktok_login_candidate` must serialize the in-memory sanitized state to a `0600` temporary file inside `COOKIE_DIR`, flush and `fsync`, use `os.replace` to a UUID `.json` basename, call the account saver, and delete the new file on any database error. After a successful update it may delete the old cookie only when its basename differs and no current type-6 row references it.

- [ ] **Step 6: Implement the public login session orchestration**

The session thread must emit only this ordered public protocol:

```text
OPENING_SYSTEM_BROWSER
SYSTEM_BROWSER_OPENED
WAITING_BROWSER_EXIT
VALIDATING_TIKTOK_SESSION
CLEANING_LOGIN_ATTEMPT
ACCOUNT_SAVED:<positive id>
```

The orchestration order is strict: wait for the browser process, wait for profile-lock release, collect and revalidate an in-memory candidate, validate its identity against `existing_account` with `allow_initial_bind=False` in update mode (or against an empty `accountReference` with `allow_initial_bind=True` for a new binding), remove the staging attempt, then call `commit_tiktok_login_candidate`. `save_tiktok_browser_account` repeats the same identity check inside its transaction to prevent a concurrent account-row change. The flow must never write the candidate session or account row before staging cleanup succeeds. Cancellation sets `last_error_code = "tiktok_login_cancelled"` and emits `CANCELLED` only after its owned process has been stopped and its owned attempt has entered cleanup. Other failures set the same property and emit `ERROR:<stable_error_code>` after cleanup; `tiktok_login_cleanup_failed` takes precedence because no account may be committed while sensitive staging remains. `save()` only emits a plain-language message stating that manual save is disabled.

- [ ] **Step 7: Run the focused security and identity tests**

Run:

```bash
../../.venv/bin/python -m unittest -v \
  test_overseas_tiktok_session_scope \
  test_overseas_tiktok_system_login \
  test_overseas_tiktok_identity
```

Expected: PASS; session files contain no Google domain, mismatches do not modify the old account, and cancellation/timeout/cleanup have terminal outcomes.

- [ ] **Step 8: Commit shared account persistence separately**

```bash
git add app_core/account_service.py test_overseas_tiktok_identity.py
git commit -m "feat(accounts): save verified TikTok browser identity"
```

- [ ] **Step 9: Commit the overseas intake and session orchestration**

```bash
git add app_core/overseas_tiktok_system_login.py test_overseas_tiktok_system_login.py
git commit -m "feat(overseas): receive verified TikTok system login"
```

---

### Task 4: Wire TikTok Routing, Login UI and Startup Recovery

**Files:**
- Modify: `app_core/login_service.py:20-123`
- Modify: `ui/login_dialog.py:161-353`
- Modify: `desktop_native_app.py:26-40,80-85`
- Modify: `test_overseas_integration.py:187-240`
- Modify: `test_account_detection_ui.py:150-190`
- Create: `test_tiktok_login_startup_wiring.py`

**Interfaces:**
- Consumes: `TikTokSystemBrowserLoginSession` and `recover_stale_tiktok_login_attempts` from Task 3.
- Changes: `login_service.start_login(6, ...)` returns and starts `TikTokSystemBrowserLoginSession`.
- Preserves: type 7 remains `YouTubeOAuthLoginSession`; type 8/9 remains `RecoveredOverseasLoginSession`; domestic authorization remains unchanged.
- UI consumes: `OPENING_SYSTEM_BROWSER`, `SYSTEM_BROWSER_OPENED`, `WAITING_BROWSER_EXIT`, `VALIDATING_TIKTOK_SESSION`, `CLEANING_LOGIN_ATTEMPT`, `ACCOUNT_SAVED:<id>`, `CANCELLED`, `ERROR:<code>`.

- [ ] **Step 1: Write failing routing and UI tests**

```python
def test_login_service_routes_tiktok_to_system_browser_session(self):
    created = MagicMock()
    created.start = MagicMock()
    with patch("app_core.overseas_tiktok_system_login.TikTokSystemBrowserLoginSession", return_value=created) as session_type:
        session = login_service.start_login(6, "TikTok 测试")
    self.assertIs(session, created)
    created.start.assert_called_once_with()
    self.assertEqual(session_type.call_args.kwargs["profile_name"], "TikTok 测试")

def test_tiktok_dialog_explains_system_chrome_and_disables_manual_save(self):
    dialog = LoginDialog(background_login=True)
    dialog.platform_combo.setCurrentIndex(dialog.platform_combo.findData(6))
    dialog.reset_login_prompt()
    self.assertIn("系统 Chrome", dialog.qr_label.text())
    self.assertIn("关闭专用窗口", dialog.qr_label.text())
```

Add queue-message assertions for all five intermediate states and every stable TikTok error code. Assert no UI string advises Cookie export, default-profile reuse, Google avoidance or manual save.

- [ ] **Step 2: Run focused routing/UI tests and confirm they fail**

Run:

```bash
QT_QPA_PLATFORM=offscreen ../../.venv/bin/python -m unittest -v \
  test_overseas_integration \
  test_account_detection_ui
```

Expected: FAIL because type 6 still uses `RecoveredOverseasLoginSession` and the dialog still describes an independent automated session.

- [ ] **Step 3: Route TikTok without changing YouTube or Meta**

```python
if login_type == 6:
    from .overseas_tiktok_system_login import TikTokSystemBrowserLoginSession
    existing_account = (
        account_service.get_managed_account(int(record_id))
        if update_mode and record_id is not None
        else None
    )
    session = TikTokSystemBrowserLoginSession(
        profile_name=profile_name,
        update_mode=update_mode,
        record_id=record_id,
        existing_account=existing_account,
    )
    session.start()
    return session
```

Remove TikTok from `RecoveredOverseasLoginSession._run` imports/callback map so `_browser_cookie_gen(6)` is unreachable from the public binding route. Do not alter the Meta callback or YouTube OAuth branch.

- [ ] **Step 4: Add explicit TikTok lifecycle copy**

`reset_login_prompt` must show: `请点击“开始登录”，一键发将打开系统 Chrome 专用临时窗口；登录完成后请关闭该窗口。`

`poll_messages` must map:

```python
TIKTOK_LOGIN_ERROR_TEXT = {
    "tiktok_system_browser_unavailable": "未找到可用的系统 Chrome 或 Edge。",
    "tiktok_login_attempt_timeout": "等待登录超时，未保存账号。",
    "tiktok_login_profile_busy": "TikTok 临时登录资料仍被浏览器占用。",
    "tiktok_session_scope_invalid": "登录状态包含超出 TikTok 的数据，已拒绝保存。",
    "tiktok_session_missing": "未检测到可用的 TikTok 登录状态。",
    "tiktok_session_expired": "TikTok 登录状态已失效。",
    "tiktok_account_invalid": "TikTok 未返回唯一可核对账号。",
    "tiktok_account_identity_mismatch": "当前 TikTok 账号与原记录不一致。",
    "tiktok_login_cleanup_failed": "临时登录资料清理失败，已停止保存账号。",
    "tiktok_login_commit_failed": "TikTok 会话未能安全写入账号库。",
}
```

The save button must stay disabled because `manual_save_supported=False`. `ACCOUNT_SAVED` continues through the existing silent account readback before the dialog accepts success.

- [ ] **Step 5: Run stale-attempt recovery during normal GUI startup**

Import only the recovery function in `desktop_native_app.py` and call it at the start of `create_main_window()`, before `MainWindow()` is constructed. Catch only `TikTokSystemLoginError`, log the stable code without a path or browser data, and continue startup; the next login attempt will surface cleanup failure if the owned staging remains.

Add `test_tiktok_login_startup_wiring.py` with a patched `MainWindow`: `create_main_window()` must call recovery once before constructing the window, while `run_controlled_publish_cli` and MCP entry paths are not invoked by this helper. Preserve the existing source-live lock and backup behavior.

- [ ] **Step 6: Run routing, UI and source-live tests**

Run:

```bash
QT_QPA_PLATFORM=offscreen ../../.venv/bin/python -m unittest -v \
  test_overseas_integration \
  test_account_detection_ui \
  test_tiktok_login_startup_wiring \
  test_source_live_launcher \
  test_source_live_runtime
```

Expected: PASS; TikTok uses system login, YouTube/Meta stay unchanged, and startup cleanup does not bypass source-live isolation.

- [ ] **Step 7: Commit shared routing/UI/startup changes separately**

```bash
git add \
  app_core/login_service.py \
  ui/login_dialog.py \
  desktop_native_app.py \
  test_overseas_integration.py \
  test_account_detection_ui.py \
  test_tiktok_login_startup_wiring.py
git commit -m "feat(accounts): route TikTok login through system Chrome"
```

---

### Task 5: Regression, Source Client and One Real Login Readback

**Files:**
- Create: `docs/superpowers/reports/2026-08-28-tiktok-system-browser-login-verification.md`
- Modify: `SOURCE_OF_TRUTH.md`

**Interfaces:**
- Verifies: the system-browser binding route is usable from the real account-management UI.
- Verifies: saved state is TikTok-only and restart validation reads the same public handle.
- Preserves: no upload, no `platform_form_check`, no final action and no `Post`.

- [ ] **Step 1: Run the complete focused TikTok login ladder**

Run:

```bash
../../.venv/bin/python -m unittest -v \
  test_overseas_tiktok_session_scope \
  test_overseas_tiktok_system_login \
  test_overseas_tiktok_identity \
  test_overseas_integration \
  test_account_detection_ui
```

Expected: all tests PASS and no test creates a real system-browser process.

- [ ] **Step 2: Run affected overseas publishing and routing regression**

Run:

```bash
../../.venv/bin/python -m unittest -v \
  test_overseas_tiktok_publish \
  test_overseas_video_publish \
  test_overseas_publish_routing \
  test_overseas_youtube_login \
  test_overseas_youtube_publish \
  test_meta_browser_publish
```

Expected: every module in the list passes and no test reaches a platform.

- [ ] **Step 3: Run syntax, diff and full-suite checks**

Run:

```bash
../../.venv/bin/python -m py_compile \
  app_core/overseas_tiktok_session_scope.py \
  app_core/overseas_tiktok_system_login.py \
  app_core/account_service.py \
  app_core/login_service.py \
  ui/login_dialog.py \
  desktop_native_app.py
git diff --check
../../.venv/bin/python -m unittest discover -v
```

Expected: compilation succeeds, `git diff --check` prints nothing, and the complete suite passes. Any pre-existing unrelated failure must be reported with its exact module and cannot be called a full pass.

- [ ] **Step 4: Start the source client with installed account data**

Close the installed client first, then run:

```bash
../../.venv/bin/python tools/run_source_live.py --page accounts
```

Expected: the launcher makes its normal production-data backup, acquires the source-live marker, and opens account management. No login starts until the user explicitly selects TikTok and clicks bind/relogin.

- [ ] **Step 5: Complete one bounded real TikTok login acceptance**

In the source client, bind the designated TikTok test account. Expected observable sequence:

```text
system Chrome dedicated window opens
user completes TikTok/Google login and closes only that dedicated window
client shows local validation and cleanup
account row is saved with one public TikTok handle
silent readback reports normal
```

After success, close and restart the source client once and run account detection. Confirm the same handle returns and the account remains normal. Inspect the saved storage-state structurally: all cookie domains and origins satisfy the TikTok-only allowlist; never print values.

- [ ] **Step 6: Stop at the login boundary**

Do not select media, do not open TikTok Studio upload, do not call `platform_form_check`, do not create a formal task, and do not click `Post`. If login requires CAPTCHA, device confirmation or credentials, pause only for that human action and continue automatically afterward.

- [ ] **Step 7: Record exact evidence and update current truth**

The verification report must record commit IDs, test counts, source-live backup path, account ID, normalized public handle, staging cleanup result and restart readback. It must state session structure as `tiktokOnly=true/false` without Cookie names or values. Update `SOURCE_OF_TRUTH.md` with exactly one of:

```text
TikTok 系统 Chrome 登录与重启后同主体回读已验收；上传与正式发布仍未验收。
```

or the precise failure boundary. Never convert local tests, a visible account row or a login page into publication success.

- [ ] **Step 8: Commit evidence only after actual verification**

```bash
git add docs/superpowers/reports/2026-08-28-tiktok-system-browser-login-verification.md SOURCE_OF_TRUTH.md
git commit -m "docs(overseas): record TikTok system login verification"
```
