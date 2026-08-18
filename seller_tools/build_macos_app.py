# -*- coding: utf-8 -*-
"""构建并安装一键发激活码管理器 macOS 应用。"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
APP_NAME = "一键发激活码管理器"
RELEASE_DIR = ROOT_DIR / "seller_release"
DIST_DIR = RELEASE_DIR / "dist"
BUILD_DIR = RELEASE_DIR / "build"
SPEC_DIR = RELEASE_DIR / "spec"
ICON_PATH = ROOT_DIR / "ui" / "assets" / "fashetai-app-icon.png"
ENTRY_PATH = ROOT_DIR / "seller_tools" / "license_issuer_gui.py"
INSTALL_DIR = Path.home() / "Applications"


def build_command() -> list[str]:
    return [
        "--noconfirm",
        "--clean",
        "--windowed",
        "--name",
        APP_NAME,
        "--distpath",
        str(DIST_DIR),
        "--workpath",
        str(BUILD_DIR),
        "--specpath",
        str(SPEC_DIR),
        "--osx-bundle-identifier",
        "com.yijianfa.license-issuer",
        "--icon",
        str(ICON_PATH),
        "--add-data",
        f"{ICON_PATH}:ui/assets",
        str(ENTRY_PATH),
    ]


def main() -> int:
    if importlib.util.find_spec("PyInstaller") is None:
        raise SystemExit("未安装 PyInstaller，请先安装 requirements-build.txt。")
    if not ICON_PATH.is_file():
        raise SystemExit(f"未找到应用图标：{ICON_PATH}")

    import PyInstaller.__main__

    PyInstaller.__main__.run(build_command())
    built_app = DIST_DIR / f"{APP_NAME}.app"
    if not built_app.is_dir():
        raise SystemExit(f"未生成应用：{built_app}")
    forbidden_names = {"license-private.json", "issue-history.json"}
    if any(path.name in forbidden_names for path in built_app.rglob("*")):
        raise SystemExit("构建产物意外包含卖家私钥，已停止安装。")

    import os
    import subprocess

    test_env = os.environ.copy()
    test_env["QT_QPA_PLATFORM"] = "offscreen"
    executable = built_app / "Contents" / "MacOS" / APP_NAME
    completed = subprocess.run(
        [str(executable), "--ui-test"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env=test_env,
    )
    if "SELLER_LICENSE_UI_OK" not in completed.stdout:
        raise SystemExit("卖家激活码管理器未通过离屏界面自检。")

    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    installed_app = INSTALL_DIR / built_app.name
    if installed_app.exists():
        shutil.rmtree(installed_app)
    shutil.copytree(built_app, installed_app, symlinks=True)
    print(f"卖家桌面应用已安装：{installed_app}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
