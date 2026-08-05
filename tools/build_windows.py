# -*- coding: utf-8 -*-
"""构建一键发 Windows x64 客户端的离线帮助函数。"""

from __future__ import annotations

import json
import re
import shutil
import zipfile
from pathlib import Path


REQUIRED_PLAYWRIGHT_BROWSER_NAMES = (
    "chromium",
    "chromium-headless-shell",
    "ffmpeg",
)
FORBIDDEN_ARCHIVE_MARKERS = (
    "demo-runtime/",
    "cookiesFile/",
    "storage_state",
    "database.db",
    "oauth_token",
    "access_token",
    "refresh_token",
)


def archive_filename(build_date: str, version: str) -> str:
    """返回兼容 Windows 解压路径的 ASCII ZIP 文件名。"""

    if not re.fullmatch(r"\d{8}", build_date):
        raise ValueError("构建日期必须为 YYYYMMDD")
    if not re.fullmatch(r"\d+(?:\.\d+)*", version):
        raise ValueError("版本号只能包含数字和点")
    return f"Fashetai_{version}_Windows_x64_{build_date}.zip"


def resolve_playwright_browser_dirs(
    browser_cache: Path,
    browsers_manifest: Path,
) -> list[Path]:
    """按 Playwright manifest 解析必须随 Windows 包复制的浏览器目录。"""

    manifest = json.loads(browsers_manifest.read_text(encoding="utf-8"))
    records = {
        item.get("name"): item
        for item in manifest.get("browsers", [])
        if item.get("name") in REQUIRED_PLAYWRIGHT_BROWSER_NAMES
    }
    missing_records = sorted(set(REQUIRED_PLAYWRIGHT_BROWSER_NAMES) - records.keys())
    if missing_records:
        raise RuntimeError("Playwright 浏览器清单缺少：" + "、".join(missing_records))

    browser_dirs: list[Path] = []
    missing_dirs: list[str] = []
    for name in REQUIRED_PLAYWRIGHT_BROWSER_NAMES:
        revision = str(records[name]["revision"])
        directory_name = f"{name.replace('-', '_')}-{revision}"
        source = browser_cache / directory_name
        if source.is_dir():
            browser_dirs.append(source.resolve())
        else:
            missing_dirs.append(directory_name)
    if missing_dirs:
        raise RuntimeError(
            "缺少打包所需的 Playwright 浏览器资源：" + "、".join(missing_dirs)
        )
    return browser_dirs


def bundle_playwright_browsers(
    dist_root: Path,
    browser_dirs: list[Path],
) -> Path:
    """把浏览器资源复制到 PyInstaller one-dir 的内部运行时目录。"""

    target_root = dist_root / "_internal" / "runtime" / "playwright-browsers"
    target_root.mkdir(parents=True, exist_ok=True)
    for source in browser_dirs:
        target = target_root / source.name
        if target.exists():
            raise RuntimeError(f"浏览器资源目标已存在：{target}")
        shutil.copytree(source, target)
    return target_root


def assert_archive_safe(zip_path: Path) -> None:
    """拒绝含账号会话或运行数据标识的安装包。"""

    with zipfile.ZipFile(zip_path) as archive:
        names = [name.replace("\\", "/").lower() for name in archive.namelist()]
    hits = [
        marker
        for marker in FORBIDDEN_ARCHIVE_MARKERS
        if any(marker.lower() in name for name in names)
    ]
    if hits:
        raise RuntimeError("安装包包含禁止的运行数据标识：" + "、".join(hits))
