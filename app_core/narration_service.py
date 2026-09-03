# -*- coding: utf-8 -*-
"""使用 macOS 或 Windows 系统中文声音生成本地配音。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Callable, Protocol, Sequence

from .montage_models import MontageFailure


Runner = Callable[..., subprocess.CompletedProcess[str]]
_CHINESE_LOCALE_PRIORITIES = {
    "zh-cn": 0,
    "zh-sg": 1,
    "zh-tw": 2,
    "zh-hk": 3,
}
_WINDOWS_SYNTHESIS_SCRIPT = """param([string]$TextPath, [string]$OutputPath, [string]$VoiceName)
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
  $synth.SelectVoice($VoiceName)
  $text = [IO.File]::ReadAllText($TextPath, [Text.Encoding]::UTF8)
  $synth.SetOutputToWaveFile($OutputPath)
  $synth.Speak($text)
} finally {
  $synth.Dispose()
}
"""
_WINDOWS_VOICE_LIST_SCRIPT = """Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
  @($synth.GetInstalledVoices() | ForEach-Object {
    [PSCustomObject]@{id=$_.VoiceInfo.Name; locale=$_.VoiceInfo.Culture.Name}
  }) | ConvertTo-Json -Compress
} finally {
  $synth.Dispose()
}
"""


class AudioRuntime(Protocol):
    ffmpeg: Path
    ffprobe: Path


@dataclass(frozen=True, slots=True)
class SystemVoice:
    id: str
    locale: str


@dataclass(frozen=True, slots=True)
class AudioReadback:
    duration_ms: int
    stream_sha256: str


@dataclass(frozen=True, slots=True)
class NarrationArtifact:
    master_path: Path
    voice_id: str
    voice_locale: str
    speech_duration_ms: int
    master_duration_ms: int
    sha256: str
    stream_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "status": "success",
            "master_path": str(self.master_path),
            "voice_id": self.voice_id,
            "voice_locale": self.voice_locale,
            "speech_duration_ms": self.speech_duration_ms,
            "master_duration_ms": self.master_duration_ms,
            "sha256": self.sha256,
            "stream_sha256": self.stream_sha256,
        }


def _normalized_locale(locale: str) -> str:
    return locale.replace("_", "-").casefold()


def choose_chinese_voice(voices: Sequence[SystemVoice]) -> SystemVoice:
    def rank(voice: SystemVoice) -> tuple[int, str, str]:
        locale = _normalized_locale(voice.locale)
        return (_CHINESE_LOCALE_PRIORITIES[locale], locale, voice.id.casefold())

    chinese = [
        voice
        for voice in voices
        if _normalized_locale(voice.locale) in _CHINESE_LOCALE_PRIORITIES
    ]
    if not chinese:
        raise MontageFailure(
            "montage_narration_voice_unavailable",
            "本机没有可用的中文系统声音",
        )
    return min(chinese, key=rank)


def _parse_macos_voices(output: str) -> tuple[SystemVoice, ...]:
    voices: list[SystemVoice] = []
    for line in str(output or "").splitlines():
        match = re.match(r"^(.+?)\s+([A-Za-z]{2}[_-][A-Za-z]{2})\s+#", line)
        if not match:
            continue
        name, locale = match.groups()
        language, region = locale.replace("_", "-").split("-", 1)
        voices.append(SystemVoice(name.strip(), f"{language.lower()}-{region.upper()}"))
    return tuple(voices)


def _parse_windows_voices(output: str) -> tuple[SystemVoice, ...]:
    try:
        payload = json.loads(str(output or ""))
    except (TypeError, json.JSONDecodeError):
        return ()
    if not isinstance(payload, list):
        return ()
    voices: list[SystemVoice] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        voice_id = item.get("id")
        locale = item.get("locale")
        if isinstance(voice_id, str) and voice_id.strip() and isinstance(locale, str):
            voices.append(SystemVoice(voice_id.strip(), locale.replace("_", "-")))
    return tuple(voices)


def _invoke(
    command: list[str],
    *,
    runner: Runner,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    options: dict[str, object] = {}
    if sys.platform.startswith("win"):
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    return runner(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        **options,
    )


def _list_macos_voices(*, runner: Runner = subprocess.run) -> tuple[SystemVoice, ...]:
    try:
        completed = _invoke(
            ["/usr/bin/say", "-v", "?"],
            runner=runner,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MontageFailure(
            "montage_narration_synthesis_failed",
            "无法读取 macOS 系统声音",
        ) from exc
    if completed.returncode != 0:
        raise MontageFailure(
            "montage_narration_synthesis_failed",
            "无法读取 macOS 系统声音",
        )
    return _parse_macos_voices(completed.stdout)


def _list_windows_voices(*, runner: Runner = subprocess.run) -> tuple[SystemVoice, ...]:
    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        _WINDOWS_VOICE_LIST_SCRIPT,
    ]
    try:
        completed = _invoke(command, runner=runner, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MontageFailure(
            "montage_narration_synthesis_failed",
            "无法读取 Windows 系统声音",
        ) from exc
    if completed.returncode != 0:
        raise MontageFailure(
            "montage_narration_synthesis_failed",
            "无法读取 Windows 系统声音",
        )
    return _parse_windows_voices(completed.stdout)


def _synthesize_macos_raw(
    text_path: Path,
    raw_output_path: Path,
    voice: SystemVoice,
    *,
    runner: Runner = subprocess.run,
) -> None:
    command = [
        "/usr/bin/say",
        "-v",
        voice.id,
        "-f",
        str(text_path),
        "-o",
        str(raw_output_path),
    ]
    _run_raw_synthesis(command, raw_output_path, runner=runner)


def _synthesize_windows_raw(
    text_path: Path,
    raw_output_path: Path,
    voice: SystemVoice,
    *,
    runner: Runner = subprocess.run,
) -> None:
    script_path = Path(text_path).parent / "system-speech.ps1"
    script_path.write_text(_WINDOWS_SYNTHESIS_SCRIPT, encoding="utf-8")
    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
        str(text_path),
        str(raw_output_path),
        voice.id,
    ]
    _run_raw_synthesis(command, raw_output_path, runner=runner)


def _run_raw_synthesis(
    command: list[str],
    raw_output_path: Path,
    *,
    runner: Runner,
) -> None:
    try:
        completed = _invoke(command, runner=runner, timeout=180)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MontageFailure(
            "montage_narration_synthesis_failed",
            "系统配音生成失败",
        ) from exc
    if (
        completed.returncode != 0
        or not Path(raw_output_path).is_file()
        or Path(raw_output_path).stat().st_size <= 0
    ):
        raise MontageFailure(
            "montage_narration_synthesis_failed",
            "系统配音生成失败",
        )


def probe_audio_readback(
    path: Path,
    *,
    runtime: AudioRuntime,
    runner: Runner = subprocess.run,
) -> AudioReadback:
    source = Path(path)
    probe_command = [
        str(runtime.ffprobe),
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,duration:format=duration",
        "-of",
        "json",
        str(source),
    ]
    try:
        probed = _invoke(probe_command, runner=runner, timeout=45)
        if probed.returncode != 0:
            raise ValueError("ffprobe failed")
        payload = json.loads(probed.stdout or "{}")
        if not isinstance(payload, dict):
            raise ValueError("invalid probe payload")
        streams = payload.get("streams")
        if not isinstance(streams, list):
            raise ValueError("missing streams")
        audio_stream = next(
            stream
            for stream in streams
            if isinstance(stream, dict) and stream.get("codec_type") == "audio"
        )
        raw_duration = audio_stream.get("duration")
        if raw_duration in (None, ""):
            format_data = payload.get("format")
            if not isinstance(format_data, dict):
                raise ValueError("missing format duration")
            raw_duration = format_data.get("duration")
        duration_ms = round(float(raw_duration) * 1000)

        hash_command = [
            str(runtime.ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-c:a",
            "copy",
            "-f",
            "hash",
            "-hash",
            "sha256",
            "-",
        ]
        hashed = _invoke(hash_command, runner=runner, timeout=120)
        if hashed.returncode != 0:
            raise ValueError("stream hash failed")
        match = re.search(r"SHA256=([0-9a-fA-F]{64})(?:\s|$)", hashed.stdout or "")
        if match is None:
            raise ValueError("missing stream hash")
    except (
        OSError,
        OverflowError,
        subprocess.SubprocessError,
        StopIteration,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise MontageFailure(
            "montage_narration_audio_readback_failed",
            "配音音轨回读失败",
        ) from exc
    return AudioReadback(
        duration_ms=duration_ms,
        stream_sha256=match.group(1).lower(),
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_master(
    raw_path: Path,
    staged_master: Path,
    *,
    runtime: AudioRuntime,
    master_duration_ms: int,
    runner: Runner,
) -> None:
    seconds = f"{master_duration_ms / 1000:.3f}"
    normalize_command = [
        str(runtime.ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(raw_path),
        "-af",
        f"aresample=48000,apad,atrim=duration={seconds}",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        "-ac",
        "2",
        str(staged_master),
    ]
    try:
        completed = _invoke(normalize_command, runner=runner, timeout=180)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MontageFailure(
            "montage_narration_synthesis_failed",
            "配音主音轨生成失败",
        ) from exc
    if (
        completed.returncode != 0
        or not staged_master.is_file()
        or staged_master.stat().st_size <= 0
    ):
        raise MontageFailure(
            "montage_narration_synthesis_failed",
            "配音主音轨生成失败",
        )


def synthesize_system_narration(
    narration_text: str,
    output_dir: Path,
    *,
    runtime: AudioRuntime,
    platform_name: str | None = None,
    runner: Runner = subprocess.run,
) -> NarrationArtifact:
    platform_value = (platform_name or sys.platform).casefold()
    if platform_value != "darwin" and not platform_value.startswith("win"):
        raise MontageFailure(
            "montage_narration_platform_unsupported",
            "当前系统暂不支持本机自动配音",
        )

    target_dir = Path(output_dir).expanduser().resolve()
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".narration-", dir=target_dir.parent) as temp_name:
        temp_dir = Path(temp_name)
        text_path = temp_dir / "narration.txt"
        text_path.write_text(narration_text, encoding="utf-8")

        if platform_value == "darwin":
            voice = choose_chinese_voice(_list_macos_voices(runner=runner))
            raw_path = temp_dir / "raw.aiff"
            _synthesize_macos_raw(text_path, raw_path, voice, runner=runner)
        else:
            voice = choose_chinese_voice(_list_windows_voices(runner=runner))
            raw_path = temp_dir / "raw.wav"
            _synthesize_windows_raw(text_path, raw_path, voice, runner=runner)

        speech = probe_audio_readback(raw_path, runtime=runtime, runner=runner)
        master_duration_ms = max(5_000, speech.duration_ms + 300)
        if speech.duration_ms <= 0:
            raise MontageFailure(
                "montage_narration_duration_invalid",
                "配音时长无效",
            )
        if master_duration_ms > 180_000:
            raise MontageFailure(
                "montage_narration_duration_invalid",
                "配音超过 180 秒上限",
            )

        staged_master = temp_dir / "master.m4a"
        _normalize_master(
            raw_path,
            staged_master,
            runtime=runtime,
            master_duration_ms=master_duration_ms,
            runner=runner,
        )
        master_readback = probe_audio_readback(
            staged_master,
            runtime=runtime,
            runner=runner,
        )
        if abs(master_readback.duration_ms - master_duration_ms) > 100:
            raise MontageFailure(
                "montage_narration_audio_readback_failed",
                "配音主音轨时长回读不一致",
            )

        target_dir.mkdir(parents=True, exist_ok=True)
        master_path = target_dir / "master.m4a"
        os.replace(staged_master, master_path)
        return NarrationArtifact(
            master_path=master_path,
            voice_id=voice.id,
            voice_locale=voice.locale,
            speech_duration_ms=speech.duration_ms,
            master_duration_ms=master_duration_ms,
            sha256=_sha256_file(master_path),
            stream_sha256=master_readback.stream_sha256,
        )
