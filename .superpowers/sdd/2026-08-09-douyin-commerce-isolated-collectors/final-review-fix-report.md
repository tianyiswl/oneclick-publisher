# 最终全分支审查集中修复报告

## 结论与边界

- Base SHA：`981c632ff9a86ecfb428638237cac9bdcf43443c`。
- 本轮仅修复简报锁定的四项：retry 进程控制异常终态、真实探针 `accountId` 链路、验证器 `cleanup_interrupted` 白名单、UI 文案与工作流顺序。
- 未修改 `progress.md`，未改动真实页面选择器、批量提交门、账号读取逻辑或发布回执规则。
- 未运行真实 `--execute`，未读取真实账号，未启动真实浏览器，未访问抖音，未上传、预检、保存草稿、提交或发布。

## 修复结果

### 1. retry 进程控制异常

- `retry_collector` 关闭旧 manager 时只显式处理 `KeyboardInterrupt` 和 `SystemExit`。
- 复用 generation cleanup 的 `_complete_collector_close_locked()` 终态化帮助逻辑：固定写入 `cleanup_interrupted`，将旧槽标为 `failed`，完成 owner，释放 manager identity 注册，保留 runtime 供后续严格关闭重试。
- 终态化后使用原样 `raise`，异常继续保留在 action queue Future 边界；没有捕获通用 `BaseException`，也没有吞掉进程控制语义。
- 后续同一单线程队列可继续执行，新 `close_generation` 可声明新 owner，对同一 manager 再次严格关闭成功后返回 `closed=True` 和 `aliveCollectorCount=0`。

### 2. 真实探针 `accountId` 链路

- `build_probe_upload_payload()` 在安全白名单中保留严格内建、非负整数 `accountId`。
- `bool`、负整数、字符串和自定义转换对象固定抛出 `DouyinCommerceProbeError`，不执行自定义 `__int__`。
- UI 生产 `collect_upload_payload()` 现在携带当前账号的本机 `accountId`。
- 真实 builder 加 fake Session 的跨模块回归证明 runtime 保留 `31`，公开诊断生成 `account-31`，不暴露账号文件路径。

### 3. 验证器 cleanup 投影

- `_CLEANUP_RESULTS` 新增固定合法值 `cleanup_interrupted`。
- 公开 close 投影和最终 JSON 写盘净化两层都保留该值；未知 cleanup 值仍降级为 `cleanup_unknown`。

### 4. UI 文案与工作流顺序

- 平台设置步骤提示改为“独立临时采集会话读取候选；本页仅本机暂存，正式发布时逐条重新核验”，不再误说为同一抖音编辑页。
- 工作流文档按真实执行调用改为：上传并回读标题/文案/标签 → `prepare_publish_settings` 干净基线 → 音乐/声明/地点/排期 → 预检/提交。

## TDD RED / GREEN 证据

1. retry 进程控制异常：
   - RED：2 项分别失败，实际 `first_owner.done=False`，核心断言为 `AssertionError: False is not true`。
   - GREEN：`Ran 2 tests in 0.001s` / `OK`。
2. 真实探针与 UI `accountId`：
   - RED：4 个目标测试命中 `accountId` 缺失、runtime `0 != 31`、非法值未拒绝，结果 `FAILED (failures=5, errors=2)`。
   - GREEN：`Ran 4 tests in 0.145s` / `OK`。
3. 验证器 cleanup 白名单：
   - RED：`cleanup_unknown != cleanup_interrupted`，`Ran 1 test` / `FAILED (failures=1)`。
   - GREEN：`Ran 1 test` / `OK`。
4. UI 步骤提示：
   - RED：实际文案仍为“在同一抖音编辑页…”，目标语义缺失，`Ran 1 test` / `FAILED (failures=1)`。
   - GREEN：`Ran 1 test in 0.163s` / `OK`。

## 新鲜验证证据

- 协调器/探针/UI/验证器专项：`Ran 266 tests in 5.202s`，`OK`。
- 任务 1—8 相关回归：`Ran 445 tests in 13.944s`，`OK`。
- 完整 `unittest discover -q`：提交前最后一轮 `Ran 749 tests in 22.843s`，`OK`；相比 Base 的 742 项新增 7 项回归。
- `py_compile`：7 个变更 Python 模块/测试全部退出码 0。
- 离屏 UI：输出 `NATIVE_DESKTOP_UI_OK`。
- 验证器默认模式：未传 `--execute`，只输出 A/B/C 三代际计划，国内次数 `20/20/1`，结束集合 `normal/abandon/normal`。
- `git diff --check`：退出码 0。
- AST 安全自审：`FORBIDDEN_IMPORTS []`、`FORBIDDEN_CALLS []`、retry 仅存一个 `KeyboardInterrupt/SystemExit` 处理器且含原样 re-raise，`FORBIDDEN_BASE_HANDLERS []`。

## 变更文件

- `app_core/douyin_commerce_collectors.py`
- `app_core/douyin_commerce_probe.py`
- `ui/douyin_commerce_page.py`
- `tools/verify_douyin_commerce_collectors.py`
- `test_douyin_commerce_collectors.py`
- `test_douyin_commerce_probe.py`
- `test_douyin_commerce_service.py`
- `docs/DOUYIN_COMMERCE_WORKFLOW.md`
- `.superpowers/sdd/2026-08-09-douyin-commerce-isolated-collectors/final-review-fix-report.md`

## 剩余风险

- 进程控制异常回归使用确定性 fake manager；本轮证明的是协调器 owner/Future/队列终态，不是真实浏览器资源已在平台环境中关闭。
- `accountId` 只是本机整数身份；公开诊断只输出 `account-<id>`，但仍需保持现有账号文件、Cookie 和页面内容脱敏门不退化。
- 未经 Andy 当次明确授权，不得把本轮离线结果推断为真实采集、草稿、定时或发布成功。
