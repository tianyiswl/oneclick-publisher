# Task 4 完成报告：抖音带货串行批量执行器

## 实现

- 新增 `DouyinCommerceBatchExecutor`：每条视频严格按上传、内容同步、收藏音乐、声明、地点精确匹配与写入、定时同步、预检、最终提交、关闭会话的顺序串行执行；任意时刻最多一个编辑会话。
- `run_preflight()` 始终使用 `runtimeMode=preflight` 与 `debugDryRun=true`，不调用提交；`run_publish()` 需要显式 `confirmed=true`，且仅最终提交临时切换为发布模式。
- 单条一般失败记录失败后继续下一条；登录、二维码、短信验证码、风控、合规或未知验证进入 `waiting_verification`，停止后续条目。验证挑战仅在内存携带条目序号与标签。
- 成功状态不再存在可公开传入字典的写入桥接；只有同一执行器的最终 `submit` 平台回读可调用内部回执写入器。
- 回执写入器同时严格校验 `publishedAt` 与 `scheduleTime` 为 `YYYY-MM-DD HH:MM`，并要求回读时区为 `Asia/Shanghai`。
- 批量载荷保留所选收藏音乐模式，避免被旧版默认规则覆盖。

## 验证

- 先写入并运行失败测试，确认执行器缺失后再实现。
- 完整离线回归：`test_douyin_commerce_batch_executor test_douyin_verification test_douyin_commerce_service test_douyin_publish_executor test_task_service`，共 208 项通过。
- 静态校验：`git diff --check` 与关键模块 `compileall` 通过。

## 安全边界

- 本任务没有启动浏览器、登录账号、上传素材、保存草稿或发布内容。
- 验证码、二维码与会话敏感数据不进入任务事件或长期存储。

## 审查修复：登录暂停与验证状态边界

- 会话管理器在 `start_upload()` 明确返回 `needs_login`、`login_required`、`requires_login` 或 `login_expired` 时，批量执行器现在立即将当前条记为等待人工重新登录、停止整批；不会把它误判为“缺少会话”后继续下一条。
- `waiting_verification` 现在只在同一进程验证代理仍持有 `waiting`/`processing` 请求时成立。验证码已取消、失败或等待超时后，当前条会记为 `verification_failed`，整批安全停止，后续条保持待处理；不会伪装成可恢复验证。
- 真正 active 的短信验证保留同一编辑会话与验证代理请求。用户验证码成功后，从同一 `submit` 调用继续；离线替身验证了当前视频只提交一次，再继续后续视频。
- 离线回归：`test_douyin_commerce_batch_executor test_douyin_verification test_douyin_commerce_service test_douyin_publish_executor test_task_service` 共 212 项通过；`compileall` 与 `git diff --check` 通过。
