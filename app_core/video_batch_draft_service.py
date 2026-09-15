"""批量视频填写内容的原子保存；未完成的内容允许保存。"""
import json
import os
from pathlib import Path
import tempfile

from .paths import USER_DATA_DIR
from .video_batch_contract import VideoBatchError


def validate_draft(value):
    if type(value) is not dict or set(value) - {'platform', 'accountId', 'defaults', 'items', 'schedulePolicy', 'schemaVersion'}:
        raise VideoBatchError('批次草稿格式无效')
    if value.get('platform') != '视频号':
        raise VideoBatchError('草稿平台无效')
    defaults = value.get('defaults', {})
    if type(defaults) is not dict or set(defaults) - {'title', 'body', 'tags'}:
        raise VideoBatchError('通用内容格式无效')
    items = value.get('items', [])
    if type(items) is not list or len(items) > 20:
        raise VideoBatchError('草稿最多保存 20 条视频')
    for item in items:
        if type(item) is not dict or set(item) - {'itemId', 'path', 'overrides', 'coverPath', 'scheduledAt'}:
            raise VideoBatchError('草稿视频字段无效')
        overrides = item.get('overrides', {})
        if type(overrides) is not dict or set(overrides) - {'title', 'body', 'tags'}:
            raise VideoBatchError('独立内容字段无效')
        for key in ('itemId', 'path', 'coverPath', 'scheduledAt'):
            if key in item and type(item[key]) is not str:
                raise VideoBatchError('草稿视频文本字段无效')
    policy = value.get('schedulePolicy', {})
    if type(policy) is not dict or set(policy) - {'mode', 'start', 'intervalMinutes', 'startDate', 'times'}:
        raise VideoBatchError('排期字段无效')
    if 'mode' in policy and policy['mode'] not in {'immediate', 'interval', 'daily'}:
        raise VideoBatchError('排期方式无效')
    for key in ('start', 'startDate'):
        if key in policy and type(policy[key]) is not str:
            raise VideoBatchError('排期时间必须是文本')
    if 'intervalMinutes' in policy and (type(policy['intervalMinutes']) is not int or not 1 <= policy['intervalMinutes'] <= 10080):
        raise VideoBatchError('发布间隔须为 1–10080 分钟')
    if 'times' in policy and (type(policy['times']) is not list or any(type(t) is not str for t in policy['times'])):
        raise VideoBatchError('每日时段必须是文本列表')
    for content in [defaults] + [i.get('overrides', {}) for i in items]:
        for key, field in content.items():
            if key == 'tags':
                if type(field) is not list or any(type(t) is not str for t in field):
                    raise VideoBatchError('话题必须是文本列表')
            elif type(field) is not str:
                raise VideoBatchError('标题和正文必须是文本')
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _path(root):
    return Path(root if root is not None else USER_DATA_DIR) / 'video-batch-draft.json'


def save_draft(value, *, root=None):
    normalized = validate_draft(value)
    target = _path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.video-batch-', dir=target.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(normalized, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return normalized


def load_draft(*, root=None):
    target = _path(root)
    if not target.exists():
        return None
    try:
        return validate_draft(json.loads(target.read_text(encoding='utf-8')))
    except (ValueError, OSError) as exc:
        raise VideoBatchError('保存内容损坏，未覆盖当前填写') from exc
