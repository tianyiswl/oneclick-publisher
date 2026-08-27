import unittest

from app_core.douyin_location_search_plan import (
    MUNICIPALITIES,
    PROVINCE_CITIES,
    advance_after_page,
    build_location_search_plan,
    plan_progress_text,
    record_plan_error,
)


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

    def test_province_suffix_and_whitespace_build_the_same_plan(self):
        compact = build_location_search_plan("广东joymark")
        spaced = build_location_search_plan(" 广东省 joymark ")

        self.assertEqual(spaced.search_kind, "province")
        self.assertEqual(spaced.province, "广东")
        self.assertEqual(spaced.merchant_term, "joymark")
        self.assertEqual(spaced.original_keyword, "广东省joymark")
        self.assertEqual(spaced.subqueries[0], "广东省joymark")
        self.assertEqual(spaced.subqueries[1:], compact.subqueries[1:])

    def test_city_and_plain_queries_do_not_fan_out(self):
        self.assertEqual(
            build_location_search_plan("广州joymark").subqueries,
            ("广州joymark",),
        )
        self.assertEqual(
            build_location_search_plan("joymark").subqueries,
            ("joymark",),
        )

    def test_city_suffix_wins_when_city_name_matches_a_province(self):
        plan = build_location_search_plan("吉林市joymark")

        self.assertEqual(plan.search_kind, "city")
        self.assertEqual(plan.merchant_term, "joymark")
        self.assertEqual(plan.subqueries, ("吉林市joymark",))

    def test_brand_phrase_is_not_misread_as_city_prefix(self):
        plan = build_location_search_plan("夜南香北京烤鸭")

        self.assertEqual(plan.search_kind, "plain")

    def test_municipalities_and_non_mainland_regions_do_not_fan_out(self):
        self.assertEqual(build_location_search_plan("北京joymark").search_kind, "city")
        self.assertEqual(
            build_location_search_plan("北京joymark").subqueries,
            ("北京joymark",),
        )
        self.assertEqual(build_location_search_plan("香港joymark").search_kind, "plain")
        self.assertEqual(
            build_location_search_plan("香港joymark").subqueries,
            ("香港joymark",),
        )

    def test_every_supported_non_municipality_has_unique_city_names(self):
        self.assertEqual(
            set(PROVINCE_CITIES),
            {
                "河北", "山西", "辽宁", "吉林", "黑龙江", "江苏", "浙江", "安徽", "福建",
                "江西", "山东", "河南", "湖北", "湖南", "广东", "海南", "四川", "贵州",
                "云南", "陕西", "甘肃", "青海", "内蒙古", "广西", "西藏", "宁夏", "新疆",
            },
        )
        self.assertTrue(MUNICIPALITIES.isdisjoint(PROVINCE_CITIES))
        for province, cities in PROVINCE_CITIES.items():
            with self.subTest(province=province):
                self.assertTrue(cities)
                self.assertEqual(len(cities), len(set(cities)))

    def test_every_province_plan_has_unique_platform_subqueries(self):
        """省名与地级市同名时，根词与城市词也不得重复。"""

        for province in PROVINCE_CITIES:
            with self.subTest(province=province):
                plan = build_location_search_plan(f"{province}joymark")
                self.assertEqual(plan.search_kind, "province")
                self.assertEqual(len(plan.subqueries), len(set(plan.subqueries)))

        self.assertIn(
            "吉林市joymark",
            build_location_search_plan("吉林joymark").subqueries,
        )

    def test_hubei_plan_contains_only_prefecture_level_regions(self):
        plan = build_location_search_plan("湖北joymark")

        self.assertEqual(len(plan.subqueries[1:]), 13)
        self.assertNotIn("仙桃joymark", plan.subqueries)
        self.assertNotIn("神农架joymark", plan.subqueries)

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
        self.assertEqual(stopped.completed_indices, tuple(range(len(stopped.subqueries))))

    def test_recording_platform_error_preserves_retryable_position(self):
        loaded = advance_after_page(
            build_location_search_plan("广东joymark"),
            has_more=True,
            eligible_total=4,
        )

        errored = record_plan_error(
            loaded,
            "province_location_search_action_timeout",
        )

        self.assertEqual(errored.current_index, loaded.current_index)
        self.assertEqual(errored.current_load_count, loaded.current_load_count)
        self.assertEqual(errored.completed_indices, loaded.completed_indices)
        self.assertEqual(
            errored.last_error_code,
            "province_location_search_action_timeout",
        )
        retried = advance_after_page(errored, has_more=True, eligible_total=4)
        self.assertEqual(retried.current_index, loaded.current_index)
        self.assertEqual(retried.current_load_count, loaded.current_load_count + 1)
        self.assertEqual(retried.completed_indices, loaded.completed_indices)
        self.assertEqual(retried.last_error_code, "")

    def test_progress_text_names_current_city_and_eligible_total(self):
        plan = advance_after_page(
            build_location_search_plan("广东joymark"),
            has_more=False,
            eligible_total=4,
        )

        self.assertEqual(
            plan_progress_text(plan, filter_label="返佣", eligible_total=18),
            "广东已检查 0/21 个城市 · 当前广州 · 已找到返佣地址 18 个",
        )


if __name__ == "__main__":
    unittest.main()
