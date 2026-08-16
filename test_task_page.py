# -*- coding: utf-8 -*-
"""任务明细窗口回归测试。"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QComboBox, QLabel, QTableWidget, QTabWidget

from ui.task_page import TaskDetailDialog


class TaskDetailDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _batch_task(self) -> dict:
        return {
            "taskNo": "T08082217-665B",
            "taskNoDisplay": "T08082217-665B",
            "contentTypeLabel": "视频",
            "workflow": "douyin-commerce-batch",
            "workflowLabel": "抖音带货批量",
            "status": "partial_failed",
            "title": "测试",
            "accountSummary": "3199｜逆浪风",
            "platformSummary": "抖音",
            "createdAt": "2026-08-08 22:17:06",
            "finishedAt": "2026-08-08 22:38:12",
            "itemCount": 2,
            "successCount": 1,
            "failedCount": 1,
            "lastError": "第 12 条视频地点设置失败",
            "commerceSummary": "不应在批量任务顶部显示的超长带货摘要",
            "items": [
                {
                    "batchItemIndex": 12,
                    "fileName": "b24dad24-92ee-11f1-aa95-16d46793b38a_测试13.mp4",
                    "filePath": "/tmp/b24dad24-92ee-11f1-aa95-16d46793b38a_测试13.mp4",
                    "locationSummary": "地点【返佣】（地址）",
                    "scheduleSummary": "北京时间定时 2026-08-09 21:30",
                    "status": "failed",
                    "message": "抖音带货位置搜索失败：抖音带货模式当前值无法识别",
                    "attempts": 1,
                },
                {
                    "batchItemIndex": 1,
                    "fileName": "b2e76584-92ee-11f1-aa95-16d46793b38a_测试24.mp4",
                    "filePath": "/tmp/b2e76584-92ee-11f1-aa95-16d46793b38a_测试24.mp4",
                    "locationSummary": "地点【无佣】（地址）",
                    "scheduleSummary": "北京时间定时 2026-08-09 16:00",
                    "status": "success",
                    "message": "抖音作品管理页已读到定时回执",
                    "attempts": 1,
                },
            ],
            "events": [],
            "payloadJson": "{}",
        }

    def test_batch_task_uses_compact_six_column_result_table(self) -> None:
        """缺少批量专用分支时，应捕获长摘要遮挡逐视频结果的回归。"""
        dialog = TaskDetailDialog(self._batch_task())
        self.addCleanup(dialog.close)

        summary = dialog.findChild(QLabel, "batchResultSummary")
        self.assertIsNotNone(summary)
        self.assertEqual(summary.text(), "共 2 条 · 成功 1 · 失败 1 · 未完成 0")
        self.assertNotIn(
            "不应在批量任务顶部显示的超长带货摘要",
            [label.text() for label in dialog.findChildren(QLabel)],
        )

        table = dialog.findChild(QTableWidget, "batchResultTable")
        self.assertIsNotNone(table)
        self.assertEqual(table.columnCount(), 6)
        self.assertEqual(
            [table.horizontalHeaderItem(index).text() for index in range(6)],
            ["序号", "视频", "地点", "定时时间", "状态", "结果说明"],
        )
        self.assertEqual(table.rowCount(), 2)
        self.assertEqual(table.item(0, 0).text(), "1")
        self.assertEqual(table.item(0, 1).text(), "测试24.mp4")
        self.assertEqual(table.item(0, 2).text(), "地点【无佣】（地址）")
        self.assertEqual(table.item(0, 2).toolTip(), "地点【无佣】（地址）")
        self.assertEqual(table.item(0, 5).text(), "平台已回读")
        self.assertIn("b2e76584-", table.item(0, 1).toolTip())
        self.assertEqual(table.item(1, 0).text(), "12")
        self.assertEqual(table.item(1, 1).text(), "测试13.mp4")
        self.assertEqual(table.item(1, 2).text(), "地点【返佣】（地址）")
        self.assertEqual(table.item(1, 2).toolTip(), "地点【返佣】（地址）")
        self.assertIn("抖音带货位置搜索失败", table.item(1, 5).text())

    def test_batch_task_filters_and_focuses_the_first_failed_video(self) -> None:
        """缺少失败定位或状态筛选时，应捕获失败视频仍难查找的回归。"""
        dialog = TaskDetailDialog(self._batch_task())
        self.addCleanup(dialog.close)

        status_filter = dialog.findChild(QComboBox, "batchStatusFilter")
        self.assertIsNotNone(status_filter)
        self.assertEqual(status_filter.itemText(0), "全部（2）")
        self.assertIn(
            "失败（1）",
            [status_filter.itemText(index) for index in range(status_filter.count())],
        )

        table = dialog.findChild(QTableWidget, "batchResultTable")
        self.assertEqual(table.currentRow(), 1)
        self.assertEqual(table.selectedItems(), [])
        self.assertEqual(table.item(1, 4).foreground().color().name().upper(), "#B42318")
        self.assertEqual(table.item(1, 4).background().color().name().upper(), "#FEF3F2")

        status_filter.setCurrentText("失败（1）")
        self.assertEqual(table.rowCount(), 1)
        self.assertEqual(table.item(0, 1).text(), "测试13.mp4")

    def test_batch_failure_row_and_event_show_exact_location_limit(self) -> None:
        """地点上限失败须同时保留逐视频提示和净化后的事件详情。"""

        message = (
            "发布定位恢复失败：累计检查 100 个不同地点、加载 7 次仍未命中目标"
            "（错误码 publish_location_candidate_limit）"
        )
        task = {
            **self._batch_task(),
            "items": [{**self._batch_task()["items"][0], "message": message}],
            "events": [
                {
                    "createdAt": "2026-08-08 22:38:12",
                    "level": "error",
                    "eventType": "batch_item_failed",
                    "message": message,
                    "detailJson": (
                        '{"errorCode":"publish_location_candidate_limit",'
                        '"loadMoreClicks":7,"candidateCount":100}'
                    ),
                }
            ],
        }
        dialog = TaskDetailDialog(task)
        self.addCleanup(dialog.close)

        self.assertIn(
            "累计检查 100 个不同地点",
            dialog.batch_result_table.item(0, 5).text(),
        )
        event_table = next(
            table for table in dialog.findChildren(QTableWidget) if table.columnCount() == 5
        )
        self.assertIn(
            "publish_location_candidate_limit",
            event_table.item(0, 4).text(),
        )

    def test_non_batch_task_keeps_the_generic_items_table(self) -> None:
        """批量专用表格不得改变普通任务的十列执行项明细。"""
        task = {
            **self._batch_task(),
            "workflow": "domestic-video",
            "workflowLabel": "国内视频",
            "commerceSummary": "普通任务带货信息",
            "items": [
                {
                    "platformName": "抖音",
                    "accountLabel": "逆浪风",
                    "fileName": "测试.mp4",
                    "status": "success",
                    "message": "完成",
                }
            ],
        }
        dialog = TaskDetailDialog(task)
        self.addCleanup(dialog.close)

        tabs = dialog.findChild(QTabWidget, "taskDetailTabs")
        self.assertEqual(tabs.tabText(0), "执行项")
        generic_tables = [
            table
            for table in dialog.findChildren(QTableWidget)
            if table.columnCount() == 10
        ]
        self.assertEqual(len(generic_tables), 1)
        self.assertIsNone(dialog.findChild(QComboBox, "batchStatusFilter"))

    def test_resume_button_is_visible_only_for_allowed_batch_and_only_emits_task_id(self) -> None:
        """若详情页绕过资格判断创建任务，或不可续发任务仍显示按钮，该测试必须失败。"""

        task = {**self._batch_task(), "id": 41, "status": "paused", "pauseReasonCode": "user_request"}
        with patch("ui.task_page.task_service.prepare_douyin_batch_resume") as prepared:
            prepared.return_value = {"resumeAllowed": True, "pendingCount": 2}
            dialog = TaskDetailDialog(task)
            self.addCleanup(dialog.close)
            emitted: list[int] = []
            dialog.resume_douyin_batch_requested.connect(emitted.append)
            self.assertFalse(dialog.resume_batch_button.isHidden())
            self.assertEqual(dialog.resume_batch_button.text(), "继续未开始的 2 条")
            dialog.resume_batch_button.click()
            self.assertEqual(emitted, [41])
        with patch("ui.task_page.task_service.prepare_douyin_batch_resume") as prepared:
            prepared.return_value = {"resumeAllowed": False, "pendingCount": 0}
            dialog = TaskDetailDialog(task)
            self.addCleanup(dialog.close)
            self.assertTrue(dialog.resume_batch_button.isHidden())


if __name__ == "__main__":
    unittest.main()
