# Task 5 执行报告：抖音评论只读采集器

日期：2026-08-23

分支：feature/data-monitoring-v3

## 范围与事实边界

- 本轮只新增评论采集器、它的专项测试和本报告。
- 没有访问真实抖音页面，没有平台写入，没有创建生产合同清单。
- 测试只使用注入的已验证合同和假 Playwright 资源。
- 生产合同缺失时固定返回 comment_content_unavailable，不猜测路径、字段或选择器。

## 基线

实现前运行：

    .venv/bin/python -m unittest -v test_platform_data_comment_models test_platform_data_comment_contract test_douyin_data_collector

结果：Ran 94 tests in 0.470s / OK / 0 失败。

## RED 证据

### RED 1：采集器模块尚不存在

先完整写入命名行为测试，再运行：

    .venv/bin/python -m unittest -v test_douyin_comment_data_collector

精确结果：

    ModuleNotFoundError: No module named 'app_core.douyin_comment_data_collector'
    Ran 1 test in 0.000s
    FAILED (errors=1)

这个 RED 证明新测试在生产实现前无法通过。

### RED 2：抗取消内层任务没有进入清理计数

首批最小采集实现后，命名测试：

    test_cancellation_resistant_close_and_worker_cannot_hang_or_leak (mode='worker')

精确失败：

    AssertionError: 'comment_login_required' != 'comment_sync_cancelled'
    Ran 22 tests in 0.217s
    FAILED (failures=1)

原因：响应 worker 内部的 response.json() 任务可以在 worker 被取消后继续存活，但首版没有将它纳入同一清理回执。

## GREEN 证据

Parser 定向运行：

    .venv/bin/python -m unittest -v test_douyin_comment_data_collector.DouyinCommentParserTests

结果：Ran 5 tests in 0.001s / OK。

生命周期缺口修复后，定向运行：

    .venv/bin/python -m unittest -v test_douyin_comment_data_collector.DouyinCommentCollectorTests.test_cancellation_resistant_close_and_worker_cannot_hang_or_leak

结果：Ran 1 test in 0.025s / OK。

Task 5 专项运行：

    .venv/bin/python -m unittest -v test_douyin_comment_data_collector

结果：Ran 22 tests in 0.225s / OK。

## 实现结果

- 仅在加载到已验证合同后启动短会话。
- 先安装响应监听，再导航到合同提供的评论页模板。
- 只接受精确 HTTPS authority、路径和 HTTP 方法匹配的响应。
- 翻页只使用合同中的 click 或 scroll 官方 UI 动作；代码中没有评论请求合成或重放。
- 只保留一级评论；正文、点赞数、回复数、评论时间和作品归属按合同校验。
- 平台评论 ID 读取后立即转换为 SHA-256 匿名键；作者对象、原始 ID、原始行和响应不进入批次或报告。
- 坏记录逐条拒绝；非终页没有一条有效一级评论时固定失败。
- 批次内同 ID 只保留一次；冲突重复、游标环、翻页控件缺失全部失败关闭。
- 遇到第一条已知评论时，该评论返回一次，然后在触发下一页前停止。
- 100 条精确硬截断为 limit_reached 加 comment_limit_reached。
- 登录、验证、无权限、超时、载荷异常和清理失败只暴露固定错误码。
- CancelledError、KeyboardInterrupt 和 SystemExit 均在所有关闭尝试后保留原语义。
- 所有启动、导航、响应任务和四层关闭共用一个总时限；监听、Future、worker 和内层操作任务均被消费或计入清理失败。

## 自审与风险

已检查：

- 生产源码中没有测试使用的 /verified 路径、data.comments 字段或 verified-comment-load-more 选择器。
- 不存在 requests、fetch 或 XHR 等评论请求入口。
- 成功、载荷失败、登录失效、清理失败和进程控制退出都有四层关闭断言。
- 抗取消关闭和抗取消响应 worker 均在墙钟上限内返回，无 traceback、Task was destroyed 或本机路径输出。

接口张力：

CommentCollectionBatch 共享模型只允许在精确 100 条时使用 limit_reached 和 comment_limit_reached，但采集入口允许内建整数 1 到 100。本任务锁定：

- limit 等于 100 且未读到平台结束：limit_reached 加 comment_limit_reached。
- limit 小于 100 且主动截断：stop_reason 和 warning_code 都为空，不伪报 100 条硬上限。

低限批次是内部或测试截断状态。下一层服务不得把空 stop_reason 解读为“平台已完整同步”。本轮按范围要求不修改共享模型，留待 Task 6 审查。

仍未验证：

当前不存在生产合同清单。因此本轮只证明本地合同、隐私和资源生命周期，不代表真实抖音评论已经可用。

## 文件

- app_core/douyin_comment_data_collector.py
- test_douyin_comment_data_collector.py
- .superpowers/sdd/2026-08-23-douyin-comment-insight/task-5-report.md

## 最终验证

最终按要求只运行受影响测试，没有运行全仓测试：

    .venv/bin/python -m unittest -v test_douyin_comment_data_collector test_platform_data_comment_models test_platform_data_comment_contract test_douyin_data_collector

结果：Ran 116 tests in 0.684s / OK / 0 失败。

静态检查：

- py_compile：实现和测试均通过。
- git diff --check：通过。
- 数据支线范围门禁：stream=data、base=origin/main、changed=19，scope-check: OK。
