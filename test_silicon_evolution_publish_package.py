import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from app_core.silicon_evolution_publish_package import (
    FrozenWechatPublishPackageError,
    load_frozen_wechat_publish_package,
)


class FrozenWechatPublishPackageTests(unittest.TestCase):
    def _write_package(
        self,
        root: Path,
        *,
        quality_passed: bool = True,
        disclosure: dict | None = None,
    ) -> tuple[Path, str]:
        package = root / "WX-20260826-001"
        (package / "assets").mkdir(parents=True)
        if disclosure is None:
            disclosure = {
                "containsAiGeneratedContent": True,
                "contentKinds": ["image"],
                "assetPaths": ["assets/01.png"],
                "allowPlatformAutoDeclaration": True,
            }
        files = {
            "content.html": "<p>正文</p>".encode("utf-8"),
            "cover-master.png": b"cover",
            "assets/01.png": b"image",
            "publication.json": json.dumps(
                {
                    "article_id": "WX-20260826-001",
                    "title": "测试标题",
                    "digest": "测试摘要",
                    "author": "硅基进化",
                },
                ensure_ascii=False,
            ).encode("utf-8"),
            "quality-receipt.json": json.dumps(
                {
                    "article_id": "WX-20260826-001",
                    "gates": [{"name": "facts", "passed": quality_passed}],
                },
                ensure_ascii=False,
            ).encode("utf-8"),
            "publish-disclosure.json": json.dumps(
                disclosure, ensure_ascii=False
            ).encode("utf-8"),
        }
        for name, content in files.items():
            (package / name).write_bytes(content)
        manifest = {
            "article_id": "WX-20260826-001",
            "built_at": "2026-08-26T08:00:00+08:00",
            "files": {
                name: {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
                for name, content in files.items()
            },
            "publish_allowed": False,
            "renderer_contract": {"implementation_sha256": "a" * 64, "version": "test"},
            "state": "release_ready",
            "title": "测试标题",
        }
        manifest_path = package / "release-manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        return package, hashlib.sha256(manifest_path.read_bytes()).hexdigest()

    def test_loads_hash_verified_release_with_explicit_ai_image_declaration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package, digest = self._write_package(Path(directory))
            loaded = load_frozen_wechat_publish_package(
                package, expected_sha256=digest
            )
        self.assertEqual(loaded.article_id, "WX-20260826-001")
        self.assertEqual(loaded.title, "测试标题")
        self.assertEqual([path.name for path in loaded.body_image_paths], ["01.png"])
        self.assertTrue(loaded.ai_disclosure["containsAiGeneratedContent"])

    def test_rejects_failed_quality_or_missing_ai_image_declaration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package, digest = self._write_package(Path(directory), quality_passed=False)
            with self.assertRaisesRegex(FrozenWechatPublishPackageError, "质量"):
                load_frozen_wechat_publish_package(package, expected_sha256=digest)

            package, digest = self._write_package(
                Path(directory) / "missing", disclosure={
                    "containsAiGeneratedContent": False,
                    "contentKinds": [],
                    "assetPaths": [],
                    "allowPlatformAutoDeclaration": False,
                }
            )
            with self.assertRaisesRegex(FrozenWechatPublishPackageError, "AI"):
                load_frozen_wechat_publish_package(package, expected_sha256=digest)


if __name__ == "__main__":
    unittest.main()
