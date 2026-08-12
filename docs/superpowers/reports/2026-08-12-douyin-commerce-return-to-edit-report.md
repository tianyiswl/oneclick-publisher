# 抖音带货未完成视频返回修改验收报告

验收日期：2026-08-12

验收分支：`codex/douyin-commerce-return-to-edit`

设计／计划基线：`db20b66c8453df63b4394516f0c36c9574107440`

Task 1–5 验收节点：`33cd9185f8ae86b7e947111bba1262c20dac0a15`

## 结论

Task 1–5 的首轮实现及 Task 6 各轮旧结果 `177/947`、`181/951`、`198/962`、`201/965`、`203/967`、`262/969`、`263/970` 均已因后续生产代码修改降级为历史证据，不能支持最终结论。Fix round 2 修复两个 Important：Qt `refresh()` 重排素材后按旧数值位置选入成功媒体并使来源绑定漂移；双空格真实路径在草稿恢复后又被 UI、批量契约或单视频 `fileList` 校验折叠。最终静态路径审计再封住上传身份仍以通用文本规则折叠 `fileList` 的遗漏。最终相关组合 `366/366 OK`，最终完整回归 `971/971 OK`，退出码均为 `0`。变更 Python 文件 `py_compile`、`git diff --check`、敏感／占位扫描、变更路径扫描、通用文本规范化路径残留扫描以及返回修改默认零平台动作 AST 检查均通过。

本结论只证明本地 Python 逻辑、临时 SQLite、离线替身和 Qt offscreen 界面契约。验收期间没有启动真实客户端或浏览器，没有读取真实账号，没有上传、预检、保存平台草稿、提交或发布。真实抖音 DOM、账号状态和平台回执仍为**未验证**，不能表述为真实平台通过。

## Task 1–5 提交

| Task | 提交 |
| --- | --- |
| Task 1 | `d66c2c9c467c616725dc49aebbafe8935f433142` 保留抖音批量视频媒体身份；`f56ff19e23b6ba0bf7805c486887d02525388279` 保留抖音草稿媒体身份 |
| Task 2 | `f2ad359690c86045f1e7b3542d54e9123ef9ead6` 增加抖音失败批次修改快照；`b403ef38a25417eb33bf20cbd6eef6e057940bb8` 拒绝无稳定媒体身份的修订 |
| Task 3 | `0e8327ffa6aa6ab3eb74478fad055d405dc4c12b` 恢复抖音未完成视频到可编辑批次；`689681e9584f789ab6a99bfda44e458088ea0a81` 收紧抖音修订视频选择边界；`b66227e5c9eae06f28266f94f9d53095727798e0` 保持普通抖音视频单条选择 |
| Task 4 | `cd4291b9a3b51cbe02c634f8c2ab41c0ae51f859` 增加抖音未完成视频返回修改入口；`3ab9826a6369135a1f6a3ff294a08e4ef20f3270` 修复返回修改关闭与多代锁定边界 |
| Task 5 | `33cd9185f8ae86b7e947111bba1262c20dac0a15` 展示抖音修改批次来源 |

## 历史定向 RED／GREEN 证据

以下是 Task 1–5 各报告记录的 TDD 证据，不冒充本轮新鲜执行。命令中的解释器统一为：

```bash
QT_QPA_PLATFORM=offscreen /Users/andy/Documents/Codex/2026-07-28/new-chat/outputs/一键发桌面UI基座/.venv/bin/python -m unittest -v <测试名>
```

### Task 1

| RED 原因 | 对应 GREEN 测试名 |
| --- | --- |
| 批次规范化与单条发布载荷缺少 `mediaId`，断言触发 `KeyError` | `test_douyin_commerce_batch_service.DouyinCommerceBatchServiceTests.test_batch_items_preserve_builtin_positive_media_identity`；并联 `test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_collect_batch_payload_generates_explicit_item_timer_fields_before_ui_task_creation` |
| 草稿规范化／SQLite 往返丢失 `mediaId`，断言触发 `KeyError` | `test_douyin_commerce_batch_draft_service.DouyinCommerceBatchDraftTests.test_draft_roundtrip_preserves_only_positive_builtin_media_identity`；并联 `test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_batch_draft_payload_includes_stable_platform_intent` 与 `test_douyin_commerce_batch_draft_service.DouyinCommerceBatchDraftTests.test_unknown_and_sensitive_item_fields_are_not_persisted` |

### Task 2

