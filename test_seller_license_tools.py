# -*- coding: utf-8 -*-
"""卖家端激活码管理器的离线契约测试。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app_core import offline_license
from app_core.branding import ACTIVATION_PRODUCT_ID


class SellerLicenseToolsTests(unittest.TestCase):
    def test_issuer_rejects_machine_codes_that_client_cannot_use(self) -> None:
        from seller_tools.license_crypto import issue_activation_code

        for invalid in ("", "A" * 31, "G" * 32, "A" * 33):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "32 位"):
                    issue_activation_code(invalid, key_path=Path("unused"))

    def test_issuer_code_is_accepted_by_current_client(self) -> None:
        from seller_tools.generate_license_keypair import generate_keypair
        from seller_tools.license_crypto import issue_activation_code

        modulus, private_exponent = generate_keypair(bits=1024)
        machine = "A" * 32
        with tempfile.TemporaryDirectory() as temp_dir:
            key_path = Path(temp_dir) / "license-private.json"
            key_path.write_text(
                json.dumps(
                    {
                        "n_hex": format(modulus, "x"),
                        "d_hex": format(private_exponent, "x"),
                        "e": 65537,
                    }
                ),
                encoding="utf-8",
            )
            code = issue_activation_code(
                machine,
                order_number="TEST-ORDER",
                product_id=ACTIVATION_PRODUCT_ID,
                key_path=key_path,
            )

        with patch.object(offline_license, "PUBLIC_KEY_N_HEX", format(modulus, "x")):
            verified = offline_license.verify_activation_code(
                code,
                expected_machine=machine,
            )

        self.assertTrue(verified["activated"])
        self.assertEqual(verified["licenseId"], "TEST-ORDER")
        self.assertEqual(verified["machineCode"], machine)

    def test_history_is_private_and_keeps_latest_500_records(self) -> None:
        from seller_tools.license_history import append_history, load_history

        with tempfile.TemporaryDirectory() as temp_dir:
            history_path = Path(temp_dir) / "issue-history.json"
            for index in range(505):
                append_history(
                    {"orderNumber": str(index), "code": f"code-{index}"},
                    path=history_path,
                )

            records = load_history(history_path)

            self.assertEqual(len(records), 500)
            self.assertEqual(records[0]["orderNumber"], "504")
            self.assertEqual(records[-1]["orderNumber"], "5")
            if os.name != "nt":
                self.assertEqual(history_path.stat().st_mode & 0o777, 0o600)

    def test_seller_manager_offscreen_self_test_exits_without_private_key(self) -> None:
        from seller_tools.license_issuer_gui import run_ui_self_test

        self.assertEqual(run_ui_self_test(), "SELLER_LICENSE_UI_OK")


if __name__ == "__main__":
    unittest.main()
