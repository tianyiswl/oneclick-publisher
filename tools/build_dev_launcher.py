# -*- coding: utf-8 -*-
"""构建仅供本机源码联调使用的 macOS 原生启动器。"""

from __future__ import annotations

import argparse
import plistlib
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "开发版客户端" / "一键发开发版.app"
CONTENTS = OUTPUT / "Contents"
EXECUTABLE = CONTENTS / "MacOS" / "YiJianFaDev"
SOURCE = ROOT / "tools" / "yijianfa_dev_launcher.swift"


def expected_info() -> dict[str, object]:
    return {
        "CFBundleDevelopmentRegion": "zh_CN",
        "CFBundleDisplayName": "一键发开发版",
        "CFBundleExecutable": "YiJianFaDev",
        "CFBundleIdentifier": "com.tianyiswl.yijianfa.dev",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": "一键发开发版",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "开发版",
        "CFBundleVersion": "1",
        "NSHighResolutionCapable": True,
    }


def check_launcher() -> None:
    plist_path = CONTENTS / "Info.plist"
    if not EXECUTABLE.is_file() or not EXECUTABLE.stat().st_mode & 0o111:
        raise RuntimeError("开发版原生启动器不存在或不可执行")
    with plist_path.open("rb") as handle:
        info = plistlib.load(handle)
    for key, value in expected_info().items():
        if info.get(key) != value:
            raise RuntimeError(f"开发版启动器元数据错误：{key}")


def build_launcher() -> None:
    if sys.platform != "darwin":
        raise RuntimeError("开发版原生启动器仅支持 macOS")
    if not SOURCE.is_file():
        raise RuntimeError("未找到开发版启动器源码")
    EXECUTABLE.parent.mkdir(parents=True, exist_ok=True)
    with (CONTENTS / "Info.plist").open("wb") as handle:
        plistlib.dump(expected_info(), handle, sort_keys=True)
    subprocess.run(
        ["swiftc", str(SOURCE), "-o", str(EXECUTABLE)],
        cwd=ROOT,
        check=True,
    )
    check_launcher()
    print(f"DEV_LAUNCHER_READY={OUTPUT}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        check_launcher()
        print(f"DEV_LAUNCHER_OK={OUTPUT}")
        return 0
    build_launcher()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
