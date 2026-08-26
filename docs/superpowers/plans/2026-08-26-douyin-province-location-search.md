# Douyin Province Location Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a province-prefixed Douyin commerce location query search every prefecture-level city on demand, resume after restart, and stop only after all cities are exhausted or 100 eligible unique locations are collected.

**Architecture:** Add a pure search-plan state machine that distinguishes ordinary, city, and province queries. Persist only province-plan progress in SQLite, keep the existing candidate cache keyed by the original query, and let the UI drive one current platform subquery at a time through the existing collector/session APIs.

**Tech Stack:** Python 3, dataclasses, SQLite, PySide6, unittest, existing Douyin Playwright collector/session layer.

**Spec:** `docs/superpowers/specs/2026-08-25-douyin-province-location-search-design.md`

## Global Constraints

- Ordinary merchant queries and city-prefixed queries keep their existing one-query behavior.
- Province queries cover every prefecture-level city in a deterministic order; municipalities remain city queries.
- Each user click performs at most 3 platform actions or 30 seconds and returns immediately after at least one new eligible candidate.
- “Two consecutive zero-growth batches” is not a province-search terminal condition.
- The 100-candidate limit counts only province-matching, commission-matching, unique public candidates.
- Platform timeouts or context loss keep the current subquery retryable and never mark it exhausted.
- Manual location choices are preserved; changing the original query clears only locations auto-filled by the old query.
- Existing formal publish revalidation and final-confirmation boundaries do not change.
- No real platform publish or submission is part of this plan.

---

### Task 1: Pure province/city query planner

**Files:**
- Create: `app_core/douyin_location_search_plan.py`
- Create: `test_douyin_location_search_plan.py`

**Interfaces:**
- Produces: `build_location_search_plan(keyword: object) -> LocationSearchPlan`
- Produces: `advance_after_page(plan: LocationSearchPlan, *, has_more: bool, eligible_total: int) -> LocationSearchPlan`
- Produces: `plan_progress_text(plan: LocationSearchPlan, *, filter_label: str, eligible_total: int) -> str`
- `LocationSearchPlan` is an immutable dataclass with `original_keyword`, `search_kind`, `province`, `merchant_term`, `subqueries`, `current_index`, `current_load_count`, `completed_indices`, and `last_error_code`.

- [ ] **Step 1: Write failing parser and plan tests**

```python
class DouyinLocationSearchPlanTests(unittest.TestCase):
    def test_guangdong_query_builds_all_prefecture_city_subqueries(self):
        plan = build_location_search_plan("广东joymark")
        self.assertEqual(plan.search_kind, "province")
        self.assertEqual(plan.province, "广东")
        self.assertEqual(plan.merchant_term, "joymark")
        self.assertEqual(plan.subqueries[0], "广东joymark")
        self.assertEqual(len(plan.subqueries[1:]), 21)
        self.assertIn("广州joymark", plan.subqueries)
        self.assertIn("深圳joymark", plan.subqueries)
        self.assertEqual(len(set(plan.subqueries)), len(plan.subqueries))

    def test_city_and_plain_queries_do_not_fan_out(self):
        self.assertEqual(
            build_location_search_plan("广州joymark").subqueries,
            ("广州joymark",),
        )
        self.assertEqual(
            build_location_search_plan("joymark").subqueries,
            ("joymark",),
        )

    def test_brand_phrase_is_not_misread_as_city_prefix(self):
        plan = build_location_search_plan("夜南香北京烤鸭")
        self.assertEqual(plan.search_kind, "plain")
```

- [ ] **Step 2: Run the new tests and confirm the module is missing**

Run: `../../.venv/bin/python -m unittest -v test_douyin_location_search_plan`

Expected: FAIL with `ModuleNotFoundError: app_core.douyin_location_search_plan`.

- [ ] **Step 3: Implement the immutable planner and complete nationwide map**

