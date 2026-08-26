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
    def list_accounts(self):
        return []

    def list_profiles(self):
        return []

    def save_profile(self, project_id, display_name, targets):
        return {"projectId": project_id, "displayName": display_name, "targets": targets}

    def preflight_content(self, project_id, manifest_path, schedules=None):
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
    ):
        return {"taskId": 8, "phase": "formal", "status": "pending", "platforms": []}

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
        confirmed_preflight_task_id,
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
                "oneclick_preflight_silicon_evolution_release",
                "oneclick_auto_publish_silicon_evolution_release",
            },
        )
        preflight_schema = by_name["oneclick_preflight_content"].input_schema
        formal_schema = by_name["oneclick_formal_publish"].input_schema
        self.assertNotIn("mode", preflight_schema.get("properties", {}))
        self.assertIn("confirmed_preflight_task_id", formal_schema["properties"])
        self.assertIn("authorization_id", formal_schema["properties"])
        all_schemas = json.dumps(
            {name: tool.input_schema for name, tool in by_name.items()},
            ensure_ascii=False,
        ).lower()
        for forbidden in ("cookie", "password", "verification", "captcha"):
            self.assertNotIn(forbidden, all_schemas)

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


if __name__ == "__main__":
    unittest.main()
