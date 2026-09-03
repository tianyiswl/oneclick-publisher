# -*- coding: utf-8 -*-
"""轻量混剪批次服务与原子回执测试。"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from app_core.montage_models import MontageFailure, MontageRequest, VideoAsset
from app_core.montage_runtime import MontageRuntime
from app_core.montage_service import run_montage_batch
from app_core.narration_service import NarrationArtifact


class MontageServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.output_root = self.root / "outputs"
        self.source_a = self.root / "a.mp4"
        self.source_b = self.root / "b.mp4"
        self.source_a.write_bytes(b"a")
        self.source_b.write_bytes(b"b")
        self.runtime = MontageRuntime(self.root / "ffmpeg", self.root / "ffprobe")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def request(self, **overrides: object) -> MontageRequest:
        raw: dict[str, object] = {
            "source_paths": [str(self.source_a), str(self.source_b)],
            "output_count": 2,
            "target_duration_ms": 5_000,
            "clip_duration_ms": 1_000,
            "allow_reuse": False,
            "audio_mode": "mute",
            "seed": 99,
            "title_template": "[甲|乙]标题",
            "body_template": "正文[一|二]",
        }
        raw.update(overrides)
        return MontageRequest.from_mapping(raw)

    def fake_probe(self, path: Path, **_kwargs: object) -> VideoAsset:
        marker = "a" if Path(path).name == "a.mp4" else "b"
        return VideoAsset(
            local_path=Path(path).resolve(),
            sha256=marker * 64,
            duration_ms=10_000,
            width=1920,
            height=1080,
            fps=30,
            has_audio=False,
        )

    @staticmethod
    def fake_renderer(plan, output_path: Path, **_kwargs: object) -> dict[str, object]:
        output_path.write_bytes(f"video-{plan.index}".encode("ascii"))
        return {
            "status": "success",
            "output_path": str(output_path),
            "sha256": f"{plan.index}" * 64,
            "fingerprint": plan.fingerprint,
            "duration_ms": plan.total_duration_ms,
            "width": 1080,
            "height": 1920,
            "audio_mode": "mute",
        }

    def narration_artifact(self) -> NarrationArtifact:
        master = self.root / "master.m4a"
        master.write_bytes(b"audio")
        return NarrationArtifact(
            master_path=master,
            voice_id="Test Chinese",
            voice_locale="zh-CN",
            speech_duration_ms=5_000,
            master_duration_ms=5_300,
            sha256="a" * 64,
            stream_sha256="b" * 64,
            codec_name="aac",
            sample_rate=48_000,
            channels=2,
            format_name="mov,mp4,m4a,3gp,3g2,mj2",
        )

    def test_narration_is_synthesized_once_before_planning_and_shared(self) -> None:
        artifact = self.narration_artifact()
        narration_calls: list[str] = []
        rendered: list[tuple[int, int, object]] = []

        def narrator(text, output_dir, **_kwargs):
            narration_calls.append(text)
            return artifact

        def renderer(plan, output_path, *, narration, **_kwargs):
            rendered.append((plan.index, plan.total_duration_ms, narration))
            return self.fake_renderer(plan, output_path)

        result = run_montage_batch(
            self.request(audio_mode="narration", narration_text="完整解说"),
            output_root=self.output_root,
            runtime=self.runtime,
            batch_id="M-NARRATION",
            probe=self.fake_probe,
            narrator=narrator,
            renderer=renderer,
        )

        self.assertEqual(narration_calls, ["完整解说"])
        self.assertEqual([item[1] for item in rendered], [5_300, 5_300])
        self.assertTrue(all(item[2] is artifact for item in rendered))
        self.assertEqual(result.narration["sha256"], "a" * 64)
        request_receipt_text = (result.batch_dir / "request.json").read_text(
            encoding="utf-8"
        )
        request_receipt = json.loads(request_receipt_text)
        narration_receipt = json.loads(
            (result.batch_dir / "narration" / "receipt.json").read_text(encoding="utf-8")
        )
        batch_receipt = json.loads(result.receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(request_receipt["schema_version"], "oneclick-montage-request/v2")
        self.assertNotIn("narration_text", request_receipt)
        self.assertNotIn("完整解说", request_receipt_text)
        self.assertEqual(
            narration_receipt["schema_version"],
            "oneclick-montage-narration/v1",
        )
        self.assertEqual(
            batch_receipt["schema_version"],
            "oneclick-montage-batch-receipt/v2",
        )
        self.assertEqual(batch_receipt["effective_target_duration_ms"], 5_300)
        self.assertEqual(batch_receipt["narration"], result.narration)

    def test_one_narration_render_failure_continues_with_later_outputs(self) -> None:
        artifact = self.narration_artifact()
        calls: list[int] = []

        def renderer(plan, output_path, **kwargs):
            calls.append(plan.index)
            if plan.index == 1:
                raise MontageFailure(
                    "montage_narration_audio_readback_failed",
                    "第一条音轨回读失败",
                )
            return self.fake_renderer(plan, output_path, **kwargs)

        result = run_montage_batch(
            self.request(audio_mode="narration", narration_text="完整解说"),
            output_root=self.output_root,
            runtime=self.runtime,
            batch_id="M-NARRATION-PARTIAL",
            probe=self.fake_probe,
            narrator=lambda *_args, **_kwargs: artifact,
            renderer=renderer,
        )

        self.assertEqual(calls, [1, 2])
        self.assertEqual(result.status, "partial_failure")
        self.assertEqual([item["status"] for item in result.outputs], ["failed", "success"])
        self.assertEqual(result.outputs[0]["narration_sha256"], "a" * 64)

    def test_narration_clip_failure_keeps_effective_master_duration(self) -> None:
        artifact = self.narration_artifact()

        with self.assertRaises(MontageFailure) as caught:
            run_montage_batch(
                self.request(
                    audio_mode="narration",
                    narration_text="解说",
                    clip_duration_ms=6_000,
                ),
                output_root=self.output_root,
                runtime=self.runtime,
                batch_id="M-NARRATION-CLIP-FAILED",
                probe=self.fake_probe,
                narrator=lambda *_args, **_kwargs: artifact,
                renderer=self.fake_renderer,
            )

        self.assertEqual(caught.exception.code, "montage_clip_duration_invalid")
        batch_receipt = json.loads(
            (
                self.output_root
                / "M-NARRATION-CLIP-FAILED"
                / "batch-receipt.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(batch_receipt["effective_target_duration_ms"], 5_300)

    def test_narration_failure_writes_narration_and_batch_terminal_receipts(self) -> None:
        def narrator(*_args, **_kwargs):
            raise MontageFailure("montage_narration_voice_unavailable", "没有中文声音")

        with self.assertRaises(MontageFailure):
            run_montage_batch(
                self.request(
                    audio_mode="narration",
                    narration_text="解说",
                    output_count=3,
                ),
                output_root=self.output_root,
                runtime=self.runtime,
                batch_id="M-NARRATION-FAILED",
                probe=self.fake_probe,
                narrator=narrator,
                renderer=self.fake_renderer,
            )

        failed_dir = self.output_root / "M-NARRATION-FAILED"
        narration_receipt = json.loads(
            (failed_dir / "narration" / "receipt.json").read_text(encoding="utf-8")
        )
        batch_receipt = json.loads(
            (failed_dir / "batch-receipt.json").read_text(encoding="utf-8")
        )
        self.assertEqual(narration_receipt["error_code"], "montage_narration_voice_unavailable")
        self.assertEqual(batch_receipt["status"], "failed")
        self.assertEqual(
            batch_receipt["schema_version"],
            "oneclick-montage-batch-receipt/v2",
        )
        self.assertEqual(batch_receipt["summary"], {"success": 0, "failed": 3})
        self.assertEqual(len(batch_receipt["outputs"]), 3)
        self.assertTrue(
            all(item["status"] == "failed" for item in batch_receipt["outputs"])
        )
        self.assertTrue(
            all(
                item["error_code"] == "montage_output_not_run"
                for item in batch_receipt["outputs"]
            )
        )
        self.assertTrue(
            all(
                item["details"]["cause_error_code"]
                == "montage_narration_voice_unavailable"
                for item in batch_receipt["outputs"]
            )
        )
        self.assertNotIn("running", batch_receipt.values())

    def test_batch_abort_marks_every_remaining_output_not_run(self) -> None:
        raised = False

        def progress(event: dict[str, object]) -> None:
            nonlocal raised
            if (
                not raised
                and event.get("stage") == "output_completed"
                and event.get("output_index") == 1
            ):
                raised = True
                raise MontageFailure("montage_progress_failed", "进度回调失败")

        with self.assertRaises(MontageFailure):
            run_montage_batch(
                self.request(),
                output_root=self.output_root,
                runtime=self.runtime,
                batch_id="M-PARTIAL-BATCH-ABORT",
                probe=self.fake_probe,
                renderer=self.fake_renderer,
                progress=progress,
            )

        batch_receipt = json.loads(
            (
                self.output_root
                / "M-PARTIAL-BATCH-ABORT"
                / "batch-receipt.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(batch_receipt["summary"], {"success": 1, "failed": 1})
        self.assertEqual(
            [item["status"] for item in batch_receipt["outputs"]],
            ["success", "failed"],
        )
        self.assertEqual(
            batch_receipt["outputs"][1]["error_code"],
            "montage_output_not_run",
        )
        self.assertEqual(
            batch_receipt["outputs"][1]["details"]["cause_error_code"],
            "montage_progress_failed",
        )

    def test_success_writes_isolated_outputs_and_atomic_terminal_receipt(self) -> None:
        events: list[dict[str, object]] = []
        result = run_montage_batch(
            self.request(),
            output_root=self.output_root,
            runtime=self.runtime,
            batch_id="M-TEST-SUCCESS",
            probe=self.fake_probe,
            renderer=self.fake_renderer,
            progress=events.append,
        )

        self.assertEqual(result.status, "success")
        self.assertEqual(len(result.outputs), 2)
        self.assertTrue((result.batch_dir / "request.json").is_file())
        self.assertTrue((result.batch_dir / "001" / "video.mp4").is_file())
        self.assertTrue((result.batch_dir / "001" / "mix-plan.json").is_file())
        self.assertTrue((result.batch_dir / "001" / "copy.json").is_file())
        receipt = json.loads((result.batch_dir / "batch-receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "success")
        self.assertEqual(receipt["summary"], {"success": 2, "failed": 0})
        self.assertFalse(any(result.batch_dir.rglob("*.tmp")))
        self.assertEqual(events[0]["stage"], "probing")
        self.assertEqual(events[-1]["stage"], "completed")

    def test_one_render_failure_does_not_leave_later_output_pending(self) -> None:
        calls: list[int] = []

        def renderer(plan, output_path: Path, **kwargs: object) -> dict[str, object]:
            calls.append(plan.index)
            if plan.index == 1:
                raise MontageFailure("montage_render_failed", "第一条渲染失败")
            return self.fake_renderer(plan, output_path, **kwargs)

        result = run_montage_batch(
            self.request(),
            output_root=self.output_root,
            runtime=self.runtime,
            batch_id="M-TEST-PARTIAL",
            probe=self.fake_probe,
            renderer=renderer,
        )

        self.assertEqual(calls, [1, 2])
        self.assertEqual(result.status, "partial_failure")
        self.assertEqual([item["status"] for item in result.outputs], ["failed", "success"])
        self.assertEqual(result.outputs[0]["error_code"], "montage_render_failed")
        receipt = json.loads((result.batch_dir / "batch-receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["summary"], {"success": 1, "failed": 1})

    def test_planning_failure_still_writes_failed_terminal_receipt(self) -> None:
        request = self.request(output_count=5, title_template="", body_template="")
        with self.assertRaises(MontageFailure) as caught:
            run_montage_batch(
                request,
                output_root=self.output_root,
                runtime=self.runtime,
                batch_id="M-TEST-FAILED",
                probe=self.fake_probe,
                renderer=self.fake_renderer,
            )
        self.assertEqual(caught.exception.code, "montage_strict_capacity_insufficient")
        receipt_path = self.output_root / "M-TEST-FAILED" / "batch-receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["error_code"], "montage_strict_capacity_insufficient")
        self.assertNotIn("running", receipt.values())

    def test_existing_batch_directory_is_never_overwritten(self) -> None:
        existing = self.output_root / "M-EXISTS"
        existing.mkdir(parents=True)
        marker = existing / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        with self.assertRaises(MontageFailure) as caught:
            run_montage_batch(
                self.request(),
                output_root=self.output_root,
                runtime=self.runtime,
                batch_id="M-EXISTS",
                probe=self.fake_probe,
                renderer=self.fake_renderer,
            )
        self.assertEqual(caught.exception.code, "montage_batch_exists")
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
