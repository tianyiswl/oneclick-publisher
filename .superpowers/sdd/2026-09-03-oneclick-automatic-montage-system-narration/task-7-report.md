# Task 7 报告：项目边界、回归与人工听审停点

日期：2026-09-03

状态：纯净源码快照与隔离 Mac 中性素材批次机器验收通过；人工听审待大帅确认；Windows 仍只有适配器合同测试。

> Fix round 1 审计说明：本报告前半部分保留初始轮次的原始记录以便追溯，但其中混合工作树上的 `77/77`、`3175/3175`、`NATIVE_DESKTOP_UI_OK`、源码启动，以及写入正式用户数据目录并混用两个内容来源的批次 `M0903160612-1765F3`，均已撤销为验收证据。当前结论只以文末“Fix round 1”章节的纯净快照与隔离证据为准；旧正式目录批次没有删除、覆盖或改写。

## 做了什么

- `README.md` 补齐用户可见边界：声音支持静音、经确认的环境原声和系统自动配音；本机中文声音、整批只合成一次、视频跟随完整配音、移除素材原声、不调用云端模型、不自动进入发布任务。
- `QUALITY_GATES.md` 将原“三线”限定为三条平台功能线，并把本地内容生产明确为第四条独立功能线；补入系统配音 9–12 号精确门槛和完整定向测试入口。
- `app_core/montage_models.py` 移除配音模式单镜头 10 秒上限报错中对隐藏手工目标时长的误导；非配音模式仍保留“镜头不能超过成片时长”的明确校验。`test_montage_models.py` 增加回归用例锁定该文案边界。
- 配音设计文档把过时的“配音正文保留在 request.json”改为只在内存和临时文件中短暂存在；JSON 回执、进度和普通日志只保留声音、地区、时长、格式和哈希等安全摘要。
- 两张 Archify 图由“设计”更新为“源码实现”，明确 Mac 机器回读、Windows 合同级、人工听感待确认、不持久化配音正文、未递增版本或打包。
- `SOURCE_OF_TRUTH.md` 记录本轮测试、两个 Mac 批次、真实素材源码客户端回读、Windows 保留边界和唯一下一步。

## 初始轮次自动化验证（已撤销为验收证据）

1. 新增报错边界用例：`1/1` 通过。
2. 混剪模型用例：`17/17` 通过。
3. 指定定向套件：`77/77` 通过，用时 `6.664s`；真实 Mac 系统配音集成测试未跳过。
4. 离屏源码客户端：`NATIVE_DESKTOP_UI_OK`。
5. 完整离线回归：`3175/3175` 通过，用时 `194.381s`，零失败、零错误。
6. 批次隐私与回执断言：`NARRATION_TEXT_NOT_PERSISTED_IN_BATCH_JSON=PASS`、`BATCH_RECEIPT_INVARIANTS=PASS`。
7. 正式本地数据库只读回读：`quick_check=ok`、账号数仍为 `14`。
8. 最终 `git diff --check` 与 `git diff --cached --check` 见提交前复核，均要求零输出、退出码 0。

指定定向命令：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest -v \
  test_montage_models test_narration_service test_montage_runtime \
  test_montage_service test_automatic_montage_page \
  test_montage_narration_integration test_main_window
```

完整回归与离屏客户端命令：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python desktop_native_app.py --ui-test
.venv/bin/python -m unittest discover -p 'test_*.py'
git diff --check
```

## Archify 收据与视觉复核

两张图均执行 `validate`、`deliver` 和 `visual-check`；`validate`/`deliver` 均为 `9/9 showcase`、`0 errors`、`0 warnings`、退出码 0，`visual-check` 的四种 containment 尺寸均无横向或纵向溢出。

