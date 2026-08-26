# -*- coding: utf-8 -*-

from __future__ import annotations

import unittest

from app_core.controlled_publish import ControlledPublishError
from app_core.controlled_publish_process import (
    submit_douyin_graphic_matrix_request_in_process,
    submit_request_in_process,
)


class ControlledPublishProcessTests(unittest.TestCase):
    def test_process_adapter_preserves_cli_error_code_without_opening_desktop_ui(self) -> None:
        with self.assertRaises(ControlledPublishError) as raised:
            submit_request_in_process(
                {
                    "mode": "preflight",
                    "manifestPath": "",
                    "targets": [],
                },
                startup_timeout_seconds=10,
            )

        self.assertEqual(raised.exception.error_code, "controlled_manifest_required")
        self.assertIn("manifestPath", raised.exception.public_message)

    def test_matrix_process_adapter_uses_headless_cli_action(self) -> None:
        with self.assertRaises(ControlledPublishError) as raised:
            submit_douyin_graphic_matrix_request_in_process(
                {
                    "schemaVersion": "oneclick-douyin-graphic-matrix/v1",
                    "workflow": "douyin-graphic-matrix",
                    "runtimeMode": "local_check",
                    "content": {"images": [], "common": {}},
                    "targets": [],
                },
                startup_timeout_seconds=10,
            )

        self.assertEqual(raised.exception.error_code, "douyin_graphic_matrix_invalid")


if __name__ == "__main__":
    unittest.main()
