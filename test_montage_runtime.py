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
from app_core.narration_service import AudioReadback, NarrationArtifact, probe_audio_readback
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

    def test_narration_segments_always_drop_source_audio(self) -> None:
        ffmpeg, ffprobe = self.fixed_runtime_files()
        runtime = MontageRuntime(ffmpeg=ffmpeg, ffprobe=ffprobe)

        command = _segment_command(
            runtime,
            source_path=self.root / "spoken.mp4",
            start_ms=0,
            duration_ms=1_000,
            source_has_audio=True,
            audio_mode="narration",
            output_path=self.root / "segment.mp4",
        )

        self.assertIn("-an", command)
        self.assertNotIn("-filter_complex", command)

    def test_narration_render_requires_matching_audio_stream_hash(self) -> None:
        ffmpeg, ffprobe = self.fixed_runtime_files()
        runtime = MontageRuntime(ffmpeg=ffmpeg, ffprobe=ffprobe)
        source = self.root / "source.mp4"
        source.write_bytes(b"source")
        asset = VideoAsset(
            local_path=source.resolve(), sha256="a" * 64, duration_ms=10_000,
            width=1920, height=1080, fps=30, has_audio=True,
        )
        request = MontageRequest.from_mapping(
            {
                "source_paths": [str(source)], "output_count": 1,
                "target_duration_ms": 5_000, "clip_duration_ms": 1_000,
                "allow_reuse": False, "audio_mode": "narration",
                "source_audio_confirmed": False, "narration_text": "测试解说",
                "seed": 17, "title_template": "", "body_template": "",
            }
        )
        plan = plan_montages(request, (asset,))[0]
        master = self.root / "master.m4a"
        master.write_bytes(b"master")
        artifact = NarrationArtifact(
            master_path=master, voice_id="Test Chinese", voice_locale="zh-CN",
            speech_duration_ms=4_700, master_duration_ms=5_000,
            sha256="c" * 64, stream_sha256="d" * 64,
            codec_name="aac", sample_rate=48_000, channels=2, format_name="m4a",
        )
        measured = VideoAsset(
            local_path=self.root / "staged.mp4", sha256="e" * 64,
            duration_ms=5_000, width=1080, height=1920, fps=30, has_audio=True,
        )

        def create_output(command: list[str], **_kwargs: object) -> None:
            target = Path(command[-1])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"rendered")

        output = self.root / "output.mp4"
        with patch(
            "app_core.montage_runtime._run_render_command",
            side_effect=create_output,
        ), patch(
            "app_core.montage_runtime.probe_video",
            return_value=measured,
        ), patch(
            "app_core.montage_runtime.probe_audio_readback",
            return_value=AudioReadback(5_000, "f" * 64, "aac", 48_000, 2, "m4a"),
        ), self.assertRaises(MontageFailure) as caught:
            render_montage(
                plan, output, runtime=runtime,
                audio_mode="narration", narration=artifact,
            )
        self.assertEqual(caught.exception.code, "montage_narration_audio_readback_failed")

    def test_narration_render_muxes_the_shared_audio_stream_without_reencoding(self) -> None:
        ffmpeg, ffprobe = self.fixed_runtime_files()
        runtime = MontageRuntime(ffmpeg=ffmpeg, ffprobe=ffprobe)
        source = self.root / "source.mp4"
        source.write_bytes(b"source")
        asset = VideoAsset(
            local_path=source.resolve(), sha256="a" * 64, duration_ms=10_000,
            width=1920, height=1080, fps=30, has_audio=True,
        )
        request = MontageRequest.from_mapping(
            {
                "source_paths": [str(source)], "output_count": 1,
                "target_duration_ms": 5_000, "clip_duration_ms": 1_000,
                "allow_reuse": False, "audio_mode": "narration",
                "source_audio_confirmed": False, "narration_text": "测试解说",
                "seed": 17, "title_template": "", "body_template": "",
            }
        )
        plan = plan_montages(request, (asset,))[0]
        master = self.root / "master.m4a"
        master.write_bytes(b"master")
        artifact = NarrationArtifact(
            master_path=master, voice_id="Test Chinese", voice_locale="zh-CN",
            speech_duration_ms=4_700, master_duration_ms=5_000,
            sha256="c" * 64, stream_sha256="d" * 64,
            codec_name="aac", sample_rate=48_000, channels=2, format_name="m4a",
        )
        measured = VideoAsset(
            local_path=self.root / "staged.mp4", sha256="e" * 64,
            duration_ms=5_000, width=1080, height=1920, fps=30, has_audio=True,
        )
        commands: list[list[str]] = []

        def create_output(command: list[str], **_kwargs: object) -> None:
            commands.append(command)
            target = Path(command[-1])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"rendered")

        output = self.root / "output.mp4"
        with patch(
            "app_core.montage_runtime._run_render_command",
            side_effect=create_output,
        ), patch(
            "app_core.montage_runtime.probe_video",
            return_value=measured,
        ), patch(
            "app_core.montage_runtime.probe_audio_readback",
            return_value=AudioReadback(5_000, "d" * 64, "aac", 48_000, 2, "m4a"),
        ):
            receipt = render_montage(
                plan, output, runtime=runtime,
                audio_mode="narration", narration=artifact,
            )

        mux_command = commands[-1]
        self.assertNotIn("-t", mux_command)
        self.assertNotIn("-shortest", mux_command)
        self.assertEqual(mux_command[mux_command.index("-c:a") + 1], "copy")
        input_positions = [index for index, value in enumerate(mux_command) if value == "-i"]
        self.assertEqual(mux_command[input_positions[1] + 1], str(master))
        self.assertEqual(receipt["narration_sha256"], artifact.sha256)
        self.assertEqual(receipt["narration_stream_sha256"], artifact.stream_sha256)
        self.assertEqual(receipt["audio_stream_sha256"], artifact.stream_sha256)

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

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "requires system FFmpeg")
    def test_real_runtime_preserves_narration_audio_stream(self) -> None:
        ffmpeg = Path(shutil.which("ffmpeg") or "")
        source = self.root / "source.mp4"
        master = self.root / "master.m4a"
        subprocess.run(
            [
                str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "color=c=red:s=320x180:r=30",
                "-t", "6", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
            ],
            check=True,
        )
        subprocess.run(
            [
                str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                "-t", "5", "-c:a", "aac", "-ar", "48000", "-ac", "2", str(master),
            ],
            check=True,
        )
        runtime = MontageRuntime.resolve(resource_root=self.root)
        asset = probe_video(source, runtime=runtime)
        request = MontageRequest.from_mapping(
            {
                "source_paths": [str(source)], "output_count": 1,
                "target_duration_ms": 5_000, "clip_duration_ms": 1_000,
                "allow_reuse": False, "audio_mode": "narration",
                "source_audio_confirmed": False, "narration_text": "测试解说",
                "seed": 17, "title_template": "", "body_template": "",
            }
        )
        plan = plan_montages(request, (asset,))[0]
        master_audio = probe_audio_readback(master, runtime=runtime)
        artifact = NarrationArtifact(
            master_path=master,
            voice_id="Test Chinese",
            voice_locale="zh-CN",
            speech_duration_ms=master_audio.duration_ms,
            master_duration_ms=master_audio.duration_ms,
            sha256=hashlib.sha256(master.read_bytes()).hexdigest(),
            stream_sha256=master_audio.stream_sha256,
            codec_name=master_audio.codec_name,
            sample_rate=master_audio.sample_rate,
            channels=master_audio.channels,
            format_name=master_audio.format_name,
        )

        output = self.root / "output.mp4"
        receipt = render_montage(
            plan, output, runtime=runtime,
            audio_mode="narration", narration=artifact,
        )
        output_audio = probe_audio_readback(output, runtime=runtime)

        self.assertTrue(output.is_file())
        self.assertLessEqual(abs(output_audio.duration_ms - plan.total_duration_ms), 500)
        self.assertEqual(output_audio.stream_sha256, artifact.stream_sha256)
        self.assertEqual(receipt["audio_stream_sha256"], artifact.stream_sha256)


if __name__ == "__main__":
    unittest.main()
