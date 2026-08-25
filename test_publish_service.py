import tempfile
import unittest
from pathlib import Path

from app_core.publish_service import _validate_payloads


class PublishServiceWechatDraftTests(unittest.TestCase):
    def _payload(self, root: Path) -> dict:
        cover = root / "cover.png"
        cover.write_bytes(b"png")
        return {
            "type": 10,
            "contentType": "text",
            "title": "测试标题",
            "description": "测试摘要",
            "contentHtml": "<p>测试正文</p>",
            "coverPath": str(cover),
            "fileList": [],
            "runtimeMode": "wechat_draft",
            "debugDryRun": True,
            "wechatGroupNotification": False,
            "enableTimer": False,
            "scheduleTime": None,
            "originalDeclaration": True,
        }

    def test_wechat_draft_task_requires_exactly_one_account(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload = self._payload(Path(directory))

            with self.assertRaisesRegex(ValueError, "一个公众号账号"):
                _validate_payloads([payload, dict(payload)])


if __name__ == "__main__":
    unittest.main()
