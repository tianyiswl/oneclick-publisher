# -*- coding: utf-8 -*-
"""任务明细窗口回归测试。"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QLabel, QTableWidget

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
                    "locationSummary": "夜南香北京烤鸭(蓬莱店)（山东省烟台市蓬莱区）",
                    "scheduleSummary": "北京时间定时 2026-08-09 21:30",
                    "status": "failed",
                    "message": "抖音带货位置搜索失败：抖音带货模式当前值无法识别",
                    "attempts": 1,
                },
                {
                    "batchItemIndex": 1,
                    "fileName": "b2e76584-92ee-11f1-aa95-16d46793b38a_测试24.mp4",
                    "filePath": "/tmp/b2e76584-92ee-11f1-aa95-16d46793b38a_测试24.mp4",
                    "locationSummary": "夜南香北京烤鸭（陕西省西安市）",
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
        self.assertEqual(table.item(0, 5).text(), "平台已回读")
        self.assertIn("b2e76584-", table.item(0, 1).toolTip())
        self.assertEqual(table.item(1, 0).text(), "12")
        self.assertEqual(table.item(1, 1).text(), "测试13.mp4")
        self.assertIn("抖音带货位置搜索失败", table.item(1, 5).text())


if __name__ == "__main__":
    unittest.main()
