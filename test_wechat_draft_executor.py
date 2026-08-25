import tempfile
import unittest
from pathlib import Path

from app_core.wechat_draft_executor import (
    WechatDraftError,
    validate_wechat_draft_payload,
)


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
