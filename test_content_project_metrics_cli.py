# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent


class ContentProjectMetricsCliTests(unittest.TestCase):
    def _runtime(self, root: Path) -> dict[str, str]:
        environment = {
            **os.environ,
            "YIJIANFA_USER_DATA_DIR": str(root),
            "QT_QPA_PLATFORM": "offscreen",
        }
        bootstrap = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from app_core import database\n"
                    "from app_core.content_project_gateway import PublishProfileStore\n"
                    "database.ensure_schema()\n"
                    "with database.connect() as conn:\n"
                    "    conn.execute(\"INSERT INTO user_info "
                    "(id,type,filePath,userName,status,profileName) "
                    "VALUES (31,3,'douyin.json','测试主体',1,'测试主体')\")\n"
                    "PublishProfileStore().save({"
                    "'schemaVersion':1,'projectId':'silicon-exploration',"
                    "'displayName':'硅基探索','targets':[{'platform':'抖音','accountId':31}]})"
                ),
            ],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
        )
        self.assertEqual(bootstrap.returncode, 0, bootstrap.stderr)
        return environment

    def test_metrics_get_cli_returns_one_json_document_without_ui(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = self._runtime(Path(directory))
            completed = subprocess.run(
                [
                    sys.executable,
                    "desktop_native_app.py",
                    "--controlled-publish-action",
                    "metrics-get",
                    "--content-project-id",
                    "silicon-exploration",
                    "--metrics-days",
                    "7",
                ],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(len(completed.stdout.strip().splitlines()), 1)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["projectId"], "silicon-exploration")
        self.assertEqual(payload["days"], 7)
        self.assertEqual(payload["accounts"][0]["scope"], "account")

    def test_metrics_status_cli_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = self._runtime(Path(directory))
            completed = subprocess.run(
                [
                    sys.executable,
                    "desktop_native_app.py",
                    "--controlled-publish-action",
                    "metrics-status",
                    "--content-project-id",
                    "silicon-exploration",
                ],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["accounts"][0]["action"], "available")
        self.assertEqual(payload["accounts"][0]["status"], "never_synced")


if __name__ == "__main__":
    unittest.main()
