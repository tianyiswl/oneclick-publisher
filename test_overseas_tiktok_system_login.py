# -*- coding: utf-8 -*-
"""Tests for the TikTok system-browser login attempt lifecycle."""

from __future__ import annotations

import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app_core.overseas_tiktok_system_login import (
    SystemBrowserSpec,
    TikTokLoginAttempt,
    TikTokSystemLoginError,
    build_system_browser_command,
    create_login_attempt,
    find_system_browser,
    recover_stale_tiktok_login_attempts,
    remove_login_attempt,
    wait_for_browser_exit,
    wait_for_profile_release,
)


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
        return 0


class TikTokSystemLoginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
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
        attempt = self._attempt()

        command = build_system_browser_command(browser, attempt)

        joined = " ".join(command)
        self.assertIn(f"--user-data-dir={attempt.profile_dir}", command)
        self.assertIn("https://www.tiktok.com/login", command)
        for forbidden in ("remote-debugging", "enable-automation", "playwright"):
            self.assertNotIn(forbidden, joined.lower())

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
        self.assertEqual(process.wait_calls, [])

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


if __name__ == "__main__":
    unittest.main()
