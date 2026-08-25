# -*- coding: utf-8 -*-
"""一键发源码联调启动器的真实准备结果测试。"""

from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from tools.run_source_live import default_source_live_paths, prepare_source_launch


class SourceLiveLauncherTests(unittest.TestCase):
    def test_default_paths_keep_live_data_and_backups_in_separate_directories(self) -> None:
        home = Path("/Users/tester")

        paths = default_source_live_paths(home)

        self.assertEqual(
            paths.production_data_dir,
            Path("/Users/tester/Library/Application Support/一键发"),
        )
        self.assertEqual(
            paths.backup_root,
            Path("/Users/tester/Library/Application Support/一键发开发版/backups"),
        )
        self.assertEqual(
            paths.marker_path,
            Path("/Users/tester/Library/Application Support/一键发/source-live-session.json"),
        )

    def test_preparation_builds_source_command_live_environment_and_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "project"
            python = project / ".venv" / "bin" / "python"
            entrypoint = project / "desktop_native_app.py"
            python.parent.mkdir(parents=True)
            python.write_text("python", encoding="utf-8")
            entrypoint.write_text("entry", encoding="utf-8")
            production = root / "正式数据"
            database = production / "db" / "database.db"
            database.parent.mkdir(parents=True)
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE account (id INTEGER)")

            with prepare_source_launch(
                project,
                production_data_dir=production,
                backup_root=root / "backups",
                marker_path=production / "source-live-session.json",
                process_rows=[],
                backup_name="20260825-223000-888",
                current_pid=888,
                page="commerce",
                base_environment={"PATH": "/usr/bin"},
            ) as prepared:
                self.assertEqual(
                    prepared.command,
                    [
                        str(python),
                        "-u",
                        str(entrypoint),
                        "--page",
                        "commerce",
                    ],
                )
                self.assertEqual(
                    prepared.environment["YIJIANFA_USER_DATA_DIR"], str(production)
                )
                self.assertTrue(prepared.backup_dir.is_dir())
                self.assertTrue((production / "source-live-session.json").is_file())

            self.assertFalse((production / "source-live-session.json").exists())


if __name__ == "__main__":
    unittest.main()
