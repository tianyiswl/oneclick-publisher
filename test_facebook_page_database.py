# -*- coding: utf-8 -*-
"""Facebook Page legacy-row migration and persistence contracts."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app_core import account_service, database
from app_core.overseas_meta_errors import FacebookPagePublishError
from app_core.overseas_meta_page_identity import FacebookPageIdentity


class FacebookPageDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database_path = self.root / "database.db"
        self.cookie_dir = self.root / "cookies"
        self.avatar_dir = self.root / "avatars"
        self.cookie_dir.mkdir()
        self.avatar_dir.mkdir()
        self.database_patch = patch.object(database, "DB_PATH", self.database_path)
        self.cookie_patch = patch.object(account_service, "COOKIE_DIR", self.cookie_dir)
        self.avatar_patch = patch.object(account_service, "AVATAR_DIR", self.avatar_dir)
        self.database_patch.start()
        self.cookie_patch.start()
        self.avatar_patch.start()
        with sqlite3.connect(self.database_path) as conn:
            conn.execute(
                """
                CREATE TABLE user_info (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type INTEGER NOT NULL,
                    filePath TEXT NOT NULL,
                    userName TEXT NOT NULL,
                    status INTEGER DEFAULT 0,
                    profileName TEXT,
                    avatarPath TEXT,
                    avatarUpdatedAt TEXT,
                    remark TEXT,
                    lastCheckedAt TEXT,
                    lastLoginAt TEXT,
                    authMode TEXT NOT NULL DEFAULT 'browser',
                    accountReference TEXT
                )
                """
            )

    def tearDown(self) -> None:
        self.avatar_patch.stop()
        self.cookie_patch.stop()
        self.database_patch.stop()
        self.temp.cleanup()

    def insert_account(
        self,
        *,
        type: int,
        status: int,
        account_reference: str | None,
        file_path: str = "shared-meta.json",
        avatar_path: str | None = "shared-meta.png",
        name: str = "Legacy Page",
        remark: str = "keep this remark",
    ) -> int:
        with sqlite3.connect(self.database_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO user_info
                    (type, filePath, userName, status, profileName, avatarPath,
                     remark, authMode, accountReference)
                VALUES (?, ?, ?, ?, 'Meta 主体', ?, ?, 'browser', ?)
                """,
                (
                    type,
                    file_path,
                    name,
                    status,
                    avatar_path,
                    remark,
                    account_reference,
                ),
            )
            return int(cursor.lastrowid)

    def read_account(self, account_id: int) -> sqlite3.Row:
        with sqlite3.connect(self.database_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM user_info WHERE id = ?", (account_id,)
            ).fetchone()
        self.assertIsNotNone(row)
        return row

    def all_accounts(self) -> list[sqlite3.Row]:
        with sqlite3.connect(self.database_path) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute("SELECT * FROM user_info ORDER BY id").fetchall()

    def publishable_ids(self) -> list[int]:
        return [int(row["id"]) for row in account_service.list_publishable_accounts()]

    @staticmethod
    def identity(page_id: str, page_name: str = "Page") -> FacebookPageIdentity:
        return FacebookPageIdentity(
            page_id=page_id,
            page_name=page_name,
            can_manage_content=True,
        )

    def test_legacy_blank_page_reference_is_preserved_but_not_publishable(self):
        account_id = self.insert_account(type=9, status=1, account_reference="")

        database.ensure_schema()

        row = self.read_account(account_id)
        self.assertEqual(row["status"], 0)
        self.assertEqual(row["accountReference"], "")
        self.assertEqual(row["filePath"], "shared-meta.json")
        self.assertEqual(row["avatarPath"], "shared-meta.png")
        self.assertEqual(row["remark"], "keep this remark")
        self.assertNotIn(account_id, self.publishable_ids())

    def test_duplicate_page_reference_keeps_lowest_id_and_requires_rebind_for_later_rows(self):
        first = self.insert_account(type=9, status=1, account_reference=" 1001 ")
        second = self.insert_account(type=9, status=1, account_reference="1001")

        database.ensure_schema()

        self.assertEqual(self.read_account(first)["accountReference"], "1001")
        self.assertEqual(self.read_account(second)["accountReference"], "")
        self.assertEqual(self.read_account(second)["status"], 0)
        self.assertEqual(self.read_account(second)["filePath"], "shared-meta.json")
        self.assertEqual(self.read_account(second)["avatarPath"], "shared-meta.png")

    def test_migration_uses_task1_page_id_normalization_for_all_legacy_values(self):
        canonical = self.insert_account(
            type=9, status=1, account_reference="\t1001\t"
        )
        duplicate = self.insert_account(type=9, status=1, account_reference="1001")
        invalid_full_width = self.insert_account(
            type=9, status=1, account_reference="１００２"
        )
        invalid_name = self.insert_account(
            type=9, status=1, account_reference="legacy-name"
        )
        invalid_whitespace = self.insert_account(
            type=9, status=1, account_reference="\t\n"
        )

        database.ensure_schema()

        self.assertEqual(self.read_account(canonical)["accountReference"], "1001")
        for account_id in (
            duplicate,
            invalid_full_width,
            invalid_name,
            invalid_whitespace,
        ):
            with self.subTest(account_id=account_id):
                row = self.read_account(account_id)
                self.assertEqual(row["accountReference"], "")
                self.assertEqual(row["status"], 0)
                self.assertNotIn(account_id, self.publishable_ids())

    def test_upsert_normalizes_tab_wrapped_legacy_page_before_matching(self):
        database.ensure_schema()
        legacy_id = self.insert_account(
            type=9, status=1, account_reference="\t1001\t"
        )

        saved_id = account_service.save_facebook_page_browser_account(
            profile_name="Meta 主体",
            storage_file_name="updated.json",
            identity=self.identity("1001", "Updated Page"),
        )

        self.assertEqual(saved_id, legacy_id)
        self.assertEqual(len(self.all_accounts()), 1)
        self.assertEqual(self.read_account(legacy_id)["accountReference"], "1001")

    def test_migration_is_idempotent(self):
        self.insert_account(type=9, status=1, account_reference=" 1001 ")
        self.insert_account(type=9, status=1, account_reference="1001")
        self.insert_account(type=9, status=1, account_reference=None)
        database.ensure_schema()
        first_pass = [dict(row) for row in self.all_accounts()]

        database.ensure_schema()

        self.assertEqual([dict(row) for row in self.all_accounts()], first_pass)

    def test_database_rejects_second_type_9_row_for_same_non_empty_page_id(self):
        database.ensure_schema()
        self.insert_account(type=9, status=1, account_reference="1001")

        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_account(type=9, status=1, account_reference="1001")

    def test_another_platform_may_use_the_same_reference(self):
        database.ensure_schema()
        facebook_id = self.insert_account(type=9, status=1, account_reference="1001")
        instagram_id = self.insert_account(type=8, status=1, account_reference="1001")

        self.assertEqual(self.read_account(facebook_id)["accountReference"], "1001")
        self.assertEqual(self.read_account(instagram_id)["accountReference"], "1001")

    def test_saved_page_account_validation_returns_only_a_normalized_page_id(self):
        database.ensure_schema()
        account_id = self.insert_account(type=9, status=1, account_reference=" 1001 ")
        account = dict(self.read_account(account_id))

        self.assertEqual(
            account_service.validate_saved_facebook_page_account(account), "1001"
        )
        for invalid in (
            {**account, "type": 8},
            {**account, "accountReference": ""},
            {**account, "status": 0},
            {**account, "filePath": ""},
            {**account, "authMode": "youtube_oauth"},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(FacebookPagePublishError) as raised:
                    account_service.validate_saved_facebook_page_account(invalid)
                self.assertEqual(
                    raised.exception.error_code, "facebook_page_identity_mismatch"
                )

    def test_repeated_save_of_same_page_updates_one_row(self):
        database.ensure_schema()
        account_id = account_service.save_facebook_page_browser_account(
            profile_name="Meta 主体",
            storage_file_name="first.json",
            identity=self.identity("1001", "Original Page"),
            avatar_file_name="first.png",
        )

        repeated_id = account_service.save_facebook_page_browser_account(
            profile_name="更新主体",
            storage_file_name="second.json",
            identity=self.identity("1001", "Renamed Page"),
            avatar_file_name="second.png",
        )

        self.assertEqual(repeated_id, account_id)
        rows = self.all_accounts()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["type"], 9)
        self.assertEqual(rows[0]["filePath"], "second.json")
        self.assertEqual(rows[0]["userName"], "Renamed Page")
        self.assertEqual(rows[0]["profileName"], "更新主体")
        self.assertEqual(rows[0]["avatarPath"], "second.png")
        self.assertEqual(rows[0]["accountReference"], "1001")

    def test_bound_record_refuses_a_different_page_and_rolls_back(self):
        database.ensure_schema()
        account_id = account_service.save_facebook_page_browser_account(
            profile_name="Meta 主体",
            storage_file_name="first.json",
            identity=self.identity("1001"),
        )
        expected = dict(self.read_account(account_id))

        with self.assertRaises(FacebookPagePublishError) as raised:
            account_service.save_facebook_page_browser_account(
                profile_name="Changed",
                storage_file_name="second.json",
                identity=self.identity("2002"),
                record_id=account_id,
                expected_account=expected,
            )

        self.assertEqual(raised.exception.error_code, "facebook_page_identity_mismatch")
        row = self.read_account(account_id)
        self.assertEqual(row["accountReference"], "1001")
        self.assertEqual(row["filePath"], "first.json")
        self.assertEqual(row["profileName"], "Meta 主体")

    def test_legacy_blank_record_binds_once(self):
        legacy_id = self.insert_account(type=9, status=1, account_reference="")
        database.ensure_schema()
        expected = dict(self.read_account(legacy_id))

        bound_id = account_service.save_facebook_page_browser_account(
            profile_name="Meta 主体",
            storage_file_name="bound.json",
            identity=self.identity("1001"),
            record_id=legacy_id,
            expected_account=expected,
        )

        self.assertEqual(bound_id, legacy_id)
        self.assertEqual(self.read_account(legacy_id)["accountReference"], "1001")
        with self.assertRaises(FacebookPagePublishError) as raised:
            account_service.save_facebook_page_browser_account(
                profile_name="Meta 主体",
                storage_file_name="other.json",
                identity=self.identity("2002"),
                record_id=legacy_id,
                expected_account=dict(self.read_account(legacy_id)),
            )
        self.assertEqual(raised.exception.error_code, "facebook_page_identity_mismatch")
        self.assertEqual(self.read_account(legacy_id)["accountReference"], "1001")

    def test_shared_session_and_avatar_are_deleted_only_after_last_reference(self):
        database.ensure_schema()
        session = self.cookie_dir / "shared.json"
        avatar = self.avatar_dir / "shared.png"
        session.write_text("session", encoding="utf-8")
        avatar.write_bytes(b"avatar")
        first = account_service.save_facebook_page_browser_account(
            profile_name="Meta 主体",
            storage_file_name="shared.json",
            identity=self.identity("1001"),
            avatar_file_name="shared.png",
        )
        second = account_service.save_facebook_page_browser_account(
            profile_name="Meta 主体",
            storage_file_name="shared.json",
            identity=self.identity("2002"),
            avatar_file_name="shared.png",
        )

        account_service.delete_account(first)
        self.assertTrue(session.exists())
        self.assertTrue(avatar.exists())

        account_service.delete_account(second)
        self.assertFalse(session.exists())
        self.assertFalse(avatar.exists())

    def test_page_save_never_inserts_an_instagram_row(self):
        database.ensure_schema()

        account_service.save_facebook_page_browser_account(
            profile_name="Meta 主体",
            storage_file_name="facebook.json",
            identity=self.identity("1001"),
        )

        rows = self.all_accounts()
        self.assertEqual([row["type"] for row in rows], [9])

    def test_page_publishability_requires_status_page_id_and_session_reference(self):
        database.ensure_schema()
        healthy = self.insert_account(
            type=9,
            status=1,
            account_reference="1001",
            file_path="healthy.json",
        )
        missing_session = self.insert_account(
            type=9,
            status=1,
            account_reference="1002",
            file_path="",
        )
        inactive = self.insert_account(
            type=9,
            status=0,
            account_reference="1003",
            file_path="inactive.json",
        )
        blank_page = self.insert_account(
            type=9,
            status=1,
            account_reference="",
            file_path="blank.json",
        )

        publishable = self.publishable_ids()

        self.assertIn(healthy, publishable)
        self.assertNotIn(missing_session, publishable)
        self.assertNotIn(inactive, publishable)
        self.assertNotIn(blank_page, publishable)


if __name__ == "__main__":
    unittest.main()
