# -*- coding: utf-8 -*-
"""System-browser attempt primitives for TikTok's manual login phase.

This module deliberately does not automate the browser.  It creates an owned,
private Chromium profile, opens the official TikTok login URL with a normal
system browser, and offers bounded lifecycle helpers for the caller.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

from app_core import account_service
from app_core.overseas_tiktok_identity import (
    TikTokIdentity,
    TikTokIdentityError,
    TIKTOK_IDENTITY_URL,
    normalize_tiktok_handle,
    read_tiktok_identity,
    validate_identity_binding,
)
from app_core.overseas_tiktok_identity_probe import (
    combine_tiktok_identity_probes,
    probe_tiktok_identity_structure,
    sanitize_tiktok_identity_probe,
    summarize_tiktok_identity_probe,
)
from app_core.overseas_tiktok_session_scope import (
    TikTokSessionScopeError,
    sanitize_tiktok_storage_state,
)
from app_core.overseas_tiktok_profile import (
    TikTokPublicProfile,
    fetch_tiktok_public_profile,
    persist_tiktok_public_profile,
    read_tiktok_public_profile,
)
from app_core.paths import COOKIE_DIR, USER_DATA_DIR


_ATTEMPT_ID = re.compile(r"[0-9a-f]{32}\Z")
_PROFILE_LOCK_NAMES = ("SingletonLock", "SingletonCookie", "SingletonSocket")
_TIKTOK_LOGIN_URL = "https://www.tiktok.com/login"
_COMPLETE_BROWSER_EXIT_TIMEOUT_SECONDS = 5.0
_COMPLETE_BROWSER_FORCE_EXIT_TIMEOUT_SECONDS = 5.0
_TIKTOK_IDENTITY_NAVIGATION_TIMEOUT_MS = 120_000
_TIKTOK_IDENTITY_MAX_READ_ATTEMPTS = 5


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


@dataclass(frozen=True, slots=True)
class TikTokLoginCandidate:
    """Sanitized TikTok-only state bound to one twice-read public identity."""

    storage_state: dict[str, list[dict[str, Any]]]
    identity: TikTokIdentity
    tiktok_cookie_present: bool = True
    validation_stage: str = "tiktok_blank_identity_verified"
    public_profile: TikTokPublicProfile | None = None


class TikTokSystemLoginError(RuntimeError):
    """A system-browser login failure that is safe to show in the UI."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        tiktok_cookie_present: bool | None = None,
        validation_stage: str = "",
        identity_probe: object = None,
    ) -> None:
        self.error_code = str(error_code)
        self.public_message = str(message)
        self.tiktok_cookie_present = tiktok_cookie_present
        self.validation_stage = str(validation_stage)
        self.identity_probe = sanitize_tiktok_identity_probe(identity_probe)
        self.probe_summary = summarize_tiktok_identity_probe(self.identity_probe)
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


def _has_symlinked_existing_component(path: Path) -> bool:
    """Fail closed when any existing component of an absolute path is a link."""

    absolute = _absolute_path(path)
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            return False
        except OSError:
            return True
        if stat.S_ISLNK(mode):
            return True
    return False


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
        if _has_symlinked_existing_component(user_root):
            raise _cleanup_failed()
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

    profile_dir = _browser_profile_dir(browser, attempt)
    command = [
        str(browser.executable),
        f"--user-data-dir={profile_dir}",
        "--profile-directory=Default",
        "--new-window",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",
        _TIKTOK_LOGIN_URL,
    ]
    # The profile was verified above as an owned, private staging directory.
    # macOS Chrome otherwise relies on a Keychain item that a dedicated
    # temporary profile cannot persist after the window exits.
    if sys.platform == "darwin" and browser.name == "Google Chrome":
        command.insert(2, "--use-mock-keychain")
    return command


def _uses_macos_google_chrome(browser: SystemBrowserSpec) -> bool:
    return sys.platform == "darwin" and browser.name == "Google Chrome"


