import asyncio
import tempfile
import unittest
from pathlib import Path

from app_core.douyin_graphic_editor import (
    DouyinGraphicEditor,
    DouyinGraphicEditorError,
)


class FakeGraphicPage:
    def __init__(self) -> None:
        self.uploaded_files: list[str] = []
        self.official_topics = {"图文矩阵", "门店经营"}
        self.submit_clicks = 0
        self.receipt = {
            "platformPostId": "post-31",
            "postUrl": "https://creator.douyin.com/content/manage/post-31",
            "scheduledAt": "2026-08-27 18:00",
            "timezone": "Asia/Shanghai",
        }

    async def douyin_graphic_upload(self, files: list[str]) -> int:
        self.uploaded_files = list(files)
        return len(files)

    async def douyin_graphic_sync_content(
        self, *, title: str, body: str, tags: list[str]
    ) -> dict:
        confirmed = [tag for tag in tags if tag in self.official_topics]
        return {"title": title, "body": body, "tags": confirmed}

    async def douyin_graphic_set_schedule(self, value: str) -> str:
        return value

    async def douyin_graphic_submit(self) -> None:
        self.submit_clicks += 1

    async def douyin_graphic_receipt(self, _payload: dict) -> dict | None:
        return self.receipt


class DouyinGraphicEditorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.files = []
        for index in range(3):
            image = Path(self.tempdir.name) / f"{index}.jpg"
            image.write_bytes(f"image-{index}".encode())
            self.files.append(str(image))
        self.payload = {
            "type": 3,
            "contentType": "article",
            "workflow": "douyin-graphic-matrix",
            "title": "矩阵标题",
            "description": "矩阵正文",
            "tags": ["图文矩阵", "门店经营"],
            "fileList": list(self.files),
            "enableTimer": True,
            "scheduleTime": "2026-08-27 18:00",
            "scheduleTimezone": "Asia/Shanghai",
        }
        self.page = FakeGraphicPage()
        self.editor = DouyinGraphicEditor()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_prepare_uploads_all_images_and_requires_official_topic_entities(self) -> None:
        readback = asyncio.run(self.editor.prepare(self.page, self.payload))

        self.assertEqual(self.page.uploaded_files, self.payload["fileList"])
        self.assertEqual(readback.image_count, 3)
        self.assertEqual(readback.title, "矩阵标题")
        self.assertEqual(readback.body, "矩阵正文")
        self.assertEqual(readback.tags, ("图文矩阵", "门店经营"))
        self.assertEqual(readback.scheduled_at, "2026-08-27 18:00")
        self.assertEqual(self.page.submit_clicks, 0)

    def test_plain_hash_text_is_not_accepted_as_a_topic_entity(self) -> None:
        self.page.official_topics = {"图文矩阵"}

        with self.assertRaises(DouyinGraphicEditorError) as raised:
            asyncio.run(self.editor.prepare(self.page, self.payload))

        self.assertEqual(raised.exception.error_code, "douyin_topic_entity_missing")
        self.assertEqual(self.page.submit_clicks, 0)

    def test_submit_requires_platform_receipt(self) -> None:
        asyncio.run(self.editor.prepare(self.page, self.payload))
        self.page.receipt = None

        with self.assertRaises(DouyinGraphicEditorError) as raised:
            asyncio.run(
                self.editor.submit_and_read_receipt(self.page, self.payload)
            )

        self.assertEqual(
            raised.exception.error_code,
            "douyin_graphic_submit_receipt_missing",
        )


if __name__ == "__main__":
    unittest.main()
