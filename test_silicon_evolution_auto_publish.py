import unittest
from pathlib import Path

from app_core.silicon_evolution_auto_publish import (
    AutoPublishProfile,
    SiliconEvolutionAutoPublishError,
    build_silicon_evolution_payload,
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


if __name__ == "__main__":
    unittest.main()
