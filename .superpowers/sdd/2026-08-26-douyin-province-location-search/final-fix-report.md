# Douyin province location search final fix report

## Result

- Round 1 base/head: `6488459..f23fd5e`.
- Round 2 base: `f23fd5e`.
- Round 3 base: `0a83496`.
- Round 2 closed the original open Important 1 / new Important 3 except for the
  brand full-keyword province boundary, which Round 3 now fixes and covers at
  the cache and real batch-UI entrances.
- Across all three rounds, the original Critical 1 / Important 4 and the three scoped re-review regressions are covered by passing offline regressions.
- Source version remains `0.5.10`.
- Real platform, publish, package, and install: **NOT RUN** by task boundary.

## Round 1 fixes

1. Province cache and display filtering now compare a parsed top-level province. Active province-plan queries carry a parent-province cache identity, so Qinghai's `海南joymark` cannot share visible associations with the Hainan-province root query. A bare `海南藏族自治州` address is treated as province-unknown rather than guessed as Hainan province.
2. Jilin's city subquery is now `吉林市joymark`; every supported province plan has unique subqueries and the Jilin plan round-trips through persistence.
3. A confirmed metadata-mode platform zero returns `platformResultCount=0`, `candidates=[]`, `hasMore=False`, and `stopReason=no_visible_load_more_control` through service, session, collector, and UI. Non-metadata empty results retain `candidate_empty`.
4. An internal `province_mode` crosses UI, collector, and session boundaries. Only that mode bypasses the legacy 10-load and 100-raw-identity synthesis; ordinary and direct-city calls keep the existing limits. Province completion still uses eligible root-filtered identities and platform-confirmed exhaustion.
5. Restart replay now uses the same three-action / 30-second province coordinator. Search reconstruction and replayed loads are distinct actions; unused replay count remains retryable at budget exhaustion, shortened pagination clears replay and advances safely, and completed replay stops before requesting an unsaved new page.

## Round 2 fixes

1. Province cache revalidation now enters the same three-action / 30-second coordinator as normal province pagination. The legacy common callback also applies its 10-load / 100-display synthesis only to non-province plans, so a non-exhausted province plan remains retryable at the old tenth-page boundary.
2. Top-level province parsing can safely infer a province from an address that starts with a uniquely owned prefecture city carrying the explicit `市` suffix, including `广州市… -> 广东`. Ambiguous cross-level names such as a province-less `海南藏族自治州…`, and bare district names such as `朝阳区…`, remain province-unknown. The full normalized keyword-in-store-name brand exception is restored, while the Qinghai-Hainan collision cannot use that exception.
3. Parent-province cache hashing is now limited to genuinely ambiguous active city keys. A Guangdong-plan `广州joymark` write uses the ordinary city association and can be read by a later direct-city search; Qinghai's `海南joymark` remains context-isolated from Hainan province.
4. Structured platform zero is now province-only and evidence-backed. Three early empty snapshots alone never complete the search; success requires repeated explicit empty-state evidence or an observed loading-to-complete transition. Ordinary/direct-city metadata searches keep waiting safely, and late candidates are accepted.

## Round 3 fix

1. The full normalized keyword-in-store-name exception is now limited to
   non-province searches. A root province plan or an active province-plan query
   carrying `province_context` must first match the candidate address's parsed
   top-level province. Consequently `广东joymark南京店 · 江苏省南京市…` cannot
   enter the Guangdong cache, display list, or automatic video assignment.
   `北京烤鸭` remains usable as a non-province brand phrase; province-less
   `广州市…` inference, Qinghai/Hainan isolation, and ordinary Guangzhou cache
   reuse are unchanged.

## TDD evidence

The new regressions were added and observed failing first for duplicate Jilin queries, cross-province cache identity, unsupported province-mode arguments, service-level zero-result failure, legacy pagination caps, and replay counts that never decreased. The implementation was then changed until those focused tests passed.

Round 2 added the compatibility regressions before production edits. The first run produced seven expected assertion failures and one expected missing-argument error: province-less Guangzhou and the brand exception were rejected, three early empties returned too soon, regular metadata accepted an empty page, revalidation did not enter the coordinator, the old tenth-page limit disabled the province plan, direct Guangzhou cache reuse returned zero, and the service lacked its province-only zero-result switch. The same ten-test set then passed. Self-review found one further bare-district ambiguity; its `朝阳区` regression failed first and passed after city inference was restricted to the explicit `市` suffix.

