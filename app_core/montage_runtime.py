# -*- coding: utf-8 -*-
"""轻量混剪使用的完整 FFmpeg 运行时、探测和确定性渲染。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Callable

from .montage_models import MontageFailure, MontagePlan, VideoAsset
from .narration_service import NarrationArtifact, probe_audio_readback
from .paths import ROOT_DIR


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _hidden_process_options() -> dict[str, object]:
    if sys.platform.startswith("win"):
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def _invoke(
    command: list[str],
    *,
    runner: Runner = subprocess.run,
    timeout: float = 60,
) -> subprocess.CompletedProcess[str]:
    return runner(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        **_hidden_process_options(),
    )


def _error_excerpt(value: object, limit: int = 1200) -> str:
    text = str(value or "").strip()
    return text[-limit:]


@dataclass(frozen=True, slots=True)
class MontageRuntime:
    ffmpeg: Path
    ffprobe: Path

    @classmethod
    def resolve(
        cls,
        *,
        resource_root: Path = ROOT_DIR,
        which: Callable[[str], str | None] = shutil.which,
        runner: Runner = subprocess.run,
    ) -> "MontageRuntime":
        """只接受项目完整运行时或系统 FFmpeg，明确排除 Playwright 精简版。"""

        executable_suffix = ".exe" if sys.platform.startswith("win") else ""
        fixed_bin = Path(resource_root) / "runtime" / "ffmpeg" / "bin"
        candidates: list[tuple[Path, Path]] = [
            (
                fixed_bin / f"ffmpeg{executable_suffix}",
                fixed_bin / f"ffprobe{executable_suffix}",
            )
        ]
        system_ffmpeg = which("ffmpeg")
        system_ffprobe = which("ffprobe")
        if system_ffmpeg and system_ffprobe:
            candidates.append((Path(system_ffmpeg), Path(system_ffprobe)))

        pair = next(
            ((ffmpeg, ffprobe) for ffmpeg, ffprobe in candidates if ffmpeg.is_file() and ffprobe.is_file()),
            None,
        )
        if pair is None:
            raise MontageFailure(
                "montage_ffmpeg_unavailable",
                "未找到完整剪辑运行时。当前浏览器组件不能用于混剪，请安装完整 FFmpeg 后重试",
            )
        ffmpeg, ffprobe = pair
        try:
            filters_result = _invoke(
                [str(ffmpeg), "-hide_banner", "-filters"],
                runner=runner,
                timeout=20,
            )
            encoders_result = _invoke(
                [str(ffmpeg), "-hide_banner", "-encoders"],
                runner=runner,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise MontageFailure(
                "montage_ffmpeg_unavailable",
                f"完整剪辑运行时无法启动：{exc}",
            ) from exc
        if filters_result.returncode != 0 or encoders_result.returncode != 0:
            raise MontageFailure(
                "montage_ffmpeg_unavailable",
                "完整剪辑运行时能力读取失败",
            )

        filter_text = f"{filters_result.stdout}\n{filters_result.stderr}"
        encoder_text = f"{encoders_result.stdout}\n{encoders_result.stderr}"
        required_filters = {"scale", "crop", "concat"}
        required_encoders = {"libx264", "aac"}
        missing_filters = sorted(name for name in required_filters if name not in filter_text)
        missing_encoders = sorted(name for name in required_encoders if name not in encoder_text)
        if missing_filters or missing_encoders:
            raise MontageFailure(
                "montage_ffmpeg_capability_missing",
                "当前 FFmpeg 缺少混剪所需能力",
                details={
                    "missing_filters": missing_filters,
                    "missing_encoders": missing_encoders,
                },
            )
        return cls(ffmpeg=ffmpeg.resolve(), ffprobe=ffprobe.resolve())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_fps(raw: object) -> float:
    text = str(raw or "").strip()
    if not text:
        return 0.0
    if "/" in text:
        numerator, denominator = text.split("/", 1)
        bottom = float(denominator)
        return float(numerator) / bottom if bottom else 0.0
    return float(text)


def probe_video(
    path: Path,
    *,
    runtime: MontageRuntime,
    runner: Runner = subprocess.run,
) -> VideoAsset:
    source = Path(path).resolve()
    if not source.is_file():
        raise MontageFailure("montage_source_missing", f"找不到素材：{source}")
    command = [
        str(runtime.ffprobe),
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=codec_type,width,height,r_frame_rate",
        "-of",
        "json",
        str(source),
    ]
    try:
        completed = _invoke(command, runner=runner, timeout=45)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MontageFailure(
            "montage_source_probe_failed",
            f"无法读取素材 {source.name}：{exc}",
        ) from exc
    if completed.returncode != 0:
        raise MontageFailure(
            "montage_source_probe_failed",
            f"无法读取素材 {source.name}：{_error_excerpt(completed.stderr) or '文件不是有效视频'}",
        )
    try:
        payload = json.loads(completed.stdout or "{}")
        streams = payload.get("streams") or []
        video = next(item for item in streams if item.get("codec_type") == "video")
        duration_ms = round(float((payload.get("format") or {}).get("duration")) * 1000)
        width = int(video.get("width"))
        height = int(video.get("height"))
        fps = _parse_fps(video.get("r_frame_rate"))
    except (StopIteration, TypeError, ValueError, json.JSONDecodeError, ZeroDivisionError) as exc:
        raise MontageFailure(
            "montage_source_probe_failed",
            f"素材 {source.name} 缺少有效的视频轨或时长信息",
        ) from exc
    if duration_ms <= 0 or width <= 0 or height <= 0 or fps <= 0:
        raise MontageFailure(
            "montage_source_probe_failed",
            f"素材 {source.name} 的视频信息无效",
        )
    return VideoAsset(
        local_path=source,
        sha256=sha256_file(source),
        duration_ms=duration_ms,
        width=width,
        height=height,
        fps=fps,
        has_audio=any(item.get("codec_type") == "audio" for item in streams),
    )


def _seconds(milliseconds: int) -> str:
    return f"{milliseconds / 1000:.3f}"


def _video_filter() -> str:
    return (
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,setsar=1,fps=30,setpts=PTS-STARTPTS"
    )


def _run_render_command(
    command: list[str],
    *,
    runner: Runner,
    stage: str,
    timeout: float,
) -> None:
    try:
        completed = _invoke(command, runner=runner, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MontageFailure(
            "montage_render_failed",
            f"{stage}失败：{exc}",
            details={"stage": stage},
        ) from exc
    if completed.returncode != 0:
        raise MontageFailure(
            "montage_render_failed",
            f"{stage}失败：{_error_excerpt(completed.stderr) or 'FFmpeg 未返回原因'}",
            details={"stage": stage},
        )


def _segment_command(
    runtime: MontageRuntime,
    *,
    source_path: Path,
    start_ms: int,
    duration_ms: int,
    source_has_audio: bool,
    audio_mode: str,
    output_path: Path,
) -> list[str]:
    base = [
        str(runtime.ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        _seconds(start_ms),
        "-i",
        str(source_path),
    ]
    duration = _seconds(duration_ms)
    encoding = [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "21",
        "-pix_fmt",
        "yuv420p",
    ]
    if audio_mode in {"mute", "narration"}:
        return base + [
            "-t",
            duration,
            "-vf",
            _video_filter(),
            "-an",
            *encoding,
            str(output_path),
    ]
    if source_has_audio:
        fade_duration_ms = min(80, max(1, duration_ms // 4))
        fade_duration = _seconds(fade_duration_ms)
        fade_out_start = _seconds(max(0, duration_ms - fade_duration_ms))
        filter_complex = (
            f"[0:v:0]{_video_filter()}[v];"
            f"[0:a:0]aresample=48000,apad,atrim=duration={duration},"
            "asetpts=PTS-STARTPTS,"
            f"afade=t=in:st=0:d={fade_duration},"
            f"afade=t=out:st={fade_out_start}:d={fade_duration}[a]"
        )
        return base + [
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-t",
            duration,
            *encoding,
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            str(output_path),
        ]
    return base + [
        "-f",
        "lavfi",
        "-t",
        duration,
        "-i",
        "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-t",
        duration,
        "-vf",
        _video_filter(),
        *encoding,
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-shortest",
        str(output_path),
    ]


def render_montage(
    plan: MontagePlan,
    output_path: Path,
    *,
    runtime: MontageRuntime,
    audio_mode: str,
    narration: NarrationArtifact | None = None,
    runner: Runner = subprocess.run,
    progress: Callable[[dict[str, object]], None] | None = None,
) -> dict[str, object]:
    """把冻结计划渲染为正式 MP4；通过回读前不会写入目标文件。"""

    if audio_mode not in {"mute", "source", "narration"}:
        raise MontageFailure("montage_audio_mode_invalid", "音频模式无效")
    if audio_mode == "narration" and narration is None:
        raise MontageFailure("montage_narration_audio_readback_failed", "本批次缺少配音主音轨")
    target = Path(output_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix=".montage-", dir=target.parent) as temp_name:
            temp_root = Path(temp_name)
            segments: list[Path] = []
            for position, clip in enumerate(plan.clips, start=1):
                segment = temp_root / f"segment-{position:03d}.mp4"
                command = _segment_command(
                    runtime,
                    source_path=clip.source_path,
                    start_ms=clip.start_ms,
                    duration_ms=clip.duration_ms,
                    source_has_audio=clip.has_audio,
                    audio_mode=audio_mode,
                    output_path=segment,
                )
                _run_render_command(
                    command,
                    runner=runner,
                    stage=f"第 {position}/{len(plan.clips)} 个镜头渲染",
                    timeout=max(90, clip.duration_ms / 1000 * 20),
                )
                if not segment.is_file() or segment.stat().st_size == 0:
                    raise MontageFailure("montage_render_failed", "镜头渲染没有生成有效文件")
                segments.append(segment)
                if progress:
                    progress(
                        {
                            "stage": "rendering",
                            "clip": position,
                            "clip_count": len(plan.clips),
                            "output_index": plan.index,
                        }
                    )

            concat_list = temp_root / "segments.txt"
            concat_list.write_text(
                "".join(f"file '{segment.as_posix()}'\n" for segment in segments),
                encoding="utf-8",
            )
            silent_video = temp_root / "silent-video.mp4"
            staged_output = temp_root / "video.mp4"
            _run_render_command(
                [
                    str(runtime.ffmpeg),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(concat_list),
                    "-c",
                    "copy",
                    "-movflags",
                    "+faststart",
                    str(silent_video if audio_mode == "narration" else staged_output),
                ],
                runner=runner,
                stage="成片合并",
                timeout=max(120, plan.total_duration_ms / 1000 * 10),
            )
            if audio_mode == "narration":
                assert narration is not None
                _run_render_command(
                    [
                        str(runtime.ffmpeg),
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-i",
                        str(silent_video),
                        "-i",
                        str(narration.master_path),
                        "-map",
                        "0:v:0",
                        "-map",
                        "1:a:0",
                        "-c:v",
                        "copy",
                        "-c:a",
                        "copy",
                        "-movflags",
                        "+faststart",
                        str(staged_output),
                    ],
                    runner=runner,
                    stage="配音主音轨合并",
                    timeout=max(120, plan.total_duration_ms / 1000 * 10),
                )
            result = probe_video(staged_output, runtime=runtime, runner=runner)
            problems: list[str] = []
            if (result.width, result.height) != (1080, 1920):
                problems.append(f"尺寸为 {result.width}×{result.height}")
            if abs(result.duration_ms - plan.total_duration_ms) > 500:
                problems.append(f"时长为 {result.duration_ms / 1000:.2f} 秒")
            if audio_mode == "mute" and result.has_audio:
                problems.append("静音模式仍包含音轨")
            if audio_mode == "source" and not result.has_audio:
                problems.append("原声模式缺少音轨")
            audio_stream_sha256: str | None = None
            if audio_mode == "narration":
                assert narration is not None
                audio = probe_audio_readback(staged_output, runtime=runtime, runner=runner)
                audio_stream_sha256 = audio.stream_sha256
                if abs(audio.duration_ms - plan.total_duration_ms) > 500:
                    problems.append(f"配音时长为 {audio.duration_ms / 1000:.2f} 秒")
                if audio.stream_sha256 != narration.stream_sha256:
                    problems.append("配音音轨内容与批次主音轨不一致")
                if problems:
                    raise MontageFailure(
                        "montage_narration_audio_readback_failed",
                        "配音成片回读未通过：" + "；".join(problems),
                        details={"problems": problems},
                    )
            if problems:
                raise MontageFailure(
                    "montage_output_validation_failed",
                    "成片回读未通过：" + "；".join(problems),
                    details={"problems": problems},
                )
            digest = result.sha256
            os.replace(staged_output, target)
    except MontageFailure:
        raise
    except OSError as exc:
        raise MontageFailure("montage_output_write_failed", f"成片写入失败：{exc}") from exc

    return {
        "status": "success",
        "output_path": str(target),
        "sha256": digest,
        "fingerprint": plan.fingerprint,
        "duration_ms": result.duration_ms,
        "expected_duration_ms": plan.total_duration_ms,
        "width": 1080,
        "height": 1920,
        "audio_mode": audio_mode,
        "narration_sha256": narration.sha256 if narration else None,
        "narration_stream_sha256": narration.stream_sha256 if narration else None,
        "audio_stream_sha256": audio_stream_sha256 if audio_mode == "narration" else None,
    }
