# Task 3 report — Start and restore province plans in the commerce UI

## Baseline failure

- Added the Task 3 UI regressions first. Before implementation, the three UI
  cases failed because shared location state had no `rootKeyword`/
  `activeKeyword` fields or assignment-source map.
- Added the draft provenance regressions first. Before implementation, both
  cases failed because batch-draft items had no `locationAssignmentSource`.

## Implementation

- The UI now loads a saved root-query search plan after cache read, or creates
  one from the normalized root keyword, before starting a platform search.
- Shared state keeps root and active keywords separately. Collectors and
  paging receive the active keyword; the displayed/cache aggregate keeps the
  root keyword.
- Restored load counts are replayed through the existing asynchronous search
  and load-more handoff, with normal identity de-duplication.
- Review round 1 fixed the cache-sufficient restore branch: a saved positive
  load count now independently reopens the saved active city and replays only
  that saved count before any user-requested new page.
- City-origin platform candidates merge under both root and active cache keys.
- Batch drafts persist `manual` or `auto:<normalized-root-keyword>` assignment
  provenance. Legacy missing provenance is treated as manual; a root-keyword
  change clears only automatic assignments.

## Verification

- `QT_QPA_PLATFORM=offscreen ../../.venv/bin/python -m unittest -v test_douyin_commerce_service.DouyinCommerceBatchUiTests`
  - PASS: 188 tests.
- `../../.venv/bin/python -m unittest -v test_douyin_commerce_batch_draft_service`
  - PASS: 9 tests.
- `../../.venv/bin/python tools/check_workstream_scope.py --stream integration --base 8c3a1f5`
  - PASS: four owned implementation/test files.
- `git diff --check`
  - PASS: no whitespace errors.

## Self-review

- Confirmed no collector API was widened to understand province plans.
- Confirmed this task does not introduce the Task 4 bounded multi-action
  city-advance driver.
- Confirmed no real platform, publishing, packaging, or full-suite action was
  run.

## Concerns

- This is source and focused-local-test verification only. A commerce-capable
  Douyin account still needs to verify cross-city search continuation in a
  later integration task.
