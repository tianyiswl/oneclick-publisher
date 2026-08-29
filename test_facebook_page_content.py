# -*- coding: utf-8 -*-
"""Final Facebook Page caption construction is a single frozen contract."""

from __future__ import annotations

import hashlib
import unittest

from app_core.overseas_meta_content import (
    build_facebook_page_caption,
    facebook_page_caption_sha256,
)
from app_core.overseas_meta_errors import FacebookPagePublishError


class FacebookPageContentTests(unittest.TestCase):
    def test_caption_joins_title_body_and_topic_line_with_one_blank_line(self) -> None:
        caption = build_facebook_page_caption(
            title="标题",
            body="正文",
            topics=("#OneClick", "AI工具"),
        )
        self.assertEqual(caption, "标题\n\n正文\n\n#OneClick #AI工具")

    def test_structured_topics_strip_one_hash_and_deduplicate_first_seen_exact_values(self) -> None:
        caption = build_facebook_page_caption(
            title="标题",
            body="正文",
            topics=("#OneClick", "OneClick", "##保留一个", "oneclick"),
        )
        self.assertEqual(caption, "标题\n\n正文\n\n#OneClick ##保留一个 #oneclick")

    def test_empty_or_multiline_structured_topics_are_rejected(self) -> None:
        for topic in ("", "   ", "#", "first\nsecond", "first\rsecond"):
            with self.subTest(topic=repr(topic)), self.assertRaises(FacebookPagePublishError) as raised:
                build_facebook_page_caption(title="标题", body="正文", topics=(topic,))
            self.assertEqual(raised.exception.error_code, "facebook_page_content_invalid")

    def test_title_and_body_are_preserved_and_raw_markers_stay_plain_text(self) -> None:
        title = "标题 #不去重"
        body = "正文 @普通文本 #不是结构化话题\n正文 #不去重"
        caption = build_facebook_page_caption(title=title, body=body, topics=("结构化",))
        self.assertEqual(
            caption,
            "标题 #不去重\n\n正文 @普通文本 #不是结构化话题\n正文 #不去重\n\n#结构化",
        )
        self.assertNotIn("verifiedMentions", caption)

    def test_caption_hash_is_sha256_of_final_utf8_bytes(self) -> None:
        caption = "标题\n\n正文\n\n#OneClick #AI工具"
        self.assertEqual(
            facebook_page_caption_sha256(caption),
            hashlib.sha256(caption.encode("utf-8")).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()
