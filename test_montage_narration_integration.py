# -*- coding: utf-8 -*-
"""当前 macOS 系统声音与完整 FFmpeg 的真实混剪配音验证。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from app_core.montage_models import MontageRequest
from app_core.montage_runtime import MontageRuntime
from app_core.montage_service import run_montage_batch
from app_core.narration_service import (
    probe_audio_readback,
    synthesize_system_narration,
)


class MontageNarrationIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    @unittest.skipUnless(
        sys.platform == "darwin"
        and Path("/usr/bin/say").is_file()
        and shutil.which("ffmpeg")
        and shutil.which("ffprobe"),
        "requires macOS say and complete FFmpeg",
    )
    def test_real_batch_reuses_one_system_narration_for_three_outputs(self) -> None:
        ffmpeg = Path(shutil.which("ffmpeg") or "")
        ffprobe = Path(shutil.which("ffprobe") or "")
        runtime = MontageRuntime(ffmpeg=ffmpeg, ffprobe=ffprobe)
        source_a = self.root / "red-440hz.mp4"
        source_b = self.root / "blue-880hz.mp4"
        for color, frequency, target in (
            ("red", 440, source_a),
            ("blue", 880, source_b),
        ):
            subprocess.run(
                [
                    str(ffmpeg),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    f"color=c={color}:s=320x180:r=30",
                    "-f",
                    "lavfi",
                    "-i",
                    f"sine=frequency={frequency}:sample_rate=48000",
                    "-t",
                    "20",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-shortest",
                    str(target),
                ],
                check=True,
            )
        source_audio_hashes = {
            probe_audio_readback(path, runtime=runtime).stream_sha256
            for path in (source_a, source_b)
        }
        narration_text = (
            "你好，这是一段本机自动配音测试。"
            "三条成片会共享同一条主音轨，不会保留素材原声。"
        )
        request = MontageRequest.from_mapping(
            {
                "source_paths": [str(source_a), str(source_b)],
                "output_count": 3,
                "target_duration_ms": 5_000,
                "clip_duration_ms": 1_000,
                "allow_reuse": False,
                "audio_mode": "narration",
                "source_audio_confirmed": False,
                "narration_text": narration_text,
                "seed": 20260903,
                "title_template": "",
                "body_template": "",
            }
        )
        evidence_root = os.environ.get("ONECLICK_MONTAGE_EVIDENCE_DIR")
        output_root = Path(evidence_root).resolve() if evidence_root else self.root / "outputs"
        narration_commands: list[list[str]] = []
        progress_events: list[dict[str, object]] = []

        def recording_runner(command: list[str], **kwargs: object):
            narration_commands.append(list(command))
            return subprocess.run(command, **kwargs)

        def real_narrator(text: str, output_dir: Path, **kwargs: object):
            return synthesize_system_narration(
                text,
                output_dir,
                runtime=kwargs["runtime"],
                runner=recording_runner,
            )

        result = run_montage_batch(
            request,
            output_root=output_root,
            runtime=runtime,
            batch_id="M-NARRATION-INTEGRATION",
            narrator=real_narrator,
            progress=progress_events.append,
        )

        say_synthesis_commands = [
            command
            for command in narration_commands
            if command and command[0] == "/usr/bin/say" and "-o" in command
        ]
        self.assertEqual(len(say_synthesis_commands), 1)
        self.assertTrue(
            all(narration_text not in argument for command in narration_commands for argument in command)
        )
        self.assertNotIn(narration_text, json.dumps(progress_events, ensure_ascii=False))

        self.assertEqual(result.status, "success")
        self.assertEqual(len(result.outputs), 3)
        self.assertIsNotNone(result.narration)
        assert result.narration is not None
        narration = result.narration
        master_path = Path(str(narration["master_path"]))
        master_readback = probe_audio_readback(master_path, runtime=runtime)
        self.assertEqual(master_path.suffix.lower(), ".m4a")
        self.assertEqual(master_readback.codec_name, "aac")
        self.assertEqual(master_readback.sample_rate, 48_000)
        self.assertEqual(master_readback.channels, 2)
        self.assertIn("m4a", master_readback.format_name.split(","))
        self.assertEqual(narration["master_duration_ms"], narration["speech_duration_ms"] + 300)
        self.assertGreaterEqual(narration["master_duration_ms"], 5_000)
        self.assertLessEqual(narration["master_duration_ms"], 180_000)
        self.assertLessEqual(
            abs(master_readback.duration_ms - narration["master_duration_ms"]),
            100,
        )

        master_hash = narration["stream_sha256"]
        self.assertEqual(master_readback.stream_sha256, master_hash)
        self.assertTrue(
            all(item["audio_stream_sha256"] == master_hash for item in result.outputs)
        )
        self.assertTrue(
            all(item["narration_sha256"] == narration["sha256"] for item in result.outputs)
        )
        self.assertTrue(
            all(item["audio_stream_sha256"] not in source_audio_hashes for item in result.outputs)
        )
        self.assertTrue(
            all(abs(item["duration_ms"] - item["expected_duration_ms"]) <= 500 for item in result.outputs)
        )
        self.assertTrue(all(item["status"] == "success" for item in result.outputs))
        self.assertTrue(
            all(Path(str(item["output_path"])).is_file() for item in result.outputs)
        )
        for item in result.outputs:
            output_readback = probe_audio_readback(Path(str(item["output_path"])), runtime=runtime)
            self.assertEqual(output_readback.codec_name, "aac")
            self.assertEqual(output_readback.sample_rate, 48_000)
            self.assertEqual(output_readback.channels, 2)
            self.assertEqual(output_readback.stream_sha256, master_hash)

        receipt = json.loads(result.receipt_path.read_text(encoding="utf-8"))
        request_receipt = json.loads(
            (result.batch_dir / "request.json").read_text(encoding="utf-8")
        )
        self.assertEqual(request_receipt["schema_version"], "oneclick-montage-request/v2")
        self.assertEqual(receipt["schema_version"], "oneclick-montage-batch-receipt/v2")
        self.assertEqual(receipt["batch_id"], "M-NARRATION-INTEGRATION")
        self.assertEqual(receipt["status"], "success")
        self.assertTrue(receipt["started_at"])
        self.assertTrue(receipt["finished_at"])
        self.assertEqual(receipt["summary"], {"success": 3, "failed": 0})
        self.assertEqual(receipt["effective_target_duration_ms"], narration["master_duration_ms"])
        self.assertEqual(receipt["narration"], narration)
        self.assertEqual(len(receipt["outputs"]), 3)
        self.assertTrue(all(item["status"] == "success" for item in receipt["outputs"]))
        for json_path in result.batch_dir.rglob("*.json"):
            self.assertNotIn(narration_text, json_path.read_text(encoding="utf-8"))
        print(f"NARRATION_EVIDENCE={result.receipt_path}")


if __name__ == "__main__":
    unittest.main()
