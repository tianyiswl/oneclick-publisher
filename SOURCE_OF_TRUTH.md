# 一键发当前事实

更新时间：2026-08-23（Asia/Shanghai）

本文件是一键发当前版本、三线状态和真实验收的唯一状态入口。能力说明与本文件冲突时，先按本文件降级判断，再用当前源码、同次运行证据和平台回读核验并修正冲突。

## 当前版本与基线

- 当前正式主线功能提交：`1da7b85`。
- 当前应用版本：`0.4.2`。
- 下一整合候选版本：建议 `0.5.0`；三线完成集成前不修改版本号，不称已经发布。
- 2026-08-23 在 `main@1da7b85` 使用 `.venv/bin/python -m unittest discover -p 'test_*.py'` 完整运行：`1446` 项通过，`0` 项失败，用时 `68.536s`。
- 加入本轮治理文件与边界检查器后最终完整运行：`1454` 项通过，`0` 项失败，用时 `64.694s`。
- 本轮只建立协作与集成基线，没有新增数据监测、海外发布或定位业务能力。

## 状态口径

| 状态 | 只代表什么 |
| --- | --- |
| 源码已接入 | 代码和入口存在 |
| 本地已验证 | 对应自动测试或离屏界面通过 |
| 真实登录已验证 | 当前专用测试账号登录并在重启后检测成功 |
| 平台预检已验证 | 素材和字段在最终按钮前完成回读，没有提交 |
| 平台提交已触发 | 经当次授权执行最终动作，但仍可能没有成功 |
| 平台成功已回读 | 平台明确回执或内容管理页读到目标内容 |

低一层状态不能替代高一层状态。

## 三线当前状态

| 支线 | 当前代码与本地状态 | 当前真实证据 | 仍缺什么 | 独立分支 |
| --- | --- | --- | --- | --- |
| 数据监测 | 已注册抖音、小红书、快手、B站、公众号五个平台的只读采集器；视频号未注册。当前主线完整测试通过。 | 小红书最近两次同步记录为成功；公众号、B站、快手各有连续两次真实只读同步和数据库/界面读回记录。抖音已有历史真实同步，但当前优化完成后仍须重新验证。 | 当前优化内容的两次真实同步、数据库读回、界面读回和资源关闭证据；视频号仍为 `collector_not_available`。 | `feature/data-monitoring-v3` |
| 海外登录发布 | TikTok、YouTube、Instagram Reels、Facebook Reels 已有登录、静默检测、视频预检和受控正式动作源码，离线回归存在。 | 当前没有专用海外测试账号从登录到内容列表回读的完整证据。 | 默认先完成 YouTube：登录、重启检测、预检、当次授权后的测试发布、内容列表回读；再逐个平台推进。 | `feature/overseas-login-publish-v2` |
| 国内定位 | 抖音普通发布与抖音带货已有地点搜索、完整地址/平台标识匹配、分页、缓存和发布前重新核验；最新地区词缓存修复已进入主线。 | 旧带货账号曾完成受控采集和真实批次回读；最新地区词修复因当前账号失去带货模式，尚未完成同能力账号实测。其他国内平台未形成可宣称已接入的定位能力。 | 可用带货账号下验证三组地区关键词、加载更多、自动填入、预检和经单独授权的发布回读；其他平台必须逐个平台确认是否支持定位和字段合同。 | `feature/domestic-location-v2` |
| 集成与发布 | 当前只负责共享核心、合并、全量回归和版本一致性。 | 本轮治理文件和边界检查器加入后完整测试为 `1454/1454`。 | 三条线依次通过质量门后才能合并；最终再做 Mac 真实启动、Windows 候选包和版本一致性检查。 | `integration/oneclick-0.5.0` |

真实验收明细仍保留在：

- `docs/superpowers/reports/2026-08-20-xiaohongshu-data-contract-report.md`
- `docs/verification/domestic-platform-data-monitoring-2026-08-21.md`
- `docs/OVERSEAS_PLATFORM_STATUS.md`
- `docs/DOUYIN_COMMERCE_WORKFLOW.md`

## Worktree 布局

四条工作线从包含本文件的同一治理提交创建：

| 用途 | 分支 | 项目内目录 |
| --- | --- | --- |
| 单线集成 | `integration/oneclick-0.5.0` | `.worktrees/integration-oneclick-0.5.0` |
| 数据监测 | `feature/data-monitoring-v3` | `.worktrees/data-monitoring-v3` |
| 海外登录发布 | `feature/overseas-login-publish-v2` | `.worktrees/overseas-login-publish-v2` |
| 国内定位 | `feature/domestic-location-v2` | `.worktrees/domestic-location-v2` |

三条功能线可以同时开发；共享核心合并、全量回归、版本递增和打包必须串行进行。

## 现有未跟踪产物保护

建立治理基线时，主工作区已有以下未跟踪内容：

- `.superpowers/brainstorm/`
- `docs/architecture/douyin-comment-insight-*`
- `docs/architecture/xiaohongshu-data-monitoring.visual-check.*`
- `docs/superpowers/` 下与 `douyin-comment-insight` 有关的设计产物
- `outputs/`

它们没有被本轮提交、移动或删除。默认由数据监测线先确认归属；确认前其他支线不得触碰。

## 当前唯一下一步

三条功能线分别在自己的 worktree 内确认首个七天可验证目标；任何需要修改共享核心的需求先形成独立接线提交，交给集成线处理。功能开发与真实平台验收仍须分别记录。
