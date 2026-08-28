# SDD ledger — plan: docs/superpowers/plans/2026-08-28-tiktok-system-browser-login-bridge.md

## Setup

- Workspace: `/Users/andy/Documents/Codex/2026-07-28/new-chat/outputs/一键发桌面UI基座/.worktrees/overseas-login-publish-v2`
- Branch: `feature/overseas-login-publish-v2`
- Plan baseline: `3c5d786`
- Spec: `docs/superpowers/specs/2026-08-28-tiktok-system-browser-login-bridge-design.md`
- Isolation: linked git worktree; not a submodule; no additional worktree created.
- Baseline: `QT_QPA_PLATFORM=offscreen ../../.venv/bin/python -m unittest discover -v` — 2389 tests, 0 failures, 83.111s.

## Pre-flight consistency scan

| Scope | Producer / requirement | Consumer / test | Finding |
| --- | --- | --- | --- |
| Task 1 self-check | TikTok-only domain/origin filter and stable errors | Allowlist, malformed input, missing session, immutability tests | Consistent after adding malformed-port rejection. |
| Task 2 self-check | System browser discovery, owned staging, lifecycle and cleanup | Command allowlist, process outcomes, path/symlink and cleanup tests | Consistent; browser process fakes prevent real launches. |
| Task 3 self-check | Two-context handle readback and atomic account/session persistence | Leakage, mismatch, transaction, compensation and permissions tests | Conflict found: candidate collector text referenced `existing_account` absent from its signature. |
| Task 4 self-check | Public TikTok route, UI lifecycle and GUI-only stale cleanup | Routing, queue, source-live isolation and startup-order tests | Consistent; shared files are isolated in a separate commit. |
| Task 5 self-check | Regression plus one login-only source-client acceptance | Same-handle restart readback and TikTok-only structure | Consistent; explicitly forbids upload/form/Post. |
| Tasks 1 → 3 | `sanitize_tiktok_storage_state` | Candidate collection consumes sanitized state | Interface names and error contract match. |
| Tasks 1 → 5 | Scope tests and structural predicate | Real acceptance records only `tiktokOnly=true/false` | Evidence does not expose cookie names or values. |
| Tasks 2 → 3 | `SystemBrowserSpec`, `TikTokLoginAttempt`, process/profile-release lifecycle | `overseas_tiktok_system_login.py` candidate/session orchestration | Same file is intentionally extended; Task 3 starts after Task 2 review. |
| Tasks 2 → 4 | `recover_stale_tiktok_login_attempts` | GUI startup invokes recovery before `MainWindow` | Interface matches; CLI/MCP remain outside this helper. |
| Tasks 2 → 5 | Owned staging cleanup guarantees | Real acceptance checks staging removal | Evidence boundary matches design. |
| Tasks 3 → 4 | `TikTokSystemBrowserLoginSession` and stable queue protocol | `login_service` and `login_dialog` | Interface and message set match after adding `OPENING_SYSTEM_BROWSER`. |
| Tasks 3 → 5 | Verified account/session commit | Restart silent readback | Same public-handle contract is reused. |
| Tasks 4 → 5 | Public UI route and source-live startup | Real account-management acceptance | No public route remains on `_browser_cookie_gen(6)`. |

Ruling: Keep `collect_validated_tiktok_candidate` account-agnostic; perform the existing-account identity check in session orchestration and repeat it transactionally in the account saver — this matches the spec's single identity contract and avoids adding UI/database state to the browser intake helper — if wrong, Task 3 would need a signature change and focused rework before routing.

## Task status

