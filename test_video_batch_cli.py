import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class BatchCliTests(unittest.TestCase):
    def test_real_cli_local_check_in_isolated_directory(self):
        with tempfile.TemporaryDirectory() as root:
            env = {**os.environ, 'YIJIANFA_USER_DATA_DIR': root, 'QT_QPA_PLATFORM': 'offscreen'}
            subprocess.run([sys.executable, '-c',
                "from app_core.database import connect\nwith connect() as conn:\n conn.execute(\"INSERT INTO user_info(id,type,filePath,userName,status) VALUES (1,2,'test.json','离线测试',1)\")"],
                env=env, check=True, capture_output=True, timeout=30)
            media = Path(root) / 'test.mp4'
            media.write_bytes(b'offline-content-contract-fixture')
            request = {'platform': '视频号', 'accountId': 1, 'defaults': {'title': '测试标题', 'body': '测试正文', 'tags': []},
                       'items': [{'itemId': 'one', 'path': str(media)}], 'schedulePolicy': {'mode': 'immediate'}}
            result = subprocess.run([sys.executable, 'desktop_native_app.py', '--controlled-publish-action', 'batch-check',
                                     '--controlled-publish-request', '-'], input=json.dumps(request), text=True,
                                    capture_output=True, env=env, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            response = json.loads(result.stdout)
            self.assertEqual(response['status'], 'local_check_passed')
            self.assertFalse(response['platformWriteOccurred'])
            self.assertFalse(response['formalAvailable'])

    def test_cli_prepare_then_status_readback(self):
        with tempfile.TemporaryDirectory() as root:
            env = {**os.environ, 'YIJIANFA_USER_DATA_DIR': root, 'QT_QPA_PLATFORM': 'offscreen'}
            subprocess.run([sys.executable, '-c',
                "from app_core.database import connect\nwith connect() as conn:\n conn.execute(\"INSERT INTO user_info(id,type,filePath,userName,status) VALUES (1,2,'test.json','离线测试',1)\")"],
                env=env, check=True, capture_output=True, timeout=30)
            media = Path(root) / 'test.mp4'; media.write_bytes(b'offline')
            request = {'platform': '视频号', 'accountId': 1, 'defaults': {'title': '测试', 'body': '', 'tags': []},
                       'items': [{'itemId': 'one', 'path': str(media)}], 'schedulePolicy': {'mode': 'immediate'}}
            prepared = subprocess.run([sys.executable, 'desktop_native_app.py', '--controlled-publish-action', 'batch-prepare',
                '--controlled-publish-request', '-'], input=json.dumps(request), text=True, capture_output=True, env=env, timeout=30)
            self.assertEqual(prepared.returncode, 0, prepared.stderr)
            batch_id = json.loads(prepared.stdout)['batchId']
            status = subprocess.run([sys.executable, 'desktop_native_app.py', '--controlled-publish-action', 'batch-status',
                '--controlled-publish-batch-id', batch_id], text=True, capture_output=True, env=env, timeout=30)
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertEqual(json.loads(status.stdout)['status'], 'prepared')
            cancelled = subprocess.run([sys.executable, 'desktop_native_app.py', '--controlled-publish-action', 'batch-cancel',
                '--controlled-publish-batch-id', batch_id], text=True, capture_output=True, env=env, timeout=30)
            self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
            self.assertEqual(json.loads(cancelled.stdout)['items'][0]['status'], 'cancelled')

    def test_formal_batch_actions_are_hard_gated(self):
        for action in ('batch-authorize', 'batch-start'):
            result = subprocess.run([sys.executable, 'desktop_native_app.py', '--controlled-publish-action', action],
                                    text=True, capture_output=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            response = json.loads(result.stdout)
            self.assertEqual(response['errorCode'], 'video_batch_formal_unavailable')

    def test_status_recovers_expired_running_batch(self):
        with tempfile.TemporaryDirectory() as root:
            env = {**os.environ, 'YIJIANFA_USER_DATA_DIR': root, 'QT_QPA_PLATFORM': 'offscreen'}
            script = """from app_core.video_batch_service import default_queue
from app_core.video_batch_contract import fingerprint
q=default_queue(); s={'platform':'视频号','accountId':1,'items':[{'itemId':'a','fingerprint':'f'}]}; s['snapshotHash']=fingerprint(s)
b=q.create(s); t=q.authorize(b,s['snapshotHash'],now=1); q.claim(b,t,now=2); print(b)
"""
            made = subprocess.run([sys.executable, '-c', script], env=env, text=True, capture_output=True, timeout=30, check=True)
            result = subprocess.run([sys.executable, 'desktop_native_app.py', '--controlled-publish-action', 'batch-status',
                '--controlled-publish-batch-id', made.stdout.strip()], env=env, text=True, capture_output=True, timeout=30)
            self.assertEqual(json.loads(result.stdout)['status'], 'paused')
