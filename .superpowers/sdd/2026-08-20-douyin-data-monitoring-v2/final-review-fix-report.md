# 抖音数据监测 V2 final review fix 报告

## 结论与证据层级

- 基线 HEAD：`e25ed2317f0a7c4ebee339b95005c49731067b69`。
- 本轮完成 C1 / I1 / I2 / I3 / I4 / M1 的离线交付闭环：实现、回归测试和公开边界检查均有新证据。
- 未执行真实账号、真实浏览器或平台发布；因此不声称真实运行闭环、作品 endpoint 可用或业务结果闭环。
- 未增加或放宽作品 endpoint / allowlist；当无经验证作品响应时，安全终态仍是“账号趋势完成，作品数据未取得”。

## 旧 1208 证据降级

`task-6-report.md` 记录的 round2 历史 full 证据为 `Ran 1208 tests in 59.853s` / `OK` / `exit_code=0`，对应提交 `e25ed23`。本 final fix wave 修改了生产代码，因此该 1208 只保留为历史基线，不能再作当前树的通过证据。当前证据以本报告下方的新鲜 affected 与唯一 final full 为准。

## 各项 TDD RED / GREEN

### C1：1 / 7 / 30 天统一结束于北京昨日

- 命名测试：`test_period_ranges_end_at_beijing_yesterday_across_calendar_boundaries`。
- RED：`Ran 1 test`，2 个 subtest 失败；跨年实际为 `2027-01-01`，期望 `2026-12-31`；跨月实际为 `2026-03-01`，期望 `2026-02-28`。
- GREEN：`Ran 1 test in 0.005s` / `OK`。
- 修复：`periodEnd = _beijing_today() - 1 day`，`periodStart = periodEnd - (days - 1)`；测试覆盖 1 / 7 / 30 天、跨月、跨年及北京时间零点后的日历边界。

### I1：作品查询可用性、warning 与历史时间

- 命名测试：`test_account_contents_exposes_latest_availability_and_trusted_time`、`test_content_header_distinguishes_unavailable_history_and_true_zero`、`test_account_contents_rejects_uncontrolled_data_times`。
- RED ①：服务输出缺少 `availability`，触发 `KeyError`；UI 将 unavailable 空列表显示为“共 0 条 · 0-0”。`Ran 2 tests`，1 failure + 1 error。
- GREEN ①：`Ran 2 tests in 0.139s` / `OK`。
- RED ②：畸形路径/query 字符串被当作时间原样返回。`Ran 1 test`，1 failure。
- GREEN ②：`Ran 1 test in 0.009s` / `OK`。
- 修复：`account_contents` 输出受控 `available / partial / unavailable / missing`、`warningCode`、`platformObservedAt`、`localSyncedAt`；最新尝试决定本次 availability，最新可信作品 run 决定历史数据时间。非受控时间不公开。UI 分别显示“作品数据暂未取得”、“本次未取得，显示历史 N 条”、“部分取得”和真实 `available` 的“已取得 0 条”。

### I2：单次平台响应唯一观测时间

- 命名测试：`test_direct_response_uses_one_real_observation_time_for_all_points`、`test_browser_response_uses_one_real_observation_time_for_all_points`。
- RED：直连 points 仍为 `2026-08-20T00:00:00+08:00`；浏览器 points 同时出现 `2026-08-19T00:00:00+08:00` / `2026-08-20T00:00:00+08:00`，均不等于 batch 观测时间。`Ran 2 tests`，2 failures。
- GREEN：`Ran 2 tests in 0.004s` / `OK`。
- 修复：每个 direct 或 browser-signed 响应只调用一次本地观测时钟，同时写入 `batch.platform_observed_at` 与该响应全部 `MetricPoint.observed_at`；`periodStart / periodEnd` 继续保留平台统计日。浏览器粉丝总数同样受此约束。

### I3：batch flags / entity / warning 不变量

