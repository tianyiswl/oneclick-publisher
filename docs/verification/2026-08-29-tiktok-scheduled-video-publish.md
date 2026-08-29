# TikTok 平台原生定时发布验证

## 本地证据

- 最终复审修复实现提交：海外专属运行时与测试 `7151f17e77500b53c287fd4ba68f40a37b85808e`，共享核心与测试 `41971c59213fed2f17392289eb61dfd61e870caf`，海外会话边界集成回归 `6d11216e960ef68ed78e618f81ef8c331f7cf5f6`。
- 严格 TDD：`5` 次 RED 命令共运行 `20` 个顶层测试，得到预期的 `9` 个 failure 与 `12` 个 subtest error；对应 `5` 次 GREEN 命令 `29/29` 通过，另有 receipt-only 不可逆证据确认 `2/2` 通过。逐命令证据见 `.superpowers/sdd/2026-08-29-tiktok-scheduled-video-publish/final-fix-report.md`。
- 架构图：Architecture 和 Lifecycle 的最终 Archify `validate` / `deliver` 均为 `9/9`、`0` errors、`0` warnings。Architecture 固定到实现基线 `6d11216e960ef68ed78e618f81ef8c331f7cf5f6` 并验证 `29` 个源码引用，spec/artifact SHA256 分别为 `2ef5bc8a76191f9c7b74c36f52715de38806e79606e6e44d96a7adf43c576812` / `5de8bf62b6dbd1d9b6a4563db5e223083c483e87e7af58f4ff976b6f552b329b`。Lifecycle spec/artifact SHA256 分别为 `8e1ad65fc6569901e855384624f01179b1216776e87dbda04fa73c3f526e550b` / `b0a9ec122b8c471b330b58703fbef11a6d9bb94389cb5ec18aa859a9db0b07b1`。
- 两图最终 `visual-check` 在 `1440×900`、`1600×1000`、`1920×1080`、`2048×1320` 均无横向或纵向溢出。已人工检查两图最小/最大尺寸的明暗主题截图，未见裁切、穿越无关节点、歧义走廊或不可读文字；Architecture 修正轮次 `0`，Lifecycle 修正轮次 `2`。
- 海外受影响测试：`.venv/bin/python -m unittest test_account_detection_ui test_controlled_publish test_oneclick_authorization test_oneclick_capabilities test_overseas_integration test_overseas_publish_routing test_overseas_tiktok_identity test_overseas_tiktok_profile test_overseas_tiktok_publish test_tiktok_schedule_contract test_overseas_tiktok_schedule_form test_overseas_tiktok_session_scope test_overseas_tiktok_system_login test_overseas_video_publish test_task_service test_tiktok_login_startup_wiring`，`528/528` 通过，用时 `2.935s`，进程返回值 `0`。
- 完整回归：`.venv/bin/python -m unittest discover -p 'test_*.py'`，`2627/2627` 通过，用时 `83.195s`，进程返回值 `0`。
- 离屏客户端：`QT_QPA_PLATFORM=offscreen .venv/bin/python desktop_native_app.py --ui-test` 返回 `NATIVE_DESKTOP_UI_OK`，进程返回值 `0`。
- 差异检查：`git diff --check origin/main...HEAD` 和 `git diff --check` 均返回 `0`，输出字节数均为 `0`。
- 范围检查：`.venv/bin/python tools/check_workstream_scope.py --stream overseas --base origin/main` 返回 `2 / REVIEW_REQUIRED`；`changed=63`，其中 `owned=39`、`shared=24`、`outside=0`。TikTok 实现、架构、规格、计划和验证路径均为 `owned`，核心协调文件为待集成线复审的 `shared`。
- 最终复审阻断修复覆盖：定时结果不明、受理、回读与脱敏最终动作回执的统一不可逆证据保护；真实页面与列表行账号身份独立核对；点击前稳定列表基线及唯一新排期；发布加载/刷新全程 TikTok-only 会话；定时公开状态投影；Schedule 后通用异常归一化；CRLF/CR 正文换行统一。未修改 `10` 个延期 Minor，也未修改登录取消 Minor。

## 平台动作

- 本轮最终复审修复未打开 TikTok Studio、未启动真实平台浏览器会话、未上传视频、未点击 `Post` / `Schedule`，也未打包、升级版本、合并或推送。Archify 只在本机离线 HTML 上执行截图检查。

## 尚未验证

- 当前账号定时入口真实可用性。
- TikTok 对正式 `Schedule` 的受理与定时列表回读。
- 本地测试通过不代表平台原生定时已在真实账号验收。
