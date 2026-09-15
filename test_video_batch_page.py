import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from PyQt6.QtWidgets import QApplication
from ui.video_batch_page import VideoBatchDialog


class VideoBatchPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        with patch('ui.video_batch_page.account_service.list_accounts', return_value=[{'id': 2, 'type': 2, 'userName': '测试账号'}]):
            self.dialog = VideoBatchDialog()
        self.addCleanup(self.dialog.close)
        self.dialog.add_paths(['/tmp/a.mp4', '/tmp/b.mp4'])

    def test_common_change_preserves_explicit_empty_tags(self):
        d = self.dialog
        d.defaults['tags'].setText('通用')
        d.overrides['tags'].setChecked(True)
        d.editors['tags'].clear()
        d.defaults['tags'].setText('改变')
        self.assertEqual(d.request()['items'][0]['overrides']['tags'], [])
        d.videos.setCurrentRow(1)
        self.assertEqual(d.editors['tags'].text(), '改变')

    def test_reorder_keeps_override_bound_to_media(self):
        d = self.dialog
        d.overrides['title'].setChecked(True)
        d.editors['title'].setText('独立标题')
        d.move(1)
        self.assertEqual(d.request()['items'][1]['path'], '/tmp/a.mp4')
        self.assertEqual(d.request()['items'][1]['overrides']['title'], '独立标题')

    def test_restore_common_removes_override(self):
        d = self.dialog
        d.overrides['body'].setChecked(True)
        d.editors['body'].setPlainText('独立')
        d.overrides['body'].setChecked(False)
        self.assertNotIn('body', d.request()['items'][0]['overrides'])

    def test_corrupt_draft_does_not_replace_current_form(self):
        self.dialog.defaults['title'].setText('保留我')
        with patch('ui.video_batch_page.load_draft', side_effect=ValueError('损坏')):
            self.dialog.restore()
        self.assertEqual(self.dialog.defaults['title'].text(), '保留我')

    def test_prepare_background_work_blocks_close(self):
        with patch.object(self.dialog.runner, 'is_running', side_effect=lambda name: name == 'batch-prepare'):
            self.dialog.reject()
        self.assertTrue(self.dialog.isVisible() or '等待' in self.dialog.status.text())
