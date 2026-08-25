# -*- coding: utf-8 -*-
"""产品品牌与构建标识的唯一配置入口。"""

from __future__ import annotations


PRODUCT_NAME = "一键发"
PRODUCT_TAGLINE = "多平台内容发布工作台"
APP_TITLE = f"{PRODUCT_NAME}· {PRODUCT_TAGLINE}"
APP_VERSION = "0.5.9"
APP_ICON_RELATIVE_PATH = "ui/assets/fashetai-app-icon.png"
TRIAL_DAYS = 7
UPGRADE_STORE = "逆浪风"

# 构建产物使用独立项目的 ASCII 名称，避免 Windows 打包和解压路径兼容问题。
# 用户可见名称始终为“一键发”；此名称同时决定 Windows ZIP 顶层目录和 exe 名称。
APP_EXECUTABLE_NAME = "YiJianFa"
ACTIVATION_PRODUCT_ID = "fashetai_desktop"

# 发射台等旧标识只用于本地升级识别和服务端兼容，不再用于新构建产物。
LEGACY_APP_EXECUTABLE_NAMES = ("Fashetai", "MatrixArk", "zimeiti_desktop")
LEGACY_ACTIVATION_PRODUCT_IDS = ("matrixark_desktop", "zimeiti_desktop")
