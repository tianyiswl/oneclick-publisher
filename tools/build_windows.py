# -*- coding: utf-8 -*-
"""构建一键发 Windows x64 客户端。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app_core.branding import APP_EXECUTABLE_NAME, APP_VERSION  # noqa: E402


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
    "seller_tools/",
    "license-private.json",
    "issue-history.json",
)


def archive_filename(build_date: str, version: str) -> str:
    """返回兼容 Windows 解压路径的 ASCII ZIP 文件名。"""

    if not re.fullmatch(r"\d{8}", build_date):
        raise ValueError("构建日期必须为 YYYYMMDD")
    if not re.fullmatch(r"\d+(?:\.\d+)*", version):
        raise ValueError("版本号只能包含数字和点")
    return f"{APP_EXECUTABLE_NAME}_{version}_Windows_x64_{build_date}.zip"


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
    matched_entries = {
        marker: [name for name in names if marker.lower() in name]
        for marker in FORBIDDEN_ARCHIVE_MARKERS
    }
    hits = [marker for marker, entries in matched_entries.items() if entries]
    if hits:
        samples: list[str] = []
        for marker in hits:
            entry = matched_entries[marker][0]
            marker_index = entry.find(marker.lower())
            samples.append(entry[: marker_index + len(marker)] + "<redacted>")
        raise RuntimeError(
            "安装包包含禁止的运行数据标识："
            + "、".join(hits)
            + "；命中路径："
            + "、".join(samples)
        )


def assert_windows_platform(actual_platform: str | None = None) -> None:
    """在真实构建前拒绝非 Windows 宿主。"""

    if (actual_platform or sys.platform) != "win32":
        raise RuntimeError("当前脚本只构建 Windows x64 客户端")


def assert_windows_x64(machine: str | None = None) -> None:
    """确保产物来自 x64 Windows Runner，而非不兼容架构。"""

    actual_machine = (machine or platform.machine()).lower()
    if actual_machine not in {"amd64", "x86_64"}:
        raise RuntimeError(f"当前脚本只构建 Windows x64 客户端，实际架构：{actual_machine}")


def remove_exact_generated_path(path: Path, expected_parent: Path) -> None:
    """只删除本脚本命名的确定构建目录。"""

    resolved = path.resolve()
    parent = expected_parent.resolve()
    if resolved.parent != parent or not resolved.name:
        raise RuntimeError(f"拒绝清理非构建目标：{resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)


def find_playwright_browser_dirs() -> list[Path]:
    """解析 Windows Playwright 缓存中当前依赖版本的浏览器资源。"""

    import playwright

    package_root = Path(playwright.__file__).resolve().parent
    manifest = package_root / "driver" / "package" / "browsers.json"
    configured_cache = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if configured_cache and configured_cache != "0":
        browser_cache = Path(configured_cache).expanduser()
    else:
        local_app_data = Path(
            os.environ.get("LOCALAPPDATA")
            or (Path.home() / "AppData" / "Local")
        )
        browser_cache = local_app_data / "ms-playwright"
    return resolve_playwright_browser_dirs(browser_cache, manifest)


def render_spec(root: Path) -> str:
    """生成 Windows PyInstaller one-dir spec，不写入任何运行时数据。"""

    assets = root / "ui" / "assets"
    stealth = root / "utils" / "stealth.min.js"
    return f'''# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [({str(assets)!r}, "ui/assets"), ({str(stealth)!r}, "utils")]
binaries = []
hiddenimports = []
hiddenimports += collect_submodules("myUtils")
hiddenimports += collect_submodules("utils")
hiddenimports += collect_submodules("uploader")
for package in ("playwright", "xhs", "biliup", "tzdata"):
    package_data, package_binaries, package_hidden = collect_all(package)
    datas += package_data
    binaries += package_binaries
    hiddenimports += package_hidden

a = Analysis(
    [{str(root / "desktop_native_app.py")!r}],
    pathex=[{str(root)!r}],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name={APP_EXECUTABLE_NAME!r},
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name={APP_EXECUTABLE_NAME!r},
)
'''


def sha256(path: Path) -> str:
    """返回文件 SHA-256。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_zip(source_root: Path, zip_path: Path) -> None:
    """将目录型 Windows 应用压缩为保留顶层目录的 ZIP。"""

    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(
        zip_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as archive:
        for source in sorted(source_root.rglob("*")):
            if source.is_file():
                archive.write(source, source.relative_to(source_root.parent))


def run_bundled_self_test(executable: Path, argument: str, marker: str) -> None:
    """运行打包客户端的离屏自检并保留其日志标识。"""

    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    completed = subprocess.run(
        [str(executable), argument],
        cwd=ROOT,
        env=env,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=90,
    )
    print(completed.stdout, end="")
    if marker not in completed.stdout:
        raise RuntimeError(f"打包客户端未通过自检：{marker}")


def build_windows_client(build_date: str) -> tuple[Path, Path, Path]:
    """在 Windows x64 上构建客户端，返回应用目录、ZIP 与校验文件。"""

    assert_windows_platform()
    assert_windows_x64()
    archive_name = archive_filename(build_date, APP_VERSION)

    build_parent = ROOT / "build"
    release_parent = ROOT / "release"
    build_parent.mkdir(parents=True, exist_ok=True)
    release_parent.mkdir(parents=True, exist_ok=True)
    build_root = build_parent / f"windows-{build_date}-v{APP_VERSION}"
    release_root = release_parent / f"windows-{build_date}-v{APP_VERSION}"
    remove_exact_generated_path(build_root, build_parent)
    remove_exact_generated_path(release_root, release_parent)
    build_root.mkdir(parents=True)
    release_root.mkdir(parents=True)

    spec_path = build_root / f"{APP_EXECUTABLE_NAME}.spec"
    spec_path.write_text(render_spec(ROOT), encoding="utf-8")
    browser_dirs = find_playwright_browser_dirs()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            str(release_root),
            "--workpath",
            str(build_root / "work"),
            str(spec_path),
        ],
        cwd=ROOT,
        check=True,
    )

    dist_root = release_root / APP_EXECUTABLE_NAME
    executable = dist_root / f"{APP_EXECUTABLE_NAME}.exe"
    if not executable.is_file():
        raise RuntimeError(f"未生成 Windows 客户端：{executable}")
    bundle_playwright_browsers(dist_root, browser_dirs)
    run_bundled_self_test(executable, "--ui-test", "NATIVE_DESKTOP_UI_OK")
    run_bundled_self_test(
        executable,
        "--browser-self-test",
        "NATIVE_DESKTOP_BROWSER_OK",
    )

    zip_path = release_parent / archive_name
    create_zip(dist_root, zip_path)
    assert_archive_safe(zip_path)
    digest = sha256(zip_path)
    sha_path = zip_path.with_suffix(zip_path.suffix + ".sha256")
    sha_path.write_text(f"{digest}  {zip_path.name}\n", encoding="ascii")
    print(f"WINDOWS_APP={dist_root}")
    print(f"WINDOWS_ZIP={zip_path}")
    print(f"WINDOWS_ZIP_SHA256={digest}")
    print("WINDOWS_BUILD_OK")
    return dist_root, zip_path, sha_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--date",
        default=datetime.now().strftime("%Y%m%d"),
        help="构建日期，格式 YYYYMMDD",
    )
    args = parser.parse_args()
    build_windows_client(args.date)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
