import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from app_core.wechat_draft_executor import (
    WechatDraftError,
    _open_draft_list,
    _readback_saved_draft,
    validate_wechat_draft_payload,
)


class _DraftListPage:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    async def evaluate(self, _script: str, _title: str) -> list[dict]:
        return list(self.rows)


class _ClickableNode:
    def __init__(self) -> None:
        self.clicked = False

    async def is_visible(self) -> bool:
        return True

    async def is_enabled(self) -> bool:
        return True

    async def click(self, **_kwargs) -> None:
        self.clicked = True


class _SingleNodeLocator:
    def __init__(self, node: _ClickableNode) -> None:
        self.node = node

    async def count(self) -> int:
        return 1

    def nth(self, _index: int) -> _ClickableNode:
        return self.node


class _DraftEntryPage:
    def __init__(self) -> None:
        self.url = "https://mp.weixin.qq.com/cgi-bin/appmsg?action=edit&type=10"
        self.node = _ClickableNode()
        self.waited = False

    def get_by_text(self, text: str, *, exact: bool):
        assert text == "草稿箱"
        assert exact is True
        return _SingleNodeLocator(self.node)

    async def wait_for_load_state(self, _state: str, **_kwargs) -> None:
        self.waited = True


class WechatDraftExecutorTests(unittest.TestCase):
    def _payload(self, root: Path) -> dict:
        cover = root / "cover.png"
        cover.write_bytes(b"png")
        return {
            "type": 10,
            "title": "测试标题",
            "contentHtml": "<p>测试正文</p>",
            "coverPath": str(cover),
            "runtimeMode": "wechat_draft",
            "debugDryRun": True,
            "wechatGroupNotification": False,
            "enableTimer": False,
            "scheduleTime": None,
            "originalDeclaration": True,
        }

    def test_draft_executor_rejects_publish_controls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload = self._payload(Path(directory))
            self.assertEqual(
                validate_wechat_draft_payload(payload)["runtimeMode"], "wechat_draft"
            )
            with self.assertRaisesRegex(WechatDraftError, "runtimeMode"):
                validate_wechat_draft_payload({**payload, "runtimeMode": "publish"})
            with self.assertRaisesRegex(WechatDraftError, "群发"):
                validate_wechat_draft_payload(
                    {**payload, "wechatGroupNotification": True}
                )
            with self.assertRaisesRegex(WechatDraftError, "定时"):
                validate_wechat_draft_payload(
                    {**payload, "enableTimer": True, "scheduleTime": "2099-01-01"}
                )

    def test_draft_readback_uses_same_title_and_current_time_window(self) -> None:
        started_at = datetime(2026, 8, 25, 9, 0, 0)
        page = _DraftListPage(
            [
                {
                    "title": "测试标题",
                    "text": "测试标题 昨天 08:00 草稿",
                    "savedAt": (started_at - timedelta(days=1)).isoformat(),
                    "hasCover": True,
                },
                {
                    "title": "测试标题",
                    "text": "测试标题 今天 09:01 草稿",
                    "savedAt": (started_at + timedelta(minutes=1)).isoformat(),
                    "hasCover": True,
                },
            ]
        )

        result = __import__("asyncio").run(
            _readback_saved_draft(page, "测试标题", started_at=started_at)
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["draftTitle"], "测试标题")

    def test_draft_readback_rejects_only_old_same_title_record(self) -> None:
        started_at = datetime(2026, 8, 25, 9, 0, 0)
        page = _DraftListPage(
            [
                {
                    "title": "测试标题",
                    "text": "测试标题 昨天 08:00 草稿",
                    "savedAt": (started_at - timedelta(days=1)).isoformat(),
                    "hasCover": True,
                }
            ]
        )

        result = __import__("asyncio").run(
            _readback_saved_draft(
                page,
                "测试标题",
                started_at=started_at,
                timeout_seconds=0,
            )
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["errorCode"], "draft_readback_missing")

    def test_draft_readback_opens_the_unique_draft_list_entry(self) -> None:
        page = _DraftEntryPage()

        __import__("asyncio").run(_open_draft_list(page))

        self.assertTrue(page.node.clicked)
        self.assertTrue(page.waited)
