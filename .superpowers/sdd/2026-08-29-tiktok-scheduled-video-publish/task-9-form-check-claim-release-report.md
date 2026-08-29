# Task 9 terminal platform-form-check claim release report

## Scope and result

Implemented the terminal `platform_form_check` claim-release path at
`5bfbabf` without touching the production database, browser, platform, media,
packaging, version, or public evidence documents.

The worker now attempts claim release in both terminal paths that previously
leaked a `started` claim:

- publish-lock-busy early return after its failed result is persisted;
- the normal worker `finally`, after success or pre-final failure reaches a
  terminal task status.

Cleanup exceptions do not change the persisted terminal result. They add only
the stable warning event `tiktok_form_check_claim_release_failed`; the private
exception text is not persisted.

## RED evidence

Before production code was added, the seven new temporary-SQLite tests were
run together. All seven failed for the intended missing behavior:

- the three transaction tests failed with
  `terminal platform-form-check claim release helper is missing`;
- the busy, success, and pre-final-exception worker tests each retained one
  claim instead of zero;
- the cleanup-failure test had no stable diagnostic event.

Result: `Ran 7 tests ... FAILED (failures=7)`.

## GREEN evidence

After the minimal implementation, the same exact seven tests passed:

```text
Ran 7 tests in 0.120s
OK
```

They use a real temporary SQLite database for task, item, event, and claim
state. Only the external TikTok platform runner is replaced in worker tests;
all persistence and release behavior is real.

## Atomic transaction contract

`release_terminal_tiktok_platform_form_check_claim(task_id)` runs one
`DELETE` inside `BEGIN IMMEDIATE` and deletes exactly one row only when every
condition is true:

1. `taskId` equals the requested task exactly;
2. claim `mode = 'platform_form_check'`;
3. claim `state = 'started'`;
4. the joined task has `mode = 'oneclick_platform_form_check'`;
5. task status is `success`, `failed`, or `partial_failed`;
6. the shared complete `tiktok_irreversible_evidence_sql(...)` predicate is
   false.

Zero matches return `False` without broad cleanup. SQL or transaction errors
roll back and propagate to the safe worker wrapper, which preserves the task
result and writes only a stable warning when possible.

The tests explicitly retain:

- formal claims;
- a claim belonging to another task;
- `claimed` state rather than expected `started`;
- pending and running task claims;
- a `platform_form_check` claim attached to the wrong task mode;
- every event, error-code, receipt-boolean, and receipt-phase shape recognized
  by the shared irreversible-evidence predicate.

## Changed files

- `app_core/controlled_publish.py`
  - added the exact atomic terminal form-check release helper.
- `app_core/publish_service.py`
  - wired safe release into busy and normal worker termination paths;
  - added stable cleanup-failure diagnostics without leaking exception text.
- `test_controlled_publish.py`
  - added real-SQLite transaction and irreversible-evidence coverage.
- `test_publish_service.py`
  - added real-SQLite busy, success, pre-final-failure, and cleanup-failure
    worker coverage.
- `test_overseas_publish_routing.py`
  - isolated the existing mocked routing test from the new database side
    effect while retaining its dedicated-service assertion.

## Verification

- exact new tests: `7/7` passed;
- `test_controlled_publish`: `64/64` passed;
- `test_publish_service`: `7/7` passed;
- `test_task_service`: `68/68` passed;
- `test_overseas_publish_routing`: `14/14` passed;
- combined specified set: `153/153` passed;
- `git diff --check 1d4ceac..HEAD`: clean;
- `git diff --check`: clean;
- scope checker from `1d4ceac`: `owned=1`, `shared=4`, `outside=0`, expected
  `REVIEW_REQUIRED` because this approved repair necessarily changes shared
  claim/task service files.

## Commit

- code and tests: `5bfbabf fix(tiktok): release terminal form-check claims`

## Self-review

- The implementation does not call or weaken the formal claim-release path,
  authorization consumption, stale recovery, startup compensation, duplicate
  worker gate, or task deletion protection.
- A terminal success cannot be invented by cleanup: release happens only
  after persisted task terminalization, and cleanup errors are swallowed only
  after a stable warning attempt.
- A final-action, acceptance, scheduled-readback, published-readback, or
  ambiguous receipt prevents release through the same shared predicate used by
  the existing formal dedupe and delete guards.
- No sensitive payload, session, Cookie, exception detail, or platform
  response is added to the new diagnostic.
- Residual risk: shared-core changes require independent integration review;
  the scope checker correctly reports `REVIEW_REQUIRED` rather than `OK`.
