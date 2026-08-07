# -*- coding: utf-8 -*-
"""抖音带货收藏音乐的本地、安全缓存。

缓存只用于让客户端更快展示用户已同步的候选。它从不携带页面瞬态 marker，
也不构成平台已经选择、保存草稿或发布的证据。
"""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from typing import Any, Mapping

from . import database, douyin_music_service


class DouyinFavoriteMusicCacheError(ValueError):
    """本地收藏音乐缓存不满足安全字段约定。"""


_PUBLIC_KEYS = ("musicId", "title", "creator", "duration", "syncedAt")
_UNSTABLE_ID_PREFIXES = ("visible:", "favorite-index:")


def _metadata_music_id(value: Mapping[str, Any]) -> str:
    """为无平台 ID 的收藏音乐生成本地稳定指纹。

    指纹只由歌曲名、作者和时长组成。后续写入前仍会在当次收藏列表中按这三项
    完整且唯一地重新定位，不能把该指纹当成抖音平台 ID 使用。
    """

    material = "\x1f".join(
        str(value.get(key) or "").strip()
        for key in ("title", "creator", "duration")
    )
    return f"metadata:{sha256(material.encode('utf-8')).hexdigest()}"


def _account_id(value: object) -> int:
    try:
        account_id = int(value)
    except (TypeError, ValueError) as exc:
        raise DouyinFavoriteMusicCacheError("抖音收藏音乐缓存缺少有效账号") from exc
    if account_id <= 0:
        raise DouyinFavoriteMusicCacheError("抖音收藏音乐缓存缺少有效账号")
    return account_id


def _stable_music_id(value: object) -> str:
    music_id = " ".join(str(value or "").replace("\u200b", " ").split())
    if not music_id or music_id.startswith(_UNSTABLE_ID_PREFIXES):
        return ""
    return music_id


def _normalize_cache_row(value: object) -> dict[str, str] | None:
    music = douyin_music_service.normalize_music_readback(value)
    if not music:
        return None
    music_id = _stable_music_id(music.get("musicId")) or _metadata_music_id(music)
    return {
        "musicId": music_id,
        "title": str(music["title"]),
        "creator": str(music["creator"]),
        "duration": str(music["duration"]),
    }


def _public(row: Mapping[str, Any]) -> dict[str, str]:
    return {key: str(row[key] or "") for key in _PUBLIC_KEYS}


def replace_cached_favorite_music(account_id: int, rows: object) -> list[dict[str, str]]:
    """事务性替换一个账号的已同步收藏音乐，不保存瞬态浏览器字段。"""

    safe_account_id = _account_id(account_id)
    if not isinstance(rows, list):
        raise DouyinFavoriteMusicCacheError("抖音收藏音乐缓存候选格式无效")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in rows:
        row = _normalize_cache_row(value)
        if row is None:
            continue
        if row["musicId"] in seen:
            raise DouyinFavoriteMusicCacheError("抖音收藏音乐缓存出现重复身份")
        seen.add(row["musicId"])
        normalized.append(row)
    synced_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    with database.connect() as conn:
        conn.execute(
            "DELETE FROM douyin_favorite_music_cache WHERE accountId = ?",
            (safe_account_id,),
        )
        conn.executemany(
            """
            INSERT INTO douyin_favorite_music_cache
                (accountId, musicId, title, creator, duration, syncedAt)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    safe_account_id,
                    row["musicId"],
                    row["title"],
                    row["creator"],
                    row["duration"],
                    synced_at,
                )
                for row in normalized
            ],
        )
    return list_cached_favorite_music(safe_account_id)


def list_cached_favorite_music(account_id: int, *, limit: int = 200) -> list[dict[str, str]]:
    """读取一个账号的本地候选，不访问平台。"""

    safe_account_id = _account_id(account_id)
    safe_limit = max(1, min(int(limit or 200), 200))
    with database.connect() as conn:
        rows = conn.execute(
            """
            SELECT musicId, title, creator, duration, syncedAt
            FROM douyin_favorite_music_cache
            WHERE accountId = ?
            ORDER BY syncedAt DESC, title COLLATE NOCASE, musicId
            LIMIT ?
            """,
            (safe_account_id, safe_limit),
        ).fetchall()
    return [_public(row) for row in rows]


def cached_music_ids(rows: object) -> set[str]:
    """仅返回可长期复用的平台音乐身份。"""

    if not isinstance(rows, list):
        return set()
    return {
        music_id
        for value in rows
        if (music_id := _stable_music_id(
            value.get("musicId") if isinstance(value, Mapping) else ""
        ))
    }
