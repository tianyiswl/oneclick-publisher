# -*- coding: utf-8 -*-
"""平台草稿保存的通用安全检查。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

from utils.publish_observer import publish_event


DEFAULT_DRAFT_SUCCESS_TEXTS = (
    "草稿已保存",
    "已保存到草稿",
    "已保存至草稿",
    "已保存为草稿",
    "保存草稿成功",
    "保存成功",
)


def record_draft_field_warning(
    platform_name: str,
    field_name: str,
    detail: str,
) -> None:
    message = (
        f"{platform_name}{field_name}未完成：{detail}。"
        "已继续保存平台草稿，请在平台草稿中人工补充。"
    )
    publish_event(
        "draft_field_warning",
        message,
        level="warning",
        platformName=platform_name,
        fieldName=field_name,
    )


async def _visible_items(locator) -> list:
    items = []
    for index in range(await locator.count()):
        item = locator.nth(index)
        try:
            if await item.is_visible(timeout=300):
                items.append(item)
        except Exception:
            continue
    return items


async def find_unique_draft_control(page, control_texts: Iterable[str]):
    """只返回唯一、可见的草稿控件，避免误点相似操作。"""
    matches = []
    labels = []
    for text in control_texts:
        role_items = await _visible_items(
            page.get_by_role("button", name=text, exact=True)
        )
        if role_items:
            matches.extend(role_items)
            labels.extend([text] * len(role_items))
            continue
        text_items = await _visible_items(page.get_by_text(text, exact=True))
        matches.extend(text_items)
        labels.extend([text] * len(text_items))

    if not matches:
        return None, ""
    if len(matches) != 1:
        raise RuntimeError(
            f"页面同时出现 {len(matches)} 个可见草稿控件，无法唯一判定，已停止点击"
        )
    return matches[0], labels[0]


async def page_body_text(page) -> str:
    try:
        return await page.evaluate(
            "() => document.body ? document.body.innerText : ''"
        )
    except Exception:
        return ""


async def wait_for_draft_success(
    page,
    *,
    success_texts: Iterable[str] = DEFAULT_DRAFT_SUCCESS_TEXTS,
    baseline_text: str = "",
    attempts: int = 30,
    interval_seconds: float = 0.5,
) -> list[str]:
    """等待平台返回明确的草稿保存成功文案。"""
    markers = tuple(str(item).strip() for item in success_texts if str(item).strip())
    for _ in range(attempts):
        body = await page_body_text(page)
        evidence = [
            marker
            for marker in markers
            if body.count(marker) > baseline_text.count(marker)
        ]
        if evidence:
            return evidence
        await asyncio.sleep(interval_seconds)
    return []


async def save_draft_with_text_control(
    page,
    *,
    platform_name: str,
    control_texts: Iterable[str],
    success_texts: Iterable[str] = DEFAULT_DRAFT_SUCCESS_TEXTS,
) -> dict:
    """点击唯一草稿控件，并要求平台返回成功证据。"""
    control, control_name = await find_unique_draft_control(page, control_texts)
    if control is None:
        raise RuntimeError(
            f"{platform_name}当前页面未提供可验证的保存草稿入口"
        )

    baseline_text = await page_body_text(page)
    await control.scroll_into_view_if_needed(timeout=5000)
    await control.click(timeout=10000)
    evidence = await wait_for_draft_success(
        page,
        success_texts=success_texts,
        baseline_text=baseline_text,
    )
    if not evidence:
        raise RuntimeError(
            f"{platform_name}点击“{control_name}”后未返回草稿保存成功回执"
        )
    return {
        "status": "draft_saved",
        "control": control_name,
        "evidence": evidence,
    }
