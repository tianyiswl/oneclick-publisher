# -*- coding: utf-8 -*-

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app_core.controlled_publish import (
    ControlledPublishError,
    build_controlled_payloads,
    consume_authorization,
    create_authorization,
    create_authorization_schema,
    project_task,
    scope_fingerprint,
)


class ControlledPublishTests(unittest.TestCase):
    def _bundle(self, root: Path) -> Path:
        (root / "video.mp4").write_bytes(b"video")
        (root / "cover.png").write_bytes(b"png")
        (root / "正文.md").write_text(
            "# 硅基探索 008\n\n## 抖音\n预览\n\n## 小红书\n预览",
            encoding="utf-8",
        )
        manifest = root / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "schemaVersion": "oneclick-content/v1",
                    "contentType": "video",
                    "title": "预览标题",
                    "bodyFile": "正文.md",
                    "tags": ["预览标签"],
                    "assets": ["video.mp4"],
                    "covers": {"3:4": "cover.png"},
                    "preferredPlatforms": ["抖音", "小红书"],
                    "platformOverrides": {
                        "抖音": {"title": "抖音标题", "body": "抖音正文", "tags": ["抖音话题"]},
                        "小红书": {"title": "小红书标题", "body": "小红书正文", "tags": ["小红书话题"]},
                    },
                    "debugDryRun": True,
                    "publishAllowed": False,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return manifest

    @staticmethod
    def _accounts() -> list[dict]:
        return [
            {"id": 31, "type": 3, "filePath": "douyin.json", "profileName": "抖音主体", "userName": "抖音主体"},
            {"id": 11, "type": 1, "filePath": "xhs.json", "profileName": "小红书主体", "userName": "小红书主体"},
        ]

    def test_builds_clean_mixed_schedule_preflight_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._bundle(Path(temporary))
            payloads = build_controlled_payloads(
                {
                    "manifestPath": str(manifest),
                    "mode": "preflight",
                    "targets": [
                        {"platform": "抖音", "accountId": 31, "schedule": None},
                        {"platform": "小红书", "accountId": 11, "schedule": {"localTime": "2026-08-24 20:30", "timezone": "Asia/Shanghai"}},
                    ],
                },
                accounts=self._accounts(),
            )

        self.assertEqual([item["type"] for item in payloads], [3, 1])
        self.assertEqual(payloads[0]["title"], "抖音标题")
        self.assertEqual(payloads[0]["description"], "抖音正文")
        self.assertNotIn("## 抖音", payloads[0]["description"])
        self.assertFalse(payloads[0]["enableTimer"])
        self.assertTrue(payloads[1]["enableTimer"])
        self.assertEqual(payloads[1]["scheduleTime"], "2026-08-24 20:30")
        self.assertTrue(all(item["debugDryRun"] is True for item in payloads))

    def test_formal_requires_preflight_and_authorization_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._bundle(Path(temporary))
            with self.assertRaises(ControlledPublishError) as raised:
                build_controlled_payloads(
                    {
                        "manifestPath": str(manifest),
                        "mode": "formal",
                        "targets": [{"platform": "抖音", "accountId": 31, "schedule": None}],
                    },
                    accounts=self._accounts(),
                )
        self.assertEqual(raised.exception.error_code, "controlled_authorization_required")

    def test_douyin_body_raw_mention_is_not_treated_as_platform_mention(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._bundle(Path(temporary))
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["platformOverrides"]["抖音"]["body"] = "正文\n@抖音科技"
            manifest.write_text(
                json.dumps(data, ensure_ascii=False),
                encoding="utf-8",
            )
            with self.assertRaises(ControlledPublishError) as raised:
                build_controlled_payloads(
                    {
                        "manifestPath": str(manifest),
                        "mode": "preflight",
                        "targets": [
                            {"platform": "抖音", "accountId": 31, "schedule": None}
                        ],
                    },
                    accounts=self._accounts(),
                )

        self.assertEqual(
            raised.exception.error_code,
            "controlled_mentions_unsupported",
        )

    def test_authorization_is_bound_short_lived_and_single_use(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        create_authorization_schema(conn)
        payloads = [{"type": 3, "accountIds": [31], "title": "标题", "description": "正文", "scheduleTime": None}]
        now = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
        grant = create_authorization(conn, 77, payloads, now=now, ttl_seconds=600)

        consume_authorization(conn, grant["authorizationId"], 77, payloads, now=now + timedelta(seconds=1))
        with self.assertRaises(ControlledPublishError) as replayed:
            consume_authorization(conn, grant["authorizationId"], 77, payloads, now=now + timedelta(seconds=2))
        self.assertEqual(replayed.exception.error_code, "controlled_authorization_consumed")

        second = create_authorization(conn, 78, payloads, now=now, ttl_seconds=1)
        with self.assertRaises(ControlledPublishError) as expired:
            consume_authorization(conn, second["authorizationId"], 78, payloads, now=now + timedelta(seconds=2))
        self.assertEqual(expired.exception.error_code, "controlled_authorization_expired")

        third = create_authorization(conn, 79, payloads, now=now, ttl_seconds=600)
        changed = [{**payloads[0], "title": "被修改"}]
        self.assertNotEqual(scope_fingerprint(payloads), scope_fingerprint(changed))
        with self.assertRaises(ControlledPublishError) as changed_error:
            consume_authorization(conn, third["authorizationId"], 79, changed, now=now + timedelta(seconds=1))
        self.assertEqual(changed_error.exception.error_code, "controlled_authorization_scope_mismatch")

        declaration_changed = [{**payloads[0], "aiGenerated": True}]
        self.assertNotEqual(
            scope_fingerprint(payloads), scope_fingerprint(declaration_changed)
        )

    def test_task_projection_keeps_preflight_distinct_from_publish_receipt(self) -> None:
        projected = project_task(
            {
                "id": 42,
                "taskNo": "T42",
                "mode": "oneclick_preflight",
                "status": "partial_failed",
                "payloadJson": json.dumps(
                    [
                        {"type": 3, "accountIds": [31], "scheduleTime": None},
                        {"type": 1, "accountIds": [11], "scheduleTime": "2026-08-24 20:30"},
                    ],
                    ensure_ascii=False,
                ),
                "items": [
                    {
                        "platformType": 3,
                        "status": "success",
                        "message": "预发布检查通过",
                        "accountLabel": "抖音主体",
                    },
                    {
                        "platformType": 1,
                        "status": "failed",
                        "message": "PreflightError：封面入口不可用（错误码 xhs_cover_trigger_missing）",
                        "accountLabel": "小红书主体",
                        "scheduleSummary": "2026-08-24 20:30",
                    },
                ],
            }
        )
        self.assertEqual(projected["taskId"], 42)
        self.assertEqual(projected["phase"], "preflight")
        self.assertEqual(projected["platforms"][0]["receipt"], None)
        self.assertEqual(projected["platforms"][0]["account"], "抖音主体")
        self.assertEqual(projected["platforms"][0]["accountId"], 31)
        self.assertEqual(projected["platforms"][1]["errorCode"], "xhs_cover_trigger_missing")
        self.assertEqual(projected["platforms"][1]["scheduledAt"], "2026-08-24 20:30")

    def test_projection_maps_legacy_douyin_topic_failure_to_stable_code(self) -> None:
        projected = project_task(
            {
                "id": 12,
                "taskNo": "T12",
                "mode": "oneclick_publish",
                "status": "failed",
                "payloadJson": "[]",
                "items": [
                    {
                        "platformType": 3,
                        "status": "failed",
                        "message": (
                            "正式发布异常：RuntimeError："
                            "抖音没有返回任何可用的话题候选，已停止填写"
                        ),
                    }
                ],
            }
        )
        self.assertEqual(
            projected["platforms"][0]["errorCode"],
            "douyin_topic_candidates_unavailable",
        )


if __name__ == "__main__":
    unittest.main()
