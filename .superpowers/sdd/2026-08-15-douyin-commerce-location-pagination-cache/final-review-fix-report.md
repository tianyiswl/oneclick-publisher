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

## 10. Final review remediation cycle 2（2026-08-16）

### 10.1 权威、边界与结论

- 本轮唯一精确需求：`final-review-remediation-2.md`；起始 HEAD：`f46a4396f4494cedb1390382134e0efde3ee10b5`。
- 实现提交 SHA：`f64244c712229562d97a0208c107a5270e230188`（`f64244c 修复抖音地点缓存复审剩余问题`）。
- 结论：R1–R5 均已在当前代码验证为真实可达，完成确定性 RED→GREEN；最终独立只读复审为 `PASS`，未发现 Critical、Important 或 Minor load-bearing 问题。
- 闭环层级：交付闭环。未登录真实账号，未连接真实浏览器会话，未读写真实平台，未执行上传或发布；Playwright 仅运行本地 headless HTML/DOM 契约用例。
- 证据降级：本文第 5 节的 `1074 tests / 42.047s` 及本轮中途的 `646 tests`、`1082 tests` 均只作历史证据；因其后产品码与测试继续变更，不代表最终树。本轮唯一最终证据为下文 `649` 相关组合与 `1085` 全量。

### 10.2 R1——真实发布 preset 链保留返佣身份

- 独立验证：审查结论成立。UI 正式快照只有 `observedCommissionType`，batch payload 进入 session 后的归一化只识别 `commissionType/commerceInfo`，`commission` 可被改写为 `no_commission`。
- RED：`test_real_batch_preset_preserves_observed_commission_type_through_session_apply`；`commission` 子用例的真实 session apply 观测到 `no_commission`，exit `1`。
- GREEN：同一命名用例同时覆盖 `commission/no_commission`，并在 R1–R5 命名组合 `Ran 11 tests in 1.175s`中通过。
- 修复：batch 受控 preset 边界只归一化一次观测返佣类型，并同时写入 `observedCommissionType` 和严格四字段匹配所需的权威 `commissionType`；非法/缺失值仍按现有兼容规则安全失败。

### 10.3 R2——首次设置搜索的 100 个四字段身份硬门

- 独立验证：审查结论成立。session 与 UI 首屏回包可直接保存 101 个唯一身份，且真实 cache-first 路径会把缓存与 101 个平台候选合并成 100 以上。
- RED：`test_initial_setup_search_caps_first_hundred_unique_identities` 观测长度 `101 != 100`；`test_initial_platform_search_caps_ui_and_cache_merge_before_autofill` 先以真实缓存回调放入 10 条不重复候选，再回传平台 101 条，观测 UI 状态 `110 != 100`，exit `1`。
- GREEN：上述 session/UI 两项在 R1–R5 命名组合中通过；第 101 个身份未进入 session、UI state、dropdown、auto-fill 或 cache merge payload。
- 修复：首屏回包先按 `(poiId,name,address,commissionType)` 稳定去重，保留平台顺序前 100；UI 与已显示缓存合并后再截断总量，在 auto-fill/cache merge 前就设置 `hasMore=False`、`candidate_identity_limit` 和受控上限文案。

### 10.4 R3——从真实 cache-search 路径可达的无感校对

- 独立验证：审查结论成立。缓存回调的 `requiresRevalidation=True` 会被首次平台搜索成功无条件清除，因此真实路径无法进入 reconciliation。
- RED：`test_cache_search_silently_pages_to_exhaustion_and_second_miss_invalidates`；从真实 cache-search 回调进入后标志变为 `False`，且没有派发后续无感 load-more。
- GREEN：上述真实链用例与 `test_cache_search_interaction_failure_never_marks_missing` 在 R1–R5 命名组合中通过。
- 修复：初始搜索保留待校对标志，在有界静默分页期间不清除；只有 `hasMore=False` 且 `stopReason=no_visible_load_more_control` 的确认穷尽会调用完整已见集合的原子校对。空集、部分页、超时或交互失败都不标记 missing。

