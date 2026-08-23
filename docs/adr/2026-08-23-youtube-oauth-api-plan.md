# YouTube OAuth/API Implementation Plan

> **For Codex:** Use the subagent-driven-development workflow and execute every task with test-driven development. Do not perform real Google authorization or upload in this plan.

**Goal:** Add an overseas-owned, offline-testable Google desktop OAuth adapter and YouTube private-upload/readback adapter without touching shared core.

**Architecture:** Keep OAuth, loopback callback parsing, credential-store protocol, YouTube HTTP protocol, private-only policy and receipt verification in two flat `app_core/overseas_*` modules. Inject HTTP, clock and persistence boundaries. Existing browser publishing stays unchanged; selection and UI wiring are deferred to a separate integration commit.

**Tech Stack:** Python 3, standard library, existing `requests`, `unittest`.

---

### Task 1: OAuth authorization contract

**Files:**
- Create: `test_overseas_youtube_oauth.py`
- Create: `app_core/overseas_youtube_oauth.py`

**Step 1: Write failing tests**

Cover authorization URL generation with a random loopback port/path, `state`, PKCE S256, minimal `youtube.upload` scope, offline access and consent prompt. Cover callback success, denial, missing code, mismatched state and single-use consumption. Assert secret values never appear in exceptions or public result representations.

**Step 2: Verify RED**

Run:

```bash
.venv/bin/python -m unittest -v test_overseas_youtube_oauth
```

Expected: fail because `app_core.overseas_youtube_oauth` does not exist or the tested contract is missing.

**Step 3: Implement the minimum contract**

Add immutable authorization request/callback value objects, PKCE helpers, authorization URL construction and a single-use callback verifier. Use only `127.0.0.1`; never include authorization code, verifier, state or callback URL in error text.

**Step 4: Verify GREEN**

Run the same test command and require all OAuth contract tests to pass.

**Step 5: Commit**

```bash
git add app_core/overseas_youtube_oauth.py test_overseas_youtube_oauth.py
git commit -m "feat: add youtube desktop oauth contract"
```

### Task 2: Token exchange, refresh and credential boundary

**Files:**
- Modify: `test_overseas_youtube_oauth.py`
- Modify: `app_core/overseas_youtube_oauth.py`

**Step 1: Write failing tests**

Cover authorization-code exchange, refresh-token preservation when refresh responses omit it, expiry calculation, revoked/invalid grant handling, HTTP timeout and response sanitization. Define a credential-store protocol and an in-memory test implementation; assert no production cleartext fallback exists.

**Step 2: Verify RED**

```bash
.venv/bin/python -m unittest -v test_overseas_youtube_oauth
```

Expected: new tests fail on missing token client and storage contract.

**Step 3: Implement the minimum contract**

Add injected HTTP transport and clock, typed token value object, exchange/refresh methods and credential-store protocol. Public errors contain stable reason codes only, not raw response bodies or request payloads.

**Step 4: Verify GREEN and commit**

```bash
.venv/bin/python -m unittest -v test_overseas_youtube_oauth
git add app_core/overseas_youtube_oauth.py test_overseas_youtube_oauth.py
git commit -m "feat: add youtube oauth token lifecycle"
```

### Task 3: YouTube zero-write preflight and channel identity

**Files:**
- Create: `test_overseas_youtube_api.py`
- Create: `app_core/overseas_youtube_api.py`

**Step 1: Write failing tests**

Cover exactly one readable video, non-empty title, YouTube limits, private-only visibility, unsupported field rejection and no HTTP mutation during local preflight. Cover a unique channel result, empty result and ambiguous result. Assert returned identity exposes no tokens.

**Step 2: Verify RED**

```bash
.venv/bin/python -m unittest -v test_overseas_youtube_api
```

Expected: fail because the API adapter does not exist.

**Step 3: Implement the minimum contract**

Add request/identity value objects, local preflight validation and injected read-only channel lookup. Do not call `videos.insert` from preflight. Reject playlist, thumbnail, timer, public/unlisted and AI declaration instead of ignoring them.