```python
@dataclass(frozen=True)
class LocationSearchPlan:
    schema_version: int
    original_keyword: str
    search_kind: str
    province: str
    merchant_term: str
    subqueries: tuple[str, ...]
    current_index: int = 0
    current_load_count: int = 0
    completed_indices: tuple[int, ...] = ()
    last_error_code: str = ""

    @property
    def current_keyword(self) -> str:
        return self.subqueries[self.current_index]

    @property
    def exhausted(self) -> bool:
        return len(self.completed_indices) == len(self.subqueries)


def build_location_search_plan(keyword: object) -> LocationSearchPlan:
    normalized = _normalize_keyword(keyword)
    province, merchant = _leading_region(normalized, PROVINCE_CITIES)
    if province and province not in MUNICIPALITIES and merchant:
        subqueries = (normalized,) + tuple(
            f"{city}{merchant}" for city in PROVINCE_CITIES[province]
        )
        return LocationSearchPlan(1, normalized, "province", province, merchant, subqueries)
    city, city_merchant = _leading_city(normalized)
    kind = "city" if city and city_merchant else "plain"
    return LocationSearchPlan(1, normalized, kind, "", city_merchant, (normalized,))
```

Implement the full mainland province/autonomous-region map in the same module. Direct-controlled municipalities are `北京`, `天津`, `上海`, and `重庆`; Hong Kong, Macau, and Taiwan remain direct one-query searches.

- [ ] **Step 4: Add state-transition tests and minimal transition logic**

```python
def test_empty_page_moves_to_next_city_only_when_platform_is_exhausted(self):
    plan = build_location_search_plan("广东joymark")
    same = advance_after_page(plan, has_more=True, eligible_total=4)
    self.assertEqual(same.current_index, 0)
    self.assertEqual(same.current_load_count, 1)
    next_city = advance_after_page(same, has_more=False, eligible_total=4)
    self.assertEqual(next_city.current_index, 1)
    self.assertIn(0, next_city.completed_indices)

def test_one_hundred_eligible_candidates_is_terminal(self):
    plan = build_location_search_plan("广东joymark")
    stopped = advance_after_page(plan, has_more=True, eligible_total=100)
    self.assertTrue(stopped.exhausted)
```

Use `dataclasses.replace()` for transitions. Represent the 100-item stop by marking every subquery completed; never use zero-growth count as a terminal input.

- [ ] **Step 5: Run Task 1 tests**

Run: `../../.venv/bin/python -m unittest -v test_douyin_location_search_plan`

Expected: PASS.

- [ ] **Step 6: Commit Task 1**

```bash
git add app_core/douyin_location_search_plan.py test_douyin_location_search_plan.py
git commit -m "feat: add province location search planner"
```

---

### Task 2: Persist plan progress and dual keyword associations

**Files:**
- Modify: `app_core/database.py:242-325`
- Modify: `app_core/douyin_location_cache.py`
- Modify: `test_douyin_location_cache.py`

**Interfaces:**
- Consumes: `LocationSearchPlan` from Task 1.
- Produces: `load_location_search_plan(query: LocationCacheQuery) -> LocationSearchPlan | None`
- Produces: `save_location_search_plan(query: LocationCacheQuery, plan: LocationSearchPlan, *, eligible_total: int) -> None`
- Produces: `merge_platform_locations_for_queries(queries: list[LocationCacheQuery], candidates: list[Mapping[str, Any]]) -> dict[str, Any]`

Add these local test helpers at the top of `DouyinLocationCacheTests`:

```python
def commission_candidate(poi_id: str) -> dict[str, object]:
    return {
        "poiId": poi_id,
        "name": f"JOYMARK {poi_id}",
        "address": "广东省广州市测试路1号",
        "commissionType": "commission",
    }

def location_query(account_id: str, keyword: str, commission_filter: str):
    return LocationCacheQuery(account_id, "domestic", keyword, commission_filter)
```

- [ ] **Step 1: Write failing schema and round-trip tests**

```python
def test_location_search_progress_round_trips_atomically(self):
    query = LocationCacheQuery("7", "domestic", "广东joymark", "commission")
    plan = advance_after_page(
        build_location_search_plan("广东joymark"),
        has_more=False,
        eligible_total=4,
    )
    save_location_search_plan(query, plan, eligible_total=4)
    restored = load_location_search_plan(query)
    self.assertEqual(restored, plan)

def test_same_candidate_can_be_associated_with_province_and_city_queries(self):
    province = LocationCacheQuery("7", "domestic", "广东joymark", "commission")
    city = LocationCacheQuery("7", "domestic", "广州joymark", "commission")
    merge_platform_locations_for_queries([province, city], [commission_candidate("gd-1")])
    self.assertEqual(get_cached_locations(province, excluded_identities=[])["total"], 1)
    self.assertEqual(get_cached_locations(city, excluded_identities=[])["total"], 1)
```

