# -*- coding: utf-8 -*-
"""Conservative cleanup for files owned by one managed runtime directory."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_MANAGED_REFERENCE_COLUMNS = frozenset({"filePath", "avatarPath"})


@dataclass(frozen=True)
class _ManagedArtifactTarget:
    path: Path
    device: int
    inode: int


def _normalized_managed_dir(managed_dir: Path) -> Path | None:
    try:
        return Path(managed_dir).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None


def _stored_candidate(
    raw_value: Any,
    *,
    managed_dir: Path,
) -> tuple[Path | None, bool]:
    if not isinstance(raw_value, str):
        return None, False
    value = raw_value.strip()
    if not value:
        return None, True
    if "\x00" in value:
        return None, False
    try:
        stored_path = Path(value)
        candidate = (
            stored_path
            if stored_path.is_absolute()
            else managed_dir / stored_path
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return None, False
    return candidate, True


def _deletion_target(
    raw_value: Any,
    *,
    managed_dir: Path,
) -> _ManagedArtifactTarget | None:
    candidate, safe = _stored_candidate(raw_value, managed_dir=managed_dir)
    if not safe or candidate is None:
        return None
    try:
        initial = candidate.lstat()
        if stat.S_ISLNK(initial.st_mode):
            return None
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(managed_dir)
        resolved_stat = resolved.lstat()
        if stat.S_ISLNK(resolved_stat.st_mode):
            return None
    except FileNotFoundError:
        return None
    except (OSError, RuntimeError, ValueError):
        return None
    return _ManagedArtifactTarget(
        path=resolved,
        device=int(resolved_stat.st_dev),
        inode=int(resolved_stat.st_ino),
    )


def _resolved_reference(
    raw_value: Any,
    *,
    managed_dir: Path,
) -> tuple[Path | None, bool]:
    candidate, safe = _stored_candidate(raw_value, managed_dir=managed_dir)
    if not safe or candidate is None:
        return None, safe
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(managed_dir)
    except ValueError:
        return None, True
    except (OSError, RuntimeError):
        return None, False
    return resolved, True


def _reference_matches_target(
    raw_value: Any,
    *,
    managed_dir: Path,
    target: Path,
) -> tuple[bool, bool]:
    reference, safe = _resolved_reference(
        raw_value,
        managed_dir=managed_dir,
    )
    if not safe or reference is None:
        return False, safe
    if reference == target:
        return True, True
    try:
        reference.stat()
    except FileNotFoundError:
        return False, True
    except OSError:
        return False, False
    try:
        return bool(os.path.samefile(reference, target)), True
    except FileNotFoundError:
        return False, True
    except OSError:
        return False, False


def unlink_managed_artifact_if_unreferenced(
    conn,
    *,
    raw_target: Any,
    managed_dir: Path,
    reference_column: str,
) -> bool:
    """Unlink one safe in-directory file only while no row aliases it.

    The caller must hold the same database write transaction that removed or
    replaced the source row. Filesystem uncertainty always keeps the file.
    """

    if reference_column not in _MANAGED_REFERENCE_COLUMNS:
        raise ValueError("unsupported managed reference column")
    resolved_dir = _normalized_managed_dir(managed_dir)
    if resolved_dir is None:
        return False
    target = _deletion_target(raw_target, managed_dir=resolved_dir)
    if target is None:
        return False
    for row in conn.execute(
        f"SELECT {reference_column} FROM user_info"
    ):
        matches, safe = _reference_matches_target(
            row[0],
            managed_dir=resolved_dir,
            target=target.path,
        )
        if not safe or matches:
            return False
    try:
        current = target.path.lstat()
        if stat.S_ISLNK(current.st_mode):
            return False
        if (int(current.st_dev), int(current.st_ino)) != (
            target.device,
            target.inode,
        ):
            return False
        target.path.unlink()
    except (OSError, RuntimeError, ValueError):
        return False
    return True
