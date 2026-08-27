# YouTube Desktop Login Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect the existing official YouTube desktop OAuth adapter to account management, operating-system credential storage, and restart validation without exposing the OAuth account to publishing yet.

**Architecture:** A YouTube-specific login session opens the system browser, consumes one loopback callback, exchanges the code, verifies exactly one channel, stores only the refresh token through `keyring`, and returns public account identity to the shared account service. SQLite stores `authMode=youtube_oauth`, an opaque credential reference, and the stable channel ID. Account management includes these rows; existing publishing and data collectors continue to receive browser-session rows only until the official publish route is wired separately.

**Tech Stack:** Python 3.12, `requests`, `keyring`, PyQt6, SQLite, `unittest`.

**Spec:** `docs/adr/2026-08-23-youtube-oauth-api-design.md`

## Global Constraints

- Work only in `.worktrees/overseas-login-publish-v2` on `feature/overseas-login-publish-v2`.
- Do not change the application version, create an installer, merge `main`, authorize a real Google account, or upload a video.
- Keep API failure from falling back to the old browser-login route.
- Never store or log authorization codes, access tokens, refresh tokens, callback URLs, or raw provider responses.
- Keep OAuth accounts out of publish targets until a separate official API publish integration is implemented and reviewed.
- Put shared account/UI/configuration edits in one separate wiring commit and stop after pushing the feature branch.

---

### Task 1: Synchronize the feature branch with the current released mainline

**Files:**
- No hand-authored source changes.

**Interfaces:**
- Consumes: `origin/main@5cea481` and `feature/overseas-login-publish-v2@1ae2e03`.
- Produces: one merge commit on the feature branch, leaving `main` unchanged.

- [ ] **Step 1: Verify the merge is conflict-free**

Run:

```bash
git merge-tree --write-tree --messages HEAD origin/main
```

Expected: exit `0` with no conflict messages.

- [ ] **Step 2: Merge current main into the feature branch**

Run:

```bash
git merge --no-ff origin/main -m "chore(overseas): sync youtube integration with current main"
```

- [ ] **Step 3: Re-run the focused overseas baseline**

Run:

```bash
.venv/bin/python -m unittest -v \
  test_overseas_youtube_oauth test_overseas_youtube_api \
  test_overseas_integration test_overseas_publish_routing \
  test_overseas_video_publish test_meta_browser_publish
```

Expected: all tests pass before new behavior is added.

### Task 2: Operating-system credential store and OAuth login session

**Files:**
- Create: `app_core/overseas_youtube_credentials.py`
- Create: `app_core/overseas_youtube_login.py`
- Create: `test_overseas_youtube_login.py`
- Modify: `app_core/overseas_youtube_oauth.py`

**Interfaces:**
- Consumes: `start_authorization_session(client_id)`, `YouTubeOAuthTokenClient`, `YouTubeChannelIdentityClient`, and the `OAuthCredentialStore` protocol.
- Produces: `KeyringOAuthCredentialStore`, `YouTubeOAuthLoginSession`, and `validate_saved_youtube_oauth_account(account, client_id=...)`.

- [ ] **Step 1: Write failing credential-store tests**

Cover opaque references, exact save/load/delete calls, missing backend, empty token rejection, and stable secret-free errors. A fake keyring backend must receive the secret, while exception strings and value representations must not contain it.

- [ ] **Step 2: Verify credential tests fail for missing code**

Run:

```bash
.venv/bin/python -m unittest -v test_overseas_youtube_login.YouTubeCredentialStoreTests
```

Expected: import or attribute failure because `KeyringOAuthCredentialStore` does not exist.

- [ ] **Step 3: Implement the minimal keyring adapter**

Expose this production boundary:

```python
class KeyringOAuthCredentialStore:
    def load_refresh_token(self, credential_reference: str) -> str | None: ...
    def save_refresh_token(self, credential_reference: str, refresh_token: str) -> None: ...
    def delete_refresh_token(self, credential_reference: str) -> None: ...
```