- [ ] **Step 2: Run the focused persistence tests and confirm missing APIs**

Run: `../../.venv/bin/python -m unittest -v test_douyin_location_cache.DouyinLocationCacheTests.test_location_search_progress_round_trips_atomically test_douyin_location_cache.DouyinLocationCacheTests.test_same_candidate_can_be_associated_with_province_and_city_queries`

Expected: FAIL because the table and functions do not exist.

- [ ] **Step 3: Add the progress table and indexes**

```sql
CREATE TABLE IF NOT EXISTS douyin_location_search_progress (
    accountId TEXT NOT NULL,
    scope TEXT NOT NULL,
    keyword TEXT NOT NULL,
    commissionFilter TEXT NOT NULL,
    planJson TEXT NOT NULL,
    eligibleTotal INTEGER NOT NULL,
    updatedAt TEXT NOT NULL,
    PRIMARY KEY(accountId, scope, keyword, commissionFilter)
);
```

Add a `CHECK(eligibleTotal >= 0 AND eligibleTotal <= 100)` constraint. Store one complete JSON document per transaction; reject unknown `schemaVersion` on read rather than silently inventing progress.

- [ ] **Step 4: Implement serialization and multi-query association in one transaction**

```python
def merge_platform_locations_for_queries(queries, candidates):
    safe_queries = [_query(item) for item in queries]
    if not safe_queries:
        raise DouyinLocationCacheError("地点缓存查询不能为空")
    with database.connect() as conn:
        normalized = [_candidate(item) for item in candidates]
        location_ids = _merge_candidates_in_connection(conn, safe_queries[0], normalized)
        for query in safe_queries:
            _associate_keywords_in_connection(conn, query, location_ids)
    return get_cached_locations(safe_queries[0], excluded_identities=[])
```

Extract only transaction-local helpers needed by both the existing single-query merge and the new multi-query merge. Do not duplicate candidate validation, eviction, or keyword-position logic.

- [ ] **Step 5: Add retryability and isolation tests**

```python
def test_progress_isolated_by_account_keyword_and_commission_filter(self):
    plan = build_location_search_plan("广东joymark")
    save_location_search_plan(location_query("7", "广东joymark", "commission"), plan, eligible_total=4)
    self.assertIsNone(load_location_search_plan(location_query("8", "广东joymark", "commission")))
    self.assertIsNone(load_location_search_plan(location_query("7", "广西joymark", "commission")))
    self.assertIsNone(load_location_search_plan(location_query("7", "广东joymark", "all")))

def test_invalid_progress_json_is_reported_instead_of_marked_complete(self):
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO douyin_location_search_progress "
            "(accountId, scope, keyword, commissionFilter, planJson, eligibleTotal, updatedAt) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("7", "domestic", "广东joymark", "commission", "{broken", 0, "2026-08-26T00:00:00+00:00"),
        )
    with self.assertRaisesRegex(DouyinLocationCacheError, "进度"):
        load_location_search_plan(location_query("7", "广东joymark", "commission"))
```

- [ ] **Step 6: Run the cache module tests**

Run: `../../.venv/bin/python -m unittest -v test_douyin_location_cache`

Expected: PASS.

- [ ] **Step 7: Commit Task 2**

```bash
git add app_core/database.py app_core/douyin_location_cache.py test_douyin_location_cache.py
git commit -m "feat: persist province location search progress"
```

---

### Task 3: Start and restore province plans in the commerce UI

**Files:**
- Modify: `ui/douyin_commerce_page.py:2043-2680`
- Modify: `app_core/douyin_commerce_batch_draft_service.py`
- Modify: `test_douyin_commerce_service.py`
- Modify: `test_douyin_commerce_batch_draft_service.py`

