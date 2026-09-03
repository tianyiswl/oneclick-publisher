# -*- coding: utf-8 -*-
"""轻量混剪的纯数据合同、文案变体和确定性计划器。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Mapping, Sequence


VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".mkv", ".avi", ".wmv", ".flv", ".webm"})
REQUEST_FIELDS = frozenset(
    {
        "source_paths",
        "output_count",
        "target_duration_ms",
        "clip_duration_ms",
        "allow_reuse",
        "audio_mode",
        "source_audio_confirmed",
        "seed",
        "title_template",
        "body_template",
        "narration_text",
    }
)


class MontageFailure(RuntimeError):
    """带稳定错误码、可直接显示给用户的混剪失败。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.details = dict(details or {})


def _integer(raw: object, *, code: str, label: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise MontageFailure(code, f"{label}必须是整数")
    return int(raw)


@dataclass(frozen=True, slots=True)
class MontageRequest:
    source_paths: tuple[Path, ...]
    output_count: int
    target_duration_ms: int
    clip_duration_ms: int
    allow_reuse: bool
    audio_mode: str
    source_audio_confirmed: bool
    seed: int
    title_template: str
    body_template: str
    narration_text: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "MontageRequest":
        unknown = sorted(set(raw) - REQUEST_FIELDS)
        if unknown:
            raise MontageFailure(
                "montage_request_unknown_field",
                "混剪设置包含无法识别的字段：" + "、".join(unknown),
                details={"fields": unknown},
            )

        source_value = raw.get("source_paths")
        if (
            not isinstance(source_value, (list, tuple))
            or isinstance(source_value, (str, bytes))
            or not 1 <= len(source_value) <= 50
        ):
            raise MontageFailure(
                "montage_source_count_invalid",
                "请选择 1–50 个视频素材",
            )
        source_paths: list[Path] = []
        identities: set[str] = set()
        for value in source_value:
            if not isinstance(value, (str, Path)) or not str(value).strip():
                raise MontageFailure("montage_source_invalid", "素材路径无效")
            path = Path(value).expanduser()
            if not path.is_absolute():
                raise MontageFailure("montage_source_invalid", "素材必须使用本机绝对路径")
            resolved = path.resolve()
            identity = str(resolved).casefold()
            if identity in identities:
                raise MontageFailure("montage_source_duplicate", f"素材重复：{resolved.name}")
            identities.add(identity)
            if not resolved.is_file():
                raise MontageFailure("montage_source_missing", f"找不到素材：{resolved}")
            if resolved.suffix.lower() not in VIDEO_SUFFIXES:
                raise MontageFailure(
                    "montage_source_type_unsupported",
                    f"暂不支持该素材格式：{resolved.name}",
                )
            source_paths.append(resolved)

        output_count = _integer(
            raw.get("output_count"),
            code="montage_output_count_invalid",
            label="生成数量",
        )
        if not 1 <= output_count <= 50:
            raise MontageFailure("montage_output_count_invalid", "生成数量必须在 1–50 之间")

        target_duration_ms = _integer(
            raw.get("target_duration_ms"),
            code="montage_target_duration_invalid",
            label="成片时长",
        )
        if not 5_000 <= target_duration_ms <= 180_000:
            raise MontageFailure("montage_target_duration_invalid", "成片时长必须在 5–180 秒之间")

        clip_duration_ms = _integer(
            raw.get("clip_duration_ms"),
            code="montage_clip_duration_invalid",
            label="镜头时长",
        )
        if not 500 <= clip_duration_ms <= 10_000:
            raise MontageFailure(
                "montage_clip_duration_invalid",
                "镜头时长必须在 0.5–10 秒之间，且不能超过成片时长",
            )

        allow_reuse = raw.get("allow_reuse", False)
        if not isinstance(allow_reuse, bool):
            raise MontageFailure("montage_reuse_mode_invalid", "复用设置无效")

        audio_mode = str(raw.get("audio_mode") or "").strip().lower()
        if audio_mode not in {"mute", "source", "narration"}:
            raise MontageFailure(
                "montage_audio_mode_invalid",
                "音频模式只能是静音、保留环境原声或系统自动配音",
            )

        narration_value = raw.get("narration_text", "")
        if not isinstance(narration_value, str):
            raise MontageFailure("montage_narration_text_required", "配音文案必须是文字")
        narration_text = narration_value.strip()
        if audio_mode == "narration" and not narration_text:
            raise MontageFailure("montage_narration_text_required", "请输入需要配音的解说文案")
        if audio_mode != "narration":
            narration_text = ""

        if audio_mode != "narration" and clip_duration_ms > target_duration_ms:
            raise MontageFailure(
                "montage_clip_duration_invalid",
                "镜头时长不能超过成片时长",
            )

        source_audio_confirmed = raw.get("source_audio_confirmed", False)
        if not isinstance(source_audio_confirmed, bool):
            raise MontageFailure(
                "montage_source_audio_confirmation_invalid",
                "环境原声确认状态无效",
            )
        if audio_mode == "source" and not source_audio_confirmed:
            raise MontageFailure(
                "montage_source_audio_confirmation_required",
                "保留环境原声仅适合无人声素材。请确认所选素材不含连续讲话；有口播时请选择静音混剪",
            )
        if audio_mode != "source":
            source_audio_confirmed = False

        seed = _integer(raw.get("seed"), code="montage_seed_invalid", label="随机种子")
        if seed < 0:
            raise MontageFailure("montage_seed_invalid", "随机种子不能小于 0")

        title_template = raw.get("title_template", "")
        body_template = raw.get("body_template", "")
        if not isinstance(title_template, str) or not isinstance(body_template, str):
            raise MontageFailure("copy_variant_template_invalid", "标题和正文模板必须是文字")

        return cls(
            source_paths=tuple(source_paths),
            output_count=output_count,
            target_duration_ms=target_duration_ms,
            clip_duration_ms=clip_duration_ms,
            allow_reuse=allow_reuse,
            audio_mode=audio_mode,
            source_audio_confirmed=source_audio_confirmed,
            seed=seed,
            title_template=title_template.strip(),
            body_template=body_template.strip(),
            narration_text=narration_text,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "source_paths": [str(path) for path in self.source_paths],
            "output_count": self.output_count,
            "target_duration_ms": self.target_duration_ms,
            "clip_duration_ms": self.clip_duration_ms,
            "allow_reuse": self.allow_reuse,
            "audio_mode": self.audio_mode,
            "source_audio_confirmed": self.source_audio_confirmed,
            "seed": self.seed,
            "title_template": self.title_template,
            "body_template": self.body_template,
            "narration_text": self.narration_text,
        }


@dataclass(frozen=True, slots=True)
class VideoAsset:
    local_path: Path
    sha256: str
    duration_ms: int
    width: int
    height: int
    fps: float
    has_audio: bool

    def __post_init__(self) -> None:
        if len(self.sha256) != 64 or any(char not in "0123456789abcdef" for char in self.sha256.lower()):
            raise ValueError("素材 SHA-256 无效")
        if self.duration_ms <= 0 or self.width <= 0 or self.height <= 0 or self.fps <= 0:
            raise ValueError("素材元数据无效")

    def to_dict(self) -> dict[str, object]:
        return {
            "local_path": str(self.local_path),
            "sha256": self.sha256,
            "duration_ms": self.duration_ms,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "has_audio": self.has_audio,
        }


@dataclass(frozen=True, slots=True)
class CopyVariant:
    index: int
    title: str
    body: str

    def to_dict(self) -> dict[str, object]:
        return {"index": self.index, "title": self.title, "body": self.body}


@dataclass(frozen=True, slots=True)
class PlannedClip:
    source_path: Path
    source_sha256: str
    start_ms: int
    duration_ms: int
    window_duration_ms: int
    has_audio: bool

    @property
    def window_identity(self) -> str:
        return f"{self.source_sha256}:{self.start_ms}:{self.window_duration_ms}"

    def to_dict(self) -> dict[str, object]:
        return {
            "source_path": str(self.source_path),
            "source_sha256": self.source_sha256,
            "start_ms": self.start_ms,
            "duration_ms": self.duration_ms,
            "window_duration_ms": self.window_duration_ms,
            "window_identity": self.window_identity,
            "has_audio": self.has_audio,
        }


@dataclass(frozen=True, slots=True)
class MontagePlan:
    index: int
    clips: tuple[PlannedClip, ...]
    copy: CopyVariant
    total_duration_ms: int
    fingerprint: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "oneclick-montage-plan/v1",
            "index": self.index,
            "clips": [clip.to_dict() for clip in self.clips],
            "copy": self.copy.to_dict(),
            "total_duration_ms": self.total_duration_ms,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True, slots=True)