**Step 4: Verify GREEN and commit**

```bash
.venv/bin/python -m unittest -v test_overseas_youtube_api
git add app_core/overseas_youtube_api.py test_overseas_youtube_api.py
git commit -m "feat: add youtube api preflight contract"
```

### Task 4: Private resumable upload and exact-ID readback

**Files:**
- Modify: `test_overseas_youtube_api.py`
- Modify: `app_core/overseas_youtube_api.py`

**Step 1: Write failing tests**

Cover mandatory explicit confirmation, fixed private metadata, one resumable-session initiation, media upload to the returned `Location`, `201` video ID extraction, `308` same-session continuation, exact-ID `videos.list` query, channel/title/privacy match and processing states. Cover missing location, missing ID, mismatch, ambiguous outcome and the invariant that no second `videos.insert` occurs automatically.

**Step 2: Verify RED**

```bash
.venv/bin/python -m unittest -v test_overseas_youtube_api
```

Expected: new tests fail on missing upload/readback behavior.

**Step 3: Implement the minimum contract**

Add upload state/receipt value objects and adapter methods for initiating one session, uploading or resuming only that session, and verifying exact-ID readback. Treat the session URI as sensitive. Return `outcome_unknown` instead of issuing a new insert when the result is unclear.

**Step 4: Verify GREEN and commit**

```bash
.venv/bin/python -m unittest -v test_overseas_youtube_api
git add app_core/overseas_youtube_api.py test_overseas_youtube_api.py
git commit -m "feat: add youtube private upload readback"
```

### Task 5: Status, regression and scope evidence

**Files:**
- Modify: `docs/OVERSEAS_PLATFORM_STATUS.md`
- Keep: `docs/adr/2026-08-23-youtube-oauth-api-v3.architecture.*`
- Keep: `docs/adr/2026-08-23-youtube-oauth-api-design.md`
- Keep: `docs/adr/2026-08-23-youtube-oauth-api-plan.md`

**Step 1: Update status truthfully**

Record that the official adapter is locally implemented and offline-tested but not wired into shared UI, not authorized against a Google project, not restart-tested and not upload-tested. Preserve the browser-route failure evidence.

**Step 2: Run focused and overseas regression tests**

```bash
.venv/bin/python -m unittest -v \
  test_overseas_youtube_oauth test_overseas_youtube_api \
  test_overseas_integration test_overseas_publish_routing \
  test_overseas_video_publish test_meta_browser_publish
```

**Step 3: Run hygiene and scope gates**

```bash
git diff --check
.venv/bin/python tools/check_workstream_scope.py --stream overseas --base origin/main
```

Expected: tests pass; scope checker reports only overseas-owned files.

**Step 4: Review and commit docs**

```bash
git add docs/OVERSEAS_PLATFORM_STATUS.md docs/adr/2026-08-23-youtube-oauth-api-*
git commit -m "docs: record youtube oauth api validation boundary"
```

### Task 6: Review, push and integration handoff

**Files:**
- Do not modify shared files in the overseas feature commits.

**Step 1: Inspect branch delta**

```bash
git status --short --branch
git diff --stat origin/main...HEAD
git log --oneline origin/main..HEAD
```

**Step 2: Run final verification from a clean state**

Repeat Task 5 tests, `git diff --check`, and the overseas scope checker after all review fixes.

**Step 3: Push only this branch**

```bash
git push origin feature/overseas-login-publish-v2
```

**Step 4: Hand off shared wiring without merging it**

Provide the integration task with exact shared touchpoints: `requirements-oneclick.txt`, account persistence/service/UI, publish page/service/runtime/task routing and OS credential-store implementation. A separate integration commit must wire adapter selection explicitly and run the repository full suite plus offscreen UI. Do not merge that commit from this feature line.

**Step 5: Stop at the external boundary**

Do not create a Google project, accept legal agreements, enter credentials, authorize an account or upload a real video without the required user action/authorization. Report local and real-platform validation as separate states.