### 10.5 R4——真实 UI 选择入口的最近选择生命周期

- 独立验证：审查结论成立。真实 `_select_batch_location_candidate` 仅保存 preset，未调用 `record_location_selection`，`lastSelectedAt` 在产品路径始终为空。
- 首次 RED：`test_real_ui_selection_records_frozen_cache_identity_and_survives_eviction`中受控 runner 未收到 selection 任务。
- 并发 RED：将同一 query 的 capacity merge 交错在迟到 selection 之前，已选行先被淘汰，旧的纯 `UPDATE` 命中 0 行，最终断言 `poi-cache-099` 不在 100 条保留集中。
- 生命周期 RED：第一版缺行 UPSERT 把 selection 时间伪造为 `verifiedAt`，`test_late_selection_never_revives_invalid_evicted_row` 观测 `reusable != invalid`；改为永久保留 orphan 后，`test_repeated_capacity_replacement_cleans_orphan_entities` 观测实体数 `300 != 100`；容量淘汰后先收到地点发布失败时，`test_publish_failure_after_eviction_blocks_late_selection_restore` 观测迟到 selection 错误恢复了 target。三项均 exit `1`。
- GREEN：最终 UI 竞态用例 `Ran 1 test in 0.148s`，且断言恢复行的 `verifiedAt` 与原值完全相同；发布失败、invalid 不复活、orphan 有界和生命周期优先级 4 项 `Ran 4 tests in 0.059s`；随后的 649 项相关组合全部覆盖，均 exit `0`。
- 修复：成功保存 UI 地点后，用唯一动态 key 异步/最大努力记录冻结 account/query/四字段身份；失败只写固定日志，不撤销 UI 选择，不交付迟到 UI 回调。
- 竞态收口：保留现有原子 orphan 清理；容量淘汰只把原始 DB lifecycle 放入进程内受控 tombstone（TTL 5 分钟、全局最多 1000 条、同 DB/account/scope/四字段隔离、锁保护）。迟到 selection 只能恢复未过期的原 `reusable` 状态，只更新 `lastSelectedAt`，不修改 `status/verifiedAt`；invalid/expired 不恢复，且更新的受控地点失败会作废同身份 tombstone。

### 10.6 R5——加载更多控件只属于当前地点面板

- 独立验证：审查结论成立。旧实现从 listbox 不断上爬，当地点面板无按钮而共享编辑器祖先有无关“加载更多”时会误点击。
- RED：`test_load_more_does_not_escape_panel_to_shared_editor_button`；共享祖先分别使用通用 `section` 与 `role=dialog` 时，`_unique_visible_load_more_control` 都返回了无关 locator，exit `1`。
- GREEN：共享 editor 负例、当前面板正例、父子节点合并为一个逻辑按钮、同面板两个独立按钮 fail-closed，最终 `Ran 4 tests in 0.974s`，exit `0`。
- 修复：优先使用产品受控 `[data-oneclick-commerce-location-panel]` 与明确 location/poi 边界；移除通用 `dialog/section/aside` owner 猜测；只在最小当前地点区域内查找，折叠祖先/后代逻辑按钮，歧义保持安全拒绝。

### 10.7 Fail-fast 恢复、最终相关组合与唯一全量

- 中途首次相关组合暴露 exit `139`；系统调试确认是旧 UI 选择测试启动真后台 worker，Qt page teardown 时任务仍存活。只将该用例改为受控 runner，两用例最小组合转绿，未改产品语义。
- 恢复组合依次在首个普通失败停止，暴露 6 个与本轮四字段入口不相干但缺少 `commissionType` 的旧 session 模拟候选。只为这 6 个命名夹具补齐固定佣型：`test_location_search_can_start_before_music_selection`、`test_location_search_never_refreshes_music`、`test_location_search_remains_available_after_music_refresh_closes_picker`、`test_selected_music_keeps_same_session_available_for_location_search`、`test_music_replace_after_refresh_leaves_no_picker_before_location_search`、`test_transient_missing_scope_panel_waits_then_retries_without_music`。
- 上述恢复后曾得到 `646` 相关与 `1082` 全量；其后 R2/R4/R5 按真实 cache-first、并发 lifecycle 与 `role=dialog` 复审继续修正，故该两组数字已明确降为历史。
- 最终受影响组合：

