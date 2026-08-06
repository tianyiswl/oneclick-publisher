# -*- coding: utf-8 -*-
"""抖音地点预设的离线回归测试。"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_core.douyin_location_preset_service import (
    DouyinLocationPresetError,
    list_location_presets,
    match_location_preset,
    save_location_preset,
)


LOCATION = {"poiId": "p", "name": "银滩", "address": "广西北海市银海区银滩大道"}


class DouyinLocationPresetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_patch = patch(
            "app_core.database.DB_PATH",
            Path(self.temporary_directory.name) / "presets.sqlite3",
        )
        self.database_patch.start()

    def tearDown(self) -> None:
        self.database_patch.stop()
        self.temporary_directory.cleanup()

    def test_location_preset_is_account_scoped_and_requires_full_identity(self) -> None:
        preset = save_location_preset(7, LOCATION, "domestic")
        self.assertEqual(list_location_presets(7), [preset])
        self.assertEqual(list_location_presets(8), [])
        with self.assertRaisesRegex(DouyinLocationPresetError, "完整地址"):
            save_location_preset(7, {"poiId": "p", "name": "银滩"}, "domestic")

    def test_match_requires_one_exact_poi_name_and_address(self) -> None:
        preset = save_location_preset(7, LOCATION, "domestic")
        matched = match_location_preset(preset, [{**LOCATION, "distance": "2km"}])
        self.assertEqual(matched["poiId"], "p")
        with self.assertRaisesRegex(DouyinLocationPresetError, "未找到"):
            match_location_preset(preset, [{**LOCATION, "address": "其他地址"}])
        with self.assertRaisesRegex(DouyinLocationPresetError, "多个"):
            match_location_preset(preset, [LOCATION, LOCATION])


if __name__ == "__main__":
    unittest.main()
