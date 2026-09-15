"""视频号批次单次提交边界，平台按钮和结果读取由已验证适配器传入。"""
from .video_batch_contract import VideoBatchError


async def submit_once(*, persist_intent, click, readback):
    # 持久化失败时平台零动作；点击发生后任何异常都不可自动再次点击。
    persist_intent()
    click_error = None
    try:
        await click()
    except Exception as exc:
        click_error = exc
    try:
        result = await readback()
    except Exception as exc:
        raise VideoBatchError('提交后未取得明确结果，请先核对后台', 'channels_publish_outcome_unknown') from exc
    if not isinstance(result, dict) or result.get('status') not in {'success', 'scheduled'} or not (result.get('platformPostId') or result.get('postUrl')):
        raise VideoBatchError('提交后未取得唯一作品回执，请先核对后台', 'channels_publish_outcome_unknown') from click_error
    if result['status'] == 'scheduled' and not result.get('scheduledAt'):
        raise VideoBatchError('提交后缺少定时回执', 'channels_publish_outcome_unknown')
    return result