```text
QT_QPA_PLATFORM=offscreen <venv-python> -m unittest -f \
  test_douyin_location_cache \
  test_douyin_commerce_batch_service \
  test_douyin_commerce_collectors \
  test_douyin_commerce_setup_state \
  test_douyin_commerce_service
```

结果：`Ran 649 tests in 29.609s`，`OK`，`real 29.82s`，`user 23.40s`，`sys 4.71s`，exit `0`。

- 唯一一次新最终全量：

```text
QT_QPA_PLATFORM=offscreen <venv-python> -m unittest discover -f
```

结果：`Ran 1085 tests in 41.641s`，`OK`，`real 41.94s`，`user 29.29s`，`sys 6.17s`，exit `0`。全量之后未再修改产品代码或测试。

### 10.8 静态、敏感与占位扫描

- `py_compile`：编译 5 个受影响产品文件与 2 个测试文件，exit `0`。
- `git diff --check`：无输出，exit `0`。
- 占位扫描：新增行中 `TODO|FIXME|XXX|NotImplementedError|raise NotImplemented|pass` 无命中，`rg` exit `1`。
- 敏感扫描：新增行中长 `sk-`、AWS access key、private-key header、长 Bearer token 模式无命中，`rg` exit `1`。
- 未新增真实 Cookie、密码、API Key、验证码、二维码链接、账号凭据、客户/订单数据或真实平台 DOM 快照。

### 10.9 自审、独立复审与安全停点

- 范围：实现提交只修改 7 个产品/测试文件，全部直接用于 R1–R5 或其确定性并发回归；无范围外重构，未覆盖/重置用户改动。
- 身份与上限：四字段身份在真实 preset→batch→session 链上一致；首屏平台与 UI 合并都无法越过 100。
- 校对与生命周期：只有确认穷尽会标记 missing；selection 不伪造平台校对时间，invalid/expired/更新失败均不能被迟到 selection 复活；DB 实体与进程内 tombstone 均有界。
- DOM：加载更多控件不再以通用 editor/dialog/section/aside 猜测 owner；无唯一逻辑按钮时安全停止。
- 独立复审：最终只读 diff review 对 R1/R3 无新阻塞，确认 R2 cache-first 总量与 R5 `role=dialog` 负例已闭合；在 R4 连续提出并验证了 merge/selection、validity、orphan 无界及 failure/selection 四个竞态后，最终明确 `PASS`，未发现任何 load-bearing 问题。
- 尚未验证：真实抖音当日 DOM、overlay/footer 层级、真实 POI 属性、虚拟列表回收、平台分页节奏、真实账号与最终平台回读。这是本轮明确禁止真实平台动作的预期边界，不将离线通过冒充运行或结果闭环。
- 未解决 Critical / Important / Deferred Minor：无。
- 安全停点：分支与 worktree 保留，未合并、未推送、未清理。

## 11. Final remediation cycle 3（2026-08-16）

### 11.1 权威、范围与结论

- 本轮唯一精确需求：`final-review-remediation-3.md`；起始 HEAD：`82301cd112d869f98772ec8956a52f49c6d04cb1`。
- 实现提交 SHA：`7b888002c14cc2c76346a861bd1430b8bb8a5332`（`7b88800 修复抖音地点缓存第三轮终审问题`）。
- 范围：只处理 R3-1、R3-2、R3-3 三项剩余 Important；产品代码只修改 UI 的组合容量/选择生命周期边界和 service 的加载更多控件归属证明，测试修改只用于三项回归及受影响旧夹具恢复，无范围外重构。
- 结论：三项均先验证为真实可达，完成确定性 RED→最小 GREEN；本轮分配的剩余 Important 为 0。
- 闭环层级：交付闭环。没有登录真实账号、连接真实浏览器会话、上传、提交或发布；所有平台行为均由离线替身或本地 DOM 契约模拟。
- 证据降级：cycle 2 的 `1085 tests / 41.641s` 及更早全量均标为历史，不代表本轮最终实现树；当前证据为下文 `652` 项相关组合与 `1088` 项全量。

