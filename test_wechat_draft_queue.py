import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from app_core.wechat_draft_queue import process_wechat_draft_inbox


class WechatDraftQueueTests(unittest.TestCase):
    def _package(self, root: Path, article_id: str = "WX-20260825-001") -> tuple[Path, str]:
        package = root / article_id
        assets = package / "assets"
        assets.mkdir(parents=True)
        files = {
            "content.html": b"<section><p>body</p><img src='assets/01.png'></section>",
            "cover-master.png": b"cover",
            "publication.json": json.dumps(
                {"article_id": article_id, "title": "唯一标题", "digest": "摘要"},
                ensure_ascii=False,
            ).encode("utf-8"),
            "assets/01.png": b"body-image",
        }
        records = {}
        for name, content in files.items():
            path = package / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            records[name] = {
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
        manifest = {
            "article_id": article_id,
            "title": "唯一标题",
            "state": "release_ready",
            "publish_allowed": False,
            "files": records,
        }
        manifest_path = package / "release-manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        return package, hashlib.sha256(manifest_path.read_bytes()).hexdigest()

    def _handoff(
        self,
        root: Path,
        *,
        account: str = "硅基进化",
    ) -> tuple[Path, str]:
        package, digest = self._package(root)
        inbox = root / "inbox"
        inbox.mkdir()
        (inbox / "WX-20260825-001.json").write_text(
            json.dumps(
                {
                    "schema_version": "silicon-evolution-wechat-draft-handoff/v1",
                    "article_id": "WX-20260825-001",
                    "package_path": str(package.resolve()),
                    "package_sha256": digest,
                    "target_account_name": account,
                    "intent": "wechat_draft_only",
                    "created_at": "2026-08-25T08:00:00+08:00",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return inbox, digest

    @staticmethod
    def _accounts() -> list[dict]:
        return [
            {
                "id": 6,
                "type": 10,
                "filePath": "wechat.json",
                "profileName": "硅基进化",
                "userName": "硅基进化",
                "status": 1,
            }
        ]

    def test_queue_is_inert_until_user_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox, _digest = self._handoff(root)
            called = []

            result = process_wechat_draft_inbox(
                inbox,
                root / "receipts",
                enabled=False,
                account_loader=lambda: called.append(True),
            )

            self.assertEqual(result, [])
            self.assertEqual(called, [])
            self.assertFalse((root / "receipts").exists())

    def test_queue_writes_stopped_receipt_without_running_for_bad_account(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox, digest = self._handoff(root, account="别的账号")
            called = []

            result = process_wechat_draft_inbox(
                inbox,
                root / "receipts",
                enabled=True,
                account_loader=self._accounts,
                task_runner=lambda _payload: called.append(True),
            )

            self.assertEqual(result[0].error_code, "account_mismatch")
            self.assertEqual(called, [])
            receipt = json.loads(
                (root / "receipts" / "WX-20260825-001.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(receipt["status"], "stopped")
            self.assertEqual(receipt["package_sha256"], digest)
            self.assertFalse(list((root / "receipts").glob("*.tmp")))

    def test_queue_runs_once_and_writes_matching_confirmed_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox, digest = self._handoff(root)
            payloads = []

            def run(payload: dict) -> dict:
                payloads.append(payload)
                return {
                    "ok": True,
                    "message": "公众号草稿已由草稿列表回读",
                    "errorCode": None,
                }

            first = process_wechat_draft_inbox(
                inbox,
                root / "receipts",
                enabled=True,
                account_loader=self._accounts,
                task_runner=run,
            )
            second = process_wechat_draft_inbox(
                inbox,
                root / "receipts",
                enabled=True,
                account_loader=self._accounts,
                task_runner=run,
            )

            self.assertEqual(first[0].status, "draft_readback_confirmed")
            self.assertEqual(second, [])
            self.assertEqual(len(payloads), 1)
            self.assertEqual(payloads[0]["runtimeMode"], "wechat_draft")
            self.assertTrue(payloads[0]["frozenWechatDraftHtml"])
            self.assertEqual(payloads[0]["accountIds"], [6])
            self.assertEqual(payloads[0]["packageSha256"], digest)
            self.assertEqual(
                payloads[0]["wechatArticleTemplate"],
                "silicon-evolution-tech-v1",
            )
            receipt = json.loads(
                (root / "receipts" / "WX-20260825-001.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(receipt["status"], "draft_readback_confirmed")
            self.assertEqual(receipt["error_code"], None)


if __name__ == "__main__":
    unittest.main()
