# -*- coding: utf-8 -*-
"""源码联调共用正式账号数据的安全边界测试。"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from app_core.source_live_runtime import (
    SourceLiveRuntimeError,
    backup_live_runtime,
    build_source_live_environment,
    find_conflicting_installed_processes,
    installed_gui_block_reason,
    source_live_session,
    source_live_session_active,
)


class SourceLiveRuntimeTests(unittest.TestCase):
    @staticmethod
    def _create_database(production: Path) -> None:
        database = production / "db" / "database.db"
        database.parent.mkdir(parents=True)
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE accounts (id INTEGER, name TEXT)")
            connection.execute("INSERT INTO accounts VALUES (4, '测试账号')")

    def test_environment_points_source_to_installed_data_and_marks_live_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            production = Path(temp_dir) / "正式数据"
            environment = build_source_live_environment(
                {"PATH": "/usr/bin"}, production
            )

        self.assertEqual(environment["PATH"], "/usr/bin")
        self.assertEqual(environment["YIJIANFA_USER_DATA_DIR"], str(production))
        self.assertEqual(environment["YIJIANFA_SOURCE_LIVE_DATA"], "1")
        self.assertEqual(environment["PYTHONUNBUFFERED"], "1")

    def test_running_installed_ui_or_controlled_task_blocks_source_but_idle_mcp_does_not(self) -> None:
        rows = [
            "101 /Users/andy/文件/一键发/一键发.app/Contents/MacOS/一键发",
            "102 /Applications/一键发.app/Contents/MacOS/一键发 --mcp-server",
            (
                "103 /Applications/一键发.app/Contents/MacOS/一键发 "
                "--controlled-publish-action create"
            ),
            "104 python desktop_native_app.py --page commerce",
        ]

        conflicts = find_conflicting_installed_processes(rows)

        self.assertEqual([item[0] for item in conflicts], [101, 103])

    def test_backup_copies_consistent_database_and_project_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            production = root / "正式数据"
            self._create_database(production)
            profiles = production / "publish-profiles.json"
            profiles.write_text(
                json.dumps({"schemaVersion": 1, "profiles": []}, ensure_ascii=False),
                encoding="utf-8",
            )

            backup = backup_live_runtime(
                production,
                root / "开发版备份",
                backup_name="20260825-220000-123",
            )

            with sqlite3.connect(backup / "database.db") as connection:
                row = connection.execute("SELECT id, name FROM accounts").fetchone()
            self.assertEqual(row, (4, "测试账号"))
            self.assertEqual(
                json.loads((backup / "publish-profiles.json").read_text(encoding="utf-8")),
                {"schemaVersion": 1, "profiles": []},
            )
            self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
            self.assertEqual((backup / "database.db").stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                (backup / "publish-profiles.json").stat().st_mode & 0o777, 0o600
            )

    def test_source_session_writes_live_marker_then_removes_it_on_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            production = root / "正式数据"
            self._create_database(production)
            marker = production / "source-live-session.json"

            with source_live_session(
                production,
                root / "开发版备份",
                process_rows=[],
                marker_path=marker,
                backup_name="20260825-221000-555",
                current_pid=555,
            ) as session:
                self.assertTrue(marker.is_file())
                self.assertTrue(session.backup_dir.is_dir())
                self.assertTrue(
                    source_live_session_active(
                        marker, pid_is_alive=lambda pid: pid == 555
                    )
                )

            self.assertFalse(marker.exists())

    def test_conflicting_installed_task_stops_before_backup_or_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            production = root / "正式数据"
            self._create_database(production)
            backups = root / "开发版备份"
            marker = production / "source-live-session.json"

            with self.assertRaisesRegex(SourceLiveRuntimeError, "正式客户端"):
                with source_live_session(
                    production,
                    backups,
                    process_rows=[
                        "103 /Applications/一键发.app/Contents/MacOS/一键发 "
                        "--controlled-publish-action create"
                    ],
                    marker_path=marker,
                    backup_name="20260825-221100-556",
                    current_pid=556,
                ):
                    self.fail("冲突时不应进入源码联调会话")

            self.assertFalse(marker.exists())
            self.assertFalse(backups.exists())

    def test_installed_gui_is_blocked_by_live_marker_but_source_process_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = Path(temp_dir) / "source-live-session.json"
            marker.write_text('{"pid":777}', encoding="utf-8")

            installed_reason = installed_gui_block_reason(
                {}, marker, pid_is_alive=lambda pid: pid == 777
            )
            source_reason = installed_gui_block_reason(
                {"YIJIANFA_SOURCE_LIVE_DATA": "1"},
                marker,
                pid_is_alive=lambda pid: pid == 777,
            )

        self.assertIn("源码联调客户端", installed_reason)
        self.assertEqual(source_reason, "")

    def test_backup_keeps_only_five_latest_source_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            production = root / "正式数据"
            self._create_database(production)
            backups = root / "开发版备份"
            for index in range(1, 7):
                (backups / f"20260825-22000{index}-10{index}").mkdir(parents=True)

            newest = backup_live_runtime(
                production,
                backups,
                backup_name="20260825-230000-999",
                max_backups=5,
            )

            remaining = sorted(path.name for path in backups.iterdir() if path.is_dir())

        self.assertEqual(len(remaining), 5)
        self.assertIn(newest.name, remaining)
        self.assertNotIn("20260825-220001-101", remaining)
        self.assertNotIn("20260825-220002-102", remaining)


if __name__ == "__main__":
    unittest.main()
