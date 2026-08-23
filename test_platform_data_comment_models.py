# -*- coding: utf-8 -*-
"""匿名评论和洞察模型的隐私、合同与边界测试。"""

from __future__ import annotations

import unittest

from app_core.platform_data_comment_models import (
    ALLOWED_COMMENT_LABELS,
    CommentClassification,
    CommentCollectionBatch,
    CommentInsightFailure,
    CommentPage,
    CommentRecord,
    InsightResult,
    TopicCandidate,
    derive_comment_key,
)


OBSERVED = "2026-08-23T10:21:00+08:00"


def comment(key: str = "a" * 64, body: str = "  保留原始空格  ") -> CommentRecord:
    return CommentRecord(
        content_id="work-7",
        comment_key=key,
        body=body,
        like_count=3,
        reply_count=1,
        commented_at="2026-08-23T10:20:30+08:00",
        observed_at=OBSERVED,
    )


class CommentModelsTests(unittest.TestCase):
    def test_comment_id_is_hashed_and_raw_id_never_enters_record(self):
        key = derive_comment_key(12, "work-7", "platform-comment-99")
        self.assertRegex(key, r"^[0-9a-f]{64}$")
        self.assertNotIn("platform-comment-99", key)
        record = comment(key)
        self.assertEqual(record.body, "  保留原始空格  ")
        self.assertFalse(hasattr(record, "author_id"))
        self.assertFalse(hasattr(record, "platform_comment_id"))

    def test_anonymization_is_deterministic_and_validates_exact_scalars(self):
        self.assertEqual(
            derive_comment_key(12, "work-7", "platform-comment-99"),
            derive_comment_key(12, "work-7", "platform-comment-99"),
        )
        for args in ((True, "work-7", "c"), (0, "work-7", "c"), (12, "", "c"), (12, "w", "") ):
            with self.subTest(args=args), self.assertRaises(CommentInsightFailure):
                derive_comment_key(*args)

    def test_record_requires_exact_types_nonnegative_counts_and_timezone(self):
        values = dict(
            content_id="work-7",
            comment_key="a" * 64,
            body="正文",
            like_count=0,
            reply_count=0,
            commented_at="2026-08-23T10:20:30+08:00",
            observed_at=OBSERVED,
        )
        self.assertIs(type(comment().like_count), int)
        for field, value in {
            "like_count": -1,
            "reply_count": True,
            "commented_at": "2026-08-23T10:20:30",
            "observed_at": "2026-08-23T10:21:00",
            "body": "",
            "comment_key": "not-a-sha256-key",
        }.items():
            with self.subTest(field=field):
                with self.assertRaises(CommentInsightFailure):
                    CommentRecord(**(values | {field: value}))

    def test_page_rejects_duplicate_comments_and_requires_tuple(self):
        with self.assertRaises(CommentInsightFailure):
            CommentPage(comments=(comment(), comment()), has_more=False, next_cursor="", platform_end=True)
        with self.assertRaises(CommentInsightFailure):
            CommentPage(comments=[comment()], has_more=False, next_cursor="", platform_end=True)

    def test_batch_counts_are_deduplicated_and_limit_warning_is_strict(self):
        page = CommentPage(comments=(comment(),), has_more=False, next_cursor="", platform_end=True)
        batch = CommentCollectionBatch(
            platform_type=3,
            source_mode="direct_session",
            content_id="work-7",
            comments=page.comments,
            accepted_count=1,
            rejected_count=0,
            page_count=1,
            stop_reason="platform_end",
            warning_code="",
            platform_observed_at=OBSERVED,
            cleanup_receipt=None,
        )
        self.assertEqual(batch.accepted_count, len(batch.comments))
        pending = CommentCollectionBatch(
            platform_type=3,
            source_mode="direct_session",
            content_id="work-7",
            comments=(),
            accepted_count=0,
            rejected_count=0,
            page_count=0,
            stop_reason="",
            warning_code="",
            platform_observed_at=OBSERVED,
            cleanup_receipt=None,
        )
        self.assertEqual(pending.stop_reason, "")
        with self.assertRaises(CommentInsightFailure):
            CommentCollectionBatch(
                platform_type=3, source_mode="direct_session", content_id="work-7",
                comments=page.comments, accepted_count=2, rejected_count=0, page_count=1,
                stop_reason="platform_end", warning_code="", platform_observed_at=OBSERVED,
                cleanup_receipt=None,
            )

    def test_classification_allows_only_six_labels_and_no_duplicate_keys(self):
        self.assertEqual(len(ALLOWED_COMMENT_LABELS), 6)
        valid = CommentClassification("a" * 64, ("追问", "认同"))
        self.assertEqual(valid.labels, ("追问", "认同"))
        for labels in (("未知",), ("追问", "追问"), ()):
            with self.subTest(labels=labels), self.assertRaises(CommentInsightFailure):
                CommentClassification("a" * 64, labels)

    def test_insight_rejects_unknown_or_empty_evidence(self):
        known = frozenset({"a" * 64})
        with self.assertRaises(CommentInsightFailure) as raised:
            InsightResult(
                classifications=(CommentClassification("a" * 64, ("追问",)),),
                candidates=(TopicCandidate("下一期", "回答读者问题", ("b" * 64,)),),
                known_comment_keys=known,
            )
        self.assertEqual(raised.exception.error_code, "comment_ai_evidence_invalid")

    def test_insight_caps_candidates_and_rejects_duplicate_or_empty_evidence(self):
        known = frozenset({"a" * 64, "b" * 64})
        with self.assertRaises(CommentInsightFailure):
            InsightResult(
                classifications=(),
                candidates=tuple(TopicCandidate(str(i), "理由", ("a" * 64,)) for i in range(6)),
                known_comment_keys=known,
            )
        with self.assertRaises(CommentInsightFailure):
            TopicCandidate("标题", "理由", ("a" * 64, "a" * 64))


if __name__ == "__main__":
    unittest.main()