| 图 | JSON SHA-256 | HTML SHA-256 | 视觉复核 |
| --- | --- | --- | --- |
| architecture | `33f69ff7b43af4740b0276f285e3492670a985a0585d2017709f32b00580b029` | `5cc195833b6ed17e34c08645e5b05ad426b29f1e0d258c86f34517b6f0eaa814` | 1440×900 与 2048×1320、亮/暗共 4 张截图人工查看通过；修正轮次 0 |
| lifecycle | `ecaa280983e8be332cba685fe048720102ebe1fad005c4416e80ba448d01abe2` | `02216b1161d58b51d2d505ece5260d06b433f0715da8f2eedaa61cdc81db5434` | 1440×900 与 2048×1320、亮/暗共 4 张截图人工查看通过；修正轮次 0 |

人工查看项目：没有发现截断、节点文字溢出、连线穿过节点、含糊走线或亮暗主题可读性问题。`visual-check` JSON 的自动字段仍按工具合同保留 `visualReview: pending`；上表的“人工查看通过”是本轮实际逐张检查结果，不伪造工具字段。

## 初始轮次 Mac 批次记录（最终验收以 Fix round 1 为准）

可控测试批次 `M-NARRATION-INTEGRATION`：

- 系统声音：`Eddy (中文（中国大陆）)`，区域 `zh-CN`。
- 实测讲话 `8638ms`，主音轨与 3 条输出均 `8938ms`。
- 主音轨文件 SHA-256：`cc620750956d539724379cd532abf932773e5d20cd5a5cdbd9c2d8746f3eaec9`。
- 主音轨流 SHA-256：`44d855f9f8e9506d06583dda2e9855006ad592060b750ee5cac242ff0c5344c2`；3 条成片的唯一音频流均与之相等，无素材原声。
- 三条输出 SHA-256：`87dea59f09c7c9f72ebceb89680a0b9f19410886d5cd7c19185dab70707de62f`、`e96dfe234364468c809ce86acbd2a433c7fe36726b05b3fc9c86fe1f12633f55`、`f5f2ce1a45a5e1ec64225184d566b8f09df34ad4384c3af5a93d41789f25560b`。

源码客户端真实本地素材批次 `M0903160612-1765F3`（混源且写入正式目录，已撤销为验收证据）：

- 使用素材管理中勾选的 2 条真实本地内容视频，生成 3 条；未勾选受控复用。
- 系统声音：`Eddy (中文（中国大陆）)`；讲话 `7993ms`，主音轨 `8293ms`，3 条输出各 `8300ms`、1080×1920。
- 主音轨文件 SHA-256：`6ff67af5fe2d28645fe9cc2ffe19290f478db6b7ea64337f053fb3a4817d9d63`；音频流 SHA-256：`0a2d67c285b27822c8cb74dd5143489061eac10e92d1935db3760ae24930471b`。
- 三条输出 SHA-256：`dfde3f6a05726119255feda0a8e578da691d42cf1f0a9d907cd0a28223be0c87`、`9f1d1887d31c5f911a334163d16380aeaf381e6040246b491943c5d2841fac1d`、`6235a4e47f5e77d41133f8f0e0d0fce0aaa8374e7c859d269e9a59b384b125dd`。
- 每条只有 1 条 AAC 音频流，均等于主音轨流；回执 `success=3`、`failed=0`。逐文件 `ffprobe` 回读了 H.264 视频流和 8.293 秒 AAC 音频流。
- 完整配音文案在批次 JSON 精确扫描中零命中；`request.json` 只保存声音模式、素材路径、时长、数量和随机种子等安全运行摘要。
- 证据目录：`/Users/andy/Library/Application Support/一键发/automatic-montage/M0903160612-1765F3/`。

## 初始轮次源码客户端与人工停点（已失效）

