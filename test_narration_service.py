# -*- coding: utf-8 -*-
"""本机系统配音服务测试。"""

from pathlib import Path
import subprocess
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from app_core.montage_models import MontageFailure
from app_core.narration_service import (
    AudioReadback,
    SystemVoice,
    choose_chinese_voice,
    probe_audio_readback,
    synthesize_system_narration,
    _parse_macos_voices,
    _parse_windows_voices,
    _synthesize_macos_raw,
    _synthesize_windows_raw,
)


class NarrationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.runtime = SimpleNamespace(
            ffmpeg=self.root / "ffmpeg",
            ffprobe=self.root / "ffprobe",
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_choose_chinese_voice_prefers_simplified_chinese(self) -> None:
        selected = choose_chinese_voice(
            (
                SystemVoice("English", "en-US"),
                SystemVoice("Taiwan", "zh-TW"),
                SystemVoice("Mainland", "zh-CN"),
            )
        )
        self.assertEqual(selected.id, "Mainland")

    def test_choose_chinese_voice_never_falls_back_to_english(self) -> None:
        with self.assertRaises(MontageFailure) as caught:
            choose_chinese_voice((SystemVoice("English", "en-US"),))
        self.assertEqual(caught.exception.code, "montage_narration_voice_unavailable")

    def test_macos_and_windows_voice_lists_parse_to_the_same_contract(self) -> None:
        mac = _parse_macos_voices(
            "Ting-Ting             zh_CN    # 你好！我是婷婷。\n"
            "Samantha              en_US    # Hello! My name is Samantha.\n"
        )
        windows = _parse_windows_voices(
            '[{"id":"Microsoft Huihui Desktop","locale":"zh-CN"}]'
        )
        self.assertEqual(mac[0], SystemVoice("Ting-Ting", "zh-CN"))
        self.assertEqual(windows, (SystemVoice("Microsoft Huihui Desktop", "zh-CN"),))

    def test_macos_and_windows_pass_text_by_file_not_command_line(self) -> None:
        secret_text = "这段文案不能进入命令行"
        text_path = self.root / "narration.txt"
        text_path.write_text(secret_text, encoding="utf-8")
        voice = SystemVoice("Test Chinese", "zh-CN")
        mac_output = self.root / "raw.aiff"
        windows_output = self.root / "raw.wav"
        commands: list[list[str]] = []

        def mac_runner(command: list[str], **_kwargs: object):
            commands.append(command)
            mac_output.write_bytes(b"aiff")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        def windows_runner(command: list[str], **_kwargs: object):
            commands.append(command)
            windows_output.write_bytes(b"wave")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        _synthesize_macos_raw(text_path, mac_output, voice, runner=mac_runner)
        _synthesize_windows_raw(text_path, windows_output, voice, runner=windows_runner)

        self.assertTrue(mac_output.is_file())
        self.assertTrue(windows_output.is_file())
        self.assertTrue(
            all(secret_text not in argument for command in commands for argument in command)
        )
        self.assertTrue(all(str(text_path) in command for command in commands))

    def test_master_duration_is_speech_plus_tail_with_five_second_floor(self) -> None:
        def create_raw(_text_path, raw_path, _voice, **_kwargs):
            Path(raw_path).write_bytes(b"raw")

        def create_master(command: list[str], **_kwargs: object):
            Path(command[-1]).write_bytes(b"master")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with patch(
            "app_core.narration_service._list_macos_voices",
            return_value=(SystemVoice("Test Chinese", "zh-CN"),),
        ), patch(
            "app_core.narration_service._synthesize_macos_raw",
            side_effect=create_raw,
        ), patch(
            "app_core.narration_service.probe_audio_readback",
            side_effect=(
                AudioReadback(3_200, "1" * 64),
                AudioReadback(5_000, "2" * 64),
            ),
        ):
            artifact = synthesize_system_narration(
                "短文案",
                self.root / "narration",
                runtime=self.runtime,
                platform_name="darwin",
                runner=create_master,
            )
        self.assertEqual(artifact.speech_duration_ms, 3_200)
        self.assertEqual(artifact.master_duration_ms, 5_000)
        self.assertEqual(artifact.stream_sha256, "2" * 64)
        self.assertTrue(artifact.master_path.is_file())

    def test_zero_or_over_180_second_narration_fails_before_normalization(self) -> None:
        def create_raw(_text_path, raw_path, _voice, **_kwargs):
            Path(raw_path).write_bytes(b"raw")

        def normalization_must_not_run(*_args, **_kwargs):
            raise AssertionError("无效配音时长不应启动 FFmpeg 规范化")

        for speech_duration_ms in (0, 179_800):
            with self.subTest(speech_duration_ms=speech_duration_ms), patch(
                "app_core.narration_service._list_macos_voices",
                return_value=(SystemVoice("Test Chinese", "zh-CN"),),
            ), patch(
                "app_core.narration_service._synthesize_macos_raw",
                side_effect=create_raw,
            ), patch(
                "app_core.narration_service.probe_audio_readback",
                return_value=AudioReadback(speech_duration_ms, "1" * 64),
            ), self.assertRaises(MontageFailure) as caught:
                synthesize_system_narration(
                    "测试文案",
                    self.root / f"invalid-{speech_duration_ms}",
                    runtime=self.runtime,
                    platform_name="darwin",
                    runner=normalization_must_not_run,
                )
            self.assertEqual(caught.exception.code, "montage_narration_duration_invalid")

    def test_unsupported_platform_has_stable_error(self) -> None:
        with self.assertRaises(MontageFailure) as caught:
            synthesize_system_narration(
                "测试",
                self.root / "unsupported",
                runtime=self.runtime,
                platform_name="linux",
            )
        self.assertEqual(caught.exception.code, "montage_narration_platform_unsupported")

    def test_raw_synthesis_nonzero_exit_has_stable_error(self) -> None:
        text_path = self.root / "narration.txt"
        text_path.write_text("测试", encoding="utf-8")
        output_path = self.root / "raw.aiff"

        def failed_runner(command: list[str], **_kwargs: object):
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="say failed")

        with self.assertRaises(MontageFailure) as caught:
            _synthesize_macos_raw(
                text_path,
                output_path,
                SystemVoice("Test Chinese", "zh-CN"),
                runner=failed_runner,
            )
        self.assertEqual(caught.exception.code, "montage_narration_synthesis_failed")

    def test_audio_without_readable_stream_has_stable_error(self) -> None:
        audio_path = self.root / "broken.m4a"
        audio_path.write_bytes(b"broken")

        def no_stream_runner(command: list[str], **_kwargs: object):
            return subprocess.CompletedProcess(
                command, 0, stdout='{"streams": [], "format": {}}', stderr=""
            )

        with self.assertRaises(MontageFailure) as caught:
            probe_audio_readback(audio_path, runtime=self.runtime, runner=no_stream_runner)
        self.assertEqual(caught.exception.code, "montage_narration_audio_readback_failed")

    def test_audio_with_unusable_json_shape_has_stable_error(self) -> None:
        audio_path = self.root / "broken-shape.m4a"
        audio_path.write_bytes(b"broken")

        for payload in (
            "[]",
            '{"streams":[{"codec_type":"audio","duration":"Infinity"}],"format":{}}',
        ):
            with self.subTest(payload=payload):
                def invalid_probe_runner(command: list[str], **_kwargs: object):
                    return subprocess.CompletedProcess(
                        command, 0, stdout=payload, stderr=""
                    )

                with self.assertRaises(MontageFailure) as caught:
                    probe_audio_readback(
                        audio_path,
                        runtime=self.runtime,
                        runner=invalid_probe_runner,
                    )
                self.assertEqual(
                    caught.exception.code,
                    "montage_narration_audio_readback_failed",
                )


if __name__ == "__main__":
    unittest.main()
