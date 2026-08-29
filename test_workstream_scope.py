# -*- coding: utf-8 -*-

from __future__ import annotations

import unittest
from subprocess import CompletedProcess
from unittest.mock import patch

from tools.check_workstream_scope import changed_files, classify_path, normalize_path


class WorkstreamScopeTests(unittest.TestCase):
    def test_normalizes_relative_and_windows_paths(self):
        self.assertEqual(normalize_path("./app_core\\platform_data_sync.py"), "app_core/platform_data_sync.py")

    def test_shared_core_is_flagged_for_feature_streams(self):
        for stream in ("data", "overseas", "location"):
            with self.subTest(stream=stream):
                self.assertEqual(classify_path("ui/publish_page.py", stream), "shared")
                self.assertEqual(classify_path("tools/build_windows.py", stream), "shared")

    def test_controlled_tiktok_core_and_its_tests_are_shared(self):
        paths = (
            "app_core/tiktok_schedule_contract.py",
            "app_core/controlled_publish.py",
            "app_core/task_service.py",
            "test_tiktok_schedule_contract.py",
            "test_account_detection_ui.py",
            "test_controlled_publish.py",
            "test_task_service.py",
            "test_publish_service.py",
        )
        for stream in ("data", "overseas", "location"):
            for path in paths:
                with self.subTest(stream=stream, path=path):
                    self.assertEqual(classify_path(path, stream), "shared")

    def test_data_stream_owns_collectors_ui_tests_and_architecture(self):
        paths = (
            "app_core/platform_data_sync.py",
            "app_core/xiaohongshu_data_collector.py",
            "ui/data_monitor_page.py",
            "test_bilibili_data_collector.py",
            "docs/architecture/douyin-comment-insight-dataflow.json",
        )
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(classify_path(path, "data"), "owned")

    def test_overseas_stream_owns_platform_modules_and_uploaders(self):
        paths = (
            "app_core/overseas_video_publish.py",
            "app_core/overseas/meta/browser_policy.py",
            "uploader/youtube_uploader/main.py",
            "test_overseas_integration.py",
            "docs/OVERSEAS_PLATFORM_STATUS.md",
        )
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(classify_path(path, "overseas"), "owned")

    def test_overseas_stream_owns_tiktok_architecture_plan_and_verification_docs(self):
        paths = (
            "docs/architecture/tiktok-controlled-video-publish.architecture.html",
            "docs/architecture/tiktok-controlled-video-publish.architecture.json",
            "docs/architecture/tiktok-controlled-video-publish.lifecycle.html",
            "docs/architecture/tiktok-controlled-video-publish.lifecycle.json",
            "docs/superpowers/specs/2026-08-29-tiktok-scheduled-video-publish-design.md",
            "docs/superpowers/plans/2026-08-29-tiktok-scheduled-video-publish.md",
            "docs/verification/2026-08-29-tiktok-scheduled-video-publish.md",
        )
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(classify_path(path, "overseas"), "owned")

    def test_location_stream_owns_douyin_location_and_commerce_files(self):
        paths = (
            "app_core/douyin_location_cache.py",
            "app_core/douyin_commerce_service.py",
            "app_core/_douyin_commerce_batch_receipt_writer.py",
            "ui/douyin_commerce_page.py",
            "test_douyin_location.py",
            "docs/DOUYIN_COMMERCE_WORKFLOW.md",
        )
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(classify_path(path, "location"), "owned")

    def test_xhs_location_files_are_owned_but_publish_wiring_is_shared(self):
        for path in (
            "app_core/xhs_location_service.py",
            "test_xhs_location.py",
            "docs/verification/xhs-video-location.md",
        ):
            self.assertEqual(classify_path(path, "location"), "owned")
        for path in (
            "app_core/oneclick_preflight.py",
            "app_core/xhs_native_adapter.py",
            "app_core/xhs_publish_executor.py",
        ):
            self.assertEqual(classify_path(path, "location"), "shared")

    def test_unrelated_files_require_review(self):
        self.assertEqual(classify_path("ui/dashboard_page.py", "data"), "outside")
        self.assertEqual(classify_path("app_core/unknown_service.py", "overseas"), "outside")
        self.assertEqual(classify_path("uploader/xhs_uploader/main.py", "location"), "outside")

    def test_integration_stream_can_change_any_file(self):
        self.assertEqual(classify_path("ui/publish_page.py", "integration"), "owned")
        self.assertEqual(classify_path("any/new/file.py", "integration"), "owned")

    @patch("tools.check_workstream_scope.subprocess.run")
    def test_changed_files_combines_commits_worktree_index_and_untracked(self, run):
        run.side_effect = (
            CompletedProcess([], 0, "app_core/platform_data_sync.py\n", ""),
            CompletedProcess([], 0, "ui/data_monitor_page.py\n", ""),
            CompletedProcess([], 0, "ui/data_monitor_page.py\ntest_platform_data_sync.py\n", ""),
            CompletedProcess([], 0, "docs/architecture/douyin-comment-insight-dataflow.json\n", ""),
        )

        self.assertEqual(
            changed_files("origin/main"),
            [
                "app_core/platform_data_sync.py",
                "docs/architecture/douyin-comment-insight-dataflow.json",
                "test_platform_data_sync.py",
                "ui/data_monitor_page.py",
            ],
        )
        self.assertEqual(run.call_count, 4)


if __name__ == "__main__":
    unittest.main()
