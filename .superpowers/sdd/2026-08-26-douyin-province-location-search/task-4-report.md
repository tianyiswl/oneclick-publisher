# Task 4 report — bounded province location driver

## Baseline

- Base: `1b994e8`.
- Added five initial bounded-driver tests before implementation. All five failed because the page did not yet expose the bounded-driver methods.
- No real Douyin page, publishing action, package build, or full-suite test was run.

## Implementation

- Added an asynchronous province-only coordinator. One click runs no more than three platform actions and uses a monotonic 30-second budget without sleeping or waiting on the Qt thread.
- Empty, duplicate-only, wrong-region, and commission-filtered pages advance within the same click. A newly eligible root-keyword candidate stops the click immediately.
- An exhausted city persists its advanced plan in a background task before its next-city search is allowed to continue. Only the active city platform snapshot is cleared.
- Eligible candidate count and `plan_progress_text()` update after every accepted bounded page. Province plans ignore the old raw-candidate/ten-page terminal limits and terminate only on all subqueries exhausted or 100 eligible candidates.
- Timeout/context failures retain the current city as retryable, use the required stable public error codes, and discard stale request-token callbacks.
- Progress writes are included in shutdown cancellation. Existing single platform search/load-more methods remain single-action methods.

## Verification

Focused command:

```text
QT_QPA_PLATFORM=offscreen ../../.venv/bin/python -m unittest -v test_douyin_commerce_service.DouyinCommerceBatchUiTests test_douyin_commerce_service.DouyinCommerceSessionContractTests test_douyin_commerce_collectors.DouyinCommerceCollectorManagerTests
```

Result: `356` tests passed, `0` failed.

Additional checks:

```text
../../.venv/bin/python -m py_compile ui/douyin_commerce_page.py test_douyin_commerce_service.py
git diff --check
```

Result: passed.

## Self-review

- Confirmed the bounded path applies only to province plans; normal and city searches preserve the existing one-action behavior.
- Confirmed all platform and SQLite work uses existing runner callbacks; no Qt-thread blocking wait was introduced.
- Confirmed stale owners are rejected before state mutation and that retries do not add the current city to completed indices.
- Confirmed the accepted-page save completes before a zero-growth continuation can start the next city.

## Concerns

- This is offline/UI-contract verification only. A commerce-capable Douyin account must still verify actual cross-city platform behavior separately.
- `tools/check_workstream_scope.py --stream location --base origin/main` returned `REVIEW_REQUIRED` because the integration branch already contains shared/outside historical changes relative to `origin/main`; this task's working-tree diff is limited to the two allowed source/test files plus this required report.
