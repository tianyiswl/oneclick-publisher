# -*- coding: utf-8 -*-

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from app_core.douyin_graphic_matrix_service import (
    DouyinGraphicMatrixError,
    effective_item_payload,
    matrix_scope_fingerprint,
    prepare_matrix,
    retry_matrix,
)


class DouyinGraphicMatrixServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.image_a = root / "01.jpg"
        self.image_b = root / "02.png"
        self.image_a.write_bytes(b"matrix-image-a")
        self.image_b.write_bytes(b"matrix-image-b")
        self.accounts = [
            {
                "id": 31,
                "type": 3,
                "profileName": "账号一",
                "filePath": "/private/account-31.json",
            },
            {
                "id": 32,
                "type": 3,
                "profileName": "账号二",
                "filePath": "/private/account-32.json",
            },
            {"id": 41, "type": 1, "profileName": "小红书账号"},
        ]
        self.now = datetime(
            2026, 8, 26, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _raw(self) -> dict:
        return {
            "schemaVersion": "oneclick-douyin-graphic-matrix/v1",
            "workflow": "douyin-graphic-matrix",
            "runtimeMode": "local_check",
            "content": {
                "images": [str(self.image_a), str(self.image_b)],
                "common": {
                    "title": " 通用标题 ",
                    "body": "通用正文",
                    "tags": ["#矩阵发布", " 门店经营 "],
                },
            },
            "schedule": {"startTime": "18:00", "intervalMinutes": 30},
            "targets": [
                {"itemIndex": 1, "accountId": 31, "overrides": {}},
                {
                    "itemIndex": 2,
                    "accountId": 32,
                    "overrides": {"title": "账号二标题"},
                },
            ],
        }

    def test_prepare_matrix_keeps_images_once_and_resolves_each_account(self) -> None:
        prepared = prepare_matrix(self._raw(), accounts=self.accounts, now=self.now)

        self.assertEqual(
            prepared["content"]["images"],
            [str(self.image_a.resolve()), str(self.image_b.resolve())],
        )
        self.assertEqual(prepared["targets"][0]["effective"]["title"], "通用标题")
        self.assertEqual(prepared["targets"][1]["effective"]["title"], "账号二标题")
        self.assertEqual(prepared["targets"][0]["accountLabel"], "账号一")
        self.assertNotIn(
            "filePath", json.dumps(prepared["targets"], ensure_ascii=False)
        )

    def test_default_schedule_is_beijing_tomorrow_and_override_does_not_reflow(self) -> None:
        raw = self._raw()
        raw["targets"].append(
            {
                "itemIndex": 3,
                "accountId": 32,
                "overrides": {},
                "scheduleTime": "2026-08-28 09:15",
                "scheduleOverridden": True,
            }
        )
        raw["targets"][1]["accountId"] = 31
        raw["targets"][0]["accountId"] = 32
        # The contract rejects duplicate accounts, so use a third saved Douyin account.
        self.accounts.append({"id": 33, "type": 3, "profileName": "账号三"})
        raw["targets"][2]["accountId"] = 33

        prepared = prepare_matrix(raw, accounts=self.accounts, now=self.now)

        self.assertEqual(
            [target["scheduleTime"] for target in prepared["targets"]],
            ["2026-08-27 18:00", "2026-08-27 18:30", "2026-08-28 09:15"],
        )
        self.assertFalse(prepared["targets"][0]["scheduleOverridden"])
        self.assertTrue(prepared["targets"][2]["scheduleOverridden"])

    def test_effective_payload_uses_only_one_account_and_shared_images(self) -> None:
        prepared = prepare_matrix(self._raw(), accounts=self.accounts, now=self.now)

        payload = effective_item_payload(prepared, prepared["targets"][1])

        self.assertEqual(payload["contentType"], "article")
        self.assertEqual(payload["accountIds"], [32])
        self.assertEqual(payload["fileList"], prepared["content"]["images"])
        self.assertEqual(payload["tags"], ["矩阵发布", "门店经营"])
        self.assertEqual(payload["scheduleTimezone"], "Asia/Shanghai")

    def test_fingerprint_changes_with_account_content_image_or_schedule(self) -> None:
        prepared = prepare_matrix(self._raw(), accounts=self.accounts, now=self.now)
        original = matrix_scope_fingerprint(prepared)

        for mutate in (
            lambda value: value["targets"][0]["effective"].update(title="另一个标题"),
            lambda value: value["targets"][0].update(scheduleTime="2026-08-27 19:00"),
            lambda value: value["content"]["imageHashes"].append("other"),
        ):
            changed = json.loads(json.dumps(prepared, ensure_ascii=False))
            mutate(changed)
            self.assertNotEqual(matrix_scope_fingerprint(changed), original)

    def test_retry_matrix_keeps_only_requested_original_item_indexes(self) -> None:
        prepared = prepare_matrix(self._raw(), accounts=self.accounts, now=self.now)

        retried = retry_matrix(prepared, [2])

        self.assertEqual([target["itemIndex"] for target in retried["targets"]], [2])
        self.assertEqual(retried["content"], prepared["content"])
        self.assertEqual(retried["runtimeMode"], "local_check")

    def test_rejects_duplicate_or_non_douyin_accounts_and_invalid_limits(self) -> None:
        cases: list[dict] = []
        duplicate = self._raw()
        duplicate["targets"][1]["accountId"] = 31
        cases.append(duplicate)
        wrong_platform = self._raw()
        wrong_platform["targets"][1]["accountId"] = 41
        cases.append(wrong_platform)
        no_images = self._raw()
        no_images["content"]["images"] = []
        cases.append(no_images)

        for raw in cases:
            with self.subTest(raw=raw):
                with self.assertRaises(DouyinGraphicMatrixError) as raised:
                    prepare_matrix(raw, accounts=self.accounts, now=self.now)
                self.assertEqual(
                    raised.exception.error_code, "douyin_graphic_matrix_invalid"
                )


if __name__ == "__main__":
    unittest.main()
