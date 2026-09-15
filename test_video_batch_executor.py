import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

from app_core.video_batch_contract import fingerprint
from app_core.video_batch_executor import BatchAdapterError, execute_batch
from app_core.video_batch_queue import BatchQueue


class FakeAdapter:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.outcome = None
        self.events = []

    def open(self, account_id): self.events.append(('open', account_id))
    def open_item(self, item): self.events.append(('open_item', item['itemId']))
    def prepare(self, item):
        self.events.append(('prepare', item['itemId']))
        self.outcome = next(self.outcomes)
        if isinstance(self.outcome, BatchAdapterError) and not self.outcome.after_submission:
            raise self.outcome
    def submit(self, item):
        self.events.append(('submit', item['itemId']))
        outcome = self.outcome
        if isinstance(outcome, Exception): raise outcome
        return outcome
    def close_item(self): self.events.append(('close_item',))
    def close(self): self.events.append(('close',))


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.queue = BatchQueue(Path(self.tmp.name) / 'queue.db')
        self.files = []
        items = []
        for name in ('a', 'b'):
            path = Path(self.tmp.name) / f'{name}.mp4'
            path.write_bytes(name.encode())
            self.files.append(path)
            item = {'itemId': name, 'path': str(path), 'sha256': sha256(name.encode()).hexdigest(),
                    'content': {'title': name, 'body': '', 'tags': []}, 'coverPath': '',
                    'coverSha256': None, 'scheduledAt': None, 'timezone': 'Asia/Shanghai'}
            item['fingerprint'] = fingerprint(item)
            items.append(item)
        self.snapshot = {'platform': '视频号', 'accountId': 7, 'items': items}
        self.snapshot['snapshotHash'] = fingerprint(self.snapshot)

    def start(self):
        batch = self.queue.create(self.snapshot)
        token = self.queue.authorize(batch, self.snapshot['snapshotHash'])
        return batch, token

    def test_serial_execution_continues_after_pre_submit_failure(self):
        batch, token = self.start()
        adapter = FakeAdapter([BatchAdapterError('bad fields', 'fields_invalid'),
                               {'status': 'success', 'platformPostId': 'post-b'}])
        result = execute_batch(self.queue, batch, token, lambda: adapter)
        self.assertEqual(result['status'], 'partial_failure')
        self.assertEqual([i['status'] for i in result['items']], ['failed', 'success'])
        self.assertEqual(adapter.events.count(('open', 7)), 1)
        self.assertEqual(adapter.events.count(('close_item',)), 2)

    def test_unknown_after_submission_pauses_and_does_not_run_next(self):
        batch, token = self.start()
        adapter = FakeAdapter([BatchAdapterError('lost', 'submit_unknown', after_submission=True)])
        result = execute_batch(self.queue, batch, token, lambda: adapter)
        self.assertEqual(result['status'], 'paused')
        self.assertEqual([i['status'] for i in result['items']], ['unknown', 'pending'])
        self.assertNotIn(('open_item', 'b'), adapter.events)

    def test_changed_snapshot_file_is_rejected_before_adapter_open(self):
        batch, token = self.start()
        self.files[0].write_bytes(b'changed')
        adapter = FakeAdapter([])
        result = execute_batch(self.queue, batch, token, lambda: adapter)
        self.assertEqual(result['items'][0]['receipt']['errorCode'], 'video_batch_snapshot_changed')
        self.assertNotIn(('open', 7), adapter.events)

    def test_cancel_only_cancels_pending(self):
        batch, token = self.start()
        result = self.queue.cancel(batch)
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual([i['status'] for i in result['items']], ['cancelled', 'cancelled'])

    def test_adapter_factory_exception_abandons_claim(self):
        batch, token = self.start()
        with self.assertRaisesRegex(RuntimeError, 'factory'):
            execute_batch(self.queue, batch, token, lambda: (_ for _ in ()).throw(RuntimeError('factory')))
        self.assertEqual(self.queue.status(batch)['status'], 'paused')

    def test_scheduled_item_cannot_return_immediate_success(self):
        self.snapshot['items'][0]['scheduledAt'] = '2099-01-01 09:00'
        self.snapshot['snapshotHash'] = fingerprint({k: v for k, v in self.snapshot.items() if k != 'snapshotHash'})
        batch, token = self.start()
        result = execute_batch(self.queue, batch, token, lambda: FakeAdapter([
            {'status': 'success', 'platformPostId': 'wrong'}]))
        self.assertEqual(result['items'][0]['status'], 'unknown')

    def test_receipt_id_must_be_nonempty_string(self):
        batch, token = self.start()
        result = execute_batch(self.queue, batch, token, lambda: FakeAdapter([
            {'status': 'success', 'platformPostId': 123}]))
        self.assertEqual(result['items'][0]['status'], 'unknown')


if __name__ == '__main__':
    unittest.main()