**Interfaces:**
- Consumes: planner and persistence APIs from Tasks 1-2.
- Extends the shared location state with `searchPlan`, `rootKeyword`, `activeKeyword`, `eligibleIdentityCount`, `actionsThisClick`, and `replayLoadsRemaining`.
- Platform collectors continue receiving one exact `activeKeyword`; their API does not learn about provinces.

- [ ] **Step 1: Write failing UI tests for initial plan creation and restore**

```python
def test_province_search_uses_root_keyword_first_and_restores_saved_plan(self):
    saved = replace(build_location_search_plan("广东joymark"), current_index=2)
    with patch.object(douyin_location_cache, "load_location_search_plan", return_value=saved), \
         patch.object(self.page, "_start_batch_location_platform_search", return_value=True) as start:
        self.page._search_batch_locations("domestic", "广东joymark")
    state = self.page._batch_location_state()
    self.assertEqual(state["rootKeyword"], "广东joymark")
    self.assertEqual(state["activeKeyword"], saved.subqueries[2])
    start.assert_called_once()

def test_city_search_keeps_single_query_state(self):
    plan = build_location_search_plan("广州joymark")
    self.assertEqual(plan.search_kind, "city")
    self.assertEqual(plan.subqueries, ("广州joymark",))
```

- [ ] **Step 2: Run the focused UI tests and verify they fail**

Run: `QT_QPA_PLATFORM=offscreen ../../.venv/bin/python -m unittest -v test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_province_search_uses_root_keyword_first_and_restores_saved_plan test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_city_search_keeps_single_query_state`

Expected: FAIL because shared state has no plan fields and always sends the root keyword.

- [ ] **Step 3: Build or restore the plan before the first platform call**

```python
plan = douyin_location_cache.load_location_search_plan(cache_query)
if plan is None:
    plan = build_location_search_plan(normalized_keyword)
active_keyword = plan.current_keyword
state.update({
    "searchPlan": plan,
    "rootKeyword": normalized_keyword,
    "activeKeyword": active_keyword,
    "eligibleIdentityCount": len(cached_candidates),
    "actionsThisClick": 0,
    "replayLoadsRemaining": plan.current_load_count,
})
```

Keep `cache_query.keyword` as the original root keyword. Construct a separate `active_query` only for associating a city cache key; pass `activeKeyword` to the collector/session search call. A browser listbox cannot survive process restart, so a restored plan first opens `activeKeyword` again and replays `replayLoadsRemaining` platform pages before it asks for a genuinely new page. Replayed rows are deduplicated against the root cache and do not count as new growth.

- [ ] **Step 4: Make platform success merge accepted candidates under both keys**

```python
queries = [root_cache_query]
if state["activeKeyword"] != state["rootKeyword"]:
    queries.append(
        LocationCacheQuery(
            root_cache_query.account_id,
            root_cache_query.scope,
            state["activeKeyword"],
            root_cache_query.commission_filter,
        )
    )
merge_platform_locations_for_queries(queries, accepted_candidates)
```

The displayed list remains the root province aggregate. `platformCandidates` remains only the current active subquery snapshot because the session/collector paging contract requires an exact snapshot for that one platform listbox.

- [ ] **Step 5: Add query-change tests for manual versus automatic assignments**

```python
def test_new_root_keyword_clears_old_auto_assignments_but_preserves_manual(self):
    auto_path, manual_path = "/tmp/auto.mp4", "/tmp/manual.mp4"
    self.page._batch_locations[auto_path] = {"poiId": "old-auto", "name": "旧自动", "address": "广东旧地址"}
    self.page._batch_locations[manual_path] = {"poiId": "manual", "name": "手动", "address": "用户手动地址"}
    self.page._batch_location_assignment_sources[auto_path] = "auto:广东joymark"
    self.page._batch_location_assignment_sources[manual_path] = "manual"
    self.page._search_batch_locations("domestic", "广西joymark")
    self.assertNotIn(auto_path, self.page._batch_locations)
    self.assertIn(manual_path, self.page._batch_locations)
```

Track assignment provenance in the existing batch draft payload with only `manual` or `auto:<normalized-root-keyword>`. Absence of provenance in legacy drafts is treated as manual to avoid deleting user data.

- [ ] **Step 6: Run Task 3 UI tests**

Run: `QT_QPA_PLATFORM=offscreen ../../.venv/bin/python -m unittest -v test_douyin_commerce_service.DouyinCommerceBatchUiTests`

