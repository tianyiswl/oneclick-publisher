"""独立批次持久化：提交意图必须先于平台动作。"""
import json
import sqlite3
import time
from uuid import uuid4
from contextlib import contextmanager
from pathlib import Path
from .video_batch_contract import fingerprint, VideoBatchError


class BatchQueue:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS batches (
                  id TEXT PRIMARY KEY, snapshot TEXT NOT NULL, status TEXT NOT NULL,
                  owner TEXT, expires REAL, authorization TEXT, authorizationExpires REAL,
                  stage TEXT NOT NULL DEFAULT 'prepared', errorCode TEXT, errorText TEXT, currentItemId TEXT,
                  createdAt REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS items (
                  batchId TEXT NOT NULL REFERENCES batches(id), itemId TEXT NOT NULL,
                  fingerprint TEXT NOT NULL, status TEXT NOT NULL, stage TEXT NOT NULL DEFAULT 'pending', receipt TEXT,
                  PRIMARY KEY(batchId,itemId));
                CREATE UNIQUE INDEX IF NOT EXISTS active_publish_fingerprint ON items(fingerprint)
                  WHERE status IN ('submitting','unknown','success','scheduled');
            ''')
            columns = {row['name'] for row in conn.execute('PRAGMA table_info(items)')}
            if 'stage' not in columns:
                conn.execute("ALTER TABLE items ADD COLUMN stage TEXT NOT NULL DEFAULT 'pending'")
            batch_columns = {row['name'] for row in conn.execute('PRAGMA table_info(batches)')}
            for name, declaration in (
                ('stage', "TEXT NOT NULL DEFAULT 'prepared'"), ('errorCode', 'TEXT'),
                ('errorText', 'TEXT'), ('currentItemId', 'TEXT')):
                if name not in batch_columns:
                    conn.execute(f'ALTER TABLE batches ADD COLUMN {name} {declaration}')
            if 'createdAt' not in batch_columns:
                conn.execute("ALTER TABLE batches ADD COLUMN createdAt REAL NOT NULL DEFAULT 0")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def create(self, snapshot):
        if type(snapshot) is not dict or snapshot.get('platform') != '视频号' or type(snapshot.get('accountId')) is not int:
            raise VideoBatchError('批次快照结构无效')
        items = snapshot.get('items')
        if type(items) is not list or not items or any(type(item) is not dict for item in items):
            raise VideoBatchError('批次快照缺少视频条目')
        expected = snapshot.get('snapshotHash')
        if expected != fingerprint({k: v for k, v in snapshot.items() if k != 'snapshotHash'}):
            raise VideoBatchError('内容快照校验失败')
        batch_id = uuid4().hex
        with self.connect() as conn:
            conn.execute('INSERT INTO batches(id,snapshot,status,createdAt) VALUES (?,?,?,?)',
                         (batch_id, json.dumps(snapshot, ensure_ascii=False), 'prepared', time.time()))
            conn.executemany('INSERT INTO items(batchId,itemId,fingerprint,status) VALUES (?,?,?,?)',
                             [(batch_id, item['itemId'], item['fingerprint'], 'pending') for item in snapshot['items']])
        return batch_id

    def authorize(self, batch_id, snapshot_hash, *, now=None):
        now = time.time() if now is None else now
        token = uuid4().hex
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM batches WHERE id=?', (batch_id,)).fetchone()
            if not row or row['status'] != 'prepared' or json.loads(row['snapshot'])['snapshotHash'] != snapshot_hash:
                raise VideoBatchError('授权内容与待执行批次不一致')
            conn.execute('UPDATE batches SET authorization=?, authorizationExpires=? WHERE id=?',
                         (fingerprint(token), now + 600, batch_id))
        return token

    def claim(self, batch_id, token, *, now=None):
        now = time.time() if now is None else now
        owner = uuid4().hex
        with self.connect() as conn:
            result = conn.execute('''UPDATE batches SET status='running',stage='running',errorCode=NULL,errorText=NULL,
                currentItemId=NULL,owner=?, expires=?, authorization=NULL
                WHERE id=? AND status='prepared' AND authorization=? AND authorizationExpires>?''',
                                  (owner, now + 60, batch_id, fingerprint(token), now))
            if result.rowcount != 1:
                raise VideoBatchError('授权过期、已消费或批次不可启动')
        return owner

    def heartbeat(self, batch_id, owner, *, now=None):
        now = time.time() if now is None else now
        with self.connect() as conn:
            result = conn.execute("UPDATE batches SET expires=? WHERE id=? AND owner=? AND status='running' AND expires>?",
                                  (now + 60, batch_id, owner, now))
            if result.rowcount != 1:
                raise VideoBatchError('批次执行租约已失效')

    def submission_intent(self, batch_id, item_id, owner, *, now=None):
        now = time.time() if now is None else now
        try:
            with self.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                owned = conn.execute("SELECT id FROM batches WHERE id=? AND owner=? AND status='running' AND expires>?",
                                     (batch_id, owner, now)).fetchone()
                if not owned:
                    raise VideoBatchError('批次执行租约已失效')
                changed = conn.execute("UPDATE items SET status='submitting',stage='submission_intent' WHERE batchId=? AND itemId=? AND status='pending'",
                                       (batch_id, item_id))
                if changed.rowcount != 1:
                    raise VideoBatchError('此视频已执行，不得再次提交')
                conn.execute("UPDATE batches SET stage='submission_intent',currentItemId=? WHERE id=?", (item_id, batch_id))
        except sqlite3.IntegrityError as exc:
            raise VideoBatchError('同一内容已提交或结果待核对，禁止重复发布', 'video_batch_replay_blocked') from exc

    def begin_item(self, batch_id, item_id, owner, *, now=None):
        now = time.time() if now is None else now
        with self.connect() as conn:
            changed = conn.execute("""UPDATE batches SET stage='preparing',currentItemId=?
                WHERE id=? AND owner=? AND status='running' AND expires>?""",
                (item_id, batch_id, owner, now))
            if changed.rowcount != 1:
                raise VideoBatchError('批次执行租约已失效')

    def recover(self, *, now=None):
        now = time.time() if now is None else now
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            expired = [r['id'] for r in conn.execute("SELECT id FROM batches WHERE status='running' AND expires<=?", (now,))]
            for batch_id in expired:
                current = conn.execute("SELECT itemId FROM items WHERE batchId=? AND status='submitting' ORDER BY rowid LIMIT 1", (batch_id,)).fetchone()
                receipt = json.dumps({'errorCode': 'video_batch_lease_expired',
                                      'errorText': '执行租约过期，提交结果待核对',
                                      'stage': 'lease_expired'}, ensure_ascii=False)
                conn.execute("""UPDATE items SET status='unknown',stage='lease_expired',receipt=?
                    WHERE batchId=? AND status='submitting'""", (receipt, batch_id))
                conn.execute("""UPDATE batches SET status='paused',stage='lease_expired',
                    errorCode='video_batch_lease_expired',errorText='执行租约过期，已安全暂停',
                    currentItemId=?,owner=NULL,expires=NULL WHERE id=?""",
                    (current['itemId'] if current else None, batch_id))
        return expired

    def pause(self, batch_id, owner, code, text, *, now=None):
        now = time.time() if now is None else now
        with self.connect() as conn:
            batch = conn.execute("SELECT currentItemId FROM batches WHERE id=? AND owner=?", (batch_id, owner)).fetchone()
            changed = conn.execute("""UPDATE batches SET status='paused',stage='paused',errorCode=?,errorText=?,
                owner=NULL,expires=NULL WHERE id=? AND owner=? AND status='running' AND expires>?""",
                                   (code, text, batch_id, owner, now))
            if changed.rowcount != 1:
                raise VideoBatchError('批次执行租约已失效')
            if batch and batch['currentItemId']:
                receipt = json.dumps({'errorCode': code, 'errorText': text, 'stage': 'paused'}, ensure_ascii=False)
                conn.execute("""UPDATE items SET stage='paused',receipt=? WHERE batchId=? AND itemId=? AND status='pending'""",
                             (receipt, batch_id, batch['currentItemId']))
        return self.status(batch_id)

    def abandon(self, batch_id, owner, code='video_batch_executor_interrupted', text='执行已中断'):
        """本地执行器退出时收口租约；已写意图的条目保守转 unknown。"""
        with self.connect() as conn:
            row = conn.execute("SELECT id FROM batches WHERE id=? AND owner=? AND status='running'", (batch_id, owner)).fetchone()
            if not row:
                return self.status(batch_id)
            receipt = json.dumps({'errorCode': code, 'errorText': text, 'stage': 'submission_unknown'}, ensure_ascii=False)
            conn.execute("UPDATE items SET status='unknown',stage='submission_unknown',receipt=? WHERE batchId=? AND status='submitting'",
                         (receipt, batch_id))
            conn.execute("UPDATE batches SET status='paused',stage='interrupted',errorCode=?,errorText=?,owner=NULL,expires=NULL WHERE id=?",
                         (code, text, batch_id))
        return self.status(batch_id)

    def cleanup_failed(self, batch_id, code, text):
        with self.connect() as conn:
            changed = conn.execute("""UPDATE batches SET status='paused',stage='cleanup_failed',errorCode=?,errorText=?,
                owner=NULL,expires=NULL WHERE id=?""", (code, text, batch_id))
            if changed.rowcount != 1:
                raise VideoBatchError('批次不存在')
        return self.status(batch_id)

    def status(self, batch_id):
        with self.connect() as conn:
            row = conn.execute('SELECT id,status,stage,errorCode,errorText,currentItemId FROM batches WHERE id=?', (batch_id,)).fetchone()
            if not row:
                raise VideoBatchError('批次不存在')
            items = []
            for raw in conn.execute('SELECT itemId,status,stage,receipt FROM items WHERE batchId=? ORDER BY rowid', (batch_id,)):
                item = dict(raw)
                item['receipt'] = json.loads(item['receipt']) if item['receipt'] else None
                items.append(item)
        return {**dict(row), 'items': items}

    def list_batch_ids(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 500:
            raise VideoBatchError('批次查询数量无效')
        with self.connect() as conn:
            return [row['id'] for row in conn.execute(
                'SELECT id FROM batches ORDER BY createdAt DESC,rowid DESC LIMIT ?', (limit,))]

    def snapshot(self, batch_id):
        with self.connect() as conn:
            row = conn.execute('SELECT snapshot FROM batches WHERE id=?', (batch_id,)).fetchone()
        if not row:
            raise VideoBatchError('批次不存在')
        snapshot = json.loads(row['snapshot'])
        if snapshot.get('snapshotHash') != fingerprint({k: v for k, v in snapshot.items() if k != 'snapshotHash'}):
            raise VideoBatchError('已保存的批次快照校验失败', 'video_batch_snapshot_corrupt')
        return snapshot

    def pending_items(self, batch_id):
        snapshot = self.snapshot(batch_id)
        states = {row['itemId']: row['status'] for row in self.status(batch_id)['items']}
        return [item for item in snapshot['items'] if states.get(item['itemId']) == 'pending']

    def cancel(self, batch_id):
        with self.connect() as conn:
            row = conn.execute('SELECT status FROM batches WHERE id=?', (batch_id,)).fetchone()
            if not row:
                raise VideoBatchError('批次不存在')
            if row['status'] == 'running':
                raise VideoBatchError('正在执行的批次请先等待安全停顿', 'video_batch_cancel_running')
            conn.execute("UPDATE items SET status='cancelled',stage='cancelled' WHERE batchId=? AND status='pending'", (batch_id,))
            remaining = conn.execute("SELECT count(*) n FROM items WHERE batchId=? AND status='pending'", (batch_id,)).fetchone()['n']
            if not remaining and row['status'] in {'prepared', 'paused'}:
                conn.execute("UPDATE batches SET status='cancelled',authorization=NULL WHERE id=?", (batch_id,))
        return self.status(batch_id)

    def fail_pending(self, batch_id, item_id, owner, code, text, *, now=None):
        return self.finish_item(batch_id, item_id, owner, 'failed',
                                {'errorCode': code, 'errorText': text, 'stage': 'pre_submit'}, now=now)

    def finish_item(self, batch_id, item_id, owner, status, receipt=None, *, now=None):
        now = time.time() if now is None else now
        if status not in {'success', 'scheduled', 'failed', 'unknown'}:
            raise VideoBatchError('无效的条目终态')
        if status in {'success', 'scheduled'}:
            identifiers = [receipt.get('platformPostId'), receipt.get('postUrl')] if isinstance(receipt, dict) else []
            if not any(type(value) is str and value.strip() for value in identifiers):
                raise VideoBatchError('缺少平台作品回执')
            if status == 'scheduled' and (type(receipt.get('scheduledAt')) is not str or not receipt['scheduledAt'].strip()):
                raise VideoBatchError('缺少平台定时回执')
        allowed = {'platformPostId', 'postUrl', 'scheduledAt', 'errorCode', 'errorText', 'stage'}
        if receipt is not None and (type(receipt) is not dict or set(receipt) - allowed):
            raise VideoBatchError('回执包含不支持的字段')
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            owned = conn.execute("SELECT id FROM batches WHERE id=? AND owner=? AND status='running' AND expires>?",
                                 (batch_id, owner, now)).fetchone()
            if not owned:
                raise VideoBatchError('执行租约已失效')
            item = conn.execute('SELECT status FROM items WHERE batchId=? AND itemId=?', (batch_id, item_id)).fetchone()
            if not item or item['status'] not in {'pending', 'submitting'}:
                raise VideoBatchError('条目已结束，禁止覆盖')
            if item['status'] == 'pending' and status != 'failed':
                raise VideoBatchError('尚未提交，不能记录平台结果')
            if item['status'] == 'submitting' and status == 'failed':
                raise VideoBatchError('提交意图已落盘，结果不明时不得记为可重试失败')
            stage = (receipt or {}).get('stage') or ('receipt' if status in {'success', 'scheduled'} else status)
            conn.execute('UPDATE items SET status=?,stage=?,receipt=? WHERE batchId=? AND itemId=?',
                         (status, stage, json.dumps(receipt, ensure_ascii=False), batch_id, item_id))
            if status == 'unknown':
                conn.execute("UPDATE batches SET status='paused',owner=NULL,expires=NULL WHERE id=?", (batch_id,))
                return
            states = [row['status'] for row in conn.execute('SELECT status FROM items WHERE batchId=?', (batch_id,))]
            if not any(s in {'pending', 'submitting'} for s in states):
                state = 'success' if all(s in {'success', 'scheduled'} for s in states) else ('failed' if all(s == 'failed' for s in states) else 'partial_failure')
                conn.execute("UPDATE batches SET status=?,stage='finished',owner=NULL,expires=NULL,currentItemId=NULL WHERE id=?", (state, batch_id))
