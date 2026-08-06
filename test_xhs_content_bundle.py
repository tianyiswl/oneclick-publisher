# -*- coding: utf-8 -*-
"""小红书图文内容包的离线安全校验测试。"""

import json
import tempfile
import unittest
from pathlib import Path

from app_core.xhs_content_bundle import XhsContentBundleError, load_xhs_content_bundle


class XhsContentBundleTests(unittest.TestCase):
    def _bundle(self, root: Path, **overrides) -> Path:
        (root / "图片").mkdir()
        (root / "正文.md").write_text("这是正文。", encoding="utf-8")
        (root / "图片/01.png").write_bytes(b"png")
        data = {
            "schemaVersion": "oneclick-xhs-content/v1",
            "platform": "小红书",
            "contentType": "article",
            "title": "测试标题",
            "bodyFile": "正文.md",
            "tags": ["北海天气", "北海出行"],
            "images": ["图片/01.png"],
            "debugDryRun": True,
            "publishAllowed": False,
        }
        data.update(overrides)
        manifest = root / "manifest.json"
        manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return manifest

    def test_loads_local_xhs_article_without_external_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bundle = load_xhs_content_bundle(self._bundle(Path(temporary)))
        self.assertEqual(bundle["title"], "测试标题")
        self.assertEqual(bundle["tags"], ["北海天气", "北海出行"])
        self.assertEqual(len(bundle["imagePaths"]), 1)

    def test_rejects_publish_enabled_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._bundle(Path(temporary), publishAllowed=True)
            with self.assertRaisesRegex(XhsContentBundleError, "publishAllowed=false"):
                load_xhs_content_bundle(manifest)

    def test_rejects_absolute_image_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._bundle(Path(temporary), images=["/tmp/elsewhere.png"])
            with self.assertRaisesRegex(XhsContentBundleError, "相对路径"):
                load_xhs_content_bundle(manifest)

    def test_accepts_ten_tags_and_rejects_the_eleventh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tags = [f"标签{i}" for i in range(1, 11)]
            self.assertEqual(load_xhs_content_bundle(self._bundle(root, tags=tags))["tags"], tags)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tags = [f"标签{i}" for i in range(1, 12)]
            with self.assertRaisesRegex(XhsContentBundleError, "最多支持 10"):
                load_xhs_content_bundle(self._bundle(root, tags=tags))


if __name__ == "__main__":
    unittest.main()
