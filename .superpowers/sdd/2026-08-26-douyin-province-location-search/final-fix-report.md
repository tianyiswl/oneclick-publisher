# Douyin province location search final fix report

## Result

- Base: `6488459`.
- Final-review findings closed: Critical 1 / Important 4.
- Source version remains `0.5.10`.
- Real platform, publish, package, and install: **NOT RUN** by task boundary.

## Fixes

1. Province cache and display filtering now compare a parsed top-level province. Active province-plan queries carry a parent-province cache identity, so Qinghai's `海南joymark` cannot share visible associations with the Hainan-province root query. A bare `海南藏族自治州` address is treated as province-unknown rather than guessed as Hainan province.
2. Jilin's city subquery is now `吉林市joymark`; every supported province plan has unique subqueries and the Jilin plan round-trips through persistence.
3. A confirmed metadata-mode platform zero returns `platformResultCount=0`, `candidates=[]`, `hasMore=False`, and `stopReason=no_visible_load_more_control` through service, session, collector, and UI. Non-metadata empty results retain `candidate_empty`.
4. An internal `province_mode` crosses UI, collector, and session boundaries. Only that mode bypasses the legacy 10-load and 100-raw-identity synthesis; ordinary and direct-city calls keep the existing limits. Province completion still uses eligible root-filtered identities and platform-confirmed exhaustion.
5. Restart replay now uses the same three-action / 30-second province coordinator. Search reconstruction and replayed loads are distinct actions; unused replay count remains retryable at budget exhaustion, shortened pagination clears replay and advances safely, and completed replay stops before requesting an unsaved new page.

## TDD evidence

The new regressions were added and observed failing first for duplicate Jilin queries, cross-province cache identity, unsupported province-mode arguments, service-level zero-result failure, legacy pagination caps, and replay counts that never decreased. The implementation was then changed until those focused tests passed.

## Verification

- Mandatory focused boundaries: `16/16` PASS (`0.669s`). This includes service-to-UI true zero, legacy non-metadata empty, page 11, 100 raw identities, ordinary 10/100 limits, Hainan/Qinghai isolation, 100 wrong-province rows, nationwide unique plans, Jilin persistence, and four replay terminal/budget cases.
- Affected modules only: `752/752` PASS (`54.809s`) with:
  - `test_douyin_location_search_plan`
  - `test_douyin_location_cache`
  - `test_douyin_commerce_collectors`
  - `test_douyin_commerce_setup_state`
  - `test_douyin_commerce_batch_draft_service`
  - `test_douyin_commerce_service`
- `py_compile`: PASS for all changed Python source and test files.
- `git diff --check` and staged-diff whitespace check: PASS.
- Full suite: **NOT RUN**, as the final-fix brief reserves it for the verification agent.

## Self-review

- Confirmed ordinary/city calls omit `province_mode` at the UI boundary, preserve their public call shape, and still stop at the exact legacy limits.
- Confirmed province-context cache keys affect only candidate associations; root plan persistence remains on the original normalized root keyword.
- Confirmed top-level parsing fails closed for ambiguous prefecture-only addresses and still accepts explicit provinces, autonomous-region aliases, municipalities, and known bare province-plus-city forms.
- Confirmed metadata zero is accepted only after a mounted candidate panel is stably empty; ordinary non-metadata service behavior is unchanged.
- Confirmed replay search does not decrement saved load count, replay loads do, a click never exceeds three actions, remaining replay is retained, and replay completion does not fetch new unsaved data.
- No real account, browser platform check, publication, package, installer, or external write was performed.

## Remaining concern

The affected-module run emitted one non-failing `ResourceWarning` from an existing collector lifecycle test. The new real zero-result service-to-UI regression was also run separately with `-W error::ResourceWarning` and passed cleanly. Real Douyin behavior remains unverified until the later authorized platform check.
