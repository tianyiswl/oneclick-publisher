# -*- coding: utf-8 -*-
"""System-browser attempt primitives for TikTok's manual login phase.

This module deliberately does not automate the browser.  It creates an owned,
private Chromium profile, opens the official TikTok login URL with a normal
system browser, and offers bounded lifecycle helpers for the caller.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from app_core.paths import USER_DATA_DIR


_ATTEMPT_ID = re.compile(r"[0-9a-f]{32}\Z")
_PROFILE_LOCK_NAMES = ("SingletonLock", "SingletonCookie", "SingletonSocket")
_TIKTOK_LOGIN_URL = "https://www.tiktok.com/login"


@dataclass(frozen=True, slots=True)
class SystemBrowserSpec:
    """An installed, supported system browser executable."""

    name: str
    executable: Path


@dataclass(frozen=True, slots=True)
class TikTokLoginAttempt:
    """The program-owned paths for one manual TikTok login attempt."""

    attempt_id: str
    staging_root: Path
    attempt_root: Path
    profile_dir: Path


class TikTokSystemLoginError(RuntimeError):
    """A system-browser login failure that is safe to show in the UI."""

    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = str(error_code)
        self.public_message = str(message)
        super().__init__(self.public_message)


def _browser_unavailable() -> TikTokSystemLoginError:
    return TikTokSystemLoginError(
        "tiktok_system_browser_unavailable", "未找到可用的系统 Chrome 或 Edge 浏览器"
    )


def _cleanup_failed() -> TikTokSystemLoginError:
    return TikTokSystemLoginError(
        "tiktok_login_cleanup_failed", "TikTok 登录临时资料清理失败"
    )


def _is_executable(candidate: Path) -> bool:
    """Check an allowlisted candidate without accepting arbitrary paths."""

    if not candidate.is_file():
        return False
    return os.name == "nt" or os.access(candidate, os.X_OK)


def _first_installed(candidates: tuple[Path, ...]) -> Path | None:
    for candidate in candidates:
        if _is_executable(candidate):
            return candidate
    return None


def _windows_browser_candidates(environ: Mapping[str, str], vendor_parts: tuple[str, ...]) -> tuple[Path, ...]:
    roots = tuple(
        Path(value)
        for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
        if (value := environ.get(key, "").strip())
    )
    return tuple(root.joinpath(*vendor_parts) for root in roots)


def find_system_browser(
    *,
    platform: str = sys.platform,
    environ: Mapping[str, str] = os.environ,
    home: Path = Path.home(),
) -> SystemBrowserSpec:
    """Find Chrome first, then Edge, from fixed system locations only."""

    if platform == "darwin":
        chrome = _first_installed(
            (
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                home / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            )
        )
        if chrome is not None:
            return SystemBrowserSpec("Google Chrome", chrome)
        edge = _first_installed(
            (
                Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
                home / "Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            )
        )
        if edge is not None:
            return SystemBrowserSpec("Microsoft Edge", edge)
        raise _browser_unavailable()

    if platform.startswith("win"):
        chrome = _first_installed(
            _windows_browser_candidates(
                environ, ("Google", "Chrome", "Application", "chrome.exe")
            )
        )
        if chrome is not None:
            return SystemBrowserSpec("Google Chrome", chrome)
        edge = _first_installed(
            _windows_browser_candidates(
                environ, ("Microsoft", "Edge", "Application", "msedge.exe")
            )
        )
        if edge is not None:
            return SystemBrowserSpec("Microsoft Edge", edge)
        raise _browser_unavailable()

    if platform.startswith("linux"):
        for name, display_name in (
            ("google-chrome", "Google Chrome"),
            ("google-chrome-stable", "Google Chrome"),
            ("microsoft-edge", "Microsoft Edge"),
            ("microsoft-edge-stable", "Microsoft Edge"),
        ):
            executable = shutil.which(name)
            if executable:
                return SystemBrowserSpec(display_name, Path(executable))
        raise _browser_unavailable()

    raise _browser_unavailable()


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _make_private_directory(path: Path, *, exist_ok: bool = True) -> None:
    path.mkdir(parents=False, exist_ok=exist_ok, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise _cleanup_failed()
    if os.name == "posix":
        path.chmod(0o700)


def create_login_attempt(user_data_dir: Path = USER_DATA_DIR) -> TikTokLoginAttempt:
    """Create one private, program-owned profile below the TikTok staging root."""

    user_root = _absolute_path(Path(user_data_dir))
    try:
        user_root.mkdir(parents=True, exist_ok=True)
        if user_root.is_symlink() or not user_root.is_dir():
            raise _cleanup_failed()

        login_staging = user_root / "login-staging"
        _make_private_directory(login_staging)
        staging_root = login_staging / "tiktok"
        _make_private_directory(staging_root)

        for _ in range(16):
            attempt_id = uuid.uuid4().hex
            attempt_root = staging_root / attempt_id
            try:
                _make_private_directory(attempt_root, exist_ok=False)
            except FileExistsError:
                continue
            profile_dir = attempt_root / "chrome-profile"
            _make_private_directory(profile_dir, exist_ok=False)
            return TikTokLoginAttempt(attempt_id, staging_root, attempt_root, profile_dir)
    except TikTokSystemLoginError:
        raise
    except OSError as exc:
        raise _cleanup_failed() from exc

    raise _cleanup_failed()


def build_system_browser_command(
    browser: SystemBrowserSpec, attempt: TikTokLoginAttempt
) -> list[str]:
    """Build the fixed, non-automated system-browser command for this attempt."""

    return [
        str(browser.executable),
        f"--user-data-dir={attempt.profile_dir}",
        "--profile-directory=Default",
        "--new-window",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",
        _TIKTOK_LOGIN_URL,
    ]


def _resolved_owned_attempt(attempt_root: Path, staging_root: Path) -> tuple[Path, Path]:
    """Return verified attempt/staging paths without accepting aliases or links."""

    raw_staging = _absolute_path(Path(staging_root))
    raw_attempt = _absolute_path(Path(attempt_root))
    raw_login_staging = raw_staging.parent
    raw_user_root = raw_login_staging.parent
    if (
        raw_staging.name != "tiktok"
        or raw_login_staging.name != "login-staging"
        or raw_attempt == raw_staging
        or raw_attempt.parent != raw_staging
        or _ATTEMPT_ID.fullmatch(raw_attempt.name) is None
        or raw_user_root.is_symlink()
        or raw_login_staging.is_symlink()
        or raw_staging.is_symlink()
        or raw_attempt.is_symlink()
        or not raw_staging.is_dir()
        or not raw_attempt.is_dir()
    ):
        raise _cleanup_failed()
    try:
        resolved_staging = raw_staging.resolve(strict=True)
        resolved_attempt = raw_attempt.resolve(strict=True)
    except OSError as exc:
        raise _cleanup_failed() from exc
    if resolved_attempt.parent != resolved_staging:
        raise _cleanup_failed()
    return resolved_attempt, resolved_staging


def remove_login_attempt(attempt_root: Path, staging_root: Path) -> None:
    """Remove exactly one verified, program-owned TikTok attempt directory."""

    try:
        resolved_attempt, _ = _resolved_owned_attempt(attempt_root, staging_root)
        shutil.rmtree(resolved_attempt)
    except TikTokSystemLoginError:
        raise
    except OSError as exc:
        raise _cleanup_failed() from exc


def recover_stale_tiktok_login_attempts(
    user_data_dir: Path = USER_DATA_DIR,
) -> list[str]:
    """Remove safe orphan attempts using direct-child metadata only.

    Recovery intentionally never opens a Chromium profile, history database,
    cookie database, or storage-state file.
    """

    user_root = _absolute_path(Path(user_data_dir))
    login_staging = user_root / "login-staging"
    staging_root = login_staging / "tiktok"
    if (
        user_root.is_symlink()
        or not user_root.is_dir()
        or not staging_root.exists()
        or not staging_root.is_dir()
        or login_staging.is_symlink()
        or staging_root.is_symlink()
    ):
        return []

    recovered: list[str] = []
    try:
        children = tuple(staging_root.iterdir())
    except OSError:
        return recovered
    for child in children:
        if _ATTEMPT_ID.fullmatch(child.name) is None or child.is_symlink():
            continue
        try:
            if not child.is_dir():
                continue
            remove_login_attempt(child, staging_root)
        except TikTokSystemLoginError:
            continue
        recovered.append(child.name)
    return recovered


def _terminate_owned_process(process, *, poll_seconds: float) -> None:
    """Request termination and wait briefly; never signal any other process."""

    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=max(0.1, poll_seconds))
    except (ProcessLookupError, subprocess.TimeoutExpired):
        return


def wait_for_browser_exit(
    process,
    cancel_event: threading.Event,
    *,
    timeout_seconds: float,
    poll_seconds: float = 0.2,
) -> str:
    """Wait for the owned browser process and return one finite public outcome."""

    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    interval = max(0.001, float(poll_seconds))
    while True:
        if process.poll() is not None:
            return "closed"
        if cancel_event.is_set():
            _terminate_owned_process(process, poll_seconds=interval)
            return "cancelled"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate_owned_process(process, poll_seconds=interval)
            return "timeout"
        time.sleep(min(interval, remaining))


def _resolved_owned_profile(attempt: TikTokLoginAttempt) -> Path:
    raw_attempt = _absolute_path(attempt.attempt_root)
    raw_profile = _absolute_path(attempt.profile_dir)
    if raw_profile != raw_attempt / "chrome-profile" or raw_profile.is_symlink():
        raise _cleanup_failed()
    resolved_attempt, _ = _resolved_owned_attempt(raw_attempt, attempt.staging_root)
    try:
        resolved_profile = raw_profile.resolve(strict=True)
    except OSError as exc:
        raise _cleanup_failed() from exc
    if resolved_profile.parent != resolved_attempt or not resolved_profile.is_dir():
        raise _cleanup_failed()
    return resolved_profile


def _profile_lock_entry_exists(lock_path: Path) -> bool:
    """Check for a lock directory entry without following a symlink target."""

    try:
        lock_path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def wait_for_profile_release(
    attempt: TikTokLoginAttempt,
    *,
    timeout_seconds: float = 15.0,
    poll_seconds: float = 0.2,
) -> bool:
    """Poll only Chromium's profile-lock names; never inspect or alter profile data."""

    profile_dir = _resolved_owned_profile(attempt)
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    interval = max(0.001, float(poll_seconds))
    while True:
        if not any(_profile_lock_entry_exists(profile_dir / name) for name in _PROFILE_LOCK_NAMES):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(interval, remaining))
