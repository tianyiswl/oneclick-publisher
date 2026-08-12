# 抖音带货地点返佣筛选终审修复报告

日期：2026-08-12

分支：`codex/douyin-location-commission-filter`

终审起点：`6bc0743`

边界：全部验证只使用离线替身、Qt offscreen 和本地隔离 HTML；不访问真实平台、账号，不上传、不预检、不保存平台草稿、不提交或发布。

## C1：点击后真实页面回读

### RED

- 用例：`DouyinCommerceLocationDomTests.test_keyword_equal_location_name_and_noop_click_cannot_fake_readback`
- 场景：地点输入框在点击前已是地点名，候选节点点击完全无效，节点没有 `selected/active` 页面状态。
- 结果：`Ran 1 test` / `FAILED (failures=1)`；旧实现未抛错，证明它只用“输入框名称 + 点击前候选”伪装成完整回读。

### GREEN

- 修复：点击后重新打开／读取当前候选面板，只接受页面实际标记为 `selected` 的唯一节点；再核对 POI、名称、完整地址和当前返佣筛选。返回值来自点击后页面节点，不再复用点击前候选。
- 无法证明完整身份时统一抛 `publish_location_readback_mismatch` 且无 cause。
- 单项结果：`Ran 1 test in 0.838s` / `OK`。

## I1：数量 token、千分位与冲突摘要

### RED

- 用例：`DouyinCommercePayloadTests.test_commission_summary_parses_all_bounded_tokens_and_rejects_conflicts`
- 结果：`Ran 1 test` / `FAILED (failures=5)`。旧解析把 `1,234` 错读为 `234`，把畸形 `1,23` 错读为 `23`，且多个商品／返佣数冲突时只取第一个。

### GREEN

- 修复：遍历全部有数字边界的数量 token，支持英文逗号和中文逗号的三位千分分组；畸形分组不再匹配尾数。任一同类数量出现不同值时返回 `unknown`，冲突字段数量为 `None`。
- 单项结果：`Ran 1 test in 0.001s` / `OK`。

## I2：DOM 摘要兜底边界

### RED

- 用例：`DouyinCommerceLocationDomTests.test_location_name_with_product_word_without_badge_is_no_commission`
- 结果：`Ran 1 test` / `FAILED (failures=1)`。无徽标候选“北海商品城／广西北海市商品街1号”被整行文本兜底误当成 `commerceInfo`。

### GREEN

- 本节是 Round 1 当时的历史证据；Round 1 后续追加的隐藏节点证据最多也只覆盖直接隐藏节点，已被 Round 2 的可见 wrapper 反例推翻并替换。
- 修复：DOM 摘要只接受有边界的“数量 + 件商品／件返佣”或明确“返佣／佣金”文本；名称节点、地址节点及其父子节点全部排除。同一规则用于候选描述与点击目标读取。
- 单项首次转绿后发现 Python 字符串逃逸警告，立即改为双反斜杠并重跑；最终 `Ran 1 test in 0.262s` / `OK`，无警告。

## I3：采集代际与内容指纹

### RED

- 用例：`DouyinCommerceBatchUiTests.test_active_setup_generation_locks_content_and_rejects_stale_fingerprint`
- 结果：`Ran 1 test` / `FAILED (failures=1)`。旧实现在活跃 setup generation 下仍启用账号选择，且没有内容指纹可阻断变更后的搜索与迟到回调。

### GREEN

- 修复：活跃／启动／关闭 setup generation 期间禁用账号、视频与清空／恢复入口；代际绑定“账号文件 + 整批视频路径”稳定指纹。搜索、采集回调和“继续”入口均重新比对；失配时只向后台派发严格 close，不在 Qt 线程同步等待。指纹一致时直接复用已就绪代际。
- 单项结果：`Ran 1 test in 0.140s` / `OK`。

## I4：当前搜索意图与上次结果分离

### RED

- 用例：`DouyinCommerceBatchUiTests.test_late_location_result_does_not_overwrite_current_intent_or_saved_draft`
- 结果：`Ran 1 test` / `FAILED (failures=1)`。旧结果回调会把顶部“本地／新关键词”改回“国内／旧关键词”，草稿也保存上次结果筛选而不是当前控件。

### GREEN

- 修复：上次已接纳结果状态仅驱动候选与每视频快照；顶部当前意图每次直接从范围、关键词、返佣控件读取，草稿只保存该意图。结果重绘不再回写顶部控件；请求 token 使已失效的成功／失败回调不能重建候选。每条自动填入地点仍冻结该结果发起时的 `commissionFilter` 与观测佣型。
- 单项结果：`Ran 1 test in 0.134s` / `OK`。

