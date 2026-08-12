# 抖音带货地点返佣筛选验收报告

验收日期：2026-08-12

验收分支：`codex/douyin-location-commission-filter`

实施基线：`14d8d40`

原始验收节点：`6bc0743`

终审修复起点：`6bc0743`

## 结论

原“离线实施验收通过”结论已被对 `14d8d40..6bc0743` 的终审推翻。终审发现 1 个 Critical、7 个 Important 和 3 个 Minor；其中包括点击无效仍伪造回读、采集代际与内容漂移、批量会话关闭不严格等安全性问题。因此，旧全量测试通过仅是当时测试集的历史证据，不证明当时功能已正确或风险已清零。

scoped re-review round 3/5 发现 Round 2 可见性修复仍把祖先 `visibility:hidden` 当作不可覆盖条件，并且未在 listbox/option 入口排除透明或 aria-hidden 外层 portal。Round 3 I2 已完成单项 RED/GREEN，新相关组合 `453/453` 与新最终全量 `915/915` 均通过；Round 2 `452/914` 已撤回为历史证据，当前离线实现验收改由 Round 3 新鲜证据支持。

本结论只证明本地代码、离线替身、Qt offscreen 界面契约以及本地隔离 HTML 的 DOM 契约。没有连接抖音平台，没有启动真实账号会话，没有上传素材，没有执行预检，没有保存平台草稿，也没有正式提交或发布。

## 设计覆盖审计

| 验收项 | 静态结论 | 主要证据 |
| --- | --- | --- |
| 三档文案与默认值 | 通过 | 设置页顺序为“全部／返佣／无佣”，新批次默认 `commission` |
| 无标识归无佣 | 通过 | 空可见摘要解析为 `no_commission` |
| unknown 只进全部 | 通过 | `filter_location_candidates` 仅在 `all` 保留 `unknown` |
| 只填空行、已选不覆盖 | 通过 | 自动填入只遍历未出现在 `_batch_locations` 的视频路径 |
| 每条独立筛选 | 通过 | 保存地点时冻结 `commissionFilter` 与观测字段；顶部切换不回写已选视频 |
| 旧任务兼容 | 通过 | 缺少筛选字段时逐视频发布载荷与执行器均按 `all` 处理 |
| 正式发布 mismatch | 通过 | 固定错误码 `publish_location_commission_mismatch`，不回退到全部或其它地点 |
| 后续视频继续 | 通过 | 批执行器回归覆盖当前视频 mismatch 后下一条继续 |
| 账号级预设边界 | 通过 | `save_location_preset` 仅接收 `poiId/name/address` 稳定身份 |
| 无原始 DOM 持久化 | 通过 | 批次、草稿、设置页持久化边界无 `commerceInfo/innerHTML/outerHTML/Cookie/verificationCode` |

## 旧全量回归（已被终审推翻）

Task 6 当时记录过以下完整回归：

```bash
QT_QPA_PLATFORM=offscreen /Users/andy/Documents/Codex/2026-07-28/new-chat/outputs/一键发桌面UI基座/.venv/bin/python -m unittest discover -v
```

历史结果：`Ran 901 tests in 32.231s`，`OK`，退出码 `0`。

这 901 项不包含本轮终审补入的失败用例，已不能作为当前代码的验收结论。它只能说明旧测试集在当时退出码为 0。

## 终审修复后新证据

- 历史证据：旧 `901`、`449/911`、历次 `451/913` 以及 Round 2 `452/914` 均已被后续终审或 scoped re-review 推翻，不是当前验收证据。
- 相关组合：Service Payload + DOM + SessionContract、collectors、BatchUi、batch executor、draft/batch draft/batch service、task service/page。
- Round 2 相关组合历史结果：`Ran 452 tests in 16.203s`，`OK`，退出码 `0`。Round 3 新相关组合：`Ran 453 tests in 21.673s`，`OK`，退出码 `0`。
- 完整离线回归命令：`QT_QPA_PLATFORM=offscreen ../../.venv/bin/python -m unittest discover -v`。
- Round 2 完整离线回归历史结果：`Ran 914 tests in 31.071s`，`OK`，退出码 `0`。Round 3 新最终全量：`Ran 915 tests in 32.549s`，`OK`，退出码 `0`。
- 全部证据仍仅限离线替身、Qt offscreen 和本地隔离 HTML。

## Scoped re-review round 2/5

- I2 RED：本地隔离 DOM 在可见 wrapper 内嵌 `aria-hidden=true` 或 `opacity:0` 的“15件商品 · 15件返佣”；两个场景都被旧 wrapper `innerText` 误读为返佣，`Ran 1 test in 0.670s`，`FAILED (failures=1)`。GREEN：两个 DOM 入口统一校验祖先链可见性，在 clone 中剔除隐藏后代后才读 `textContent`；隐藏场景均为 `no_commission`，真正可见嵌套徽标仍为 `commission`。`Ran 1 test in 0.334s`，`OK`。
- M3 RED：新测试使用生产 `BackgroundTaskRunner` 和受控任务池，mock 只返回结构化 envelope，不再断言 mock 自己的 `ok`。未改生产时新测试已直接通过，证明此项是测试证据缺口；受控 mutation 临时切断生产成功回调后，三种顺序均因 UI 计数仍为 `0` 失败，`Ran 1 test in 0.149s`，`FAILED (failures=3)`。GREEN：恢复生产回调后，逐顺序断言 UI 已接纳的范围、关键词、返佣筛选、平台计数、候选数、返佣类型与无错误状态，并保留 `commission_filter/include_metadata` 参数断言。`Ran 1 test in 0.137s`，`OK`。

## Scoped re-review round 3/5

