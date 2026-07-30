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

    def test_loads_article_bundle_with_platform_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bundle = load_content_bundle(self._write_bundle(Path(temporary), "article"))
        self.assertEqual(len(bundle["assetPaths"]), 1)
        self.assertEqual(bundle["platformOverrides"]["小红书"]["tags"], ["测试话题"])

    def test_rejects_publish_enabled_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._write_bundle(root)
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["publishAllowed"] = True
            manifest.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ContentBundleError, "publishAllowed=false"):
                load_content_bundle(manifest)


if __name__ == "__main__":
    unittest.main()
