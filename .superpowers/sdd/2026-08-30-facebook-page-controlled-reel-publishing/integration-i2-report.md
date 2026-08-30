# Integration I2 report — authorization, recovery and deletion state contracts

## Status

Completed on `integration/facebook-page-v1-local`.

## Fixes

- A Facebook Page preflight is now permanently single-attempt once any formal claim references it. Both a newly requested grant and a grant signed before the first attempt fail safely with `facebook_preflight_already_used`; the rejected pre-signed grant remains unconsumed. A fresh preflight plus fresh authorization still proceeds after a safe terminal result.
- Stale Page recovery now uses the persisted heartbeat instead of treating the live desktop host PID as proof that its worker thread is alive. A fresh active task is left alone, while an expired Page heartbeat can repair terminal claim evidence into the task and item projection.
- Generic CLI/process-stop handling now routes Page tasks through the Page claim reconciler. Interruption after `final_action_claimed` atomically converges claim, task, item and event to ambiguous/result-unknown with replay still blocked; interruption before the irreversible boundary remains safely failed.
- Deletion now detects Page claim history before any mutation. Deleting either the formal task or its preflight returns a stable `ValueError`, preserves the evidence, and keeps mixed batch deletion atomic.

## TDD evidence

Each required behavior was first added as a focused test and observed failing against the previous production code:

- Used preflight after `safe_failed` / `confirmed_not_published`: expected `ControlledPublishError`, but no exception was raised.
- Stale Page heartbeat with live host PID and no worker: recovery returned `False`.
- CLI interrupt after `final_action_claimed`: claim remained `final_action_claimed` instead of `ambiguous`.
- Protected Page deletion: raw `sqlite3.IntegrityError` escaped.

After the minimal production changes, the focused tests and unchanged-behavior guards passed.

## Verification

- Affected controlled publish, task, publish and CLI suites: `Ran 310 tests in 4.882s` — `OK`.
- Full repository suite: `Ran 2962 tests in 133.717s` — `OK`.
- Native desktop offscreen smoke test: `NATIVE_DESKTOP_UI_OK`.
- `git diff --check`: passed.

No feature flag, version, package, browser adapter, login/account UI, real Facebook session, preflight, upload or publish action was changed or exercised.

## Files

- `app_core/controlled_publish.py`
- `app_core/task_service.py`
- `test_controlled_publish_process.py`
- `test_facebook_page_controlled_publish.py`
- `test_facebook_page_task_service.py`
- `test_publish_service.py`

## Concerns

None within the I2 state-machine scope. Real Facebook behavior remains intentionally unverified because this task forbids login, browser use, upload and publish.