class _ParsedTemplate:
    segments: tuple[str | tuple[str, ...], ...]
    capacity: int

    def render(self, variant_index: int) -> str:
        option_index = int(variant_index)
        selected: list[str] = []
        for segment in self.segments:
            if isinstance(segment, str):
                selected.append(segment)
                continue
            selected.append(segment[option_index % len(segment)])
            option_index //= len(segment)
        return "".join(selected)


def _parse_template(template: str) -> _ParsedTemplate:
    segments: list[str | tuple[str, ...]] = []
    cursor = 0
    capacity = 1
    while cursor < len(template):
        opening = template.find("[", cursor)
        stray_closing = template.find("]", cursor)
        if stray_closing != -1 and (opening == -1 or stray_closing < opening):
            raise MontageFailure("copy_variant_syntax_invalid", "文案候选组括号不完整")
        if opening == -1:
            segments.append(template[cursor:])
            cursor = len(template)
            break
        if opening > cursor:
            segments.append(template[cursor:opening])
        closing = template.find("]", opening + 1)
        if closing == -1:
            raise MontageFailure("copy_variant_syntax_invalid", "文案候选组缺少右括号")
        content = template[opening + 1 : closing]
        if "[" in content or "]" in content:
            raise MontageFailure("copy_variant_syntax_invalid", "文案候选组不能嵌套")
        options = tuple(part.strip() for part in content.split("|"))
        if len(options) < 2 or any(not option for option in options):
            raise MontageFailure(
                "copy_variant_syntax_invalid",
                "候选组必须至少包含两个非空选项，例如 [今天|这次]",
            )
        unique_options = tuple(dict.fromkeys(options))
        if len(unique_options) < 2:
            raise MontageFailure("copy_variant_syntax_invalid", "同一候选组的选项不能重复")
        segments.append(unique_options)
        capacity *= len(unique_options)
        cursor = closing + 1
    if not segments:
        segments.append("")
    return _ParsedTemplate(tuple(segments), capacity)


