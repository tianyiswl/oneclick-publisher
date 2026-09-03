# -*- coding: utf-8 -*-
"""轻量混剪批次编排、隔离输出和原子回执。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import json
import os
from pathlib import Path
import re
import uuid
from typing import Callable, Sequence

from .montage_models import MontageFailure, MontageRequest, VideoAsset, plan_montages
from .montage_runtime import MontageRuntime, probe_video, render_montage
from .narration_service import NarrationArtifact, synthesize_system_narration
from .paths import MONTAGE_DIR


Progress = Callable[[dict[str, object]], None]


@dataclass(frozen=True, slots=True)
class MontageBatchResult:
    batch_id: str
    status: str
    batch_dir: Path
    outputs: tuple[dict[str, object], ...]
    receipt_path: Path
    narration: dict[str, object] | None

    def to_dict(self) -> dict[str, object]:
        return {
            "batch_id": self.batch_id,
            "status": self.status,
            "batch_dir": str(self.batch_dir),
            "outputs": list(self.outputs),
            "receipt_path": str(self.receipt_path),
            "narration": self.narration,
        }


def _now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _new_batch_id() -> str:
    return f"M{datetime.now().strftime('%m%d%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"


def _validate_batch_id(value: str) -> str:
    batch_id = str(value).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", batch_id):
        raise MontageFailure("montage_batch_id_invalid", "混剪批次号无效")
    return batch_id


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _emit(progress: Progress | None, stage: str, **detail: object) -> None:
    if progress:
        progress({"stage": stage, **detail})


def _failed_output(
    index: int,
    plan_fingerprint: str,
    exc: Exception,
    *,
    narration_sha256: str | None = None,
) -> dict[str, object]:
    if isinstance(exc, MontageFailure):
        error_code = exc.code
        error = str(exc)
        details = exc.details
    else:
        error_code = "montage_unexpected_failure"
        error = str(exc) or exc.__class__.__name__
        details = {}
    receipt = {
        "index": index,
        "status": "failed",
        "fingerprint": plan_fingerprint,
        "error_code": error_code,
        "error": error,
        "details": details,
    }
    if narration_sha256 is not None:
        receipt["narration_sha256"] = narration_sha256
    return receipt


def run_montage_batch(
    request: MontageRequest,
    *,
    output_root: Path = MONTAGE_DIR,
    runtime: MontageRuntime | None = None,
    batch_id: str | None = None,
    probe: Callable[..., VideoAsset] = probe_video,
    narrator: Callable[..., NarrationArtifact] = synthesize_system_narration,
    renderer: Callable[..., dict[str, object]] = render_montage,
    progress: Progress | None = None,
) -> MontageBatchResult:
    """同步运行一个完整批次；调用方可放入桌面后台线程。"""

    resolved_batch_id = _validate_batch_id(batch_id or _new_batch_id())
    root = Path(output_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    batch_dir = root / resolved_batch_id
    try:
        batch_dir.mkdir()
    except FileExistsError as exc:
        raise MontageFailure(
            "montage_batch_exists",
            f"混剪批次已存在，未覆盖原结果：{resolved_batch_id}",
        ) from exc
    receipt_path = batch_dir / "batch-receipt.json"
    started_at = _now_text()
    _write_json_atomic(
        batch_dir / "request.json",
        {
            "schema_version": "oneclick-montage-request/v2",
            "batch_id": resolved_batch_id,
            "created_at": started_at,
            **request.to_dict(),
        },
    )

    outputs: list[dict[str, object]] = []
    narration: NarrationArtifact | None = None
    narration_payload: dict[str, object] | None = None
    planning_request = request
    try:
        selected_runtime = runtime or MontageRuntime.resolve()
        assets: list[VideoAsset] = []
        for index, source_path in enumerate(request.source_paths, start=1):
            _emit(
                progress,
                "probing",
                current=index,
                total=len(request.source_paths),
                filename=source_path.name,
            )
            assets.append(probe(source_path, runtime=selected_runtime))

        if request.audio_mode == "narration":
            _emit(progress, "narration_synthesizing")
            try:
                narration = narrator(
                    request.narration_text,
                    batch_dir / "narration",
                    runtime=selected_runtime,
                )
            except Exception as exc:
                failure = exc if isinstance(exc, MontageFailure) else MontageFailure(
                    "montage_narration_synthesis_failed",
                    str(exc) or exc.__class__.__name__,
                )
                _write_json_atomic(
                    batch_dir / "narration" / "receipt.json",
                    {
                        "schema_version": "oneclick-montage-narration/v1",
                        "status": "failed",
                        "error_code": failure.code,
                        "error": str(failure),
                        "details": failure.details,
                    },
                )
                raise failure
            narration_payload = narration.to_dict()
            _write_json_atomic(
                batch_dir / "narration" / "receipt.json",
                {
                    "schema_version": "oneclick-montage-narration/v1",
                    **narration_payload,
                },
            )
            _emit(progress, "narration_readback", **narration_payload)
            if request.clip_duration_ms > narration.master_duration_ms:
                raise MontageFailure(
                    "montage_clip_duration_invalid",
                    "镜头时长不能超过配音决定的成片时长",
                )
            planning_request = replace(
                request,
                target_duration_ms=narration.master_duration_ms,
            )

        _emit(progress, "planning", output_count=request.output_count)
        plans = plan_montages(planning_request, assets)
        _write_json_atomic(
            batch_dir / "assets.json",
            {
                "schema_version": "oneclick-montage-assets/v1",
                "assets": [asset.to_dict() for asset in assets],
            },
        )

        for plan in plans:
            output_dir = batch_dir / f"{plan.index:03d}"
            output_dir.mkdir()
            _write_json_atomic(output_dir / "mix-plan.json", plan.to_dict())
            _write_json_atomic(
                output_dir / "copy.json",
                {
                    "schema_version": "oneclick-copy-variant/v1",
                    "title_template": request.title_template,
                    "body_template": request.body_template,
                    **plan.copy.to_dict(),
                },
            )
            _emit(
                progress,
                "rendering",
                output_index=plan.index,
                output_count=len(plans),
            )
            try:
                render_receipt = renderer(
                    plan,
                    output_dir / "video.mp4",
                    runtime=selected_runtime,
                    audio_mode=request.audio_mode,
                    narration=narration,
                    progress=progress,
                )
                output_receipt = {
                    "index": plan.index,
                    "status": "success",
                    "title": plan.copy.title,
                    "body": plan.copy.body,
                    **render_receipt,
                }
            except Exception as exc:
                output_receipt = _failed_output(
                    plan.index,
                    plan.fingerprint,
                    exc,
                    narration_sha256=narration.sha256 if narration else None,
                )
            outputs.append(output_receipt)
            _write_json_atomic(output_dir / "result.json", output_receipt)
            _emit(
                progress,
                "output_completed",
                output_index=plan.index,
                output_count=len(plans),
                status=output_receipt["status"],
            )

        success_count = sum(item["status"] == "success" for item in outputs)
        failed_count = len(outputs) - success_count
        status = "success" if failed_count == 0 else "failed" if success_count == 0 else "partial_failure"
        receipt = {
            "schema_version": "oneclick-montage-batch-receipt/v2",
            "batch_id": resolved_batch_id,
            "status": status,
            "started_at": started_at,
            "finished_at": _now_text(),
            "summary": {"success": success_count, "failed": failed_count},
            "outputs": outputs,
            "effective_target_duration_ms": planning_request.target_duration_ms,
            "narration": narration_payload,
        }
        _write_json_atomic(receipt_path, receipt)
        _emit(
            progress,
            "completed",
            status=status,
            success=success_count,
            failed=failed_count,
            batch_dir=str(batch_dir),
        )
        return MontageBatchResult(
            batch_id=resolved_batch_id,
            status=status,
            batch_dir=batch_dir,
            outputs=tuple(outputs),
            receipt_path=receipt_path,
            narration=narration_payload,
        )
    except Exception as exc:
        if isinstance(exc, MontageFailure):
            failure = exc
        else:
            failure = MontageFailure(
                "montage_batch_failed",
                str(exc) or exc.__class__.__name__,
            )
        receipt = {
            "schema_version": "oneclick-montage-batch-receipt/v2",
            "batch_id": resolved_batch_id,
            "status": "failed",
            "started_at": started_at,
            "finished_at": _now_text(),
            "summary": {
                "success": sum(item.get("status") == "success" for item in outputs),
                "failed": max(1, sum(item.get("status") == "failed" for item in outputs)),
            },
            "outputs": outputs,
            "effective_target_duration_ms": planning_request.target_duration_ms,
            "narration": narration_payload,
            "error_code": failure.code,
            "error": str(failure),
            "details": failure.details,
        }
        _write_json_atomic(receipt_path, receipt)
        _emit(
            progress,
            "completed",
            status="failed",
            error_code=failure.code,
            error=str(failure),
            batch_dir=str(batch_dir),
        )
        raise failure
