# -*- coding: utf-8 -*-
"""SQLite 初始化和轻量迁移。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

from .paths import DB_PATH, ensure_runtime_dirs


@contextmanager
def open_connection(
    path: str | Path,
    *,
    row_factory: bool = False,
) -> Iterator[sqlite3.Connection]:
    """打开一个事务连接，并在退出上下文时可靠关闭文件句柄。"""

    conn = sqlite3.connect(path)
    if row_factory:
        conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    ensure_schema()
    with open_connection(DB_PATH, row_factory=True) as conn:
        yield conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_columns(conn: sqlite3.Connection, table: str, definitions: Iterable[tuple[str, str]]) -> None:
    existing = _columns(conn, table)
    for name, definition in definitions:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def ensure_schema() -> None:
    ensure_runtime_dirs()
    with open_connection(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_info (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type INTEGER NOT NULL,
                filePath TEXT NOT NULL,
                userName TEXT NOT NULL,
                status INTEGER DEFAULT 0
            )
            """
        )
        _add_columns(
            conn,
            "user_info",
            (
                ("profileName", "TEXT"),
                ("avatarPath", "TEXT"),
                ("avatarUpdatedAt", "TEXT"),
                ("remark", "TEXT"),
                ("lastCheckedAt", "TEXT"),
                ("lastLoginAt", "TEXT"),
                ("authMode", "TEXT NOT NULL DEFAULT 'browser'"),
                ("accountReference", "TEXT"),
            ),
        )
        conn.execute(
            "UPDATE user_info SET authMode = 'browser' "
            "WHERE authMode IS NULL OR TRIM(authMode) = ''"
        )
        conn.execute("UPDATE user_info SET profileName = userName WHERE profileName IS NULL OR profileName = ''")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS file_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                filesize REAL,
                upload_time DATETIME DEFAULT CURRENT_TIMESTAMP,
                file_path TEXT
            )
            """
        )
        _add_columns(
            conn,
            "file_records",
            (
                ("remark", "TEXT"),
                ("coverPath", "TEXT"),
                ("mediaCategory", "TEXT NOT NULL DEFAULT '其他'"),
            ),
        )
        conn.execute(
            "UPDATE file_records SET mediaCategory = '其他' "
            "WHERE mediaCategory IS NULL OR TRIM(mediaCategory) = ''"
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tag_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tag TEXT NOT NULL UNIQUE,
                useCount INTEGER NOT NULL DEFAULT 1,
                lastUsedAt TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS publish_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                payloadJson TEXT NOT NULL,
                createdAt TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updatedAt TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS publish_drafts (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                payloadJson TEXT NOT NULL,
                updatedAt TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS douyin_commerce_content_drafts (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                payloadJson TEXT NOT NULL,
                updatedAt TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS douyin_commerce_batch_drafts (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                payloadJson TEXT NOT NULL,
                updatedAt TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS douyin_location_presets (
                id TEXT PRIMARY KEY,
                accountId INTEGER NOT NULL,
                poiId TEXT NOT NULL,
                name TEXT NOT NULL,
                address TEXT NOT NULL,
                scope TEXT NOT NULL,
                verifiedAt TEXT NOT NULL,
                UNIQUE(accountId, poiId)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS douyin_location_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                accountId TEXT NOT NULL,
                scope TEXT NOT NULL,
                poiId TEXT NOT NULL,
                name TEXT NOT NULL,
                address TEXT NOT NULL,
                commissionType TEXT NOT NULL,
                distance TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                productCount INTEGER,
                commissionProductCount INTEGER,
                commissionLabel TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                verifiedAt TEXT NOT NULL,
                lastSeenAt TEXT NOT NULL,
                revalidationFailures INTEGER NOT NULL DEFAULT 0,
                UNIQUE(accountId, scope, poiId, name, address, commissionType)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS douyin_location_cache_keywords (
                locationCacheId INTEGER NOT NULL,
                accountId TEXT NOT NULL,
                scope TEXT NOT NULL,
                keyword TEXT NOT NULL,
                commissionFilter TEXT NOT NULL,
                position INTEGER NOT NULL,
                PRIMARY KEY(locationCacheId, keyword, commissionFilter),
                FOREIGN KEY(locationCacheId) REFERENCES douyin_location_cache(id)
                    ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_douyin_location_cache_keywords_query
            ON douyin_location_cache_keywords(
                accountId, scope, keyword, commissionFilter, position, locationCacheId
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_douyin_location_cache_capacity
            ON douyin_location_cache(accountId, scope, lastSeenAt, id)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS douyin_favorite_music_cache (
                accountId INTEGER NOT NULL,
                musicId TEXT NOT NULL,
                title TEXT NOT NULL,
                creator TEXT NOT NULL,
                duration TEXT NOT NULL,
                syncedAt TEXT NOT NULL,
                PRIMARY KEY(accountId, musicId)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS platform_collections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                accountId INTEGER NOT NULL,
                platformType INTEGER NOT NULL,
                collectionName TEXT NOT NULL,
                updatedAt TEXT NOT NULL,
                UNIQUE(accountId, platformType, collectionName),
                FOREIGN KEY(accountId) REFERENCES user_info(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS publish_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                taskNo TEXT NOT NULL UNIQUE,
                mode TEXT NOT NULL,
                title TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                dryRun INTEGER NOT NULL DEFAULT 0,
                platformCount INTEGER NOT NULL DEFAULT 0,
                itemCount INTEGER NOT NULL DEFAULT 0,
                successCount INTEGER NOT NULL DEFAULT 0,
                failedCount INTEGER NOT NULL DEFAULT 0,
                skippedCount INTEGER NOT NULL DEFAULT 0,
                contentType TEXT,
                payloadJson TEXT,
                lastError TEXT,
                pauseReasonCode TEXT,
                resumeSourceTaskId INTEGER,
                revisionSourceTaskId INTEGER,
                createdAt TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                startedAt TEXT,
                finishedAt TEXT
            )
            """
        )
        _add_columns(
            conn,
            "publish_tasks",
            (
                ("accountSummary", "TEXT"),
                ("platformSummary", "TEXT"),
                ("contentType", "TEXT"),
                ("pauseReasonCode", "TEXT"),
                ("resumeSourceTaskId", "INTEGER"),
                ("revisionSourceTaskId", "INTEGER"),
            ),
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_publish_tasks_resume_source
            ON publish_tasks(resumeSourceTaskId)
            WHERE resumeSourceTaskId IS NOT NULL
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_publish_tasks_revision_source
            ON publish_tasks(revisionSourceTaskId)
            WHERE revisionSourceTaskId IS NOT NULL
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS publish_task_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                taskId INTEGER NOT NULL,
                platformType INTEGER NOT NULL,
                platformName TEXT NOT NULL,
                accountFile TEXT,
                accountLabel TEXT,
                filePath TEXT,
                fileName TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                message TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                createdAt TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                startedAt TEXT,
                finishedAt TEXT,
                FOREIGN KEY(taskId) REFERENCES publish_tasks(id) ON DELETE CASCADE
            )
            """
        )
        _add_columns(
            conn,
            "publish_task_items",
            (
                ("profileName", "TEXT"),
                ("userName", "TEXT"),
                ("accountRemark", "TEXT"),
                ("contentType", "TEXT"),
                ("batchItemIndex", "INTEGER"),
                ("locationSummary", "TEXT"),
                ("scheduleSummary", "TEXT"),
            ),
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS publish_task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                taskId INTEGER NOT NULL,
                itemId INTEGER,
                level TEXT NOT NULL DEFAULT 'info',
                eventType TEXT NOT NULL,
                message TEXT,
                detailJson TEXT,
                createdAt TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(taskId) REFERENCES publish_tasks(id) ON DELETE CASCADE,
                FOREIGN KEY(itemId) REFERENCES publish_task_items(id) ON DELETE CASCADE
            )
            """
        )
        conn.commit()
