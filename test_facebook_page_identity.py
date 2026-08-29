# -*- coding: utf-8 -*-
"""Facebook Page V1 identity, feature-gate, and safe-receipt contracts."""

from __future__ import annotations

import unittest

from app_core.overseas_meta_errors import (
    FacebookPagePublishError,
    project_facebook_page_receipt,
)
from app_core.overseas_meta_page_identity import (
    FacebookPageIdentity,
    activate_saved_facebook_page,
    discover_manageable_facebook_pages,
    facebook_page_v1_enabled,
    normalize_facebook_page_id,
    resolve_facebook_page_selection,
    validate_facebook_page_binding,
)


class _FakePageRow:
    def __init__(self, page: "_FakePage", record: dict[str, object]) -> None:
        self._page = page
        self._record = record

    async def get_attribute(self, name: str):
        if name == "data-page-id":
            return self._record["page_id"]
        if name == "data-page-name":
            return self._record["page_name"]
        if name == "data-avatar-url":
            return self._record.get("avatar_url", "")
        if name == "data-can-manage-content":
            return "true" if self._record.get("can_manage_content") else "false"
        if name == "data-page-active":
            return "true" if self._page.active_page_id == self._record["page_id"] else "false"
        return None

    async def inner_text(self) -> str:
        return str(self._record["page_name"])

    async def click(self) -> None:
        self._page.clicked_page_ids.append(str(self._record["page_id"]))
        self._page.active_page_id = str(self._record["page_id"])


class _FakeLocator:
    def __init__(self, rows: list[_FakePageRow]) -> None:
        self._rows = rows

    async def count(self) -> int:
        return len(self._rows)

    def nth(self, index: int) -> _FakePageRow:
        return self._rows[index]


class _FakePage:
    def __init__(self, records: list[dict[str, object]], active_page_id: str = "") -> None:
        self.records = records
        self.active_page_id = active_page_id
        self.clicked_page_ids: list[str] = []

    def locator(self, selector: str) -> _FakeLocator:
        records = self.records
        if 'data-page-active="true"' in selector:
            records = [item for item in records if item["page_id"] == self.active_page_id]
        elif 'data-page-id="' in selector:
            expected = selector.split('data-page-id="', 1)[1].split('"', 1)[0]
            records = [item for item in records if item["page_id"] == expected]
        return _FakeLocator([_FakePageRow(self, item) for item in records])