- 启动前确认已安装客户端和源码客户端均未运行；之后运行 `.venv/bin/python tools/run_source_live.py --page montage`。
- 源码进程 PID `76345`；窗口标题为“一键发· 多平台内容发布工作台｜源码联调（正式账号数据）”，源码启动备份为 `/Users/andy/Library/Application Support/一键发开发版/backups/20260903-160306-76345`。
- 自动混剪页显示“已生成 3 条；系统配音：Eddy (中文（中国大陆）)，主音轨 8.293 秒，检查无误后再进入发布”，进度 100%。
- 已双击首条结果；QuickTime 打开精确文件 `.../M0903160612-1765F3/001/video.mp4`，播放开关为 `off`、时间线为 `00:00`。没有替大帅播放或判断音色。
- 源码客户端继续运行并停在结果页；QuickTime 停在首帧。大帅只需点击播放并确认音色、语速、末句完整性以及没有素材原声串入。

## 初始轮次保留边界与自审（以 Fix round 1 更正为准）

- 本轮没有递增版本、打包、安装替换、推送、平台预检、创建发布任务、操作账号或公开发布；`0.5.31` 安装包不含本轮源码能力。
- Windows 只完成适配器合同测试，没有 Windows 真机、系统中文声音或安装包验收；Mac 结果不能外推到 Windows。
- 本轮能确认的是测试通过、Mac 机器回读通过、真实素材成片已打开；不能确认音色、语速、末句或整体听感已经通过。
- 两条真实素材来自正式素材管理库且属于不同内容来源，本批只作本地技术验收；这不构成来源项目的内容合并、再创作、复用或发布授权，也不得把成片用于对外交付。若要求验收产物本身必须同一内容项目，还需另取同项目两条素材重跑。
- 工作树原有 `account_service.py`、`desktop_native_app.py`、`ui/main_window.py`、`tools/run_source_live.py`、若干平台/UI 测试和其他自动视频生产未跟踪产物未被本任务提交；Archify 视觉 sidecar 也保持未跟踪，避免把既有工作擅自纳入提交。

## 初始轮次原下一步（已被 Fix round 1 取代）

大帅在当前已打开的 QuickTime 窗口点击播放；听完整条并确认音色、语速、末句完整、没有素材原声串入，即完成人工听审门。人工确认前保持“Mac 源码真实素材机器验收通过、人工听审待确认”。

## Fix round 1：纯净快照、隔离批次与最终停点

### 两项 Important 的处理结论

1. 初始批次 `M0903160612-1765F3` 写入正式 `/Users/andy/Library/Application Support/一键发/automatic-montage/`，且两条源视频来自 EP09 与知言两个独立内容来源，违反本次验收的项目隔离边界。该批从验收证据中撤销；目录、17 个文件与回执保持原样，未删除、覆盖或改写。Fix round 1 只读复核时回执 SHA-256 为 `b79eb04adef337c3ec7b5de91aca2cb4dd89c0e9cb8333bb65c026e2a01bf3f4`、mtime 为 `2026-09-03T16:06:18+0800`。新批次 ID 在正式目录中不存在。
2. 初始 `77/77`、`3175/3175`、`NATIVE_DESKTOP_UI_OK` 与源码启动均发生在含其他未提交改动的混合工作树，不能归因于提交 `663b68e`，现一并撤销为验收证据。Fix round 1 建立 detached 临时 worktree `/private/tmp/oneclick-task7-fix1-663b68e`，先验证 `663b68e970a2cf9e4dc103bd6f3a9d49529b9ff4`，再只补齐源码客户端自动混剪接线与相应导航回归，形成纯净验证提交 `606925f28fcf9fddfab01c83349e6c831d3a2d43`。实际分支上的对应提交是 `696ab17324e7bbe0c2fda6997a42032728df93eb`，两者 tree object 均为 `e671320f3737b9b7c3217347ed41622c0428f87b`，所以运行时与测试源码逐字相同。

### 纯净快照验证及中途失败记录

