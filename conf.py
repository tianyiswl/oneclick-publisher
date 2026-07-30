"""一键发桌面端的资源与本机运行数据配置。"""

import sys
from pathlib import Path


# PyInstaller 冻结后，资源位于只读的包内目录；账号、素材和数据库必须
# 始终写入当前 macOS 用户的 Application Support，不能随安装包分发。
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
if getattr(sys, "frozen", False) and sys.platform == "darwin":
    BASE_DIR = Path.home() / "Library" / "Application Support" / "一键发"
else:
    BASE_DIR = Path(__file__).resolve().parent / "demo-runtime"
XHS_SERVER = ""
LOCAL_CHROME_PATH = ""
USE_SYSTEM_BROWSER = False
ACTIVATION_SERVER_URL = ""
ACTIVATION_TIMEOUT_SECONDS = 1
REQUIRE_ONLINE_ENTITLEMENTS = False
DEBUG_SKIP_FINAL_PUBLISH = True
DEBUG_DRY_RUN_HOLD_SECONDS = 0
