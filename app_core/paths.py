# -*- coding: utf-8 -*-
"""项目路径常量。"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from conf import BASE_DIR as CONFIGURED_USER_DATA_DIR
from conf import RESOURCE_DIR


ROOT_DIR = Path(RESOURCE_DIR)
LEGACY_RUNTIME_ROOT = Path(__file__).resolve().parent.parent
USER_DATA_DIR = Path(CONFIGURED_USER_DATA_DIR)
# OAuth 客户端密钥和平台令牌比普通演示数据更敏感。开发版的
# USER_DATA_DIR 位于仓库 demo-runtime，因此官方 OAuth 数据仍必须单独放入
# 系统用户目录，避免被 Git、打包资源或演示数据误收集。
PRIVATE_DATA_DIR = (
    Path.home() / "Library" / "Application Support" / "一键发"
    if sys.platform == "darwin"
    else Path.home() / ".oneclick-publisher"
)
DB_PATH = USER_DATA_DIR / "db" / "database.db"
VIDEO_DIR = USER_DATA_DIR / "videoFile"
COOKIE_DIR = USER_DATA_DIR / "cookiesFile"
AVATAR_DIR = USER_DATA_DIR / "avatars"
LOG_DIR = USER_DATA_DIR / "logs"
COVER_DIR = AVATAR_DIR / "media_covers"
TIKTOK_COOKIE_DIR = USER_DATA_DIR / "cookies"
LEGACY_ACTIVATION_FILE = LEGACY_RUNTIME_ROOT / "db" / "activation.json"
ACTIVATION_FILE = USER_DATA_DIR / "license-state.json"
MIGRATION_MARKER = USER_DATA_DIR / ".legacy-data-migrated-v1"
LEGACY_RUNTIME_DIRS = ("db", "cookies", "cookiesFile", "videoFile", "avatars", "logs")


def _copy_missing_tree(source: Path, destination: Path) -> None:
    for source_path in source.rglob("*"):
        relative = source_path.relative_to(source)
        destination_path = destination / relative
        if source_path.is_dir():
            destination_path.mkdir(parents=True, exist_ok=True)
            continue
        if source_path.is_file() and not destination_path.exists():
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination_path)


def migrate_legacy_runtime_data() -> None:
    """把旧版程序目录中的运行数据复制到系统用户数据目录。"""

    if MIGRATION_MARKER.exists():
        return
    try:
        if LEGACY_RUNTIME_ROOT.resolve() != USER_DATA_DIR.resolve():
            for dirname in LEGACY_RUNTIME_DIRS:
                source = LEGACY_RUNTIME_ROOT / dirname
                if source.is_dir():
                    _copy_missing_tree(source, USER_DATA_DIR / dirname)
        USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        MIGRATION_MARKER.write_text("legacy runtime data migrated\n", encoding="utf-8")
    except OSError:
        # 迁移失败时仍允许程序创建新目录；下一次启动会继续尝试迁移。
        return


def ensure_runtime_dirs() -> None:
    migrate_legacy_runtime_data()
    for path in (
        DB_PATH.parent,
        VIDEO_DIR,
        COOKIE_DIR,
        TIKTOK_COOKIE_DIR,
        AVATAR_DIR,
        LOG_DIR,
        COVER_DIR,
        USER_DATA_DIR,
        PRIVATE_DATA_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)
