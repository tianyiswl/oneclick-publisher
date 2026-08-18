# -*- coding: utf-8 -*-
"""构建一键发 macOS arm64 客户端。

仅把源码与只读资源加入应用；账号、会话、数据库和素材目录
均不属于 PyInstaller datas，也不会进入压缩包。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app_core.branding import APP_VERSION  # noqa: E402


APP_NAME = "一键发"
BUNDLE_ID = "com.yijianfa.desktop"


def run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, env=env, check=True)


def remove_exact_generated_path(path: Path, expected_parent: Path) -> None:
    """只清理构建脚本自己命名的确定目录，拒绝宽泛路径。"""

    resolved = path.resolve()
    parent = expected_parent.resolve()
    if resolved.parent != parent or not resolved.name:
        raise RuntimeError(f"拒绝清理非构建目标：{resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)


def build_icon(icon_png: Path, icon_root: Path) -> Path:
    iconset = icon_root / "一键发.iconset"
    iconset.mkdir(parents=True, exist_ok=True)
    sizes = (
        (16, "icon_16x16.png"),
        (32, "icon_16x16@2x.png"),
        (32, "icon_32x32.png"),
        (64, "icon_32x32@2x.png"),
        (128, "icon_128x128.png"),
        (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"),
        (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"),
        (1024, "icon_512x512@2x.png"),
    )
    for size, name in sizes:
        run(
            [
                "sips",
                "-z",
                str(size),
                str(size),
                str(icon_png),
                "--out",
                str(iconset / name),
            ]
        )
    icon_path = icon_root / "一键发.icns"
    run(["iconutil", "-c", "icns", str(iconset), "-o", str(icon_path)])
    return icon_path


def resolve_playwright_browser_dirs(
    browser_cache: Path,
    browsers_manifest: Path,
) -> list[Path]:
    """解析当前 Playwright 版本实际需要随客户端携带的 Chromium 资源。"""

    required_names = {"chromium", "chromium-headless-shell", "ffmpeg"}
    manifest = json.loads(browsers_manifest.read_text(encoding="utf-8"))
    records = {
        item.get("name"): item
        for item in manifest.get("browsers", [])
        if item.get("name") in required_names
    }
    missing_records = sorted(required_names - records.keys())
    if missing_records:
        raise RuntimeError(
            "Playwright 浏览器清单缺少：" + "、".join(missing_records)
        )

    browser_dirs: list[Path] = []
    missing_dirs: list[str] = []
    for name in ("chromium", "chromium-headless-shell", "ffmpeg"):
        revision = str(records[name]["revision"])
        directory_name = f"{name.replace('-', '_')}-{revision}"
        source = browser_cache / directory_name
        if source.is_dir():
            browser_dirs.append(source.resolve())
        else:
            missing_dirs.append(directory_name)
    if missing_dirs:
        raise RuntimeError(
            "缺少打包所需的 Playwright 浏览器资源："
            + "、".join(missing_dirs)
            + "。请先执行 .venv/bin/python -m playwright install chromium"
        )
    return browser_dirs


def find_playwright_browser_dirs() -> list[Path]:
    """从当前虚拟环境清单和本机 Playwright 缓存确定资源版本。"""

    import playwright

    package_root = Path(playwright.__file__).resolve().parent
    manifest = package_root / "driver" / "package" / "browsers.json"
    configured_cache = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if configured_cache and configured_cache != "0":
        browser_cache = Path(configured_cache).expanduser()
    else:
        browser_cache = Path.home() / "Library" / "Caches" / "ms-playwright"
    return resolve_playwright_browser_dirs(browser_cache, manifest)


def write_spec(
    spec_path: Path,
    icon_path: Path,
) -> None:
    assets = ROOT / "ui" / "assets"
    stealth = ROOT / "utils" / "stealth.min.js"
    spec = f'''# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [({str(assets)!r}, "ui/assets"), ({str(stealth)!r}, "utils")]
binaries = []
hiddenimports = []
hiddenimports += collect_submodules("myUtils")
hiddenimports += collect_submodules("utils")
hiddenimports += collect_submodules("uploader")
for package in ("playwright", "xhs", "biliup"):
    package_data, package_binaries, package_hidden = collect_all(package)
    datas += package_data
    binaries += package_binaries
    hiddenimports += package_hidden

a = Analysis(
    [{str(ROOT / "desktop_native_app.py")!r}],
    pathex=[{str(ROOT)!r}],
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
    name={APP_NAME!r},
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="arm64",
    codesign_identity=None,
    entitlements_file=None,
    icon=[{str(icon_path)!r}],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name={APP_NAME!r},
)
app = BUNDLE(
    coll,
    name={f"{APP_NAME}.app"!r},
    icon={str(icon_path)!r},
    bundle_identifier={BUNDLE_ID!r},
)
'''
    spec_path.write_text(spec, encoding="utf-8")


def bundle_playwright_browsers(
    app_path: Path,
    browser_dirs: list[Path],
) -> Path:
    """在 PyInstaller 完成后原样复制浏览器，避免破坏 Chromium 内嵌签名结构。"""

    target_root = (
        app_path
        / "Contents"
        / "Resources"
        / "runtime"
        / "playwright-browsers"
    )
    target_root.mkdir(parents=True, exist_ok=True)
    for source in browser_dirs:
        target = target_root / source.name
        if target.exists():
            raise RuntimeError(f"浏览器资源目标已存在：{target}")
        shutil.copytree(source, target, symlinks=True)
    return target_root


def set_bundle_metadata(app_path: Path) -> None:
    plist_path = app_path / "Contents" / "Info.plist"
    with plist_path.open("rb") as handle:
        metadata = plistlib.load(handle)
    metadata.update(
        {
            "CFBundleDisplayName": APP_NAME,
            "CFBundleName": APP_NAME,
            "CFBundleIdentifier": BUNDLE_ID,
            "CFBundleShortVersionString": APP_VERSION,
            "CFBundleVersion": APP_VERSION,
        }
    )
    with plist_path.open("wb") as handle:
        plistlib.dump(metadata, handle, sort_keys=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def assert_archive_safe(zip_path: Path) -> None:
    result = subprocess.run(
        ["zipinfo", "-1", str(zip_path)],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
    )
    forbidden = (
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
    # zipinfo 会按 ZIP 文件名编码原样输出；中文应用名不保证是 UTF-8。
    # 禁止项均为 ASCII，直接在原始字节中检查，避免因文件名解码失败跳过安全门禁。
    lowered = result.stdout.lower()
    hits = [
        item
        for item in forbidden
        if item.lower().encode("ascii") in lowered
    ]
    if hits:
        raise RuntimeError("安装包包含禁止的运行数据标识：" + "、".join(hits))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--date",
        default=datetime.now().strftime("%Y%m%d"),
        help="构建日期，格式 YYYYMMDD",
    )
    args = parser.parse_args()

    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise RuntimeError("当前脚本只构建 macOS arm64 客户端")

    pyinstaller = ROOT / ".venv" / "bin" / "pyinstaller"
    if not pyinstaller.is_file():
        raise RuntimeError("未找到 .venv/bin/pyinstaller")

    build_parent = ROOT / "build"
    release_parent = ROOT / "release"
    build_parent.mkdir(parents=True, exist_ok=True)
    release_parent.mkdir(parents=True, exist_ok=True)
    build_root = build_parent / f"macos-{args.date}-v{APP_VERSION}"
    release_root = release_parent / f"macos-{args.date}-v{APP_VERSION}"
    remove_exact_generated_path(build_root, build_parent)
    remove_exact_generated_path(release_root, release_parent)
    build_root.mkdir(parents=True)
    release_root.mkdir(parents=True)

    icon_path = build_icon(
        ROOT / "ui" / "assets" / "fashetai-app-icon.png",
        build_root / "icon",
    )
    spec_path = build_root / "一键发.spec"
    playwright_browser_dirs = find_playwright_browser_dirs()
    write_spec(spec_path, icon_path)
    run(
        [
            str(pyinstaller),
            "--noconfirm",
            "--clean",
            "--distpath",
            str(release_root),
            "--workpath",
            str(build_root / "work"),
            str(spec_path),
        ]
    )

    app_path = release_root / f"{APP_NAME}.app"
    set_bundle_metadata(app_path)
    bundle_playwright_browsers(app_path, playwright_browser_dirs)
    run(["xattr", "-cr", str(app_path)])
    run(["codesign", "--force", "--deep", "--sign", "-", str(app_path)])
    run(["codesign", "--verify", "--deep", "--strict", str(app_path)])

    executable = app_path / "Contents" / "MacOS" / APP_NAME
    test_env = os.environ.copy()
    test_env["QT_QPA_PLATFORM"] = "offscreen"
    completed = subprocess.run(
        [str(executable), "--ui-test"],
        cwd=ROOT,
        env=test_env,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=90,
    )
    if "NATIVE_DESKTOP_UI_OK" not in completed.stdout:
        raise RuntimeError("打包客户端未通过离屏界面自检")

    browser_completed = subprocess.run(
        [str(executable), "--browser-self-test"],
        cwd=ROOT,
        env=test_env,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=90,
    )
    if "NATIVE_DESKTOP_BROWSER_OK" not in browser_completed.stdout:
        raise RuntimeError("打包客户端未通过内置浏览器自检")

    zip_path = release_parent / f"一键发_{APP_VERSION}_macOS_arm64_{args.date}.zip"
    if zip_path.exists():
        zip_path.unlink()
    run(
        [
            "ditto",
            "-c",
            "-k",
            "--sequesterRsrc",
            "--keepParent",
            str(app_path),
            str(zip_path),
        ]
    )
    assert_archive_safe(zip_path)

    print(f"MACOS_APP={app_path}")
    print(f"MACOS_ZIP={zip_path}")
    print(f"MACOS_ZIP_SHA256={sha256(zip_path)}")
    print("MACOS_BUILD_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
