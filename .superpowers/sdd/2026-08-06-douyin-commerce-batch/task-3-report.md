# 任务 3 报告：逐视频任务记录与平台回执状态

## 已完成

- `publish_task_items` 增加 `batchItemIndex`、`locationSummary`、`scheduleSummary` 迁移列。
- 新增 `create_douyin_batch_task()`：将批次信封拆为每视频一个内部单视频载荷及一个独立任务项，保留视频文件名、完整地点地址和立即发布/北京时间定时摘要。
- 批量任务对外显示为“抖音带货批量”；内部单视频载荷仍保持 `workflow=douyin-commerce`。
- 新增 `mark_batch_item_result()`：按 `itemId` 写入事件与回读；`editor_written`、`verification_waiting`、`preflight_readback` 保持 `running`，仅 `platform_publish_receipt` 与 `platform_scheduled_receipt` 使该视频成为 `success`。失败仅影响当前视频，其他视频保持原状态。

## 测试

初始要求的 `.venv/bin/python` 不存在；已按 `requirements-oneclick.txt` 建立本地 `.venv` 后执行：

```bash
.venv/bin/python -m unittest -v test_task_service test_douyin_commerce_service
```

结果：通过，153 项测试全部成功。

## 平台边界

未打开浏览器、未登录、未上传、未创建草稿、未提交或发布到抖音；本任务只处理本地 SQLite 任务记录与回执状态机。

## 修复轮 1

- `success` 现在要求最终事件和必要的、已白名单化的回执字段同时满足：发布回执须有 `platformPostId` 或 `postUrl`，定时回执须有 `scheduleTime`。字段不足时条目保持 `running`。
- 事件 `detailJson` 只可保存 `platformPostId`、`postUrl`、`publishedAt`、`scheduleTime`；Cookie、Token、会话和账号等字段一律不落库。
- 成功条目收到普通编辑、验证或预检事件时维持 `success`，事件仅作审计追加。
- 新增 4 项覆盖测试；执行 `.venv/bin/python -m unittest -v test_task_service test_douyin_commerce_service`，157 项全部通过。

## 修复轮 2

- 最终回执还必须携带模块私有的受控来源哨兵；仅伪造 `event_type` 或任意字符串来源不能触发成功。
- 白名单回执字段只接受非空字符串；布尔值、数值和其他伪值不会入库或满足成功条件。
- 定时回执的 `scheduleTime` 必须严格符合北京时间 `YYYY-MM-DD HH:MM` 且可被日期时间解析。
- 新增来源伪造、定时缺失/无效值、布尔值和数值身份字段覆盖；执行 `.venv/bin/python -m unittest -v test_task_service test_douyin_commerce_service`，159 项全部通过。

## 修复轮 3

- `mark_batch_item_result()` 不再接受来源参数，也永远不能将条目写为 `success`；它只记录进展或失败。
- 成功只可经批量执行器内部的 `_build_controlled_batch_receipt()` 与 `_mark_controlled_batch_receipt()` 路径回填，可信回执对象不属于公开调用契约。
- 受控回执强制 `timezone=Asia/Shanghai`；定时回执使用严格北京时间格式并将该时区一同写入审计数据。
- 已覆盖公开伪造调用保持 `running`，以及受控执行入口有效定时回执成功并持久化时区；执行 `.venv/bin/python -m unittest -q test_task_service test_douyin_commerce_service`，160 项全部通过。

## 修复轮 4

- 从 `task_service` 删除可由调用者构造的 `_ControlledBatchReceipt`、回执构造器与成功回填函数；公开 `mark_batch_item_result()` 及其底层写入路径现在只具备 `running/failed` 能力。
- 新增内部 `_douyin_commerce_batch_receipt_writer`，未来只由批量执行器在取得平台最终回读后导入。最终事件白名单、必要字段、字段类型、未知字段、严格日期、参数时区及回执内时区均在写库函数内重新验证；不依赖哨兵或 dataclass。
- 封堵通用 `mark_platform_result()` 对批量任务的旁路：批量任务必须逐视频走批量执行器回填，通用接口不能批量写成成功。
- 覆盖手工对象、伪造字典、普通公开接口、通用平台接口、UTC、伪造相等比较的时区对象、回执内嵌 UTC、缺字段、非法字段/类型/事件与无效日期均不能成功；合法发布和定时回执可成功并持久化 `Asia/Shanghai`。
- 未打开浏览器、未登录、未上传、未创建草稿、未提交或发布到抖音；本轮只重构本地任务状态与最终回执写入边界。
- 执行 `.venv/bin/python -m unittest -v test_task_service test_douyin_commerce_service`，164 项全部通过。

## 修复轮 5

- 封堵历史任务工具的成功旁路：`utils.publish_tasks.mark_items`、`mark_platform_results`、`complete_task` 在载荷含 `batchWorkflow=douyin-commerce-batch` 时会在写库前拒绝成功状态；失败和进展事件仍可记录。
- 旧 `publish_runtime.execute_single_publish` 与 `execute_batch_publish` 在任何账号检查、浏览器或上传动作前拒绝批量载荷；通用 `publish_service` 也不会再把批量内部载荷创建为可由通用接口完成的任务。`task_service.mark_platform_result` 同时按任务载荷识别批量，避免缺失 `batchItemIndex` 的通用创建路径绕过。
- 新增最小 `douyin_commerce_batch_executor.write_verified_platform_result()` 作为未来串行批量执行器到内部回执写入器的唯一桥接；它只消费最终会话结果，当前不启动浏览器、不上传、不提交。内部写入函数改为模块私有。
- 定时与立即发表回执均强制要求回执内明确 `timezone=Asia/Shanghai` 和合法 `YYYY-MM-DD HH:MM` 日期时间：定时须 `scheduleTime`，发表须 `platformPostId` 或 `postUrl` 加 `publishedAt`。缺失时区或由调用方补写时区均不能记为成功。
- 新增分层离线覆盖：三种旧成功写入器、两个旧发布运行时入口、通用桌面发布服务、缺失回执时区、非法发表日期、以及合法的批量执行器桥接回执。

### 测试

```bash
/Users/andy/Documents/Codex/2026-07-28/new-chat/outputs/一键发桌面UI基座/.venv/bin/python -m unittest -q test_task_service test_douyin_commerce_batch_service test_douyin_commerce_service test_douyin_publish_executor
```

结果：194 项通过。

### 遗留边界

- 本轮没有启动真实浏览器、登录、上传、草稿或发布。
- 完整的串行上传/预检/验证暂停/继续循环仍由 Task 4 实现；它必须从真实会话回读中提供上述日期时间与时区字段，字段不足时保持非成功状态。
