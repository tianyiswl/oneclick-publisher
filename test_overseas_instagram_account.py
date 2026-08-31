# -*- coding: utf-8 -*-
"""Instagram 账号绑定和重启识别的离线数据库测试。"""

from __future__ import annotations

import sqlite3
import unittest

from app_core import overseas_instagram_account as account_binding
from app_core.overseas_instagram_identity import InstagramIdentity


class InstagramAccountBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute(
            """
            CREATE TABLE user_info (
                id INTEGER PRIMARY KEY,
                type INTEGER NOT NULL,
                filePath TEXT NOT NULL,
                userName TEXT NOT NULL,
                status INTEGER NOT NULL DEFAULT 0,
                profileName TEXT,
                accountReference TEXT
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO user_info
                (id, type, filePath, userName, status, profileName, accountReference)
            VALUES (1, 8, 'instagram-session.json', 'Pending', 0, 'Pending', '')
            """
        )

    def tearDown(self) -> None:
        self.conn.close()

    def test_binding_persists_restart_identity_without_remote_secrets(self) -> None:
        identity = InstagramIdentity(
            user_id="17841400000000000",
            username="creator.one",
            display_name="Creator One",
            avatar_url="https://example.test/avatar.png?token=must-not-persist",
            account_type="creator",
            linked_page_id="1001",
            linked_page_name="Main Page",
            can_manage_content=True,
        )

        with self.conn:
            account_binding.save_instagram_account_binding(
                self.conn,
                account_id=1,
                identity=identity,
                observed_at="2026-08-31T15:00:00+08:00",
            )

        row = self.conn.execute(
            "SELECT * FROM user_info WHERE id = 1"
        ).fetchone()
        self.assertEqual(row["accountReference"], "17841400000000000")
        self.assertEqual(row["userName"], "creator.one")
        self.assertEqual(row["profileName"], "Creator One")
        self.assertEqual(row["status"], 1)
        restored = account_binding.load_instagram_account_binding(self.conn, 1)
        self.assertEqual(restored.user_id, "17841400000000000")
        self.assertEqual(restored.account_type, "creator")
        self.assertEqual(restored.linked_page_id, "1001")
        self.assertEqual(restored.avatar_url, "")
        dump = "\n".join(self.conn.iterdump())
        self.assertNotIn("must-not-persist", dump)


if __name__ == "__main__":
    unittest.main()