Expected: PASS.

- [ ] **Step 7: Commit Task 3**

```bash
git add ui/douyin_commerce_page.py test_douyin_commerce_service.py app_core/douyin_commerce_batch_draft_service.py test_douyin_commerce_batch_draft_service.py
git commit -m "feat: route province searches through city plans"
```

---

### Task 4: Bounded multi-action “load more” driver

**Files:**
- Modify: `ui/douyin_commerce_page.py:2680-3245`
- Modify: `test_douyin_commerce_service.py`
- Modify if stable diagnostics need mapping: `app_core/douyin_commerce_setup_state.py`

**Interfaces:**
- Consumes: `advance_after_page()` and persisted plan APIs.
- Produces: `_continue_province_location_click(request_owner, *, started_at: float, actions_used: int) -> None`.
- Produces: `_run_current_location_action(request_owner, *, on_success: Callable[[Mapping[str, object]], None]) -> bool`.
- Produces: `_accept_province_location_page(request_owner, rows: Mapping[str, object], *, started_at: float, actions_used: int) -> None`.
- Produces: `_province_location_action_failed(message: object, *, request_token: int) -> None`.
- Produces: `_finish_province_location_click(request_owner, *, terminal: bool) -> None`.
- Existing `_start_batch_location_platform_search()` and `_start_batch_location_platform_load_more()` still execute exactly one platform action per call.

Add these test-only helpers inside `DouyinCommerceBatchUiTests` so every regression uses explicit public candidate data:

```python
def _candidate(poi_id, address="广东省广州市测试路1号", commission="commission"):
    return {"poiId": poi_id, "name": f"JOYMARK {poi_id}", "address": address, "commissionType": commission}

def _page(candidates, *, has_more):
    return {
        "platformResultCount": len(candidates),
        "candidates": list(candidates),
        "newCandidateCount": len(candidates),
        "hasMore": has_more,
        "stopReason": "loaded" if has_more else "no_visible_load_more_control",
    }

def _candidate_ids(state):
    return [item["poiId"] for item in state["candidates"]]

def _install_province_fixture(self, keyword, *, at_last_subquery=False):
    plan = build_location_search_plan(keyword)
    if at_last_subquery:
        plan = replace(
            plan,
            current_index=len(plan.subqueries) - 1,
            completed_indices=tuple(range(len(plan.subqueries) - 1)),
        )
    state = self.page._batch_location_state()
    state.update({
        "accountId": "7",
        "scope": "domestic",
        "keyword": keyword,
        "rootKeyword": keyword,
        "activeKeyword": plan.current_keyword,
        "commissionFilter": "commission",
        "searchPlan": plan,
        "candidates": [],
        "rawCandidates": [],
        "platformCandidates": [],
        "platformContextReady": False,
        "hasMore": True,
    })
    self.page._batch_location_searches["__shared_location_search__"] = state
    query = douyin_location_cache.LocationCacheQuery(
        "7", "domestic", keyword, "commission"
    )
    return self.page._batch_location_request_owner(
        query, self.page._batch_location_search_token
    )
```

- [ ] **Step 1: Write failing bounded-driver regression tests**

```python
def test_one_click_skips_empty_city_batches_until_new_eligible_candidate(self):
    self._install_province_fixture("广东joymark")
    responses = [
        _page([], has_more=False),
        _page([_candidate("js-1", address="江苏省南京市测试路1号")], has_more=False),
        _page([_candidate("gz-1")], has_more=True),
    ]
    def dispatch(_owner, *, on_success):
        on_success(responses.pop(0))
        return True
    with patch.object(self.page, "_run_current_location_action", side_effect=dispatch):
        self.page._load_more_batch_locations()
    self.assertIn("gz-1", _candidate_ids(self.page._batch_location_state()))
    self.assertTrue(self.page.batch_location_load_more_button.isEnabled())

def test_three_empty_actions_return_retryable_not_complete(self):
    self._install_province_fixture("广东joymark")
    def dispatch(_owner, *, on_success):
        on_success(_page([], has_more=False))
        return True
    with patch.object(self.page, "_run_current_location_action", side_effect=dispatch):
        self.page._load_more_batch_locations()
    self.assertIn("仍可继续加载", self.page.batch_item_settings_status.text())
    self.assertTrue(self.page.batch_location_load_more_button.isEnabled())

def test_all_subqueries_exhausted_disables_button(self):
    owner = self._install_province_fixture("广东joymark", at_last_subquery=True)
    self.page._accept_province_location_page(
        owner,
        _page([], has_more=False),
        started_at=time.monotonic(),
        actions_used=1,
    )
    self.assertIn("已加载全部地址", self.page.batch_item_settings_status.text())
    self.assertFalse(self.page.batch_location_load_more_button.isEnabled())
```