def _browser_profile_dir(
    browser: SystemBrowserSpec,
    attempt: TikTokLoginAttempt,
) -> Path:
    """Resolve the only profile eligible for macOS Chrome's mock keychain."""

    if _uses_macos_google_chrome(browser):
        return _resolved_owned_profile(attempt)
    return attempt.profile_dir


def _browser_profile_launch_args(
    browser: SystemBrowserSpec,
    attempt: TikTokLoginAttempt,
) -> list[str]:
    """Return the profile-scoped Chromium flags for the owned profile reopen."""

    # Resolve before adding the mock keychain workaround, so this same safety
    # gate applies both to the interactive launcher and Playwright readback.
    _browser_profile_dir(browser, attempt)
    args = ["--profile-directory=Default"]
    if _uses_macos_google_chrome(browser):
        args.append("--use-mock-keychain")
    return args


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
        or _has_symlinked_existing_component(raw_user_root)
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
        _has_symlinked_existing_component(user_root)
        or user_root.is_symlink()
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


def _owned_process_has_exited(process) -> bool:
    try:
        return process.poll() is not None
    except ProcessLookupError:
        return True
    except Exception:
        return False


def _wait_for_owned_process_stop(process, *, timeout_seconds: float) -> bool:
    try:
        process.wait(timeout=timeout_seconds)
    except ProcessLookupError:
        return True
    except subprocess.TimeoutExpired:
        return False
    except Exception:
        return _owned_process_has_exited(process)
    return _owned_process_has_exited(process)


def _terminate_owned_process(process, *, poll_seconds: float) -> bool:
    """Stop only the owned process and prove it exited within bounded waits."""

    interval = max(0.1, poll_seconds)
    if _owned_process_has_exited(process):
        return True
    try:
        process.terminate()
    except ProcessLookupError:
        return True
    except Exception:
        return _owned_process_has_exited(process)
    if _wait_for_owned_process_stop(process, timeout_seconds=interval):
        return True
    try:
        process.kill()
    except ProcessLookupError:
        return True
    except Exception:
        return _owned_process_has_exited(process)
    return _wait_for_owned_process_stop(process, timeout_seconds=interval)


def _gracefully_stop_owned_process(process, *, timeout_seconds: float) -> bool:
    """Close the owned browser, force-stopping only its hung background process."""

    if _owned_process_has_exited(process):
        return True
    try:
        process.terminate()
    except ProcessLookupError:
        return True
    except Exception:
        return _owned_process_has_exited(process)
    if _wait_for_owned_process_stop(
        process, timeout_seconds=max(0.0, float(timeout_seconds))
    ):
        return True
    try:
        process.kill()
    except ProcessLookupError:
        return True
    except Exception:
        return _owned_process_has_exited(process)
    return _wait_for_owned_process_stop(
        process,
        timeout_seconds=_COMPLETE_BROWSER_FORCE_EXIT_TIMEOUT_SECONDS,
    )


