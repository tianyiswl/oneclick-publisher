# -*- coding: utf-8 -*-
"""完整剪辑运行时、素材探测与本地渲染测试。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from app_core.montage_models import MontageFailure, MontageRequest, VideoAsset, plan_montages
from app_core.montage_runtime import (
    MontageRuntime,
    _segment_command,
    probe_video,
    render_montage,
)


class MontageRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    @staticmethod
    def capable_runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "-filters" in command:
            output = " T.. scale V->V\n T.. crop V->V\n .. concat N->N\n"
        elif "-encoders" in command:
            output = " V..... libx264 H.264\n A..... aac AAC\n"
        else:
            output = "ffmpeg version test\n"
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    def fixed_runtime_files(self) -> tuple[Path, Path]:
        bin_dir = self.root / "runtime" / "ffmpeg" / "bin"
        bin_dir.mkdir(parents=True)
        ffmpeg = bin_dir / "ffmpeg"
        ffprobe = bin_dir / "ffprobe"
        ffmpeg.write_bytes(b"bin")
        ffprobe.write_bytes(b"bin")
        return ffmpeg, ffprobe

    def test_resolve_does_not_accept_playwright_media_binary(self) -> None:
        playwright = self.root / "runtime" / "playwright-browsers" / "ffmpeg-1011"
        playwright.mkdir(parents=True)
        (playwright / "ffmpeg-mac").write_bytes(b"browser only")

        with self.assertRaises(MontageFailure) as caught:
            MontageRuntime.resolve(
                resource_root=self.root,
                which=lambda _name: None,
                runner=self.capable_runner,
            )
        self.assertEqual(caught.exception.code, "montage_ffmpeg_unavailable")

    def test_resolve_accepts_fixed_complete_runtime(self) -> None:
        ffmpeg, ffprobe = self.fixed_runtime_files()
        runtime = MontageRuntime.resolve(
            resource_root=self.root,
            which=lambda _name: None,
            runner=self.capable_runner,
        )
        self.assertEqual(runtime.ffmpeg, ffmpeg.resolve())
        self.assertEqual(runtime.ffprobe, ffprobe.resolve())

    def test_resolve_rejects_runtime_without_required_encoder(self) -> None:
        self.fixed_runtime_files()

        def incomplete(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if "-filters" in command:
                output = "scale\ncrop\nconcat\n"
            elif "-encoders" in command:
                output = "aac\n"
            else:
                output = "version\n"
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

        with self.assertRaises(MontageFailure) as caught:
            MontageRuntime.resolve(
                resource_root=self.root,
                which=lambda _name: None,
                runner=incomplete,
            )
        self.assertEqual(caught.exception.code, "montage_ffmpeg_capability_missing")
        self.assertIn("libx264", caught.exception.details["missing_encoders"])

    def test_probe_video_parses_metadata_and_hashes_source(self) -> None:
        ffmpeg, ffprobe = self.fixed_runtime_files()
        runtime = MontageRuntime(ffmpeg=ffmpeg, ffprobe=ffprobe)
        source = self.root / "source.mp4"
        source.write_bytes(b"source-content")
        payload = {
            "streams": [
                {"codec_type": "video", "width": 720, "height": 1280, "r_frame_rate": "30000/1001"},
                {"codec_type": "audio"},
            ],
            "format": {"duration": "6.250"},
        }

        def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            self.assertEqual(Path(command[0]), ffprobe)
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

        asset = probe_video(source, runtime=runtime, runner=runner)
        self.assertEqual(asset.duration_ms, 6_250)
        self.assertEqual((asset.width, asset.height), (720, 1280))
        self.assertAlmostEqual(asset.fps, 30000 / 1001)
        self.assertTrue(asset.has_audio)
        self.assertEqual(asset.sha256, hashlib.sha256(b"source-content").hexdigest())

    def test_probe_invalid_video_has_stable_error(self) -> None:
        ffmpeg, ffprobe = self.fixed_runtime_files()
        runtime = MontageRuntime(ffmpeg=ffmpeg, ffprobe=ffprobe)
        source = self.root / "broken.mp4"
        source.write_bytes(b"broken")

        def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="Invalid data")

        with self.assertRaises(MontageFailure) as caught:
            probe_video(source, runtime=runtime, runner=runner)
        self.assertEqual(caught.exception.code, "montage_source_probe_failed")

    def test_source_audio_segment_fades_in_and_out_at_cut_boundaries(self) -> None:
        ffmpeg, ffprobe = self.fixed_runtime_files()
        runtime = MontageRuntime(ffmpeg=ffmpeg, ffprobe=ffprobe)
        command = _segment_command(
            runtime,
            source_path=self.root / "spoken.mp4",
            start_ms=1_000,
            duration_ms=2_000,
            source_has_audio=True,
            audio_mode="source",
            output_path=self.root / "segment.mp4",
        )

        filter_complex = command[command.index("-filter_complex") + 1]
        self.assertIn("afade=t=in:st=0:d=0.080", filter_complex)
        self.assertIn("afade=t=out:st=1.920:d=0.080", filter_complex)

    def test_render_receipt_keeps_measured_and_expected_duration_separate(self) -> None:
        ffmpeg, ffprobe = self.fixed_runtime_files()
        runtime = MontageRuntime(ffmpeg=ffmpeg, ffprobe=ffprobe)
        source = self.root / "source.mp4"
        source.write_bytes(b"source")
        asset = VideoAsset(
            local_path=source.resolve(),
            sha256="a" * 64,
            duration_ms=10_000,
            width=1920,
            height=1080,
            fps=30,
            has_audio=False,
        )
        request = MontageRequest.from_mapping(
            {
                "source_paths": [str(source)],
                "output_count": 1,
                "target_duration_ms": 5_000,
                "clip_duration_ms": 1_000,
                "allow_reuse": False,
                "audio_mode": "mute",
                "seed": 17,
                "title_template": "",
                "body_template": "",
            }
        )
        plan = plan_montages(request, (asset,))[0]
        measured = VideoAsset(
            local_path=self.root / "staged.mp4",
            sha256="b" * 64,
            duration_ms=5_123,
            width=1080,
            height=1920,
            fps=30,
            has_audio=False,
        )

        def create_output(command: list[str], **_kwargs: object) -> None:
            Path(command[-1]).write_bytes(b"rendered")

        output = self.root / "output.mp4"
        with patch("app_core.montage_runtime._run_render_command", side_effect=create_output), patch(
            "app_core.montage_runtime.probe_video",
            return_value=measured,
        ):
            receipt = render_montage(plan, output, runtime=runtime, audio_mode="mute")

        self.assertEqual(receipt["duration_ms"], 5_123)
        self.assertEqual(receipt["expected_duration_ms"], 5_000)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "requires system FFmpeg")
    def test_real_runtime_renders_decodable_vertical_video(self) -> None:
        ffmpeg = Path(shutil.which("ffmpeg") or "")
        ffprobe = Path(shutil.which("ffprobe") or "")
        source_a = self.root / "red.mp4"
        source_b = self.root / "blue.mp4"
        for color, target in (("red", source_a), ("blue", source_b)):
            subprocess.run(
                [
                    str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"color=c={color}:s=320x180:r=30",
                    "-t", "6", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(target),
                ],
                check=True,
            )
        runtime = MontageRuntime.resolve(resource_root=self.root)
        assets = tuple(probe_video(path, runtime=runtime) for path in (source_a, source_b))
        request = MontageRequest.from_mapping(
            {
                "source_paths": [str(source_a), str(source_b)],
                "output_count": 1,
                "target_duration_ms": 5_000,
                "clip_duration_ms": 1_000,
                "allow_reuse": False,
                "audio_mode": "mute",
                "seed": 17,
                "title_template": "",
                "body_template": "",
            }
        )
        plan = plan_montages(request, assets)[0]
        output = self.root / "output.mp4"
        receipt = render_montage(plan, output, runtime=runtime, audio_mode="mute")
        result = probe_video(output, runtime=runtime)

        self.assertTrue(output.is_file())
        self.assertEqual((result.width, result.height), (1080, 1920))
        self.assertFalse(result.has_audio)
        self.assertLessEqual(abs(result.duration_ms - 5_000), 500)
        self.assertEqual(receipt["fingerprint"], plan.fingerprint)
        self.assertEqual(receipt["sha256"], result.sha256)


if __name__ == "__main__":
    unittest.main()
