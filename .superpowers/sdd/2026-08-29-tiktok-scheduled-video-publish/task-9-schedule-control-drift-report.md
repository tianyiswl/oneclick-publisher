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

- Preserved legacy schedule-switch selectors and resolves native radio/checkbox choices only through Playwright exact `get_by_role(..., name=..., exact=True)` semantics for `Schedule` / `定时发布` / `排期`; this includes associated-label and `aria-labelledby` accessible names without querying final-action buttons.
- Kept final-action selectors out of schedule-choice resolution.
- Each observation freezes locators to element handles before comparison. A usable candidate, or a multi-candidate set, must be the same frozen DOM identity on two consecutive observations.
- Resolves each schedule field over the upload base plus top page, deduplicates identical DOM nodes, and stops on stable distinct usable controls with `tiktok_schedule_control_ambiguous`.
- Native radio/checkbox choices use `is_checked()`; legacy switches retain only the `aria-checked`/`checked` fallback. The choice is re-resolved after selection until checked; date and time remain editable-only with exact readback.
- Added a 15-second monotonic absolute deadline for each control resolution; every DOM await uses the remaining budget via `asyncio.wait_for`. The 64-observation cap is an additional guard, not a substitute for elapsed time. Only timeout/detached/remount transitions are retried.
- `tiktok_schedule_unavailable` diagnostics now identify only `schedule choice`, `date`, or `time` and bounded structural counts (`scopes`, `candidates`, `usable`). No page text, HTML, attributes, captions, cookies, screenshots, or session data are retained.

## GREEN evidence

- New focused drift tests: `6` passed in `0.011s`.
- `test_overseas_tiktok_schedule_form`: `34` passed in `17.649s`.
- Focused publishing chain (`test_task_service`, `test_controlled_publish`, `test_publish_service`, `test_overseas_publish_routing`, `test_tiktok_schedule_contract`, `test_overseas_tiktok_schedule_form`, `test_overseas_tiktok_publish`, `test_overseas_video_publish`): `325` passed in `18.936s`.
- Explicit overseas/TikTok affected suite: `542` passed in `20.431s`.
- `git diff --check`: passed with no output.
- `git diff --cached --check`: passed with no output.
- `.venv/bin/python tools/check_workstream_scope.py --stream overseas --base origin/main`: `REVIEW_REQUIRED`, because the checker sees 63 branch changes relative to `origin/main`, including pre-existing shared files. This task's own source and test changes are overseas-owned; this report is the required task artifact.

## Review remediation (round 1)

- RED: four adversarial tests failed against the first repair: exact accessible-name/native-property handling was unavailable, a live locator returned its original node after re-mount, changing multi-node sets were declared ambiguous too early, and a hung DOM read exceeded the caller deadline.
- GREEN: the four adversarial tests passed in `0.283s`; the full schedule-form suite passed `38` tests in `14.787s`; the focused publishing chain passed `329` tests in `15.989s`; the explicit affected suite passed `546` tests in `17.390s`.
- Diff checks: `git diff --check` and staged `git diff --cached --check` passed with no output before the implementation commit.
- Exact scope: `.venv/bin/python tools/check_workstream_scope.py --stream overseas --base 49f5395` reported `changed=2`, both overseas-owned, `scope-check: OK`.

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

Initial repair: `49f5395`

Review remediation implementation: `6c80a25ced742b20345fab2c44a0df23c50b0a97` (`fix(tiktok): harden schedule control polling`)

## Review remediation (round 2)