def _stable_number(*parts: object) -> int:
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest(), "big")


def _permuted_indexes(capacity: int, count: int, seed: int) -> list[int]:
    if capacity == 1:
        return [0]
    start = _stable_number("copy-start", seed) % capacity
    step = (_stable_number("copy-step", seed) % (capacity - 1)) + 1
    while math.gcd(step, capacity) != 1:
        step = (step % (capacity - 1)) + 1
    return [(start + offset * step) % capacity for offset in range(count)]


def expand_copy_variants(
    title_template: str,
    body_template: str,
    *,
    count: int,
    seed: int,
) -> tuple[CopyVariant, ...]:
    if count < 1:
        raise MontageFailure("montage_output_count_invalid", "生成数量必须大于 0")
    if not title_template.strip() and not body_template.strip():
        return tuple(CopyVariant(index + 1, "", "") for index in range(count))
    title = _parse_template(title_template.strip())
    body = _parse_template(body_template.strip())
    capacity = title.capacity * body.capacity
    if count > capacity:
        raise MontageFailure(
            "copy_variant_capacity_insufficient",
            f"当前文案只能生成 {capacity} 组不重复表达，请增加候选词组或减少成片数量",
            details={"capacity": capacity, "requested": count},
        )
    variants: list[CopyVariant] = []
    seen_text: set[tuple[str, str]] = set()
    scan_limit = capacity if capacity <= 100_000 else min(capacity, max(10_000, count * 200))
    for global_index in _permuted_indexes(capacity, scan_limit, seed):
        title_index = global_index % title.capacity
        body_index = global_index // title.capacity
        final_title = title.render(title_index)
        final_body = body.render(body_index)
        final_text = (final_title, final_body)
        if final_text in seen_text:
            continue
        seen_text.add(final_text)
        variants.append(CopyVariant(index=len(variants) + 1, title=final_title, body=final_body))
        if len(variants) == count:
            return tuple(variants)
    actual_capacity = len(seen_text)
    raise MontageFailure(
        "copy_variant_capacity_insufficient",
        f"候选词组合后只有 {actual_capacity} 组不同文案，请增加候选词组或减少成片数量",
        details={
            "capacity": actual_capacity,
            "theoretical_capacity": capacity,
            "requested": count,
            "scan_complete": scan_limit == capacity,
        },
    )


