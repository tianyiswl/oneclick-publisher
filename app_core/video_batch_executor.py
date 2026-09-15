"""视频号批次的串行本地编排器。

平台适配器由调用方注入；本模块不导入浏览器或上传器。
"""
from __future__ import annotations

from threading import Event, Thread

from app_core.video_batch_contract import VideoBatchError, file_hash


class BatchAdapterError(RuntimeError):
    def __init__(self, message, error_code='video_batch_adapter_failed', *, pause=False,
                 after_submission=False):
        super().__init__(message)
        self.error_code = error_code
        self.pause = pause
        self.after_submission = after_submission


def _changed(item):
    if file_hash(item['path']) != item['sha256']:
        return True
    cover = item.get('coverPath')
    return bool(cover and file_hash(cover) != item.get('coverSha256'))


def execute_batch(queue, batch_id, authorization, adapter_factory):
    """消费一次授权并串行执行 pending 条目。"""
    owner = queue.claim(batch_id, authorization)
    adapter = None
    stop_heartbeat = Event()
    lease_lost = []
    def keep_lease():
        while not stop_heartbeat.wait(15):
            try:
                queue.heartbeat(batch_id, owner)
            except Exception as exc:
                lease_lost.append(exc)
                return
    keeper = Thread(target=keep_lease, name='video-batch-heartbeat', daemon=True)
    keeper.start()
    try:
        snapshot = queue.snapshot(batch_id)
        # 全部快照在任何适配器/平台副作用之前核验。
        changed = [item for item in queue.pending_items(batch_id) if _changed(item)]
        if changed:
            for item in queue.pending_items(batch_id):
                queue.fail_pending(batch_id, item['itemId'], owner,
                                   'video_batch_snapshot_changed', '批次中的视频或封面已变化')
            return queue.status(batch_id)
        adapter = adapter_factory()
        adapter.open(snapshot['accountId'])
        for item in queue.pending_items(batch_id):
            queue.heartbeat(batch_id, owner)
            if lease_lost:
                raise VideoBatchError('批次执行租约已失效')
            intent_written = False
            try:
                queue.begin_item(batch_id, item['itemId'], owner)
                adapter.open_item(item)
                adapter.prepare(item)
                if lease_lost:
                    raise BatchAdapterError('批次执行租约已失效', 'video_batch_lease_lost', pause=True)
                # 不可调换顺序：意图落盘成功后才能点击最终动作。
                queue.submission_intent(batch_id, item['itemId'], owner)
                intent_written = True
                receipt = adapter.submit(item)
                if type(receipt) is not dict or receipt.get('status') not in {'success', 'scheduled'}:
                    raise BatchAdapterError('平台回执缺失或不明', 'video_batch_receipt_unknown',
                                            after_submission=True)
                receipt = dict(receipt)
                status = receipt.pop('status')
                expected_status = 'scheduled' if item.get('scheduledAt') else 'success'
                if status != expected_status:
                    raise BatchAdapterError('平台回执状态与冻结排期不一致',
                                            'video_batch_schedule_receipt_mismatch', after_submission=True)
                if status == 'scheduled' and receipt.get('scheduledAt') != item.get('scheduledAt'):
                    raise BatchAdapterError('平台定时回执与冻结排期不一致',
                                            'video_batch_schedule_receipt_mismatch', after_submission=True)
                ids = [receipt.get('platformPostId'), receipt.get('postUrl')]
                if not any(type(value) is str and value.strip() for value in ids):
                    raise BatchAdapterError('平台回执缺少有效作品标识',
                                            'video_batch_receipt_unknown', after_submission=True)
                receipt['stage'] = 'platform_receipt'
                queue.finish_item(batch_id, item['itemId'], owner, status, receipt)
            except BatchAdapterError as exc:
                if intent_written or exc.after_submission:
                    queue.finish_item(batch_id, item['itemId'], owner, 'unknown',
                                      {'errorCode': exc.error_code, 'errorText': str(exc), 'stage': 'submission_unknown'})
                    break
                if exc.pause:
                    queue.pause(batch_id, owner, exc.error_code, str(exc))
                    break
                queue.fail_pending(batch_id, item['itemId'], owner, exc.error_code, str(exc))
            except Exception as exc:
                if intent_written:
                    queue.finish_item(batch_id, item['itemId'], owner, 'unknown',
                                      {'errorCode': 'video_batch_submit_exception', 'errorText': str(exc),
                                       'stage': 'submission_unknown'})
                    break
                queue.fail_pending(batch_id, item['itemId'], owner,
                                   'video_batch_prepare_exception', str(exc))
            finally:
                try:
                    adapter.close_item()
                except Exception as exc:
                    queue.cleanup_failed(batch_id, 'video_batch_item_cleanup_failed', str(exc))
                    break
    except BaseException as exc:
        # 包含 adapter factory/open 与取消：先收口持久状态，再保留原异常。
        queue.abandon(batch_id, owner, getattr(exc, 'error_code', 'video_batch_executor_interrupted'), str(exc))
        raise
    finally:
        stop_heartbeat.set()
        keeper.join(timeout=1)
        if adapter is not None:
            try:
                adapter.close()
            except Exception as exc:
                queue.cleanup_failed(batch_id, 'video_batch_session_cleanup_failed', str(exc))
    return queue.status(batch_id)