| RED 原因 | 对应 GREEN 测试名 |
| --- | --- |
| 修订规划接口不存在，触发 `AttributeError` | `test_task_service.DouyinCommerceBatchTaskTests.test_prepare_revision_keeps_only_failed_and_pending_items` |
| 非人工暂停被错误放行 | `test_task_service.DouyinCommerceBatchTaskTests.test_prepare_revision_rejects_ambiguous_and_nonmanual_pauses` |
| 新任务创建接口不接收 `revision_source_task_id`，触发 `TypeError` | `test_task_service.DouyinCommerceBatchTaskTests.test_revision_child_records_source_without_mutating_it` |
| 成功项没有稳定媒体键仍可修订 | `test_task_service.DouyinCommerceBatchTaskTests.test_prepare_revision_rejects_success_item_without_stable_media_key` |
| 路径解析异常越过公开脱敏边界 | `test_task_service.DouyinCommerceBatchTaskTests.test_prepare_revision_maps_media_key_error_to_fixed_rejection` |

### Task 3

| RED 原因 | 对应 GREEN 测试名 |
| --- | --- |
| 缺少修订计划载入器，未恢复未完成视频及可编辑字段 | `test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_revision_plan_restores_only_unfinished_items_and_all_editable_fields` |
| 程序化选择可重新选入成功视频 | `test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_revision_blocks_successful_video_from_checkbox_and_programmatic_selection` |
| 列表复选框可保留成功视频勾选 | `test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_revision_unchecks_successful_video_selected_directly_in_list` |
| 新批次、放弃或完成后修订状态未清理 | `test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_revision_state_clears_only_at_new_abandoned_or_completed_batch_boundaries` |
| 采集启动失败误清可恢复修订状态 | `test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_revision_state_survives_setup_operation_failure` |
| 媒体键异常原文逃出且未失败关闭 | `test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_revision_media_key_error_stays_fixed_and_fail_closed_in_ui` |
| “选择视频”菜单可绕过成功媒体禁选 | `test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_revision_video_picker_cannot_select_successful_media_for_upload` |
| 路径普通文本规范化会折叠合法的连续空格 | `test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_revision_path_media_key_preserves_legal_internal_double_spaces` |
| 修订素材无法映射时旧选择被提前破坏 | `test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_revision_restore_prevalidates_atomically_and_can_retry` |
| 普通单视频菜单被误切换为批量选择 | `test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_normal_video_picker_keeps_single_video_out_of_batch_selection` |

### Task 4

| RED 原因 | 对应 GREEN 测试名 |
| --- | --- |
| 可修订结果仍显示“开始新内容”，不可修订结果缺少反向约束 | `test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_review_button_offers_revision_after_failed_or_manual_paused_batch`；`test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_review_button_never_offers_revision_after_all_success_or_ambiguous_receipt` |
| worker 完成后未保留结果任务 ID／未重新投影资格 | `test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_batch_finish_retains_result_task_and_projects_revision_after_cleanup` |
| 点击返回未重读任务或未等待严格关闭；关闭不完整仍可能跳页 | `test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_return_to_edit_rechecks_task_and_waits_for_zero_alive_barrier`；`test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_return_to_edit_keeps_result_page_when_cleanup_is_incomplete` |
| `False`、`0.0` 或字符串零可绕过计数屏障 | `test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_return_to_edit_rejects_non_builtin_zero_close_counts` |
| 旧批量 worker 未结束就关闭采集资源 | `test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_revision_close_waits_for_racing_batch_worker_before_collectors` |
| 双击会重复读取来源并排队第二个关闭代际 | `test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_return_to_edit_double_click_enqueues_only_one_close_generation` |
| 客户端退出未取消排队的返回任务 | `test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_shutdown_cancels_queued_revision_and_ignores_its_late_callbacks` |
| 旧关闭成功回调可覆盖当前状态 | `test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_stale_revision_close_success_cannot_apply_an_old_plan` |
| 新任务丢失修订来源，创建异常会破坏编辑态 | `test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_revision_confirmation_passes_source_task_into_new_batch`；`test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_revision_task_creation_failure_keeps_all_edited_state` |
| 真实工作流边界未同步失效旧结果、资格和 token | `test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_revision_state_clears_only_at_new_abandoned_or_completed_batch_boundaries` |
| 页面 session ID 为空时可能跳过仍 active 的真实 manager | `test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_revision_close_uses_manager_state_when_page_session_id_is_empty` |
| 修订恢复未先完成整份原子预校验 | `test_douyin_commerce_service.DouyinCommerceBatchUiTests.test_revision_restore_prevalidates_atomically_and_can_retry` |
| 第三代修订未锁住祖先成功媒体，断链／环未固定拒绝 | `test_task_service.DouyinCommerceBatchTaskTests.test_prepare_revision_aggregates_all_ancestors_and_rejects_bad_chain` |

