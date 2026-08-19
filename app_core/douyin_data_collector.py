# -*- coding: utf-8 -*-
"""抖音创作者数据的登录会话只读采集器。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import json
import math
from pathlib import Path
from typing import Callable

import requests

from .paths import COOKIE_DIR
from .platform_data_models import CollectionBatch, CollectionFailure, MetricPoint


DOUYIN_DASHBOARD_URL = (
    "https://creator.douyin.com/janus/douyin/creator/data/overview/dashboard"
)
_REQUEST_TIMEOUT_SECONDS = 20.0
_RAW_METRIC_MAP = {
    "play": "views",
    "play_cnt": "views",
    "digg": "likes",
    "digg_cnt": "likes",
    "comment": "comments",
    "comment_cnt": "comments",
    "share": "shares",
    "share_cnt": "shares",
    "fans": "followers_total",
    "fans_cnt": "followers_total",
    "new_fans": "followers_net",
    "net_fans_cnt": "followers_net",
    "profile": "profile_visits",
    "profile_cnt": "profile_visits",
}


class DouyinDataCollectionError(CollectionFailure):
    def __init__(self, error_code: str, *, fallback_allowed: bool) -> None:
        self.fallback_allowed = bool(fallback_allowed)
        super().__init__(error_code, retryable=fallback_allowed)


def _is_douyin_cookie_domain(value: object) -> bool:
    if type(value) is not str:
        return False
    domain = value.strip().lower().lstrip(".")
    return domain == "douyin.com" or domain.endswith(".douyin.com")


def _required_account(account: object) -> tuple[int, Path]:
    if type(account) is not dict:
        raise DouyinDataCollectionError(
            "metric_payload_invalid", fallback_allowed=False
        )
    account_id = account.get("id")
    platform_type = account.get("type")
    file_path = account.get("filePath")
    if (
        type(account_id) is not int
        or account_id <= 0
        or type(platform_type) is not int
        or platform_type != 3
        or type(file_path) is not str
        or not file_path.strip()
    ):
        raise DouyinDataCollectionError(
            "metric_payload_invalid", fallback_allowed=False
        )
    state_path = COOKIE_DIR / Path(file_path).name
    if not state_path.is_file():
        raise DouyinDataCollectionError(
            "session_state_missing", fallback_allowed=False
        )
    return account_id, state_path


def _contains_login_rejection(value: object) -> bool:
    if isinstance(value, Mapping):
        status = value.get("status_code")
        if type(status) is int and status == 8:
            return True
        return any(_contains_login_rejection(item) for item in value.values())
    if type(value) is list:
        return any(_contains_login_rejection(item) for item in value)
    return False


def _observed_at(date_value: object) -> str:
    if type(date_value) is str:
        raw = date_value.strip()
        if len(raw) == 8 and raw.isdigit():
            return (
                f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
                "T00:00:00+08:00"
            )
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _metric_value(metric: Mapping) -> tuple[int | float, str]:
    trends = metric.get("trends")
    if type(trends) is not list or not trends:
        raise DouyinDataCollectionError(
            "metric_payload_invalid", fallback_allowed=False
        )
    latest = trends[-1]
    if not isinstance(latest, Mapping):
        raise DouyinDataCollectionError(
            "metric_payload_invalid", fallback_allowed=False
        )
    value = latest.get("value")
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise DouyinDataCollectionError(
            "metric_payload_invalid", fallback_allowed=False
        )
    return value, _observed_at(latest.get("date_time"))


class DouyinDataCollector:
    def __init__(
        self,
        *,
        session_factory: Callable[[], object] = requests.Session,
    ) -> None:
        self._session_factory = session_factory

    def _load_state(self, state_path: Path) -> dict:
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            raise DouyinDataCollectionError(
                "session_state_missing", fallback_allowed=False
            ) from None
        if type(payload) is not dict or type(payload.get("cookies")) is not list:
            raise DouyinDataCollectionError(
                "session_state_missing", fallback_allowed=False
            )
        return payload

    def _install_state(self, session: object, state: dict) -> None:
        cookies = getattr(session, "cookies", None)
        headers = getattr(session, "headers", None)
        if cookies is None or headers is None:
            raise DouyinDataCollectionError(
                "direct_request_rejected", fallback_allowed=False
            )
        for item in state["cookies"]:
            if not isinstance(item, Mapping):
                continue
            if not _is_douyin_cookie_domain(item.get("domain")):
                continue
            name = item.get("name")
            value = item.get("value")
            if type(name) is not str or not name or type(value) is not str:
                continue
            domain = str(item.get("domain") or "")
            path = item.get("path")
            cookies.set(
                name,
                value,
                domain=domain,
                path=path if type(path) is str and path else "/",
            )
        headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": "https://creator.douyin.com",
                "Referer": (
                    "https://creator.douyin.com/"
                    "creator-micro/data-center/operation"
                ),
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
                ),
            }
        )

    def _parse_payload(self, payload: object, account_id: int) -> CollectionBatch:
        if not isinstance(payload, Mapping):
            raise DouyinDataCollectionError(
                "metric_payload_invalid", fallback_allowed=False
            )
        if _contains_login_rejection(payload):
            raise DouyinDataCollectionError(
                "login_required", fallback_allowed=True
            )
        status_code = payload.get("status_code")
        if type(status_code) is not int or status_code != 0:
            raise DouyinDataCollectionError(
                "direct_request_rejected", fallback_allowed=True
            )
        metrics = payload.get("metrics")
        if type(metrics) is not list:
            raise DouyinDataCollectionError(
                "metric_payload_empty", fallback_allowed=True
            )
        points: list[MetricPoint] = []
        seen: set[str] = set()
        for metric in metrics:
            if not isinstance(metric, Mapping):
                continue
            raw_key = metric.get("english_metric_name")
            if type(raw_key) is not str:
                continue
            metric_key = _RAW_METRIC_MAP.get(raw_key)
            if metric_key is None or metric_key in seen:
                continue
            value, observed_at = _metric_value(metric)
            points.append(
                MetricPoint(
                    entity_type="account",
                    entity_key=f"account:{account_id}",
                    metric_key=metric_key,
                    raw_metric_key=raw_key,
                    metric_value=value,
                    metric_unit="count",
                    observed_at=observed_at,
                )
            )
            seen.add(metric_key)
        if not points:
            raise DouyinDataCollectionError(
                "metric_payload_empty", fallback_allowed=True
            )
        return CollectionBatch(
            platform_type=3,
            source_mode="direct_session",
            metrics=tuple(points),
        )

    def collect_direct(self, account: dict) -> CollectionBatch:
        account_id, state_path = _required_account(account)
        state = self._load_state(state_path)
        session = None
        try:
            session = self._session_factory()
            self._install_state(session, state)
            response = session.post(
                DOUYIN_DASHBOARD_URL,
                json={"recent_days": 30},
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
            if type(getattr(response, "status_code", None)) is not int:
                raise DouyinDataCollectionError(
                    "direct_request_rejected", fallback_allowed=False
                )
            if response.status_code != 200:
                raise DouyinDataCollectionError(
                    "direct_request_rejected",
                    fallback_allowed=response.status_code in (401, 403),
                )
            return self._parse_payload(response.json(), account_id)
        except DouyinDataCollectionError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            raise DouyinDataCollectionError(
                "direct_request_rejected", fallback_allowed=False
            ) from None
        finally:
            if session is not None:
                try:
                    session.close()
                except (KeyboardInterrupt, SystemExit):
                    raise
                except BaseException:
                    pass

    def collect(self, account: dict) -> CollectionBatch:
        return self.collect_direct(account)
