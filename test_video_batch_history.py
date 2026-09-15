import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication

from app_core.video_batch_contract import fingerprint
from app_core.video_batch_queue import BatchQueue
from app_core.video_batch_service import list_batches, batch_details
from ui.task_page import TaskPage
from ui.video_batch_history import VideoBatchHistoryDialog


class VideoBatchHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        accounts = patch('app_core.account_service.list_accounts', return_value=[])
        accounts.start(); self.addCleanup(accounts.stop)
        self.queue = BatchQueue(Path(self.tmp.name) / 'queue.db')
        media = Path(self.tmp.name) / 'a.mp4'; media.write_bytes(b'a')
        item = {'itemId': 'a', 'path': str(media), 'sha256': 'x',
                'content': {'title': '短标题', 'body': '正文', 'tags': []},
                'coverPath': '', 'coverSha256': None, 'scheduledAt': '2099-01-01 09:00',
                'timezone': 'Asia/Shanghai', 'fingerprint': 'fp'}
        snapshot = {'platform': '视频号', 'accountId': 8, 'items': [item]}
        snapshot['snapshotHash'] = fingerprint(snapshot)
        self.batch_id = self.queue.create(snapshot)

    def test_service_projects_list_and_details_without_secrets(self):
        rows = list_batches(queue=self.queue)
        self.assertEqual(rows[0]['statusLabel'], '待执行')
        self.assertEqual(rows[0]['accountId'], 8)
        details = batch_details(self.batch_id, queue=self.queue)
        self.assertEqual(details['items'][0]['fileName'], 'a.mp4')
        self.assertEqual(details['items'][0]['title'], '短标题')
        self.assertNotIn('owner', repr(details))
        self.assertNotIn('authorization', repr(details))

    def test_dialog_renders_list_and_item_detail(self):
        with patch('ui.video_batch_history.list_batches', side_effect=lambda: list_batches(queue=self.queue)), \
             patch('ui.video_batch_history.batch_details', side_effect=lambda bid: batch_details(bid, queue=self.queue)):
            dialog = VideoBatchHistoryDialog()
            self.addCleanup(dialog.close)
            self.assertEqual(dialog.batch_table.rowCount(), 1)
            self.assertEqual(dialog.item_table.item(0, 1).text(), '短标题')
            self.assertIn('平台定时受理', dialog.notice.text())

    def test_task_page_button_opens_independent_history(self):
        with patch('ui.task_page.task_service.list_tasks', return_value=[]), \
             patch('ui.video_batch_history.VideoBatchHistoryDialog.exec', return_value=0) as opened:
            page = TaskPage(); self.addCleanup(page.close)
            page.video_batch_history_button.click()
            opened.assert_called_once()

    def test_refresh_error_preserves_existing_rows_and_explains_failure(self):
        with patch('ui.video_batch_history.list_batches', side_effect=lambda: list_batches(queue=self.queue)), \
             patch('ui.video_batch_history.batch_details', side_effect=lambda bid: batch_details(bid, queue=self.queue)):
            dialog = VideoBatchHistoryDialog(); self.addCleanup(dialog.close)
        with patch('ui.video_batch_history.list_batches', side_effect=OSError('disk')):
            dialog.refresh()
        self.assertEqual(dialog.batch_table.rowCount(), 1)
        self.assertIn('已保留上次显示', dialog.error.text())

    def test_dialog_safe_cancel_changes_only_pending(self):
        with patch('ui.video_batch_history.list_batches', side_effect=lambda: list_batches(queue=self.queue)), \
             patch('ui.video_batch_history.batch_details', side_effect=lambda bid: batch_details(bid, queue=self.queue)), \
             patch('ui.video_batch_history.cancel_batch', side_effect=lambda bid: self.queue.cancel(bid)):
            dialog = VideoBatchHistoryDialog(); self.addCleanup(dialog.close)
            dialog.cancel_pending()
        self.assertEqual(self.queue.status(self.batch_id)['items'][0]['status'], 'cancelled')

    def test_failed_detail_switch_keeps_previous_batch_identity(self):
        row_a = list_batches(queue=self.queue)[0]
        row_b = {**row_a, 'batchId': 'batch-b'}
        detail_a = batch_details(self.batch_id, queue=self.queue)
        with patch('ui.video_batch_history.list_batches', return_value=[row_a, row_b]), \
             patch('ui.video_batch_history.batch_details', side_effect=lambda bid: detail_a if bid == self.batch_id else (_ for _ in ()).throw(OSError('broken'))):
            dialog = VideoBatchHistoryDialog(); self.addCleanup(dialog.close)
            dialog.batch_table.setCurrentCell(1, 0)
        self.assertIn(self.batch_id, dialog.detail_identity.text())
        self.assertNotIn('batch-b', dialog.detail_identity.text())
        self.assertIn('已保留上次显示', dialog.error.text())

    def test_batch_cleanup_failure_reason_is_visible(self):
        self.queue.cleanup_failed(self.batch_id, 'video_batch_session_cleanup_failed', 'close failed')
        with patch('ui.video_batch_history.list_batches', side_effect=lambda: list_batches(queue=self.queue)), \
             patch('ui.video_batch_history.batch_details', side_effect=lambda bid: batch_details(bid, queue=self.queue)):
            dialog = VideoBatchHistoryDialog(); self.addCleanup(dialog.close)
        self.assertIn('清理失败', dialog.detail_identity.text())
        self.assertIn('video_batch_session_cleanup_failed', dialog.detail_identity.text())
        self.assertIn('close failed', dialog.detail_identity.text())


if __name__ == '__main__':
    unittest.main()