## I5：同 POI 异佣型 UI 快照

### RED

- 用例：`DouyinCommerceBatchUiTests.test_same_poi_with_different_commission_type_keeps_saved_snapshot_option`
- 结果：`Ran 1 test` / `FAILED (failures=1)`。旧 UI 只按 `poiId` 匹配，同 POI 的新“返佣”候选会冒充已保存的“无佣”视频快照，下拉框仅有 2 项而非应有的 3 项。

### GREEN

- 修复：当前候选与已保存项只在 `poiId + commissionType/observedCommissionType` 同时相等时合并。佣型不同时，新候选与旧视频快照作为独立选项并存，当前选中仍指向已保存快照。
- 单项结果：`Ran 1 test in 0.134s` / `OK`。

## I6：DOM 描述保留重复 option

### RED

- 用例：`DouyinCommerceLocationDomTests.test_same_commission_duplicate_options_survive_dom_then_stop_after_filter`
- 结果：`Ran 1 test` / `FAILED (failures=1)`。页面明确存在两个同名、同址、同返佣摘要的可见 option，旧描述层却只返回 1 条。

### GREEN

- 修复：在没有可证明的虚拟列表克隆规则时，`_store_option_descriptors` 保留每个可见 option，不再按身份+佣型提前去重。归一化边界先执行返佣筛选；不匹配筛选时得到空集，筛选后仍有重复完整地址时才明确报错安全停止。
- 单项结果：`Ran 1 test in 0.271s` / `OK`。

## I7：批量执行器严格关闭屏障

### RED

- 用例：`DouyinCommerceBatchExecutorTests.test_strict_close_barrier_pauses_batch_and_preserves_process_control`
- 结果：`Ran 1 test` / `FAILED (failures=1)`。严格关闭明确返回 `closed=False, aliveSessionCount=1` 后，旧执行器仍用吞错 `close` 继续，将三条全部记为 `preflighted`。

### GREEN

- 修复：每条视频结束仅调用 `close_strict`，并同时要求 `closed is True`、`aliveSessionCount` 是严格 `int 0`、状态回读 `active=false`。缺少严格入口或任一证据不足，以固定码 `batch_session_cleanup_incomplete` 标记当前条并用 `cleanup_incomplete` 暂停整批，后续条保持 pending。关闭屏障不捕获 `BaseException`，且普通关闭失败不会覆盖正在传播的进程控制信号。
- 单项结果：`Ran 1 test in 0.015s` / `OK`。

## M1：结构化候选幂等归一化

### RED

- 用例：`DouyinCommercePayloadTests.test_structured_commission_candidate_is_idempotent_and_rejects_invalid_types`
- 结果：`Ran 1 test` / `FAILED (failures=1)`。一次归一化为“返佣 12 件”的公开候选，二次归一化因已不含 `commerceInfo` 而被改写为“无佣”。

### GREEN

- 修复：新增幂等的结构化字段归一化边界。存在 `commissionType` 时严格验证并保留其佣型和有效数量；无效类型明确拒绝。只有原始 `commerceInfo` 时才解析文本；显式传入 `None`/布尔/数字/容器等非字符串时归为 `unknown`，不再被空字符串语义误判为无佣。
- 单项结果：`Ran 1 test in 0.001s` / `OK`。

## M2：地点结果稳定签名包含返佣证据

### RED

- 用例：`DouyinCommercePayloadTests.test_location_result_signature_changes_with_structured_commission_evidence`
- 结果：`Ran 1 test` / `FAILED (failures=1)`。同一地点分别改变佣型、商品数或返佣商品数时，旧实现仍只产生 1 个签名。

### GREEN

- 修复：稳定签名在名称、地址、距离之外，加入严格归一化的 `commissionType`、`productCount`、`commissionProductCount`；数量 0 也显式进入签名。平台候选身份未变但返佣证据改变时，不再被当成旧的稳定列表。
- 单项结果：`Ran 1 test in 0.001s` / `OK`。

## M3：任意顺序 UI mock 契约与验收结论纠偏

### RED

- 用例：`DouyinCommerceBatchUiTests.test_music_domestic_and_local_collectors_accept_any_order_in_one_generation`
- 结果：首个子用例 `order=('music', 'domestic', 'local')` 失败。旧 `location_result` mock 只接收三个位置参数，无法承接真实 UI 链路的 `commission_filter` 和 `include_metadata`，且旧断言只看 `call.args`，漏掉两个关键字参数与回包成功状态。

### GREEN