- 命名测试：`test_collection_batch_enforces_flag_entity_and_warning_invariants`。
- RED：10 组矛盾批次全部未抛错，`Ran 1 test`，10 subtest failures。
- GREEN：`Ran 1 test in 0.007s` / `OK`。
- 修复：本产品 batch 必须存在 account points；`account_metrics_available=True` 不能只由 content points 充数；本产品拒绝无 account 批次。`content_data_available=True` 必须有作品元数据和与全部作品 ID 精确匹配的 lifetime points；false 不得夹带 contents / content points。完整批次 warning 为空，unavailable / payload invalid 只允许 content=false，truncated 只允许 content=true；任一矛盾固定 `metric_payload_invalid`。

### I4：legacy `account_data_summary` 只读 account

- 命名测试：`test_legacy_summary_uses_latest_account_period_and_ignores_content`。
- 测试夹具先碰到同 run 唯一索引，修正为合法乱序数据后取得确定性 RED：实际 `{'views': 9999, 'comments': 5, 'likes': 8888}`，期望 `{'views': 190, 'comments': 19, 'likes': 7}`。`Ran 1 test`，1 failure。
- GREEN：`Ran 1 test in 0.007s` / `OK`。
- 修复：仅查询 `entityType='account'`。V2 rows 先按 `metricKey`，再按最新 `periodEnd / id` 选取；对旧 `metricScope / periodStart / periodEnd` 全空 rows 继续兼容，但仍只接受 account。content lifetime 不再覆盖同名账号指标。

### M1：拒绝重复 `content_id`

- 命名测试：`test_collection_batch_rejects_duplicate_content_ids`。
- RED：重复 `aweme-1` 未抛错，`Ran 1 test`，1 failure。
- GREEN：`Ran 1 test in 0.006s` / `OK`。
- 修复：`CollectionBatch` 对 `contents` 的 ID 序列与集合长度作一致性校验，重复固定 `metric_payload_invalid`。

## affected 与静态验证

- affected 采用 `unittest -f`。每次遇到首个失败即停，单独复现后修正；期间失败均为旧测试夹具仍构造“content=false 但无 warning”、“content=true 但无 content points”、“无 account points”或仍以当天为区间末日；每个失败的命名单项均立即回归为 `OK`。
- 最终 affected：`test_platform_data_service` + `test_douyin_data_collector` + `test_platform_data_sync` + `test_data_monitor_page` + `test_main_window`，`Ran 81 tests in 0.730s` / `OK` / exit 0。
- `py_compile` 覆盖 models / service / collector / sync / UI；退出码 0。
- `git diff --check`：退出码 0。
- 生产文件敏感模式 `Cookie|sessionid|responseBody|Authorization|Bearer ` 扫描：退出码 1，无输出，表示无命中。
- `.venv` 是本轮开始前已存在的未跟踪环境入口，不纳入提交。

## 唯一 final full 证据

- 生产最后改动完成后，仅执行一次：`QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -v`。
- 运行会话：`session_id=46711`；终态：`exit_code=0`。
- 完整终态摘要：`Ran 1218 tests in 58.199s` / `OK`。该证据取代旧 1208，作为当前提交树的全量通过证据；全量结束后未再修改生产代码，也未重复执行 full discover。

## 保留的架构与安全边界

- 单事务写入、mixed provenance、latest attempt / trusted freshness、账号内存终态、同步 success / partial_success / failed 三态未改弱。
- 未增加作品 endpoint，未放宽响应 allowlist，未构造或保存平台原始响应。
- 公开查询仅输出受控业务字段；损坏时间、路径、query/token 和内部错误不进入 UI。

## 残余关注点

1. 当前仍无实测、允许列表内的官方作品 endpoint 证据；因此真实平台运行可能仍只有账号趋势，作品区必须继续显示未取得，不可报 0 或完整。
2. 本轮按明确约束不做真实账号/浏览器验收；真实数值与官方后台的同日对齐仍未验证。
