# 国内平台数据监测最终修复报告

- 修复前 HEAD：`47d94c7`
- 对照规格：`docs/superpowers/specs/2026-08-21-domestic-platform-data-monitoring-design.md`
- 对照计划：`docs/superpowers/plans/2026-08-21-domestic-platform-data-monitoring.md`
- 结果：8 条审查意见全部修复；受影响测试 `174/174` 通过，全量测试 `1389/1389` 通过。
- 验证边界：代码、本地持久化和离线 UI 已验证；本轮未使用真实平台账号执行小红书采集。

## Finding 修复映射

| # | 级别 | 修复 | 聚焦测试 |
|---|---|---|---|
| 1 | Important | `DomesticBrowserCollector` 在路径白名单之前对每个唯一请求计数，并持有请求引用防止对象 ID 复用绕过。白名单响应仍由独立 `response_count` 限制。 | `test_domestic_data_collector.py::DomesticBrowserCollectorTests::test_collector_total_request_limit_counts_unreviewed_page_noise` |
| 2 | Important | `sync_account_data` 保留抖音/小红书的 `collect_direct` 到 `collect_browser_signed` 双路径，同时兼容只实现统一 `collect()` 协议的国内平台采集器，并严格校验 `CollectionBatch`。 | `test_platform_data_sync.py::PlatformDataSyncTests::test_registry_syncs_a_collect_only_domestic_browser_collector` |
| 3 | Important | 新增受控失败收口 `_record_failed_result`。持久化异常返回 `sync_persist_failed`，指标/作品数均为 0；数据库仍可写时尽力记录失败运行。UI 只显示固定文案。 | `test_platform_data_sync.py::PlatformDataSyncTests::test_persistence_exception_returns_controlled_failure_and_records_when_possible`<br>`test_platform_data_sync.py::PlatformDataSyncTests::test_persistence_failed_result_emits_failed_not_partial`<br>`test_data_monitor_page.py::DataMonitorPageTests::test_immediate_persist_failure_uses_fixed_copy_without_exception_text` |
| 4 | Important | `parse_content_list` 通过 `XhsContentPage` 返回受限作品和截断标记。`total` 大于当前页数量时保留最近作品与账号指标，以 `content_list_truncated` 持久化为部分成功。服务返回 `coveredCount`，页面显示“仅取得最近 N 条”。 | `test_xiaohongshu_data_collector.py::XiaohongshuDataContractTests::test_content_list_marks_normal_platform_pagination_without_dropping_rows`<br>`test_xiaohongshu_data_collector.py::XiaohongshuDataCollectorTests::test_paginated_content_list_keeps_recent_content_as_partial_success`<br>`test_platform_data_service.py::PlatformDataServiceTests::test_xhs_truncated_page_persists_account_and_recent_content_as_partial`<br>`test_data_monitor_page.py::DataMonitorPageTests::test_content_header_distinguishes_unavailable_history_and_true_zero` |
| 5 | Important | 模型层增加 `unsupported_account_metric_keys`。小红书合同已知不提供的账号指标标为 `unsupported`；仍需等待跨日快照的 `followers_net` 保持 `missing`。UI 分别显示“平台未提供”与“暂未取得”。 | `test_platform_data_service.py::PlatformDataServiceTests::test_xhs_model_capability_distinguishes_unsupported_from_snapshot_pending`<br>`test_platform_data_service.py::PlatformDataServiceTests::test_xhs_period_summary_marks_contract_absence_but_keeps_delta_pending`<br>`test_data_monitor_page.py::DataMonitorPageTests::test_xhs_cards_distinguish_unsupported_from_snapshot_pending` |
| 6 | Important | 小红书观察时间先转换为 `Asia/Shanghai`，观察时间和快照日期共用同一北京时间投影；无时区时间值关闭式拒绝。 | `test_xiaohongshu_data_collector.py::XiaohongshuDataCollectorTests::test_observation_day_uses_beijing_date_during_utc_boundary` |
| 7 | Minor | 同步回调立即返回 `login_required` 时直接显示“重新登录”；新同步开始或普通 worker 错误时清除旧按钮状态。 | `test_data_monitor_page.py::DataMonitorPageTests::test_immediate_login_required_result_shows_relogin_action` |
| 8 | Minor | 删除 implementation plan 末尾多余空行；文件现以单个换行结束。提交区间空白检查无输出、退出码 0。 | `git diff --check 174648d..HEAD` |

