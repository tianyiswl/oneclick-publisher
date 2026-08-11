# -*- coding: utf-8 -*-
"""平台合集读取与本地缓存服务。

合集同步只复用一键发保存的官方平台登录态，读取合集名称并写入本地缓存；
不会上传素材、修改合集、保存草稿或提交发布。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .database import connect
from .paths import COOKIE_DIR


DOUYIN_COLLECTIONS_URL = (
    "https://creator.douyin.com/creator-micro/content/manage?tab=collections"
)
SUPPORTED_PLATFORM_TYPES = frozenset({3})
MAX_COLLECTIONS = 100


class CollectionSyncError(RuntimeError):
    """平台合集无法安全读取。"""


def _normalized(value: object) -> str:
    return " ".join(str(value or "").replace("\u200b", "").split())


def _account_id(account: dict) -> int:
    try:
        account_id = int(account.get("id") or 0)
    except (TypeError, ValueError) as exc:
        raise CollectionSyncError("账号记录无效") from exc
    if account_id <= 0:
        raise CollectionSyncError("账号记录无效")
    return account_id


def _platform_type(account: dict) -> int:
    try:
        platform_type = int(account.get("type") or 0)
    except (TypeError, ValueError) as exc:
        raise CollectionSyncError("账号平台记录无效") from exc
    if platform_type <= 0:
        raise CollectionSyncError("账号平台记录无效")
    return platform_type


def _storage_state(account: dict) -> Path:
    file_name = Path(str(account.get("filePath") or "")).name
    state = COOKIE_DIR / file_name
    if not file_name or not state.is_file():
        raise CollectionSyncError("一键发本地登录会话不存在，请先重新登录")
    return state


def normalize_collection_names(values: Iterable[object]) -> list[str]:
    """清理、去重合集名称，同时保留平台返回顺序。"""

    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        name = _normalized(value)
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(name)
        if len(result) >= MAX_COLLECTIONS:
            break
    return result


def cached_collections(
    account_ids: Iterable[int],
    platform_type: int,
) -> dict[int, list[str]]:
    """按账号读取本地合集缓存；没有缓存的账号也返回空列表。"""

    normalized_ids: list[int] = []
    for value in account_ids:
        try:
            account_id = int(value)
        except (TypeError, ValueError):
            continue
        if account_id > 0 and account_id not in normalized_ids:
            normalized_ids.append(account_id)
    if not normalized_ids:
        return {}

    result = {account_id: [] for account_id in normalized_ids}
    placeholders = ",".join("?" for _ in normalized_ids)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT accountId, collectionName
            FROM platform_collections
            WHERE platformType = ? AND accountId IN ({placeholders})
            ORDER BY accountId, id
            """,
            (int(platform_type), *normalized_ids),
        ).fetchall()
    for row in rows:
        account_id = int(row["accountId"])
        name = _normalized(row["collectionName"])
        if name and name not in result[account_id]:
            result[account_id].append(name)
    return result


def common_collections(by_account: dict[int, Iterable[object]]) -> list[str]:
    """返回所有所选账号共有的合集，并保留第一个账号的显示顺序。"""

    if not isinstance(by_account, dict) or not by_account:
        return []
    ordered_rows = list(by_account.values())
    first = normalize_collection_names(ordered_rows[0])
    if len(ordered_rows) == 1:
        return first
    remaining = [set(normalize_collection_names(row)) for row in ordered_rows[1:]]
    return [name for name in first if all(name in names for names in remaining)]


def _replace_cached_collections(
    account_id: int,
    platform_type: int,
    names: Iterable[object],
) -> None:
    normalized = normalize_collection_names(names)
    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with connect() as conn:
        conn.execute(
            "DELETE FROM platform_collections WHERE accountId = ? AND platformType = ?",
            (int(account_id), int(platform_type)),
        )
        conn.executemany(
            """
            INSERT INTO platform_collections
                (accountId, platformType, collectionName, updatedAt)
            VALUES (?, ?, ?, ?)
            """,
            [
                (int(account_id), int(platform_type), name, updated_at)
                for name in normalized
            ],
        )


async def _read_douyin_collections(page: Any) -> list[str]:
    await page.goto(
        DOUYIN_COLLECTIONS_URL,
        wait_until="domcontentloaded",
        timeout=45_000,
    )
    await page.wait_for_function(
        """() => {
            const text = document.body?.innerText || '';
            // “创建合集”会在列表数据返回前先出现，不能据此判定读取完成。
            return text.includes('没有更多合集')
                || text.includes('扫码登录')
                || text.includes('登录抖音');
        }""",
        timeout=15_000,
    )
    body_text = _normalized(await page.locator("body").inner_text())
    if "内容管理" not in body_text and (
        "扫码登录" in body_text or "登录抖音" in body_text
    ):
        raise CollectionSyncError("抖音登录会话已失效，请先在账号管理中重新登录")

    names = await page.locator('[class*="new-layout-title-text"]').all_inner_texts()
    if not names:
        # 样式散列变化时使用标题容器兜底；只接收不含操作按钮的单行文本。
        candidates = await page.locator('[class*="new-layout-title-"]').all_inner_texts()
        blocked = {"编辑合集", "设为私密", "删除合集", "创建合集"}
        names = [
            value
            for value in candidates
            if "\n" not in value and _normalized(value) not in blocked
        ]
    return normalize_collection_names(names)


async def _sync_accounts(accounts: list[dict], platform_type: int) -> dict[int, list[str]]:
    from playwright.async_api import async_playwright
    from utils.base_social_media import launch_chromium_with_codecs

    by_account: dict[int, list[str]] = {}
    async with async_playwright() as playwright:
        browser = await launch_chromium_with_codecs(
            playwright,
            headless=True,
            force_bundled=True,
        )
        try:
            for account in accounts:
                account_id = _account_id(account)
                context = await browser.new_context(
                    storage_state=str(_storage_state(account)),
                    locale="zh-CN",
                )
                try:
                    page = await context.new_page()
                    if platform_type == 3:
                        names = await _read_douyin_collections(page)
                    else:  # pragma: no cover - 入口校验会先阻止不支持的平台
                        raise CollectionSyncError("该平台暂不支持自动同步合集")
                    by_account[account_id] = names
                finally:
                    await context.close()
        finally:
            await browser.close()
    return by_account


def sync_accounts_collections(accounts: Iterable[dict]) -> dict[str, object]:
    """读取所选账号合集、刷新缓存，并返回稳定的界面数据结构。"""

    normalized_accounts = [dict(account) for account in accounts if isinstance(account, dict)]
    if not normalized_accounts:
        raise CollectionSyncError("请先选择账号")
    platform_types = {_platform_type(account) for account in normalized_accounts}
    if len(platform_types) != 1:
        raise CollectionSyncError("一次只能同步同一平台的账号合集")
    platform_type = platform_types.pop()
    if platform_type not in SUPPORTED_PLATFORM_TYPES:
        raise CollectionSyncError("当前仅抖音支持自动同步合集，可先手动输入合集名称")

    try:
        by_account = asyncio.run(_sync_accounts(normalized_accounts, platform_type))
    except CollectionSyncError:
        raise
    except Exception as exc:
        message = _normalized(str(exc)) or exc.__class__.__name__
        raise CollectionSyncError(f"平台合集读取失败：{message}") from exc

    for account_id, names in by_account.items():
        _replace_cached_collections(account_id, platform_type, names)
    return {
        "platformType": platform_type,
        "collections": common_collections(by_account),
        "byAccount": by_account,
    }
