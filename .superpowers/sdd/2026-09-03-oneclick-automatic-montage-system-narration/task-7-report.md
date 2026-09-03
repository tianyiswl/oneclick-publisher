# Task 7 报告：项目边界、回归与人工听审停点

日期：2026-09-03

状态：源码和 Mac 真实素材机器验收通过；人工听审待大帅确认；Windows 仍只有适配器合同测试。

## 做了什么

- `README.md` 补齐用户可见边界：声音支持静音、经确认的环境原声和系统自动配音；本机中文声音、整批只合成一次、视频跟随完整配音、移除素材原声、不调用云端模型、不自动进入发布任务。
- `QUALITY_GATES.md` 将原“三线”限定为三条平台功能线，并把本地内容生产明确为第四条独立功能线；补入系统配音 9–12 号精确门槛和完整定向测试入口。
- `app_core/montage_models.py` 移除配音模式单镜头 10 秒上限报错中对隐藏手工目标时长的误导；非配音模式仍保留“镜头不能超过成片时长”的明确校验。`test_montage_models.py` 增加回归用例锁定该文案边界。
- 配音设计文档把过时的“配音正文保留在 request.json”改为只在内存和临时文件中短暂存在；JSON 回执、进度和普通日志只保留声音、地区、时长、格式和哈希等安全摘要。
- 两张 Archify 图由“设计”更新为“源码实现”，明确 Mac 机器回读、Windows 合同级、人工听感待确认、不持久化配音正文、未递增版本或打包。
- `SOURCE_OF_TRUTH.md` 记录本轮测试、两个 Mac 批次、真实素材源码客户端回读、Windows 保留边界和唯一下一步。

## 自动化验证

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

## Mac 批次证据

可控测试批次 `M-NARRATION-INTEGRATION`：

- 系统声音：`Eddy (中文（中国大陆）)`，区域 `zh-CN`。
- 实测讲话 `8638ms`，主音轨与 3 条输出均 `8938ms`。
- 主音轨文件 SHA-256：`cc620750956d539724379cd532abf932773e5d20cd5a5cdbd9c2d8746f3eaec9`。
- 主音轨流 SHA-256：`44d855f9f8e9506d06583dda2e9855006ad592060b750ee5cac242ff0c5344c2`；3 条成片的唯一音频流均与之相等，无素材原声。
- 三条输出 SHA-256：`87dea59f09c7c9f72ebceb89680a0b9f19410886d5cd7c19185dab70707de62f`、`e96dfe234364468c809ce86acbd2a433c7fe36726b05b3fc9c86fe1f12633f55`、`f5f2ce1a45a5e1ec64225184d566b8f09df34ad4384c3af5a93d41789f25560b`。

源码客户端真实本地素材批次 `M0903160612-1765F3`：

- 使用素材管理中勾选的 2 条真实本地内容视频，生成 3 条；未勾选受控复用。
- 系统声音：`Eddy (中文（中国大陆）)`；讲话 `7993ms`，主音轨 `8293ms`，3 条输出各 `8300ms`、1080×1920。
- 主音轨文件 SHA-256：`6ff67af5fe2d28645fe9cc2ffe19290f478db6b7ea64337f053fb3a4817d9d63`；音频流 SHA-256：`0a2d67c285b27822c8cb74dd5143489061eac10e92d1935db3760ae24930471b`。
- 三条输出 SHA-256：`dfde3f6a05726119255feda0a8e578da691d42cf1f0a9d907cd0a28223be0c87`、`9f1d1887d31c5f911a334163d16380aeaf381e6040246b491943c5d2841fac1d`、`6235a4e47f5e77d41133f8f0e0d0fce0aaa8374e7c859d269e9a59b384b125dd`。
- 每条只有 1 条 AAC 音频流，均等于主音轨流；回执 `success=3`、`failed=0`。逐文件 `ffprobe` 回读了 H.264 视频流和 8.293 秒 AAC 音频流。
- 完整配音文案在批次 JSON 精确扫描中零命中；`request.json` 只保存声音模式、素材路径、时长、数量和随机种子等安全运行摘要。
- 证据目录：`/Users/andy/Library/Application Support/一键发/automatic-montage/M0903160612-1765F3/`。

## 源码客户端与人工停点

- 启动前确认已安装客户端和源码客户端均未运行；之后运行 `.venv/bin/python tools/run_source_live.py --page montage`。
- 源码进程 PID `76345`；窗口标题为“一键发· 多平台内容发布工作台｜源码联调（正式账号数据）”，源码启动备份为 `/Users/andy/Library/Application Support/一键发开发版/backups/20260903-160306-76345`。
- 自动混剪页显示“已生成 3 条；系统配音：Eddy (中文（中国大陆）)，主音轨 8.293 秒，检查无误后再进入发布”，进度 100%。
- 已双击首条结果；QuickTime 打开精确文件 `.../M0903160612-1765F3/001/video.mp4`，播放开关为 `off`、时间线为 `00:00`。没有替大帅播放或判断音色。
- 源码客户端继续运行并停在结果页；QuickTime 停在首帧。大帅只需点击播放并确认音色、语速、末句完整性以及没有素材原声串入。

## 保留边界与自审

- 本轮没有递增版本、打包、安装替换、推送、平台预检、创建发布任务、操作账号或公开发布；`0.5.31` 安装包不含本轮源码能力。
- Windows 只完成适配器合同测试，没有 Windows 真机、系统中文声音或安装包验收；Mac 结果不能外推到 Windows。
- 本轮能确认的是测试通过、Mac 机器回读通过、真实素材成片已打开；不能确认音色、语速、末句或整体听感已经通过。
- 两条真实素材来自正式素材管理库且属于不同内容来源，本批只作本地技术验收；这不构成来源项目的内容合并、再创作、复用或发布授权，也不得把成片用于对外交付。若要求验收产物本身必须同一内容项目，还需另取同项目两条素材重跑。
- 工作树原有 `account_service.py`、`desktop_native_app.py`、`ui/main_window.py`、`tools/run_source_live.py`、若干平台/UI 测试和其他自动视频生产未跟踪产物未被本任务提交；Archify 视觉 sidecar 也保持未跟踪，避免把既有工作擅自纳入提交。

## 唯一下一步

大帅在当前已打开的 QuickTime 窗口点击播放；听完整条并确认音色、语速、末句完整、没有素材原声串入，即完成人工听审门。人工确认前保持“Mac 源码真实素材机器验收通过、人工听审待确认”。
