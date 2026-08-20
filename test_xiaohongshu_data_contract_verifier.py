import io
import json
import unittest
from unittest.mock import patch

import tools.verify_xiaohongshu_data_contract as verifier


class XiaohongshuDataContractVerifierTests(unittest.TestCase):
    def test_execute_requires_exactly_one_normal_xiaohongshu_account(self):
        cases = (
            [],
            [{"id": 1, "type": 1, "status": 0}],
            [{"id": 1, "type": 1, "status": 1},
             {"id": 2, "type": 1, "status": 1}],
            [{"id": 1, "type": True, "status": 1}],
        )
        for accounts in cases:
            with self.subTest(accounts=accounts), patch(
                "app_core.account_service.list_accounts", return_value=accounts
            ):
                with self.assertRaisesRegex(
                    verifier.ProbeFailure,
                    "^xiaohongshu_account_selection_required$",
                ):
                    verifier._select_single_eligible_account()

    def test_execute_accepts_only_platform_type_one_and_builtin_ids(self):
        account = {"id": 7, "type": 1, "status": 1, "filePath": "state.json"}
        with patch("app_core.account_service.list_accounts", return_value=[account]):
            selected = verifier._select_single_eligible_account()
        self.assertEqual(selected, account)

    def test_default_mode_is_zero_action_plan(self):
        with patch("tools.verify_xiaohongshu_data_contract._execute") as execute:
            output = io.StringIO()
            code = verifier.main([], stdout=output)
        self.assertEqual(code, 0)
        execute.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["mode"], "plan")
        self.assertEqual(payload["phases"], [
            "account_overview", "content_list", "content_lifetime"
        ])

    def test_report_sanitizer_drops_values_and_sensitive_keys(self):
        report = verifier.sanitize_probe_report({
            "responses": [{
                "url": "https://creator.xiaohongshu.com/api/data?token=secret",
                "headers": {"Cookie": "secret"},
                "keys": ["data", "note_id", "title"],
                "sample": {"title": "private work"},
            }]
        })
        encoded = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("secret", encoded)
        self.assertNotIn("private work", encoded)
        self.assertEqual(report["responses"][0]["path"], "/api/data")

    def test_report_sanitizer_uses_safe_default_for_malformed_builtin_url(self):
        report = verifier.sanitize_probe_report({
            "responses": [{"url": "https://["}],
        })
        self.assertEqual(report["responses"][0]["path"], "")

    def test_report_sanitizer_caps_structural_text_and_rejects_subclasses(self):
        class UntrustedText(str):
            def __str__(self):
                raise AssertionError("sanitizer must not stringify untrusted data")

        report = verifier.sanitize_probe_report({
            "mode": UntrustedText("execute"),
            "platformType": True,
            "responses": [{
                "url": "https://creator.xiaohongshu.com/" + "a" * 130,
                "status": True,
                "keyPaths": ["a" * 130] * 301,
            }] * 101,
        })
        self.assertEqual(report["mode"], "plan")
        self.assertEqual(report["platformType"], 1)
        self.assertEqual(len(report["responses"]), 100)
        self.assertEqual(report["responses"][0]["status"], 0)
        self.assertEqual(len(report["responses"][0]["keyPaths"]), 300)
        self.assertEqual(len(report["responses"][0]["keyPaths"][0]), 120)

    def test_report_sanitizer_rebuilds_builtin_containers_with_safe_defaults(self):
        report = verifier.sanitize_probe_report({
            "responses": "not-a-list",
            "cleanup": {"closed": "yes", "aliveResourceCount": True},
        })
        self.assertEqual(report["responses"], [])
        self.assertEqual(report["cleanup"], {"closed": True, "aliveResourceCount": 0})
        self.assertIs(type(report), dict)
        self.assertIs(type(report["responses"]), list)
        self.assertIs(type(report["cleanup"]), dict)
