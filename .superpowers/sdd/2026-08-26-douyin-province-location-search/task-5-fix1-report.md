# Task 5 fix round 1 — align legacy reset-state tests

## Scope and baseline

- Base: `76c7c5bca11f18b264cbd0f7e68c12433aa1c39a`.
- Changed only `test_douyin_commerce_service.py`.
- Preserved the existing untracked verification report untouched.

## Reproduction and diagnosis

The two failing legacy exact-state assertions were reproduced before editing:

```text
QT_QPA_PLATFORM=offscreen ../../.venv/bin/python -m unittest -v \
  test_douyin_commerce_service.DouyinCommerceUiTests.test_clear_current_content_keeps_saved_content_recoverable \
  test_douyin_commerce_service.DouyinCommerceUiTests.test_abandon_session_resets_all_platform_settings_but_keeps_content
```

Result: 2 tests, 2 failures. Both actual reset states included the six approved stable province-search fields omitted by the legacy expectations: `rootKeyword`, `activeKeyword`, `searchPlan`, `eligibleIdentityCount`, `actionsThisClick`, and `replayLoadsRemaining`.

## Change

Added those six fields with their reset values to the two exact expected dictionaries. No production code, collector behavior, or reset behavior changed.

## Verification

- Reproduced two tests after change: 2/2 PASS.
- `DouyinCommerceUiTests`: 104/104 PASS.
- `DouyinCommerceBatchUiTests`: 203/203 PASS.
- `git diff --check`: PASS.

## Self-review

- The six fields are explicitly required shared state in approved Task 3 and are initialized by `_batch_location_state()`; asserting their empty reset values protects the established contract instead of concealing it.
- The patch touches only the two stated legacy tests and preserves all prior expected reset values.
- The untracked `docs/superpowers/reports/2026-08-26-douyin-province-location-search-verification.md` was not changed or staged.

## Concerns

This is a focused fix round only. The broader Task 5 regression ladder remains for its owning task to restart from the smallest regression command; no real account, packaging, installation, or publish action was run here.
