# TikTok 定时内容回读刷新纠错报告

日期：2026-08-29（Asia/Shanghai）

## 结论

已补足定时提交接受并写入 `scheduled_accepted` 检查点之后的只读内容列表刷新。读回现在会在保留同一隔离内容列表页的前提下，至少重新导航一次受限 TikTok Studio 内容路由，再按既有基线差异、页面账号和行账号身份、完整文案哈希、排期和观察窗口进行唯一匹配。

## 根因确认

`capture_scheduled_content_baseline()` 在最终 Schedule 点击前载入并缓存内容列表页。原先 `readback_scheduled_content()` 再调用 `_load_scheduled_content_page()` 时会命中 `_readback_route_loaded` 快路径，直接使用旧 DOM；真实平台若仅在新的内容列表读取后展示服务器创建的行，就会在 120 秒观察窗口内错误保留为 `tiktok_schedule_outcome_unknown`。

## RED → GREEN 证据

RED 命令：

```bash
.venv/bin/python -m unittest -v test_overseas_tiktok_schedule_form.TikTokScheduleFormTests.test_readback_refreshes_after_baseline_and_accepts_only_new_exact_row
```

未修改生产代码时，该测试以 `AssertionError: unexpectedly None` 失败。假页先为基线暴露一个旧的精确行，并只在下一次内容列表导航后暴露唯一新行；失败原因是读回没有第二次导航，而不是假页缺失方法或测试拼写问题。

GREEN 命令：

```bash
.venv/bin/python -m unittest -v test_overseas_tiktok_schedule_form.TikTokScheduleFormTests.test_readback_refreshes_after_baseline_and_accepts_only_new_exact_row
```

生产修复后，该单项测试通过。

## 变更

- `uploader/tk_uploader/schedule_form.py`：为内容列表加载器增加私有 `refresh` 选项；仅 `readback_scheduled_content()` 请求一次刷新，基线捕获仍使用冻结前的既有加载路径。
- `test_overseas_tiktok_schedule_form.py`：新增行为测试，证明旧基线行不会被接受、仅刷新后出现的唯一新行会被接受、第二次内容列表导航发生，且所有行操作点击计数都为零。

没有修改 deferred minors、任务状态、授权、会话、版本、打包、合并或推送行为。

## 验证

- 定向模块：`.venv/bin/python -m unittest -v test_overseas_tiktok_schedule_form test_overseas_video_publish`，`87` 项通过。
  - 其中已有 `test_scheduled_submit_clicks_schedule_once_and_never_post` 继续证明最终 `Schedule` 仅点击一次，且不解析或点击 `Post`。
- 全仓离线发现：`.venv/bin/python -m unittest discover -p 'test_*.py'`，`2628` 项通过，耗时 `79.965s`。
- `git diff --check`：通过，无输出。
- 工作线范围：`.venv/bin/python tools/check_workstream_scope.py --stream overseas --base a9d851c6e7174611b2aed044686810576b38ee54` 返回 `scope-check: OK`；生产和测试修改均为海外支线拥有文件。
- 本报告位于项目刻意忽略的 `.superpowers/sdd/` 审计目录；按报告提交合同强制暂存后，范围检查只将该文档列为 `outside` / `REVIEW_REQUIRED`，不涉及任何生产或测试文件。

## 自检与安全结论

- 新刷新只对已经创建的隔离内容列表页执行受限 `goto`；不会进入上传/编辑页，不会创建额外最终动作。
- 基线 `baseline_row_keys`、页面账号/行账号唯一身份检查、零或多条精确新行拒绝逻辑均未改变。
- 刷新加载失败或观察窗口内没有唯一新行时，读回仍返回 `None`，调用方仍转为既有的 `tiktok_schedule_outcome_unknown`；没有重试 Schedule。
- 测试明确保证读回不点击 edit、publish、retry 或 delete；定向集成测试保证 Schedule 仅一次。
- 本纠错仅运行离线测试。没有打开 TikTok 或可见浏览器、上传媒体、点击真实 Post/Schedule、发布、打包、升级版本、合并或推送；没有任何外部平台动作。

## 提交与关注点

生产与测试修复提交：`0a55c60407265ee60bbe1cacbcb0070bbcb9bb32`（`fix(tiktok): refresh scheduled content readback`）。

关注点：本轮证明的是离线语义和刷新调用边界，尚不构成真实 TikTok 平台回读验证；真实平台定时提交仍需另行、明确授权后验证。
