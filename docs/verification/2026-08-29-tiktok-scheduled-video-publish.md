# TikTok 平台原生定时发布验证

## 本地证据

- 最终复审修复实现提交：海外专属运行时与测试 `7151f17e77500b53c287fd4ba68f40a37b85808e`，共享核心与测试 `41971c59213fed2f17392289eb61dfd61e870caf`，海外会话边界集成回归 `6d11216e960ef68ed78e618f81ef8c331f7cf5f6`。
- 严格 TDD：`5` 次 RED 命令共运行 `20` 个顶层测试，得到预期的 `9` 个 failure 与 `12` 个 subtest error；对应 `5` 次 GREEN 命令 `29/29` 通过，另有 receipt-only 不可逆证据确认 `2/2` 通过。逐命令证据见 `.superpowers/sdd/2026-08-29-tiktok-scheduled-video-publish/final-fix-report.md`。
- 架构图：Architecture 和 Lifecycle 的最终 Archify `validate` / `deliver` 均为 `9/9`、`0` errors、`0` warnings。Architecture 固定到实现基线 `6d11216e960ef68ed78e618f81ef8c331f7cf5f6` 并验证 `29` 个源码引用，spec/artifact SHA256 分别为 `2ef5bc8a76191f9c7b74c36f52715de38806e79606e6e44d96a7adf43c576812` / `5de8bf62b6dbd1d9b6a4563db5e223083c483e87e7af58f4ff976b6f552b329b`。Lifecycle spec/artifact SHA256 分别为 `8e1ad65fc6569901e855384624f01179b1216776e87dbda04fa73c3f526e550b` / `b0a9ec122b8c471b330b58703fbef11a6d9bb94389cb5ec18aa859a9db0b07b1`。
- 两图最终 `visual-check` 在 `1440×900`、`1600×1000`、`1920×1080`、`2048×1320` 均无横向或纵向溢出。已人工检查两图最小/最大尺寸的明暗主题截图，未见裁切、穿越无关节点、歧义走廊或不可读文字；Architecture 修正轮次 `0`，Lifecycle 修正轮次 `2`。
- 海外受影响测试：`.venv/bin/python -m unittest test_account_detection_ui test_controlled_publish test_oneclick_authorization test_oneclick_capabilities test_overseas_integration test_overseas_publish_routing test_overseas_tiktok_identity test_overseas_tiktok_profile test_overseas_tiktok_publish test_tiktok_schedule_contract test_overseas_tiktok_schedule_form test_overseas_tiktok_session_scope test_overseas_tiktok_system_login test_overseas_video_publish test_task_service test_tiktok_login_startup_wiring`，`528/528` 通过，用时 `2.935s`，进程返回值 `0`。
- 完整回归：`.venv/bin/python -m unittest discover -p 'test_*.py'`，`2627/2627` 通过，用时 `83.195s`，进程返回值 `0`。
- 离屏客户端：`QT_QPA_PLATFORM=offscreen .venv/bin/python desktop_native_app.py --ui-test` 返回 `NATIVE_DESKTOP_UI_OK`，进程返回值 `0`。
- 差异检查：`git diff --check origin/main...HEAD` 和 `git diff --check` 均返回 `0`，输出字节数均为 `0`。
- 范围检查：`.venv/bin/python tools/check_workstream_scope.py --stream overseas --base origin/main` 返回 `2 / REVIEW_REQUIRED`；`changed=63`，其中 `owned=39`、`shared=24`、`outside=0`。TikTok 实现、架构、规格、计划和验证路径均为 `owned`，核心协调文件为待集成线复审的 `shared`。
- 最终复审阻断修复覆盖：定时结果不明、受理、回读与脱敏最终动作回执的统一不可逆证据保护；真实页面与列表行账号身份独立核对；点击前稳定列表基线及唯一新排期；发布加载/刷新全程 TikTok-only 会话；定时公开状态投影；Schedule 后通用异常归一化；CRLF/CR 正文换行统一。未修改 `10` 个延期 Minor，也未修改登录取消 Minor。

