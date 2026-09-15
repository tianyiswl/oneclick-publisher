"""UI 与 CLI 共用的本地检查与持久化入口。"""
from pathlib import Path
from .video_batch_contract import prepare_batch, VideoBatchError
from .video_batch_queue import BatchQueue


FORMAL_AVAILABLE = False
FORMAL_GATE_MESSAGE = '视频号当前页面、官方话题实体和唯一回执尚未验证；正式授权与启动已硬关闭'
STATUS_LABELS = {'prepared': '待执行', 'running': '执行中', 'paused': '已暂停',
                 'success': '全部已取得回执', 'partial_failure': '部分失败',
                 'failed': '失败', 'cancelled': '已取消'}
ITEM_STATUS_LABELS = {'pending': '待执行', 'submitting': '提交后待核对', 'unknown': '结果不明',
                      'success': '已取得发布回执', 'scheduled': '平台定时受理',
                      'failed': '失败', 'cancelled': '已取消'}
STAGE_LABELS = {'pending': '待执行', 'preparing': '正在准备', 'submission_intent': '提交意图已记录',
                'platform_receipt': '已取得平台回执', 'pre_submit': '提交前失败',
                'submission_unknown': '提交结果待核对', 'lease_expired': '执行租约已过期',
                'paused': '已暂停', 'cancelled': '已取消', 'receipt': '已取得回执',
                'failed': '失败', 'unknown': '结果不明'}
BATCH_STAGE_LABELS = {**STAGE_LABELS, 'prepared': '已准备', 'running': '执行中',
                      'finished': '执行已结束', 'interrupted': '执行已中断',
                      'cleanup_failed': '清理失败'}


def default_queue():
    from .paths import USER_DATA_DIR
    return BatchQueue(Path(USER_DATA_DIR) / 'video-batch' / 'queue.db')


def check_batch(request, *, now=None, accounts=None):
    if accounts is None:
        from .account_service import list_accounts
        accounts = list_accounts()
    matches = [a for a in accounts if a.get('id') == request.get('accountId') and a.get('type') == 2]
    if len(matches) != 1:
        raise VideoBatchError('所选账号不是当前可用的视频号账号', 'video_batch_account_missing')
    snapshot = prepare_batch(request, now=now)
    return {'status': 'local_check_passed', 'stage': 'local_check', 'snapshot': snapshot,
            'platformWriteOccurred': False, 'formalAvailable': FORMAL_AVAILABLE,
            'message': '本地检查通过；视频号批量正式回执通道尚待当前页面验证'}


def prepare_and_store(request, *, now=None, accounts=None, queue=None):
    result = check_batch(request, now=now, accounts=accounts)
    queue = queue or default_queue()
    batch_id = queue.create(result['snapshot'])
    return {'batchId': batch_id, 'status': 'prepared', 'snapshotHash': result['snapshot']['snapshotHash'],
            'formalAvailable': FORMAL_AVAILABLE, 'platformWriteOccurred': False,
            'message': FORMAL_GATE_MESSAGE}


def batch_status(batch_id, *, queue=None):
    queue = queue or default_queue()
    queue.recover()
    result = queue.status(batch_id)
    states = {item['status'] for item in result['items']}
    if states & {'success', 'scheduled'}:
        platform_write = True
    elif states & {'submitting', 'unknown'}:
        platform_write = None
    else:
        platform_write = False
    result.update(formalAvailable=FORMAL_AVAILABLE, platformWriteOccurred=platform_write,
                  message=FORMAL_GATE_MESSAGE)
    return result


def cancel_batch(batch_id, *, queue=None):
    queue = queue or default_queue()
    queue.cancel(batch_id)
    return batch_status(batch_id, queue=queue)


def authorize_batch(*args, **kwargs):
    raise VideoBatchError(FORMAL_GATE_MESSAGE, 'video_batch_formal_unavailable')


def start_batch(*args, **kwargs):
    raise VideoBatchError(FORMAL_GATE_MESSAGE, 'video_batch_formal_unavailable')


def _project(batch_id, queue):
    raw = queue.status(batch_id)
    snapshot = queue.snapshot(batch_id)
    items_by_id = {row['itemId']: row for row in raw['items']}
    items = []
    for frozen in snapshot['items']:
        state = items_by_id[frozen['itemId']]
        receipt = state.get('receipt') if isinstance(state.get('receipt'), dict) else None
        items.append({'itemId': frozen['itemId'], 'fileName': Path(frozen['path']).name,
                      'title': frozen.get('content', {}).get('title', ''),
                      'scheduledAt': frozen.get('scheduledAt'), 'status': state['status'],
                      'statusLabel': ITEM_STATUS_LABELS.get(state['status'], '未知状态'),
                      'stage': state.get('stage'), 'stageLabel': STAGE_LABELS.get(state.get('stage'), '未知阶段'),
                      'errorCode': (receipt or {}).get('errorCode'),
                      'errorText': (receipt or {}).get('errorText'), 'receipt': receipt})
    account_id = snapshot['accountId']
    account_display = f'账号 ID {account_id}'
    try:
        from .account_service import list_accounts
        matches = [row for row in list_accounts() if row.get('id') == account_id and row.get('type') == 2]
        if len(matches) == 1:
            name = str(matches[0].get('userName') or matches[0].get('profileName') or '').strip()
            if name:
                account_display = f'{name}（ID {account_id}）'
    except Exception:
        pass
    return {'batchId': batch_id, 'accountId': account_id, 'accountDisplay': account_display, 'platform': '视频号',
            'itemCount': len(items), 'status': raw['status'],
            'statusLabel': STATUS_LABELS.get(raw['status'], '未知状态'),
            'stage': raw.get('stage'), 'stageLabel': BATCH_STAGE_LABELS.get(raw.get('stage'), '未知阶段'),
            'errorCode': raw.get('errorCode'),
            'errorText': raw.get('errorText'), 'items': items}


def list_batches(*, limit=100, queue=None):
    queue = queue or default_queue()
    queue.recover()
    return [_project(batch_id, queue) for batch_id in queue.list_batch_ids(limit)]


def batch_details(batch_id, *, queue=None):
    queue = queue or default_queue()
    queue.recover()
    return _project(batch_id, queue)