- 干净 `663b68e` 定向套件为 `75/75`（`6.761s`）且真实 Mac 配音集成未跳过；离屏客户端返回 `NATIVE_DESKTOP_UI_OK`。这两项只说明当时已提交服务能力，不说明源码客户端入口已接通。
- 第一次在 `663b68e` 全局设置隔离用户目录后运行全量，`3173` 项中 1 项失败：`test_frozen_windows_uses_local_app_data_outside_bundle`。原因是验收命令的全局 `YIJIANFA_USER_DATA_DIR` 有意覆盖了 Windows 默认路径合同；去掉该环境变量后，同一纯净提交 `3173/3173` 通过（`190.820s`）。失败没有被隐藏或当作产品缺陷。
- 直接尝试从干净 `663b68e` 启动 `--page montage` 时，参数解析以退出码 2 拒绝：当时提交没有把自动混剪接入源码客户端启动参数和主窗口。Fix round 1 因而提交了最小接线，并补齐关闭顺序与所有受影响导航索引断言；没有纳入混合工作树中暂停 Meta 入口隐藏等无关改动。
- 首次在接线后的纯净快照跑全量，`3174` 项中 3 项失败，均是旧的发布中心/图文矩阵/带货固定索引断言。修正断言后 3 条定向回归通过；最终纯净提交 `606925f` 完整回归 `3174/3174` 通过（`203.833s`），前后 `git status --porcelain` 均为空。
- 最终纯净提交的本地自动混剪定向命令运行 `76/76`（`7.348s`），真实 Mac 系统配音集成未跳过；隔离离屏客户端返回 `NATIVE_DESKTOP_UI_OK`。两项运行前后纯净快照均无变更。

最终可执行命令：

```bash
ONECLICK_MONTAGE_EVIDENCE_DIR='<fix-evidence>/integration-batch-head-606925f' \
  QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest -v \
  test_montage_models test_narration_service test_montage_runtime \
  test_montage_service test_automatic_montage_page \
  test_montage_narration_integration test_main_window

.venv/bin/python -m unittest

YIJIANFA_USER_DATA_DIR='<fix-evidence>/ui-test-head-606925f' \
  QT_QPA_PLATFORM=offscreen .venv/bin/python desktop_native_app.py --ui-test
```

完整命令、失败轮次说明与最终输出文件哈希见 `fix-round-1-evidence/command-receipts.md`；最终输出（仅规范化日志行尾空格）保存在 `fix-round-1-evidence/command-output/`。

### 隔离 Mac 真实批次

隔离证据根目录：

`/Users/andy/Documents/Codex/2026-07-28/new-chat/outputs/一键发桌面UI基座/.worktrees/integration-oneclick-0.5.30-stable/.superpowers/sdd/2026-09-03-oneclick-automatic-montage-system-narration/fix-round-1-evidence/`

- 源素材是本项目生成的两条中性测试图案视频，不包含任何内容项目素材：`oneclick-neutral-a.mp4` 为 `0a22d13e042c59c6ca141d2f221408c8aafdf76f2580eb99a03a001684c12b81`，`oneclick-neutral-b.mp4` 为 `b066c908f05c53fa5901ac74d003b3b310c909f11f9b23175974f2de353e49ae`；均为 20 秒 H.264 + AAC。
- 隔离用户数据只含上述 2 条素材，数据库 `quick_check=ok`；导入副本、请求、回执、主音轨和全部输出都只位于 `fix-round-1-evidence/runtime-user-data/`。
- 源码客户端批次 `M0903163951-A0F528`：`success=3`、`failed=0`，系统声音 `Eddy (中文（中国大陆）)` / `zh-CN`，讲话 `7209ms`、主音轨和每条成片 `7509ms`，输出均为 1080×1920。
- 主音轨文件 SHA-256 为 `8ccf56dc517f05462d97853f27bc3fa79430e8d41f4ef2d7ae9e9234cd0187f8`，主音轨流为 `6f8bba7d196a00e4e0cdbe764707cd1598293d39694a40b8101d0c71d8cfe9b0`；三条成片唯一音频流均与主音轨流相等，输出文件哈希依次为 `4665d288fd2caae1635fe5bd2355cb3ad22e13aa5a974412a7f27b44c4d351ee`、`5f657948ff313588a8e585593d6544331e28323ce2027633df150ffb29e34e16`、`d4e524a8bd7a250a61bb0849847b51d48b2f2baa86a4156911f40065894b974b`。
- 三条剪辑指纹互不相同；每条 `ffprobe` 回读 H.264 视频与单一 AAC 音频。`request.json` 只含隔离副本路径和安全运行摘要；完整配音正文在批次全部 JSON 中精确扫描为零命中。
- 定向套件另生成固定批次 `integration-batch-head-606925f/M-NARRATION-INTEGRATION`，`success=3`、`failed=0`，讲话 `8638ms`、主音轨/成片 `8938ms`，三条唯一音频流均为 `44d855f9f8e9506d06583dda2e9855006ad592060b750ee5cac242ff0c5344c2`。该批是可重复的真实 Mac 系统配音集成证据，不是人工听感证据。