- Frozen-node comparison now uses the fake `node_id` seam only for immutable test nodes; real element handles compare with the exact Playwright `evaluate("(element, other) => element === other", other_handle)` protocol within the remaining deadline. Comparison errors propagate unless they are classified detached/remount/timeout transitions.
- Added protocol coverage for fresh Python wrappers of one real DOM node, duplicate same-node matches across two selectors, stable distinct handle nodes, and a broken comparison protocol. Added a real wall-clock 0.01-second deadline test; poll sleep is now `min(250 ms, remaining budget)` and itself is bounded.
- GREEN: protocol adversarial tests `5/5`; schedule form `43/43` in `14.549s`; focused publishing chain `334/334` in `15.784s`; explicit affected suite `551/551` in `17.221s`.
- Diff checks passed before the implementation commit. Implementation-only scope against `3d222d6` was `changed=2`, both overseas-owned, `scope-check: OK`.
- Current `origin/main` scope reports `changed=64` and `REVIEW_REQUIRED`: it includes the branch's pre-existing shared changes plus this required ignored-path report (`outside`). It is not an implementation scope failure.

Round 2 implementation: `dc020340f5fad7114228813b32be0e38a0b5e178` (`fix(tiktok): compare frozen schedule handles`)

## Review remediation (round 3)

- Frozen candidates now carry stable `upload` or `top_page` scope identity. Different scopes are never compared through ElementHandle evaluation and therefore remain distinct; same-scope handles retain exact evaluation de-duplication and stability checks.
- RED reproduced a cross-execution-context comparison failure. GREEN protocol coverage includes cross-scope ambiguity without comparison, same-scope same-DOM de-duplication, and top-page-only success.
- Results: schedule form `44/44` in `14.585s`; focused chain `335/335` in `15.809s`; affected suite `552/552` in `17.179s`. Implementation scope against `e9191b2` was `changed=2`, overseas-owned, `scope-check: OK`; diff checks passed.

Round 3 implementation: `f58a4609fc9373d6cedb0088047a6bfac86cafcf` (`fix(tiktok): isolate schedule control contexts`)

## Review remediation (round 4)

- Upload-frame A to B remounts now treat only the exact Playwright `JSHandles can be evaluated only in the context they were created` comparison error as a changed node; B must still be seen twice. Other comparison errors still propagate.
- GREEN: adversarial `4/4`; schedule form `45/45` in `14.492s`; focused `336/336` in `15.705s`; affected `553/553` in `17.119s`. Diff checks passed before commit.

Round 4 implementation: `db40891b3a8dddde60d18b3137476b6155f20cd5` (`fix(tiktok): tolerate upload frame remount`)

## Review remediation (round 5)

- RED used the real `playwright.async_api.Error` with the known JSHandle cross-context message; the prior RuntimeError-only handler let it escape.
- The upload remount exception is now accepted only when its type is Playwright `Error`, scope is `upload`, and its message exactly matches the known JSHandle context error. Other Playwright errors and other exceptions propagate.
- GREEN: adversarial `4/4`; schedule form `46/46` in `14.437s`; focused `337/337` in `15.627s`; affected `554/554` in `17.114s`. Implementation scope against `2a3b08e` was `changed=2`, overseas-owned, `scope-check: OK`; diff checks passed.

Round 5 implementation: `b1361f4427af3e7725b274bfb7d005f8ce562cb6` (`fix(tiktok): classify upload handle context errors`)

## Review remediation (round 6)

- Necessary correction: real Playwright errors may include a terminal `!` and the `ElementHandle.evaluate:` API prefix. Recognition remains upload-only, Playwright-Error-only, and full-string-only: optional exact API prefix, exact core sentence, optional `!`.
- RED: both variant messages escaped the round-5 narrow matcher. GREEN: short, `!`, and API-prefixed forms complete A-to-B-to-B safely; arbitrary prefix/suffix and unrelated errors still propagate.
- Results: schedule form `46/46` in `14.503s`; focused `337/337` in `15.733s`; affected `554/554` in `17.138s`. Implementation scope against `307d948` was `changed=2`, overseas-owned, `scope-check: OK`; diff checks passed.

Round 6 implementation: `f81381e2a8ec5f13fdac7693018f7bb51e79c3fc` (`fix(tiktok): match upload context error variants`)
