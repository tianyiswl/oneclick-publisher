# 抖音带货地点分页缓存 final-review 修复报告

## 1. 结论与边界

- 状态：`final-review-findings.md` 列出的 I1–I11 全部完成确定性 RED→最小 GREEN；3 项 Deferred Minor 同步关闭。
- 实现提交 SHA：`7ca356b8fb463d2a26cc16ba9838ef2f6ce7a398`（`7ca356b 修复抖音地点分页缓存最终审查问题`）。
- 闭环层级：交付闭环。代码、离线 DOM 契约、相关组合、唯一一次新全量、静态与安全扫描均通过。
- 未越界：未登录真实账号，未连接真实浏览器会话，未读写真实平台，未执行上传或发布。Playwright 用例仅在 headless 本地 HTML 上运行。
- 历史证据：`5d433e11868279664546714d52bccbf080b86e16` 的 `1048 tests / 40.944s / exit 0` 仅作历史记录，不再代表当前树。

## 2. I1–I11 逐项 RED→GREEN

### I1 — 真实 POI 四字段身份

- RED：`test_dom_preserves_real_poi_ids_across_commission_variants`，exit `1`，1 error；DOM 中的真实 ID 被名称/地址摘要覆盖，导致变体身份错误合并。
- GREEN：同一命名用例 `Ran 1 test in 0.555s`，exit `0`。
- 修复：保留 `data-poi-id` / `data-location-id` / `data-id`；只在平台未暴露 ID 时使用可见摘要；搜索、分页、去重、点击、回读、会话与缓存统一使用 `(poiId,name,address,commissionType)`；同完整身份重复安全停止。

### I2 — 设置页 10 / 100 / 两次零增长上限

- RED：`test_setup_load_more_stops_on_exact_tenth_click`、`test_setup_load_more_stops_on_exact_hundredth_identity`、`test_setup_load_more_stops_on_second_effective_zero_growth`、`test_setup_tenth_platform_result_disables_button_with_controlled_status`，exit `1`，4 failures。
- GREEN：上述 4 项 `Ran 4 tests in 0.155s`，exit `0`。
- 修复：派发前检查已用额度；回包后以累计四字段唯一身份重算有效增长；精确在 9→10、99→100、1→2 转换停止，禁用按钮并展示受控状态。

### I3 — 静默校对中原子处理缺失 POI

- RED：缓存层 `test_confirmed_exhaustion_reconciles_returned_and_missing_revalidation_rows` 与 `test_empty_or_unconfirmed_revalidation_only_marks_confirmed_missing_rows` 因缺少 `reconcile_platform_locations` 发生 2 ImportErrors；UI 层 `test_confirmed_platform_exhaustion_uses_atomic_cache_reconciliation` 断言原子校对调用数为 0。
- GREEN：缓存 2 项 `Ran 2 tests in 0.016s`，UI 1 项 `Ran 1 test`；均 exit `0`。
- 修复：单事务刷新已返回集合；只在 `hasMore=False` 且 `stopReason=no_visible_load_more_control` 的确认穷尽状态累计缺失，第二次确认缺失置 `invalid`；空集可校对，超时、交互失败、部分页与未确认穷尽不标记缺失。

### I4 — 可复用容量与生命周期优先级

- RED：`test_capacity_retains_hundred_reusable_when_invalid_and_expired_history_exists` 与 `test_lifecycle_fields_drive_reusable_eviction_priority`，exit `1`，2 ImportErrors（缺少选择生命周期入口）。
- GREEN：`Ran 2 tests in 0.030s`，exit `0`。
- 修复：补齐 `firstSeenAt`、`lastSelectedAt`、`lastPublishSuccessAt`、`lastFailureAt`、`lastErrorCode`，并迁移/回填旧库；容量先保留最多 100 个未过期 reusable，再按发布成功、选择、验证时间排序，invalid/过期历史不挤占可复用容量。

### I5 — 排除集稳定分页

- RED：`test_excluded_identity_pagination_survives_platform_reorder` 因 API 不接受 `excluded_identities` 失败；`test_cache_load_more_excludes_displayed_identities_after_reorder` 在插入/重排后仍只显示 10 条；exit `1`。
- GREEN：上述 2 项 `Ran 2 tests in 0.184s`，exit `0`。
- 修复：新增已展示四字段身份排除集，禁止与非零 offset 混用；UI 以 `cacheHasMore` + 已展示身份请求下一页，平台写入/重排后不重复、不漏项。

### I6 — 首次平台交接单击串行 search + load-more

- RED：`test_twenty_cached_rows_bootstrap_real_session_context_before_load_more` 与 `test_hundred_cached_rows_bootstrap_real_session_context_before_load_more`，exit `1`，2 failures；search 结束后没有在同一点击内接续 load-more。
- GREEN：`Ran 2 tests in 0.158s`，exit `0`。
- 修复：以 token/account/query 所有权登记 handoff，search action key 释放后精确接续一次 load-more；旧的两次点击预期改为同一点击串行契约。

