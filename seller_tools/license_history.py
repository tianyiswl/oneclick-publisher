# -*- coding: utf-8 -*-
"""卖家本机的激活码签发记录。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .license_crypto import private_key_file


def history_file() -> Path:
    return private_key_file().parent / "issue-history.json"


def load_history(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or history_file()
    if not target.is_file():
        return []
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def append_history(record: dict[str, Any], path: Path | None = None) -> None:
    target = path or history_file()
    records = load_history(target)
    records.insert(
        0,
        {
            **record,
            "createdAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(records[:500], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    target.chmod(0o600)
