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
    _list_windows_voices,
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

    @staticmethod
    def audio_readback(
        duration_ms: int,
        stream_sha256: str,
        *,
        codec_name: str = "aac",
        sample_rate: int = 48_000,
        channels: int = 2,
        format_name: str = "mov,mp4,m4a,3gp,3g2,mj2",
    ) -> AudioReadback:
        return AudioReadback(
            duration_ms=duration_ms,
            stream_sha256=stream_sha256,
            codec_name=codec_name,
            sample_rate=sample_rate,
            channels=channels,
            format_name=format_name,
        )

    def test_choose_chinese_voice_prefers_simplified_chinese(self) -> None:
        selected = choose_chinese_voice(
            (
                SystemVoice("English", "en-US"),
                SystemVoice("Taiwan", "zh-TW"),
                SystemVoice("Mainland", "zh-CN"),
            )
        )
        self.assertEqual(selected.id, "Mainland")

    def test_choose_chinese_voice_prefers_hans_locale(self) -> None:
        selected = choose_chinese_voice(
            (
                SystemVoice("Macao", "zh-MO"),
                SystemVoice("Simplified", "zh_Hans"),
            )
        )
        self.assertEqual(selected.id, "Simplified")

    def test_choose_chinese_voice_accepts_macao_locale(self) -> None:
        selected = choose_chinese_voice(
            (
                SystemVoice("English", "en-US"),
                SystemVoice("Macao", "zh-MO"),
            )
        )
        self.assertEqual(selected.id, "Macao")

    def test_choose_chinese_voice_stably_sorts_mixed_language_voices(self) -> None:
        selected = choose_chinese_voice(
            (
                SystemVoice("Japanese", "ja-JP"),
                SystemVoice("Macao Z", "zh-MO"),
                SystemVoice("English", "en-US"),
                SystemVoice("Traditional Z", "zh-Hant"),
                SystemVoice("Traditional A", "ZH_hant"),
            )
        )
        self.assertEqual(selected.id, "Traditional A")

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

    def test_macos_voice_list_accepts_script_and_macao_locales(self) -> None:
        voices = _parse_macos_voices(
            "Simplified            zh_Hans  # 你好！\n"
            "Macao                 zh_MO    # 你好！\n"
            "English               en_US    # Hello!\n"
        )
        self.assertEqual(
            voices,
            (
                SystemVoice("Simplified", "zh-Hans"),
                SystemVoice("Macao", "zh-MO"),
                SystemVoice("English", "en-US"),
            ),
        )

    def test_windows_voice_parser_accepts_zero_voices(self) -> None:
        self.assertEqual(_parse_windows_voices("[]"), ())

    def test_windows_voice_parser_normalizes_one_voice_object(self) -> None:
        self.assertEqual(
            _parse_windows_voices(
                '{"id":"Microsoft Huihui Desktop","locale":"zh-CN"}'
            ),
            (SystemVoice("Microsoft Huihui Desktop", "zh-CN"),),
        )

    def test_windows_voice_parser_accepts_multiple_voices(self) -> None:
        self.assertEqual(
            _parse_windows_voices(
                '[{"id":"Microsoft Huihui Desktop","locale":"zh-CN"},'
                '{"id":"Microsoft Tracy Desktop","locale":"zh-HK"}]'
            ),
            (
                SystemVoice("Microsoft Huihui Desktop", "zh-CN"),
                SystemVoice("Microsoft Tracy Desktop", "zh-HK"),
            ),
        )

    def test_windows_voice_discovery_forces_json_array_at_powershell_boundary(self) -> None:
        def runner(command: list[str], **_kwargs: object):
            script = command[-1]
            self.assertIn("$voices = @(", script)
            self.assertIn("ConvertTo-Json -InputObject $voices -Compress", script)
            return subprocess.CompletedProcess(command, 0, stdout="[]", stderr="")

        self.assertEqual(_list_windows_voices(runner=runner), ())

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
                self.audio_readback(
                    3_200,
                    "1" * 64,
                    codec_name="pcm_s16be",
                    sample_rate=22_050,
                    channels=1,
                    format_name="aiff",
                ),
                self.audio_readback(5_000, "2" * 64),
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
        self.assertEqual(artifact.codec_name, "aac")
        self.assertEqual(artifact.sample_rate, 48_000)
        self.assertEqual(artifact.channels, 2)
        self.assertIn("m4a", artifact.format_name.split(","))
        self.assertEqual(artifact.to_dict()["codec_name"], "aac")
        self.assertEqual(artifact.to_dict()["sample_rate"], 48_000)
        self.assertEqual(artifact.to_dict()["channels"], 2)
        self.assertIn("m4a", str(artifact.to_dict()["format_name"]).split(","))
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
                return_value=self.audio_readback(
                    speech_duration_ms,
                    "1" * 64,
                    codec_name="pcm_s16be",
                    sample_rate=22_050,
                    channels=1,
                    format_name="aiff",
                ),
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

    def test_audio_readback_records_stream_and_container_format(self) -> None:
        audio_path = self.root / "master.m4a"
        audio_path.write_bytes(b"audio")
        expected_format = "mov,mp4,m4a,3gp,3g2,mj2"

        def format_runner(command: list[str], **_kwargs: object):
            if Path(command[0]) == self.runtime.ffprobe:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=(
                        '{"streams":[{"codec_type":"audio","codec_name":"aac",'
                        '"sample_rate":"48000","channels":2,"duration":"5.001"}],'
                        f'"format":{{"duration":"5.001","format_name":"{expected_format}"}}}}'
                    ),
                    stderr="",
                )
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=f'SHA256={"a" * 64}\n',
                stderr="",
            )

        readback = probe_audio_readback(
            audio_path,
            runtime=self.runtime,
            runner=format_runner,
        )
        self.assertEqual(
            readback,
            self.audio_readback(5_001, "a" * 64, format_name=expected_format),
        )

    def test_master_with_wrong_audio_format_is_rejected_before_install(self) -> None:
        def create_raw(_text_path, raw_path, _voice, **_kwargs):
            Path(raw_path).write_bytes(b"raw")

        def create_master(command: list[str], **_kwargs: object):
            Path(command[-1]).write_bytes(b"master")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        invalid_formats = (
            {"codec_name": "mp3"},
            {"sample_rate": 44_100},
            {"channels": 1},
            {"format_name": "mp3"},
        )
        for index, overrides in enumerate(invalid_formats):
            output_dir = self.root / f"wrong-format-{index}"
            with self.subTest(**overrides), patch(
                "app_core.narration_service._list_macos_voices",
                return_value=(SystemVoice("Test Chinese", "zh-CN"),),
            ), patch(
                "app_core.narration_service._synthesize_macos_raw",
                side_effect=create_raw,
            ), patch(
                "app_core.narration_service.probe_audio_readback",
                side_effect=(
                    self.audio_readback(
                        3_200,
                        "1" * 64,
                        codec_name="pcm_s16be",
                        sample_rate=22_050,
                        channels=1,
                        format_name="aiff",
                    ),
                    self.audio_readback(5_000, "2" * 64, **overrides),
                ),
            ), self.assertRaises(MontageFailure) as caught:
                synthesize_system_narration(
                    "短文案",
                    output_dir,
                    runtime=self.runtime,
                    platform_name="darwin",
                    runner=create_master,
                )
            self.assertEqual(
                caught.exception.code,
                "montage_narration_audio_readback_failed",
            )
            self.assertFalse((output_dir / "master.m4a").exists())

    def test_output_directory_and_text_write_errors_have_stable_error(self) -> None:
        for operation, patcher in (
            ("output directory", patch.object(Path, "mkdir", side_effect=PermissionError("denied"))),
            ("temporary text", patch.object(Path, "write_text", side_effect=PermissionError("denied"))),
        ):
            with self.subTest(operation=operation), patcher, self.assertRaises(Exception) as caught:
                synthesize_system_narration(
                    "不能泄漏的文案",
                    self.root / f"failure-{operation}",
                    runtime=self.runtime,
                    platform_name="darwin",
                )
            self.assertIsInstance(caught.exception, MontageFailure)
            self.assertEqual(
                caught.exception.code,
                "montage_narration_synthesis_failed",
            )
            self.assertNotIn("不能泄漏的文案", str(caught.exception))

    def test_output_path_resolution_error_has_stable_error(self) -> None:
        with patch.object(
            Path,
            "resolve",
            side_effect=PermissionError("denied"),
        ), self.assertRaises(Exception) as caught:
            synthesize_system_narration(
                "不能泄漏的文案",
                self.root / "resolve-failure",
                runtime=self.runtime,
                platform_name="darwin",
            )
        self.assertIsInstance(caught.exception, MontageFailure)
        self.assertEqual(caught.exception.code, "montage_narration_synthesis_failed")
        self.assertNotIn("不能泄漏的文案", str(caught.exception))

    def test_windows_script_write_error_has_stable_error(self) -> None:
        text_path = self.root / "narration.txt"
        text_path.write_text("测试", encoding="utf-8")
        with patch.object(
            Path,
            "write_text",
            side_effect=PermissionError("denied"),
        ), self.assertRaises(Exception) as caught:
            _synthesize_windows_raw(
                text_path,
                self.root / "raw.wav",
                SystemVoice("Test Chinese", "zh-CN"),
            )
        self.assertIsInstance(caught.exception, MontageFailure)
        self.assertEqual(caught.exception.code, "montage_narration_synthesis_failed")

    def test_replace_error_has_stable_error_and_leaves_no_new_master(self) -> None:
        output_dir = self.root / "replace-failure"

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
                self.audio_readback(
                    3_200,
                    "1" * 64,
                    codec_name="pcm_s16be",
                    sample_rate=22_050,
                    channels=1,
                    format_name="aiff",
                ),
                self.audio_readback(5_000, "2" * 64),
            ),
        ), patch(
            "app_core.narration_service.os.replace",
            side_effect=PermissionError("denied"),
        ), self.assertRaises(Exception) as caught:
            synthesize_system_narration(
                "短文案",
                output_dir,
                runtime=self.runtime,
                platform_name="darwin",
                runner=create_master,
            )
        self.assertIsInstance(caught.exception, MontageFailure)
        self.assertEqual(caught.exception.code, "montage_narration_synthesis_failed")
        self.assertFalse((output_dir / "master.m4a").exists())

    def test_master_hash_is_calculated_before_atomic_replace(self) -> None:
        output_dir = self.root / "hash-order"
        hashed_paths: list[Path] = []

        def create_raw(_text_path, raw_path, _voice, **_kwargs):
            Path(raw_path).write_bytes(b"raw")

        def create_master(command: list[str], **_kwargs: object):
            Path(command[-1]).write_bytes(b"master")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        def record_hash(path: Path) -> str:
            hashed_paths.append(Path(path))
            return "3" * 64

        with patch(
            "app_core.narration_service._list_macos_voices",
            return_value=(SystemVoice("Test Chinese", "zh-CN"),),
        ), patch(
            "app_core.narration_service._synthesize_macos_raw",
            side_effect=create_raw,
        ), patch(
            "app_core.narration_service.probe_audio_readback",
            side_effect=(
                self.audio_readback(
                    3_200,
                    "1" * 64,
                    codec_name="pcm_s16be",
                    sample_rate=22_050,
                    channels=1,
                    format_name="aiff",
                ),
                self.audio_readback(5_000, "2" * 64),
            ),
        ), patch(
            "app_core.narration_service._sha256_file",
            side_effect=record_hash,
        ):
            artifact = synthesize_system_narration(
                "短文案",
                output_dir,
                runtime=self.runtime,
                platform_name="darwin",
                runner=create_master,
            )
        self.assertEqual(artifact.sha256, "3" * 64)
        self.assertEqual(len(hashed_paths), 1)
        self.assertNotEqual(hashed_paths[0], artifact.master_path)
        self.assertFalse(hashed_paths[0].exists())
        self.assertTrue(artifact.master_path.is_file())

    def test_hash_failure_preserves_existing_master(self) -> None:
        output_dir = self.root / "hash-failure"
        output_dir.mkdir()
        master_path = output_dir / "master.m4a"
        master_path.write_bytes(b"existing-master")

        def create_raw(_text_path, raw_path, _voice, **_kwargs):
            Path(raw_path).write_bytes(b"raw")

        def create_master(command: list[str], **_kwargs: object):
            Path(command[-1]).write_bytes(b"new-master")
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
                self.audio_readback(
                    3_200,
                    "1" * 64,
                    codec_name="pcm_s16be",
                    sample_rate=22_050,
                    channels=1,
                    format_name="aiff",
                ),
                self.audio_readback(5_000, "2" * 64),
            ),
        ), patch(
            "app_core.narration_service._sha256_file",
            side_effect=PermissionError("denied"),
        ), self.assertRaises(Exception) as caught:
            synthesize_system_narration(
                "短文案",
                output_dir,
                runtime=self.runtime,
                platform_name="darwin",
                runner=create_master,
            )
        self.assertIsInstance(caught.exception, MontageFailure)
        self.assertEqual(caught.exception.code, "montage_narration_synthesis_failed")
        self.assertEqual(master_path.read_bytes(), b"existing-master")


if __name__ == "__main__":
    unittest.main()
