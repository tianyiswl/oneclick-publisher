# -*- coding: utf-8 -*-

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import unittest

from mcp import Client, StdioServerParameters

from app_core.oneclick_mcp_server import create_server


class _Gateway:
    def __init__(self):
        self.calls = []

    def list_accounts(self):
        return []

    def list_profiles(self):
        return []

    def save_profile(self, project_id, display_name, targets):
        return {"projectId": project_id, "displayName": display_name, "targets": targets}

    def preflight_content(
        self, project_id, manifest_path, schedules=None, settings=None
    ):
        self.calls.append(("preflight", schedules, settings))
        return {"taskId": 7, "phase": "preflight", "status": "pending", "platforms": []}

    def task_status(self, task_id):
        return {"taskId": task_id, "phase": "preflight", "status": "success", "platforms": []}

    def authorize_preflight(self, task_id):
        return {"preflightTaskId": task_id, "authorizationId": "grant", "singleUse": True}

    def formal_publish(
        self,
        project_id,
        manifest_path,
        *,
        confirmed_preflight_task_id,
        authorization_id,
        schedules=None,
        settings=None,
    ):
        self.calls.append(("formal", schedules, settings))
        return {"taskId": 8, "phase": "formal", "status": "pending", "platforms": []}

    def direct_publish_content(
        self, project_id, manifest_path, schedules=None, settings=None
    ):
        self.calls.append(("direct", schedules, settings))
        return {
            "taskId": 11,
            "phase": "formal",
            "status": "pending",
            "platforms": [],
        }

    def check_douyin_graphic_matrix(self, manifest_path, targets):
        return {"taskId": 61, "phase": "local_check", "status": "pending", "platforms": []}

    def authorize_douyin_graphic_matrix(self, task_id):
        return {"preflightTaskId": task_id, "authorizationId": "matrix-grant", "singleUse": True}

    def publish_douyin_graphic_matrix(
        self,
        manifest_path,
        targets,
        *,
        confirmed_check_task_id,
        authorization_id,
    ):
        return {"taskId": 62, "phase": "formal", "status": "pending", "platforms": []}

    def sync_project_metrics(self, project_id):
        return {"projectId": project_id, "accounts": []}

    def get_project_metrics(self, project_id, days=1):
        return {"projectId": project_id, "days": days, "accounts": [], "contents": []}

    def metrics_sync_status(self, project_id):
        return {"projectId": project_id, "accounts": []}

    def preflight_silicon_evolution_release(
        self, article_id, package_path, package_sha256
    ):
        return {
            "taskId": 9,
            "articleId": article_id,
            "packageSha256": package_sha256,
            "phase": "preflight",
            "status": "pending",
        }

    def auto_publish_silicon_evolution_release(
        self,
        article_id,
        package_path,
        package_sha256,
        *,
        confirmed_preflight_task_id=None,
    ):
        return {
            "taskId": 10,
            "articleId": article_id,
            "packageSha256": package_sha256,
            "phase": "formal",
            "status": "pending",
        }


