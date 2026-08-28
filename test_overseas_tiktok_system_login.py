# -*- coding: utf-8 -*-
"""Tests for the TikTok system-browser login attempt lifecycle."""

from __future__ import annotations

import asyncio
import json
import os
import queue
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from app_core import account_service
from app_core.overseas_tiktok_identity import TikTokIdentity, TikTokIdentityError

from app_core.overseas_tiktok_system_login import (
    SystemBrowserSpec,
    TikTokLoginAttempt,
    TikTokLoginCandidate,
    TikTokSystemBrowserLoginSession,
    TikTokSystemLoginError,
    build_system_browser_command,
    collect_validated_tiktok_candidate,
    commit_tiktok_login_candidate,
    create_login_attempt,
    find_system_browser,
    recover_stale_tiktok_login_attempts,
    remove_login_attempt,
    wait_for_browser_exit,
    wait_for_profile_release,
)


class _FakePage:
    def __init__(self, *, close_error: Exception | None = None) -> None:
        self.close_error = close_error
        self.url = "https://www.tiktok.com/tiktokstudio/upload"
        self.goto_calls: list[tuple[str, str, int]] = []
        self.close_calls = 0

    async def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        self.goto_calls.append((url, wait_until, timeout))

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class _FakePersistentContext:
    def __init__(self, page: _FakePage, storage_state: dict) -> None:
        self.page = page
        self.raw_storage_state = storage_state
        self.new_page_calls = 0
        self.close_calls = 0
        self.close_error: Exception | None = None

    async def new_page(self) -> _FakePage:
        self.new_page_calls += 1
        return self.page

    async def storage_state(self) -> dict:
        return self.raw_storage_state

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class _FakeBlankContext:
    def __init__(self, page: _FakePage, storage_state_input: dict) -> None:
        self.page = page
        self.storage_state_input = storage_state_input
        self.new_page_calls = 0
        self.close_calls = 0

    async def new_page(self) -> _FakePage:
        self.new_page_calls += 1
        return self.page

    async def close(self) -> None:
        self.close_calls += 1


class _FakeVerifierBrowser:
    def __init__(self, pages: list[_FakePage]) -> None:
        self.pages = list(pages)
        self.blank_context: _FakeBlankContext | None = None
        self.blank_contexts: list[_FakeBlankContext] = []
        self.close_calls = 0

    async def new_context(self, *, storage_state: dict) -> _FakeBlankContext:
        page = self.pages[len(self.blank_contexts)]
        self.blank_context = _FakeBlankContext(page, storage_state)
        self.blank_contexts.append(self.blank_context)
        return self.blank_context

    async def close(self) -> None:
        self.close_calls += 1


class _FakeChromium:
    def __init__(
        self,
        persistent: _FakePersistentContext,
        verifier: _FakeVerifierBrowser,
    ) -> None:
        self.persistent = persistent
        self.verifier = verifier
        self.persistent_kwargs: dict | None = None

    async def launch_persistent_context(self, **kwargs) -> _FakePersistentContext:
        self.persistent_kwargs = kwargs
        return self.persistent

    async def launch(self, *, headless: bool) -> _FakeVerifierBrowser:
        if headless is not True:
            raise AssertionError("verification browser must be headless")
        return self.verifier


class _FakePlaywrightManager:
    def __init__(self, chromium: _FakeChromium) -> None:
        self.chromium = chromium
        self.exit_error: Exception | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb) -> None:
        if self.exit_error is not None:
            raise self.exit_error


class FakeProcess:
    """A process double that never starts a browser."""

    def __init__(self, poll_results: list[int | None]) -> None:
        self._poll_results = list(poll_results)
        self.terminate_calls = 0
        self.wait_calls: list[float | None] = []

    def poll(self) -> int | None:
        if len(self._poll_results) > 1:
            return self._poll_results.pop(0)
        return self._poll_results[0]

    def terminate(self) -> None:
        self.terminate_calls += 1

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        self._poll_results = [0]
        return 0


class ProcessExitsDuringTerminate(FakeProcess):
    """Models the owned process exiting after poll() but before terminate()."""

    def terminate(self) -> None:
        self.terminate_calls += 1
        raise ProcessLookupError()


class ProcessStaysAliveAfterTerminate(FakeProcess):
    """Models a browser that survives bounded terminate and kill waits."""

    def __init__(self) -> None:
        super().__init__([None])
        self.kill_calls = 0

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        raise subprocess.TimeoutExpired("owned-browser", timeout)

    def kill(self) -> None:
        self.kill_calls += 1


class ProcessExitsAfterGracefulDelay(FakeProcess):
    """Models the owned browser needing a bounded graceful-exit wait."""

    def __init__(self, required_wait_seconds: float) -> None:
        super().__init__([None])
        self.required_wait_seconds = float(required_wait_seconds)
        self.kill_calls = 0

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        if timeout is None or float(timeout) < self.required_wait_seconds:
            raise subprocess.TimeoutExpired("owned-browser", timeout)
        self._poll_results = [0]
        return 0

    def kill(self) -> None:
        self.kill_calls += 1


class TikTokSystemLoginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name).resolve()
        self.user_data_dir = self.root / "user-data"
        self.staging = self.user_data_dir / "login-staging" / "tiktok"

    def _attempt(self, attempt_id: str = "a" * 32) -> TikTokLoginAttempt:
        attempt_root = self.staging / attempt_id
        profile_dir = attempt_root / "chrome-profile"
        return TikTokLoginAttempt(attempt_id, self.staging, attempt_root, profile_dir)

    def test_login_command_uses_only_the_dedicated_profile_and_no_automation_flags(self):
        browser = SystemBrowserSpec(
            "Google Chrome",
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        )
        attempt = create_login_attempt(self.user_data_dir)

        command = build_system_browser_command(browser, attempt)

        joined = " ".join(command)
        self.assertIn(f"--user-data-dir={attempt.profile_dir}", command)
        self.assertIn("https://www.tiktok.com/login", command)
        for forbidden in ("remote-debugging", "enable-automation", "playwright"):
            self.assertNotIn(forbidden, joined.lower())

    def test_macos_chrome_uses_mock_keychain_only_for_the_private_login_attempt(self):
        attempt = create_login_attempt(self.user_data_dir)
        chrome = SystemBrowserSpec(
            "Google Chrome",
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        )
        edge = SystemBrowserSpec(
            "Microsoft Edge",
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        )

        with patch("app_core.overseas_tiktok_system_login.sys.platform", "darwin"):
            macos_chrome_command = build_system_browser_command(chrome, attempt)
            macos_edge_command = build_system_browser_command(edge, attempt)
        with patch("app_core.overseas_tiktok_system_login.sys.platform", "win32"):
            windows_chrome_command = build_system_browser_command(chrome, attempt)
        with patch("app_core.overseas_tiktok_system_login.sys.platform", "linux"):
            linux_chrome_command = build_system_browser_command(chrome, attempt)

        self.assertIn("--use-mock-keychain", macos_chrome_command)
        self.assertNotIn("--use-mock-keychain", macos_edge_command)
        self.assertNotIn("--use-mock-keychain", windows_chrome_command)
        self.assertNotIn("--use-mock-keychain", linux_chrome_command)

    def test_macos_chrome_rejects_non_owned_missing_or_nonprivate_profile(self):
        chrome = SystemBrowserSpec(
            "Google Chrome",
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        )
        ordinary_profile = self.root / "ordinary-profile"
        ordinary_profile.mkdir()
        ordinary_attempt = TikTokLoginAttempt(
            "a" * 32,
            self.staging,
            self.staging / ("a" * 32),
            ordinary_profile,
        )
        missing_attempt = self._attempt()
        nonprivate_attempt = create_login_attempt(self.user_data_dir)
        if os.name == "posix":
            nonprivate_attempt.profile_dir.chmod(0o755)

        with patch("app_core.overseas_tiktok_system_login.sys.platform", "darwin"):
            for attempt in (ordinary_attempt, missing_attempt, nonprivate_attempt):
                with self.subTest(profile_dir=attempt.profile_dir):
                    with self.assertRaises(TikTokSystemLoginError) as raised:
                        build_system_browser_command(chrome, attempt)
                    self.assertEqual(raised.exception.error_code, "tiktok_login_cleanup_failed")

    def test_create_login_attempt_generates_private_uuid_profile(self):
        attempt = create_login_attempt(self.user_data_dir)

        self.assertEqual(attempt.staging_root, self.staging)
        self.assertEqual(len(attempt.attempt_id), 32)
        self.assertTrue(all(char in "0123456789abcdef" for char in attempt.attempt_id))
        self.assertEqual(attempt.attempt_root, self.staging / attempt.attempt_id)
        self.assertEqual(attempt.profile_dir, attempt.attempt_root / "chrome-profile")
        self.assertTrue(attempt.profile_dir.is_dir())
        if os.name == "posix":
            for directory in (attempt.staging_root, attempt.attempt_root, attempt.profile_dir):
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)

    def test_create_login_attempt_rejects_a_staging_symlink(self):
        external = self.root / "external"
        external.mkdir()
        self.user_data_dir.mkdir()
        (self.user_data_dir / "login-staging").symlink_to(external, target_is_directory=True)

        with self.assertRaises(TikTokSystemLoginError) as raised:
            create_login_attempt(self.user_data_dir)

        self.assertEqual(raised.exception.error_code, "tiktok_login_cleanup_failed")
        self.assertTrue(external.is_dir())

    def test_cleanup_rejects_any_target_outside_exact_staging_root(self):
        outside = self.root / "not-owned"
        outside.mkdir()
        self.staging.mkdir(parents=True)

        with self.assertRaises(TikTokSystemLoginError) as raised:
            remove_login_attempt(outside, self.staging)

        self.assertEqual(raised.exception.error_code, "tiktok_login_cleanup_failed")
        self.assertTrue(outside.is_dir())

    def test_cleanup_removes_only_one_exact_attempt_child(self):
        first = create_login_attempt(self.user_data_dir)
        second = create_login_attempt(self.user_data_dir)

        remove_login_attempt(first.attempt_root, first.staging_root)

        self.assertFalse(first.attempt_root.exists())
        self.assertTrue(second.attempt_root.is_dir())
        self.assertTrue(first.staging_root.is_dir())

    def test_cleanup_rejects_symlink_attempt_without_touching_target(self):
        self.staging.mkdir(parents=True)
        external = self.root / "external-attempt"
        external.mkdir()
        link = self.staging / ("b" * 32)
        link.symlink_to(external, target_is_directory=True)

        with self.assertRaises(TikTokSystemLoginError) as raised:
            remove_login_attempt(link, self.staging)

        self.assertEqual(raised.exception.error_code, "tiktok_login_cleanup_failed")
        self.assertTrue(link.is_symlink())
        self.assertTrue(external.is_dir())

    def test_cleanup_rejects_a_symlinked_user_data_ancestor_without_touching_external_attempt(self):
        external_user_data = self.root / "external-user-data"
        attempt_id = "d" * 32
        external_attempt = (
            external_user_data / "login-staging" / "tiktok" / attempt_id
        )
        external_attempt.mkdir(parents=True)
        self.user_data_dir.symlink_to(external_user_data, target_is_directory=True)

        with self.assertRaises(TikTokSystemLoginError) as raised:
            remove_login_attempt(self.staging / attempt_id, self.staging)

        self.assertEqual(raised.exception.error_code, "tiktok_login_cleanup_failed")
        self.assertTrue(external_attempt.is_dir())

    def test_cleanup_rejects_a_symlink_above_user_data_without_touching_external_attempt(self):
        external_tree = self.root / "external-tree"
        linked_ancestor = self.root / "linked-ancestor"
        attempt_id = "f" * 32
        external_attempt = (
            external_tree / "user-data" / "login-staging" / "tiktok" / attempt_id
        )
        external_attempt.mkdir(parents=True)
        linked_ancestor.symlink_to(external_tree, target_is_directory=True)
        user_data_dir = linked_ancestor / "user-data"
        staging = user_data_dir / "login-staging" / "tiktok"

        with self.assertRaises(TikTokSystemLoginError) as raised:
            remove_login_attempt(staging / attempt_id, staging)

        self.assertEqual(raised.exception.error_code, "tiktok_login_cleanup_failed")
        self.assertTrue(external_attempt.is_dir())

    def test_startup_recovery_removes_safe_direct_children_only(self):
        first = create_login_attempt(self.user_data_dir)
        second = create_login_attempt(self.user_data_dir)
        unsafe = self.staging / "not-an-attempt"
        unsafe.mkdir()
        external = self.root / "external"
        external.mkdir()
        linked = self.staging / ("c" * 32)
        linked.symlink_to(external, target_is_directory=True)

        recovered = recover_stale_tiktok_login_attempts(self.user_data_dir)

        self.assertEqual(set(recovered), {first.attempt_id, second.attempt_id})
        self.assertFalse(first.attempt_root.exists())
        self.assertFalse(second.attempt_root.exists())
        self.assertTrue(unsafe.is_dir())
        self.assertTrue(linked.is_symlink())
        self.assertTrue(external.is_dir())

    def test_startup_recovery_rejects_a_symlinked_user_data_ancestor_without_touching_external_attempt(self):
        external_user_data = self.root / "external-user-data"
        attempt_id = "e" * 32
        external_attempt = (
            external_user_data / "login-staging" / "tiktok" / attempt_id
        )
        external_attempt.mkdir(parents=True)
        self.user_data_dir.symlink_to(external_user_data, target_is_directory=True)

        recovered = recover_stale_tiktok_login_attempts(self.user_data_dir)

        self.assertEqual(recovered, [])
        self.assertTrue(external_attempt.is_dir())

    def test_startup_recovery_rejects_a_symlink_above_user_data_without_touching_external_attempt(self):
        external_tree = self.root / "external-tree"
        linked_ancestor = self.root / "linked-ancestor"
        attempt_id = "0" * 32
        external_attempt = (
            external_tree / "user-data" / "login-staging" / "tiktok" / attempt_id
        )
        external_attempt.mkdir(parents=True)
        linked_ancestor.symlink_to(external_tree, target_is_directory=True)
        user_data_dir = linked_ancestor / "user-data"

        recovered = recover_stale_tiktok_login_attempts(user_data_dir)

        self.assertEqual(recovered, [])
        self.assertTrue(external_attempt.is_dir())

    def test_find_system_browser_prefers_chrome_before_edge_on_linux(self):
        paths = {
            "google-chrome": "/usr/bin/google-chrome",
            "microsoft-edge": "/usr/bin/microsoft-edge",
        }
        with patch(
            "app_core.overseas_tiktok_system_login.shutil.which",
            side_effect=lambda name: paths.get(name),
        ) as which:
            browser = find_system_browser(platform="linux")

        self.assertEqual(browser, SystemBrowserSpec("Google Chrome", Path("/usr/bin/google-chrome")))
        self.assertEqual(which.call_args_list[0].args, ("google-chrome",))

    def test_find_system_browser_uses_fixed_windows_locations_and_edge_fallback(self):
        program_files = self.root / "Program Files"
        edge = program_files / "Microsoft" / "Edge" / "Application" / "msedge.exe"
        edge.parent.mkdir(parents=True)
        edge.touch()
        with patch(
            "app_core.overseas_tiktok_system_login._is_executable",
            side_effect=lambda candidate: candidate == edge,
        ):
            browser = find_system_browser(
                platform="win32",
                environ={"PROGRAMFILES": str(program_files)},
            )

        self.assertEqual(browser, SystemBrowserSpec("Microsoft Edge", edge))

    def test_find_system_browser_raises_a_stable_error_when_none_are_installed(self):
        with patch("app_core.overseas_tiktok_system_login.shutil.which", return_value=None):
            with self.assertRaises(TikTokSystemLoginError) as raised:
                find_system_browser(platform="linux")

        self.assertEqual(raised.exception.error_code, "tiktok_system_browser_unavailable")

    def test_wait_for_browser_exit_returns_closed_for_its_closed_process(self):
        process = FakeProcess([0])

        outcome = wait_for_browser_exit(
            process,
            threading.Event(),
            timeout_seconds=1.0,
            poll_seconds=0.001,
        )

        self.assertEqual(outcome, "closed")
        self.assertEqual(process.terminate_calls, 0)

    def test_complete_event_gracefully_stops_only_the_owned_browser(self):
        process = FakeProcess([None])
        cancel = threading.Event()
        complete = threading.Event()
        complete.set()

        outcome = wait_for_browser_exit(
            process,
            cancel,
            complete_event=complete,
            timeout_seconds=1,
            poll_seconds=0.001,
        )

        self.assertEqual(outcome, "closed")
        self.assertEqual(process.terminate_calls, 1)

    def test_complete_event_allows_a_bounded_delayed_owned_browser_exit(self):
        process = ProcessExitsAfterGracefulDelay(required_wait_seconds=1.0)
        complete = threading.Event()
        complete.set()

        outcome = wait_for_browser_exit(
            process,
            threading.Event(),
            complete_event=complete,
            timeout_seconds=1,
            poll_seconds=0.001,
        )

        self.assertEqual(outcome, "closed")
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.kill_calls, 0)
        self.assertEqual(process.wait_calls, [5.0])

    def test_cancel_takes_priority_over_complete_event(self):
        process = FakeProcess([None])
        cancel = threading.Event()
        complete = threading.Event()
        cancel.set()
        complete.set()

        outcome = wait_for_browser_exit(
            process,
            cancel,
            complete_event=complete,
            timeout_seconds=1,
            poll_seconds=0.001,
        )

        self.assertEqual(outcome, "cancelled")
        self.assertEqual(process.terminate_calls, 1)

    def test_complete_event_fails_closed_when_owned_process_stays_alive(self):
        process = ProcessStaysAliveAfterTerminate()
        complete = threading.Event()
        complete.set()

        outcome = wait_for_browser_exit(
            process,
            threading.Event(),
            complete_event=complete,
            timeout_seconds=1,
            poll_seconds=0.001,
        )

        self.assertEqual(outcome, "cleanup_failed")
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.kill_calls, 0)

    def test_wait_for_browser_exit_cancels_only_its_own_process(self):
        process = FakeProcess([None])
        cancelled = threading.Event()
        cancelled.set()

        outcome = wait_for_browser_exit(
            process,
            cancelled,
            timeout_seconds=1.0,
            poll_seconds=0.001,
        )

        self.assertEqual(outcome, "cancelled")
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(len(process.wait_calls), 1)

    def test_wait_for_browser_exit_times_out_and_terminates_its_own_process(self):
        process = FakeProcess([None])

        outcome = wait_for_browser_exit(
            process,
            threading.Event(),
            timeout_seconds=0.0,
            poll_seconds=0.001,
        )

        self.assertEqual(outcome, "timeout")
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(len(process.wait_calls), 1)

    def test_wait_for_browser_exit_returns_cancelled_when_process_exits_during_terminate(self):
        process = ProcessExitsDuringTerminate([None])
        cancelled = threading.Event()
        cancelled.set()

        outcome = wait_for_browser_exit(
            process,
            cancelled,
            timeout_seconds=1.0,
            poll_seconds=0.001,
        )

        self.assertEqual(outcome, "cancelled")
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.wait_calls, [])

    def test_wait_for_profile_release_returns_immediately_when_no_lock_exists(self):
        attempt = create_login_attempt(self.user_data_dir)

        released = wait_for_profile_release(attempt, timeout_seconds=0.0)

        self.assertTrue(released)

    def test_wait_for_profile_release_has_a_bounded_timeout_and_does_not_remove_lock(self):
        attempt = create_login_attempt(self.user_data_dir)
        lock = attempt.profile_dir / "SingletonLock"
        lock.touch()

        released = wait_for_profile_release(attempt, timeout_seconds=0.0)

        self.assertFalse(released)
        self.assertTrue(lock.exists())

    def test_wait_for_profile_release_treats_a_dangling_singleton_lock_symlink_as_busy(self):
        attempt = create_login_attempt(self.user_data_dir)
        lock = attempt.profile_dir / "SingletonLock"
        lock.symlink_to(self.root / "missing-lock-target")

        released = wait_for_profile_release(attempt, timeout_seconds=0.0)

        self.assertFalse(released)
        self.assertTrue(os.path.lexists(lock))


class TikTokCandidateIntakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name).resolve()
        self.attempt = create_login_attempt(self.root / "user-data")
        self.browser = SystemBrowserSpec("Chrome", self.root / "chrome")
        self.raw_state = {
            "cookies": [
                {"name": "sessionid", "value": "secret", "domain": ".tiktok.com"},
                {"name": "SID", "value": "google-secret", "domain": ".google.com"},
            ],
            "origins": [
                {"origin": "https://www.tiktok.com", "localStorage": []},
                {"origin": "https://accounts.google.com", "localStorage": []},
            ],
        }
        self.persistent_page = _FakePage()
        self.first_blank_page = _FakePage()
        self.second_blank_page = _FakePage()
        self.fake_persistent = _FakePersistentContext(
            self.persistent_page, self.raw_state
        )
        self.fake_verifier = _FakeVerifierBrowser(
            [self.first_blank_page, self.second_blank_page]
        )
        self.fake_chromium = _FakeChromium(self.fake_persistent, self.fake_verifier)
        self.fake_playwright = _FakePlaywrightManager(self.fake_chromium)
        self.fake_playwright_factory = lambda: self.fake_playwright
        self.fake_identity_reads: list[str] = []

    def _collect(self, *results: TikTokIdentity | Exception) -> TikTokLoginCandidate:
        remaining = list(results) or [
            TikTokIdentity(
                "expected.user",
                "Expected",
                "https://www.tiktok.com/@expected.user",
            ),
            TikTokIdentity(
                "expected.user",
                "Expected",
                "https://www.tiktok.com/@expected.user",
            ),
        ]

        async def read_identity(_page):
            result = remaining.pop(0)
            if isinstance(result, Exception):
                raise result
            self.fake_identity_reads.append(result.handle)
            return result

        with patch(
            "app_core.overseas_tiktok_system_login.read_tiktok_identity",
            new=read_identity,
        ):
            return asyncio.run(
                collect_validated_tiktok_candidate(
                    self.attempt,
                    self.browser,
                    playwright_factory=self.fake_playwright_factory,
                )
            )

    def test_candidate_uses_two_blank_contexts_after_exporting_sanitized_state(self):
        candidate = self._collect()

        self.assertEqual(candidate.identity.handle, "expected.user")
        self.assertTrue(getattr(candidate, "tiktok_cookie_present", False))
        self.assertEqual(
            getattr(candidate, "validation_stage", ""),
            "tiktok_blank_identity_verified",
        )
        blanks = self.fake_verifier.blank_contexts
        self.assertEqual(len(blanks), 2)
        for blank in blanks:
            self.assertEqual(
                blank.storage_state_input["cookies"][0]["domain"],
                ".tiktok.com",
            )
            self.assertEqual(blank.new_page_calls, 1)
            self.assertEqual(blank.close_calls, 1)
        self.assertNotIn("google.com", repr(blanks))
        self.assertNotIn("accounts.google.com", repr(candidate.storage_state))
        self.assertEqual(self.fake_identity_reads, ["expected.user", "expected.user"])
        self.assertEqual(self.fake_persistent.new_page_calls, 0)
        self.assertEqual(self.first_blank_page.goto_calls[0][0], "https://www.tiktok.com/")
        self.assertEqual(self.second_blank_page.goto_calls[0][0], "https://www.tiktok.com/")
        self.assertEqual(self.persistent_page.close_calls, 0)
        self.assertEqual(self.first_blank_page.close_calls, 1)
        self.assertEqual(self.second_blank_page.close_calls, 1)
        self.assertGreaterEqual(self.fake_persistent.close_calls, 1)
        self.assertEqual(self.fake_verifier.close_calls, 1)

    def test_macos_google_chrome_reopen_uses_the_same_mock_keychain_profile_mode(self):
        self.browser = SystemBrowserSpec("Google Chrome", self.root / "chrome")

        with patch("app_core.overseas_tiktok_system_login.sys.platform", "darwin"):
            self._collect()

        self.assertEqual(
            self.fake_chromium.persistent_kwargs["args"],
            ["--profile-directory=Default", "--use-mock-keychain"],
        )

    def test_candidate_reopen_keeps_mock_keychain_off_for_edge_windows_and_linux(self):
        cases = (
            ("darwin", "Microsoft Edge"),
            ("win32", "Google Chrome"),
            ("linux", "Google Chrome"),
        )
        for platform, browser_name in cases:
            with self.subTest(platform=platform, browser=browser_name):
                self.browser = SystemBrowserSpec(browser_name, self.root / "chrome")
                self.fake_verifier = _FakeVerifierBrowser(
                    [_FakePage(), _FakePage()]
                )
                self.fake_chromium.verifier = self.fake_verifier
                with patch("app_core.overseas_tiktok_system_login.sys.platform", platform):
                    self._collect()
                self.assertEqual(
                    self.fake_chromium.persistent_kwargs["args"],
                    ["--profile-directory=Default"],
                )

    def test_candidate_rejects_state_without_a_tiktok_cookie(self):
        self.fake_persistent.raw_storage_state = {
            "cookies": [{"name": "SID", "value": "secret", "domain": ".google.com"}],
            "origins": [],
        }

        with self.assertRaises(TikTokSystemLoginError) as raised:
            self._collect()

        self.assertEqual(raised.exception.error_code, "tiktok_session_missing")
        self.assertFalse(getattr(raised.exception, "tiktok_cookie_present", True))
        self.assertEqual(
            getattr(raised.exception, "validation_stage", ""),
            "tiktok_persistent_state",
        )
        self.assertNotIn("secret", raised.exception.public_message)
        self.assertEqual(self.fake_persistent.new_page_calls, 0)
        self.assertEqual(self.fake_verifier.blank_contexts, [])

    def test_candidate_rejects_when_profile_export_has_no_tiktok_state(self):
        self.fake_persistent.raw_storage_state = {"cookies": [], "origins": []}

        with self.assertRaises(TikTokSystemLoginError) as raised:
            self._collect()

        self.assertEqual(raised.exception.error_code, "tiktok_session_missing")
        self.assertFalse(getattr(raised.exception, "tiktok_cookie_present", True))
        self.assertEqual(
            getattr(raised.exception, "validation_stage", ""),
            "tiktok_persistent_state",
        )
        self.assertEqual(self.fake_verifier.blank_contexts, [])

    def test_candidate_revalidates_unknown_tiktok_cookie_name_in_two_blank_contexts(self):
        self.fake_persistent.raw_storage_state = {
            "cookies": [
                {
                    "name": "new_tiktok_session_marker",
                    "value": "secret",
                    "domain": ".tiktok.com",
                }
            ],
            "origins": [],
        }

        try:
            candidate = self._collect()
        except TikTokSystemLoginError as exc:
            self.fail(
                "Unknown TikTok cookie must be checked in blank contexts: "
                f"{exc.error_code}"
            )

        self.assertTrue(getattr(candidate, "tiktok_cookie_present", False))
        self.assertEqual(len(self.fake_verifier.blank_contexts), 2)
        self.assertEqual(self.fake_identity_reads, ["expected.user", "expected.user"])

    def test_candidate_rejects_visitor_tiktok_state_when_blank_context_has_no_identity(self):
        self.fake_persistent.raw_storage_state = {
            "cookies": [
                {
                    "name": "visitor_marker",
                    "value": "visitor-state",
                    "domain": ".tiktok.com",
                }
            ],
            "origins": [],
        }

        with self.assertRaises(TikTokSystemLoginError) as raised:
            self._collect(
                TikTokIdentityError(
                    "tiktok_account_invalid", "TikTok page has no unique account"
                )
            )

        self.assertEqual(raised.exception.error_code, "tiktok_identity_missing")
        self.assertTrue(getattr(raised.exception, "tiktok_cookie_present", False))
        self.assertEqual(len(self.fake_verifier.blank_contexts), 1)

    def test_candidate_rejects_two_blank_context_handle_mismatch(self):
        with self.assertRaises(TikTokSystemLoginError) as raised:
            self._collect(
                TikTokIdentity("first.user", "First", "https://www.tiktok.com/@first.user"),
                TikTokIdentity("second.user", "Second", "https://www.tiktok.com/@second.user"),
            )

        self.assertEqual(
            raised.exception.error_code,
            "tiktok_account_identity_mismatch",
        )

    def test_candidate_translates_blank_context_login_rejection_to_expired(self):
        self.second_blank_page.url = "https://www.tiktok.com/login"
        with self.assertRaises(TikTokSystemLoginError) as raised:
            self._collect(
                TikTokIdentity(
                    "expected.user", "Expected", "https://www.tiktok.com/@expected.user"
                ),
                TikTokIdentityError(
                    "tiktok_account_invalid",
                    "TikTok page has no unique account",
                ),
            )

        self.assertEqual(raised.exception.error_code, "tiktok_session_expired")

    def test_candidate_reports_first_blank_identity_missing_distinctly(self):
        with self.assertRaises(TikTokSystemLoginError) as raised:
            self._collect(
                TikTokIdentityError(
                    "tiktok_account_invalid",
                    "TikTok page has no unique account",
                )
            )

        self.assertEqual(raised.exception.error_code, "tiktok_identity_missing")
        self.assertTrue(getattr(raised.exception, "tiktok_cookie_present", False))
        self.assertEqual(
            getattr(raised.exception, "validation_stage", ""),
            "tiktok_blank_identity_first",
        )

    def test_candidate_reports_ambiguous_blank_identity_distinctly(self):
        with self.assertRaises(TikTokSystemLoginError) as raised:
            self._collect(
                TikTokIdentityError(
                    "tiktok_account_identity_ambiguous",
                    "conflicting public handles",
                )
            )

        self.assertEqual(
            raised.exception.error_code,
            "tiktok_account_identity_ambiguous",
        )
        self.assertTrue(getattr(raised.exception, "tiktok_cookie_present", False))
        self.assertEqual(
            getattr(raised.exception, "validation_stage", ""),
            "tiktok_blank_identity_first",
        )

    def test_blank_identity_failure_closes_resources_without_committing_an_account(self):
        commit = Mock()

        with patch(
            "app_core.overseas_tiktok_system_login.commit_tiktok_login_candidate",
            commit,
        ):
            with self.assertRaises(TikTokSystemLoginError) as raised:
                self._collect(
                    TikTokIdentityError(
                        "tiktok_account_invalid", "TikTok page has no unique account"
                    )
                )

        self.assertEqual(raised.exception.error_code, "tiktok_identity_missing")
        commit.assert_not_called()
        self.assertEqual(self.fake_persistent.close_calls, 1)
        self.assertEqual(self.fake_verifier.close_calls, 1)
        self.assertEqual(self.fake_verifier.blank_contexts[0].close_calls, 1)

    def test_candidate_rejects_a_profile_that_is_still_locked(self):
        (self.attempt.profile_dir / "SingletonLock").touch()

        with self.assertRaises(TikTokSystemLoginError) as raised:
            self._collect()

        self.assertEqual(raised.exception.error_code, "tiktok_login_profile_busy")
        self.assertIsNone(self.fake_chromium.persistent_kwargs)

    def test_candidate_does_not_return_success_when_playwright_close_fails(self):
        self.fake_persistent.close_error = RuntimeError("close failed")

        with self.assertRaises(TikTokSystemLoginError) as raised:
            self._collect()

        self.assertEqual(raised.exception.error_code, "tiktok_login_profile_busy")
        self.assertNotIn("close failed", raised.exception.public_message)


class TikTokCandidateCommitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name).resolve()
        self.cookie_dir = self.root / "cookies"
        self.cookie_dir.mkdir()
        self.candidate = TikTokLoginCandidate(
            {
                "cookies": [
                    {"name": "sessionid", "value": "secret", "domain": ".tiktok.com"}
                ],
                "origins": [
                    {"origin": "https://www.tiktok.com", "localStorage": []}
                ],
            },
            TikTokIdentity(
                "expected.user", "Expected", "https://www.tiktok.com/@expected.user"
            ),
        )
        self.old_cookie = self.cookie_dir / "old.json"
        self.old_cookie.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
        self.old_account = {
            "id": 7,
            "type": 6,
            "filePath": self.old_cookie.name,
            "userName": "Expected",
            "status": 1,
            "profileName": "TikTok 测试",
            "remark": "",
            "lastCheckedAt": "2026-08-27 12:00:00",
            "lastLoginAt": "2026-08-27 12:00:00",
            "authMode": "browser",
            "accountReference": "expected.user",
        }

    def _install_database(self) -> Path:
        database = self.root / "accounts.db"
        connection = sqlite3.connect(database)
        connection.execute(
            """
            CREATE TABLE user_info (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type INTEGER NOT NULL,
                filePath TEXT NOT NULL,
                userName TEXT NOT NULL,
                status INTEGER DEFAULT 0,
                profileName TEXT,
                avatarPath TEXT,
                avatarUpdatedAt TEXT,
                remark TEXT,
                lastCheckedAt TEXT,
                lastLoginAt TEXT,
                authMode TEXT NOT NULL DEFAULT 'browser',
                accountReference TEXT,
                oauthScopeVersion INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        connection.execute(
            """
            INSERT INTO user_info
                (id, type, filePath, userName, status, profileName, remark,
                 lastCheckedAt, lastLoginAt, authMode, accountReference)
            VALUES
                (:id, :type, :filePath, :userName, :status, :profileName, :remark,
                 :lastCheckedAt, :lastLoginAt, :authMode, :accountReference)
            """,
            self.old_account,
        )
        connection.commit()
        connection.close()

        @contextmanager
        def connect_database():
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            try:
                with connection:
                    yield connection
            finally:
                connection.close()

        patcher = patch.object(account_service, "connect", connect_database)
        patcher.start()
        self.addCleanup(patcher.stop)
        return database

    def test_atomic_commit_writes_only_sanitized_json_with_private_permissions(self):
        candidate = TikTokLoginCandidate(
            {
                "cookies": self.candidate.storage_state["cookies"]
                + [{"name": "SID", "value": "foreign", "domain": ".google.com"}],
                "origins": self.candidate.storage_state["origins"]
                + [{"origin": "https://accounts.google.com", "localStorage": []}],
            },
            self.candidate.identity,
        )
        saver = Mock(return_value=11)

        account_id = commit_tiktok_login_candidate(
            candidate,
            "TikTok 测试",
            record_id=None,
            existing_account=None,
            cookie_dir=self.cookie_dir,
            account_saver=saver,
        )

        self.assertEqual(account_id, 11)
        files = sorted(self.cookie_dir.glob("*.json"))
        self.assertEqual(len(files), 2)
        new_file = next(path for path in files if path != self.old_cookie)
        persisted = json.loads(new_file.read_text(encoding="utf-8"))
        self.assertEqual(set(persisted), {"cookies", "origins"})
        self.assertNotIn("google.com", repr(persisted))
        self.assertEqual(list(self.cookie_dir.glob("*.tmp")), [])
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(new_file.stat().st_mode), 0o600)
        saver.assert_called_once()

    def test_database_failure_removes_new_session_and_preserves_old_account(self):
        with self.assertRaises(TikTokSystemLoginError) as raised:
            commit_tiktok_login_candidate(
                self.candidate,
                "TikTok 测试",
                record_id=7,
                existing_account=self.old_account,
                cookie_dir=self.cookie_dir,
                account_saver=Mock(side_effect=RuntimeError("db unavailable")),
            )

        self.assertEqual(raised.exception.error_code, "tiktok_login_commit_failed")
        self.assertEqual(list(self.cookie_dir.glob("*.json")), [self.old_cookie])

    def test_database_failure_reports_cleanup_failed_when_candidate_unlink_fails(self):
        real_unlink = Path.unlink

        def fail_new_session_unlink(path: Path, *args, **kwargs):
            if path.parent == self.cookie_dir and path != self.old_cookie:
                raise OSError("candidate remains busy")
            return real_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", new=fail_new_session_unlink):
            with self.assertRaises(TikTokSystemLoginError) as raised:
                commit_tiktok_login_candidate(
                    self.candidate,
                    "TikTok 测试",
                    record_id=7,
                    existing_account=self.old_account,
                    cookie_dir=self.cookie_dir,
                    account_saver=Mock(side_effect=RuntimeError("db unavailable")),
                )

        self.assertEqual(raised.exception.error_code, "tiktok_login_cleanup_failed")
        self.assertEqual(len(list(self.cookie_dir.glob("*.json"))), 2)

    def test_atomic_write_reports_cleanup_failed_when_partial_temp_cannot_be_removed(self):
        real_unlink = Path.unlink

        def fail_temp_unlink(path: Path, *args, **kwargs):
            if path.name.endswith(".tmp"):
                raise OSError("temporary session remains busy")
            return real_unlink(path, *args, **kwargs)

        with (
            patch(
                "app_core.overseas_tiktok_system_login.os.replace",
                side_effect=OSError("replace failed"),
            ),
            patch.object(Path, "unlink", new=fail_temp_unlink),
        ):
            with self.assertRaises(TikTokSystemLoginError) as raised:
                commit_tiktok_login_candidate(
                    self.candidate,
                    "TikTok 测试",
                    record_id=None,
                    existing_account=None,
                    cookie_dir=self.cookie_dir,
                    account_saver=Mock(return_value=11),
                )

        self.assertEqual(raised.exception.error_code, "tiktok_login_cleanup_failed")
        self.assertTrue(
            any(path.name.endswith(".tmp") for path in self.cookie_dir.iterdir())
        )

    def test_successful_update_repoints_row_before_removing_unreferenced_old_file(self):
        database = self._install_database()

        account_id = commit_tiktok_login_candidate(
            self.candidate,
            "TikTok 测试",
            record_id=7,
            existing_account=self.old_account,
            cookie_dir=self.cookie_dir,
        )

        connection = sqlite3.connect(database)
        row = connection.execute(
            "SELECT id, filePath, accountReference FROM user_info WHERE id = 7"
        ).fetchone()
        connection.close()
        self.assertEqual(account_id, 7)
        self.assertNotEqual(row[1], self.old_cookie.name)
        self.assertEqual(row[2], "expected.user")
        self.assertTrue((self.cookie_dir / row[1]).is_file())
        self.assertFalse(self.old_cookie.exists())

    def test_old_session_final_reference_check_and_unlink_hold_a_write_transaction(self):
        database = self._install_database()
        real_unlink = Path.unlink
        concurrent_outcome: list[str] = []

        def attempt_concurrent_reference(path: Path, *args, **kwargs):
            if path == self.old_cookie:
                connection = sqlite3.connect(database, timeout=0.0)
                try:
                    connection.execute(
                        """
                        INSERT INTO user_info
                            (type, filePath, userName, status, profileName,
                             remark, authMode, accountReference)
                        VALUES
                            (6, 'old.json', 'Concurrent', 1, 'Concurrent',
                             '', 'browser', 'concurrent.user')
                        """
                    )
                    connection.commit()
                except sqlite3.OperationalError as exc:
                    connection.rollback()
                    concurrent_outcome.append(
                        "locked" if "locked" in str(exc).lower() else "error"
                    )
                else:
                    concurrent_outcome.append("inserted")
                finally:
                    connection.close()
            return real_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", new=attempt_concurrent_reference):
            commit_tiktok_login_candidate(
                self.candidate,
                "TikTok 测试",
                record_id=7,
                existing_account=self.old_account,
                cookie_dir=self.cookie_dir,
            )

        connection = sqlite3.connect(database)
        old_references = connection.execute(
            "SELECT COUNT(*) FROM user_info WHERE type = 6 AND filePath = 'old.json'"
        ).fetchone()[0]
        connection.close()
        self.assertEqual(concurrent_outcome, ["locked"])
        self.assertEqual(old_references, 0)
        self.assertFalse(self.old_cookie.exists())

    def test_concurrent_update_rejection_removes_candidate_and_preserves_current_row_file(self):
        database = self._install_database()
        connection = sqlite3.connect(database)
        connection.execute("UPDATE user_info SET remark = 'concurrent' WHERE id = 7")
        connection.commit()
        connection.close()

        with self.assertRaises(TikTokSystemLoginError) as raised:
            commit_tiktok_login_candidate(
                self.candidate,
                "TikTok 测试",
                record_id=7,
                existing_account=self.old_account,
                cookie_dir=self.cookie_dir,
            )

        self.assertEqual(raised.exception.error_code, "tiktok_login_commit_failed")
        connection = sqlite3.connect(database)
        row = connection.execute(
            "SELECT filePath, remark FROM user_info WHERE id = 7"
        ).fetchone()
        connection.close()
        self.assertEqual(row, ("old.json", "concurrent"))
        self.assertEqual(list(self.cookie_dir.glob("*.json")), [self.old_cookie])


class TikTokPublicLoginSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name).resolve()
        self.attempt = create_login_attempt(self.root / "user-data")
        self.browser = SystemBrowserSpec("Chrome", self.root / "chrome")
        self.process = FakeProcess([0])
        self.candidate = TikTokLoginCandidate(
            {
                "cookies": [{"name": "sessionid", "value": "secret", "domain": ".tiktok.com"}],
                "origins": [],
            },
            TikTokIdentity(
                "expected.user", "Expected", "https://www.tiktok.com/@expected.user"
            ),
        )

    def _session(self, **kwargs) -> TikTokSystemBrowserLoginSession:
        return TikTokSystemBrowserLoginSession(
            "TikTok 测试",
            timeout_seconds=0.01,
            user_data_dir=self.root / "user-data",
            cookie_dir=self.root / "cookies",
            process_factory=kwargs.pop("process_factory", Mock(return_value=self.process)),
            **kwargs,
        )

    @staticmethod
    def _messages(session: TikTokSystemBrowserLoginSession) -> list[str]:
        messages: list[str] = []
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                message = session.queue.get(timeout=0.05)
            except queue.Empty:
                continue
            messages.append(message)
            if message == "CANCELLED" or message.startswith(("ACCOUNT_SAVED:", "ERROR:")):
                return messages
        raise AssertionError(f"session did not reach a terminal message: {messages}")

    def test_success_protocol_cleans_staging_before_committing_account(self):
        order: list[str] = []
        session = self._session()

        async def collect(_attempt, _browser):
            order.append("validated")
            return self.candidate

        def cleanup(_attempt_root, _staging_root):
            order.append("cleaned")

        def commit(*_args, **_kwargs):
            order.append("committed")
            return 23

        with (
            patch("app_core.overseas_tiktok_system_login.create_login_attempt", return_value=self.attempt),
            patch("app_core.overseas_tiktok_system_login.find_system_browser", return_value=self.browser),
            patch("app_core.overseas_tiktok_system_login.wait_for_profile_release", return_value=True),
            patch("app_core.overseas_tiktok_system_login.collect_validated_tiktok_candidate", new=collect),
            patch("app_core.overseas_tiktok_system_login.remove_login_attempt", side_effect=cleanup),
            patch("app_core.overseas_tiktok_system_login.commit_tiktok_login_candidate", side_effect=commit),
        ):
            session.start()
            messages = self._messages(session)

        self.assertEqual(
            messages,
            [
                "OPENING_SYSTEM_BROWSER",
                "SYSTEM_BROWSER_OPENED",
                "WAITING_BROWSER_EXIT",
                "VALIDATING_TIKTOK_SESSION",
                "CLEANING_LOGIN_ATTEMPT",
                "ACCOUNT_SAVED:23",
            ],
        )
        self.assertEqual(order, ["validated", "cleaned", "committed"])
        self.assertIsNone(session.last_error_code)

    def test_complete_login_stops_owned_browser_then_uses_normal_validation_flow(self):
        process = FakeProcess([None])
        session = self._session(process_factory=Mock(return_value=process))
        session.complete_login()

        with (
            patch("app_core.overseas_tiktok_system_login.create_login_attempt", return_value=self.attempt),
            patch("app_core.overseas_tiktok_system_login.find_system_browser", return_value=self.browser),
            patch("app_core.overseas_tiktok_system_login.wait_for_profile_release", return_value=True),
            patch("app_core.overseas_tiktok_system_login.collect_validated_tiktok_candidate", new=AsyncMock(return_value=self.candidate)),
            patch("app_core.overseas_tiktok_system_login.remove_login_attempt"),
            patch("app_core.overseas_tiktok_system_login.commit_tiktok_login_candidate", return_value=23),
        ):
            session.start()
            messages = self._messages(session)

        self.assertEqual(process.terminate_calls, 1)
        self.assertNotIn("CANCELLED", messages)
        self.assertEqual(messages[-1], "ACCOUNT_SAVED:23")

    def test_update_mode_rejects_a_different_handle_before_commit(self):
        old_account = {"id": 7, "type": 6, "accountReference": "saved.user"}
        session = self._session(update_mode=True, record_id=7, existing_account=old_account)
        commit = Mock(return_value=7)

        with (
            patch("app_core.overseas_tiktok_system_login.create_login_attempt", return_value=self.attempt),
            patch("app_core.overseas_tiktok_system_login.find_system_browser", return_value=self.browser),
            patch("app_core.overseas_tiktok_system_login.wait_for_profile_release", return_value=True),
            patch("app_core.overseas_tiktok_system_login.collect_validated_tiktok_candidate", new=AsyncMock(return_value=self.candidate)),
            patch("app_core.overseas_tiktok_system_login.remove_login_attempt"),
            patch("app_core.overseas_tiktok_system_login.commit_tiktok_login_candidate", commit),
        ):
            session.start()
            messages = self._messages(session)

        self.assertEqual(messages[-2:], ["CLEANING_LOGIN_ATTEMPT", "ERROR:tiktok_account_identity_mismatch"])
        self.assertEqual(session.last_error_code, "tiktok_account_identity_mismatch")
        commit.assert_not_called()

    def test_timeout_reaches_error_only_after_owned_attempt_cleanup(self):
        session = self._session()
        cleaned = threading.Event()

        def cleanup(_attempt_root, _staging_root):
            cleaned.set()

        with (
            patch("app_core.overseas_tiktok_system_login.create_login_attempt", return_value=self.attempt),
            patch("app_core.overseas_tiktok_system_login.find_system_browser", return_value=self.browser),
            patch("app_core.overseas_tiktok_system_login.wait_for_browser_exit", return_value="timeout"),
            patch("app_core.overseas_tiktok_system_login.remove_login_attempt", side_effect=cleanup),
        ):
            session.start()
            messages = self._messages(session)

        self.assertTrue(cleaned.is_set())
        self.assertEqual(messages[-2:], ["CLEANING_LOGIN_ATTEMPT", "ERROR:tiktok_login_attempt_timeout"])
        self.assertEqual(session.last_error_code, "tiktok_login_attempt_timeout")

    def test_cleanup_failure_takes_precedence_over_validation_failure(self):
        session = self._session()
        with (
            patch("app_core.overseas_tiktok_system_login.create_login_attempt", return_value=self.attempt),
            patch("app_core.overseas_tiktok_system_login.find_system_browser", return_value=self.browser),
            patch("app_core.overseas_tiktok_system_login.wait_for_profile_release", return_value=True),
            patch(
                "app_core.overseas_tiktok_system_login.collect_validated_tiktok_candidate",
                new=AsyncMock(
                    side_effect=TikTokSystemLoginError(
                        "tiktok_session_expired", "TikTok 登录已失效"
                    )
                ),
            ),
            patch(
                "app_core.overseas_tiktok_system_login.remove_login_attempt",
                side_effect=TikTokSystemLoginError(
                    "tiktok_login_cleanup_failed", "cleanup failed"
                ),
            ),
        ):
            session.start()
            messages = self._messages(session)

        self.assertEqual(messages[-2:], ["CLEANING_LOGIN_ATTEMPT", "ERROR:tiktok_login_cleanup_failed"])
        self.assertEqual(session.last_error_code, "tiktok_login_cleanup_failed")

    def test_cancel_stops_owned_process_and_cleans_before_terminal_message(self):
        process = FakeProcess([None])
        session = self._session(process_factory=Mock(return_value=process))
        removed = threading.Event()

        def cleanup(_attempt_root, _staging_root):
            removed.set()

        with (
            patch("app_core.overseas_tiktok_system_login.create_login_attempt", return_value=self.attempt),
            patch("app_core.overseas_tiktok_system_login.find_system_browser", return_value=self.browser),
            patch("app_core.overseas_tiktok_system_login.remove_login_attempt", side_effect=cleanup),
        ):
            session.cancel()
            session.start()
            messages = self._messages(session)

        self.assertTrue(removed.is_set())
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(messages[-2:], ["CLEANING_LOGIN_ATTEMPT", "CANCELLED"])
        self.assertEqual(session.last_error_code, "tiktok_login_cancelled")

    def test_cancel_keeps_owned_process_and_staging_when_bounded_stop_fails(self):
        process = ProcessStaysAliveAfterTerminate()
        session = self._session(process_factory=Mock(return_value=process))
        with (
            patch("app_core.overseas_tiktok_system_login.create_login_attempt", return_value=self.attempt),
            patch("app_core.overseas_tiktok_system_login.find_system_browser", return_value=self.browser),
        ):
            session.cancel()
            session.start()
            messages = self._messages(session)

        self.assertEqual(messages[-1], "ERROR:tiktok_login_cleanup_failed")
        self.assertNotIn("CANCELLED", messages)
        self.assertTrue(self.attempt.attempt_root.is_dir())
        self.assertIs(session._owned_process, process)
        self.assertEqual(session.last_error_code, "tiktok_login_cleanup_failed")

    def test_cancel_after_process_exit_cleans_without_starting_playwright(self):
        session = self._session()
        collector = AsyncMock(return_value=self.candidate)
        with (
            patch("app_core.overseas_tiktok_system_login.create_login_attempt", return_value=self.attempt),
            patch("app_core.overseas_tiktok_system_login.find_system_browser", return_value=self.browser),
            patch("app_core.overseas_tiktok_system_login.collect_validated_tiktok_candidate", collector),
            patch("app_core.overseas_tiktok_system_login.remove_login_attempt"),
        ):
            session.cancel()
            session.start()
            messages = self._messages(session)

        self.assertEqual(messages[-2:], ["CLEANING_LOGIN_ATTEMPT", "CANCELLED"])
        collector.assert_not_awaited()

    def test_manual_save_is_disabled_with_a_plain_language_message(self):
        session = self._session()

        session.save()

        self.assertFalse(session.manual_save_supported)
        message = session.queue.get_nowait()
        self.assertIn("自动保存", message)
        self.assertNotIn("Cookie", message)


if __name__ == "__main__":
    unittest.main()
