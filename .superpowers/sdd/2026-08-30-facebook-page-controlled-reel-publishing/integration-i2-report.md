# Integration I2 report — authorization, recovery and deletion state contracts

## Status

Completed on `integration/facebook-page-v1-local`, including review fix round 1.

## Fixes

- A Facebook Page preflight is now permanently single-attempt once any formal claim references it. Both a newly requested grant and a grant signed before the first attempt fail safely with `facebook_preflight_already_used`; the rejected pre-signed grant remains unconsumed. A fresh preflight plus fresh authorization still proceeds after a safe terminal result.
- Stale Page recovery now uses the persisted heartbeat instead of treating the live desktop host PID as proof that its worker thread is alive. The publish service injects its in-process worker activity probe into the task service without a circular import: any live registered worker is protected even when its heartbeat is stale, while an expired heartbeat with no live registered worker can repair terminal claim evidence into the task and item projection.
- Generic CLI/process-stop handling now routes Page tasks through the Page claim reconciler. Interruption after `final_action_claimed` atomically converges claim, task, item and event to ambiguous/result-unknown with replay still blocked; interruption before the irreversible boundary remains safely failed.
- Deletion now starts `BEGIN IMMEDIATE` before checking Page claim history or mutating task rows. Deleting either the formal task or its preflight, including a competing claim insertion, returns a stable `ValueError`, preserves the evidence, and keeps mixed batch deletion atomic.

## TDD evidence

Each required behavior was first added as a focused test and observed failing against the previous production code:

- Used preflight after `safe_failed` / `confirmed_not_published`: expected `ControlledPublishError`, but no exception was raised.
- Stale Page heartbeat with live host PID and no worker: recovery returned `False`.
- CLI interrupt after `final_action_claimed`: claim remained `final_action_claimed` instead of `ambiguous`.
- Protected Page deletion: raw `sqlite3.IntegrityError` escaped.
- Review fix round 1, stale heartbeat plus live registered worker: the claim changed from `reserved` to `safe_failed` and the task changed from `running` to `failed`.
- Review fix round 1, claim insertion racing deletion after the old precheck: raw `sqlite3.IntegrityError: FOREIGN KEY constraint failed` escaped.

After each minimal production change, the focused tests and unchanged-behavior guards passed. The round-1 four-test GREEN run covered live-worker protection, absent-worker recovery, concurrent claim/delete protection and ordinary batch deletion protection.

## Verification

- Affected controlled publish, task, publish and CLI suites: `Ran 311 tests in 4.750s` — `OK`.
- Full repository suite: `Ran 2963 tests in 133.446s` — `OK`.
- Native desktop offscreen smoke test: `NATIVE_DESKTOP_UI_OK`.
- `git diff --check`: passed.

No feature flag, version, package, browser adapter, login/account UI, real Facebook session, preflight, upload or publish action was changed or exercised.

## Files

- `app_core/controlled_publish.py`
- `app_core/publish_service.py`
- `app_core/task_service.py`
- `test_controlled_publish_process.py`
- `test_facebook_page_controlled_publish.py`
- `test_facebook_page_task_service.py`
- `test_publish_service.py`

## Concerns

None within the I2 state-machine scope. Real Facebook behavior remains intentionally unverified because this task forbids login, browser use, upload and publish.
