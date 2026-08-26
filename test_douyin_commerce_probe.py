# -*- coding: utf-8 -*-
"""抖音带货设置采集的内置探针资源测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app_core import douyin_commerce_probe
from app_core.douyin_commerce_probe import (
    DOUYIN_COMMERCE_PROBE_RELATIVE_PATH,
    DouyinCommerceProbeError,
    build_probe_upload_payload,
    resolve_douyin_commerce_probe,
)


class DouyinCommerceProbeTests(unittest.TestCase):
    def _write_probe(self, root: Path, content: bytes | None = None) -> Path:
        probe = root / DOUYIN_COMMERCE_PROBE_RELATIVE_PATH
        probe.parent.mkdir(parents=True)
        if content is None:
            content = b"\x00\x00\x00\x18ftypmp42" + b"x" * 2_048
        probe.write_bytes(content)
        return probe.resolve()

    def test_probe_payload_replaces_user_content_and_keeps_only_account(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            probe = self._write_probe(root)
            payload = build_probe_upload_payload(
                {
                    "type": 3,
                    "workflow": "douyin-commerce",
                    "commerceMode": "local-group-buy",
                    "contentType": "video",
                    "accountId": 31,
                    "accountList": ["account.json"],
                    "fileList": ["/private/user-video.mp4"],
                    "title": "用户标题",
                    "description": "用户文案",
                    "tags": ["用户标签"],
                    "selectedMusic": {"musicId": "old"},
                    "locationPoi": {"poiId": "old"},
                    "locationKeyword": "用户地点",
                    "locationScope": "local",
                    "contentDeclaration": {"origin": "user"},
                    "scheduleTime": "2026-08-10T10:00:00",
                    "commerceStore": {"storeId": "old"},
                    "runtimeMode": "publish",
                    "debugDryRun": False,
                },
                resource_dir=root,
            )

        self.assertEqual(payload["fileList"], [str(probe)])
        self.assertEqual(payload["title"], "一键发平台设置探针")
        self.assertEqual(payload["description"], "仅用于读取平台设置候选，不提交发布")
        self.assertEqual(payload["tags"], [])
        self.assertEqual(payload["runtimeMode"], "preflight")
        self.assertTrue(payload["debugDryRun"])
        self.assertEqual(payload["accountId"], 31)
        self.assertEqual(payload["accountList"], ["account.json"])
        self.assertEqual(
            set(payload),
            {
                "type", "workflow", "commerceMode", "contentType", "accountId",
                "accountList", "fileList", "title", "description", "tags",
                "runtimeMode", "debugDryRun",
            },
        )

    def test_probe_payload_rejects_non_builtin_or_negative_account_ids(self) -> None:
        class ConvertibleAccountId:
            def __int__(self) -> int:
                raise AssertionError("不得执行自定义 accountId 转换")

        invalid_values = (True, -1, "31", ConvertibleAccountId())
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_probe(root)
            for invalid in invalid_values:
                with self.subTest(invalid=type(invalid).__name__), self.assertRaises(
                    DouyinCommerceProbeError
                ):
                    build_probe_upload_payload(
                        {
                            "accountId": invalid,
                            "accountList": ["account.json"],
                        },
                        resource_dir=root,
                    )

    def test_resolve_probe_enforces_one_kib_to_512_kib_size_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaises(DouyinCommerceProbeError):
                resolve_douyin_commerce_probe(root)

            probe = self._write_probe(root, b"")
            with self.assertRaises(DouyinCommerceProbeError):
                resolve_douyin_commerce_probe(root)

            header = b"\x00\x00\x00\x18ftypmp42"
            probe.write_bytes(header + b"x" * (1_023 - len(header)))
            with self.assertRaises(DouyinCommerceProbeError):
                resolve_douyin_commerce_probe(root)

            probe.write_bytes(header + b"x" * (1_024 - len(header)))
            self.assertEqual(resolve_douyin_commerce_probe(root), probe)

            probe.write_bytes(header + b"x" * (512 * 1024 - len(header)))
            self.assertEqual(resolve_douyin_commerce_probe(root), probe)

            probe.write_bytes(header + b"x" * (512 * 1024 + 1 - len(header)))
            with self.assertRaises(DouyinCommerceProbeError):
                resolve_douyin_commerce_probe(root)

    def test_repository_probe_is_small_mp4_outside_demo_runtime(self) -> None:
        root = Path(__file__).resolve().parent
        probe = resolve_douyin_commerce_probe(root)
        content = probe.read_bytes()

        self.assertIn(b"ftyp", content[:32])
        self.assertGreaterEqual(probe.stat().st_size, 1_024)
        self.assertLessEqual(probe.stat().st_size, 512 * 1024)
        self.assertNotIn("demo-runtime", probe.parts)

    def test_frozen_macos_resolves_probe_from_bundle_resources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            contents = Path(temp_dir) / "一键发.app" / "Contents"
            resources = contents / "Resources"
            frameworks = contents / "Frameworks"
            executable = contents / "MacOS" / "一键发"
            frameworks.mkdir(parents=True)
            executable.parent.mkdir(parents=True)
            executable.touch()
            probe = self._write_probe(resources)
            (frameworks / "ui").symlink_to(Path("../Resources/ui"))

            with (
                mock.patch.object(douyin_commerce_probe, "RESOURCE_DIR", frameworks),
                mock.patch.object(douyin_commerce_probe.sys, "frozen", True, create=True),
                mock.patch.object(douyin_commerce_probe.sys, "platform", "darwin"),
                mock.patch.object(
                    douyin_commerce_probe.sys,
                    "executable",
                    str(executable),
                ),
            ):
                resolved = resolve_douyin_commerce_probe()

        self.assertEqual(resolved, probe)


if __name__ == "__main__":
    unittest.main()
