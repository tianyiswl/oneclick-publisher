"""单平台视频批次的本地内容与排期合同，不访问平台。"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
from zoneinfo import ZoneInfo

SCHEMA = 'oneclick-video-batch/v1'
ZONE = ZoneInfo('Asia/Shanghai')
FIELDS = {'title', 'body', 'tags'}


class VideoBatchError(ValueError):
    def __init__(self, message, code='video_batch_invalid'):
        super().__init__(message)
        self.error_code = code


def fingerprint(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def file_hash(path):
    source = Path(path)
    if not source.is_file():
        raise VideoBatchError(f'文件不存在：{source}', 'video_batch_file_missing')
    digest = sha256()
    with source.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _keys(value, allowed, label):
    if type(value) is not dict or set(value) - allowed:
        raise VideoBatchError(f'{label}包含不支持的字段')


def _text(value, label):
    if type(value) is not str:
        raise VideoBatchError(f'{label}必须是文本')
    return value.strip()


def _date(value):
    try:
        parsed = datetime.strptime(value, '%Y-%m-%d %H:%M').replace(tzinfo=ZONE)
        if parsed.strftime('%Y-%m-%d %H:%M') != value:
            raise ValueError()
        return parsed
    except (TypeError, ValueError):
        raise VideoBatchError('发布时间格式应为 YYYY-MM-DD HH:mm') from None


def _schedule(policy, count):
    _keys(policy, {'mode', 'start', 'intervalMinutes', 'startDate', 'times'}, '排期')
    mode = policy.get('mode', 'immediate')
    if mode == 'immediate':
        return [None] * count
    if mode == 'interval':
        start = _date(policy.get('start'))
        interval = policy.get('intervalMinutes')
        if type(interval) is not int or not 1 <= interval <= 10080:
            raise VideoBatchError('发布间隔须为 1–10080 分钟')
        return [start + timedelta(minutes=i * interval) for i in range(count)]
    if mode == 'daily':
        times = policy.get('times')
        if type(times) is not list or not times or any(type(t) is not str for t in times):
            raise VideoBatchError('每天至少填写一个时间')
        if len(set(times)) != len(times):
            raise VideoBatchError('每天的发布时间不能重复')
        clocks = [_date(f"{policy.get('startDate')} {clock}") for clock in sorted(times)]
        return [clocks[i % len(clocks)] + timedelta(days=i // len(clocks)) for i in range(count)]
    raise VideoBatchError('不支持的排期方式')


def prepare_batch(request, *, now=None):
    _keys(request, {'schemaVersion', 'platform', 'accountId', 'defaults', 'items', 'schedulePolicy'}, '批次')
    if request.get('schemaVersion', SCHEMA) != SCHEMA or request.get('platform') != '视频号':
        raise VideoBatchError('首版批量视频仅支持视频号')
    account = request.get('accountId')
    if type(account) is not int or account <= 0:
        raise VideoBatchError('请选择一个视频号账号')
    defaults = request.get('defaults', {})
    _keys(defaults, FIELDS, '通用内容')
    items = request.get('items')
    if type(items) is not list or not 1 <= len(items) <= 20:
        raise VideoBatchError('每批需要 1–20 条视频')
    now = now or datetime.now(ZONE)
    if now.tzinfo is None:
        raise VideoBatchError('检查时间必须包含时区')
    policy = request.get('schedulePolicy', {'mode': 'immediate'})
    dates = _schedule(policy, len(items))
    seen_ids, seen_files, results = set(), set(), []
    for index, raw in enumerate(items):
        _keys(raw, {'itemId', 'path', 'overrides', 'coverPath', 'scheduledAt'}, '视频条目')
        item_id = _text(raw.get('itemId'), '视频标识')
        if not item_id or item_id in seen_ids:
            raise VideoBatchError('视频标识缺失或重复')
        seen_ids.add(item_id)
        path = _text(raw.get('path'), '视频路径')
        if not Path(path).is_absolute():
            raise VideoBatchError('视频必须使用绝对路径')
        if Path(path).suffix.lower() not in {'.mp4', '.mov', '.mkv', '.avi'}:
            raise VideoBatchError('请选择支持的视频文件')
        digest = file_hash(path)
        if Path(path).stat().st_size == 0:
            raise VideoBatchError('视频文件为空')
        if digest in seen_files:
            raise VideoBatchError('批次包含内容相同的视频', 'video_batch_duplicate_media')
        seen_files.add(digest)
        overrides = raw.get('overrides', {})
        _keys(overrides, FIELDS, '独立内容')
        content = {**{'title': '', 'body': '', 'tags': []}, **deepcopy(defaults), **deepcopy(overrides)}
        content['title'] = _text(content['title'], '标题')
        content['body'] = _text(content['body'], '正文')
        if not content['title'] or len(content['title']) > 10:
            raise VideoBatchError(f'第 {index + 1} 条短标题须为 1–10 字')
        tags = content['tags']
        if type(tags) is not list or any(type(t) is not str for t in tags):
            raise VideoBatchError('话题必须是文本列表')
        content['tags'] = list(dict.fromkeys(t.strip().lstrip('#').strip() for t in tags if t.strip().lstrip('#').strip()))
        date = _date(raw['scheduledAt']) if raw.get('scheduledAt') is not None else dates[index]
        if date is not None and date <= now:
            raise VideoBatchError(f'第 {index + 1} 条发布时间已过期', 'video_batch_schedule_expired')
        cover = _text(raw.get('coverPath', ''), '封面路径')
        if cover and not Path(cover).is_absolute():
            raise VideoBatchError('封面必须使用绝对路径')
        result = {'itemId': item_id, 'path': path, 'sha256': digest, 'content': content,
                  'coverPath': cover, 'coverSha256': file_hash(cover) if cover else None,
                  'scheduledAt': date.strftime('%Y-%m-%d %H:%M') if date else None,
                  'timezone': 'Asia/Shanghai'}
        result['fingerprint'] = fingerprint({'platform': '视频号', 'accountId': account,
                                            **{k: v for k, v in result.items() if k not in {'itemId', 'path', 'coverPath'}}})
        results.append(result)
    snapshot = {'schemaVersion': SCHEMA, 'workflow': 'single-platform-video-batch',
                'platform': '视频号', 'accountId': account, 'items': results}
    snapshot['snapshotHash'] = fingerprint(snapshot)
    return snapshot
