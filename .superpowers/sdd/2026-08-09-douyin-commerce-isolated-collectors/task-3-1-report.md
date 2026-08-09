# Task 3.1 实施报告：进程控制异常下关闭所有权终态

## 结果与提交

- 状态：已完成。
- Base SHA：`6fab1cd14e1c5cc2540b6d15a2864487a922dafa`。
- 实现提交 SHA：`84eb403`（完整 SHA 可由 `git rev-parse 84eb403` 回读）。
- 实现提交信息：`修复进程控制异常下的关闭终态`。
- 实现只修改 `app_core/douyin_commerce_collectors.py` 与 `test_douyin_commerce_collectors.py`；本报告为简报要求的交付文件。
- 未修改 `DouyinCommerceSessionManager.close_strict()`、UI、批量发布器、DOM、探针或正式发布流程，未访问真实平台。

## TDD RED / GREEN

### RED

先新增两个独立确定性用例：

- `test_keyboard_interrupt_close_completes_owner_and_retries`
- `test_system_exit_close_completes_owner_and_retries`

生产改动前分别运行，两条均按预期失败：首次公开 close 实际得到 `cleanup_incomplete`，而契约要求 `cleanup_interrupted`。每条旧实现测试耗时约 30 秒，因为首次 close 与清理用 close 都耗尽 15 秒等待窗，直接证明 collector close owner 没有进入终态。

失败核心断言：

```text
AssertionError: 'cleanup_incomplete' != 'cleanup_interrupted'
```

### GREEN

最小实现后，两条目标测试一次运行结果：

```text
Ran 2 tests in 0.001s
OK
```

测试同时覆盖：

- 首次 close 返回 `closed=False`、`aliveCollectorCount=1` 和固定 `cleanup_interrupted`。
- 公开结果不包含测试异常中的 Cookie、验证码或 HTML 字面量。
- 第一个 close owner 的 `done` 已置位，结果为 `cleanup_interrupted`。
- manager identity 的活动 owner 注册已释放。
- 同一 action queue 在异常 Future 后仍能执行下一项 cleanup 任务并返回 `queue_continues`。
- 第二次 close 声明不同的新 owner，再次调用 strict close 后返回 `closed=True`、`aliveCollectorCount=0`。

## 终态设计

严格关闭仍由 action queue 的单工作线程执行。协调器 `_run_generation_collector_close()` 只显式处理 `KeyboardInterrupt` 与 `SystemExit`：

1. 在状态锁内调用既有 `_complete_collector_close_locked()`。
2. 写入固定结果 `cleanup_interrupted`，将对应 slot 标为失败但不移除 collector/runtime。
3. 完成 owner，并通过既有释放逻辑移除 manager identity 的活动 owner 注册。
4. 原样重新抛出进程控制异常，让 executor Future 保留异常语义。

该边界不捕获其他 `BaseException`，也不修改资源层。普通 `Exception` 仍映射为 `cleanup_incomplete`；资源层对 `asyncio.CancelledError` 的逐资源失败记账和对 `KeyboardInterrupt/SystemExit` 的逃逸契约保持不变。

## 验证结果

所有命令均使用主项目解释器：

`/Users/andy/Documents/Codex/2026-07-28/new-chat/outputs/一键发桌面UI基座/.venv/bin/python`

### 目标与窄相关测试

- Task 3.1 两条目标测试：`Ran 2 tests`，`OK`。
- 普通 strict close 失败、cleanup 重试、资源层 CancelledError、资源层进程控制异常等窄相关测试：`Ran 7 tests`，`OK`。

### 协调器连续 5 轮

命令：

```bash
QT_QPA_PLATFORM=offscreen /Users/andy/Documents/Codex/2026-07-28/new-chat/outputs/一键发桌面UI基座/.venv/bin/python -m unittest -q test_douyin_commerce_collectors
```

五轮均为 `Ran 57 tests` / `OK`，耗时分别为 `0.932s`、`0.966s`、`0.969s`、`0.976s`、`0.950s`。

### Task 1 / Task 2 / Task 3 相关回归

```bash
QT_QPA_PLATFORM=offscreen /Users/andy/Documents/Codex/2026-07-28/new-chat/outputs/一键发桌面UI基座/.venv/bin/python -m unittest -v test_douyin_commerce_collectors test_douyin_commerce_setup_state test_douyin_commerce_probe
```

结果：`Ran 69 tests in 0.941s`，`OK`。

### SessionContractTests 整类

```bash
QT_QPA_PLATFORM=offscreen /Users/andy/Documents/Codex/2026-07-28/new-chat/outputs/一键发桌面UI基座/.venv/bin/python -m unittest -q test_douyin_commerce_service.DouyinCommerceSessionContractTests
```

结果：`Ran 45 tests in 1.489s`，`OK`。

### 完整 Qt offscreen 回归

```bash
QT_QPA_PLATFORM=offscreen /Users/andy/Documents/Codex/2026-07-28/new-chat/outputs/一键发桌面UI基座/.venv/bin/python -m unittest discover -v
```

结果：`Ran 660 tests in 21.010s`，`OK`。

### 语法与差异检查

```bash
/Users/andy/Documents/Codex/2026-07-28/new-chat/outputs/一键发桌面UI基座/.venv/bin/python -m py_compile app_core/douyin_commerce_collectors.py test_douyin_commerce_collectors.py
git diff --check
```

结果：均退出码 0，无语法错误或空白差异。

## 自审风险

- `cleanup_interrupted` 是新增公开固定结果；当前 closed 判定仍首先要求 `aliveCollectorCount == 0`，因此被中断 collector 保留时不会误报关闭成功，后续成功重试会覆盖为 `closed`。
- 终态化发生在重新抛出之前，避免 close waiter 超时和 manager identity 长期占用；测试显式检查旧 owner、新 owner与队列续跑。
- 异常原文只存在于本地测试替身输入，用于证明公开结果脱敏；生产实现没有记录异常、堆栈或 cause。
- 本轮证据仅证明本地协调器、队列和资源关闭契约，不代表真实平台登录、上传、草稿、定时或公开发布成功。
