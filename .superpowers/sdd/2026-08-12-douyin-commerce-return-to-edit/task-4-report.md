# Task 4 实施报告：检查页返回修改状态机与严格关闭屏障

## 实施范围

- 批量启动时分别保存运行中任务 ID 与结果任务 ID；批量 worker 完成并清理验证码内存后，只读调用 `prepare_douyin_batch_revision()` 投影返回修改资格。
- 检查页仅在服务层明确返回可修订且页面空闲时显示“返回修改未完成视频”；全部成功、待核对、登录／验证、系统暂停、运行中及其他不可修订状态不提供该入口。
- 点击入口时再次读取来源任务，不信任页面缓存；来源任务状态变化、返回计划损坏或任务号不一致时固定脱敏并留在结果页。
- 返回使用独立 `douyin_commerce_batch_revision` runner key。关闭 worker 先消除旧批量／setup worker 竞态，再关闭 collector generation 与正式会话；回调仅接受 `closed is True`、`aliveCollectorCount` 和 `aliveSessionCount` 均为内建整数 `0`，并要求 session 状态为 inactive。
- 关闭失败不跳页、不载入草稿、不覆盖结果任务。成功后清理验证内存并调用 Task 3 `_apply_batch_revision_plan()`。
- token、专用 runner key 与 shutdown 取消／等待共同阻止双击、旧回调和退出竞态；真实工作流边界同时清空旧结果任务、资格与旧 token。
- 再次确认创建新任务时传入 `revision_source_task_id`。创建异常使用固定说明，保留修订来源、已选视频和所有编辑控件；原任务没有写入或改动。

## TDD 证据

本任务按单项 RED→最小 GREEN 推进：

1. 按钮资格 RED：可修订任务仍显示“开始新内容”；GREEN 后按服务层资格显示返回修改入口，不可修订任务保持“开始新内容”。
2. 结束投影 RED：worker 完成后未调用修订准备接口且丢失任务 ID；GREEN 后先清验证内存，再用保留的任务 ID 只读投影资格。
3. 返回关闭 RED：缺少返回方法与关闭屏障；GREEN 后点击重读任务、后台关闭、严格成功才载入计划，关闭不完整保持结果页。
4. 严格类型变异 RED：把计数校验临时放宽为数值相等后，`False` 和 `0.0` 绕过屏障并错误载入计划；恢复内建 `int` 校验后 GREEN。
5. worker 竞态 RED：旧批量 worker 未停止就先关闭 collector；GREEN 后按 `cancel_pending` → `wait_for_finished` → collector 顺序执行。
6. 双击变异 RED：临时移除专用 runner key 的 busy 投影后，第二次点击重复读取来源任务；恢复专用 key 后只排队一个关闭代际。
7. shutdown RED：退出未取消排队的返回 worker，旧 worker 稍后仍会关闭资源；GREEN 后 shutdown 先使 token 失效并取消／等待专用任务，迟到任务不再执行。
8. 迟到回调变异 RED：临时移除 token 比较后，旧成功回调清理当前验证状态并应用旧计划；恢复 token 后旧回调无副作用。
9. 新任务来源 RED：创建调用缺少 `revision_source_task_id`，普通异常直接逃出；GREEN 后来源 ID 贯穿，异常固定脱敏且编辑态保持不变。
10. 工作流清理 RED：开始新内容、放弃或完成时旧结果任务与资格仍残留；GREEN 后来源、媒体锁、结果 ID、资格和 token 同步失效。

## 最终专项验证

使用主项目虚拟环境、`QT_QPA_PLATFORM=offscreen`，仅运行 Task 4 的 13 个命名测试：

```text
Ran 13 tests in 0.630s
OK
```

退出码为 `0`。`ui/douyin_commerce_page.py` 与 `test_douyin_commerce_service.py` 的 `py_compile` 通过，`git diff --check` 通过。

## 文件与边界

- 修改：`ui/douyin_commerce_page.py`
- 修改：`test_douyin_commerce_service.py`
- 新增：`.superpowers/sdd/2026-08-12-douyin-commerce-return-to-edit/task-4-report.md`
- 未修改：`progress.md`
- 未实施：Task 5 及后续任务

本轮没有运行全量测试、真实客户端或浏览器，没有读取账号、登录、上传、预检、保存平台草稿、提交或发布。上述证据只证明 Task 4 离线状态机与关闭边界通过，不构成平台成功证据。

## 剩余风险

- 修订资格最终依赖 Task 2 的任务存储状态和只读计划；真实平台状态仍必须由原任务回执确认，不能由 UI 推测。
- 本任务未做真实平台 DOM 或会话关闭验收；平台页面变化风险保持“未验证”，不能标记为通过。
- 按任务限制未运行相关模块或完整离线回归，留待总集成任务统一验证。

## 修复轮 1：三项 Important

### 修复结果

1. 严格关闭不再以页面 `_session_id` 是否为空作为 manager 核验条件。返回修改 worker 会无条件读取真实 manager 状态；manager 仍 active 时，以页面 ID 或 `None` 调用 `close_strict()`，随后严格要求 `closed is True`、`aliveSessionCount` 为内建整数 `0`，并二次回读 `active=false`。任何类型放宽、异常或仍存活都留在结果页。
2. 修订恢复改为先只读规范化整份草稿，再唯一校验账号以及每条未完成素材的本机映射；任一条不可恢复时不清选择、不覆盖内容、不改来源媒体锁、不切页、不清返回资格，同一结果可重试。全部校验与控件投射完成后才切到编辑页并清资格。
3. `prepare_douyin_batch_revision()` 现在沿完整 `revisionSourceTaskId` 祖先链汇总成功媒体键，因此第二、三代仍锁住更早代已经成功的视频。祖先任务缺失、状态或快照损坏、媒体身份不稳定、断链或成环统一固定拒绝为“来源任务的修改链无法确认”，且不返回草稿；全程只读，不改原任务。

### RED → GREEN 证据

- strict close 单项：RED 时页面 ID 为空导致 `close_strict(None)` 从未调用；GREEN 后合法 manager 关闭通过，`closed=1`、`aliveSessionCount=False`、关闭后仍 active 三类均拒绝。
- 原子恢复单项：RED 时不可映射素材先清空原选择并提前清资格；GREEN 后结果页原选择、文案、来源、媒体锁与资格完整保留，随后使用有效计划重试成功。
- 多代链单项：RED 时第三代 `successfulMediaKeys` 为空；GREEN 后得到第二代和第一代成功键，并对真实临时 SQLite 中制造的环与断链固定拒绝。

三个新增单项合跑：

```text
Ran 3 tests in 0.223s
OK
```

Task 4 专项仅运行一次，共 16 个命名测试：

```text
Ran 16 tests in 0.717s
OK
```

另执行 `git diff --check`，退出码为 `0`。本修复轮仍未运行全量测试、真实客户端、浏览器、账号或平台；未修改 `progress.md`，未实施 Task 5 及后续内容。公开错误均为固定脱敏文案。

### 修复轮剩余风险

- manager 替身验证了真实状态转换与严格类型语义，但没有启动真实浏览器会话；真实平台关闭仍属于后续集成验收范围。
- 原子恢复的失败覆盖输入规范化、账号／素材不可映射等业务失败；Qt 控件自身发生进程级异常不属于可恢复业务失败。
- 祖先链按来源任务不可变前提只读遍历；若数据库被外部工具破坏，将固定拒绝而不会猜测修复。
