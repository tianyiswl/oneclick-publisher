# -*- coding: utf-8 -*-
"""Tests for the TikTok-only browser storage-state boundary."""

from __future__ import annotations

import copy
import unittest

from app_core.overseas_tiktok_session_scope import (
    TikTokSessionScopeError,
    is_tiktok_cookie_domain,
    is_tiktok_https_origin,
    sanitize_tiktok_storage_state,
)


class TikTokSessionScopeTests(unittest.TestCase):
    def test_sanitize_keeps_only_tiktok_domains_and_origins(self):
        raw = {
            "cookies": [
                {"name": "sessionid", "value": "tt", "domain": ".tiktok.com", "path": "/"},
                {"name": "google", "value": "secret", "domain": ".google.com", "path": "/"},
                {"name": "lookalike", "value": "bad", "domain": "evil-tiktok.com", "path": "/"},
            ],
            "origins": [
                {"origin": "https://www.tiktok.com", "localStorage": [{"name": "tt", "value": "ok"}]},
                {"origin": "https://accounts.google.com", "localStorage": [{"name": "g", "value": "secret"}]},
            ],
        }
        original = copy.deepcopy(raw)

        result = sanitize_tiktok_storage_state(raw)

        self.assertEqual(set(result), {"cookies", "origins"})
        self.assertEqual([item["domain"] for item in result["cookies"]], [".tiktok.com"])
        self.assertEqual([item["origin"] for item in result["origins"]], ["https://www.tiktok.com"])
        self.assertEqual(raw, original)
        self.assertEqual(len(raw["cookies"]), 3)

    def test_empty_tiktok_cookie_set_is_not_a_login(self):
        with self.assertRaises(TikTokSessionScopeError) as raised:
            sanitize_tiktok_storage_state({"cookies": [], "origins": []})
        self.assertEqual(raised.exception.error_code, "tiktok_session_missing")

    def test_tiktok_visitor_cookies_without_an_auth_marker_are_not_a_login(self):
        raw = {
            "cookies": [
                {"name": "tt_webid", "value": "visitor", "domain": ".tiktok.com"}
            ],
            "origins": [],
        }

        with self.assertRaises(TikTokSessionScopeError) as raised:
            sanitize_tiktok_storage_state(raw)

        self.assertEqual(raised.exception.error_code, "tiktok_session_missing")
        self.assertNotIn("visitor", raised.exception.public_message)

    def test_cookie_domain_allowlist_accepts_tiktok_subdomains_only(self):
        accepted = ("www.tiktok.com", ".tiktok.com", "shop.tiktok.com", "SHOP.TIKTOK.COM.")
        rejected = (
            "tiktok.com.evil.test",
            "evil-tiktok.com",
            ".google.com",
            "",
            None,
        )

        for value in accepted:
            with self.subTest(value=value):
                self.assertTrue(is_tiktok_cookie_domain(value))
        for value in rejected:
            with self.subTest(value=value):
                self.assertFalse(is_tiktok_cookie_domain(value))

    def test_origin_allowlist_requires_tiktok_https_without_credentials_or_suffixes(self):
        accepted = (
            "https://www.tiktok.com",
            "https://tiktok.com/",
            "https://shop.tiktok.com:443",
            "https://SHOP.TIKTOK.COM./",
        )
        rejected = (
            "http://www.tiktok.com",
            "https://www.tiktok.com:444",
            "https://user:pass@www.tiktok.com",
            "https://www.tiktok.com?token=secret",
            "https://www.tiktok.com/#fragment",
            "https://www.tiktok.com/path",
            "https://tiktok.com.evil.test",
            "https://www.tiktok.com:bad",
            "https://accounts.google.com",
            None,
        )

        for value in accepted:
            with self.subTest(value=value):
                self.assertTrue(is_tiktok_https_origin(value))
        for value in rejected:
            with self.subTest(value=value):
                self.assertFalse(is_tiktok_https_origin(value))

    def test_sanitize_rejects_malformed_storage_state_structure(self):
        malformed_values = (
            None,
            [],
            {},
            {"cookies": None, "origins": []},
            {"cookies": [], "origins": None},
            {"cookies": ["not-a-cookie"], "origins": []},
            {"cookies": [], "origins": ["not-an-origin"]},
            {"cookies": [{"name": "missing-domain"}], "origins": []},
            {"cookies": [{"domain": ".tiktok.com"}], "origins": [{"name": "missing-origin"}]},
            {"cookies": [], "origins": [], "extra": "forbidden"},
        )

        for raw in malformed_values:
            with self.subTest(raw=raw):
                with self.assertRaises(TikTokSessionScopeError) as raised:
                    sanitize_tiktok_storage_state(raw)
                self.assertEqual(raised.exception.error_code, "tiktok_session_scope_invalid")
                self.assertNotIn("secret", str(raised.exception))

    def test_sanitize_deep_copies_retained_cookie_and_origin_entries(self):
        raw = {
            "cookies": [
                {
                    "name": "sessionid",
                    "value": "secret-cookie-value",
                    "domain": ".tiktok.com",
                    "path": "/",
                    "meta": {"nested": ["value"]},
                }
            ],
            "origins": [
                {
                    "origin": "https://www.tiktok.com",
                    "localStorage": [{"name": "session", "value": "secret-storage-value"}],
                }
            ],
        }

        result = sanitize_tiktok_storage_state(raw)
        result["cookies"][0]["meta"]["nested"].append("changed")
        result["origins"][0]["localStorage"][0]["value"] = "changed"

        self.assertEqual(raw["cookies"][0]["meta"]["nested"], ["value"])
        self.assertEqual(raw["origins"][0]["localStorage"][0]["value"], "secret-storage-value")


if __name__ == "__main__":
    unittest.main()
