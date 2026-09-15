import tempfile
import unittest
from pathlib import Path
from app_core.video_batch_queue import BatchQueue
from app_core.video_batch_contract import fingerprint, VideoBatchError


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.queue = BatchQueue(Path(self.tmp.name) / 'queue.db')
        self.snapshot = {'platform': '视频号', 'accountId': 1,
                         'items': [{'itemId': 'a', 'fingerprint': 'unique'}]}
        self.snapshot['snapshotHash'] = fingerprint(self.snapshot)

    def start(self):
        bid = self.queue.create(self.snapshot)
        token = self.queue.authorize(bid, self.snapshot['snapshotHash'], now=100)
        return bid, token, self.queue.claim(bid, token, now=101)

    def test_authorization_single_use(self):
        bid, token, owner = self.start()
        with self.assertRaises(VideoBatchError):
            self.queue.claim(bid, token, now=102)

    def test_crash_after_intent_is_unknown(self):
        bid, _, owner = self.start()
        self.queue.submission_intent(bid, 'a', owner, now=102)
        self.assertEqual(self.queue.recover(now=162), [bid])
        self.assertEqual(self.queue.status(bid)['items'][0]['status'], 'unknown')
        with self.assertRaises(VideoBatchError):
            self.queue.submission_intent(bid, 'a', owner, now=163)

    def test_other_batch_cannot_repeat_unknown(self):
        bid, _, owner = self.start()
        self.queue.submission_intent(bid, 'a', owner, now=102)
        second, _, owner2 = self.start()
        with self.assertRaises(VideoBatchError):
            self.queue.submission_intent(second, 'a', owner2, now=103)

    def test_before_submit_crash_keeps_pending(self):
        bid, _, _ = self.start()
        self.queue.recover(now=162)
        self.assertEqual(self.queue.status(bid)['items'][0]['status'], 'pending')

    def test_success_requires_receipt_and_closes_batch(self):
        bid, _, owner = self.start()
        self.queue.submission_intent(bid, 'a', owner, now=102)
        with self.assertRaises(VideoBatchError):
            self.queue.finish_item(bid, 'a', owner, 'success', now=103)
        self.queue.finish_item(bid, 'a', owner, 'success', {'platformPostId': 'test'}, now=104)
        self.assertEqual(self.queue.status(bid)['status'], 'success')

    def test_expired_owner_cannot_write_result(self):
        bid, _, owner = self.start()
        self.queue.submission_intent(bid, 'a', owner, now=102)
        with self.assertRaises(VideoBatchError):
            self.queue.finish_item(bid, 'a', owner, 'success', {'platformPostId': 'test'}, now=200)

    def test_recover_records_structured_pause_reason(self):
        bid, _, owner = self.start()
        self.queue.submission_intent(bid, 'a', owner, now=102)
        self.queue.recover(now=162)
        status = self.queue.status(bid)
        self.assertEqual(status['stage'], 'lease_expired')
        self.assertEqual(status['errorCode'], 'video_batch_lease_expired')
        self.assertEqual(status['currentItemId'], 'a')
        self.assertEqual(status['items'][0]['stage'], 'lease_expired')
        self.assertEqual(status['items'][0]['receipt']['errorCode'], 'video_batch_lease_expired')

    def test_pre_submit_pause_records_reason_on_pending_item(self):
        bid, _, owner = self.start()
        self.queue.begin_item(bid, 'a', owner, now=102)
        self.queue.pause(bid, owner, 'verification_required', '需要验证', now=103)
        item = self.queue.status(bid)['items'][0]
        self.assertEqual(item['status'], 'pending')
        self.assertEqual(item['stage'], 'paused')
        self.assertEqual(item['receipt'], {'errorCode': 'verification_required',
                                           'errorText': '需要验证', 'stage': 'paused'})

    def test_cleanup_failure_is_visible_after_success(self):
        bid, _, owner = self.start()
        self.queue.submission_intent(bid, 'a', owner, now=102)
        self.queue.finish_item(bid, 'a', owner, 'success', {'platformPostId': 'test'}, now=103)
        self.queue.cleanup_failed(bid, 'video_batch_item_cleanup_failed', 'dirty page')
        status = self.queue.status(bid)
        self.assertEqual(status['status'], 'paused')
        self.assertEqual(status['items'][0]['status'], 'success')
        self.assertEqual(status['errorCode'], 'video_batch_item_cleanup_failed')
