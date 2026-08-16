# 终审修复报告

- 日期：2026-08-16
- 修复起点：`ba35f3ffae942cb0e9b9eb065160b27184f445c1`
- 实现提交：`1cd0ab3d39c8af0a8fa24f7436b1bd4e9cedc390`
- 分支：`fix/douyin-location-progress-diagnostics`
- 闭环层级：交付闭环（离线代码、测试和审计证据）
- 限制：未访问浏览器、账号或抖音平台；不宣称真实平台运行或业务结果。

## 关闭结论

- Important：7/7 已关闭。
- Deferred Minor：1/1 已关闭。
- 非目标保留：非法 `max_*` 参数继续映射旧模糊码，按 findings 明确允许本轮不改。

## I1—I7 与 Deferred 的 RED/GREEN

### I1：稳定的前 100 个唯一候选

- RED：`test_publish_accepts_hundredth_target_and_ignores_later_same_page_rows`；同批第 100 条目标被第 101 条触发的上限提前误杀。
- GREEN：按平台顺序只接纳剩余唯一身份容量；第 100 条可匹配，第 101 条不进入匹配或持久化，`candidateCount <= 100`。
- 组合回归补漏：`test_publish_rejects_a_target_arriving_beyond_candidate_limit` 曾误报 all-pages；调整边界检查顺序后单项 GREEN。

### I2：只统计真实成功点击

- RED：`test_publish_does_not_count_missing_load_more_control_as_a_click`；无控件时误消耗点击预算。
- GREEN：无控件回包显式 `clickPerformed=False`，不增加跨关键词计数。
- RED：`test_publish_counts_a_successful_click_before_load_readback_timeout`；点击已成功但后续回读超时时诊断仍为 0。
- GREEN：点击后所有受控失败携带 `click_performed=True`，超时诊断计入该次点击。
- RED/GREEN：`test_publish_load_more_deadline_stops_before_candidate_poll` 补充异常点击标记断言，确认成功点击后 deadline 失败与成功回包的计数语义一致。

### I3：分页按钮重现 deadline 不得吞掉

- RED：`test_load_more_reappear_wait_propagates_its_deadline`；重现窗口到期被转换为无更多候选。
- GREEN：deadline 保持上抛，上层固定映射为 `publish_location_action_timeout`。

### I4：搜索前与 finally 各有独立 30 秒清理期限

- RED：`test_publish_gives_preflight_and_final_cleanup_independent_deadlines`；两次严格清理未收到独立 deadline。
- GREEN：每个关键词前的清理和该次 finally 清理各自新建 30 秒绝对期限。
- RED：`test_publish_stops_a_slow_preflight_cleanup_at_its_deadline`；慢清理后仍可能进入搜索或误分类为点击失败。
- GREEN：清理到期固定收敛为 `publish_location_cleanup_incomplete`，搜索未启动。
- RED：`test_publish_keeps_original_failure_when_final_cleanup_also_fails`；finally 的普通清理错误覆盖了原业务失败。
- GREEN：原失败或进程控制异常优先，普通清理失败仅在无活动失败时成为终止原因。

### I5：九字段、scope 和 all-pages 真实贯穿

- RED：`test_publish_all_pages_failure_has_complete_scoped_diagnostic`；all-pages 无完整诊断。
- GREEN：service 产生完整九字段，包括 `scope` 与 `stage=all_pages`。
- RED：`test_apply_saved_location_preserves_new_pagination_diagnostics`；四个新停止码在 session 投影中缺 `scope`。
- GREEN：service → session 的白名单为同一组九字段。
- RED：`test_all_pages_scoped_diagnostic_reaches_task_and_next_video_continues`；用户文案缺阶段/范围等上下文，SQLite 缺 `scope`。
- GREEN：executor 动态提示包含阶段、关键词、范围、点击数、候选数和固定码；SQLite 保存完整九字段，第二条继续并成功。

### I6：仅可信会话异常可携带严格快照

- RED：`test_forged_known_location_code_cannot_inject_diagnostic`；任意异常伪造已知码后，Cookie/路径/DOM 形状诊断被写入事件。
- GREEN：仅精确 `DouyinCommerceSessionError` 和执行器内部精确异常可产生地点固定码；任意 `.code`/`args[0]` 不再被信任。
- GREEN 严格条件：快照必须恰好九字段，`errorCode` 与异常一致，stage/scope 在枚举中，keyword 必须是当前受控搜索词，整数必须为原生类型且不超过 100/10/30 安全范围。
- RED：`test_malformed_location_snapshot_cannot_persist_diagnostic_fields`；非枚举字符串和越界整数均可落库。
- GREEN：task service 对固定码、stage、scope、keyword 和各数值字段分别做原生类型、枚举、敏感形状和安全上限检查。
- 兼容 GREEN：`test_exception_attributes_that_raise_are_safely_ignored`；不可信属性不泄露，第二条继续。进程控制 `BaseException` 仍由既有边界原样传播。

### I7：非地点失败固定为通用批次分类

- RED：`test_non_location_failure_uses_generic_batch_classification_and_continues`；设置基线的普通异常误显示为“地点已找到但点击失败”。
- GREEN：只有可信地点固定码进入地点格式化器；上传、设置、音乐、声明、排期、预检和其他普通异常固定为 `douyin_batch_item_failed`，中文提示不包含原异常。
- 继续性：首条失败后第二条仍完成；事件 `detailJson` 为空对象，没有地点误诊断。