### Task 5

| RED 原因 | 对应 GREEN 测试名 |
| --- | --- |
| 任务明细没有“修改来源”摘要行 | `test_douyin_commerce_service.DouyinCommerceTaskDetailUiTests.test_task_detail_shows_revision_source_task_number` |

## 本轮新鲜集成证据

### 解释器路径校正

计划原命令使用 worktree 相对路径 `.venv/bin/python`。该路径在隔离 worktree 不存在，命令在测试发现前以 `exit 127` 结束，执行测试数为 `0`；这不是产品测试失败。验收随即停止，没有进入全量；只把解释器改为主项目共享虚拟环境的绝对路径，相关组合本身实际执行一次。

### 首轮相关组合与全量（历史证据）

```bash
QT_QPA_PLATFORM=offscreen /Users/andy/Documents/Codex/2026-07-28/new-chat/outputs/一键发桌面UI基座/.venv/bin/python -m unittest -v \
  test_douyin_commerce_batch_service \
  test_task_service \
  test_douyin_commerce_service.DouyinCommerceBatchUiTests \
  test_douyin_commerce_service.DouyinCommerceTaskPresentationTests
```

首轮相关结果：`Ran 177 tests in 5.622s`，`OK`，退出码 `0`。

首轮完整结果：`Ran 947 tests in 32.846s`，`OK`，退出码 `0`。随后聚焦审查发现 4 个 Important 并修改生产代码，因此这组 `177/947` 已降级为历史证据，不代表最终代码状态。

### 聚焦审查修复的 RED／GREEN

1. 关闭屏障后的来源状态竞态：RED `test_return_to_edit_rechecks_revision_plan_after_close_barrier` 显示规划接口只调用 1 次，旧计划被直接应用；GREEN 后关闭归零后再次读取并只应用最新合格计划。
2. 旧任务路径身份兼容：RED `test_revision_legacy_path_identity_matches_current_media_with_id` 返回 `applied=False`；GREEN 后当前素材同时提供 `media:` 主键和 `path:` 兼容别名，旧未完成项可映射、旧成功项仍锁定。
3. 批任务创建失败原子性：RED `test_batch_task_creation_failure_removes_partially_created_task` 显示补写异常后残留 `1` 个 task、`3` 个 items、`1` 个 event；GREEN 后异常补偿删除三类记录，不产生孤儿 pending 任务。
4. 来源逐条序号贯通：RED `test_revision_confirmation_preserves_all_source_item_indexes` 显示创建调用缺少 `batch_item_indexes`；GREEN 后严格校验 `revisionItemIndexes` 的等长、互异、内建正整数，随修订状态保存／清理并传给新任务。

修复后首次相关组合曾在既有 `test_revision_media_key_error_stays_fixed_and_fail_closed_in_ui` 停止：新增路径别名在禁止集合只有 `media:` 键时仍进行了不必要的路径解析，使本可确认的视频被一并拒绝。该单项修正为仅在存在旧 `path:` 禁止键时计算兼容别名，单项 GREEN 后重新执行相关组合。

### 修复后相关组合

命令同上。结果：`Ran 181 tests in 5.524s`，`OK`，退出码 `0`。

### 修复后最终完整回归

```bash
QT_QPA_PLATFORM=offscreen /Users/andy/Documents/Codex/2026-07-28/new-chat/outputs/一键发桌面UI基座/.venv/bin/python -m unittest discover -v
```

结果：`Ran 951 tests in 31.464s`，`OK`，退出码 `0`。后续 Fix round 1 再次修改了生产代码，因此该证据已降级为历史证据。

### 整分支终审 Fix round 1 RED／GREEN