### 11.2 R3-1——缓存与平台组合身份总量不超过 100

- 独立验证：旧实现仅按平台候选计数；缓存先进入状态后，平台候选仍可把组合状态、缓存合并和自动填充带到 100 个身份之外。
- RED：`test_combined_cache_and_platform_limit_rejects_late_next_page_candidate`；10 条缓存加 91 条首屏平台候选后仍保留 `hasMore=True`，下一页身份存在被接受路径；`Ran 1 test`，exit `1`。
- GREEN：同一命名用例 `Ran 1 test in 0.145s`，`OK`，exit `0`。
- 修复：所有 cache/platform 候选在状态写入、列表投影、cache merge payload 与 auto-fill 前共用四字段稳定去重和 100 条截断；只允许进入组合集合的平台身份继续流转，组合剩余容量为 0 时关闭 `hasMore/cacheHasMore`。

### 11.3 R3-2——自动填充与手动选择共用生命周期成功边界

- 独立验证：手动选择保存后会登记 `lastSelectedAt`，自动填充只保存 preset，容量压力下无法获得相同的生命周期优先级。
- RED：`test_auto_fill_records_selection_once_and_retains_candidate_by_lifecycle`；真实自动填充路径收到 0 个 selection task，期望 1 个；`Ran 1 test in 0.138s`，exit `1`。
- GREEN：同一命名用例 `Ran 1 test in 0.154s`，`OK`，exit `0`；同时验证容量压力后自动填充身份被保留，手动入口只登记一次。
- 修复：新增唯一 `_bind_batch_location_candidate` 成功边界；preset 保存成功后统一异步、最大努力记录冻结的 account/query/四字段身份，手动与自动入口均只调用一次，记录失败不撤销已保存 preset。

### 11.4 R3-3——无法证明地点面板时拒绝加载更多控件

- 独立验证：listbox 的通用直接父级仍可被当作地点面板；共享 dialog/editor 内的无关“加载更多”存在误点路径。
- RED：`test_load_more_rejects_generic_dialog_direct_parent_fallback`；通用 dialog 直接父级内的无关控件被返回，exit `1`。
- GREEN：上述负例与 `test_load_more_control_is_scoped_to_current_location_panel`、`test_load_more_rejects_two_distinct_controls_inside_current_panel` 组合 `Ran 3 tests in 0.739s`，`OK`，exit `0`。
- 修复：移除 `listbox.parentElement` 通用兜底；只接受显式 location/poi 面板，或当前唯一受控地点搜索输入/门店锚点与 listbox 的最小共同祖先。没有归属证明返回无控件；已证明区域内存在两个独立控件仍 fail-closed。

### 11.5 Fail-fast 恢复与最终相关组合

- 严格在相关组合首个普通失败停止并只修单项：兼容旧夹具中缺失的 `commissionType` 为受控 `unknown`；将三项会触发真实后台 selection worker 的旧 UI 测试替换为受控 lifecycle runner；把“100 条缓存后仍可平台续页”和“10 缓存 + 100 平台均被接受”的旧断言更新为组合硬上限契约。
- 最终受影响组合：

```text
QT_QPA_PLATFORM=offscreen <venv-python> -m unittest -f \
  test_douyin_location_cache \
  test_douyin_commerce_batch_service \
  test_douyin_commerce_collectors \
  test_douyin_commerce_setup_state \
  test_douyin_commerce_service
```

- 结果：`Ran 652 tests in 29.881s`，`OK`，`real 30.10s`，`user 23.55s`，`sys 4.89s`，exit `0`。