### Deferred Minor：新停止码的安全回退

- RED：`test_new_location_stop_codes_have_safe_fallback_and_minimal_readback` 的 3 个子用例；action-timeout、click-limit、candidate-limit 在诊断缺失或数值畸形时只显示裸错误码。
- GREEN：三个码各有固定安全中文文案，事件最少保留 `{"errorCode": <fixed code>}`；畸形其他字段不落库，第二条继续。

## 测试命令与证据

所有测试均使用：

```text
QT_QPA_PLATFORM=offscreen ../../.venv/bin/python -m unittest <test-id> -v
```

- 每个 Important/Deferred 的上述定向用例均先执行 RED，然后单项 GREEN。
- 相关组合：

```text
test_douyin_commerce_service.DouyinCommerceLocationDomTests
test_douyin_commerce_service.DouyinCommerceSessionContractTests
test_douyin_commerce_batch_executor.DouyinCommerceBatchExecutorTests
test_task_service.DouyinCommerceBatchTaskTests
test_task_page.TaskDetailDialogTests
```

  - 唯一一次组合运行执行 272 项，暴露 4 failures + 1 error。
  - 失败分别为：第 101 条超限分类顺序、新 `scope` 的旧断言、独立清理后的模拟时钟、两个仍用 `RuntimeError` 伪造地点码的旧测试替身。
  - 按运行顺序逐个修复，5 个失败用例均分别单项 GREEN；遵守“相关组合只跑一次”，未重复运行该组合。
- 受影响模块（唯一一次）：

```text
QT_QPA_PLATFORM=offscreen ../../.venv/bin/python -m unittest \
  test_douyin_commerce_service test_douyin_commerce_batch_executor \
  test_task_service test_task_page -v
```

  - 结果：`Ran 645 tests in 44.758s` / `OK` / 退出码 0。
  - 未运行 full discover。

## 静态验证与审计

- `py_compile`：编译 4 个生产模块和 3 个改动测试模块，退出码 0。
- `git diff --check`：退出码 0。
- 敏感样本检查：在 `app_core` 和 `ui` 扫描测试中的伪 Cookie、账号文件路径、DOM 标记和私有路径样本，无生产代码命中。
- 持久化白名单：未知字段、候选列表、Cookie、DOM 和路径不落库；受影响模块中的白名单、scope 原生类型、畸形快照和 UI 事件详情测试均通过。
- 会话关闭：没有加入普通 `close` 回退；既有严格零存活关闭屏障和进程控制异常传播测试均在 645 项中通过。

## 变更文件

- `app_core/douyin_commerce_service.py`
- `app_core/douyin_commerce_session.py`
- `app_core/douyin_commerce_batch_executor.py`
- `app_core/task_service.py`
- `test_douyin_commerce_service.py`
- `test_douyin_commerce_batch_executor.py`
- `test_task_service.py`
- `.superpowers/sdd/2026-08-16-douyin-publish-location-progress-and-diagnostics/final-review-fix-report.md`

## 残余风险

1. 证据层仅为离线测试和静态审计；根据本轮禁止访问平台的边界，没有真实抖音页面回读证据。
2. 诊断信任边界依赖生产会话管理器抛出精确 `DouyinCommerceSessionError`；任意第三方异常会安全降级为通用批次失败，可能牺牲细分错误文案，但不会泄露原始信息。
3. 非法 `max_*` 参数仍保留旧模糊码，是 findings 明确接受的剩余 Minor，本轮未扩大范围。

## Final fix round 2 — 混合关键词状态

上一版“Important 7/7 已关闭”的结论被 scoped review 推翻：关键词 1 已成功穷尽、关键词 2 搜索失败时，旧实现仍用最后关键词生成 `publish_location_not_found_after_all_pages`。该历史结论不再作为合并证据。

- RED：`test_publish_does_not_report_all_pages_when_later_keyword_search_fails` 实得 `publish_location_not_found_after_all_pages`，期望非 all-pages 固定码。
- GREEN：服务层改为逐关键词记录 `successfully_exhausted_keywords`；只有所有受控关键词都成功穷尽时才产生 all-pages 九字段。任一关键词搜索失败则固定降级为 `publish_location_candidate_missing`。
- RED：`test_mixed_keyword_search_failure_persists_non_all_pages_error_and_continues` 初次执行时 SQLite `detailJson={}`。
- GREEN：可信地点固定码在没有完整九字段时至少保存 `errorCode`；测试真实贯穿 service → session → executor → SQLite，并确认第一条失败后第二条继续。
- 相邻路径：混合状态、真实单关键词穷尽、all-pages 九字段和两条继续共 5/5 通过。
- 相关类：`DouyinCommerceLocationDomTests + DouyinCommerceBatchExecutorTests` 为 153/153 通过，34.380 秒，退出码 0。
- 最终跨层用例在改为真实 service/session 后再次执行 1/1 通过。
- 静态：4 个改动 Python 文件 `py_compile` 与 `git diff --check` 均退出码 0。

本轮仍仅达到离线交付闭环，未访问真实浏览器、账号或抖音平台。
