# 抖音省份地点搜索：0.5.10 验证记录

日期：2026-08-26（Asia/Shanghai）

## 结论

`0.5.10` 源码候选已完成本地离线验证；它包含省份词按城市逐一查询、恢复进度和有效地点计数。没有打包、安装、真实带货账号验证或正式发布。

## 验证证据

- 后修复验证基线：`ee53940b1edca45bf64c37ae1057ece1ce5b746b`（最终复审已批准合入）。
- Source version: `0.5.10`。
- Focused tests:

  ```text
  QT_QPA_PLATFORM=offscreen ../../.venv/bin/python -m unittest -v test_douyin_location_search_plan test_douyin_location_cache test_douyin_commerce_service.DouyinCommerceBatchUiTests
  ```

  结果：`259` 项，PASS（`16.392s`）。
- Affected modules:

  ```text
  QT_QPA_PLATFORM=offscreen ../../.venv/bin/python -m unittest -v test_douyin_commerce_collectors test_douyin_commerce_setup_state test_douyin_commerce_batch_draft_service test_douyin_commerce_service test_douyin_location_cache
  ```

  结果：`750` 项，PASS（`53.757s`）。
- Full suite:

  ```text
  QT_QPA_PLATFORM=offscreen ../../.venv/bin/python -m unittest discover -v
  ```

  结果：`2012` 项，PASS（`77.989s`）。
- Source client self-check:

  ```text
  QT_QPA_PLATFORM=offscreen ../../.venv/bin/python desktop_native_app.py --ui-test
  ```

  Exact output: `NATIVE_DESKTOP_UI_OK`。

  Task brief 中的裸 `desktop_native_app.py` 会启动常规桌面事件循环而不退出；本轮已停止该自检进程，改用项目 README 规定的 `--ui-test` 离屏自检入口。该入口不打开抖音浏览器或执行平台动作。
- Scope/source-live tests:

  ```text
  ../../.venv/bin/python -m unittest -v test_workstream_scope test_source_live_launcher test_source_live_runtime
  ```

  结果：`18` 项，PASS。
- Real Douyin commerce account: NOT RUN。
- Package/installer: NOT BUILT。
- Formal publish: NOT RUN。

## 状态与自检

- `APP_VERSION` 已从 `0.5.9` 递增为 `0.5.10`。
- `SOURCE_OF_TRUTH.md` 已标明本轮仅有源码/本地离线证据，未把历史 `0.5.9` 安装包证据写成 `0.5.10` 打包证据。
- 自检确认测试记录使用后修复本轮实际计数，未复用历史 `1940/1940`；没有触碰真实账号、发布、打包或安装。

## 剩余真实检查

需要在具备带货能力的抖音账号中运行 `广东joymark`，确认连续点击会切换城市、添加合格地点，并回读页面和运行日志。该证据未取得前，`0.5.10` 只能称为本地源码候选。