1. 旧 schema v3 草稿的 `mediaId=None`：真实 Qt RED 显示绝对路径素材恢复为 `0` 条；GREEN 后草稿恢复同时使用 `media:` 主键和规范化 `path:` 别名，明确断言选中数 `1`、素材 ID `87`、当前视频 ID `87`，不改写合法路径。
2. 视频与来源条目号绑定：4 个真实 Qt RED 分别复现菜单缩成单条、删除后索引未更新、换序后索引不换序、菜单替换丢失其余视频；GREEN 后以媒体身份维护来源条目号映射，新视频只能继承唯一空出的来源槽位，提交前再拒绝数量错配、非内建正整数或重复索引。
3. `revisionSourceTaskId` 完整性：RED 证明 UI 提交前未重读、服务可关联不存在／已不可修订的来源、有修订后代的来源仍可删除；GREEN 后 UI 创建前重读规划，服务在同一写事务内重新确认来源存在且仍可关联，`delete_tasks()` 对已有修订后代的来源固定拒绝，多代链仍可追溯。
4. 批任务创建原子性：RED 显示成功路径使用 `2` 个连接，批次索引／批次事件失败及 `BaseException` 会使用 `3` 个连接（含补偿）；GREEN 后任务头、条目、`created`、批次索引、`batch_created`、修订来源都在同一事务。五个写入阶段故障注入及 `BaseException` 均只使用 `1` 个连接，真实 SQLite 中 tasks/items/events 均为 `0` 孤儿，不再使用二次补偿。

### Fix round 1 最终相关组合

相关组合增加 `test_douyin_commerce_batch_draft_service`，其余与上述范围一致。结果：`Ran 198 tests in 6.184s`，`OK`，退出码 `0`。本轮相关组合只执行一次，无首个失败。

### Fix round 1 最终完整回归

```bash
QT_QPA_PLATFORM=offscreen /Users/andy/Documents/Codex/2026-07-28/new-chat/outputs/一键发桌面UI基座/.venv/bin/python -m unittest discover -v
```

结果：`Ran 962 tests in 32.175s`，`OK`，退出码 `0`。追加 scoped 审查后又修改了生产代码，因此该组证据已降级为历史证据。

### Fix round 1 追加 scoped 审查 RED／GREEN

1. 绑定来源项竞态：UI RED 证明来源序号 `2` 在确认前变为 `success`、序号 `3` 仍为 `pending` 时，旧 `[2, 3]` 仍会调用创建；服务 RED 也证明“来源整体仍有待修订项”会错误放行已成功的绑定项。GREEN 后 UI 要求当前绑定序号是最新 `revisionItemIndexes` 的子集；服务在同一写事务内按 `batchItemIndex` 逐项确认唯一存在且仍为 `failed/pending`，旧库缺失序号时才按原始位置回退。
2. 严格整数边界：`True`、`"1"`、`1.0`、`2.7` 四组 RED 均可被 `int()` 宽松转换并落库；GREEN 后先要求 `type(index) is int`，再校验正数、唯一性和等长。
3. 首次相关组合立即停在旧多代链测试：该测试直接调用服务时未传递计划给出的来源序号，默认 `[1, 2]` 与真实 `[2, 3]` 冲突。只修正测试令其模拟真实 UI 传递，单项 `1/1 GREEN`。随后发现一个旧 UI 测试桩漏了最新计划必需的 `revisionItemIndexes`，在 offscreen 环境触发模态警告而等待；终止这些无摘要进程后，仅补齐测试桩并单项 `1/1 GREEN`。被中止且没有 `Ran/OK` 的命令不计作验收证据。

### 最终相关组合

相关组合包含 `test_douyin_commerce_batch_draft_service`、`test_douyin_commerce_batch_service`、`test_task_service`、`DouyinCommerceBatchUiTests` 与 `DouyinCommerceTaskPresentationTests`。结果：`Ran 201 tests in 6.814s`，`OK`，退出码 `0`。

### 最终完整回归

```bash
QT_QPA_PLATFORM=offscreen /Users/andy/Documents/Codex/2026-07-28/new-chat/outputs/一键发桌面UI基座/.venv/bin/python -m unittest discover -v
```

结果：`Ran 965 tests in 34.331s`，`OK`，退出码 `0`。随后又修改了草稿路径生产代码，因此该证据已降级为历史证据；之前被中止、无摘要的进程仍明确作废。

### 最终追加路径修复 RED／GREEN

真实 schema v3 草稿使用存在的 `legacy  final.mp4`。RED 在 normalize 阶段精确显示路径被改为 `legacy final.mp4`，尚未到 SQLite 和 Qt。GREEN 后 `mediaPath` 改用专用规范化：只去除首尾空白，保留内部字符和连续空格。单项完整覆盖 normalize → save → 真实 SQLite load → Qt restore，并明确断言选中数、素材 ID 和当前视频；全空白路径仍固定拒绝。两项单项均 GREEN。