### I7 — 加载控件限定当前地点面板

- RED：`test_load_more_control_is_scoped_to_current_location_panel`、`test_load_more_collapses_nested_nodes_for_one_logical_button`、`test_load_more_rejects_two_distinct_controls_inside_current_panel`，exit `1`；前两项被全页控件干扰，面板内多控件已保持安全停止。
- GREEN：`Ran 3 tests in 0.767s`，exit `0`。
- 修复：先标记当前唯一可见地点 listbox，从最小相关祖先区域找 footer/control；忽略文档其他按钮，折叠同一逻辑按钮的祖先/后代节点，同面板多个独立控件固定失败。

### I8 — 安全读取嵌套虚拟列表 option

- RED：`test_nested_virtual_list_options_survive_window_replacement` 找不到虚拟 listbox；`test_nested_unrelated_listbox_options_are_excluded_from_targets` 误选嵌套无关 option；exit `1`。
- GREEN：`Ran 2 tests in 0.562s`，exit `0`。
- 修复：改读可见后代 option，但每项必须满足 `option.closest('[role=listbox]') === 当前 listbox`；描述、目标匹配和门店应用共用精确所属标记，支持 recycler/window 替换并排除嵌套其他列表。

### I9 — Task 2 墙钟绝对期限

- RED：`test_load_more_timeout_without_outer_deadline_is_one_wall_clock_budget`，exit `1`；后续快照仍被等待 2 次，多个 DOM 动作没有共享一个墙钟预算。
- GREEN：与 `test_publish_load_more_deadline_stops_before_candidate_poll` 组合 `Ran 2 tests in 0.003s`，exit `0`；另因相关组合暴露真 Playwright 等待边界，又与无外部 deadline 的收敛等待联合回归 `Ran 3 tests in 1.329s`，exit `0`。
- 修复：无外部 deadline 时从 `timeout_ms` 创建本地绝对 deadline；每次 evaluate/read/scroll/click/poll 重算余量并有界等待；本地超时固定映射无 cause `publish_location_load_more_failed`，正式外部预算仍保留 `publish_location_load_more_limit`。

### I10 — 发布成功原子 upsert 缺失缓存行

- RED：`test_publish_success_upserts_empty_and_previously_evicted_targets` 因成功入口不接受 query 失败；`test_confirmed_publish_records_successful_location_cache_result` 中 batch 未传递冻结 query；exit `1`。
- GREEN：`Ran 2 tests in 0.023s`，exit `0`。
- 修复：成功回写必须携带冻结 `LocationCacheQuery`，验证账号/范围/返佣兼容性，在一个事务中 upsert 实体和查询关联、写发布成功生命周期并执行容量策略；缓存回写仍是最大努力诊断，不翻转已取得的平台发布结果。

### I11 — shutdown 失效化、取消并排空动态地点任务

- RED：`test_shutdown_cancels_queued_dynamic_location_tasks_by_prefix`、`test_shutdown_running_dynamic_location_timeout_blocks_resource_closes`、`test_shutdown_invalidates_location_owners_and_late_callbacks_cannot_restart_work`，exit `1`，3 failures；排队任务仍可交付，运行超时后继续关资源，token 未立即失效。
- GREEN：排队取消用例单独通过；后两项 `Ran 2 tests in 0.147s`，exit `0`；随后受影响组合覆盖三项。
- 修复：shutdown 首先设置标志、递增地点 token，清空 page/cache/merge owner 与 handoff；通过受控前缀快照取消 queued 任务，用一个共享绝对期限等待 running 任务；未排空时立即返回 `False`，不进入后续资源关闭；所有迟到回调受 shutdown/token/owner 共同门禁。

## 3. Deferred Minor

- 数据库连接与外键：`test_database_connections_enable_foreign_keys_and_cleanup_old_orphans`。RED：连接未开启 FK，旧 orphan 未清理；GREEN：每个连接 `PRAGMA foreign_keys=ON`，迁移在依赖 cascade 前清理 orphan。
- 缓存公开边界：`test_public_cache_boundary_rejects_unknown_enums_and_filter_mismatch`。RED：未知 scope/filter/commission 与筛选冲突可进入存储；GREEN：严格枚举与 candidate/filter 兼容性校验。上述两项组合 RED exit `1`，2 failures；GREEN `Ran 2 tests in 0.006s`，exit `0`。
- 采集诊断白名单：`test_location_pagination_errors_remain_controlled_public_codes` 与 `test_location_pagination_diagnostics_have_controlled_ui_copy`。RED exit `1`，含 4 个子断言失败；GREEN `Ran 2 tests in 0.132s`，exit `0`；`collector_search_context_mismatch` 与 `publish_location_load_more_failed` 已进入序列化白名单及 UI 受控文案。

## 4. 相关组合与 fail-fast 恢复

执行命令：

