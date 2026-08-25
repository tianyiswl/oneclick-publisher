# -*- coding: utf-8 -*-

from __future__ import annotations

import unittest

from app_core.controlled_publish import ControlledPublishError
from app_core.controlled_publish_process import submit_request_in_process


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


if __name__ == "__main__":
    unittest.main()