## TDD 证据

### 修复前基线

`QT_QPA_PLATFORM=offscreen ../../.venv/bin/python -m unittest -v test_domestic_data_collector test_xiaohongshu_data_collector test_platform_data_sync test_platform_data_service test_data_monitor_page`

精确结果：`Ran 133 tests in 0.428s`，`OK`。

### 先加测试后的红灯

一次运行新增/调整的 14 个聚焦用例，覆盖非白名单请求淹没、`collect()` 注册表同步、持久化受控失败、小红书分页/部分持久化、能力语义、北京时间边界、最近 N 条文案和即时重登按钮。

精确结果：`Ran 14 tests in 0.161s`，`FAILED (failures=9, errors=4)`。13 个用例按预期红灯；“UI 收到已受控的持久化失败结果时显示固定文案”在旧 UI 边界已通过，对应同步层异常路径依然保持红灯。

### 逐步绿灯

| 命令 | 精确结果 |
|---|---|
| `../../.venv/bin/python -m unittest -v test_domestic_data_collector` | `Ran 11 tests in 0.014s`，`OK` |
| `QT_QPA_PLATFORM=offscreen ../../.venv/bin/python -m unittest -v test_platform_data_sync` | `Ran 22 tests in 0.006s`，`OK` |
| `QT_QPA_PLATFORM=offscreen ../../.venv/bin/python -m unittest -v test_xiaohongshu_data_collector` | `Ran 47 tests in 0.031s`，`OK` |
| `QT_QPA_PLATFORM=offscreen ../../.venv/bin/python -m unittest -v test_platform_data_service` | `Ran 32 tests in 0.371s`，`OK` |
| `QT_QPA_PLATFORM=offscreen ../../.venv/bin/python -m unittest -v test_data_monitor_page` | `Ran 33 tests in 0.354s`，`OK` |

### 最终受影响范围

`QT_QPA_PLATFORM=offscreen ../../.venv/bin/python -m unittest test_domestic_data_collector test_xiaohongshu_data_collector test_douyin_data_collector test_platform_data_sync test_platform_data_service test_data_monitor_page`

精确结果：`Ran 174 tests in 0.666s`，`OK`。这一轮在最后一次源码调整后执行，并额外包含抖音采集器回归。

### 唯一一次全量回归

`QT_QPA_PLATFORM=offscreen ../../.venv/bin/python -m unittest discover -v`

精确结果：`Ran 1389 tests in 82.299s`，`OK`。

## 安全与仓库检查

`rg -n 'Cookie|Authorization|Set-Cookie|response\.text|response\.body' app_core/*_data_collector.py docs/verification/domestic-platform-data-monitoring-2026-08-21.md`

结果：无匹配。

`rg -n '(print\(|logger\.|logging\.)' app_core/domestic_data_collector.py app_core/xiaohongshu_data_collector.py app_core/platform_data_sync.py app_core/platform_data_service.py ui/data_monitor_page.py`

结果：无匹配。本轮没有 Cookie、Authorization 或原始 response body 日志。

`git diff --check 174648d`

`git diff --check 174648d..HEAD`

结果：两次均无输出，退出码 0。

## 未解决 concerns

- 无未解决代码审查项。
- 真实小红书已登录会话采集与官方页面回读不在本轮离线修复授权范围内，因此不声称真实平台运行已验证。
