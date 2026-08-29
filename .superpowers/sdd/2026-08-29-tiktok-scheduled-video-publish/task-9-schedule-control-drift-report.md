# Task 9 TikTok schedule-setting control drift repair

## Scope and safety

- Worktree: `feature/overseas-login-publish-v2` at task start `42f60ac`.
- This task used only offline fakes and automated tests. It did not open a browser, access the production database, upload media, or perform any platform action.
- No version, package, `SOURCE_OF_TRUTH.md`, or platform evidence was changed.

## RED evidence

Before production changes, the following six new behavior tests were run:

```text
.venv/bin/python -m unittest -v \
  test_overseas_tiktok_schedule_form.TikTokScheduleFormTests.test_configure_accepts_an_exact_schedule_radio_choice \
  test_overseas_tiktok_schedule_form.TikTokScheduleFormTests.test_configure_waits_for_delayed_exact_schedule_radio_choice \
  test_overseas_tiktok_schedule_form.TikTokScheduleFormTests.test_configure_waits_for_disabled_radio_then_rechecks_its_choice \
  test_overseas_tiktok_schedule_form.TikTokScheduleFormTests.test_configure_discards_detached_radio_and_uses_stable_remount \
  test_overseas_tiktok_schedule_form.TikTokScheduleFormTests.test_configure_resolves_unique_top_page_radio_when_upload_base_is_iframe \
  test_overseas_tiktok_schedule_form.TikTokScheduleFormTests.test_distinct_schedule_radios_across_base_and_top_page_are_ambiguous
```

Result: `FAILED (failures=1, errors=5)`. The existing implementation only queried legacy `role=switch` selectors from the upload base, so exact schedule radios and top-page-only controls produced `tiktok_schedule_unavailable`; distinct cross-scope controls did not produce the required ambiguity stop.

## Implementation

- Preserved legacy schedule-switch selectors and added only exact `Schedule` / `定时发布` / `排期` radio and checkbox choices.
- Kept final-action selectors out of schedule-choice resolution.
- Added an eight-observation, 250 ms bounded poll. A usable candidate must be the same DOM node on two consecutive observations.
- Resolves each schedule field over the upload base plus top page, deduplicates identical DOM nodes, and stops on stable distinct usable controls with `tiktok_schedule_control_ambiguous`.
- Re-resolves the choice after selection until it is checked; date and time remain editable-only with exact readback.
- `tiktok_schedule_unavailable` diagnostics now identify only `schedule choice`, `date`, or `time` and bounded structural counts (`scopes`, `candidates`, `usable`). No page text, HTML, attributes, captions, cookies, screenshots, or session data are retained.

## GREEN evidence

- New focused drift tests: `6` passed in `0.011s`.
- `test_overseas_tiktok_schedule_form`: `34` passed in `17.649s`.
- Focused publishing chain (`test_task_service`, `test_controlled_publish`, `test_publish_service`, `test_overseas_publish_routing`, `test_tiktok_schedule_contract`, `test_overseas_tiktok_schedule_form`, `test_overseas_tiktok_publish`, `test_overseas_video_publish`): `325` passed in `18.936s`.
- Explicit overseas/TikTok affected suite: `542` passed in `20.431s`.
- `git diff --check`: passed with no output.
- `git diff --cached --check`: passed with no output.
- `.venv/bin/python tools/check_workstream_scope.py --stream overseas --base origin/main`: `REVIEW_REQUIRED`, because the checker sees 63 branch changes relative to `origin/main`, including pre-existing shared files. This task's own source and test changes are overseas-owned; this report is the required task artifact.

## Changed files

- `uploader/tk_uploader/schedule_form.py`
- `test_overseas_tiktok_schedule_form.py`
- `.superpowers/sdd/2026-08-29-tiktok-scheduled-video-publish/task-9-schedule-control-drift-report.md`

## Self-review

- Legacy switch compatibility remains covered.
- Tests cover delayed radio appearance, disabled-to-enabled transition, detached/remounted controls, top-page/iframe uniqueness, same-node de-duplication, cross-scope ambiguity, and no final-action click during configuration.
- No selector invokes `Post`, text-coordinate clicking, force-clicking, or a final-action fallback.

## Remaining real-platform uncertainty

The actual TikTok account may still lack scheduling eligibility, render a different unsupported date/time control shape, or render an unrecognized exact accessible label. Those cases safely stop before a final action; real platform form verification remains separately authorized work.

## Commit

`fix(tiktok): stabilize schedule control resolution`