### 11.6 最终新全量

```text
QT_QPA_PLATFORM=offscreen <venv-python> -m unittest discover -f -q
```

- 最终可验收结果：`Ran 1088 tests in 41.703s`，`OK`，`real 41.98s`，`user 29.26s`，`sys 6.28s`，exit `0`。此后未再修改产品代码或测试。

### 11.7 静态、敏感与占位扫描

- `py_compile`：编译 2 个受影响产品文件与 1 个测试文件，exit `0`。
- `git diff --check` / 暂存差异检查：无输出，exit `0`。
- 占位扫描：新增行中 `TODO|FIXME|XXX|NotImplementedError|raise NotImplemented|pass` 无命中，`rg` exit `1`。
- 敏感扫描：新增行中长 `sk-`、AWS access key、private-key header、长 Bearer token 模式无命中，`rg` exit `1`。
- 未新增真实 Cookie、密码、API Key、验证码、二维码链接、账号凭据、客户/订单数据或真实平台 DOM 快照。

### 11.8 自审、尚未验证与安全停点

- 组合身份：cache/platform 共用同一四字段集合，无法从 state、render、cache merge 或 auto-fill 绕过 100 条上限。
- 生命周期：自动与手动保存共用一次成功边界；异步记录保持 token/account/query/candidate 冻结，失败不回滚 preset。
- DOM：通用父级不再构成归属证明；显式地点面板仍工作，歧义控件继续安全停止。
- 尚未验证：真实抖音当日 DOM、overlay/footer 层级、真实 POI 属性、真实平台分页与账号回读。这是禁止真实平台动作的预期边界，不将离线证据冒充运行闭环或结果闭环。
- 本轮分配的未解决 Important：无。
- 安全停点：分支与 worktree 保留，未合并、未推送、未清理。

### 11.9 唯一下一步

- 执行者：父任务/集成者。
- 前置：核对实现提交、报告提交及本节 652/1088 测试证据。
- 动作：按主任务的独立复审结论决定是否集成 `feature/douyin-location-pagination-cache`。
- 完成证据：集成侧记录采用的提交 SHA；若继续审查，仅新增明确 finding，不把旧 `1085` 当作当前证据。

## 12. Final remediation cycle 4（2026-08-16）

### 12.1 权威、范围与结论

- 本轮唯一精确需求：`final-review-remediation-4.md`；起始 HEAD：`754e56427bfefb9e59eadb5b4f67f8dd1eb4f4ce`。
- 实现提交 SHA：`fdc5407f13d7641094155b7a18cfc81600c6f1f0`（`fdc5407 修复抖音地点缓存第四轮终审问题`）。
- 范围：只处理 R4-1、R4-2 两项；产品代码只修改 UI 已观察/已接纳身份边界与 service 的加载更多面板归属判定，测试只增加两项指定回归及两条旧清空/复位精确契约字段，无范围外重构。
- 结论：两项均完成独立 RED→最小 GREEN，本轮分配的未解决项为 0。
- 闭环层级：交付闭环。没有登录真实账号、连接真实浏览器会话、上传、提交或发布；DOM 测试仅使用本地 headless HTML。
- 证据降级：cycle 3 的 `1088 tests / 41.703s` 及更早全量均只作历史，不代表当前树；本轮当前证据为下文 `654` 项相关组合与 `1090` 项全量。

### 12.2 R4-1——已观察身份与已接纳/展示身份分离

- RED：`test_revalidation_reconciles_observed_identity_beyond_display_cap`；10 条可复用缓存 + 首轮 80 条平台身份 + 累计 91 条分页身份时，展示集正确截断为 100，但 reconcile 只收到 90 条平台身份，末位 T 被排除；`Ran 1 test in 0.150s`，exit `1`。
- GREEN：同一命名用例 `Ran 1 test in 0.150s`，`OK`，exit `0`。
- 修复：独立维护完整四字段 `observedPlatformCandidates`；展示、自动填充和普通 cache merge 仍共用 100 条硬上限。只有 `hasMore=False` 且 `stopReason=no_visible_load_more_control` 的确认穷尽校对使用完整已观察集；T 未进入展示集，但被 reconcile 为 `reusable / revalidationFailures=0`。未证明完整穷尽时仍保留 `requiresRevalidation`。

