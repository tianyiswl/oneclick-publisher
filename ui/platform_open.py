# -*- coding: utf-8 -*-
"""跨平台打开文件和目录。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def open_path(path: str | Path) -> None:
    """使用当前系统默认程序打开文件或目录。"""
    target = Path(path)
    if sys.platform.startswith("win"):
        os.startfile(str(target))  # type: ignore[attr-defined]
        return
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(target)])
        return
    subprocess.Popen(["xdg-open", str(target)])


def reveal_in_folder(path: str | Path) -> None:
    """在文件管理器中打开目标所在目录。"""
    target = Path(path)
    folder = target if target.is_dir() else target.parent
    if sys.platform.startswith("win"):
        subprocess.Popen(["explorer", str(folder)])
        return
    open_path(folder)
