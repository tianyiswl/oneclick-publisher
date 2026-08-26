import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from app_core.silicon_evolution_draft_package import (
    FrozenWechatDraftPackageError,
    load_frozen_wechat_draft_package,
)


class FrozenWechatDraftPackageTests(unittest.TestCase):
    def _write_package(self, root: Path, *, image_root: str = "assets") -> tuple[Path, str]:
        package = root / "WX-20260825-001"
        (package / image_root).mkdir(parents=True)
        files = {
            "content.html": b"<p>\xe6\xad\xa3\xe6\x96\x87</p>",
            "cover-master.png": b"cover",
            f"{image_root}/01.png": b"image",
            "publication.json": json.dumps(
                {
                    "article_id": "WX-20260825-001",
                    "title": "测试标题",
                    "digest": "测试摘要",
                    "author": "硅基进化",
                },
                ensure_ascii=False,
            ).encode("utf-8"),
        }
        for name, value in files.items():
            (package / name).write_bytes(value)
        manifest = {
            "article_id": "WX-20260825-001",
            "built_at": "2026-08-25T08:00:00+08:00",
            "files": {
                name: {"sha256": hashlib.sha256(value).hexdigest(), "size": len(value)}
                for name, value in files.items()
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

    def test_loads_v12_package_and_checks_every_manifest_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package, digest = self._write_package(Path(directory))
            loaded = load_frozen_wechat_draft_package(package, expected_sha256=digest)
        self.assertEqual(loaded.article_id, "WX-20260825-001")
        self.assertEqual(loaded.title, "测试标题")
        self.assertEqual(loaded.cover_path.name, "cover-master.png")
        self.assertEqual([path.name for path in loaded.body_image_paths], ["01.png"])

    def test_loads_body_images_from_images_directory_too(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package, digest = self._write_package(Path(directory), image_root="images")
            loaded = load_frozen_wechat_draft_package(package, expected_sha256=digest)
        self.assertEqual([path.name for path in loaded.body_image_paths], ["01.png"])

    def test_rejects_changed_asset_and_publish_enabled_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package, digest = self._write_package(Path(directory))
            (package / "assets" / "01.png").write_bytes(b"changed")
            with self.assertRaisesRegex(FrozenWechatDraftPackageError, "哈希"):
                load_frozen_wechat_draft_package(package, expected_sha256=digest)

            package, digest = self._write_package(Path(directory) / "second")
            manifest_path = package / "release-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["publish_allowed"] = True
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(FrozenWechatDraftPackageError, "未授权发表"):
                load_frozen_wechat_draft_package(
                    package,
                    expected_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                )
