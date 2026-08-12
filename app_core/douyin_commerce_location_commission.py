# -*- coding: utf-8 -*-
"""抖音地点候选中的可见返佣摘要解析与本地筛选。"""

from __future__ import annotations

import re
from collections.abc import Mapping


COMMISSION_FILTER_ALL = "all"
COMMISSION_FILTER_COMMISSION = "commission"
COMMISSION_FILTER_NO_COMMISSION = "no_commission"
DEFAULT_COMMISSION_FILTER = COMMISSION_FILTER_COMMISSION

_PRODUCT_RE = re.compile(r"(?P<count>\d+)\s*件商品")
_COMMISSION_RE = re.compile(r"(?P<count>\d+)\s*件返佣")
_FILTER_VALUES = frozenset(
    {
        COMMISSION_FILTER_ALL,
        COMMISSION_FILTER_COMMISSION,
        COMMISSION_FILTER_NO_COMMISSION,
    }
)
_OBSERVED_TYPE_VALUES = frozenset(
    {
        COMMISSION_FILTER_COMMISSION,
        COMMISSION_FILTER_NO_COMMISSION,
        "unknown",
    }
)
_COMMISSION_LABELS = {
    COMMISSION_FILTER_COMMISSION: "返佣",
    COMMISSION_FILTER_NO_COMMISSION: "无佣",
    "unknown": "待确认",
}
_PUBLIC_LOCATION_CANDIDATE_FIELDS = (
    "poiId",
    "name",
    "address",
    "distance",
    "source",
    "commissionType",
    "productCount",
    "commissionProductCount",
    "commissionLabel",
)


def _text(value: object) -> str:
    """只接受页面可见的文本，避免将布尔值等伪值转换成摘要。"""

    if not isinstance(value, str):
        raise ValueError("返佣摘要必须是字符串")
    return value.strip()


def normalize_commission_filter(value: object, *, default: str = "all") -> str:
    """校验返佣筛选值；调用方须明确选择新旧数据所需默认值。"""

    if not isinstance(default, str) or default not in _FILTER_VALUES:
        raise ValueError("返佣筛选默认值无效")
    if value is None:
        return default
    if isinstance(value, str) and value in _FILTER_VALUES:
        return value
    raise ValueError("返佣筛选值无效")


def normalize_observed_commission_type(
    value: object, *, default: str = "unknown"
) -> str:
    """校验页面读回的返佣类型，旧数据缺失时使用调用方默认值。"""

    if not isinstance(default, str) or default not in _OBSERVED_TYPE_VALUES:
        raise ValueError("返佣类型默认值无效")
    if value is None:
        return default
    if isinstance(value, str) and value in _OBSERVED_TYPE_VALUES:
        return value
    raise ValueError("返佣类型无效")


def parse_commission_summary(value: object) -> dict[str, object]:
    """从页面可见中文摘要中提取返佣分类与精确数量。"""

    text = _text(value)
    product_match = _PRODUCT_RE.search(text)
    commission_match = _COMMISSION_RE.search(text)
    product_count = int(product_match.group("count")) if product_match else None
    commission_count = int(commission_match.group("count")) if commission_match else None
    if commission_count is not None:
        kind = (
            COMMISSION_FILTER_COMMISSION
            if commission_count > 0
            else COMMISSION_FILTER_NO_COMMISSION
        )
    elif not text:
        kind = COMMISSION_FILTER_NO_COMMISSION
    else:
        kind = "unknown"
    return {
        "commissionType": kind,
        "productCount": product_count,
        "commissionProductCount": commission_count,
        "commissionLabel": _COMMISSION_LABELS[kind],
    }


def filter_location_candidates(
    rows: object, commission_filter: object
) -> list[dict[str, object]]:
    """按返佣类型筛选地点；全部筛选保留待确认项。"""

    selected_filter = normalize_commission_filter(
        commission_filter, default=COMMISSION_FILTER_ALL
    )
    if not isinstance(rows, list):
        return []

    candidates: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        commission_type = normalize_observed_commission_type(
            row.get("commissionType"), default="unknown"
        )
        if (
            selected_filter == COMMISSION_FILTER_ALL
            or commission_type == selected_filter
        ):
            candidates.append(
                {
                    field: row[field]
                    for field in _PUBLIC_LOCATION_CANDIDATE_FIELDS
                    if field in row
                }
            )
    return candidates