Use a fixed service name and lazy backend import. Normalize every backend failure to `OAuthCredentialError("credential_unavailable")` without echoing arguments or backend text.

- [ ] **Step 4: Verify credential tests pass**

Run the Step 2 command and require all tests to pass.

- [ ] **Step 5: Write failing login-session tests**

Cover system-browser opening, callback exchange, unique-channel lookup, new opaque reference creation, refresh-token save, account-saver arguments, cancellation, missing client ID, channel mismatch during relogin, rollback when account persistence fails, and restart validation that refreshes then confirms the same channel ID.

- [ ] **Step 6: Verify login-session tests fail for missing behavior**

Run:

```bash
.venv/bin/python -m unittest -v test_overseas_youtube_login.YouTubeOAuthLoginSessionTests
```

Expected: failures naming the missing session and restart-validation behavior.

- [ ] **Step 7: Implement the minimal session**

Expose the same queue-based UI contract as existing login sessions:

```python
session = YouTubeOAuthLoginSession(
    client_id=client_id,
    profile_name=profile_name,
    update_mode=update_mode,
    record_id=record_id,
    existing_account=existing_account,
    account_saver=account_saver,
)
session.start()
session.cancel()
```

The worker emits `BROWSER_OPENED`, then `ACCOUNT_SAVED:<id>` only after channel identity, credential storage, and account persistence all succeed. `save()` must never bypass OAuth verification.

- [ ] **Step 8: Verify all new overseas login tests pass**

Run:

```bash
.venv/bin/python -m unittest -v test_overseas_youtube_login
```

- [ ] **Step 9: Commit overseas-owned groundwork**

```bash
git add app_core/overseas_youtube_credentials.py app_core/overseas_youtube_login.py \
  app_core/overseas_youtube_oauth.py test_overseas_youtube_login.py
git commit -m "feat(overseas): add youtube desktop oauth login service"
```

### Task 3: Shared account persistence, UI routing, and restart detection

**Files:**
- Modify: `conf.py`
- Modify: `requirements-oneclick.txt`
- Modify: `app_core/account_service.py`
- Modify: `app_core/login_service.py`
- Modify: `ui/account_page.py`
- Modify: `ui/login_dialog.py`
- Modify: `test_overseas_integration.py`
- Modify: `test_account_detection_ui.py`

**Interfaces:**
- Consumes: Task 2's `YouTubeOAuthLoginSession` and `validate_saved_youtube_oauth_account`.
- Produces: `account_service.save_youtube_oauth_account(...)`, `account_service.list_managed_accounts()`, YouTube OAuth routing in `login_service.start_login`, and account-page-only visibility for OAuth rows.

- [ ] **Step 1: Write failing account persistence and routing tests**

The tests must prove:

```python
saved_id = account_service.save_youtube_oauth_account(
    profile_name="海外主体",
    credential_reference="youtube-oauth:opaque",
    channel_id="UC123",
    display_name="测试频道",
)
```

stores `authMode=youtube_oauth`, never stores a token, and returns through `list_managed_accounts()` but not through the publish-facing `list_accounts()`. Also prove that YouTube selection creates `YouTubeOAuthLoginSession`, a missing client ID stops safely, restart validation checks the saved channel, and deletion removes the keyring entry instead of treating it as a Cookie filename.

- [ ] **Step 2: Verify the new integration tests fail for expected missing behavior**

Run:

```bash
.venv/bin/python -m unittest -v test_overseas_integration test_account_detection_ui
```

- [ ] **Step 3: Add the public client-ID configuration and dependency**

Add `keyring==25.7.0` to `requirements-oneclick.txt` and read `YIJIANFA_YOUTUBE_OAUTH_CLIENT_ID` into `conf.YOUTUBE_OAUTH_CLIENT_ID`. Do not add a client secret or a default credential.

- [ ] **Step 4: Implement account persistence and validation routing**