### 生产路径修复后最终相关组合

结果：`Ran 203 tests in 7.090s`，`OK`，退出码 `0`。

### 生产路径修复后最终完整回归

```bash
QT_QPA_PLATFORM=offscreen /Users/andy/Documents/Codex/2026-07-28/new-chat/outputs/一键发桌面UI基座/.venv/bin/python -m unittest discover -v
```

结果：`Ran 967 tests in 34.427s`，`OK`，退出码 `0`。这是最后一次生产修复后唯一份新 full discover。

### Fix round 2 RED／GREEN

1. Qt refresh 稳定身份：RED 时修订选择的失败／待处理媒体 ID `[2, 3]` 在素材重排后变成 `[3, 1]`，明确选入成功媒体 `1`；原媒体消失时也错误保留 1 条选择。GREEN 后 refresh 前快照稳定媒体键与来源序号，重建后只接受唯一键匹配并重新运行成功媒体 blocked guard。缺失、歧义、禁选或绑定不一致时清空选择和当前修订序号，显示固定恢复失败。两个 Qt 单项 `2/2 GREEN`。
2. 真实路径端到端：RED 时 schema v3 `legacy  final.mp4` 虽能从 SQLite 恢复到 Qt，`collect_upload_payload()` 立即因 `fileList` 被折叠而报“需要且只允许一条可读取的视频素材”。GREEN 后共享 `normalize_media_path()` 只处理首尾空白，并贯穿草稿、UI 路径键、批量契约、单视频 `fileList`、会话身份与本地显示。真实测试覆盖 normalize/save/load → Qt restore → upload/batch collect → resave/load。随后静态路径审计又发现单视频恢复会把 `/tmp/a b.mp4` 与 `/tmp/a  b.mp4` 当作同一路径；冲突测试 RED 明确错选 ID `1`，GREEN 后精确选中双空格 ID `2`。

首次相关命令误包含不存在的 `test_douyin_commerce_session` 模块，在测试发现阶段以 `ModuleNotFoundError` 停止；该命令不计作产品失败或通过证据。会话测试实际在 `test_douyin_commerce_service.py` 内，修正命令后相关组合为 `262/262 OK`。最后单视频路径冲突生产修复使这组及其后 `969/969` full 降级为历史证据。

### Fix round 2 最终相关组合

结果：`Ran 263 tests in 8.201s`，`OK`，退出码 `0`。

### Fix round 2 最终完整回归

```bash
QT_QPA_PLATFORM=offscreen /Users/andy/Documents/Codex/2026-07-28/new-chat/outputs/一键发桌面UI基座/.venv/bin/python -m unittest discover -v
```

结果：`Ran 970 tests in 35.285s`，`OK`，退出码 `0`。随后静态路径审计发现上传身份仍以通用文本规则处理 `fileList` 并修改生产代码，因此 `263/970` 已降级为历史证据。

### Fix round 2 最终漏扫项 RED／GREEN

`_upload_identity()` 仍对 `fileList[0]` 使用通用 `_normalized()`，会把 `/tmp/a b.mp4` 与 `/tmp/a  b.mp4` 归并为同一上传身份，存在错误复用旧编辑会话的风险。新增单项先精确 RED：两个身份元组错误相等；最小 GREEN 后文件路径改用共享 `normalize_media_path()`，只去除首尾空白、保留内部连续空格，并明确与共享路径规范化结果一致。单项 `1/1 GREEN`。

### Fix round 2 新最终相关组合

相关组合覆盖批草稿、批契约、单草稿、批执行器、任务服务、普通带货 UI、批量 UI 与任务展示。结果：`Ran 366 tests in 9.792s`，`OK`，退出码 `0`。

### Fix round 2 新最终完整回归

```bash
QT_QPA_PLATFORM=offscreen /Users/andy/Documents/Codex/2026-07-28/new-chat/outputs/一键发桌面UI基座/.venv/bin/python -m unittest discover -v
```

结果：`Ran 971 tests in 36.934s`，`OK`，退出码 `0`。这是上传身份生产修复后的唯一份最终 full discover；验收不再重复全量。

## 核心不变量证据

