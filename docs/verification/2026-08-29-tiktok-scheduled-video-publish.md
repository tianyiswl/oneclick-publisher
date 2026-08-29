# TikTok 平台原生定时发布验证

## 本地证据

- 分支与提交：`feature/overseas-login-publish-v2@8c23de2592abf752311b88fac2a777a2dfac62d1`（文档提交前的实现基线）。
- 架构图：Architecture 和 Lifecycle 的 Archify `validate` / `deliver` 均为 `9/9`，`0` errors，`0` warnings。Architecture 源码证据已固定到上述实现基线，并验证 `25` 个源码引用。两图 `visual-check` 在 `1440×900`、`1600×1000`、`1920×1080`、`2048×1320` 均无横向或纵向溢出；已人工检查最小/最大尺寸的明暗主题截图，未见裁切、穿越无关节点或歧义走廊。
- 聚焦测试：`.venv/bin/python -m unittest -v test_task_service test_controlled_publish test_publish_service test_overseas_publish_routing test_tiktok_schedule_contract test_overseas_tiktok_schedule_form test_overseas_tiktok_publish test_overseas_video_publish`，`297/297` 通过，用时 `1.207s`，进程返回值 `0`。
- 海外受影响测试：`.venv/bin/python -m unittest -v test_account_detection_ui test_controlled_publish test_oneclick_authorization test_oneclick_capabilities test_overseas_integration test_overseas_publish_routing test_overseas_tiktok_identity test_overseas_tiktok_profile test_overseas_tiktok_publish test_tiktok_schedule_contract test_overseas_tiktok_schedule_form test_overseas_tiktok_session_scope test_overseas_tiktok_system_login test_overseas_video_publish test_task_service test_tiktok_login_startup_wiring`，`512/512` 通过，用时 `2.635s`，进程返回值 `0`。
- 完整回归：`.venv/bin/python -m unittest discover -p 'test_*.py'`，`2611/2611` 通过，用时 `77.963s`，进程返回值 `0`。
- 离屏客户端：`QT_QPA_PLATFORM=offscreen .venv/bin/python desktop_native_app.py --ui-test` 返回 `NATIVE_DESKTOP_UI_OK`，进程返回值 `0`。
- 差异检查：`git diff --check origin/main...HEAD` 和 `git diff --check` 均返回 `0`，输出字节数均为 `0`。
- 范围检查：`.venv/bin/python tools/check_workstream_scope.py --stream overseas --base origin/main` 返回 `2 / REVIEW_REQUIRED`；TikTok 实现、架构、规格、计划和验证路径均为 `owned`，已批准的核心协调文件为 `shared`，`outside=0`。

## 平台动作

- 本轮未打开 TikTok Studio、未启动真实平台浏览器会话、未上传视频、未点击 `Post` / `Schedule`。

## 尚未验证

- 当前账号定时入口真实可用性。
- TikTok 对正式 `Schedule` 的受理与定时列表回读。
- 本地测试通过不代表平台原生定时已在真实账号验收。
