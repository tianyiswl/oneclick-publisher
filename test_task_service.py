# -*- coding: utf-8 -*-
"""抖音带货批量任务记录的回归测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app_core import (
    account_service,
    database,
    douyin_commerce_batch_executor,
    publish_service,
    publish_runtime,
    task_service,
)
from utils import publish_tasks


class DouyinCommerceBatchTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "database.db"
        self.db_patch = patch.object(database, "DB_PATH", self.db_path)
        self.db_patch.start()
        self.publish_task_db_patch = patch.object(publish_tasks, "DB_PATH", self.db_path)
        self.publish_task_db_patch.start()
        database.ensure_schema()
        self.batch = {
            "accountFile": "oneclick_3_offline.json",
            "shared": {
                "title": "北海团购",
                "description": "三条本地团购视频",
                "tags": ["北海", "团购"],
                "selectedMusic": {
                    "musicId": "music-001",
                    "title": "收藏音乐",
                    "creator": "测试作者",
                    "duration": "00:30",
                    "source": "douyin-favorite-visible",
                },
                "contentDeclaration": "内容由AI生成",
            },
            "items": [
                {
                    "mediaPath": f"/tmp/batch-{index}.mp4",
                    "locationPreset": {
                        "poiId": f"poi-{index}",
                        "name": "北海银滩景区",
                        "address": "广西壮族自治区北海市银海区银滩大道中段",
                        "scope": "domestic",
                    },
                    "enableTimer": False,
                }
                for index in range(1, 4)
            ],
        }
        self.batch["items"][0]["locationPreset"].update(
            {
                "commissionFilter": "all",
                "observedCommissionType": "commission",
                "productCount": 15,
                "commissionProductCount": 15,
            }
        )
        self.batch["items"][1]["locationPreset"].update(
            {
                "commissionFilter": "all",
                "observedCommissionType": "no_commission",
                "productCount": 8,
                "commissionProductCount": 0,
            }
        )

    def tearDown(self) -> None:
        self.publish_task_db_patch.stop()
        self.db_patch.stop()
        self.tempdir.cleanup()

    def _create_paused_douyin_batch_source(self) -> dict:
        """创建一条用户主动暂停、仅后两条待续发的本地来源任务。"""

        account_service.save_oneclick_authorized_account(
            3,
            "续发测试主体",
            "resume-account.json",
            display_name="续发测试账号",
        )
        self.resume_media_paths = []
        for index in range(1, 4):
            media_path = Path(self.tempdir.name) / f"resume-{index}.mp4"
            media_path.write_bytes(b"resume-test-media")
            self.resume_media_paths.append(str(media_path))
        batch = {
            "type": 3,
            "workflow": "douyin-commerce-batch",
            "commerceMode": "local-group-buy",
            "contentType": "video",
            "accountFile": "resume-account.json",
            "shared": {
                "title": "受控续发测试",
                "description": "只续发未开始的视频",
                "tags": ["北海", "团购"],
                "selectedMusic": {
                    "musicId": "music-001",
                    "title": "收藏音乐",
                    "creator": "测试作者",
                    "duration": "00:30",
                    "source": "douyin-favorite-visible",
                },
                "contentDeclaration": "内容由AI生成",
            },
            "publishMode": "interval-schedule",
            "schedule": {
                "timezone": "Asia/Shanghai",
                "startTime": "2026-08-09 16:00",
                "intervalMinutes": 30,
            },
            "items": [
                {
                    "mediaId": index,
                    "mediaPath": media_path,
                    "locationPreset": {
                        "poiId": f"resume-poi-{index}",
                        "name": f"续发地点{index}",
                        "address": f"北京市朝阳区续发路{index}号",
                        "scope": "domestic",
                        "commissionFilter": (
                            "commission",
                            "no_commission",
                            "all",
                        )[index - 1],
                        "observedCommissionType": (
                            "commission",
                            "no_commission",
                            "commission",
                        )[index - 1],
                        "productCount": 10 + index,
                        "commissionProductCount": 0 if index == 2 else index,
                    },
                    "scheduleTimeOverride": "",
                }
                for index, media_path in enumerate(self.resume_media_paths, start=1)
            ],
        }
        source = task_service.create_douyin_batch_task(
            batch,
            schedule_now=datetime(2026, 8, 8, 21, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
        first_item_id = task_service.get_task(source["id"])["items"][0]["id"]
        self._write_final_receipt(
            source["id"],
            first_item_id,
            event_type="platform_scheduled_receipt",
            readback={
                "scheduleTime": "2026-08-09 16:00",
                "timezone": "Asia/Shanghai",
            },
            timezone="Asia/Shanghai",
            message="第一条已获得平台定时回执",
        )
        task_service.mark_task_paused(
            source["id"],
            "用户主动暂停，后续视频未开始",
            pause_reason_code=task_service.PAUSE_REASON_USER_REQUEST,
        )
        return source

    def _set_revision_source_states(
        self,
        task_id: int,
        statuses: list[str],
        *,
        task_status: str,
        pause_reason: str | None,
    ) -> None:
        with database.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM publish_task_items WHERE taskId = ? ORDER BY id",
                (int(task_id),),
            ).fetchall()
            self.assertEqual(len(rows), len(statuses))
            for row, status in zip(rows, statuses):
                conn.execute(
                    "UPDATE publish_task_items SET status = ? WHERE id = ?",
                    (status, int(row["id"])),
                )
            conn.execute(
                "UPDATE publish_tasks SET status = ?, pauseReasonCode = ? WHERE id = ?",
                (task_status, pause_reason, int(task_id)),
            )
            conn.commit()

    @staticmethod
    def _write_final_receipt(*args, **kwargs) -> None:
        """通过未来批量执行器唯一允许导入的内部写入模块执行。"""

        from app_core._douyin_commerce_batch_receipt_writer import (
            _write_final_batch_receipt,
        )

        _write_final_batch_receipt(*args, **kwargs)

    def test_batch_task_creates_one_publish_item_per_video_and_keeps_location_time(self) -> None:
        task = task_service.create_douyin_batch_task(self.batch)
        detail = task_service.get_task(task["id"])

        self.assertEqual(detail["workflowLabel"], "抖音带货批量")
        self.assertEqual(len(detail["items"]), 3)
        self.assertIn("北海银滩景区", detail["commerceSummary"])
        self.assertIn("立即发布", detail["commerceSummary"])
        self.assertEqual(
            detail["items"][0]["locationSummary"],
            "北海银滩景区【返佣】（广西壮族自治区北海市银海区银滩大道中段）",
        )
        self.assertEqual(
            detail["items"][1]["locationSummary"],
            "北海银滩景区【无佣】（广西壮族自治区北海市银海区银滩大道中段）",
        )
        self.assertEqual(
            detail["items"][2]["locationSummary"],
            "北海银滩景区（广西壮族自治区北海市银海区银滩大道中段）",
        )

    def test_prepare_revision_keeps_only_failed_and_pending_items(self) -> None:
        """若修订草稿带回成功项、丢失失败项或改写来源任务，该测试必须失败。"""

        source = self._create_paused_douyin_batch_source()
        self._set_revision_source_states(
            source["id"],
            ["success", "failed", "pending"],
            task_status="paused",
            pause_reason=task_service.PAUSE_REASON_USER_REQUEST,
        )
        before = task_service.get_task(source["id"])

        plan = task_service.prepare_douyin_batch_revision(source["id"])

        after = task_service.get_task(source["id"])
        self.assertTrue(plan["revisionAllowed"])
        self.assertEqual(plan["revisionItemIndexes"], [2, 3])
        self.assertEqual(len(plan["draft"]["items"]), 2)
        self.assertEqual(plan["successfulMediaKeys"], ["media:1"])
        self.assertEqual(after, before)

    def test_prepare_revision_aggregates_all_ancestors_and_rejects_bad_chain(
        self,
    ) -> None:
        """多代修订必须锁住全部祖先成功项；环或断链必须固定拒绝。"""

        source = self._create_paused_douyin_batch_source()
        self._set_revision_source_states(
            source["id"],
            ["success", "failed", "pending"],
            task_status="partial_failed",
            pause_reason=None,
        )
        first_plan = task_service.prepare_douyin_batch_revision(source["id"])
        second = task_service.create_douyin_batch_task(
            {
                **first_plan["draft"],
                "type": 3,
                "workflow": "douyin-commerce-batch",
                "commerceMode": "local-group-buy",
                "contentType": "video",
            },
            revision_source_task_id=source["id"],
            batch_item_indexes=first_plan["revisionItemIndexes"],
            schedule_now=datetime(
                2026, 8, 8, 21, 0, tzinfo=ZoneInfo("Asia/Shanghai")
            ),
        )
        self._set_revision_source_states(
            second["id"],
            ["success", "failed"],
            task_status="partial_failed",
            pause_reason=None,
        )
        second_plan = task_service.prepare_douyin_batch_revision(second["id"])
        third = task_service.create_douyin_batch_task(
            {
                **second_plan["draft"],
                "type": 3,
                "workflow": "douyin-commerce-batch",
                "commerceMode": "local-group-buy",
                "contentType": "video",
            },
            revision_source_task_id=second["id"],
            batch_item_indexes=second_plan["revisionItemIndexes"],
            schedule_now=datetime(
                2026, 8, 8, 21, 0, tzinfo=ZoneInfo("Asia/Shanghai")
            ),
        )
        self._set_revision_source_states(
            third["id"],
            ["failed"],
            task_status="failed",
            pause_reason=None,
        )

        plan = task_service.prepare_douyin_batch_revision(third["id"])

        self.assertTrue(plan["revisionAllowed"])
        self.assertEqual(plan["successfulMediaKeys"], ["media:2", "media:1"])

        with database.connect() as conn:
            conn.execute(
                "UPDATE publish_tasks SET revisionSourceTaskId = ? WHERE id = ?",
                (third["id"], source["id"]),
            )
            conn.commit()
        cycle = task_service.prepare_douyin_batch_revision(third["id"])
        self.assertFalse(cycle["revisionAllowed"])
        self.assertEqual(cycle["blockedReason"], "来源任务的修改链无法确认")
        self.assertNotIn("draft", cycle)

        with database.connect() as conn:
            conn.execute(
                "UPDATE publish_tasks SET revisionSourceTaskId = NULL WHERE id = ?",
                (source["id"],),
            )
            conn.execute(
                "UPDATE publish_tasks SET revisionSourceTaskId = ? WHERE id = ?",
                (999999, second["id"]),
            )
            conn.commit()
        damaged = task_service.prepare_douyin_batch_revision(third["id"])
        self.assertFalse(damaged["revisionAllowed"])
        self.assertEqual(damaged["blockedReason"], "来源任务的修改链无法确认")
        self.assertNotIn("draft", damaged)

    def test_prepare_revision_rejects_ambiguous_and_nonmanual_pauses(self) -> None:
        """若非人工暂停的任务被还原为可编辑草稿，该测试必须失败。"""

        for reason in (
            task_service.PAUSE_REASON_RECEIPT_AMBIGUOUS,
            task_service.PAUSE_REASON_WAITING_LOGIN,
            task_service.PAUSE_REASON_WAITING_VERIFICATION,
            task_service.PAUSE_REASON_AUTO_FAILURE,
            task_service.PAUSE_REASON_CLIENT_SHUTDOWN,
            task_service.PAUSE_REASON_CLEANUP_INCOMPLETE,
        ):
            source = self._create_paused_douyin_batch_source()
            self._set_revision_source_states(
                source["id"],
                ["success", "pending", "pending"],
                task_status="paused",
                pause_reason=reason,
            )

            self.assertFalse(
                task_service.prepare_douyin_batch_revision(source["id"])[
                    "revisionAllowed"
                ]
            )

    def test_prepare_revision_rejects_success_item_without_stable_media_key(self) -> None:
        """若成功项没有稳定媒体键仍放行修订，该测试必须失败。"""

        source = self._create_paused_douyin_batch_source()
        self._set_revision_source_states(
            source["id"],
            ["success", "failed", "pending"],
            task_status="paused",
            pause_reason=task_service.PAUSE_REASON_USER_REQUEST,
        )
        with database.connect() as conn:
            row = conn.execute(
                "SELECT payloadJson FROM publish_tasks WHERE id = ?",
                (source["id"],),
            ).fetchone()
            payloads = json.loads(row["payloadJson"])
            payloads[0].pop("mediaId", None)
            payloads[0]["fileList"] = []
            conn.execute(
                "UPDATE publish_tasks SET payloadJson = ? WHERE id = ?",
                (json.dumps(payloads, ensure_ascii=False), source["id"]),
            )
            conn.commit()

        plan = task_service.prepare_douyin_batch_revision(source["id"])

        self.assertFalse(plan["revisionAllowed"])
        self.assertEqual(plan["blockedReason"], "来源任务的成功视频身份无法确认")
        self.assertEqual(plan["successfulMediaKeys"], [])
        self.assertNotIn("draft", plan)

    def test_prepare_revision_maps_media_key_error_to_fixed_rejection(self) -> None:
        """若媒体键构造异常或原文逃出脱敏边界，该测试必须失败。"""

        source = self._create_paused_douyin_batch_source()
        self._set_revision_source_states(
            source["id"],
            ["success", "failed", "pending"],
            task_status="paused",
            pause_reason=task_service.PAUSE_REASON_USER_REQUEST,
        )
        with database.connect() as conn:
            row = conn.execute(
                "SELECT payloadJson FROM publish_tasks WHERE id = ?",
                (source["id"],),
            ).fetchone()
            payloads = json.loads(row["payloadJson"])
            payloads[0].pop("mediaId", None)
            conn.execute(
                "UPDATE publish_tasks SET payloadJson = ? WHERE id = ?",
                (json.dumps(payloads, ensure_ascii=False), source["id"]),
            )
            conn.commit()

        try:
            with patch(
                "app_core.task_service.Path.resolve",
                side_effect=ValueError("sensitive-media-key-error"),
            ):
                plan = task_service.prepare_douyin_batch_revision(source["id"])
        except Exception as exc:
            self.fail(f"媒体键异常逃出公开边界：{type(exc).__name__}")

        self.assertFalse(plan["revisionAllowed"])
        self.assertEqual(plan["blockedReason"], "来源任务的成功视频身份无法确认")
        self.assertNotIn(
            "sensitive-media-key-error",
            json.dumps(plan, ensure_ascii=False),
        )
        self.assertNotIn("draft", plan)

    def test_revision_child_records_source_without_mutating_it(self) -> None:
        """若子任务丢失修订来源或创建时改写来源，该测试必须失败。"""

        source = self._create_paused_douyin_batch_source()
        self._set_revision_source_states(
            source["id"],
            ["failed", "failed", "failed"],
            task_status="failed",
            pause_reason=None,
        )
        before = task_service.get_task(source["id"])

        child = task_service.create_douyin_batch_task(
            self.batch,
            revision_source_task_id=source["id"],
        )

        detail = task_service.get_task(child["id"])
        self.assertEqual(detail["revisionSourceTaskId"], source["id"])
        self.assertEqual(detail["revisionSourceTaskNo"], source["taskNo"])
        self.assertEqual(task_service.get_task(source["id"]), before)

    def test_revision_child_creation_rejects_missing_or_unlinkable_source(self) -> None:
        """修订创建必须在写入事务内确认来源仍存在且可关联。"""

        before = task_service.list_tasks(limit=100)
        with self.assertRaisesRegex(ValueError, "^修订来源任务已变化，请重新返回修改$"):
            task_service.create_douyin_batch_task(
                self.batch,
                revision_source_task_id=999999,
            )
        self.assertEqual(task_service.list_tasks(limit=100), before)

        source = self._create_paused_douyin_batch_source()
        self._set_revision_source_states(
            source["id"],
            ["success", "success", "success"],
            task_status="success",
            pause_reason=None,
        )
        before = task_service.list_tasks(limit=100)
        with self.assertRaisesRegex(ValueError, "^修订来源任务已变化，请重新返回修改$"):
            task_service.create_douyin_batch_task(
                self.batch,
                revision_source_task_id=source["id"],
            )
        self.assertEqual(task_service.list_tasks(limit=100), before)

    def test_revision_child_rejects_bound_item_that_became_success(self) -> None:
        """来源整体仍可修订时，也必须逐项拒绝已变成成功的绑定序号。"""

        source = self._create_paused_douyin_batch_source()
        self._set_revision_source_states(
            source["id"],
            ["success", "success", "pending"],
            task_status="partial_failed",
            pause_reason=None,
        )
        before = task_service.list_tasks(limit=100)
        one_item_batch = {**self.batch, "items": [self.batch["items"][1]]}

        with self.assertRaisesRegex(ValueError, "^修订来源任务已变化，请重新返回修改$"):
            task_service.create_douyin_batch_task(
                one_item_batch,
                revision_source_task_id=source["id"],
                batch_item_indexes=[2],
            )

        self.assertEqual(task_service.list_tasks(limit=100), before)

    def test_batch_item_indexes_reject_non_builtin_integers(self) -> None:
        """布尔、字符串和浮点数不得被宽松转换为条目序号。"""

        invalid_indexes = (
            [True, 2, 3],
            ["1", 2, 3],
            [1.0, 2, 3],
            [1, 2.7, 3],
        )
        before = task_service.list_tasks(limit=100)

        for indexes in invalid_indexes:
            with self.subTest(indexes=indexes), self.assertRaisesRegex(
                ValueError,
                "^抖音带货续发视频序号必须为互异正整数$",
            ):
                task_service.create_douyin_batch_task(
                    self.batch,
                    batch_item_indexes=indexes,
                )

        self.assertEqual(task_service.list_tasks(limit=100), before)

    def test_delete_tasks_rejects_source_with_revision_descendant(self) -> None:
        """来源已有修订后代时，删除来源必须固定拒绝以保留追溯链。"""

        source = self._create_paused_douyin_batch_source()
        self._set_revision_source_states(
            source["id"],
            ["failed", "failed", "failed"],
            task_status="failed",
            pause_reason=None,
        )
        child = task_service.create_douyin_batch_task(
            self.batch,
            revision_source_task_id=source["id"],
        )
        self._set_revision_source_states(
            child["id"],
            ["failed", "failed", "failed"],
            task_status="failed",
            pause_reason=None,
        )

        with self.assertRaisesRegex(ValueError, "^已有修订后代的来源任务不能删除$"):
            task_service.delete_tasks([source["id"]])

        self.assertIsNotNone(task_service.get_task(source["id"]))
        saved_child = task_service.get_task(child["id"])
        self.assertEqual(saved_child["revisionSourceTaskId"], source["id"])
        self.assertEqual(saved_child["revisionSourceTaskNo"], source["taskNo"])

    def test_batch_task_creation_rolls_back_every_write_stage_in_one_transaction(
        self,
    ) -> None:
        """任务头、条目、事件和批次元数据任一步失败都必须整体回滚。"""

        markers = (
            "INSERT INTO publish_tasks",
            "INSERT INTO publish_task_items",
            "'created'",
            "UPDATE publish_task_items",
            "'batch_created'",
        )
        real_connect = task_service.connect

        for marker in markers:
            with self.subTest(marker=marker):
                connect_count = 0
                failed = False

                class FailingCursor:
                    def __init__(self, cursor, fail) -> None:
                        self._cursor = cursor
                        self._fail = fail

                    def execute(self, sql, parameters=()):
                        self._fail(sql)
                        return self._cursor.execute(sql, parameters)

                    def __getattr__(self, name):
                        return getattr(self._cursor, name)

                class FailingConnection:
                    def __init__(self, conn) -> None:
                        self._conn = conn

                    def fail(self, sql) -> None:
                        nonlocal failed
                        if not failed and marker in str(sql):
                            failed = True
                            raise RuntimeError("controlled atomic batch failure")

                    def execute(self, sql, parameters=()):
                        self.fail(sql)
                        return self._conn.execute(sql, parameters)

                    def cursor(self):
                        return FailingCursor(self._conn.cursor(), self.fail)

                    def __getattr__(self, name):
                        return getattr(self._conn, name)

                @contextmanager
                def failing_connect():
                    nonlocal connect_count
                    connect_count += 1
                    with real_connect() as conn:
                        yield FailingConnection(conn)

                with patch.object(
                    task_service, "connect", new=failing_connect
                ), self.assertRaisesRegex(
                    RuntimeError, "^controlled atomic batch failure$"
                ):
                    task_service.create_douyin_batch_task(self.batch)

                self.assertTrue(failed)
                self.assertEqual(connect_count, 1)
                with database.connect() as conn:
                    counts = [
                        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                        for table in (
                            "publish_tasks",
                            "publish_task_items",
                            "publish_task_events",
                        )
                    ]
                self.assertEqual(counts, [0, 0, 0])

    def test_batch_task_creation_rolls_back_base_exception_without_compensation(
        self,
    ) -> None:
        """BaseException 必须保持原语义传播，且仅由原事务回滚。"""

        class ControlledAbort(BaseException):
            pass

        real_connect = task_service.connect
        connect_count = 0

        class AbortingConnection:
            def __init__(self, conn) -> None:
                self._conn = conn

            def execute(self, sql, parameters=()):
                if "UPDATE publish_task_items" in str(sql):
                    raise ControlledAbort("controlled base abort")
                return self._conn.execute(sql, parameters)

            def __getattr__(self, name):
                return getattr(self._conn, name)

        @contextmanager
        def aborting_connect():
            nonlocal connect_count
            connect_count += 1
            with real_connect() as conn:
                yield AbortingConnection(conn)

        with patch.object(task_service, "connect", new=aborting_connect):
            with self.assertRaises(ControlledAbort):
                task_service.create_douyin_batch_task(self.batch)

        self.assertEqual(connect_count, 1)
        with database.connect() as conn:
            counts = [
                conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "publish_tasks",
                    "publish_task_items",
                    "publish_task_events",
                )
            ]
        self.assertEqual(counts, [0, 0, 0])

    def test_batch_task_creation_success_uses_one_transaction_connection(self) -> None:
        """成功路径也必须只使用一个写事务连接。"""

        real_connect = task_service.connect
        connect_count = 0

        @contextmanager
        def counting_connect():
            nonlocal connect_count
            connect_count += 1
            with real_connect() as conn:
                yield conn

        with patch.object(task_service, "connect", new=counting_connect):
            task_service.create_douyin_batch_task(self.batch)

        self.assertEqual(connect_count, 1)

    def test_prepare_douyin_batch_resume_only_includes_pending_source_items(self) -> None:
        """若错误复制成功项或丢失原排期，该测试必须失败。"""

        source = self._create_paused_douyin_batch_source()

        prepared = task_service.prepare_douyin_batch_resume(
            source["id"],
            now=datetime(2026, 8, 8, 21, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        self.assertTrue(prepared["resumeAllowed"])
        self.assertEqual(prepared["itemIndexes"], [2, 3])
        self.assertEqual(
            [item["mediaPath"] for item in prepared["batch"]["items"]],
            self.resume_media_paths[1:],
        )
        self.assertEqual(
            [item["scheduleTime"] for item in prepared["batch"]["items"]],
            ["2026-08-09 16:30", "2026-08-09 17:00"],
        )

    def test_create_douyin_batch_resume_keeps_source_unchanged_and_preserves_indexes(self) -> None:
        """若续发覆盖来源条目或重排原视频序号，该测试必须失败。"""

        source = self._create_paused_douyin_batch_source()
        with database.connect() as conn:
            row = conn.execute(
                "SELECT payloadJson FROM publish_tasks WHERE id = ?", (source["id"],)
            ).fetchone()
            payloads = json.loads(row["payloadJson"])
            for payload in payloads[1:]:
                payload["locationPoi"].pop("commissionFilter")
                payload["locationPoi"]["commerceInfo"] = "must-not-survive"
                payload["locationPoi"]["domMarker"] = "must-not-survive"
                payload["locationPoi"]["unknownField"] = "must-not-survive"
            conn.execute(
                "UPDATE publish_tasks SET payloadJson = ? WHERE id = ?",
                (json.dumps(payloads, ensure_ascii=False), source["id"]),
            )
        source_before = task_service.get_task(source["id"])

        created = task_service.create_douyin_batch_resume(
            source["id"],
            now=datetime(2026, 8, 8, 21, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        child = task_service.get_task(created["task"]["id"])
        source_after = task_service.get_task(source["id"])
        self.assertEqual(child["mode"], "oneclick_resume")
        self.assertEqual(child["resumeSourceTaskId"], source["id"])
        self.assertEqual([item["batchItemIndex"] for item in child["items"]], [2, 3])
        child_payloads = json.loads(child["payloadJson"])
        self.assertEqual(
            [payload["locationCommissionFilter"] for payload in child_payloads],
            ["no_commission", "all"],
        )
        self.assertEqual(
            [payload["locationPoi"]["observedCommissionType"] for payload in child_payloads],
            ["no_commission", "commission"],
        )
        for payload in child_payloads:
            self.assertNotIn("commerceInfo", payload["locationPoi"])
            self.assertNotIn("domMarker", payload["locationPoi"])
            self.assertNotIn("unknownField", payload["locationPoi"])
        self.assertEqual(source_after["status"], source_before["status"])
        self.assertEqual(source_after["successCount"], source_before["successCount"])
        self.assertEqual(source_after["failedCount"], source_before["failedCount"])
        self.assertEqual(source_after["items"], source_before["items"])

    def test_sms_cooldown_restores_for_source_and_resume_child(self) -> None:
        """若来源/续发子任务丢失冷却，或损坏状态被放行，该测试必须失败。"""

        source = task_service.create_douyin_batch_task(self.batch)
        item_id = task_service.get_task(source["id"])["items"][0]["id"]
        triggered = datetime(2026, 8, 10, 1, 0, tzinfo=ZoneInfo("UTC"))
        task_service.record_douyin_sms_cooldown(
            source["id"], item_id, triggered_at_utc=triggered
        )
        child = task_service.create_douyin_batch_task(
            self.batch, resume_source_task_id=source["id"]
        )
        now = triggered + timedelta(seconds=33)

        self.assertEqual(
            task_service.load_douyin_sms_cooldown_remaining(source["id"], now_utc=now),
            27.0,
        )
        self.assertEqual(
            task_service.load_douyin_sms_cooldown_remaining(child["id"], now_utc=now),
            27.0,
        )
        rendered = str(task_service.get_task(source["id"])["events"])
        self.assertNotIn("oneclick_3_offline.json", rendered)
        self.assertNotIn("123456", rendered)

        with database.connect() as conn:
            event = conn.execute(
                """
                SELECT id FROM publish_task_events
                WHERE taskId = ? AND eventType = 'douyin_sms_cooldown_started'
                ORDER BY id DESC LIMIT 1
                """,
                (source["id"],),
            ).fetchone()
            conn.execute(
                "UPDATE publish_task_events SET detailJson = ? WHERE id = ?",
                ('{"cooldownSeconds":"bad"}', event["id"]),
            )
            conn.commit()
        with self.assertRaisesRegex(ValueError, "^verification_cooldown_state_invalid$"):
            task_service.load_douyin_sms_cooldown_remaining(source["id"], now_utc=now)

        task_service.record_douyin_sms_cooldown(
            source["id"], item_id, triggered_at_utc=triggered
        )
        self.assertEqual(
            task_service.load_douyin_sms_cooldown_remaining(
                source["id"], now_utc=triggered + timedelta(seconds=60)
            ),
            0.0,
        )

    def test_client_shutdown_before_submit_returns_current_item_to_pending(self) -> None:
        """若退出前提交过的当前条目不能安全回退，该测试必须失败。"""

        source = task_service.create_douyin_batch_task(self.batch)
        item_id = task_service.get_task(source["id"])["items"][0]["id"]
        task_service.mark_batch_item_result(
            source["id"], item_id, ok=True,
            message="预检完成", event_type="preflight_readback", readback={},
        )
        task_service.pause_douyin_batch_before_submit(
            source["id"], item_id, "客户端退出，最终提交尚未发生"
        )

        saved = task_service.get_task(source["id"])
        self.assertEqual(saved["status"], "paused")
        self.assertEqual(saved["pauseReasonCode"], "client_shutdown")
        self.assertEqual(saved["items"][0]["status"], "pending")

    def test_sms_cooldown_rejects_non_object_event_detail(self) -> None:
        """若损坏的冷却事件详情泄漏底层异常，该测试必须失败。"""

        task = task_service.create_douyin_batch_task(self.batch)
        item_id = task_service.get_task(task["id"])["items"][0]["id"]
        triggered = datetime(2026, 8, 10, 1, 0, tzinfo=ZoneInfo("UTC"))
        task_service.record_douyin_sms_cooldown(
            task["id"], item_id, triggered_at_utc=triggered
        )
        with database.connect() as conn:
            conn.execute(
                """
                UPDATE publish_task_events SET detailJson = '[]'
                WHERE taskId = ? AND eventType = 'douyin_sms_cooldown_started'
                """,
                (task["id"],),
            )
            conn.commit()

        with self.assertRaisesRegex(ValueError, "^verification_cooldown_state_invalid$"):
            task_service.load_douyin_sms_cooldown_remaining(task["id"], now_utc=triggered)

    def test_sms_cooldown_current_task_event_takes_precedence_over_later_source_write(self) -> None:
        """若来源任务较晚写入的旧事件覆盖当前任务冷却，该测试必须失败。"""

        source = task_service.create_douyin_batch_task(self.batch)
        source_item_id = task_service.get_task(source["id"])["items"][0]["id"]
        child = task_service.create_douyin_batch_task(
            self.batch, resume_source_task_id=source["id"]
        )
        child_item_id = task_service.get_task(child["id"])["items"][0]["id"]
        base = datetime(2026, 8, 10, 1, 0, tzinfo=ZoneInfo("UTC"))

        task_service.record_douyin_sms_cooldown(
            child["id"], child_item_id, triggered_at_utc=base + timedelta(seconds=40)
        )
        task_service.record_douyin_sms_cooldown(
            source["id"], source_item_id, triggered_at_utc=base
        )

        self.assertEqual(
            task_service.load_douyin_sms_cooldown_remaining(
                child["id"], now_utc=base + timedelta(seconds=50)
            ),
            50.0,
        )

    def test_client_shutdown_does_not_roll_back_success_created_before_conditional_update(self) -> None:
        """若条件更新前的成功条目仍被回退或写暂停事件，该测试必须失败。"""

        task = task_service.create_douyin_batch_task(self.batch)
        item_id = task_service.get_task(task["id"])["items"][0]["id"]

        class RacingConnection:
            def __init__(self, connection) -> None:
                self.connection = connection
                self.raced = False

            def execute(self, sql, parameters=()):
                if (
                    not self.raced
                    and "UPDATE publish_task_items SET status = 'pending'" in sql
                ):
                    with database.connect() as concurrent:
                        concurrent.execute(
                            "UPDATE publish_task_items SET status = 'success' WHERE id = ?",
                            (item_id,),
                        )
                        concurrent.commit()
                    self.raced = True
                return self.connection.execute(sql, parameters)

            def commit(self) -> None:
                self.connection.commit()

        @contextmanager
        def racing_connect():
            with database.connect() as connection:
                yield RacingConnection(connection)

        with patch.object(task_service, "connect", new=racing_connect):
            with self.assertRaisesRegex(ValueError, "^最终提交前条目状态无法安全回退$"):
                task_service.pause_douyin_batch_before_submit(
                    task["id"], item_id, "客户端退出，最终提交尚未发生"
                )

        saved = task_service.get_task(task["id"])
        self.assertEqual(saved["status"], "pending")
        self.assertEqual(saved["items"][0]["status"], "success")
        self.assertNotIn(
            "batch_paused_client_shutdown",
            [event["eventType"] for event in saved["events"]],
        )

    def test_record_sms_cooldown_rejects_naive_timestamp(self) -> None:
        """若 naive 冷却时间按本机时区解释，该测试必须失败。"""

        task = task_service.create_douyin_batch_task(self.batch)
        item_id = task_service.get_task(task["id"])["items"][0]["id"]

        with self.assertRaisesRegex(ValueError, "^verification_cooldown_state_invalid$"):
            task_service.record_douyin_sms_cooldown(
                task["id"], item_id, triggered_at_utc=datetime(2026, 8, 10, 1, 0)
            )

    def test_record_sms_cooldown_rejects_non_utc_offset(self) -> None:
        """若 +08:00 冷却起点被写入，该测试必须失败。"""

        task = task_service.create_douyin_batch_task(self.batch)
        item_id = task_service.get_task(task["id"])["items"][0]["id"]

        with self.assertRaisesRegex(ValueError, "^verification_cooldown_state_invalid$"):
            task_service.record_douyin_sms_cooldown(
                task["id"],
                item_id,
                triggered_at_utc=datetime(
                    2026, 8, 10, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")
                ),
            )

    def test_load_sms_cooldown_rejects_non_utc_now(self) -> None:
        """若读取时接受 +08:00 当前时间，该测试必须失败。"""

        task = task_service.create_douyin_batch_task(self.batch)
        item_id = task_service.get_task(task["id"])["items"][0]["id"]
        triggered = datetime(2026, 8, 10, 1, 0, tzinfo=ZoneInfo("UTC"))
        task_service.record_douyin_sms_cooldown(
            task["id"], item_id, triggered_at_utc=triggered
        )

        with self.assertRaisesRegex(ValueError, "^verification_cooldown_state_invalid$"):
            task_service.load_douyin_sms_cooldown_remaining(
                task["id"],
                now_utc=datetime(2026, 8, 10, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            )

    def test_load_sms_cooldown_rejects_non_utc_event_start(self) -> None:
        """若已保存的 +08:00 冷却起点被接受，该测试必须失败。"""

        task = task_service.create_douyin_batch_task(self.batch)
        item_id = task_service.get_task(task["id"])["items"][0]["id"]
        triggered = datetime(2026, 8, 10, 1, 0, tzinfo=ZoneInfo("UTC"))
        task_service.record_douyin_sms_cooldown(
            task["id"], item_id, triggered_at_utc=triggered
        )
        with database.connect() as conn:
            conn.execute(
                """
                UPDATE publish_task_events SET detailJson = ?
                WHERE taskId = ? AND eventType = 'douyin_sms_cooldown_started'
                """,
                (
                    '{"cooldownStartedAt":"2026-08-10T09:00:00+08:00","cooldownSeconds":"60"}',
                    task["id"],
                ),
            )
            conn.commit()

        with self.assertRaisesRegex(ValueError, "^verification_cooldown_state_invalid$"):
            task_service.load_douyin_sms_cooldown_remaining(task["id"], now_utc=triggered)

    def test_prepare_douyin_batch_resume_rejects_nonmanual_and_historical_pauses(self) -> None:
        """若登录、验证、回执、自动暂停或历史任务可续发，该测试必须失败。"""

        for reason in (
            task_service.PAUSE_REASON_WAITING_LOGIN,
            task_service.PAUSE_REASON_WAITING_VERIFICATION,
            task_service.PAUSE_REASON_RECEIPT_AMBIGUOUS,
            task_service.PAUSE_REASON_AUTO_FAILURE,
            None,
        ):
            with self.subTest(reason=reason):
                source = self._create_paused_douyin_batch_source()
                if reason is None:
                    with database.connect() as conn:
                        conn.execute(
                            "UPDATE publish_tasks SET pauseReasonCode = NULL WHERE id = ?",
                            (source["id"],),
                        )
                        conn.commit()
                else:
                    task_service.mark_task_paused(
                        source["id"],
                        "非用户主动暂停",
                        pause_reason_code=reason,
                    )
                before_count = len(task_service.list_tasks())
                prepared = task_service.prepare_douyin_batch_resume(
                    source["id"],
                    now=datetime(2026, 8, 8, 21, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
                )
                self.assertFalse(prepared["resumeAllowed"])
                with self.assertRaises(ValueError):
                    task_service.create_douyin_batch_resume(
                        source["id"],
                        now=datetime(2026, 8, 8, 21, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
                    )
                self.assertEqual(len(task_service.list_tasks()), before_count)

    def test_prepare_douyin_batch_resume_rejects_expired_schedule_and_missing_media(self) -> None:
        """若续发自动改期或忽略缺失素材，该测试必须失败。"""

        source = self._create_paused_douyin_batch_source()
        expired = task_service.prepare_douyin_batch_resume(
            source["id"],
            now=datetime(2026, 8, 10, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
        self.assertFalse(expired["resumeAllowed"])
        self.assertIn("发布时间", expired["blockedReason"])

        source = self._create_paused_douyin_batch_source()
        Path(self.resume_media_paths[1]).unlink()
        missing_media = task_service.prepare_douyin_batch_resume(
            source["id"],
            now=datetime(2026, 8, 8, 21, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
        self.assertFalse(missing_media["resumeAllowed"])
        self.assertIn("素材不存在", missing_media["blockedReason"])

    def test_prepare_douyin_batch_resume_rejects_duplicate_child_creation(self) -> None:
        """若同一来源可重复创建子任务导致重复发布，该测试必须失败。"""

        source = self._create_paused_douyin_batch_source()
        now = datetime(2026, 8, 8, 21, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        task_service.create_douyin_batch_resume(source["id"], now=now)

        prepared = task_service.prepare_douyin_batch_resume(source["id"], now=now)
        self.assertFalse(prepared["resumeAllowed"])
        self.assertIn("已创建续发子任务", prepared["blockedReason"])
        with self.assertRaisesRegex(ValueError, "已创建续发子任务"):
            task_service.create_douyin_batch_resume(source["id"], now=now)

    def test_public_platform_receipt_keeps_a_batch_item_running(self) -> None:
        task = task_service.create_douyin_batch_task(self.batch)
        task_id = task["id"]
        item_id = task_service.get_task(task_id)["items"][0]["id"]

        task_service.mark_batch_item_result(
            task_id, item_id, ok=True, message="编辑页填写完成", event_type="editor_written", readback={}
        )
        self.assertEqual(task_service.get_task(task_id)["items"][0]["status"], "running")

        task_service.mark_batch_item_result(
            task_id,
            item_id,
            ok=True,
            message="平台已定时",
            event_type="platform_scheduled_receipt",
            readback={"scheduleTime": "2026-08-07 09:00"},
        )
        self.assertEqual(task_service.get_task(task_id)["items"][0]["status"], "running")

    def test_final_receipt_without_required_readback_keeps_item_running(self) -> None:
        task = task_service.create_douyin_batch_task(self.batch)
        task_id = task["id"]
        item_id = task_service.get_task(task_id)["items"][0]["id"]

        task_service.mark_batch_item_result(
            task_id,
            item_id,
            ok=True,
            message="平台已发布",
            event_type="platform_publish_receipt",
            readback={"publishedAt": "2026-08-06 10:00"},
        )

        self.assertEqual(task_service.get_task(task_id)["items"][0]["status"], "running")

    def test_public_batch_result_cannot_mark_item_success(self) -> None:
        task = task_service.create_douyin_batch_task(self.batch)
        task_id = task["id"]
        item_id = task_service.get_task(task_id)["items"][0]["id"]

        task_service.mark_batch_item_result(
            task_id,
            item_id,
            ok=True,
            message="平台已发布",
            event_type="platform_publish_receipt",
            readback={"platformPostId": "post-001", "publishedAt": "2026-08-06 10:00"},
        )

        self.assertEqual(task_service.get_task(task_id)["items"][0]["status"], "running")

    def test_generic_platform_result_cannot_mark_batch_items_success(self) -> None:
        task = task_service.create_douyin_batch_task(self.batch)
        task_id = task["id"]

        with self.assertRaisesRegex(ValueError, "批量执行器"):
            task_service.mark_platform_result(
                task_id,
                3,
                ok=True,
                message="调用方伪造的平台成功",
                content_type="video",
                event_type="platform_publish_receipt",
            )

        self.assertEqual(
            [item["status"] for item in task_service.get_task(task_id)["items"]],
            ["pending", "pending", "pending"],
        )

    def test_caller_cannot_construct_private_receipt_and_mark_item_success(self) -> None:
        task = task_service.create_douyin_batch_task(self.batch)
        task_id = task["id"]
        item_id = task_service.get_task(task_id)["items"][0]["id"]

        class ForgedReceipt:
            event_type = "platform_scheduled_receipt"
            readback = {
                "scheduleTime": "2026-08-07 09:00",
                "timezone": "Asia/Shanghai",
            }

        for forged_readback in (
            ForgedReceipt(),
            {
                "scheduleTime": "2026-08-07 09:00",
                "timezone": "Asia/Shanghai",
            },
        ):
            task_service.mark_batch_item_result(
                task_id,
                item_id,
                ok=True,
                message="调用方伪造的平台已定时",
                event_type="platform_scheduled_receipt",
                readback=forged_readback,
            )
            self.assertEqual(task_service.get_task(task_id)["items"][0]["status"], "running")

        with self.assertRaises(AttributeError):
            getattr(task_service, "_mark_controlled_batch_receipt")(
                task_id, item_id, ForgedReceipt(), "伪造的平台已定时"
            )

    def test_internal_writer_rejects_utc_missing_fields_and_invalid_events(self) -> None:
        class ForgedTimezone:
            def __ne__(self, _other: object) -> bool:
                return False

        invalid_receipts = (
            (
                "platform_scheduled_receipt",
                {"scheduleTime": "2026-08-07 09:00"},
                "UTC",
            ),
            (
                "platform_scheduled_receipt",
                {"scheduleTime": "2026-08-07 09:00"},
                ForgedTimezone(),
            ),
            (
                "platform_scheduled_receipt",
                {"scheduleTime": "2026-08-07 09:00", "timezone": "UTC"},
                "Asia/Shanghai",
            ),
            (
                "platform_scheduled_receipt",
                {"scheduleTime": "2026-08-07 09:00"},
                "Asia/Shanghai",
            ),
            ("platform_scheduled_receipt", {}, "Asia/Shanghai"),
            (
                "platform_scheduled_receipt",
                {"scheduleTime": "2026-02-30 09:00"},
                "Asia/Shanghai",
            ),
            (
                "platform_scheduled_receipt",
                {
                    "scheduleTime": "2026-08-07 09:00",
                    "publishedAt": "not-a-datetime",
                    "timezone": "Asia/Shanghai",
                },
                "Asia/Shanghai",
            ),
            (
                "platform_publish_receipt",
                {"publishedAt": "2026-08-06 10:00", "timezone": "Asia/Shanghai"},
                "Asia/Shanghai",
            ),
            (
                "platform_publish_receipt",
                {"platformPostId": "post-001", "publishedAt": "2026-08-06 10:00"},
                "Asia/Shanghai",
            ),
            (
                "platform_publish_receipt",
                {
                    "platformPostId": "post-001",
                    "publishedAt": "2026-02-30 10:00",
                    "timezone": "Asia/Shanghai",
                },
                "Asia/Shanghai",
            ),
            ("platform_publish_receipt", {"platformPostId": True}, "Asia/Shanghai"),
            (
                "platform_publish_receipt",
                {"platformPostId": "post-001", "cookie": "must-not-store"},
                "Asia/Shanghai",
            ),
            ("editor_written", {"platformPostId": "post-001"}, "Asia/Shanghai"),
        )

        for event_type, readback, timezone in invalid_receipts:
            with self.subTest(event_type=event_type, readback=readback, timezone=timezone):
                task = task_service.create_douyin_batch_task(self.batch)
                task_id = task["id"]
                item_id = task_service.get_task(task_id)["items"][0]["id"]

                with self.assertRaises(ValueError):
                    self._write_final_receipt(
                        task_id,
                        item_id,
                        event_type=event_type,
                        readback=readback,
                        timezone=timezone,
                        message="无效最终回执",
                    )

                self.assertEqual(task_service.get_task(task_id)["items"][0]["status"], "pending")

    def test_internal_writer_rejects_manually_constructed_receipt_object(self) -> None:
        task = task_service.create_douyin_batch_task(self.batch)
        task_id = task["id"]
        item_id = task_service.get_task(task_id)["items"][0]["id"]

        class ForgedReceipt:
            scheduleTime = "2026-08-07 09:00"

        with self.assertRaises(ValueError):
            self._write_final_receipt(
                task_id,
                item_id,
                event_type="platform_scheduled_receipt",
                readback=ForgedReceipt(),
                timezone="Asia/Shanghai",
                message="伪造的平台已定时",
            )

        self.assertEqual(task_service.get_task(task_id)["items"][0]["status"], "pending")

    def test_internal_writer_accepts_legal_platform_receipt_and_persists_timezone(self) -> None:
        task = task_service.create_douyin_batch_task(self.batch)
        task_id = task["id"]
        item_id = task_service.get_task(task_id)["items"][0]["id"]

        self._write_final_receipt(
            task_id,
            item_id,
            event_type="platform_scheduled_receipt",
            readback={
                "scheduleTime": "2026-08-07 09:00",
                "timezone": "Asia/Shanghai",
            },
            timezone="Asia/Shanghai",
            message="平台已定时",
        )

        detail = task_service.get_task(task_id)
        self.assertEqual(detail["items"][0]["status"], "success")
        self.assertEqual(json.loads(detail["events"][-1]["detailJson"])["timezone"], "Asia/Shanghai")

        published_task = task_service.create_douyin_batch_task(self.batch)
        published_task_id = published_task["id"]
        published_item_id = task_service.get_task(published_task_id)["items"][0]["id"]
        self._write_final_receipt(
            published_task_id,
            published_item_id,
            event_type="platform_publish_receipt",
            readback={
                "platformPostId": "post-001",
                "publishedAt": "2026-08-07 09:00",
                "timezone": "Asia/Shanghai",
            },
            timezone="Asia/Shanghai",
            message="平台已发布",
        )

        self.assertEqual(
            task_service.get_task(published_task_id)["items"][0]["status"],
            "success",
        )

    def test_scheduled_receipt_requires_controlled_source_and_beijing_time(self) -> None:
        task = task_service.create_douyin_batch_task(self.batch)
        task_id = task["id"]
        item_id = task_service.get_task(task_id)["items"][0]["id"]

        for readback in (
            {"scheduleTime": "2026-08-07"},
            {"scheduleTime": True},
            {"scheduleTime": "2026-08-07 09:00"},
        ):
            task_service.mark_batch_item_result(
                task_id,
                item_id,
                ok=True,
                message="平台已定时",
                event_type="platform_scheduled_receipt",
                readback=readback,
            )
            self.assertEqual(task_service.get_task(task_id)["items"][0]["status"], "running")

    def test_publish_receipt_rejects_boolean_and_non_string_identity(self) -> None:
        task = task_service.create_douyin_batch_task(self.batch)
        task_id = task["id"]
        item_id = task_service.get_task(task_id)["items"][0]["id"]

        for readback in ({"platformPostId": True}, {"platformPostId": 1}, {"postUrl": False}):
            task_service.mark_batch_item_result(
                task_id,
                item_id,
                ok=True,
                message="平台已发布",
                event_type="platform_publish_receipt",
                readback=readback,
            )
            self.assertEqual(task_service.get_task(task_id)["items"][0]["status"], "running")

    def test_readback_is_whitelisted_before_persistence(self) -> None:
        task = task_service.create_douyin_batch_task(self.batch)
        task_id = task["id"]
        item_id = task_service.get_task(task_id)["items"][0]["id"]

        task_service.mark_batch_item_result(
            task_id,
            item_id,
            ok=True,
            message="平台已定时",
            event_type="platform_scheduled_receipt",
            readback={
                "scheduleTime": "2026-08-07 09:00",
                "platformPostId": "post-001",
                "cookie": "must-not-store",
                "accessToken": "must-not-store",
                "session": {"id": "must-not-store"},
                "account": "must-not-store",
            },
        )

        event = task_service.get_task(task_id)["events"][-1]
        self.assertEqual(
            json.loads(event["detailJson"]),
            {"scheduleTime": "2026-08-07 09:00", "platformPostId": "post-001"},
        )

    def test_location_failure_diagnostic_is_whitelisted_before_persistence(self) -> None:
        """诊断事件必须只落库公开字段及其严格的原生值类型。"""

        task = task_service.create_douyin_batch_task(self.batch)
        task_id = task["id"]
        item_id = task_service.get_task(task_id)["items"][0]["id"]

        class ForgedStr(str):
            pass

        task_service.mark_batch_item_result(
            task_id,
            item_id,
            ok=False,
            message="发布定位恢复失败：累计检查 100 个不同地点、加载 7 次仍未命中目标（错误码 publish_location_candidate_limit）",
            event_type="batch_item_failed",
            readback={
                "errorCode": "publish_location_candidate_limit",
                "stage": ForgedStr("load_more"),
                "keyword": "夜南香",
                "loadMoreClicks": 7,
                "candidateCount": 100,
                "candidateLimit": "100",
                "clickLimit": True,
                "operationTimeoutSeconds": 30.0,
                "stringCount": "100",
                "cookie": "must-not-store",
                "dom": "must-not-store",
                "path": "/private/platform/profile",
                "unknownField": "must-not-store",
            },
        )

        detail = json.loads(task_service.get_task(task_id)["events"][-1]["detailJson"])
        self.assertEqual(
            detail,
            {
                "errorCode": "publish_location_candidate_limit",
                "keyword": "夜南香",
                "loadMoreClicks": 7,
                "candidateCount": 100,
            },
        )

    def test_location_failure_scope_is_whitelisted_only_as_native_nonempty_text(self) -> None:
        """范围绑定字段缺失或接受伪造类型时，地点诊断会失去可审计边界。"""

        class ForgedStr(str):
            pass

        cases = (
            ("local", "local"),
            (ForgedStr("local"), None),
            ("", None),
            ("   ", None),
            (True, None),
            (7, None),
            (7.0, None),
        )
        for value, expected in cases:
            with self.subTest(value=repr(value)):
                task = task_service.create_douyin_batch_task(self.batch)
                task_id = task["id"]
                item_id = task_service.get_task(task_id)["items"][0]["id"]
                task_service.mark_batch_item_result(
                    task_id,
                    item_id,
                    ok=False,
                    message="地点范围恢复失败",
                    event_type="batch_item_failed",
                    readback={
                        "scope": value,
                        "candidateList": ["must-not-store"],
                        "cookie": "must-not-store",
                        "dom": "must-not-store",
                        "path": "/private/platform/profile",
                        "unknownField": "must-not-store",
                    },
                )

                detail = json.loads(task_service.get_task(task_id)["events"][-1]["detailJson"])
                if expected is None:
                    self.assertNotIn("scope", detail)
                else:
                    self.assertIn("scope", detail)
                    self.assertEqual(detail["scope"], expected)
                for forbidden in ("candidateList", "cookie", "dom", "path", "unknownField"):
                    self.assertNotIn(forbidden, detail)

    def test_normal_event_after_success_does_not_roll_back_item_status(self) -> None:
        task = task_service.create_douyin_batch_task(self.batch)
        task_id = task["id"]
        item_id = task_service.get_task(task_id)["items"][0]["id"]
        self._write_final_receipt(
            task_id,
            item_id,
            event_type="platform_scheduled_receipt",
            readback={
                "scheduleTime": "2026-08-07 09:00",
                "timezone": "Asia/Shanghai",
            },
            timezone="Asia/Shanghai",
            message="平台已定时",
        )

        task_service.mark_batch_item_result(
            task_id,
            item_id,
            ok=True,
            message="编辑页状态补充",
            event_type="editor_written",
            readback={"session": "must-not-store"},
        )

        self.assertEqual(task_service.get_task(task_id)["items"][0]["status"], "success")

    def test_legacy_task_success_writers_cannot_bypass_batch_receipts(self) -> None:
        task = task_service.create_douyin_batch_task(self.batch)
        task_id = task["id"]

        legacy_writers = (
            lambda: publish_tasks.mark_items(task_id, "success", "伪造成功"),
            lambda: publish_tasks.mark_platform_results(
                task_id, [{"type": 3, "ok": True}], "伪造平台成功"
            ),
            lambda: publish_tasks.complete_task(task_id, "伪造完成"),
        )
        for writer in legacy_writers:
            with self.subTest(writer=writer), self.assertRaisesRegex(ValueError, "批量执行器"):
                writer()
            self.assertEqual(
                [item["status"] for item in task_service.get_task(task_id)["items"]],
                ["pending", "pending", "pending"],
            )

    def test_legacy_publish_runtime_rejects_batch_before_any_success_writer(self) -> None:
        task = task_service.create_douyin_batch_task(self.batch)
        task_id = task["id"]
        payload = json.loads(task_service.get_task(task_id)["payloadJson"])[0]

        for runner, arguments in (
            (publish_runtime.execute_single_publish, (payload, task)),
            (publish_runtime.execute_batch_publish, ([payload], task)),
        ):
            with self.subTest(runner=runner.__name__):
                result = runner(*arguments)
                self.assertEqual(result["code"], 409)
                self.assertIn("批量执行器", result["msg"])
                self.assertEqual(
                    [item["status"] for item in task_service.get_task(task_id)["items"]],
                    ["pending", "pending", "pending"],
                )

    def test_generic_desktop_publish_service_cannot_create_a_success_path_for_batch_item(self) -> None:
        task = task_service.create_douyin_batch_task(self.batch)
        batch_payload = json.loads(task_service.get_task(task["id"])["payloadJson"])[0]

        with self.assertRaisesRegex(ValueError, "批量执行器"):
            publish_service._validate_payloads([batch_payload])

        generic_task = task_service.create_pending_task(
            [batch_payload], mode="oneclick_publish"
        )
        with self.assertRaisesRegex(ValueError, "批量执行器"):
            task_service.mark_platform_result(
                generic_task["id"],
                3,
                ok=True,
                message="通用服务伪造成功",
                content_type="video",
                event_type="platform_publish",
            )
        self.assertEqual(
            task_service.get_task(generic_task["id"])["items"][0]["status"],
            "pending",
        )

    def test_public_batch_module_does_not_expose_a_dict_to_success_bridge(self) -> None:
        task = task_service.create_douyin_batch_task(self.batch)
        task_id = task["id"]
        item_id = task_service.get_task(task_id)["items"][0]["id"]

        self.assertFalse(
            hasattr(
                douyin_commerce_batch_executor,
                "write_verified_platform_result",
            )
        )
        self.assertEqual(task_service.get_task(task_id)["items"][0]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
