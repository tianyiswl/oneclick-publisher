# -*- coding: utf-8 -*-
"""Instagram Reels V1 的离线意图、预检与回读合同。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Mapping
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .overseas_instagram_errors import InstagramPublishError
from .overseas_instagram_identity import InstagramIdentity, normalize_instagram_user_id


INSTAGRAM_CAPTION_MAX_CHARS = 2200
_SAFE_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_MEDIA_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")


@dataclass(frozen=True, slots=True)
class InstagramPublishIntent:
    account_id: int
    instagram_user_id: str
    video_path: str
    video_sha256: str
    cover_path: str
    cover_sha256: str
    caption: str
    caption_sha256: str
    topics: tuple[str, ...]
    visibility: str
    share_to_feed: bool
    schedule_mode: str
    scheduled_at: str | None
    schedule_timezone: str
    intent_fingerprint: str


@dataclass(frozen=True, slots=True)
class InstagramContentBaseline:
    instagram_user_id: str
    content_ids: tuple[str, ...]
    captured_at: str
    baseline_hash: str


def _fail(error_code: str, message: str) -> InstagramPublishError:
    return InstagramPublishError(error_code, message)


def _normalize_multiline(value: object) -> str:
    if type(value) is not str:
        return ""
    normalized = value.replace("\u00a0", " ").replace("\u202f", " ")
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    lines = [" ".join(line.split()) for line in normalized.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _normalize_topics(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise _fail("instagram_topics_invalid", "Instagram 话题必须是结构化列表。")
    topics: list[str] = []
    seen: set[str] = set()
    for raw in value:
        if type(raw) is not str:
            raise _fail("instagram_topics_invalid", "Instagram 话题字段无效。")
        topic = raw.strip().lstrip("#")
        if (
            not topic
            or len(topic) > 100
            or not all(char.isalnum() or char == "_" for char in topic)
        ):
            raise _fail("instagram_topics_invalid", "Instagram 话题字段无效。")
        key = topic.casefold()
        if key not in seen:
            seen.add(key)
            topics.append(topic)
    return tuple(topics)


def build_instagram_caption(
    *,
    title: object,
    body: object,
    topics: object,
) -> tuple[str, tuple[str, ...]]:
    normalized_title = _normalize_multiline(title)
    normalized_body = _normalize_multiline(body)
    if "@" in normalized_title or "@" in normalized_body:
        raise _fail(
            "instagram_mentions_unsupported",
            "Instagram V1 没有提及实体回读，正文不能包含原始 @文字。",
        )
    normalized_topics = _normalize_topics(topics)
    parts = [part for part in (normalized_title, normalized_body) if part]
    if normalized_topics:
        parts.append(" ".join(f"#{topic}" for topic in normalized_topics))
    caption = "\n\n".join(parts)
    if not caption:
        raise _fail("instagram_caption_invalid", "Instagram caption 不能为空。")
    if len(caption) > INSTAGRAM_CAPTION_MAX_CHARS:
        raise _fail(
            "instagram_caption_invalid",
            f"Instagram caption 不能超过 {INSTAGRAM_CAPTION_MAX_CHARS} 个字符。",
        )
    return caption, normalized_topics


def _file_sha256(path: Path, *, error_code: str, label: str) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise _fail(error_code, f"Instagram {label}无法安全读取。") from exc
    return digest.hexdigest()


def _single_file(value: object, *, error_code: str, label: str) -> Path:
    values = value if isinstance(value, list) else [value]
    if len(values) != 1 or type(values[0]) is not str:
        raise _fail(error_code, f"Instagram V1 必须精确选择一个{label}。")
    try:
        path = Path(values[0]).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _fail(error_code, f"Instagram {label}不存在或不可读取。") from exc
    if not path.is_file():
        raise _fail(error_code, f"Instagram {label}不存在或不可读取。")
    return path


def _schedule(payload: Mapping[str, object]) -> tuple[str, str | None, str]:
    mode = str(payload.get("scheduleMode") or "immediate").strip().casefold()
    scheduled_at_value = payload.get("scheduledAt")
    timezone_name = str(payload.get("scheduleTimezone") or "").strip()
    if mode == "immediate":
        if scheduled_at_value not in (None, "") or timezone_name:
            raise _fail(
                "instagram_schedule_invalid",
                "Instagram 立即发布不能夹带定时时间。",
            )
        return mode, None, ""
    if mode != "platform_native" or type(scheduled_at_value) is not str or not timezone_name:
        raise _fail("instagram_schedule_invalid", "Instagram 定时设置无效。")
    try:
        scheduled = datetime.fromisoformat(scheduled_at_value)
        timezone = ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise _fail("instagram_schedule_invalid", "Instagram 定时设置无效。") from exc
    if scheduled.tzinfo is None or scheduled.utcoffset() != scheduled.astimezone(timezone).utcoffset():
        raise _fail("instagram_schedule_invalid", "Instagram 定时时区不一致。")
    return mode, scheduled.isoformat(), timezone_name


def prepare_instagram_publish_intent(
    payload: Mapping[str, object],
) -> InstagramPublishIntent:
    if type(payload.get("type")) is not int or payload.get("type") != 8:
        raise _fail("instagram_target_invalid", "发布载荷不是 Instagram Reels。")
    if payload.get("instagramControlledPublish") is not True:
        raise _fail(
            "instagram_controlled_publish_required",
            "Instagram 必须使用受控发布服务。",
        )
    account_ids = payload.get("accountIds")
    if (
        not isinstance(account_ids, list)
        or len(account_ids) != 1
        or type(account_ids[0]) is not int
        or account_ids[0] <= 0
    ):
        raise _fail("instagram_target_invalid", "Instagram V1 必须精确选择一个账号。")
    try:
        instagram_user_id = normalize_instagram_user_id(
            payload.get("instagramExpectedUserId")
        )
    except ValueError as exc:
        raise _fail(
            "instagram_account_invalid",
            "Instagram 账号缺少稳定主体标识。",
        ) from exc
    video = _single_file(
        payload.get("fileList"),
        error_code="instagram_video_invalid",
        label="视频",
    )
    cover = _single_file(
        payload.get("coverPath"),
        error_code="instagram_cover_invalid",
        label="封面",
    )
    caption, topics = build_instagram_caption(
        title=payload.get("title"),
        body=payload.get("description"),
        topics=payload.get("tags") or [],
    )
    if payload.get("visibility") != "public":
        raise _fail(
            "instagram_visibility_unsupported",
            "Instagram V1 只支持可回读的公开 Reel。",
        )
    share_to_feed = payload.get("shareToFeed")
    if type(share_to_feed) is not bool:
        raise _fail(
            "instagram_visibility_unsupported",
            "Instagram 必须明确是否同时分享到动态。",
        )
    schedule_mode, scheduled_at, schedule_timezone = _schedule(payload)
    video_sha256 = _file_sha256(
        video,
        error_code="instagram_video_invalid",
        label="视频",
    )
    cover_sha256 = _file_sha256(
        cover,
        error_code="instagram_cover_invalid",
        label="封面",
    )
    caption_sha256 = hashlib.sha256(caption.encode("utf-8")).hexdigest()
    projection = {
        "contentKind": "reel",
        "instagramUserId": instagram_user_id,
        "videoSha256": video_sha256,
        "coverSha256": cover_sha256,
        "captionSha256": caption_sha256,
        "visibility": "public",
        "shareToFeed": share_to_feed,
        "scheduleMode": schedule_mode,
        "scheduledAt": scheduled_at,
        "scheduleTimezone": schedule_timezone,
    }
    encoded = json.dumps(
        projection,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return InstagramPublishIntent(
        account_id=account_ids[0],
        instagram_user_id=instagram_user_id,
        video_path=str(video),
        video_sha256=video_sha256,
        cover_path=str(cover),
        cover_sha256=cover_sha256,
        caption=caption,
        caption_sha256=caption_sha256,
        topics=topics,
        visibility="public",
        share_to_feed=share_to_feed,
        schedule_mode=schedule_mode,
        scheduled_at=scheduled_at,
        schedule_timezone=schedule_timezone,
        intent_fingerprint=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    )


def verify_instagram_form_snapshot(
    intent: InstagramPublishIntent,
    identity: InstagramIdentity,
    evidence: Mapping[str, object],
) -> dict[str, object]:
    if not isinstance(intent, InstagramPublishIntent) or not isinstance(
        identity, InstagramIdentity
    ) or not isinstance(evidence, Mapping):
        raise _fail(
            "instagram_form_readback_failed",
            "Instagram 表单回读结构无效，已停在分享之前。",
        )
    expected = {
        "instagramUserId": intent.instagram_user_id,
        "username": identity.username,
        "accountType": identity.account_type,
        "linkedPageId": identity.linked_page_id,
        "videoSha256": intent.video_sha256,
        "caption": intent.caption,
        "captionSha256": intent.caption_sha256,
        "topics": list(intent.topics),
        "coverSha256": intent.cover_sha256,
        "visibility": intent.visibility,
        "shareToFeed": intent.share_to_feed,
        "scheduleMode": intent.schedule_mode,
        "scheduledAt": intent.scheduled_at,
        "scheduleTimezone": intent.schedule_timezone,
        "finalButtonCount": 1,
        "finalButtonEnabled": True,
        "platformWriteOccurred": True,
        "finalActionTriggered": False,
    }
    if intent.instagram_user_id != identity.user_id:
        raise _fail(
            "instagram_identity_mismatch",
            "Instagram 表单主体与绑定账号不一致，已停在分享之前。",
        )
    for key, expected_value in expected.items():
        actual = evidence.get(key)
        if type(actual) is not type(expected_value) or actual != expected_value:
            raise InstagramPublishError(
                "instagram_form_readback_failed",
                f"Instagram 表单字段未能精确回读：{key}。",
                receipt={
                    "phase": "safe_failed",
                    "instagramUserId": intent.instagram_user_id,
                    "platformWriteOccurred": bool(
                        evidence.get("platformWriteOccurred") is True
                    ),
                    "finalActionTriggered": False,
                },
            )
    baseline_hash = evidence.get("baselineHash")
    if type(baseline_hash) is not str or _SAFE_SHA256.fullmatch(baseline_hash) is None:
        raise _fail(
            "instagram_baseline_unavailable",
            "Instagram 内容列表基线不可用，已停在分享之前。",
        )
    snapshot = {
        "accountId": intent.account_id,
        "instagramUserId": intent.instagram_user_id,
        "username": identity.username,
        "accountType": identity.account_type,
        "linkedPageId": identity.linked_page_id,
        "linkedPageName": identity.linked_page_name,
        "videoSha256": intent.video_sha256,
        "captionSha256": intent.caption_sha256,
        "topics": list(intent.topics),
        "coverSha256": intent.cover_sha256,
        "visibility": intent.visibility,
        "shareToFeed": intent.share_to_feed,
        "scheduleMode": intent.schedule_mode,
        "scheduledAt": intent.scheduled_at,
        "scheduleTimezone": intent.schedule_timezone,
        "baselineHash": baseline_hash,
        "platformWriteOccurred": True,
        "finalActionTriggered": False,
        "finalButtonEnabled": True,
    }
    encoded = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "phase": "platform_form_verified",
        **snapshot,
        "formSnapshotHash": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    }


def capture_instagram_content_baseline(
    *,
    instagram_user_id: str,
    content_ids: object,
    complete: bool,
    captured_at: str,
) -> InstagramContentBaseline:
    try:
        normalized_user_id = normalize_instagram_user_id(instagram_user_id)
        captured = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise _fail(
            "instagram_baseline_unavailable",
            "Instagram 内容列表基线不可用。",
        ) from exc
    if complete is not True or captured.tzinfo is None or not isinstance(
        content_ids, (list, tuple)
    ):
        raise _fail(
            "instagram_baseline_unavailable",
            "Instagram 内容列表基线不完整。",
        )
    normalized_ids: list[str] = []
    seen: set[str] = set()
    for value in content_ids:
        if type(value) is not str or _SAFE_MEDIA_ID.fullmatch(value) is None:
            raise _fail(
                "instagram_baseline_unavailable",
                "Instagram 内容列表基线包含无效媒体标识。",
            )
        if value in seen:
            raise _fail(
                "instagram_baseline_unavailable",
                "Instagram 内容列表基线包含重复媒体标识。",
            )
        seen.add(value)
        normalized_ids.append(value)
    projection = {
        "instagramUserId": normalized_user_id,
        "contentIds": sorted(normalized_ids),
        "capturedAt": captured.isoformat(),
        "complete": True,
    }
    encoded = json.dumps(
        projection,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return InstagramContentBaseline(
        instagram_user_id=normalized_user_id,
        content_ids=tuple(sorted(normalized_ids)),
        captured_at=captured.isoformat(),
        baseline_hash=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    )


def _safe_instagram_url(value: object, *, required: bool) -> str | None:
    if value is None and not required:
        return None
    if type(value) is not str or len(value) > 512:
        raise ValueError
    parsed = urlsplit(value)
    host = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or host not in {"instagram.com", "www.instagram.com"}
        or not parsed.path.startswith(("/reel/", "/p/"))
    ):
        raise ValueError
    return value


def _safe_optional_time(value: object, *, required: bool) -> str | None:
    if value is None and not required:
        return None
    if type(value) is not str:
        raise ValueError
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError
    return parsed.isoformat()


def match_unique_instagram_readback(
    baseline: InstagramContentBaseline,
    intent: InstagramPublishIntent,
    records: object,
) -> dict[str, object]:
    if (
        not isinstance(baseline, InstagramContentBaseline)
        or not isinstance(intent, InstagramPublishIntent)
        or baseline.instagram_user_id != intent.instagram_user_id
        or _SAFE_SHA256.fullmatch(baseline.baseline_hash) is None
        or not isinstance(records, (list, tuple))
    ):
        raise _fail(
            "instagram_baseline_unavailable",
            "Instagram 回读基线与发布主体不一致。",
        )
    baseline_ids = set(baseline.content_ids)
    matches: list[dict[str, object]] = []
    for raw in records:
        if not isinstance(raw, Mapping):
            continue
        try:
            user_id = normalize_instagram_user_id(raw.get("instagramUserId"))
            media_id = raw.get("mediaId")
            if type(media_id) is not str or _SAFE_MEDIA_ID.fullmatch(media_id) is None:
                continue
            caption_hash = raw.get("captionSha256")
            if type(caption_hash) is not str or _SAFE_SHA256.fullmatch(caption_hash) is None:
                continue
            state = raw.get("state")
            if type(state) is not str:
                continue
            if intent.schedule_mode == "immediate":
                published_at = _safe_optional_time(
                    raw.get("publishedAt"), required=True
                )
                scheduled_at = _safe_optional_time(
                    raw.get("scheduledAt"), required=False
                )
                url = _safe_instagram_url(raw.get("url"), required=True)
                state_matches = state == "published" and scheduled_at is None
            else:
                published_at = _safe_optional_time(
                    raw.get("publishedAt"), required=False
                )
                scheduled_at = _safe_optional_time(
                    raw.get("scheduledAt"), required=True
                )
                url = _safe_instagram_url(raw.get("url"), required=False)
                state_matches = (
                    state == "scheduled"
                    and published_at is None
                    and scheduled_at == intent.scheduled_at
                )
        except (TypeError, ValueError):
            continue
        if (
            user_id == intent.instagram_user_id
            and media_id not in baseline_ids
            and caption_hash == intent.caption_sha256
            and state_matches
        ):
            matches.append(
                {
                    "mediaId": media_id,
                    "url": url,
                    "publishedAt": published_at,
                    "scheduledAt": scheduled_at,
                }
            )
    if len(matches) != 1:
        raise InstagramPublishError(
            "instagram_readback_not_unique",
            "Instagram 后台没有返回唯一的新内容，结果不明并禁止重放。",
            receipt={
                "phase": "outcome_unknown",
                "instagramUserId": intent.instagram_user_id,
                "baselineHash": baseline.baseline_hash,
                "finalActionTriggered": True,
                "blocksReplay": True,
            },
            outcome_ambiguous=True,
        )
    result = matches[0]
    return {
        "phase": (
            "published_readback_confirmed"
            if intent.schedule_mode == "immediate"
            else "scheduled_readback_confirmed"
        ),
        "accountId": intent.account_id,
        "instagramUserId": intent.instagram_user_id,
        "mediaId": result["mediaId"],
        "url": result["url"],
        "publishedAt": result["publishedAt"],
        "scheduledAt": result["scheduledAt"],
        "baselineHash": baseline.baseline_hash,
        "finalActionTriggered": True,
        "blocksReplay": True,
    }