## Task 9 修复与最终本地证据

- 针对真实表单核验暴露的启动合同、任务终态、定时控件漂移和终态 claim 遗留问题，已完成窄范围修复并经过独立复审。当前实现只识别精确的 `Schedule` / `定时发布` / `排期` 设置控件，最终 `Schedule` / `Post` 动作选择器保持隔离；跨 frame 重挂载、候选枚举瞬态和空句柄均不会把残缺观察误判为稳定控件。
- 最终聚焦发布链测试 `350/350` 通过，用时 `15.974s`；海外受影响链 `563/563` 通过，用时 `17.334s`；证据写回后再次完整发现 `2666/2666` 通过，用时 `92.723s`。
- 离屏客户端返回 `NATIVE_DESKTOP_UI_OK`；`git diff --check origin/main...HEAD` 和 `git diff --check` 均无输出。
- 范围检查为 `changed=66`、`owned=39`、`shared=24`、`outside=3`。三个 outside 文件均为本任务的 SDD 进度或审查报告，没有生产源码或测试越界；因此状态仍为待集成线复审的 `REVIEW_REQUIRED`，不是可由功能分支自行合并的证明。

## 真实平台表单核验

- 账号：`13`（显示主体 `墨白 | Mobai`，账号引用 `tianyiswl`）。输入为 `runtime/tiktok-form-check-20260829/manifest.json`，SHA256 `97c4b4eac24f56b7323acff9482d59a05941bc23556ff90d4099062ce1459623`；单个测试视频 SHA256 `014da188195cae32cc3184c3e663359a0e1b0884985f9d23089fbf02ac6d4cc6`；目标时间为 `2026-08-30 10:00 Asia/Shanghai`。
- 任务 `79 / T08291923-788D` 在打开浏览器前暴露根层排期快照合同冲突；修复后该失败任务已安全终态化并只清理其可逆 claim，没有平台动作。
- 任务 `80 / T08292003-B4F1` 已在上传控件选择五秒测试视频文件，并回读正文、官方话题实体和公开范围，但旧控件探测在定时入口前返回 `tiktok_schedule_unavailable`。现有事件只证明文件已选择，不证明平台已完成视频解析或上传回读。没有点击最终动作；该终态表单核验的可逆 claim 已精确释放。
- 完成全部控件稳定性修复后，任务 `81 / T08292139-F870` 再次使用完全相同的账号、内容哈希和目标时间。上传控件选择了同一测试视频文件，正文、官方话题与公开范围回读成功；完整加载后的发布范围内仍为 `scopes=1, candidates=0, usable=0`，最终返回稳定错误码 `tiktok_schedule_unavailable`。现有证据不把文件选择表述为视频上传完成或视频页面回读。
- 任务 `81` 没有点击 `Schedule` 或 `Post`，没有平台受理、作品 ID、URL 或排期回执，不是已定时也不是已发布；终态 claim 已自动释放，浏览器已关闭。

## 当前结论

- 代码已能安全区分“定时设置控件存在”和“当前页面没有该能力”，不会把最终发布按钮当成设置入口，也不会因失败遗留运行锁。
- 当前账号的真实上传页没有展示可用的定时设置控件。结合 TikTok 官方对 Post Scheduler 权限层级和地区/资格差异的说明，最可能是当前账号未获得或未展示 Scheduler 权限；这是基于页面与官方规则的判断，不是账号后台权限页的直接回读。
- 尚未验证 TikTok 对正式 `Schedule` 的受理与定时列表回读。下一次真实核验应使用已在 TikTok Studio 明确显示 Scheduler 的账号，或先为当前账号开通/连接相应高级权限；不得通过重复上传来猜测。
- 本轮未打包、升级版本、合并或推送。