- 本节是 Round 1 当时的历史证据；其中对 mock 自行追加 `ok=True` 的断言已被 Round 2 判定为自证，并被生产 runner/UI 接纳证据替换。
- 修复：mock 改为 `*args, **kwargs` 任意顺序接收，逐次断言三个位置身份参数、`commission_filter='commission'`、`include_metadata=True`，并断言国内／本地每个回包都是 `ok is True`。测试同步绑定 setup generation 内容指纹，避免绕过 I3 新契约。
- 单项结果：`Ran 1 test in 0.146s` / `OK`。
- 报告纠偏：正式验收报告已把旧 `901/901 OK` 明确标记为被本次终审推翻的历史证据，不再用它声称功能完成或风险清零；新结论只能由本轮修复后的组合／全量验证产生。

## Scoped re-review round 2/5

### I2：可见 wrapper 不得重新带入隐藏后代文本

- RED 用例：`DouyinCommerceLocationDomTests.test_hidden_descendant_commission_text_is_excluded_in_both_dom_entries`。本地隔离 DOM 同时覆盖可见 wrapper 内嵌 `aria-hidden=true` 与 `opacity:0` 的“15件商品 · 15件返佣”，以及真正可见的嵌套徽标。结果：`Ran 1 test in 0.670s` / `FAILED (failures=1)`；两个隐藏场景均被父 wrapper 的 `innerText` 重新读成返佣。
- GREEN：两个 DOM 入口使用同一逻辑，沿节点至 option 的祖先链验证 `hidden/aria-hidden/display/visibility/opacity/content-visibility` 及布局矩形；然后克隆候选节点，剔除不可见后代，只从剩余 `textContent` 构建摘要。隐藏两项均为 `no_commission`，可见嵌套徽标仍为 `commission`。`Ran 1 test in 0.334s` / `OK`。

### M3：任意顺序必须由真实 runner 回调链接纳

- 新用例不再断言 mock 自己追加的 `ok`；mock 只返回完整结构化 envelope。测试使用生产 `BackgroundTaskRunner` 和受控队列池，真实执行 worker、Qt signal、`_collector_action_succeeded` 及地点成功 handler。
- RED：新用例在未改生产时已直接通过，说明本项是旧测试证据缺口，不伪造生产缺陷。按回归测试 mutation 校验，临时切断生产 `on_success(payload)` 后，三种操作顺序全部因 UI `platformResultCount` 仍为 `0` 而失败：`Ran 1 test in 0.149s` / `FAILED (failures=3)`。
- GREEN：恢复生产成功回调后，逐顺序验证 domestic/local 的 `_batch_location_state()` 范围、关键词、返佣筛选、平台计数 `1`、候选数 `1`、返佣类型及无失败/错误状态；同时保留 `commission_filter='commission'` 和 `include_metadata=True` 参数断言。`Ran 1 test in 0.137s` / `OK`。

## 终审后组合与全量证据

### 收尾 diff 审计追加 RED/GREEN

第一轮相关组合 `449/449` 和全量 `911/911` 通过后，只读生产 diff 审计又发现 3 个真实缺口，因此这两组通过证据立即作废，未被用作最终交付结论。

1. C1 否定类名：在既有无效点击用例中把 option 设为 `class="is-not-selected"`。RED：`Ran 1 test in 0.829s` / `FAILED (failures=1)`，证明旧正则把否定 token 当成已选。GREEN：显式 `aria-selected` 优先，只在属性缺失时接受无否定 token 的正向类；`Ran 1 test in 0.823s` / `OK`。
2. C1 `all` 佣型回读：新增 `test_all_filter_readback_rejects_changed_commission_type`，点击后同 POI 从返佣变为无佣。RED：`Ran 1 test in 0.839s` / `FAILED (failures=1)`。GREEN：点击后唯一 selected 节点还必须与点击前目标佣型相等；`Ran 1 test in 0.813s` / `OK`。
3. I2 隐藏摘要：在无徽标“北海商品城”用例加入 `hidden` 返佣节点。RED：`Ran 1 test in 0.266s` / `FAILED (failures=1)`。GREEN：两个 DOM 读取入口均排除 hidden/aria-hidden/display:none/visibility/opacity/content-visibility 节点，只读取有可见布局矩形的 `innerText`；`Ran 1 test in 0.262s` / `OK`。
4. I3 入队即锁：新增 `test_queued_setup_generation_immediately_locks_content_controls`。RED：`Ran 1 test in 0.134s` / `FAILED (failures=1)`，setup worker 已 active 但账号仍启用。GREEN：`runner.run` 返回后立即 `_sync_view()`，实时投影账号／视频锁；`Ran 1 test in 0.133s` / `OK`。
5. C1 否定类名分隔写法：第二轮只读复审指出 `un-selected` / `un_chosen` 仍可被末尾正向单词误命中。将既有无效点击 fixture 改为 `is-un-selected` 类名。RED：`Ran 1 test in 0.838s` / `FAILED (failures=1)`。GREEN：`aria-selected=true` 作为权威选中证据；无 ARIA 时，所有含 `selected/chosen` 的 class token 都必须是精确 token `selected` / `chosen`，任何否定或混合 token 都不放行。`Ran 1 test in 0.829s` / `OK`。最后只读复审确认无 blocker。