- I2 RED：本地隔离 DOM 中，父 wrapper `visibility:hidden` 而子徽标 `visibility:visible`，Playwright 确认子徽标可见，旧逻辑仍读成空摘要；外层 portal 为 `opacity:0` 或 `aria-hidden=true` 时，旧列表入口仍放行。`Ran 1 test in 0.524s`，`FAILED (failures=3)`。
- I2 GREEN：列表识别、option 候选和两个摘要入口共用单一页面 JS helper。目标节点 computed `visibility` 允许 CSS 子节点覆盖；祖先链仍否决 `display:none`、`hidden`、`aria-hidden`、`content-visibility:hidden`、任一层 `opacity:0` 与无正尺寸布局矩形。可覆盖徽标正确为 `commission`；透明或隐藏 portal 不产出列表、描述或点击目标。`Ran 1 test in 0.329s`，`OK`。

## 终审 finding 状态

- C1：已修复。点击后只接受真实面板唯一选中节点回读，重新核对 POI、名称、完整地址与佣型；`aria-selected=true` 作为权威证据，无 ARIA 时仅放行精确 `selected/chosen` token，否定或混合 token 安全停止。证据不足统一固定码停止。
- I1–I7：已修复。I2 已追加 CSS visibility 覆盖与透明／隐藏 portal 回归；Round 3 相关组合 `453/453` 与最终全量 `915/915` 均通过。
- M1–M3：已修复。M3 现通过真实 runner 成功回调后的 UI 状态、候选数和无错误文案证明接纳，不再检查 mock 自己的成功值；新组合／全量通过。

## 终审修复后收尾审计

- Round 2 独立只读 diff 复审当时记录“无 blocker”，但已被 Round 3 CSS visibility 覆盖反例推翻；只作历史审计证据。
- Round 3 独立只读 diff 复审：无 blocker。确认 CSS visibility 子节点覆盖、透明／隐藏 portal 排除，以及 listbox、option、两个摘要入口的单一 helper 语义一致；复审未修改、未测试、未访问平台。
- `py_compile`：9 个指定生产文件和 2 个相关测试文件通过，退出码 `0`。
- AST：9 个指定生产文件全部可解析。
- `git diff --check`：通过，无输出。
- 真正持久化模块（batch service、batch draft service、设置页）的禁用字段名精确 AST 常量：`0`。即时归一化器中有 `3` 个 `commerceInfo` AST 常量引用（源码两行），仅执行原始 DOM 摘要输入类型检查与即时解析；输出不包含该字段，不进入公开候选、批次或草稿。
- 新增生产 diff 未发现 `TODO/FIXME/NotImplemented`，未发现 AWS key、私钥或 Bearer 凭据模式。
- `progress.md` 无变更。

## 旧编译、差异与敏感审计（历史证据）

- Task 6 指定的 9 个生产文件执行 `py_compile`：当时通过。
- `git diff --check`：当时通过，无输出。
- AST：10 个相关生产文件全部可解析。
- 范围：相对实施基线仅改动计划授权的生产与测试文件；`ui/task_page.py` 只验证，无生产差异。
- 占位实现：新增生产差异中未发现 `TODO`、`FIXME`、`NotImplemented`、空 `pass` 或省略号占位。
- 敏感文本：计划指定的四个持久化边界文件中未发现 `commerceInfo`、`innerHTML`、`outerHTML`、`cookie`、`verificationCode`。
- 敏感 AST：上述持久化边界模块中禁用字段常量数为 `0`。
- 账号预设 AST：设置页只有一个 `save_location_preset` 调用，使用三个位置参数；传入候选已先收敛为 `poiId/name/address`。
- `commerceInfo` 仅在 `app_core/douyin_commerce_service.py` 的当前 DOM 描述读取与即时解析入口短暂出现；出口仅保留公开结构化字段。

## 证据边界

### 已验证：离线代码与本地 UI

- 返佣摘要分类、白名单、筛选与先过滤后判重。
- 批次、草稿 v4、旧数据、任务载荷、续发和任务摘要。
- Qt offscreen 设置页控件、默认值、状态文案、自动填入和生命周期重置。
- mismatch 固定诊断、当前视频停止、后续视频继续。

### 已验证：本地隔离 DOM

- 本地 HTML 中同名同址的返佣／无佣候选可在当前面板按筛选唯一定位。
- 点击后地点输入框回读符合保存身份。
- 这不证明真实抖音页面结构、真实候选数据或账号状态当前可用。

### 未验证：真实平台

- 未验证真实抖音 DOM 是否仍与本地契约一致。
- 未验证真实账号下“返佣／无佣／全部”的候选数量与标签。
- 未验证任何上传、预检、平台草稿、提交、定时或公开发布结果。

## 真实账号三步低风险检查清单

以下检查由用户另行明确授权后人工执行；只在设置页读取候选和观察自动填入，不上传、不预检、不保存平台草稿、不提交。

1. 默认返佣：打开一份可安全读取的设置会话，确认顶部默认显示“返佣”；搜索一个已知关键词，记录“平台返回数／返佣筛选后数量／自动填入数／待选数”，确认只有返佣标签候选进入空行，已有地点不变。
2. 切换无佣：把一条测试行主动清回“请选择地点”，切换“无佣”并用同一关键词重新搜索；确认只给该空行填入无佣候选，其他已选返佣地点不被覆盖，结果为空时不回退到全部。
3. 切换全部：再次清空测试行，切换“全部”并搜索；确认返佣、无佣和待确认候选按平台可见结果展示，同身份重复项保持歧义停止，不默认第一项、不模糊匹配。

完成三步后立即关闭候选面板并退出设置会话；不选择媒体、不上传、不进入预检、不保存平台草稿、不点击任何提交或发布按钮。
