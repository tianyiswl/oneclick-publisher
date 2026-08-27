import unittest
import json
from pathlib import Path

from app_core.silicon_evolution_auto_publish import (
    AutoPublishProfile,
    SiliconEvolutionAutoPublishError,
    build_silicon_evolution_payload,
    require_matching_successful_preflight,
)
from app_core.silicon_evolution_publish_package import FrozenWechatPublishPackage


class SiliconEvolutionAutoPublishTests(unittest.TestCase):
    def setUp(self) -> None:
        self.package = FrozenWechatPublishPackage(
            article_id="WX-20260826-001",
            package_sha256="a" * 64,
            title="测试标题",
            digest="测试摘要",
            content_html="<p>正文</p>",
            cover_path=Path("/tmp/cover.png"),
            body_image_paths=(Path("/tmp/01.png"),),
            ai_disclosure={
                "containsAiGeneratedContent": True,
                "contentKinds": ["image"],
                "assetPaths": ["assets/01.png"],
                "allowPlatformAutoDeclaration": True,
            },
        )
        self.profile = AutoPublishProfile(
            project_id="silicon-evolution",
            account_id=11,
            account_display_name="硅基进化",
            enabled=True,
        )

    def test_auto_payload_disables_notification_and_timer(self) -> None:
        payload = build_silicon_evolution_payload(
            self.package, self.profile, account_id=11, mode="publish"
        )
        self.assertEqual(payload["type"], 10)
        self.assertEqual(payload["runtimeMode"], "publish")
        self.assertFalse(payload["wechatGroupNotification"])
        self.assertFalse(payload["enableTimer"])
        self.assertEqual(payload["siliconEvolutionPackageSha256"], "a" * 64)
        self.assertTrue(payload["frozenWechatDraftHtml"])
        self.assertEqual(payload["contentHtml"], "<p>正文</p>")
        self.assertEqual(payload["digest"], "测试摘要")

    def test_direct_payload_is_formal_but_hidden(self) -> None:
        payload = build_silicon_evolution_payload(
            self.package,
            self.profile,
            account_id=11,
            mode="direct",
        )
        self.assertEqual(payload["runtimeMode"], "publish")
        self.assertFalse(payload["debugDryRun"])
        self.assertTrue(payload["backgroundMode"])
        self.assertTrue(payload["aiDeclarationExplicitlyConfirmed"])

    def test_rejects_account_mismatch_or_disabled_profile(self) -> None:
        with self.assertRaisesRegex(SiliconEvolutionAutoPublishError, "账号"):
            build_silicon_evolution_payload(
                self.package, self.profile, account_id=12, mode="preflight"
            )
        disabled = AutoPublishProfile(
            project_id="silicon-evolution",
            account_id=11,
            account_display_name="硅基进化",
            enabled=False,
        )
        with self.assertRaisesRegex(SiliconEvolutionAutoPublishError, "未启用"):
            build_silicon_evolution_payload(
                self.package, disabled, account_id=11, mode="preflight"
            )

    def test_formal_requires_matching_successful_preflight(self) -> None:
        task = {
            "mode": "oneclick_preflight",
            "status": "success",
            "payloadJson": json.dumps(
                [{
                    "type": 10,
                    "accountIds": [11],
                    "siliconEvolutionArticleId": self.package.article_id,
                    "siliconEvolutionPackageSha256": self.package.package_sha256,
                }]
            ),
        }
        require_matching_successful_preflight(task, self.package, self.profile)
        task["payloadJson"] = json.dumps([{
            "type": 10,
            "accountIds": [11],
            "siliconEvolutionArticleId": self.package.article_id,
            "siliconEvolutionPackageSha256": "b" * 64,
        }])
        with self.assertRaisesRegex(SiliconEvolutionAutoPublishError, "预检"):
            require_matching_successful_preflight(task, self.package, self.profile)


if __name__ == "__main__":
    unittest.main()