- Task 1: complete (commits `179cef0..ec6e0be`, review clean)
- Task 1 base: `179cef0f963fca17c3e4d613cf07a4bbdc9e5c6c`
- Task 1 verification note: reviewer could not independently verify the reported full-suite run from the diff; controller confirmed the report contains the exact command/result (`2395/2395`) and the pre-task baseline independently passed (`2389/2389`).
- Task 2 base: `ec6e0be`
- Task 2 review at `09871c4`: spec failed — Critical: cleanup can escape through a symlinked staging ancestor; Important: dangling Chromium lock symlink is treated as absent; Important: process exit between poll and terminate can escape the finite outcome contract.
- Task 2 fix round 1/5 (2 addressed, 1 open — higher ancestor symlink still permits external deletion; commits `09871c4..9184b5e`).
- Task 2 fix round 2/5 (1 addressed, 0 open; commits `9184b5e..037dbd0`).
- Task 2: complete (commits `ec6e0be..037dbd0`, review clean after 2 fix rounds)
- Task 3: in progress
- Task 3 base: `037dbd0`
- Task 3 review at `649cefe`: spec failed — Important: cancellation can finish while the owned process survives; Important: commit-failure cleanup suppresses unlink failures; Important: old-session reference check and unlink have a race.
- Task 3: minor (deferred): cancellation during the profile-lock wait may still start Playwright before the next cancellation check.
- Task 3 verification note: focused `64/64` output is clean; full-suite `ResourceWarning` lacks traceback and cannot be attributed to this diff. Recheck under Task 5 full-suite evidence.
- Task 3 fix round 1/5 (3 addressed, 0 open; commits `649cefe..eaf5652`).
Ruling: If the owned browser does not exit after bounded termination, do not clear ownership, do not delete its staging, and terminate the login with `tiktok_login_cleanup_failed` instead of `CANCELLED` — a live browser makes safe cleanup unprovable — if wrong, users may see cleanup failure rather than cancellation and need to close the dedicated window manually.
Ruling: If rollback cannot remove a newly written candidate/temp session file, surface `tiktok_login_cleanup_failed` rather than hiding it behind `tiktok_login_commit_failed` — sensitive orphan cleanup outranks the original database error — if wrong, the public error wording will be less specific to the database failure.
Ruling: Old-session deletion must perform its final reference check and unlink while holding a new SQLite `BEGIN IMMEDIATE` transaction after the account row already points to the new file — this closes the DB-writer race without deleting an old file before the replacement row is committed — if wrong, file I/O inside a short write transaction may temporarily delay another account update.
- Task 3: complete (commits `037dbd0..eaf5652`, review clean after 1 fix round; 1 deferred Minor remains for final triage)
- Task 4: in progress
- Task 4 base: `eaf5652`
- Task 4 review at `2aca66b`: spec failed — Important: `run_ui_test()` calls `create_main_window()` and therefore can recover/delete real staging before source-live conflict checks.
Ruling: Keep normal `create_main_window()` recovery enabled by default as the plan requires, add an explicit keyword to disable recovery only for `run_ui_test()`, and test the real `run_ui_test` call path — this preserves production startup recovery while keeping self-test read-only — if wrong, another future non-production caller could omit the opt-out and would need the same explicit guard.
- Task 4 fix round 1/5 (1 addressed, 0 open; commits `2aca66b..e73e808`).
- Task 4: complete (commits `eaf5652..e73e808`, review clean after 1 fix round)
- Task 5: in progress
- Task 5 base: `e73e808`
- Task 5 real-login round 1: macOS Chrome reported Keychain authentication/encryption failure; after the dedicated window closed, silent validation returned `tiktok_session_expired` and no account was saved.
- Task 5 review at `f2a0229`: spec failed — Important: the mock-keychain flag was gated only by platform/browser name and did not itself prove the profile was the program-owned private TikTok staging profile.
Ruling: Before adding `--use-mock-keychain`, the macOS Google Chrome command builder must fail closed unless the profile is the exact existing `login-staging/tiktok/<32hex>/chrome-profile`, contains no symlinked owned path component, and is POSIX mode `0700`; Windows, Linux, and Edge remain unchanged — this keeps the workaround inside an ephemeral private profile — if wrong, a future caller could apply the predictable mock keychain to a normal Chrome profile.
- Task 5 fix round 1/5 (1 addressed, 0 open; commits `f2a0229..527c67f`; independent re-review PASS).
- Task 5 real-login round 2: mock-keychain removed the prior Keychain/encryption errors, but macOS kept the dedicated Chrome process alive after the user closed its last window; after the controller terminated only that owned process, validation still returned `tiktok_session_expired` before any account row was saved.
Ruling: Replace the unreliable macOS “close the window” completion dependency with a TikTok-only `完成登录并保存` action that gracefully stops only the session-owned browser and then validates; natural process exit remains supported, cancellation still discards the attempt, and the action never means login success by itself — if wrong, users on macOS remain permanently waiting or may confuse completion with a verified account.
Ruling: TikTok identity readback must wait a short bounded period for the SPA profile link to stabilize and must distinguish a final login/challenge route from an authenticated Studio page that lacks a unique public handle; diagnostics may expose only the route class and stable error code, never query strings, DOM text, cookies, or account secrets — if wrong, a valid but slowly rendered session will be discarded or a selector regression will continue to be mislabeled as an expired login.
- Task 5 fix round 2/5 (macOS completion action plus bounded identity readback; commits `527c67f..357d263`; independent review PASS).
- Task 5 real-login round 3: while the TikTok window stayed open, clicking “完成登录并保存” returned `tiktok_login_cleanup_failed`; subsequent read-only inspection confirmed the session-owned Chrome process had exited, but one TikTok staging directory remained.
Ruling: The completion action has its own bounded, default five-second graceful-exit wait for the session-owned browser. It must never call kill or affect a normal Chrome process; an exit within that window continues the normal validation flow, while a process that remains alive still returns `tiktok_login_cleanup_failed` and preserves staging. Cancellation and the overall login timeout keep their existing safety behavior — if wrong, a normal delayed Chrome shutdown can be discarded before its session is read, or an unproven cleanup can be reported as a success.
- Task 5 fix round 3/5 (delayed owned-browser exit acceptance, no-kill preservation, and bounded completion wait; commit `45201e8`; TikTok focused `136/136`, affected overseas `132/132`, account UI `20/20`; no real browser, upload, publish, push, or merge run).