Keep `list_accounts()` browser-only. Add `list_managed_accounts()` for account management and login checks. Persist the opaque credential reference in `filePath`, the stable channel ID in `accountReference`, and `youtube_oauth` in `authMode`. Branch validation and deletion by `authMode` before any Cookie-path operation.

- [ ] **Step 5: Route YouTube login to the official session**

`login_service.start_login(7, ...)` must create Task 2's session and must never instantiate `RecoveredOverseasLoginSession` for YouTube. TikTok and Meta routing remain unchanged.

- [ ] **Step 6: Make the existing dialog truthful for system-browser OAuth**

Keep the existing “绑定账号” button. When YouTube is selected, explain that the system browser will open and disable the manual-save fallback. On `ACCOUNT_SAVED:<id>`, reuse the existing background account readback before accepting the dialog.

- [ ] **Step 7: Verify the shared wiring tests pass**

Run the Step 2 command and `test_overseas_youtube_login`.

- [ ] **Step 8: Commit the shared wiring separately**

```bash
git add conf.py requirements-oneclick.txt app_core/account_service.py \
  app_core/login_service.py ui/account_page.py ui/login_dialog.py \
  test_overseas_integration.py test_account_detection_ui.py
git commit -m "feat(integration): wire youtube oauth account login"
```

Stop treating this commit as merge-ready until the integration task reviews it against the current mainline.

### Task 4: Architecture, status, and complete local verification

**Files:**
- Modify: `docs/adr/2026-08-23-youtube-oauth-api-v3.architecture.json`
- Regenerate: `docs/adr/2026-08-23-youtube-oauth-api-v3.architecture.html`
- Modify: `docs/OVERSEAS_PLATFORM_STATUS.md`
- Keep: `docs/adr/2026-08-27-youtube-desktop-login-integration-plan.md`

**Interfaces:**
- Consumes: the implemented account login and restart behavior.
- Produces: truthful local evidence and a pushable handoff branch.

- [ ] **Step 1: Update status without upgrading real-platform claims**

Record that the desktop UI path and restart-validation code are locally connected, but no Google project, real OAuth consent, Keychain round trip, or real channel readback has occurred. Keep private upload and all other overseas platforms unchanged.

- [ ] **Step 2: Deliver and visually inspect the architecture**

Run Archify `validate`, `deliver`, and `visual-check` in showcase mode with the repository root. Inspect the smallest and largest light/dark screenshots before claiming visual completion.

- [ ] **Step 3: Run focused and full verification**

```bash
.venv/bin/python -m unittest -v \
  test_overseas_youtube_login test_overseas_youtube_oauth \
  test_overseas_youtube_api test_overseas_integration \
  test_account_detection_ui test_overseas_publish_routing \
  test_overseas_video_publish test_meta_browser_publish
.venv/bin/python -m unittest discover -p 'test_*.py'
QT_QPA_PLATFORM=offscreen .venv/bin/python desktop_native_app.py --ui-test
.venv/bin/python -m py_compile \
  app_core/overseas_youtube_credentials.py \
  app_core/overseas_youtube_login.py \
  app_core/overseas_youtube_oauth.py \
  app_core/overseas_youtube_api.py
git diff --check
```

- [ ] **Step 4: Run the scope gate and preserve the expected stop**

```bash
.venv/bin/python tools/check_workstream_scope.py --stream overseas --base origin/main
```

Expected: exit `2` identifying the intentional shared wiring files. This is the handoff signal, not a failed feature test.

- [ ] **Step 5: Commit docs and push only the feature branch**

```bash
git add docs/OVERSEAS_PLATFORM_STATUS.md docs/adr/2026-08-23-youtube-oauth-api-v3.architecture.* \
  docs/adr/2026-08-27-youtube-desktop-login-integration-plan.md
git commit -m "docs(overseas): record youtube desktop login boundary"
git push origin feature/overseas-login-publish-v2
```

Do not create a pull request, merge main, build an installer, open Google Cloud Console, authorize an account, or upload a video.
