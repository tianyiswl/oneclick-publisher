"""一键发桌面端的资源与本机运行数据配置。"""

import os
import sys
from pathlib import Path


def resolve_user_data_dir(
    *,
    frozen: bool | None = None,
    platform_name: str | None = None,
    home: Path | None = None,
    local_app_data: Path | None = None,
    source_dir: Path | None = None,
) -> Path:
    """返回运行数据目录；冻结客户端绝不写入安装包目录。"""

    is_frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    current_platform = sys.platform if platform_name is None else platform_name
    current_home = Path.home() if home is None else Path(home)
    project_dir = Path(__file__).resolve().parent if source_dir is None else Path(source_dir)
    if not is_frozen:
        return project_dir / "demo-runtime"
    if current_platform == "darwin":
        return current_home / "Library" / "Application Support" / "一键发"
    if current_platform == "win32":
        app_data = local_app_data or os.environ.get("LOCALAPPDATA")
        return Path(app_data) / "一键发" if app_data else current_home / "AppData" / "Local" / "一键发"
    return current_home / ".local" / "share" / "一键发"


# PyInstaller 冻结后，资源位于只读的包内目录；账号、素材和数据库必须
# 始终写入当前操作系统的用户数据目录，不能随安装包分发。
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
BASE_DIR = resolve_user_data_dir()
XHS_SERVER = ""
LOCAL_CHROME_PATH = ""
USE_SYSTEM_BROWSER = False
ACTIVATION_SERVER_URL = ""
ACTIVATION_TIMEOUT_SECONDS = 1
REQUIRE_ONLINE_ENTITLEMENTS = False
DEBUG_SKIP_FINAL_PUBLISH = True
DEBUG_DRY_RUN_HOLD_SECONDS = 0