Round 3 added two generic non-Hainan regressions before the production edit.
The corrected red run produced the two expected assertion failures: the Jiangsu
candidate was associated with the `广东joymark` cache and was present in the
batch display/automatic-assignment candidate list. The same two tests passed
after the single filter-boundary change.

## Verification

- Mandatory focused boundaries: `16/16` PASS (`0.669s`). This includes service-to-UI true zero, legacy non-metadata empty, page 11, 100 raw identities, ordinary 10/100 limits, Hainan/Qinghai isolation, 100 wrong-province rows, nationwide unique plans, Jilin persistence, and four replay terminal/budget cases.
- Affected modules only: `752/752` PASS (`54.809s`) with:
  - `test_douyin_location_search_plan`
  - `test_douyin_location_cache`
  - `test_douyin_commerce_collectors`
  - `test_douyin_commerce_setup_state`
  - `test_douyin_commerce_batch_draft_service`
  - `test_douyin_commerce_service`
- Round 2 focused regressions: `11/11` PASS (`0.500s`).
- Round 2 retained boundary checks: `20/20` PASS plus the corrected nationwide planner selector `1/1` PASS.
- Round 2 affected modules only: `761/761` PASS (`54.302s`) with the same six modules above.
- Round 3 new regressions: `2/2` PASS (`0.390s`) after the expected `2/2`
  assertion-failure red run.
- Round 3 retained compatibility boundaries: `6/6` PASS (`0.438s`), covering
  `北京烤鸭`, province-less Guangzhou, Qinghai/Hainan isolation, and direct
  Guangzhou cache reuse.
- Round 3 affected modules only: `763/763` PASS (`54.324s`) with the same six
  modules above.
- The real service-to-session-to-collector-to-UI zero-result regression also passed separately with `-W error::ResourceWarning`.
- `py_compile`: PASS for all changed Python source and test files.
- `git diff --check`: PASS.
- Full suite: **NOT RUN**, as the final-fix brief reserves it for the verification agent.

## Self-review

- Confirmed ordinary/city calls omit `province_mode` at the UI boundary, preserve their public call shape, and still stop at the exact legacy limits.
- Confirmed province-context cache keys affect only candidate associations; root plan persistence remains on the original normalized root keyword.
- Confirmed non-ambiguous active city associations use the ordinary city key, while only cross-level collision keys such as Qinghai `海南joymark` retain the parent-province hash.
- Confirmed top-level parsing fails closed for ambiguous prefecture-only and bare-district addresses, and accepts explicit provinces, autonomous-region aliases, municipalities, and uniquely owned province-less prefecture cities with the explicit `市` suffix.
- Confirmed the brand full-keyword exception does not override the Qinghai/Hainan top-level province mismatch.
- Confirmed root province plans and province-context activity queries disable
  the brand full-keyword exception before candidate admission; the parsed
  top-level province remains the required boundary.
- Confirmed the real batch platform callback retains the unfiltered platform
  snapshot only for pagination context, while the Jiangsu candidate is absent
  from public display candidates, cache writes, and automatic assignments.
- Confirmed metadata zero is available only to `province_mode` after explicit platform evidence; ordinary/direct-city metadata and non-metadata behavior remain safe.
- Confirmed province revalidation enters the bounded coordinator and the common 10/100 synthesis remains limited to non-province searches.
- Confirmed replay search does not decrement saved load count, replay loads do, a click never exceeds three actions, remaining replay is retained, and replay completion does not fetch new unsaved data.
- No real account, browser platform check, publication, package, installer, or external write was performed.

## Remaining concern

The first two affected-module runs emitted one non-failing `ResourceWarning`
from an existing collector lifecycle test. The real zero-result service-to-UI
regression was run separately with `-W error::ResourceWarning` and passed
cleanly in both earlier rounds. The explicit empty-state DOM selectors are
conservative and all Round 3 province-boundary evidence remains offline; real
Douyin behavior is unverified until the later authorized platform check.