def wait_for_browser_exit(
    process,
    cancel_event: threading.Event,
    *,
    complete_event: threading.Event | None = None,
    complete_timeout_seconds: float = _COMPLETE_BROWSER_EXIT_TIMEOUT_SECONDS,
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
            stopped = _terminate_owned_process(process, poll_seconds=interval)
            return "cancelled" if stopped else "cleanup_failed"
        if complete_event is not None and complete_event.is_set():
            complete_wait = min(
                _COMPLETE_BROWSER_EXIT_TIMEOUT_SECONDS,
                max(0.0, float(complete_timeout_seconds)),
            )
            stopped = _gracefully_stop_owned_process(
                process, timeout_seconds=complete_wait
            )
            return "closed" if stopped else "cleanup_failed"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            stopped = _terminate_owned_process(process, poll_seconds=interval)
            return "timeout" if stopped else "cleanup_failed"
        time.sleep(min(interval, remaining))


def _resolved_owned_profile(attempt: TikTokLoginAttempt) -> Path:
    raw_attempt = _absolute_path(attempt.attempt_root)
    raw_profile = _absolute_path(attempt.profile_dir)
    if (
        str(attempt.attempt_id) != raw_attempt.name
        or _ATTEMPT_ID.fullmatch(raw_attempt.name) is None
        or raw_profile != raw_attempt / "chrome-profile"
        or raw_profile.is_symlink()
    ):
        raise _cleanup_failed()
    resolved_attempt, _ = _resolved_owned_attempt(raw_attempt, attempt.staging_root)
    try:
        resolved_profile = raw_profile.resolve(strict=True)
    except OSError as exc:
        raise _cleanup_failed() from exc
    if resolved_profile.parent != resolved_attempt or not resolved_profile.is_dir():
        raise _cleanup_failed()
    if os.name == "posix" and stat.S_IMODE(resolved_profile.stat().st_mode) != 0o700:
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


def _profile_busy() -> TikTokSystemLoginError:
    return TikTokSystemLoginError(
        "tiktok_login_profile_busy", "TikTok 临时登录资料仍被浏览器占用"
    )


def _translate_identity_error(error: TikTokIdentityError) -> TikTokSystemLoginError:
    code = str(error.error_code or "tiktok_account_invalid")
    if code == "tiktok_account_identity_ambiguous":
        return TikTokSystemLoginError(
            "tiktok_account_invalid", "TikTok 未返回唯一可核对账号"
        )
    if code == "tiktok_account_invalid":
        return TikTokSystemLoginError(
            "tiktok_session_expired", "TikTok 登录状态已失效"
        )
    if code == "tiktok_account_identity_mismatch":
        return TikTokSystemLoginError(
            code, "当前 TikTok 账号与原记录不一致"
        )
    return TikTokSystemLoginError(
        "tiktok_account_invalid", "TikTok 未返回唯一可核对账号"
    )


def _page_is_tiktok_login_or_challenge(page) -> bool:
    """Classify only the route family; never retain a page URL or its query."""

    try:
        parsed = urlsplit(str(getattr(page, "url", "")))
        host = (parsed.hostname or "").lower().rstrip(".")
        path = parsed.path.lower()
    except Exception:
        return False
    if host not in {"tiktok.com", "www.tiktok.com"}:
        return False
    return any(token in path for token in ("login", "challenge", "verify", "captcha", "security"))


def _translate_candidate_identity_error(
    error: TikTokIdentityError,
    page,
    *,
    validation_stage: str,
) -> TikTokSystemLoginError:
    code = str(error.error_code or "")
    if code == "tiktok_account_invalid":
        if _page_is_tiktok_login_or_challenge(page):
            return TikTokSystemLoginError(
                "tiktok_session_expired",
                "TikTok 登录状态已失效",
                tiktok_cookie_present=True,
                validation_stage=validation_stage,
            )
        return TikTokSystemLoginError(
            "tiktok_identity_missing",
            "TikTok 登录状态已读取，但未找到唯一公开账号",
            tiktok_cookie_present=True,
            validation_stage=validation_stage,
        )
    if code == "tiktok_account_identity_ambiguous":
        return TikTokSystemLoginError(
            "tiktok_account_identity_ambiguous",
            "TikTok 返回了多个公开账号，已停止保存",
            tiktok_cookie_present=True,
            validation_stage=validation_stage,
        )
    translated = _translate_identity_error(error)
    return TikTokSystemLoginError(
        translated.error_code,
        translated.public_message,
        tiktok_cookie_present=True,
        validation_stage=validation_stage,
    )


async def _close_playwright_resources(*resources) -> bool:
    close_failed = False
    for resource in resources:
        if resource is None:
            continue
        try:
            await resource.close()
        except Exception:
            close_failed = True
    return close_failed


async def collect_validated_tiktok_candidate(
    attempt: TikTokLoginAttempt,
    browser: SystemBrowserSpec,
    *,
    playwright_factory=async_playwright,
) -> TikTokLoginCandidate:
    """Export one profile, then match two pages in a TikTok-only context."""

    if not wait_for_profile_release(attempt, timeout_seconds=0.0):
        raise _profile_busy()

    persistent = None
    verifier = None
    blank_contexts: list[Any] = []
    blank_pages: list[Any] = []
    primary_error_pending = False
    try:
        try:
            manager = playwright_factory()
            async with manager as playwright:
                try:
                    profile_dir = _browser_profile_dir(browser, attempt)
                    persistent = await playwright.chromium.launch_persistent_context(
                        user_data_dir=str(profile_dir),
                        executable_path=str(browser.executable),
                        headless=True,
                        args=_browser_profile_launch_args(browser, attempt),
                    )
                except Exception as exc:
                    raise _profile_busy() from exc

                sanitized = sanitize_tiktok_storage_state(
                    await persistent.storage_state()
                )
                if await _close_playwright_resources(persistent):
                    raise _profile_busy()
                persistent = None

                verifier = await playwright.chromium.launch(headless=True)
                identity_reads: list[
                    tuple[Any, TikTokIdentity | None, TikTokSystemLoginError | None]
                ] = []
                matching_identities: list[TikTokIdentity] = []
                verified_context = None

                def remember_matching_identity(identity: TikTokIdentity) -> None:
                    handle = normalize_tiktok_handle(identity.handle)
                    if not handle:
                        raise TikTokSystemLoginError(
                            "tiktok_identity_missing",
                            "TikTok 登录状态已读取，但未找到唯一公开账号",
                            tiktok_cookie_present=True,
                            validation_stage="tiktok_blank_identity_probe",
                        )
                    if matching_identities:
                        expected = normalize_tiktok_handle(
                            matching_identities[0].handle
                        )
                        if handle != expected:
                            raise TikTokSystemLoginError(
                                "tiktok_account_identity_mismatch",
                                "TikTok 两次账号回读不一致",
                            )
                    matching_identities.append(identity)

                def validation_stage(
                    context_number: int, *, confirmation: bool
                ) -> str:
                    if context_number == 1:
                        base = "tiktok_blank_identity_first"
                    elif context_number == 2:
                        base = "tiktok_blank_identity_second"
                    else:
                        base = f"tiktok_blank_identity_retry_{context_number}"
                    return f"{base}_confirm" if confirmation else base

                async def read_blank_page(
                    context,
                    *,
                    context_number: int,
                    confirmation: bool,
                ) -> TikTokIdentity | None:
                    nonlocal verified_context
                    page = await context.new_page()
                    blank_pages.append(page)
                    await page.goto(
                        TIKTOK_IDENTITY_URL,
                        wait_until="domcontentloaded",
                        timeout=_TIKTOK_IDENTITY_NAVIGATION_TIMEOUT_MS,
                    )
                    identity = None
                    identity_error = None
                    try:
                        identity = await read_tiktok_identity(page)
                    except TikTokIdentityError as exc:
                        identity_error = _translate_candidate_identity_error(
                            exc,
                            page,
                            validation_stage=validation_stage(
                                context_number,
                                confirmation=confirmation,
                            ),
                        )
                        if identity_error.error_code != "tiktok_identity_missing":
                            raise identity_error from None
                    identity_reads.append((page, identity, identity_error))
                    if identity is not None:
                        remember_matching_identity(identity)
                        if len(matching_identities) >= 2:
                            verified_context = context
                    return identity

                for context_number in range(
                    1, _TIKTOK_IDENTITY_MAX_READ_ATTEMPTS + 1
                ):
                    if len(matching_identities) >= 2:
                        break
                    context = await verifier.new_context(storage_state=sanitized)
                    blank_contexts.append(context)
                    identity = await read_blank_page(
                        context,
                        context_number=context_number,
                        confirmation=False,
                    )
                    if identity is not None and len(matching_identities) < 2:
                        await read_blank_page(
                            context,
                            context_number=context_number,
                            confirmation=True,
                        )

                if len(matching_identities) < 2:
                    missing_reads = [
                        item for item in identity_reads if item[2] is not None
                    ]
                    successful_reads = [
                        item for item in identity_reads if item[1] is not None
                    ]
                    if missing_reads and successful_reads:
                        probe_reads = (missing_reads[0], successful_reads[0])
                    else:
                        probe_reads = tuple(identity_reads[:2])
                    first_probe = await probe_tiktok_identity_structure(
                        probe_reads[0][0]
                    )
                    second_probe = await probe_tiktok_identity_structure(
                        probe_reads[1][0]
                    )
                    identity_probe = combine_tiktok_identity_probes(
                        first_probe,
                        second_probe,
                    )
                    if identity_probe is not None:
                        raise TikTokSystemLoginError(
                            "tiktok_identity_probe_required",
                            "TikTok 当前页面账号入口发生变化，已生成安全诊断，未保存账号",
                            tiktok_cookie_present=True,
                            validation_stage="tiktok_blank_identity_probe",
                            identity_probe=identity_probe,
                        ) from None
                    missing = next(
                        (item[2] for item in identity_reads if item[2] is not None),
                        None,
                    )
                    if missing is None:
                        raise TikTokSystemLoginError(
                            "tiktok_identity_missing",
                            "TikTok 登录状态已读取，但未找到唯一公开账号",
                            tiktok_cookie_present=True,
                            validation_stage="tiktok_blank_identity_probe",
                        ) from None
                    raise TikTokSystemLoginError(
                        missing.error_code,
                        missing.public_message,
                        tiktok_cookie_present=True,
                        validation_stage=missing.validation_stage,
                    ) from None

                if verified_context is None:
                    raise TikTokSystemLoginError(
                        "tiktok_identity_missing",
                        "TikTok 登录状态已读取，但未找到唯一公开账号",
                        tiktok_cookie_present=True,
                        validation_stage="tiktok_blank_identity_probe",
                    ) from None
                confirmed_identity = matching_identities[1]
                public_profile = None
                try:
                    public_profile = await read_tiktok_public_profile(
                        identity_reads[-1][0],
                        confirmed_identity,
                    )
                except TikTokIdentityError as exc:
                    if exc.error_code == "tiktok_account_identity_mismatch":
                        raise
                    try:
                        public_profile = await asyncio.to_thread(
                            fetch_tiktok_public_profile,
                            confirmed_identity,
                        )
                    except TikTokIdentityError as fallback_exc:
                        if fallback_exc.error_code == "tiktok_account_identity_mismatch":
                            raise
                if public_profile is not None:
                    confirmed_identity = TikTokIdentity(
                        public_profile.handle,
                        public_profile.display_name,
                        f"https://www.tiktok.com/@{public_profile.handle}",
                    )
                verified_state = sanitize_tiktok_storage_state(
                    await verified_context.storage_state()
                )

                if await _close_playwright_resources(
                    *blank_pages,
                    *blank_contexts,
                    verifier,
                ):
                    raise _profile_busy()
                blank_pages.clear()
                blank_contexts.clear()
                verifier = None
                candidate = TikTokLoginCandidate(
                    verified_state,
                    confirmed_identity,
                    tiktok_cookie_present=True,
                    validation_stage="tiktok_blank_identity_verified",
                    public_profile=public_profile,
                )
        except TikTokSystemLoginError:
            primary_error_pending = True
            raise
        except TikTokSessionScopeError as exc:
            primary_error_pending = True
            raise TikTokSystemLoginError(
                exc.error_code,
                exc.public_message,
                tiktok_cookie_present=False,
                validation_stage="tiktok_persistent_state",
            ) from exc
        except TikTokIdentityError as exc:
            primary_error_pending = True
            raise _translate_candidate_identity_error(
                exc,
                blank_pages[-1] if blank_pages else None,
                validation_stage="tiktok_blank_identity_first",
            ) from None
        except Exception as exc:
            primary_error_pending = True
            raise TikTokSystemLoginError(
                "tiktok_session_expired", "TikTok 登录状态已失效"
            ) from exc
    finally:
        close_failed = await _close_playwright_resources(
            *blank_pages,
            *blank_contexts,
            verifier,
            persistent,
        )
        if close_failed and not primary_error_pending:
            raise _profile_busy() from None
    return candidate


def _remove_file_safely(path: Path | None) -> bool:
    if path is None:
        return True
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def _write_atomic_tiktok_state(
    storage_state: Mapping[str, Any],
    cookie_dir: Path,
) -> tuple[str, Path]:
    try:
        sanitized = sanitize_tiktok_storage_state(storage_state)
    except TikTokSessionScopeError as exc:
        raise TikTokSystemLoginError(exc.error_code, exc.public_message) from exc

    root = _absolute_path(Path(cookie_dir))
    temporary: Path | None = None
    final: Path | None = None
    try:
        root.mkdir(parents=True, exist_ok=True)
        if root.is_symlink() or not root.is_dir():
            raise OSError("unsafe cookie directory")
        basename = f"{uuid.uuid4().hex}.json"
        temporary = root / f".{basename}.tmp"
        final = root / basename
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                descriptor = -1
                json.dump(
                    sanitized,
                    output,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                output.flush()
                os.fsync(output.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if os.name == "posix":
            temporary.chmod(0o600)
        os.replace(temporary, final)
        temporary = None
        return basename, final
    except Exception as exc:
        cleanup_succeeded = all(
            (
                _remove_file_safely(temporary),
                _remove_file_safely(final),
            )
        )
        if not cleanup_succeeded:
            raise _cleanup_failed() from exc
        raise TikTokSystemLoginError(
            "tiktok_login_commit_failed", "TikTok 会话未能安全写入账号库"
        ) from exc


def _delete_replaced_tiktok_session_if_unreferenced(
    *,
    account_id: int,
    new_basename: str,
    old_basename: str,
    cookie_dir: Path,
) -> None:
    if not old_basename or old_basename == new_basename:
        return
    try:
        with account_service.connect() as conn:
            # Hold a SQLite write reservation across the final reference check
            # and unlink so no type-6 writer can claim the old basename between
            # those two operations.
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT type, filePath FROM user_info WHERE id = ?",
                (int(account_id),),
            ).fetchone()
            old_references = int(
                conn.execute(
                    "SELECT COUNT(*) FROM user_info WHERE type = 6 AND filePath = ?",
                    (old_basename,),
                ).fetchone()[0]
            )
            if (
                current
                and int(current["type"] or 0) == 6
                and Path(str(current["filePath"] or "")).name == new_basename
                and old_references == 0
            ):
                _remove_file_safely(
                    _absolute_path(Path(cookie_dir)) / old_basename
                )
    except Exception:
        # The new row/file are already committed.  If the reference check cannot
        # be proven, preserve the old file instead of risking a live session.
        return


def commit_tiktok_login_candidate(
    candidate: TikTokLoginCandidate,
    profile_name: str,
    *,
    record_id: int | None,
    existing_account: Mapping[str, Any] | None,
    cookie_dir: Path = COOKIE_DIR,
    account_saver=account_service.save_tiktok_browser_account,
    profile_persister=persist_tiktok_public_profile,
) -> int:
    """Atomically replace the session file, compensating any database failure."""

    basename, final_path = _write_atomic_tiktok_state(
        candidate.storage_state,
        cookie_dir,
    )
    try:
        account_id = int(
            account_saver(
                profile_name=profile_name,
                storage_file_name=basename,
                identity=candidate.identity,
                record_id=record_id,
                expected_account=existing_account,
            )
        )
        if account_id <= 0:
            raise ValueError("invalid account id")
    except Exception as exc:
        if not _remove_file_safely(final_path):
            raise _cleanup_failed() from exc
        raise TikTokSystemLoginError(
            "tiktok_login_commit_failed", "TikTok 会话未能安全写入账号库"
        ) from exc

    if record_id is not None and existing_account is not None:
        old_basename = Path(str(existing_account.get("filePath") or "")).name
        _delete_replaced_tiktok_session_if_unreferenced(
            account_id=account_id,
            new_basename=basename,
            old_basename=old_basename,
            cookie_dir=cookie_dir,
        )
    if candidate.public_profile is not None:
        try:
            profile_persister(account_id, candidate.public_profile)
        except Exception:
            # The stable handle and isolated session are already committed.
            # A transient public-avatar write must not turn that valid login
            # into a false failure; account-page refresh can retry this field.
            pass
    return account_id


class TikTokSystemBrowserLoginSession:
    """Queue-compatible system-browser login with cleanup-before-commit order."""

    manual_save_supported = False

    def __init__(
        self,
        profile_name: str,
        *,
        update_mode: bool = False,
        record_id: int | None = None,
        existing_account: Mapping[str, Any] | None = None,
        timeout_seconds: float = 600.0,
        user_data_dir: Path = USER_DATA_DIR,
        cookie_dir: Path = COOKIE_DIR,
        process_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
    ) -> None:
        self.profile_name = str(profile_name or "").strip()
        self.update_mode = bool(update_mode)
        self.record_id = int(record_id) if record_id is not None else None
        self.existing_account = (
            dict(existing_account) if existing_account is not None else None
        )
        self.timeout_seconds = float(timeout_seconds)
        self.user_data_dir = Path(user_data_dir)
        self.cookie_dir = Path(cookie_dir)
        self.process_factory = process_factory
        self.queue: queue.Queue[str] = queue.Queue()
        self.last_error_code: str | None = None
        self.last_identity_probe: dict[str, Any] | None = None
        self._cancel_requested = threading.Event()
        self._complete_requested = threading.Event()
        self._owned_process = None
        self._owned_attempt: TikTokLoginAttempt | None = None

    def start(self) -> None:
        threading.Thread(
            target=self.run,
            daemon=True,
            name="oneclick-tiktok-system-login",
        ).start()

    def cancel(self) -> None:
        self._cancel_requested.set()

    def complete_login(self) -> None:
        """Finish the manual browser phase; validation still decides success."""

        self._complete_requested.set()

    def save(self) -> None:
        self.queue.put("TikTok 会在完成账号核对后自动保存，不支持手动保存。")

    def _cleanup_attempt(
        self,
        attempt: TikTokLoginAttempt | None,
    ) -> TikTokSystemLoginError | None:
        if attempt is None:
            return None
        self.queue.put("CLEANING_LOGIN_ATTEMPT")
        try:
            remove_login_attempt(attempt.attempt_root, attempt.staging_root)
        except TikTokSystemLoginError:
            return _cleanup_failed()
        except Exception:
            return _cleanup_failed()
        return None

    def _finish_failure(
        self,
        error: TikTokSystemLoginError,
        attempt: TikTokLoginAttempt | None,
    ) -> None:
        cleanup_error = self._cleanup_attempt(attempt)
        if cleanup_error is None and attempt is not None:
            self._owned_attempt = None
        final_error = cleanup_error or error
        self.last_error_code = final_error.error_code
        self.last_identity_probe = None
        if final_error.error_code == "tiktok_login_cancelled":
            self.queue.put("CANCELLED")
        else:
            if (
                final_error.error_code == "tiktok_identity_probe_required"
                and final_error.identity_probe is not None
                and final_error.probe_summary
            ):
                self.last_identity_probe = final_error.identity_probe
                self.queue.put(
                    f"IDENTITY_PROBE_SUMMARY:{final_error.probe_summary}"
                )
            self.queue.put(f"ERROR:{final_error.error_code}")

    def run(self) -> int | None:
        attempt: TikTokLoginAttempt | None = None
        try:
            if not self.profile_name:
                raise TikTokSystemLoginError(
                    "tiktok_account_invalid", "TikTok 账号主体不能为空"
                )
            attempt = create_login_attempt(self.user_data_dir)
            self._owned_attempt = attempt
            self.queue.put("OPENING_SYSTEM_BROWSER")
            browser = find_system_browser()
            try:
                self._owned_process = self.process_factory(
                    build_system_browser_command(browser, attempt)
                )
            except Exception as exc:
                raise _browser_unavailable() from exc
            self.queue.put("SYSTEM_BROWSER_OPENED")
            self.queue.put("WAITING_BROWSER_EXIT")

            outcome = wait_for_browser_exit(
                self._owned_process,
                self._cancel_requested,
                complete_event=self._complete_requested,
                timeout_seconds=self.timeout_seconds,
            )
            if outcome == "cleanup_failed":
                # The browser still owns the profile.  Keep both references and
                # staging intact for a later bounded cleanup attempt.
                self.last_error_code = "tiktok_login_cleanup_failed"
                self.queue.put("ERROR:tiktok_login_cleanup_failed")
                return None
            self._owned_process = None
            if outcome == "cancelled" or self._cancel_requested.is_set():
                raise TikTokSystemLoginError(
                    "tiktok_login_cancelled", "TikTok 登录已取消"
                )
            if outcome == "timeout":
                raise TikTokSystemLoginError(
                    "tiktok_login_attempt_timeout", "等待 TikTok 登录超时"
                )
            if not wait_for_profile_release(attempt):
                raise _profile_busy()

            self.queue.put("VALIDATING_TIKTOK_SESSION")
            candidate = asyncio.run(
                collect_validated_tiktok_candidate(attempt, browser)
            )
            if self._cancel_requested.is_set():
                raise TikTokSystemLoginError(
                    "tiktok_login_cancelled", "TikTok 登录已取消"
                )
            if self.update_mode:
                if self.record_id is None or self.existing_account is None:
                    raise TikTokSystemLoginError(
                        "tiktok_account_invalid", "TikTok 账号更新信息不完整"
                    )
                validate_identity_binding(
                    self.existing_account,
                    candidate.identity,
                    allow_initial_bind=False,
                )
            else:
                validate_identity_binding(
                    {"accountReference": ""},
                    candidate.identity,
                    allow_initial_bind=True,
                )

            cleanup_error = self._cleanup_attempt(attempt)
            if cleanup_error is not None:
                attempt = None
                raise cleanup_error
            self._owned_attempt = None
            attempt = None
            if self._cancel_requested.is_set():
                raise TikTokSystemLoginError(
                    "tiktok_login_cancelled", "TikTok 登录已取消"
                )
            account_id = commit_tiktok_login_candidate(
                candidate,
                self.profile_name,
                record_id=self.record_id if self.update_mode else None,
                existing_account=(
                    self.existing_account if self.update_mode else None
                ),
                cookie_dir=self.cookie_dir,
            )
            self.queue.put(f"ACCOUNT_SAVED:{account_id}")
            self.last_error_code = None
            self.last_identity_probe = None
            return account_id
        except TikTokIdentityError as exc:
            self._finish_failure(_translate_identity_error(exc), attempt)
        except TikTokSystemLoginError as exc:
            self._finish_failure(exc, attempt)
        except Exception:
            self._finish_failure(
                TikTokSystemLoginError(
                    "tiktok_session_expired", "TikTok 登录状态已失效"
                ),
                attempt,
            )
        return None