### 12.3 R4-2——受控输入不能证明通用共享对话框

- RED：`test_controlled_input_cannot_claim_generic_shared_dialog`；输入同时带两个受控标记时，旧实现把其与 listbox 的共享 `role=dialog` 当成地点面板，进入无关按钮点击路径并报 `publish_location_load_more_failed`；`Ran 1 test in 5.288s`，exit `1`。
- GREEN：同一命名用例 `Ran 1 test in 0.275s`，`OK`，exit `0`；结果 `hasMore=False`、`stopReason=no_visible_load_more_control`，无关按钮点击数为 0。
- 修复：受控锚点的最小共同祖先若本身是通用 dialog/editor 所有者则 fail closed；显式 location/poi panel 仍优先，更小的受控有界区域仍可用，面板内两个独立控件仍保持歧义停止。

### 12.4 Fail-fast 恢复与最终相关组合

- 首次相关组合使用 `-f` 在第一个普通失败停下：`test_abandon_session_resets_all_platform_settings_but_keeps_content`，旧精确状态断言缺少新内部已观察集的空值。只补该复位契约字段后，命名用例 `Ran 1 test in 0.131s`，`OK`。
- 最终相关组合：`test_douyin_location_cache` + `test_douyin_commerce_batch_service` + `test_douyin_commerce_collectors` + `test_douyin_commerce_setup_state` + `test_douyin_commerce_service`。
- 结果：`Ran 654 tests in 30.041s`，`OK`，`real 30.26s`，`user 23.97s`，`sys 4.87s`，exit `0`。

### 12.5 唯一一次新最终全量

```text
QT_QPA_PLATFORM=offscreen <venv-python> -m unittest discover -f -q
```

- 结果：`Ran 1090 tests in 42.019s`，`OK`，`real 42.33s`，`user 29.67s`，`sys 6.39s`，exit `0`。
- 该次全量之后没有再修改产品代码或测试；仅追加本证据报告。旧 `1088` 已明确降为历史。

### 12.6 静态、敏感与差异检查

- `py_compile`：编译 2 个受影响产品文件与 1 个测试文件，exit `0`。
- `git diff --check`：无输出，exit `0`。
- 占位扫描：新增行中 `TODO|FIXME|XXX|NotImplementedError|raise NotImplemented|pass` 无命中。
- 敏感扫描：新增行中长 `sk-`、AWS access key、private-key header、长 Bearer token 模式无命中。
- 未新增真实 Cookie、密码、API Key、验证码、二维码链接、账号凭据、客户/订单数据或真实平台 DOM 快照。

### 12.7 尚未验证与安全停点

- 尚未验证：真实抖音当日 DOM、真实账号平台分页、真实 POI 四字段与平台读回。这是本轮禁止真实平台动作的预期边界，不将离线证据冒充运行闭环或结果闭环。
- 安全停点：分支与 worktree 保留，未合并、未推送、未清理。

### 12.8 唯一下一步

- 执行者：父任务/集成者。
- 前置：核对实现提交、报告提交及本节 `654/1090` 测试证据。
- 动作：按主任务的独立复审结论决定是否集成 `feature/douyin-location-pagination-cache`。
- 完成证据：集成侧记录采用的提交 SHA；若继续审查，仅新增明确 finding，不把旧 `1088` 当作当前证据。

## 13. Final remediation cycle 5（2026-08-16）

### 13.1 权威、范围与结论

