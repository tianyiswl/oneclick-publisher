# -*- coding: utf-8 -*-
"""素材数据服务。"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

from . import account_service
from .database import connect
from .paths import COVER_DIR, ROOT_DIR, VIDEO_DIR


VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi", ".wmv", ".flv", ".webm"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def media_type(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return "图片"
    if suffix in VIDEO_SUFFIXES:
        return "视频"
    return "文件"


def _row_to_dict(row) -> dict:
    data = dict(row)
    stored = data.get("file_path") or data.get("filename")
    data["storedPath"] = str(VIDEO_DIR / Path(stored).name) if stored else ""
    data["typeText"] = media_type(data.get("filename") or stored or "")
    data["remark"] = data.get("remark") or ""
    data["mediaCategory"] = str(data.get("mediaCategory") or "其他").strip() or "其他"
    cover = data.get("coverPath") or ""
    data["coverPath"] = str(COVER_DIR / Path(cover).name) if cover else ""
    return data


def _ffmpeg_path() -> Path | None:
    executable = shutil.which("ffmpeg")
    candidates = [
        ROOT_DIR / "runtime" / "ffmpeg" / "bin" / "ffmpeg.exe",
        ROOT_DIR / "runtime" / "ffmpeg" / "bin" / "ffmpeg",
        ROOT_DIR / "runtime" / "playwright-browsers" / "ffmpeg-1011" / "ffmpeg-win64.exe",
        ROOT_DIR / "runtime" / "playwright-browsers" / "ffmpeg-1011" / "ffmpeg-mac",
    ]
    if executable:
        candidates.append(Path(executable))
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def generate_video_cover(video_path: Path, media_id: int) -> str:
    if not video_path.exists():
        return ""
    COVER_DIR.mkdir(parents=True, exist_ok=True)
    cover_name = f"media_{media_id}.jpg"
    cover_path = COVER_DIR / cover_name
    ffmpeg = _ffmpeg_path()
    if ffmpeg:
        command = [
            str(ffmpeg),
            "-y",
            "-ss",
            "00:00:01",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(cover_path),
        ]
        try:
            subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        except Exception:
            pass
    if not cover_path.exists():
        _generate_video_cover_with_qt(video_path, cover_path)
    return cover_name if cover_path.exists() else ""


def _generate_video_cover_with_qt(video_path: Path, cover_path: Path) -> None:
    try:
        from PyQt6.QtCore import QCoreApplication, QEventLoop, QTimer, QUrl
        from PyQt6.QtMultimedia import QMediaPlayer, QVideoSink
    except Exception:
        return
    if QCoreApplication.instance() is None:
        return
    loop = QEventLoop()
    player = QMediaPlayer()
    sink = QVideoSink()
    timer = QTimer()
    timer.setSingleShot(True)

    def save_frame(frame) -> None:
        try:
            image = frame.toImage()
            if not image.isNull() and image.save(str(cover_path), "JPG"):
                loop.quit()
        except Exception:
            pass

    sink.videoFrameChanged.connect(save_frame)
    timer.timeout.connect(loop.quit)
    player.setVideoSink(sink)
    player.setSource(QUrl.fromLocalFile(str(video_path)))
    timer.start(5000)
    player.play()
    loop.exec()
    player.stop()
    player.deleteLater()
    sink.deleteLater()


def list_media(category: str | None = None) -> list[dict]:
    with connect() as conn:
        if category and category != "全部":
            rows = conn.execute(
                "SELECT * FROM file_records WHERE mediaCategory = ? ORDER BY upload_time DESC, id DESC",
                (str(category),),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM file_records ORDER BY upload_time DESC, id DESC").fetchall()
    return [_row_to_dict(row) for row in rows]


def list_categories() -> list[dict]:
    """主体自动形成分类，并始终保留“其他”。已有素材分类也不会丢失。"""

    profile_names = account_service.list_profiles()
    with connect() as conn:
        rows = conn.execute(
            "SELECT mediaCategory, COUNT(*) AS count FROM file_records GROUP BY mediaCategory"
        ).fetchall()
    counts = {str(row["mediaCategory"] or "其他").strip() or "其他": int(row["count"] or 0) for row in rows}
    ordered = list(dict.fromkeys([*profile_names, *counts.keys()]))
    ordered = [name for name in ordered if name and name != "其他"]
    categories = ["全部", *ordered, "其他"]
    return [
        {"name": name, "count": sum(counts.values()) if name == "全部" else counts.get(name, 0)}
        for name in categories
    ]


def ensure_cover(file_id: int) -> str:
    with connect() as conn:
        row = conn.execute("SELECT id, filename, file_path, coverPath FROM file_records WHERE id = ?", (file_id,)).fetchone()
        if not row:
            return ""
        data = _row_to_dict(row)
        existing = Path(data["coverPath"]) if data.get("coverPath") else None
        if existing and existing.exists():
            return str(existing)
        suffix = Path(data["storedPath"] or data.get("filename") or "").suffix.lower()
        if suffix in IMAGE_SUFFIXES:
            path = Path(data["storedPath"])
            return str(path) if path.exists() else ""
        if suffix not in VIDEO_SUFFIXES:
            return ""
        cover_name = generate_video_cover(Path(data["storedPath"]), int(file_id))
        if cover_name:
            conn.execute("UPDATE file_records SET coverPath = ? WHERE id = ?", (cover_name, file_id))
            conn.commit()
            return str(COVER_DIR / cover_name)
    return ""


def refresh_covers(ids: list[int]) -> int:
    """重新生成选中视频素材的封面。"""
    refreshed = 0
    with connect() as conn:
        for file_id in ids:
            row = conn.execute("SELECT id, filename, file_path, coverPath FROM file_records WHERE id = ?", (file_id,)).fetchone()
            if not row:
                continue
            data = _row_to_dict(row)
            suffix = Path(data["storedPath"] or data.get("filename") or "").suffix.lower()
            if suffix not in VIDEO_SUFFIXES:
                continue
            old_cover = data.get("coverPath")
            if old_cover:
                old_cover_path = Path(old_cover)
                if old_cover_path.exists():
                    old_cover_path.unlink()
            cover_name = generate_video_cover(Path(data["storedPath"]), int(file_id))
            if cover_name:
                conn.execute("UPDATE file_records SET coverPath = ? WHERE id = ?", (cover_name, file_id))
                refreshed += 1
        conn.commit()
    return refreshed


def cover_display_path(row: dict) -> str:
    cover_raw = row.get("coverPath") or ""
    cover = Path(cover_raw) if cover_raw else None
    if cover and cover.exists() and cover.is_file():
        return str(cover)
    stored = Path(row.get("storedPath") or "")
    suffix = Path(row.get("storedPath") or row.get("filename") or "").suffix.lower()
    if suffix in IMAGE_SUFFIXES and stored.exists():
        return str(stored)
    if suffix in VIDEO_SUFFIXES and row.get("id"):
        return ensure_cover(int(row["id"]))
    return ""


def media_stats() -> dict:
    rows = list_media()
    return {
        "total": len(rows),
        "videos": sum(1 for row in rows if row["typeText"] == "视频"),
        "images": sum(1 for row in rows if row["typeText"] == "图片"),
    }


def import_files(paths: list[str], *, category: str = "其他") -> int:
    count = 0
    category = str(category or "其他").strip() or "其他"
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        for raw in paths:
            src = Path(raw)
            if not src.is_file():
                continue
            stored_name = f"{uuid.uuid1()}_{src.name}"
            dest = VIDEO_DIR / stored_name
            shutil.copy2(src, dest)
            size_mb = round(dest.stat().st_size / (1024 * 1024), 2)
            conn.execute(
                "INSERT INTO file_records (filename, filesize, file_path, mediaCategory) VALUES (?, ?, ?, ?)",
                (src.name, size_mb, stored_name, category),
            )
            media_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()[0]
            if media_type(src.name) == "视频":
                cover_name = generate_video_cover(dest, int(media_id))
                if cover_name:
                    conn.execute("UPDATE file_records SET coverPath = ? WHERE id = ?", (cover_name, media_id))
            count += 1
        conn.commit()
    return count


def rename_media(file_id: int, new_name: str) -> None:
    with connect() as conn:
        row = conn.execute("SELECT filename, file_path FROM file_records WHERE id = ?", (file_id,)).fetchone()
        if not row:
            raise ValueError("素材不存在")
        old_stored = Path(row["file_path"] or row["filename"]).name
        suffix = Path(row["filename"] or old_stored).suffix
        display_name = Path(new_name).name.strip()
        if not Path(display_name).suffix and suffix:
            display_name += suffix
        prefix = old_stored[:37] if len(old_stored) > 37 and old_stored[36] == "_" else f"{uuid.uuid1()}_"
        new_stored = f"{prefix}{display_name}"
        old_path = VIDEO_DIR / old_stored
        new_path = VIDEO_DIR / new_stored
        if old_path.exists() and old_path.resolve() != new_path.resolve():
            old_path.rename(new_path)
        conn.execute(
            "UPDATE file_records SET filename = ?, file_path = ? WHERE id = ?",
            (display_name, new_stored, file_id),
        )
        conn.commit()


def update_remark(file_id: int, remark: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE file_records SET remark = ? WHERE id = ?", (remark.strip(), file_id))
        conn.commit()


def delete_media(ids: list[int]) -> int:
    deleted = 0
    with connect() as conn:
        for file_id in ids:
            row = conn.execute("SELECT filename, file_path, coverPath FROM file_records WHERE id = ?", (file_id,)).fetchone()
            if not row:
                continue
            stored = Path(row["file_path"] or row["filename"]).name
            path = VIDEO_DIR / stored
            if path.exists():
                path.unlink()
            cover = row["coverPath"]
            if cover:
                cover_path = COVER_DIR / Path(cover).name
                if cover_path.exists():
                    cover_path.unlink()
            conn.execute("DELETE FROM file_records WHERE id = ?", (file_id,))
            deleted += 1
        conn.commit()
    return deleted
