# -*- coding: utf-8 -*-
"""Instagram 稳定错误与非敏感任务回执的离线测试。"""

from __future__ import annotations

import unittest

from app_core import overseas_instagram_errors as errors


class InstagramReceiptTests(unittest.TestCase):
    def test_projection_keeps_public_result_and_drops_sensitive_fields(self) -> None:
        projected = errors.project_instagram_receipt(
            {
                "phase": "outcome_unknown",
                "accountId": 7,
                "instagramUserId": "17841400000000000",
                "mediaId": None,
                "url": None,
                "finalActionTriggered": True,
                "blocksReplay": True,
                "errorCode": "instagram_publish_outcome_unknown",
                "platformErrorText": "The result could not be confirmed",
                "Cookie": "secret",
                "accessToken": "secret",
                "rawHtml": "secret",
                "caption": "not persisted in receipt",
            }
        )

        self.assertEqual(
            projected,
            {
                "phase": "outcome_unknown",
                "accountId": 7,
                "instagramUserId": "17841400000000000",
                "mediaId": None,
                "url": None,
                "finalActionTriggered": True,
                "blocksReplay": True,
                "errorCode": "instagram_publish_outcome_unknown",
                "platformErrorText": "The result could not be confirmed",
            },
        )


if __name__ == "__main__":
    unittest.main()