### 相关组合

- 范围：Service Payload + DOM + SessionContract、collectors、BatchUi、batch executor、draft/batch draft/batch service、task service/page。
- 首次组合暴露旧测试替身未模拟点击后 `selected` 状态，以及旧 BatchUi fixture 手工注入 generation 却未绑内容指纹；分别单项修复并转绿。Qt 段一度因这些失效代际真实派发后台关闭而 `SIGSEGV`，用 `PYTHONFAULTHANDLER` 定位后将“有效测试代际”统一绑定当前内容指纹，崩溃定位单测 `1/1 OK`。
- 随后两个兼容失败严格按“单项修复→单项 GREEN→重跑相关组合”收口：草稿旧断言改为验证当前顶部意图；隔离 generation 测试的 mock 启动指纹与当前页面保持一致。
- Round 2 复审发现 I2 wrapper 隐藏后代和 M3 证据缺口，因此 `Ran 451 tests in 15.628s` / `OK` 已降级为历史证据。Round 2 新鲜相关组合：`Ran 452 tests in 16.203s` / `OK`，退出码 `0`。

### 完整离线回归

- 命令：`QT_QPA_PLATFORM=offscreen ../../.venv/bin/python -m unittest discover -v`。
- Round 2 复审发现上述两项未闭环，因此 `Ran 913 tests in 30.004s` / `OK` 已降级为历史证据。Round 2 新鲜最终全量：`Ran 914 tests in 31.071s` / `OK`，退出码 `0`。
- 证据链说明：旧 `901`、`449/911`、首轮 `451/913` 及上轮最终 `451/913` 均已被后续终审或 scoped re-review 推翻，仅作历史证据。

## Finding 收口

| Finding | 状态 | 离线证据边界 |
| --- | --- | --- |
| C1 | 已修复 | 无效点击不能复用点击前候选伪造回读；无法证明完整身份固定码停止 |
| I1–I2 | 已修复 | 数量 token／千分位／冲突与 DOM 摘要边界已回归；隐藏后代不得经可见 wrapper 泄入 |
| I3–I5 | 已修复 | 内容指纹、搜索意图与结果、同 POI 异佣型快照已隔离 |
| I6–I7 | 已修复 | DOM 重复 option 过滤后判重；批处理只在严格零存活回读后继续 |
| M1–M3 | 已修复 | 幂等归一化、稳定签名、关键字参数与真实 runner/UI 回调接纳均已回归 |

当前结论：Round 2 两个 scoped finding 已完成单项 RED/GREEN，新相关组合 `452/452` 和新最终全量 `914/914` 均通过，本地离线契约验收通过。这不是真实抖音 DOM、真实账号、上传、预检、草稿或发布成功证据。

## 最终离线收尾审计

- Round 2 独立只读 diff 复审：I2 两个 DOM 入口的祖先可见性与 clone 剔除逻辑一致，可见嵌套徽标仍保留；M3 走生产 `BackgroundTaskRunner`、Qt signals、生产成功 handler 并断言最终 UI 状态；无 blocker。
- `py_compile`：9 个指定生产文件 + 2 个相关测试文件通过，退出码 `0`。
- AST：9 个指定生产文件可解析，`AST_OK=9`。
- `git diff --check`：通过，无输出。
- 敏感文本：真正持久化模块（batch service、batch draft service、设置页）中，禁用字段名 `commerceInfo/innerHTML/outerHTML/cookie/verificationCode` 的精确 AST 常量计数为 `0`。即时归一化器中有 `3` 个 `commerceInfo` AST 常量引用（源码两行），仅用于 M1 要求的原始 DOM 摘要输入类型检查与即时解析；输出只保留结构化佣型／数量，不进入公开候选、批次或草稿。
- 占位／凭据扫描：新增生产 diff 未命中 `TODO/FIXME/NotImplemented`，未命中 AWS key、私钥或 Bearer 凭据模式。
- 范围：`progress.md` 无变更。