- [ ] **Step 2: Run the three regressions and confirm current one-page behavior fails**

Run: `QT_QPA_PLATFORM=offscreen ../../.venv/bin/python -m unittest -v test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_one_click_skips_empty_city_batches_until_new_eligible_candidate test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_three_empty_actions_return_retryable_not_complete test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_all_subqueries_exhausted_disables_button`

Expected: FAIL because current logic drives one keyword and treats `hasMore=False` as global completion.

- [ ] **Step 3: Implement the per-click budget and city handoff**

```python
_PROVINCE_CLICK_MAX_ACTIONS = 3
_PROVINCE_CLICK_MAX_SECONDS = 30.0

def _province_click_can_continue(self, *, started_at, actions_used):
    return (
        actions_used < _PROVINCE_CLICK_MAX_ACTIONS
        and time.monotonic() - started_at < _PROVINCE_CLICK_MAX_SECONDS
    )

def _continue_province_location_click(self, owner, *, started_at, actions_used):
    state = self._batch_location_state()
    plan = state["searchPlan"]
    if plan.exhausted or len(state["candidates"]) >= 100:
        self._finish_province_location_click(owner, terminal=True)
        return
    if not self._province_click_can_continue(started_at=started_at, actions_used=actions_used):
        self._finish_province_location_click(owner, terminal=False)
        return
    self._run_current_location_action(
        owner,
        on_success=lambda rows: self._accept_province_location_page(
            owner, rows, started_at=started_at, actions_used=actions_used + 1
        ),
    )
```

After an exhausted page, atomically save the advanced plan, clear only the current platform snapshot, then start a new platform search for `plan.current_keyword`. After a non-exhausted page, use the existing load-more action with the exact current snapshot.

- [ ] **Step 4: Implement effective-growth and progress text rules**

```python
before = {
    self._batch_location_candidate_identity(item)
    for item in state["candidates"]
}
regional = douyin_location_cache.filter_locations_for_search_keyword(
    state["rootKeyword"], rows["candidates"]
)
accepted = filter_location_candidates(regional, state["commissionFilter"])
after_candidates = self._merge_batch_location_candidates(
    state["candidates"], accepted
)
effective_growth = len({
    self._batch_location_candidate_identity(item)
    for item in after_candidates
} - before)
state["eligibleIdentityCount"] = len(after_candidates)
state["searchPlan"] = advance_after_page(
    state["searchPlan"],
    has_more=rows["hasMore"],
    eligible_total=len(after_candidates),
)
```

If `effective_growth > 0`, stop the current click and render. If it is zero and the click budget remains, continue inside the same click. Display `plan_progress_text()` after every accepted page.

- [ ] **Step 5: Add error recovery tests and fixed diagnostics**

```python
def test_timeout_keeps_current_city_retryable(self):
    self._install_province_fixture("广东joymark")
    before = self.page._batch_location_state()["searchPlan"]
    self.page._province_location_action_failed(
        "province_location_search_action_timeout",
        request_token=self.page._batch_location_search_token,
    )
    after = self.page._batch_location_state()["searchPlan"]
    self.assertEqual(after.current_index, before.current_index)
    self.assertTrue(self.page.batch_location_load_more_button.isEnabled())

def test_context_loss_does_not_mark_city_exhausted(self):
    self._install_province_fixture("广东joymark")
    self.page._province_location_action_failed(
        "province_location_search_context_lost",
        request_token=self.page._batch_location_search_token,
    )
    self.assertNotIn(
        self.page._batch_location_state()["searchPlan"].current_index,
        self.page._batch_location_state()["searchPlan"].completed_indices,
    )
```