- 本轮唯一精确需求：`final-review-remediation-5.md`；起始 HEAD：`95860052165a1ce58fc0ac924804303a0bf4c712`。
- 实现提交 SHA：`fcc4809d97fb2e5f0e7d1e28a547760320917ad0`（`fcc4809 修复通用对话框地点控件归属边界`）。
- 范围：只修复通用 dialog/editor 内普通 wrapper 的地点控件归属误判；产品代码只收紧一个 DOM 边界条件，测试只增加指定负例，无范围外重构。
- 结论：指定 Important 已完成确定性 RED→最小 GREEN；显式 location/poi panel 正例保留，同面板多个独立控件仍 fail-closed。
- 闭环层级：交付闭环。没有登录真实账号、连接真实浏览器会话、上传、提交或发布；DOM 测试仅使用本地 headless HTML。
- 证据降级：cycle 4 的 `1090 tests / 42.019s` 及更早全量均只作历史，不代表当前树。

### 13.2 通用 dialog 内普通 wrapper 无地点所有权

- RED：`test_generic_dialog_content_wrapper_has_no_location_ownership`；夹具为 `role=dialog > ordinary dialog-content wrapper > 双标记输入 + location listbox + 无关加载更多按钮`。旧实现进入无关按钮点击路径并报 `publish_location_load_more_failed`；`Ran 1 test in 5.569s`，exit `1`。
- GREEN：同一命名用例 `Ran 1 test in 0.278s`，`OK`，exit `0`；返回 `hasMore=False` / `stopReason=no_visible_load_more_control`，无关按钮点击数为 `0`。
- 最小修复：受控锚点与 listbox 的最小共同祖先只要位于通用 `dialog/editor` owner 内，就不能证明地点所有权。显式 `data-oneclick-commerce-location-panel` 及 location/poi 面板标记仍优先有效。

### 13.3 一次相关 DOM 组合

- 解释器：`/Users/andy/Documents/Codex/2026-07-28/new-chat/outputs/一键发桌面UI基座/.venv/bin/python`。
- 用例：cycle5 新负例、显式地点面板正例、通用 dialog 直接父级负例、共享 dialog 双标记负例、同面板多独立控件拒绝。
- 结果：`Ran 5 tests in 1.304s`，`OK`，exit `0`。

### 13.4 唯一一次新最终全量

```text
QT_QPA_PLATFORM=offscreen /Users/andy/Documents/Codex/2026-07-28/new-chat/outputs/一键发桌面UI基座/.venv/bin/python -m unittest discover -f -q
```

- 结果：`Ran 1091 tests in 42.623s`，`OK`，`real 42.95s`，`user 29.93s`，`sys 6.59s`，exit `0`。
- 全量之后没有再修改产品代码或测试；只追加本证据报告。

### 13.5 静态、敏感与差异检查

- `py_compile`：编译 1 个受影响产品文件与 1 个测试文件，exit `0`。
- `git diff --check`：无输出，exit `0`。
- 占位扫描：受影响产品/测试新增行中 `TODO|FIXME|XXX|NotImplementedError|raise NotImplemented|pass` 无命中，`rg` exit `1`。
- 敏感扫描：受影响产品/测试新增行中长 `sk-`、AWS access key、private-key header、长 Bearer token 模式无命中，`rg` exit `1`。
- 未新增真实 Cookie、密码、API Key、验证码、二维码链接、账号凭据、客户/订单数据或真实平台 DOM 快照。

### 13.6 尚未验证与安全停点

- 尚未验证：真实抖音当日 DOM、overlay/footer 层级、真实账号平台分页与最终读回。这是本轮禁止真实平台动作的预期边界，不将离线证据冒充运行或结果闭环。
- 本轮分配的未解决 Important：无。
- 安全停点：分支与 worktree 保留，未合并、未推送、未清理。

### 13.7 唯一下一步

- 执行者：父任务/集成者。
- 前置：核对实现提交、报告提交及本节 `5/1091` 测试证据。
- 动作：按主任务的独立复审结论决定是否集成 `feature/douyin-location-pagination-cache`。
- 完成证据：集成侧记录采用的提交 SHA；若继续审查，只新增明确 finding，不把旧 `1090` 当作当前证据。
