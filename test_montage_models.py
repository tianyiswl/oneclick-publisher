# -*- coding: utf-8 -*-
"""轻量混剪请求、文案变体与计划器测试。"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from app_core.montage_models import (
    MontageFailure,
    MontageRequest,
    VideoAsset,
    expand_copy_variants,
    plan_montages,
)


class MontageModelsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.source_a = self.root / "a.mp4"
        self.source_b = self.root / "b.mov"
        self.source_a.write_bytes(b"a")
        self.source_b.write_bytes(b"b")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def request(self, **overrides: object) -> MontageRequest:
        raw: dict[str, object] = {
            "source_paths": [str(self.source_a), str(self.source_b)],
            "output_count": 2,
            "target_duration_ms": 6_000,
            "clip_duration_ms": 2_000,
            "allow_reuse": False,
            "audio_mode": "mute",
            "narration_text": "",
            "seed": 42,
            "title_template": "[今天|这次]看[新品|新款]",
            "body_template": "重点看看[做工|细节]。",
        }
        raw.update(overrides)
        return MontageRequest.from_mapping(raw)

    def test_narration_mode_requires_text_and_normalizes_it(self) -> None:
        with self.assertRaises(MontageFailure) as missing:
            self.request(audio_mode="narration", narration_text="  ")
        self.assertEqual(missing.exception.code, "montage_narration_text_required")

        request = self.request(
            audio_mode="narration",
            narration_text="  你好，这是一段解说。  ",
        )
        self.assertEqual(request.narration_text, "你好，这是一段解说。")
        self.assertFalse(request.source_audio_confirmed)
        self.assertEqual(request.to_dict()["narration_text"], "你好，这是一段解说。")

    def test_non_narration_mode_ignores_stale_narration_text(self) -> None:
        request = self.request(audio_mode="mute", narration_text="不会参与生成")
        self.assertEqual(request.narration_text, "")

    def test_narration_mode_does_not_compare_clip_with_ignored_ui_target(self) -> None:
        request = self.request(
            audio_mode="narration",
            narration_text="这段配音最终会超过五秒。",
            target_duration_ms=5_000,
            clip_duration_ms=10_000,
        )
        self.assertEqual(request.clip_duration_ms, 10_000)

    def asset(
        self,
        path: Path,
        sha: str,
        *,
        duration_ms: int = 12_000,
        has_audio: bool = True,
    ) -> VideoAsset:
        return VideoAsset(
            local_path=path.resolve(),
            sha256=sha * 64,
            duration_ms=duration_ms,
            width=1920,
            height=1080,
            fps=30.0,
            has_audio=has_audio,
        )

    def test_request_rejects_unknown_fields_and_duplicate_sources(self) -> None:
        with self.assertRaises(MontageFailure) as unknown:
            self.request(unexpected=True)
        self.assertEqual(unknown.exception.code, "montage_request_unknown_field")

        with self.assertRaises(MontageFailure) as duplicate:
            self.request(source_paths=[str(self.source_a), str(self.source_a)])
        self.assertEqual(duplicate.exception.code, "montage_source_duplicate")

    def test_request_rejects_invalid_ranges_and_unsupported_suffix(self) -> None:
        with self.assertRaises(MontageFailure) as bad_count:
            self.request(output_count=0)
        self.assertEqual(bad_count.exception.code, "montage_output_count_invalid")

        text_file = self.root / "notes.txt"
        text_file.write_text("x", encoding="utf-8")
        with self.assertRaises(MontageFailure) as bad_source:
            self.request(source_paths=[str(text_file)])
        self.assertEqual(bad_source.exception.code, "montage_source_type_unsupported")

    def test_source_audio_requires_explicit_no_speech_confirmation(self) -> None:
        with self.assertRaises(MontageFailure) as unconfirmed:
            self.request(audio_mode="source")
        self.assertEqual(
            unconfirmed.exception.code,
            "montage_source_audio_confirmation_required",
        )

        confirmed = self.request(
            audio_mode="source",
            source_audio_confirmed=True,
        )
        self.assertTrue(confirmed.source_audio_confirmed)
        self.assertTrue(confirmed.to_dict()["source_audio_confirmed"])

    def test_copy_variants_are_unique_and_reproducible(self) -> None:
        first = expand_copy_variants(
            "[今天|这次]看[新品|新款]",
            "重点看[做工|细节]。",
            count=8,
            seed=7,
        )
        second = expand_copy_variants(
            "[今天|这次]看[新品|新款]",
            "重点看[做工|细节]。",
            count=8,
            seed=7,
        )
        self.assertEqual(first, second)
        self.assertEqual(len({(item.title, item.body) for item in first}), 8)

    def test_copy_variant_capacity_and_syntax_have_stable_errors(self) -> None:
        with self.assertRaises(MontageFailure) as capacity:
            expand_copy_variants("同一个标题", "同一个正文", count=2, seed=1)
        self.assertEqual(capacity.exception.code, "copy_variant_capacity_insufficient")

        with self.assertRaises(MontageFailure) as syntax:
            expand_copy_variants("[今天|]", "正文", count=1, seed=1)
        self.assertEqual(syntax.exception.code, "copy_variant_syntax_invalid")

    def test_copy_capacity_counts_final_text_not_only_cartesian_combinations(self) -> None:
        with self.assertRaises(MontageFailure) as caught:
            expand_copy_variants("[ab|a][c|bc]", "", count=4, seed=0)
        self.assertEqual(caught.exception.code, "copy_variant_capacity_insufficient")
        self.assertEqual(caught.exception.details["capacity"], 3)

    def test_empty_copy_is_allowed_for_video_only_batch(self) -> None:
        variants = expand_copy_variants("", "", count=3, seed=1)
        self.assertEqual([(item.title, item.body) for item in variants], [("", "")] * 3)

    def test_strict_plans_never_reuse_a_source_window_and_are_deterministic(self) -> None:
        assets = (
            self.asset(self.source_a, "a"),
            self.asset(self.source_b, "b"),
        )
        request = self.request()
        first = plan_montages(request, assets)
        second = plan_montages(request, assets)

        self.assertEqual(first, second)
        self.assertEqual([plan.fingerprint for plan in first], [plan.fingerprint for plan in second])
        identities = [clip.window_identity for plan in first for clip in plan.clips]
        self.assertEqual(len(identities), len(set(identities)))
        self.assertEqual([clip.duration_ms for clip in first[0].clips], [2_000, 2_000, 2_000])

    def test_last_clip_uses_target_remainder(self) -> None:
        request = self.request(
            output_count=1,
            target_duration_ms=5_000,
            title_template="",
            body_template="",
        )
        plans = plan_montages(
            request,
            (self.asset(self.source_a, "a"), self.asset(self.source_b, "b")),
        )
        self.assertEqual([clip.duration_ms for clip in plans[0].clips], [2_000, 2_000, 1_000])
        self.assertEqual(plans[0].total_duration_ms, 5_000)

    def test_strict_capacity_failure_reports_maximum(self) -> None:
        request = self.request(output_count=3, title_template="", body_template="")
        assets = (
            self.asset(self.source_a, "a", duration_ms=6_000),
            self.asset(self.source_b, "b", duration_ms=6_000),
        )
        with self.assertRaises(MontageFailure) as caught:
            plan_montages(request, assets)
        self.assertEqual(caught.exception.code, "montage_strict_capacity_insufficient")
        self.assertEqual(caught.exception.details["max_strict_outputs"], 2)

    def test_same_video_copied_under_two_filenames_is_rejected(self) -> None:
        request = self.request(output_count=1, title_template="", body_template="")
        duplicate_assets = (
            self.asset(self.source_a, "a"),
            self.asset(self.source_b, "a"),
        )
        with self.assertRaises(MontageFailure) as caught:
            plan_montages(request, duplicate_assets)
        self.assertEqual(caught.exception.code, "montage_source_content_duplicate")

    def test_controlled_reuse_prefers_least_used_and_keeps_plans_unique(self) -> None:
        request = self.request(
            output_count=4,
            target_duration_ms=6_000,
            allow_reuse=True,
            title_template="[甲|乙|丙|丁]",
            body_template="",
        )
        assets = (
            self.asset(self.source_a, "a", duration_ms=6_000),
            self.asset(self.source_b, "b", duration_ms=6_000),
        )
        plans = plan_montages(request, assets)
        self.assertEqual(len(plans), 4)
        self.assertEqual(len({plan.fingerprint for plan in plans}), 4)
        for plan in plans:
            identities = [clip.window_identity for clip in plan.clips]
            self.assertEqual(len(identities), len(set(identities)))

        use_counts: dict[str, int] = {}
        for plan in plans:
            for clip in plan.clips:
                use_counts[clip.window_identity] = use_counts.get(clip.window_identity, 0) + 1
        self.assertLessEqual(max(use_counts.values()) - min(use_counts.values()), 1)


if __name__ == "__main__":
    unittest.main()
