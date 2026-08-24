# -*- coding: utf-8 -*-

import unittest
from unittest.mock import patch

from ui.publish_page import _preflight_action_copy


class PublishPreflightCopyTests(unittest.TestCase):
    def test_domestic_only_never_mentions_overseas(self) -> None:
        copy = _preflight_action_copy([{"type": 1}, {"type": 2}])
        self.assertTrue(copy["formalReady"])
        self.assertEqual(copy["buttonText"], "继续正式发布")
        self.assertNotIn("海外", copy["actionHint"])

    def test_supported_overseas_uses_overseas_copy(self) -> None:
        copy = _preflight_action_copy([{"type": 6}, {"type": 7}])
        self.assertTrue(copy["formalReady"])
        self.assertEqual(copy["buttonText"], "继续海外平台正式发布")
        self.assertIn("海外", copy["actionHint"])

    def test_unsupported_overseas_disables_formal_choice(self) -> None:
        with patch("ui.publish_page.account_service.OVERSEAS_PLATFORM_TYPES", {6, 7, 8, 9, 11}):
            copy = _preflight_action_copy([{"type": 1}, {"type": 11}])
        self.assertFalse(copy["formalReady"])