- 原任务不可变：`test_prepare_revision_keeps_only_failed_and_pending_items` 在规划前后逐字段比较来源任务；`test_revision_child_records_source_without_mutating_it` 在创建修订子任务前后再次比较来源任务。二者均进入相关组合与完整回归并通过。
- 只复制明确未成功项：服务层只接纳 `failed` 与 `pending`；`success` 只生成稳定禁止媒体键，含 `running`、待核对、登录／验证或非用户暂停时整体拒绝。
- 多代成功媒体锁：`test_prepare_revision_aggregates_all_ancestors_and_rejects_bad_chain` 覆盖祖先汇总、断链和成环固定拒绝。
- 严格零存活关闭：返回修改先停止旧 worker，再关闭 collector 和真实 session manager；只有 `closed is True`，且 `aliveCollectorCount`、`aliveSessionCount` 都是内建 `int` 的 `0`，并二次回读 manager `active=false` 才载入修改计划。类型变异、manager active、竞态、双击、shutdown 和迟到回调测试均通过。
- 失败原子性：关闭不完整、来源状态变化、草稿／素材不可恢复、新任务创建失败时不跳页、不改来源任务，并保留当前可重试编辑态。

## 静态、差异与安全门禁

- `py_compile`：最终变更的 8 个生产 Python 文件与 1 个测试文件通过，退出码 `0`。
- `git diff --check`：工作树通过，无输出；`git diff 3df684b --check` 同样通过。
- 工作树：提交前仅包含本功能生产、测试与报告差异。
- 实施范围路径：无账号状态、数据库／SQLite、媒体、日志、`.env`、`.superpowers/brainstorm/` 或 `outputs/` 文件。
- 新增生产差异：`TODO`／`FIXME`／`NotImplemented`、空 `pass`／省略号占位均为 `0`。
- 高置信凭据扫描：AWS access key、私钥头、Bearer 凭据均为 `0`。
- 返回修改默认零平台动作 AST：检查 `return_unfinished_batch_to_edit`、关闭屏障、修订 UI 预校验／应用等 6 个方法，上传、预检、提交、发布或启动采集代际的禁止调用为 `0`。
- 修订规划只读 SQL：`prepare_douyin_batch_revision()` 内唯一直接 SQL 为 `SELECT`，非 `SELECT` 为 `0`。
- `progress.md` 未修改。

## 聚焦审查

初次只读审查 `db20b66..33cd918`：`0 Critical / 4 Important / 0 Minor`，结论为修复前不可合并。4 项分别为屏障后旧计划竞态、旧路径身份无法与当前媒体 ID 兼容、批任务第二阶段失败遗留孤儿任务、来源逐条序号未贯通。

修复后的 scoped 只读复审：`0 Critical / 0 Important / 1 Minor`，`Ready to merge: Yes`。该结论发生在 Fix round 1 之前，因后续生产代码改动降级为历史审查证据。

Fix round 1 首次 scoped 复审：`0 Critical / 2 Important / 0 Minor`，两项为已成功绑定序号可重复进入修订任务，以及序号宽松类型转换。修复后回看又发现 `0 Critical / 1 Important / 0 Minor`：旧 schema v3 真实路径内部空格被折叠。最终 scoped 复审：`0 Critical / 0 Important / 0 Minor`，`Ready to merge: Yes`。

Fix round 2 首次 scoped 复审：`0 Critical / 0 Important / 0 Minor`，`Ready to merge: Yes`；该结论早于最后一行上传身份生产修复，故降级为历史审查证据。对最终提交 `33699ae887854bd47c41a0d863ef2ed61769c2d4` 的只读 scoped 复审为 `0 Critical / 0 Important / 0 Minor`，`Ready to merge: Yes`。复审确认共享路径模块已纳入提交、`fileList` 上传身份可区分单双空格并触发重传，以及 refresh 身份／来源绑定、旧 schema v3 路径链、UI 与事务逐项状态门禁和严格内建整数边界均无回退。

## 证据边界与残余风险

### 已验证

- 本地媒体身份白名单、草稿持久化和旧数据路径回退。
- 失败／手动暂停任务的只读修订快照、来源关联和原任务不可变。
- Qt 离屏修订恢复、成功媒体禁选、结果页状态机、严格关闭屏障和任务明细来源展示。
- 本地错误固定脱敏、关闭失败原子性、worker／双击／shutdown／迟到回调竞态。

### 未验证

- 真实抖音页面 DOM 是否仍符合现有定位、关闭和回读契约。
- 真实账号登录、验证码、平台任务状态与作品回执。
- 真实上传、预检、平台草稿、提交、定时或公开发布。

因此，下一步如需真实验收，必须由 Andy 另行明确授权，并限定为低风险、单批次、提交前停住；本报告本身不构成任何真实平台成功证据。