@dataclass(frozen=True, slots=True)
class _SourceWindow:
    asset: VideoAsset
    start_ms: int
    duration_ms: int

    @property
    def identity(self) -> str:
        return f"{self.asset.sha256}:{self.start_ms}:{self.duration_ms}"


def _segment_durations(target_ms: int, clip_ms: int) -> tuple[int, ...]:
    full, remainder = divmod(target_ms, clip_ms)
    result = [clip_ms] * full
    if remainder:
        result.append(remainder)
    return tuple(result)


def _windows(assets: Sequence[VideoAsset], clip_ms: int) -> tuple[_SourceWindow, ...]:
    values: list[_SourceWindow] = []
    for asset in assets:
        for start_ms in range(0, asset.duration_ms - clip_ms + 1, clip_ms):
            values.append(_SourceWindow(asset=asset, start_ms=start_ms, duration_ms=clip_ms))
    return tuple(values)


def _fingerprint(clips: Sequence[PlannedClip], *, audio_mode: str) -> str:
    payload = {
        "audio_mode": audio_mode,
        "clips": [
            {
                "window": clip.window_identity,
                "duration_ms": clip.duration_ms,
            }
            for clip in clips
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _build_planned_clips(
    selected: Sequence[_SourceWindow],
    segment_durations: Sequence[int],
) -> tuple[PlannedClip, ...]:
    return tuple(
        PlannedClip(
            source_path=window.asset.local_path,
            source_sha256=window.asset.sha256,
            start_ms=window.start_ms,
            duration_ms=duration_ms,
            window_duration_ms=window.duration_ms,
            has_audio=window.asset.has_audio,
        )
        for window, duration_ms in zip(selected, segment_durations, strict=True)
    )


def plan_montages(
    request: MontageRequest,
    assets: Sequence[VideoAsset],
) -> tuple[MontagePlan, ...]:
    asset_paths = tuple(asset.local_path.resolve() for asset in assets)
    if set(asset_paths) != set(request.source_paths) or len(asset_paths) != len(request.source_paths):
        raise MontageFailure(
            "montage_asset_set_mismatch",
            "素材探测结果与本次选择不一致，请重新读取素材",
        )
    hashes: dict[str, Path] = {}
    for asset in assets:
        previous = hashes.get(asset.sha256)
        if previous is not None:
            raise MontageFailure(
                "montage_source_content_duplicate",
                f"{previous.name} 与 {asset.local_path.name} 是同一份视频，请只保留一个",
                details={"sha256": asset.sha256},
            )
        hashes[asset.sha256] = asset.local_path

    segment_durations = _segment_durations(
        request.target_duration_ms,
        request.clip_duration_ms,
    )
    clips_per_output = len(segment_durations)
    windows = _windows(assets, request.clip_duration_ms)
    if len(windows) < clips_per_output:
        raise MontageFailure(
            "montage_source_duration_insufficient",
            "素材可用时长不足以生成一条成片",
            details={"required_windows": clips_per_output, "available_windows": len(windows)},
        )

    copy_variants = expand_copy_variants(
        request.title_template,
        request.body_template,
        count=request.output_count,
        seed=request.seed,
    )
    plans: list[MontagePlan] = []

    if not request.allow_reuse:
        maximum = len(windows) // clips_per_output
        if request.output_count > maximum:
            raise MontageFailure(
                "montage_strict_capacity_insufficient",
                f"当前素材最多可严格生成 {maximum} 条不重复成片",
                details={
                    "max_strict_outputs": maximum,
                    "requested": request.output_count,
                    "available_windows": len(windows),
                    "windows_per_output": clips_per_output,
                },
            )
        ordered = sorted(
            windows,
            key=lambda window: (
                _stable_number("strict", request.seed, window.identity),
                window.identity,
            ),
        )
        for output_index in range(request.output_count):
            start = output_index * clips_per_output
            selected = ordered[start : start + clips_per_output]
            clips = _build_planned_clips(selected, segment_durations)
            plans.append(
                MontagePlan(
                    index=output_index + 1,
                    clips=clips,
                    copy=copy_variants[output_index],
                    total_duration_ms=sum(segment_durations),
                    fingerprint=_fingerprint(clips, audio_mode=request.audio_mode),
                )
            )
        return tuple(plans)

    maximum_unique = math.perm(len(windows), clips_per_output)
    if request.output_count > maximum_unique:
        raise MontageFailure(
            "montage_unique_plan_exhausted",
            f"当前素材最多只能组成 {maximum_unique} 条不同剪辑顺序",
            details={"maximum_unique_plans": maximum_unique},
        )

    usage = {window.identity: 0 for window in windows}
    fingerprints: set[str] = set()
    for output_index in range(request.output_count):
        accepted: tuple[tuple[_SourceWindow, ...], tuple[PlannedClip, ...], str] | None = None
        for salt in range(max(128, request.output_count * 8)):
            selected: list[_SourceWindow] = []
            selected_ids: set[str] = set()
            for position in range(clips_per_output):
                available = [window for window in windows if window.identity not in selected_ids]
                available.sort(
                    key=lambda window: (
                        usage[window.identity],
                        _stable_number(
                            "reuse",
                            request.seed,
                            output_index,
                            salt,
                            position,
                            window.identity,
                        ),
                        window.identity,
                    )
                )
                chosen = available[0]
                selected.append(chosen)
                selected_ids.add(chosen.identity)
            clips = _build_planned_clips(selected, segment_durations)
            fingerprint = _fingerprint(clips, audio_mode=request.audio_mode)
            if fingerprint not in fingerprints:
                accepted = (tuple(selected), clips, fingerprint)
                break
        if accepted is None:
            raise MontageFailure(
                "montage_unique_plan_exhausted",
                "无法继续生成新的剪辑组合，请增加素材或减少生成数量",
            )
        selected, clips, fingerprint = accepted
        fingerprints.add(fingerprint)
        for window in selected:
            usage[window.identity] += 1
        plans.append(
            MontagePlan(
                index=output_index + 1,
                clips=clips,
                copy=copy_variants[output_index],
                total_duration_ms=sum(segment_durations),
                fingerprint=fingerprint,
            )
        )
    return tuple(plans)
