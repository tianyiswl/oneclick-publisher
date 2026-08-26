# -*- coding: utf-8 -*-
"""源码联调复用正式客户端数据时的本机安全辅助。"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from typing import Callable, Iterable, Iterator, Mapping


SOURCE_LIVE_FLAG = "YIJIANFA_SOURCE_LIVE_DATA"
_BACKUP_NAME_RE = re.compile(r"\d{8}-\d{6}-\d+")


class SourceLiveRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceLiveSession:
    production_data_dir: Path
    backup_dir: Path
    marker_path: Path


def build_source_live_environment(
    base_environment: Mapping[str, str], production_data_dir: Path | str
) -> dict[str, str]:
    production = Path(production_data_dir).expanduser().absolute()
    environment = dict(base_environment)
    environment["YIJIANFA_USER_DATA_DIR"] = str(production)
    environment[SOURCE_LIVE_FLAG] = "1"
    environment["PYTHONUNBUFFERED"] = "1"
    return environment


def find_conflicting_installed_processes(
    process_rows: Iterable[str],
) -> list[tuple[int, str]]:
    conflicts: list[tuple[int, str]] = []
    for raw_row in process_rows:
        row = str(raw_row or "").strip()
        if not row:
            continue
        pid_text, separator, command = row.partition(" ")
        if not separator or not pid_text.isdigit():
            continue
        normalized = command.strip()
        if ".app/Contents/MacOS/一键发" not in normalized:
            continue
        if "--mcp-server" in normalized and "--controlled-publish-action" not in normalized:
            continue
        conflicts.append((int(pid_text), normalized))
    return conflicts


def backup_live_runtime(
    production_data_dir: Path | str,
    backup_root: Path | str,
    *,
    backup_name: str,
    max_backups: int = 5,
) -> Path:
    production = Path(production_data_dir).expanduser().resolve()
    database = production / "db" / "database.db"
    if not database.is_file():
        raise SourceLiveRuntimeError("正式客户端账号数据库不存在，请先使用正式客户端登录账号")

    destination = Path(backup_root).expanduser().resolve() / str(backup_name)
    destination.mkdir(parents=True, exist_ok=False)
    destination.chmod(0o700)
    backup_database = destination / "database.db"
    with sqlite3.connect(database) as source, sqlite3.connect(
        backup_database
    ) as target:
        source.backup(target)
    backup_database.chmod(0o600)

    profiles = production / "publish-profiles.json"
    if profiles.is_file():
        backup_profiles = destination / profiles.name
        shutil.copy2(profiles, backup_profiles)
        backup_profiles.chmod(0o600)
    backup_directories = sorted(
        path
        for path in destination.parent.iterdir()
        if path.is_dir() and _BACKUP_NAME_RE.fullmatch(path.name)
    )
    for expired in backup_directories[: -max(1, int(max_backups))]:
        shutil.rmtree(expired)
    return destination


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def source_live_session_active(
    marker_path: Path | str,
    *,
    pid_is_alive: Callable[[int], bool] = _pid_is_alive,
) -> bool:
    marker = Path(marker_path)
    if not marker.is_file():
        return False
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        pid = int(payload.get("pid") or 0)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pid = 0
    if pid > 0 and pid_is_alive(pid):
        return True
    try:
        marker.unlink()
    except FileNotFoundError:
        pass
    return False


def _write_marker(marker_path: Path, payload: Mapping[str, object]) -> None:
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=marker_path.parent,
            prefix=f".{marker_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(dict(payload), handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
            temporary_path = Path(handle.name)
        temporary_path.replace(marker_path)
        marker_path.chmod(0o600)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


@contextmanager
def source_live_session(
    production_data_dir: Path | str,
    backup_root: Path | str,
    *,
    process_rows: Iterable[str],
    marker_path: Path | str,
    backup_name: str,
    current_pid: int,
) -> Iterator[SourceLiveSession]:
    production = Path(production_data_dir).expanduser().absolute()
    marker = Path(marker_path).expanduser().absolute()
    conflicts = find_conflicting_installed_processes(process_rows)
    if conflicts:
        raise SourceLiveRuntimeError(
            "正式客户端或受控发布任务仍在运行，请先结束后再打开源码联调客户端"
        )
    if source_live_session_active(marker):
        raise SourceLiveRuntimeError("另一个源码联调客户端仍在运行")
    backup = backup_live_runtime(production, backup_root, backup_name=backup_name)
    _write_marker(
        marker,
        {
            "pid": int(current_pid),
            "productionDataDir": str(production),
            "backupDir": str(backup),
        },
    )
    session = SourceLiveSession(production, backup, marker)
    try:
        yield session
    finally:
        try:
            current = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = {}
        if int(current.get("pid") or 0) == int(current_pid):
            try:
                marker.unlink()
            except FileNotFoundError:
                pass


def source_live_data_active(environment: Mapping[str, str] | None = None) -> bool:
    values = os.environ if environment is None else environment
    return str(values.get(SOURCE_LIVE_FLAG) or "").strip() == "1"


def installed_gui_block_reason(
    environment: Mapping[str, str],
    marker_path: Path | str,
    *,
    pid_is_alive: Callable[[int], bool] = _pid_is_alive,
) -> str:
    if source_live_data_active(environment):
        return ""
    if source_live_session_active(marker_path, pid_is_alive=pid_is_alive):
        return "源码联调客户端正在共用正式账号数据，请先关闭源码联调客户端"
    return ""
