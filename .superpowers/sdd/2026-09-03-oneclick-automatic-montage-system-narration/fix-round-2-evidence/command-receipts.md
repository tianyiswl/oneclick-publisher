# Task 7 fix round 2 命令收据

日期：2026-09-03

结论：公众号已授权定时任务在启动发布服务前，会通过稳定页面 key 切换到用户可见的“发布中心”。全部验收均在 detached 纯净 worktree `/private/tmp/oneclick-task7-fix1-663b68e` 上执行，不归因于原工作树的其他未提交改动。

## 源码快照与提交对应

- RED 基线：`1dc3a54e13f6a4deea887f54f1d47b361641faac`
- 纯净验证提交：`f86ce818cf3f9512999ae32565f28465be8579e5`
- 实际分支对应提交：`ce9c41c1bd0f1671261aaa13c17328d315f76bdf`
- 两个 GREEN 提交的 tree object 均为 `fdaf94c9a888ab6c76d9870a175a0ab0a901a482`，因此验证的源码与分支提交逐字一致。
- 纯净 worktree 在最终验证前后 `git status --porcelain` 均为空。

## TDD RED

```bash
.venv/bin/python -m unittest -v \
  test_desktop_wechat_schedule_wiring.DesktopWechatScheduleWiringTests.test_authorized_schedule_is_visible_on_publish_page_before_task_start
```

结果：`1` 项测试，`FAIL`，退出码 `1`。断言精确证明旧实现在启动已授权公众号任务时可见页仍为 `montage` 而非 `publish`：

```text
AssertionError: 'montage' != 'publish'
: authorized WeChat schedule started while its page was hidden
```

输出：`command-output/red-authorized-wechat-navigation.log`
SHA-256：`9e766fb0a01926ff45784f24bd259165bd26e3a3b9a4f46c930f99afad3be193`

## TDD GREEN

同一条命令在把固定索引改为 `window.set_current_page_by_key("publish")` 后重跑。

结果：`1/1`，`OK`，用时 `0.003s`，退出码 `0`。
输出：`command-output/green-authorized-wechat-navigation.log`
SHA-256：`22910fb852015b7ee4d1c4521ac787889ea08752612fbc19360b93efdf2aa5b9`

## 受影响公众号与 UI 套件

```bash
.venv/bin/python -m unittest -v \
  test_desktop_wechat_schedule_wiring test_desktop_wechat_draft_bridge_wiring \
  test_main_window test_publish_page test_wechat_publish_policy \
  test_wechat_publish_executor test_wechat_preflight
```

结果：`120/120`，`OK`，用时 `8.787s`。
输出：`command-output/wechat-entry-related-f86ce81.log`
SHA-256：`10f7b91b64c80afd0089ed404709fa7aaf884ab563a42aa02d23c1089f7b96cb`

## 定向混剪与真实 Mac 系统配音集成

```bash
ONECLICK_MONTAGE_EVIDENCE_DIR='<fix-round-2-evidence>/integration-batch-head-f86ce81' \
  QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest -v \
  test_montage_models test_narration_service test_montage_runtime \
  test_montage_service test_automatic_montage_page \
  test_montage_narration_integration test_main_window
```

结果：`76/76`，`OK`，用时 `6.608s`；真实 Mac 配音测试未跳过。隔离批次 `M-NARRATION-INTEGRATION` 回执为 `success=3`、`failed=0`，讲话 `8638ms`，主音轨/成片 `8938ms`。
输出：`command-output/montage-directed-f86ce81.log`
SHA-256：`87e809b2be26096ff5a2a01c3687b2a0032abea577f87bd7471607daae7c851b`

## 离屏源码客户端

```bash
YIJIANFA_USER_DATA_DIR='<fix-round-2-evidence>/ui-test-head-f86ce81' \
  QT_QPA_PLATFORM=offscreen .venv/bin/python desktop_native_app.py --ui-test
```

结果：退出码 `0`，输出 `NATIVE_DESKTOP_UI_OK`。
输出：`command-output/offscreen-ui-f86ce81.log`
SHA-256：`c4252eec19a1f7eb7644b89c440db3b1a45ec2eef0b6077113fb278db1fcbb17`

## 共享桌面入口全量回归

```bash
.venv/bin/python -m unittest
```

结果：`3175/3175`，`OK`，用时 `192.909s`。
输出：`command-output/full-unittest-f86ce81.log`
SHA-256：`183580ab0c1533da2d374658abd00ae7d692c4e9fa0e3ecee55784dfe015bf99`

## 进程与人工听审边界

- Fix round 1 的纯净源码客户端已退出，原 QuickTime `video.mp4` 窗口已关闭。
- Fix round 2 没有重新启动可视源码客户端，没有保留 QuickTime `0:00` 人工听审停点，没有播放或自行宣称听感通过。
- 未改版本、未打包、未推送，未创建发布任务，未操作任何平台或账号。
