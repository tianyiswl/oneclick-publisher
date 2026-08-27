# -*- coding: utf-8 -*-
"""统一内容包的离线安全校验测试。"""

import json
import tempfile
import unittest
from pathlib import Path

from app_core.content_bundle import ContentBundleError, load_content_bundle


class ContentBundleTests(unittest.TestCase):
    def _write_bundle(self, root: Path, content_type: str = "text") -> Path:
        (root / "封面_竖.png").write_bytes(b"png")
        (root / "正文.md").write_text("这是正文。", encoding="utf-8")
        data = {
            "schemaVersion": "oneclick-content/v1",
            "contentType": content_type,
            "title": "测试标题",
            "bodyFile": "正文.md",
            "covers": {"3:4": "封面_竖.png"},
            "preferredPlatforms": ["小红书", "公众号"],
            "debugDryRun": True,
            "publishAllowed": False,
        }
        if content_type == "article":
            (root / "01.png").write_bytes(b"png")
            data["assets"] = ["01.png"]
            data["platformOverrides"] = {"小红书": {"tags": ["测试话题"]}}
        manifest = root / "manifest.json"
        manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return manifest

    def test_loads_text_bundle_and_normalizes_platform_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bundle = load_content_bundle(self._write_bundle(Path(temporary)))
        self.assertEqual(bundle["contentType"], "text")
        self.assertEqual(bundle["preferredPlatforms"], ["小红书", "微信公众号"])
        self.assertEqual(bundle["assetPaths"], [])
        self.assertFalse(bundle["aiDisclosure"]["allowPlatformAutoDeclaration"])
        self.assertFalse(bundle["originalDeclaration"])

    def test_original_declaration_must_be_explicit_boolean(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._write_bundle(root, "article")
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["originalDeclaration"] = True
            manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            self.assertTrue(load_content_bundle(manifest)["originalDeclaration"])

            data["originalDeclaration"] = "true"
            manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ContentBundleError, "originalDeclaration"):
                load_content_bundle(manifest)

    def test_loads_article_bundle_with_platform_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bundle = load_content_bundle(self._write_bundle(Path(temporary), "article"))
        self.assertEqual(len(bundle["assetPaths"]), 1)
        self.assertEqual(bundle["articleImages"][0]["placement"], "after_intro")
        self.assertEqual(bundle["platformOverrides"]["小红书"]["tags"], ["测试话题"])

    def test_loads_explicit_silicon_evolution_wechat_template(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._write_bundle(root, "article")
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["wechatArticleTemplate"] = "silicon-evolution-tech-v1"
            manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            bundle = load_content_bundle(manifest)
        self.assertEqual(
            bundle["wechatArticleTemplate"],
            "silicon-evolution-tech-v1",
        )

    def test_rejects_unknown_wechat_article_template(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._write_bundle(root)
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["wechatArticleTemplate"] = "unknown-theme"
            manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ContentBundleError, "wechatArticleTemplate 不支持"):
                load_content_bundle(manifest)

    def test_article_images_support_placement_and_keep_cover_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._write_bundle(root, "article")
            (root / "02.png").write_bytes(b"png")
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["assets"] = [
                {"path": "01.png", "placement": "after_intro"},
                {
                    "path": "02.png",
                    "placement": "after_heading",
                    "anchor": "关键结论",
                },
            ]
            manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            bundle = load_content_bundle(manifest)
        self.assertEqual(
            [item["placement"] for item in bundle["articleImages"]],
            ["after_intro", "after_heading"],
        )
        self.assertEqual(bundle["articleImages"][1]["anchor"], "关键结论")
        self.assertNotIn(bundle["coverPaths"]["3:4"], bundle["assetPaths"])

    def test_legacy_multi_image_bundle_distributes_images_instead_of_appending(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._write_bundle(root, "article")
            (root / "02.png").write_bytes(b"png")
            (root / "03.png").write_bytes(b"png")
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["assets"] = ["01.png", "02.png", "03.png"]
            manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            bundle = load_content_bundle(manifest)
        self.assertEqual(
            [item["placement"] for item in bundle["articleImages"]],
            ["after_intro", "auto_distribute", "auto_distribute"],
        )

    def test_after_heading_requires_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._write_bundle(root, "article")
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["assets"] = [{"path": "01.png", "placement": "after_heading"}]
            manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ContentBundleError, "必须同时提供 anchor"):
                load_content_bundle(manifest)

    def test_rejects_publish_enabled_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._write_bundle(root)
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["publishAllowed"] = True
            manifest.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ContentBundleError, "publishAllowed=false"):
                load_content_bundle(manifest)

    def test_accepts_ten_tags_and_rejects_the_eleventh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._write_bundle(root, "article")
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["tags"] = [f"标签{i}" for i in range(1, 11)]
            manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(load_content_bundle(manifest)["tags"], data["tags"])

            data["tags"].append("标签11")
            manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ContentBundleError, "最多支持 10"):
                load_content_bundle(manifest)

    def test_ai_image_disclosure_requires_explicit_assets_and_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._write_bundle(root, "article")
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["aiDisclosure"] = {
                "containsAiGeneratedContent": True,
                "contentKinds": ["image"],
                "assetPaths": ["封面_竖.png", "01.png"],
                "allowPlatformAutoDeclaration": True,
            }
            manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            disclosure = load_content_bundle(manifest)["aiDisclosure"]
            self.assertTrue(disclosure["containsAiGeneratedContent"])
            self.assertEqual(disclosure["contentKinds"], ["image"])
            self.assertEqual(len(disclosure["assetPaths"]), 2)

            data["aiDisclosure"]["assetPaths"] = []
            manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ContentBundleError, "必须列出"):
                load_content_bundle(manifest)

    def test_platform_preview_markdown_is_not_exposed_as_common_publish_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._write_bundle(root)
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["preferredPlatforms"] = ["抖音", "小红书"]
            data["tags"] = ["错误通用话题"]
            data["platformOverrides"] = {
                "抖音": {
                    "title": "抖音标题",
                    "body": "纯净抖音正文\n@抖音科技",
                    "tags": ["人工智能"],
                },
                "小红书": {
                    "title": "小红书标题",
                    "body": "纯净小红书正文",
                    "tags": ["AI工具"],
                },
            }
            (root / "正文.md").write_text(
                "# 硅基探索 008\n\n## 抖音\n标题：抖音标题\n\n## 小红书\n标题：小红书标题",
                encoding="utf-8",
            )
            manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

            bundle = load_content_bundle(manifest)

        self.assertTrue(bundle["bodyIsPreview"])
        self.assertEqual(bundle["commonTitle"], "")
        self.assertEqual(bundle["commonBody"], "")
        self.assertEqual(bundle["commonTags"], [])
        self.assertEqual(bundle["platformOverrides"]["抖音"]["body"], "纯净抖音正文\n@抖音科技")

    def test_legacy_ai_disclosure_is_migrated_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._write_bundle(root)
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["aiDisclosure"] = {
                "text": False,
                "image": False,
                "video": True,
                "audio": True,
            }
            data["allowPlatformAutoDeclaration"] = False
            manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

            bundle = load_content_bundle(manifest)

        self.assertTrue(bundle["aiDisclosure"]["containsAiGeneratedContent"])
        self.assertEqual(bundle["aiDisclosure"]["contentKinds"], ["video", "audio"])
        self.assertFalse(bundle["aiDisclosure"]["allowPlatformAutoDeclaration"])
        self.assertEqual(bundle["migrationWarnings"], ["legacy_ai_disclosure_migrated"])

    def test_loads_explicit_local_publish_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._write_bundle(root, "article")
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["publishSchedule"] = {
                "enabled": True,
                "localTime": "2026-08-01 11:00",
                "timezone": "Asia/Shanghai",
            }
            manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            schedule = load_content_bundle(manifest)["publishSchedule"]
        self.assertTrue(schedule["enabled"])
        self.assertEqual(schedule["localTime"], "2026-08-01 11:00")
        self.assertEqual(schedule["timezone"], "Asia/Shanghai")

    def test_rejects_incomplete_or_disabled_publish_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._write_bundle(root, "article")
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["publishSchedule"] = {
                "enabled": True,
                "localTime": "2026-08-01 11:00",
            }
            manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ContentBundleError, "必须同时填写"):
                load_content_bundle(manifest)

            data["publishSchedule"] = {
                "enabled": False,
                "localTime": "2026-08-01 11:00",
                "timezone": "Asia/Shanghai",
            }
            manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ContentBundleError, "不得携带"):
                load_content_bundle(manifest)


if __name__ == "__main__":
    unittest.main()