### Archify 可复核收据与人工视觉检查

- 在纯净 `606925f` 上对 frozen architecture/lifecycle JSON+HTML 分别重新执行 `validate`、`deliver`、`visual-check`；两图均为 `9/9 showcase`、`0 errors`、`0 warnings`，四个 viewport containment 均无横纵溢出，4 张亮/暗截图均成功。JSON/HTML SHA 与初始轮次完全相同，没有修改 frozen 图。
- 鞋匠逐张查看两图的 1440×900 light/dark 与 2048×1320 light/dark，共 8 张：未见截断、节点文字溢出、连线穿过节点、含糊走线或主题对比度问题，修正轮次 0。工具回执仍按合同保留 `visualReview: pending`，人工结论不篡改工具字段。
- 提交只纳入两张 narration 图各自的 `visual-check.json`、contact sheet 和 4 张截图；不纳入旧 `automatic-video-production*` 或其他无关架构产物。纯净快照生成的 10 张截图/contact sheet 与所提交 sidecar 字节一致；两份 JSON 收据仅因 `artifact.path` 分别指向临时 worktree 与当前 worktree 而不同。

### 最终源码启动与人工停点

- 最终源码客户端由纯净提交 `606925f` 的 `/private/tmp/oneclick-task7-fix1-663b68e` 启动，进程 PID `82411`，`cwd` 已由 `lsof` 回读为该临时 worktree。启动命令只为该进程设置 `YIJIANFA_USER_DATA_DIR=<fix-evidence>/runtime-user-data`，没有读取或写入正式用户数据目录。
- 源码客户端当前停在“自动混剪”页，只显示两条“一键发中性验收”素材；QuickTime 打开隔离批次 `M0903163951-A0F528/001/video.mp4`，播放开关 `off`、经过时间 `00:00`、时间线 `0`、总时长 `00:07`。最终客户端与 QuickTime 截图位于 `fix-round-1-evidence/ui-evidence/`。
- 鞋匠没有播放或试听，不宣称音色、语速、末句完整性或是否混入素材原声已通过人工听审。大帅只需在当前 QuickTime 窗口点击播放并听完整条。

### Fix round 1 保留边界与疑虑

- 未改版本、打包、安装、推送、创建平台任务、操作平台或账号；Windows 仍只完成适配器合同级单测，没有 Windows 真机、系统中文声音或安装包验收。
- 当前脏工作树中原有的账号/Meta 修改、其测试和 `automatic-video-production*` 产物继续原样保留，未被本任务提交。源码接线提交只包含 6 个明确文件；文档收口提交只包含本报告、权威文档、两张 narration 图的 visual-check sidecar 和必要隔离证据。
- `QUALITY_GATES.md` 已明确：第四条本地内容生产线不属于原三条 `check_workstream_scope.py` stream 参数；它使用文档中的可执行定向测试，改到共享核心时再由集成线跑完整回归、离屏客户端与差异检查。
- 唯一未完成项是人工听审；在大帅确认前，准确状态只能是“Mac 隔离批次机器回读通过，人工听感待确认”。