class FacebookPageIdentityTests(unittest.IsolatedAsyncioTestCase):
    def test_zero_pages_is_not_found(self) -> None:
        with self.assertRaises(FacebookPagePublishError) as raised:
            resolve_facebook_page_selection(())
        self.assertEqual(raised.exception.error_code, "facebook_page_not_found")

    def test_one_manageable_page_is_auto_selected(self) -> None:
        page = FacebookPageIdentity("1001", "唯一主页", can_manage_content=True)
        self.assertIs(resolve_facebook_page_selection((page,)), page)

    def test_multiple_pages_require_an_explicit_page_id(self) -> None:
        pages = (
            FacebookPageIdentity("1001", "同名主页", can_manage_content=True),
            FacebookPageIdentity("1002", "同名主页", can_manage_content=True),
        )
        with self.assertRaises(FacebookPagePublishError) as raised:
            resolve_facebook_page_selection(pages)
        self.assertEqual(raised.exception.error_code, "facebook_page_selection_required")

    def test_same_name_never_substitutes_for_saved_page_id(self) -> None:
        pages = (
            FacebookPageIdentity("1001", "同名主页", can_manage_content=True),
            FacebookPageIdentity("1002", "同名主页", can_manage_content=True),
        )
        selected = resolve_facebook_page_selection(pages, "1002")
        self.assertEqual(selected.page_id, "1002")

    def test_unknown_selected_page_is_an_identity_mismatch(self) -> None:
        with self.assertRaises(FacebookPagePublishError) as raised:
            resolve_facebook_page_selection(
                (FacebookPageIdentity("1001", "主页", can_manage_content=True),),
                "1002",
            )
        self.assertEqual(raised.exception.error_code, "facebook_page_identity_mismatch")

    def test_duplicate_page_ids_are_one_page_record(self) -> None:
        selected = resolve_facebook_page_selection(
            (
                FacebookPageIdentity("1001", "主页", can_manage_content=True),
                FacebookPageIdentity("1001", "另一个显示名", can_manage_content=True),
            )
        )
        self.assertEqual(selected.page_id, "1001")

    def test_duplicate_page_id_with_conflicting_permissions_is_rejected_in_either_order(self) -> None:
        allowed = FacebookPageIdentity("1001", "主页", can_manage_content=True)
        denied = FacebookPageIdentity("1001", "主页", can_manage_content=False)
        for pages in ((allowed, denied), (denied, allowed)):
            with self.subTest(pages=pages):
                with self.assertRaises(FacebookPagePublishError) as raised:
                    resolve_facebook_page_selection(pages, "1001")
                self.assertEqual(raised.exception.error_code, "facebook_page_identity_mismatch")

    def test_blank_or_non_scalar_page_ids_are_rejected(self) -> None:
        for value in ("", "  ", [], {"page": "1001"}, True):
            with self.subTest(value=repr(value)), self.assertRaises(FacebookPagePublishError) as raised:
                normalize_facebook_page_id(value)
            self.assertEqual(raised.exception.error_code, "facebook_page_identity_mismatch")

    def test_page_without_content_permission_is_rejected(self) -> None:
        with self.assertRaises(FacebookPagePublishError) as raised:
            resolve_facebook_page_selection(
                (FacebookPageIdentity("1001", "主页", can_manage_content=False),),
                "1001",
            )
        self.assertEqual(raised.exception.error_code, "facebook_page_content_permission_missing")

    async def test_discovery_returns_only_page_fields(self) -> None:
        page = _FakePage(
            [
                {
                    "page_id": "1001",
                    "page_name": "主页",
                    "avatar_url": "https://example.test/avatar.png",
                    "can_manage_content": True,
                }
            ],
            active_page_id="1001",
        )
        discovered = await discover_manageable_facebook_pages(page)
        self.assertEqual(
            discovered,
            (FacebookPageIdentity("1001", "主页", "https://example.test/avatar.png", True),),
        )

    async def test_saved_page_mismatch_stops_before_any_switch(self) -> None:
        page = _FakePage(
            [{"page_id": "1002", "page_name": "同名主页", "can_manage_content": True}],
            active_page_id="1002",
        )
        with self.assertRaises(FacebookPagePublishError) as raised:
            await validate_facebook_page_binding(page, {"accountReference": "1001"})
        self.assertEqual(raised.exception.error_code, "facebook_page_identity_mismatch")
        self.assertEqual(page.clicked_page_ids, [])

    async def test_binding_rejects_when_saved_page_exists_but_another_page_is_active(self) -> None:
        page = _FakePage(
            [
                {"page_id": "1001", "page_name": "目标主页", "can_manage_content": True},
                {"page_id": "1002", "page_name": "当前主页", "can_manage_content": True},
            ],
            active_page_id="1002",
        )
        with self.assertRaises(FacebookPagePublishError) as raised:
            await validate_facebook_page_binding(page, {"accountReference": "1001"})
        self.assertEqual(raised.exception.error_code, "facebook_page_identity_mismatch")
        self.assertEqual(page.clicked_page_ids, [])

    async def test_permission_loss_is_stable_before_upload(self) -> None:
        page = _FakePage(
            [{"page_id": "1001", "page_name": "主页", "can_manage_content": False}],
            active_page_id="1001",
        )
        with self.assertRaises(FacebookPagePublishError) as raised:
            await validate_facebook_page_binding(page, {"accountReference": "1001"})
        self.assertEqual(raised.exception.error_code, "facebook_page_content_permission_missing")

    async def test_activation_clicks_only_the_exact_saved_page_and_reads_it_back(self) -> None:
        page = _FakePage(
            [
                {"page_id": "1001", "page_name": "同名主页", "can_manage_content": True},
                {"page_id": "1002", "page_name": "同名主页", "can_manage_content": True},
            ],
            active_page_id="1001",
        )
        selected = await activate_saved_facebook_page(page, "1002")
        self.assertEqual(selected.page_id, "1002")
        self.assertEqual(page.clicked_page_ids, ["1002"])
        self.assertEqual(page.active_page_id, "1002")

    def test_feature_gate_is_disabled_by_default_and_only_exact_one_enables_it(self) -> None:
        self.assertFalse(facebook_page_v1_enabled({}))
        self.assertFalse(facebook_page_v1_enabled({"ONECLICK_ENABLE_FACEBOOK_PAGE_V1": "true"}))
        self.assertFalse(facebook_page_v1_enabled({"ONECLICK_ENABLE_FACEBOOK_PAGE_V1": "1 "}))
        self.assertTrue(facebook_page_v1_enabled({"ONECLICK_ENABLE_FACEBOOK_PAGE_V1": "1"}))

    def test_request_or_manifest_data_cannot_override_the_process_local_gate(self) -> None:
        environ = {
            "ONECLICK_ENABLE_FACEBOOK_PAGE_V1": "0",
            "manifest": '{"ONECLICK_ENABLE_FACEBOOK_PAGE_V1":"1"}',
            "content": "ONECLICK_ENABLE_FACEBOOK_PAGE_V1=1",
        }
        self.assertFalse(facebook_page_v1_enabled(environ))


class FacebookPageReceiptTests(unittest.TestCase):
    def test_receipt_projection_drops_secret_and_body_fields(self) -> None:
        receipt = project_facebook_page_receipt(
            {
                "accountId": 7,
                "pageId": "1001",
                "pageName": "主页",
                "captionSha256": "a" * 64,
                "phase": "platform_form_verified",
                "Cookie": "not-retained",
                "token": "not-retained",
                "qrCode": "not-retained",
                "body": "not-retained",
                "storageState": "not-retained",
            }
        )
        self.assertEqual(
            receipt,
            {
                "accountId": 7,
                "pageId": "1001",
                "pageName": "主页",
                "captionSha256": "a" * 64,
                "phase": "platform_form_verified",
            },
        )

    def test_allowlisted_nested_or_invalid_value_raises(self) -> None:
        class _SpoofedPublicVisibility:
            def __eq__(self, other: object) -> bool:
                return other == "public"

        for receipt in (
            {"accountId": [7]},
            {"pageId": "not-a-page-id"},
            {"platformWriteOccurred": "true"},
            {"captionSha256": {"hash": "a" * 64}},
            {"visibility": _SpoofedPublicVisibility()},
        ):
            with self.subTest(receipt=receipt), self.assertRaises(ValueError):
                project_facebook_page_receipt(receipt)


if __name__ == "__main__":
    unittest.main()
