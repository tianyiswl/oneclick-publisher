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