```text
QT_QPA_PLATFORM=offscreen <venv-python> -m unittest -f \
  test_douyin_location_cache \
  test_douyin_commerce_setup_state \
  test_douyin_commerce_service
```

严格按“遇第一个普通失败即停、只修命名用例、再恢复组合”执行。恢复过程先后暴露并关闭以下旧契约/组合边界：

1. `test_load_more_button_tracks_running_and_platform_progress`：两次点击旧预期改为单击 handoff。
2. `test_load_more_uses_cache_then_platform_once`：offset 假页改为排除集，并改为单击 search→load-more。
3. `test_closed_location_panel_proves_click_without_reopening_search_results`：定位到无外部 deadline 固定等待被误套绝对期限，修正后与 I9 组合回归。
4. `test_hidden_descendant_commission_text_is_excluded_in_both_dom_entries`：可见性夹具补齐四字段身份。
5. `test_load_more_clicks_once_and_returns_only_public_metadata`：按钮放回当前地点面板，并修正轮询等待与局部 deadline 的真实 Playwright 边界。
6. `test_load_more_rejects_multiple_visible_controls`：两个独立控件放入同一当前面板，保持安全拒绝。
7. `test_same_commission_duplicate_options_survive_dom_then_stop_after_filter`：断言改为“重复完整身份”固定语义。
8. `test_visibility_override_and_hidden_portal_obey_effective_visibility`：可见性契约传入完整规范候选。
9. `test_duplicate_dom_targets_stop_before_store_click` 与 `test_store_dom_scripts_keep_newline_regex_escaped`：纯单元 listbox 假对象补齐所属标记 `evaluate`。
10. `test_same_location_commission_variants_are_filtered_before_ambiguity`：`all` 正确保留同 POI 的两种佣型，筛选后各自唯一。
11. `test_apply_saved_location_updates_session_only_after_atomic_readback` 与 `test_location_selection_does_not_bind_store_without_explicit_store_step`：模拟回读补齐 POI/佣型，会话公开地点仍仅存最小字段。
12. `test_abandon_session_resets_all_platform_settings_but_keeps_content`：复位契约补齐 `cacheHasMore=False`。

最终受影响组合：`Ran 525 tests in 28.338s`，`OK`，`real 28.57s`，exit `0`。

## 5. 唯一一次新最终全量

执行命令：

```text
QT_QPA_PLATFORM=offscreen <venv-python> -m unittest discover -f
```

结果：`Ran 1074 tests in 42.047s`，`OK`，`real 42.43s`，`user 29.40s`，`sys 6.26s`，exit `0`。

这是 11 项 Important 、Deferred Minor 及 fail-fast 恢复全部结束后，对当前实现树执行的唯一一次新全量。旧 `1048` 证据已明确降为历史。

## 6. 静态、敏感与占位扫描

- `py_compile`：已编译 8 个受影响产品模块和 3 个受影响测试模块，exit `0`。
- `git diff --check`：无输出，exit `0`。
- 占位扫描：新增行中 `TODO|FIXME|XXX|NotImplementedError|pass` 无命中。
- 敏感扫描：受影响产品/测试文件中长 `sk-`、AWS access key、private-key header、长 Bearer token 模式无命中。
- 不含真实 Cookie、密码、API Key、验证码、二维码链接或客户/订单数据。

## 7. 自审

- 范围：只修改 service/cache/session/collector/UI/batch/database 中闭合 11 项 finding 和指定 Deferred Minor 所需代码与契约，未做范围外重构。
- 身份：四字段身份跨 DOM、会话、UI、缓存一致；无真实 ID 时的同完整身份歧义安全停止。
- 上限：派发前和回包后双重检查，但每个点击只计一次；平台返回的 raw count 不代替有效唯一身份计数。
- 校对：仅在明确穷尽后原子标记 missing，错误/部分结果不产生假失效。
- 分页：排除集基于已展示四字段身份，平台合并重排不改变下一页的未见集。
- 发布证据：缓存 upsert 失败只留固定诊断，不将已取得的平台回执降级为失败。
- 退出：动态任务未排空时在资源关闭前安全停止；迟到回调无法重启工作。
- 工作树：实现提交仅含 11 个受影响产品/测试文件，无其他用户修改被覆盖或重置。

## 8. 尚未验证与安全停点

- 未验证真实抖音 DOM 当日结构、真实账号下的按钮区域、真实平台分页/虚拟列表行为与公开回读；这是本轮禁止真实平台动作的预期边界，不将离线通过冒充运行闭环或结果闭环。
- 未解决 Important / Deferred Minor：无。
- 安全停点：分支与 worktree 保留，未合并、未推送、未清理。

## 9. 唯一下一步

- 执行者：父任务/集成者。
- 前置：核对本报告、实现 SHA 与唯一新全量证据。
- 动作：按主任务决定保留或集成 `feature/douyin-location-pagination-cache`，不重跑本轮全量。
- 完成证据：集成侧记录已采用的提交 SHA，且本 worktree 不产生未预期改动。
