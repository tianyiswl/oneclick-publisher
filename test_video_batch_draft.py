import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from app_core.video_batch_draft_service import save_draft, load_draft
from app_core.video_batch_contract import VideoBatchError


class VideoBatchDraftTests(unittest.TestCase):
    def test_incomplete_draft_roundtrip(self):
        with tempfile.TemporaryDirectory() as root:
            value = {'platform': '视频号', 'accountId': None, 'items': [], 'defaults': {'body': '正文\n第二行'}}
            save_draft(value, root=root)
            self.assertEqual(load_draft(root=root), value)

    def test_atomic_failure_retains_previous_draft(self):
        with tempfile.TemporaryDirectory() as root:
            value = {'platform': '视频号', 'items': []}
            save_draft(value, root=root)
            with patch('app_core.video_batch_draft_service.os.replace', side_effect=OSError('disk')):
                with self.assertRaises(OSError):
                    save_draft({**value, 'accountId': 2}, root=root)
            self.assertEqual(load_draft(root=root), value)
            self.assertEqual(len(list(Path(root).iterdir())), 1)

    def test_nested_credentials_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(VideoBatchError):
                save_draft({'platform': '视频号', 'items': [{'overrides': {'cookie': 'test'}}]}, root=root)

    def test_invalid_widget_types_are_rejected(self):
        invalid = [
            {'platform': '视频号', 'items': [{'itemId': 1, 'path': '/a.mp4'}]},
            {'platform': '视频号', 'items': [], 'schedulePolicy': {'mode': 'interval', 'intervalMinutes': 'bad'}},
            {'platform': '视频号', 'items': [], 'schedulePolicy': {'mode': 'daily', 'times': ['09:00', 3]}},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(VideoBatchError):
                save_draft(value, root=tempfile.mkdtemp())
