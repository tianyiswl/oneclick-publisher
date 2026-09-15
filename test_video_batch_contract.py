import copy
import tempfile
import unittest
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from app_core.video_batch_contract import prepare_batch, VideoBatchError


class VideoBatchContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = [Path(self.tmp.name) / f'{i}.mp4' for i in range(2)]
        for i, path in enumerate(self.paths):
            path.write_bytes(b'video' + bytes([i]))
        self.now = datetime(2026, 9, 12, 8, tzinfo=ZoneInfo('Asia/Shanghai'))
        self.request = {'platform': '视频号', 'accountId': 2,
                        'defaults': {'title': '标题', 'body': '正文\n下一行', 'tags': ['探店']},
                        'items': [{'itemId': str(i), 'path': str(p), 'overrides': {}}
                                  for i, p in enumerate(self.paths)],
                        'schedulePolicy': {'mode': 'immediate'}}

    def prepare(self):
        return prepare_batch(self.request, now=self.now)

    def test_explicit_empty_override_is_not_inheritance(self):
        self.request['items'][0]['overrides'] = {'tags': [], 'body': ''}
        result = self.prepare()
        self.assertEqual(result['items'][0]['content']['tags'], [])
        self.assertEqual(result['items'][0]['content']['body'], '')
        self.assertEqual(result['items'][1]['content']['body'], '正文\n下一行')

    def test_source_is_not_mutated(self):
        before = copy.deepcopy(self.request)
        self.prepare()
        self.assertEqual(self.request, before)

    def test_reorder_retains_identity_and_publish_fingerprint(self):
        first = self.prepare()
        self.request['items'].reverse()
        second = self.prepare()
        self.assertEqual(first['items'][0]['fingerprint'], second['items'][1]['fingerprint'])
        self.assertNotEqual(first['snapshotHash'], second['snapshotHash'])

    def test_renamed_duplicate_rejected(self):
        self.paths[1].write_bytes(self.paths[0].read_bytes())
        with self.assertRaises(VideoBatchError):
            self.prepare()

    def test_file_change_changes_snapshot(self):
        before = self.prepare()['snapshotHash']
        self.paths[0].write_bytes(b'changed')
        self.assertNotEqual(before, self.prepare()['snapshotHash'])

    def test_interval_schedule(self):
        self.request['schedulePolicy'] = {'mode': 'interval', 'start': '2026-09-12 09:00', 'intervalMinutes': 30}
        self.assertEqual(self.prepare()['items'][1]['scheduledAt'], '2026-09-12 09:30')

    def test_daily_rolls_over(self):
        self.request['schedulePolicy'] = {'mode': 'daily', 'startDate': '2026-09-12', 'times': ['09:00']}
        self.assertEqual(self.prepare()['items'][1]['scheduledAt'], '2026-09-13 09:00')

    def test_stale_schedule_rejected(self):
        self.request['items'][0]['scheduledAt'] = '2026-09-12 07:00'
        with self.assertRaises(VideoBatchError):
            self.prepare()

    def test_account_and_platform_are_strict(self):
        self.request['accountId'] = True
        with self.assertRaises(VideoBatchError):
            self.prepare()

    def test_unknown_credentials_rejected(self):
        self.request['cookie'] = 'not-a-real-cookie'
        with self.assertRaises(VideoBatchError):
            self.prepare()

    def test_short_title_not_silently_truncated(self):
        self.request['defaults']['title'] = '一' * 11
        with self.assertRaises(VideoBatchError):
            self.prepare()

    def test_duplicate_item_id_rejected(self):
        self.request['items'][1]['itemId'] = '0'
        with self.assertRaises(VideoBatchError):
            self.prepare()