class OneclickMcpServerTests(unittest.TestCase):
    def test_desktop_entrypoint_serves_mcp_over_stdio_without_opening_the_ui(self) -> None:
        async def inspect_entrypoint() -> tuple[set[str], str]:
            root = Path(__file__).resolve().parent
            parameters = StdioServerParameters(
                command=sys.executable,
                args=[str(root / "desktop_native_app.py"), "--mcp-server"],
                cwd=root,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
            )
            async with Client(parameters, read_timeout_seconds=10) as client:
                result = await client.list_tools()
                return {tool.name for tool in result.tools}, str(client.server_info.name)

        names, server_name = asyncio.run(inspect_entrypoint())

        self.assertIn("oneclick_preflight_content", names)
        self.assertIn("oneclick_formal_publish", names)
        self.assertEqual(server_name, "yijianfa-local")

    def test_mcp_contract_has_separate_preflight_and_formal_tools_without_sensitive_args(self) -> None:
        tools = asyncio.run(create_server(_Gateway()).list_tools())
        by_name = {tool.name: tool for tool in tools}

        self.assertEqual(
            set(by_name),
            {
                "oneclick_list_accounts",
                "oneclick_list_publish_profiles",
                "oneclick_save_publish_profile",
                "oneclick_preflight_content",
                "oneclick_task_status",
                "oneclick_authorize_preflight",
                "oneclick_formal_publish",
                "oneclick_direct_publish_content",
                "oneclick_sync_project_metrics",
                "oneclick_get_project_metrics",
                "oneclick_metrics_sync_status",
                "oneclick_preflight_silicon_evolution_release",
                "oneclick_auto_publish_silicon_evolution_release",
                "oneclick_check_douyin_graphic_matrix",
                "oneclick_authorize_douyin_graphic_matrix",
                "oneclick_publish_douyin_graphic_matrix",
            },
        )
        preflight_schema = by_name["oneclick_preflight_content"].input_schema
        formal_schema = by_name["oneclick_formal_publish"].input_schema
        self.assertNotIn("mode", preflight_schema.get("properties", {}))
        self.assertIn("confirmed_preflight_task_id", formal_schema["properties"])
        self.assertIn("authorization_id", formal_schema["properties"])
        direct_schema = by_name["oneclick_direct_publish_content"].input_schema
        self.assertIn("settings", preflight_schema["properties"])
        self.assertIn("settings", formal_schema["properties"])
        self.assertIn("settings", direct_schema["properties"])
        self.assertNotIn(
            "confirmed_preflight_task_id",
            direct_schema.get("properties", {}),
        )
        self.assertNotIn("authorization_id", direct_schema.get("properties", {}))
        all_schemas = json.dumps(
            {name: tool.input_schema for name, tool in by_name.items()},
            ensure_ascii=False,
        ).lower()
        for forbidden in (
            "cookie",
            "password",
            "verification_code",
            "captcha",
            "storage_state",
        ):
            self.assertNotIn(forbidden, all_schemas)

    def test_matrix_tools_keep_local_check_and_formal_publish_separate(self) -> None:
        server = create_server(_Gateway())
        checked = asyncio.run(
            server.call_tool(
                "oneclick_check_douyin_graphic_matrix",
                {
                    "manifest_path": "/content/manifest.json",
                    "targets": [{"accountId": 31}],
                },
            )
        )
        published = asyncio.run(
            server.call_tool(
                "oneclick_publish_douyin_graphic_matrix",
                {
                    "manifest_path": "/content/manifest.json",
                    "targets": [{"accountId": 31}],
                    "confirmed_check_task_id": 61,
                    "authorization_id": "matrix-grant",
                },
            )
        )

        self.assertEqual(checked.structured_content["task"]["phase"], "local_check")
        self.assertEqual(published.structured_content["task"]["phase"], "formal")

    def test_metrics_tools_return_stable_project_envelopes(self) -> None:
        server = create_server(_Gateway())
        synced = asyncio.run(
            server.call_tool(
                "oneclick_sync_project_metrics",
                {"project_id": "silicon-exploration"},
            )
        )
        queried = asyncio.run(
            server.call_tool(
                "oneclick_get_project_metrics",
                {"project_id": "silicon-exploration", "days": 7},
            )
        )
        status = asyncio.run(
            server.call_tool(
                "oneclick_metrics_sync_status",
                {"project_id": "silicon-exploration"},
            )
        )

        self.assertEqual(synced.structured_content["sync"]["projectId"], "silicon-exploration")
        self.assertEqual(queried.structured_content["metrics"]["days"], 7)
        self.assertEqual(status.structured_content["status"]["projectId"], "silicon-exploration")

    def test_silicon_evolution_preflight_tool_keeps_frozen_identity(self) -> None:
        server = create_server(_Gateway())
        result = asyncio.run(
            server.call_tool(
                "oneclick_preflight_silicon_evolution_release",
                {
                    "article_id": "WX-20260826-001",
                    "package_path": "/content/WX-20260826-001",
                    "package_sha256": "a" * 64,
                },
            )
        )
        self.assertTrue(result.structured_content["ok"])
        self.assertEqual(
            result.structured_content["task"]["articleId"],
            "WX-20260826-001",
        )

    def test_preflight_tool_returns_stable_json_envelope(self) -> None:
        server = create_server(_Gateway())
        result = asyncio.run(
            server.call_tool(
                "oneclick_preflight_content",
                {
                    "project_id": "silicon-exploration",
                    "manifest_path": "/content/manifest.json",
                },
            )
        )

        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["ok"], True)
        self.assertEqual(result.structured_content["task"]["taskId"], 7)
        self.assertEqual(result.structured_content["task"]["phase"], "preflight")

    def test_direct_publish_tool_uses_chat_authorization_without_preflight_args(self) -> None:
        gateway = _Gateway()
        server = create_server(gateway)
        result = asyncio.run(
            server.call_tool(
                "oneclick_direct_publish_content",
                {
                    "project_id": "silicon-exploration",
                    "manifest_path": "/content/manifest.json",
                    "settings": {
                        "YouTube": {
                            "visibility": "private",
                            "madeForKids": False,
                        }
                    },
                },
            )
        )

        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["task"]["taskId"], 11)
        self.assertEqual(result.structured_content["task"]["phase"], "formal")
        self.assertEqual(
            gateway.calls[0][2]["YouTube"]["visibility"],
            "private",
        )


if __name__ == "__main__":
    unittest.main()