Map collector/session timeout and context mismatch messages to the stable public codes defined in the spec. Preserve the original safe diagnostic in the local execution log, never cookies or DOM content.

- [ ] **Step 6: Run location UI, session, and collector tests**

Run: `QT_QPA_PLATFORM=offscreen ../../.venv/bin/python -m unittest -v test_douyin_commerce_service.DouyinCommerceBatchUiTests test_douyin_commerce_service.DouyinCommerceSessionContractTests test_douyin_commerce_collectors.DouyinCommerceCollectorManagerTests`

Expected: PASS.

- [ ] **Step 7: Commit Task 4**

```bash
git add ui/douyin_commerce_page.py app_core/douyin_commerce_setup_state.py test_douyin_commerce_service.py
git commit -m "fix: continue province location searches across cities"
```

---

### Task 5: Regression closure, version, and current-status evidence

**Files:**
- Modify: `app_core/branding.py:10`
- Modify: `SOURCE_OF_TRUTH.md`
- Modify only if required by final test expectations: `test_workstream_scope.py`
- Create: `docs/superpowers/reports/2026-08-26-douyin-province-location-search-verification.md`

**Interfaces:**
- Consumes all prior tasks.
- Produces source candidate version `0.5.10`; no installer is created unless separately requested.

- [ ] **Step 1: Run the smallest new regression set**

Run:

```bash
QT_QPA_PLATFORM=offscreen ../../.venv/bin/python -m unittest -v \
  test_douyin_location_search_plan \
  test_douyin_location_cache \
  test_douyin_commerce_service.DouyinCommerceBatchUiTests
```

Expected: PASS. Stop and repair immediately if any test fails.

- [ ] **Step 2: Run affected platform-setting modules**

Run:

```bash
QT_QPA_PLATFORM=offscreen ../../.venv/bin/python -m unittest -v \
  test_douyin_commerce_collectors \
  test_douyin_commerce_setup_state \
  test_douyin_commerce_batch_draft_service \
  test_douyin_commerce_service \
  test_douyin_location_cache
```

Expected: PASS.

- [ ] **Step 3: Bump the source candidate version to 0.5.10**

```python
APP_VERSION = "0.5.10"
```

Update `SOURCE_OF_TRUTH.md` to say exactly: the source candidate contains province-wide city fan-out and local tests; it has not been packaged and has not been verified on a real commerce-capable Douyin account.

- [ ] **Step 4: Run the full offline suite only at final closure**

Run: `QT_QPA_PLATFORM=offscreen ../../.venv/bin/python -m unittest discover -v`

Expected: all discovered tests PASS. Record the exact passed-test count rather than copying the previous `1940/1940` count.

- [ ] **Step 5: Run source client and scope checks without platform actions**

Run:

```bash
QT_QPA_PLATFORM=offscreen ../../.venv/bin/python desktop_native_app.py
../../.venv/bin/python -m unittest -v test_workstream_scope test_source_live_launcher test_source_live_runtime
git diff --check
```

Expected: desktop self-check returns `NATIVE_DESKTOP_UI_OK`; scope/source-live tests PASS; `git diff --check` prints nothing.

- [ ] **Step 6: Write the verification report**

Include these exact evidence categories:

```markdown
- Source version: 0.5.10
- Focused tests: command, test count, PASS/FAIL
- Affected modules: command, test count, PASS/FAIL
- Full suite: command, test count, PASS/FAIL
- Source client self-check: exact output
- Real Douyin commerce account: NOT RUN
- Package/installer: NOT BUILT
- Formal publish: NOT RUN
```

- [ ] **Step 7: Commit Task 5**

```bash
git add app_core/branding.py SOURCE_OF_TRUTH.md test_workstream_scope.py docs/superpowers/reports/2026-08-26-douyin-province-location-search-verification.md
git commit -m "chore: verify province location search 0.5.10"
```

- [ ] **Step 8: Report the one remaining real-world check**

State that the source fix is locally complete only if every preceding command passed. The next real check is for the user to run `广东joymark` on a commerce-capable account and confirm that repeated clicks move through cities and add eligible locations; do not call that platform check complete until its UI/log evidence is read back.
