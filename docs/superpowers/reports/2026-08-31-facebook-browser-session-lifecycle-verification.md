# Facebook Page 浏览器会话生命周期修复验收

日期：2026-08-31（Asia/Shanghai）

## 结论

用户看到的“进入后台后关闭、随后又重新打开”不是操作系统闪退。预检成功后关闭浏览器原本就是正常收尾；真正造成重复弹窗的是同一个结果不明任务可被多次显式调用只读核对，而任务回执又没有说明浏览器为何关闭，导致正常关闭、失败关闭和重复核对看起来完全一样。

本轮源码已经把一次任务收敛为可解释、可查询的会话生命周期：

- 预检、正式发布和只读核对分别记录浏览器打开与关闭事件；
- 关闭原因固定为正常完成、安全失败、结果不明、核对失败或进程中断；
- 任务 JSON 返回最近一次 `browserSession`，调用方无需通过窗口是否消失猜测结果；
- 同一个结果不明任务最多开启一次只读核对浏览器，之后再次查询只读数据库，不会继续弹窗；
- 正式任务仍在同一会话内进行原有的有界结果回读，不增加第二次最终点击或自动重发。

## 测试证据

- 测试先在旧实现上得到预期失败：没有浏览器生命周期事件、任务 JSON 没有会话状态、第二次核对仍会启动浏览器。
- 新增四项针对性测试全部通过：预检正常关闭、正式结果不明关闭、第二次核对不再开浏览器、任务 JSON 暴露会话状态。
- Facebook 发布服务与任务服务受影响模块共 `170/170` 项通过。
- 完整离线回归 `3083/3083` 项通过，用时 `189.566s`。
- 源码离屏界面返回 `NATIVE_DESKTOP_UI_OK`。
- Facebook Page 生命周期图通过 Archify showcase 结构校验：9 项检查全部通过，0 error、0 warning；1440×900、1600×1000、1920×1080、2048×1320 均无溢出，明暗截图已人工检查。
- `git diff --check` 通过。工作线范围检查返回 `REVIEW_REQUIRED`，原因是本修复必须同时修改受控发布、任务持久化和状态投影等共享核心；当前工作位于集成分支，已按共享核心变更验收。

## 真实平台边界

离线修复完成后，大帅明确要求先用源码测试。鞋匠因此通过源码联调锁和正式账号数据备份执行了一次全新 P8 真实预检：

- 启动前数据库备份：`/Users/andy/Library/Application Support/一键发开发版/backups/20260831-122119-51967`；
- 内容包：`runtime/facebook-page-source-lifecycle-20260831-v8/manifest.json`；
- 任务：`101 / T08311221-29C5`；
- 结果：`success / platform_form_verified`；
- 正确回读目标 Page、视频 `facebook-page-source-lifecycle-v8.mp4`、正文、公开范围和唯一可用最终按钮；
- 浏览器事件严格为 `facebook_preflight_browser_opened=1`、`facebook_preflight_browser_closed=1`；关闭回执为 `preflight_completed`；
- 预检结束后连续两次从源码查询任务状态，均返回同一关闭回执，没有新增浏览器事件，也没有重新弹出后台；
- `platformWriteOccurred=true`，表示预检向表单选择了临时媒体；`finalActionTriggered=false`，没有 Reel ID、URL 或发布时间；
- 结束后活动任务和未消费授权均为 `0`，源码联调标记已清除，数据库 `quick_check=ok`，没有残留源码客户端或 Playwright 进程。

大帅随后单独明确授权“基于任务 101 新建一次性授权，公开发布这条 P8 测试 Reel，并从后台唯一回读结果”。鞋匠按该授权只执行一次正式动作：

- 正式运行前备份：`/Users/andy/Library/Application Support/一键发开发版/backups/20260831-122639-52339`；
- 正式任务：`102 / T08311226-C607`；
- 一次性授权已即时消费，数据库只记录 `facebook_final_action_claimed=1` 和 `facebook_final_action_clicked=1`，没有第二次点击；
- 正式浏览器事件为 `facebook_formal_browser_opened=1`、`facebook_formal_browser_closed=1`，关闭原因是 `outcome_unknown`；
- 同一会话的有界回读没有取得 Reel ID、URL、发布时间或内容列表唯一命中，任务进入 `facebook_publish_outcome_unknown / ambiguous`；
- 本地防重 claim 为 `blocksReplay=1`，该任务不得再次授权、点击或重放。

按同一授权中的“后台唯一回读结果”要求，随后只执行一次不含上传和发布动作的只读核对：

- 核对前备份：`/Users/andy/Library/Application Support/一键发开发版/backups/20260831-122832-52437`；
- 浏览器事件为 `facebook_reconciliation_browser_opened=1`、`facebook_reconciliation_browser_closed=1`，关闭原因是 `reconciliation_completed`；
- 仍未找到唯一 Reel 回执，任务继续保持 `ambiguous / blocksReplay=1`；
- 为验证本轮防循环修复，再次调用同一任务核对时约 `0.4` 秒直接返回已有状态，没有重新打开浏览器，四类浏览器事件计数均未增加。

正式运行和只读核对结束后，活动任务和未消费授权均为 `0`，源码联调标记已清除，没有残留桌面客户端或 Playwright 进程，数据库 `quick_check=ok`。任务 `89`、`91`、`93` 继续保持结果不明和禁止重放；任务 `100` 仍只是一条旧预检证据。

这组证据只证明浏览器生命周期和“同一结果不明任务最多一次只读核对”的修复在真实流程中生效。任务 `102` 已触发最终动作，但没有平台成功回执，也没有明确失败证据；因此不能记为已发布或正式发布可用，也不能建议重发。

## 任务 102 后续根因与源码修复

任务数据库中没有保存任务 `102` 点击后的页面截图或浏览器跟踪文件，平台也没有返回 Reel ID、URL、发布时间或唯一列表命中，因此不能从现有证据反推当时 Meta 页面究竟已受理、仍待确认还是拒绝，也不能事后把任务状态升级为成功或失败。

源码复盘确认了一个会丢失有效回读结果的独立缺陷：最终点击后页面可能立即切换，`read_platform_decision()` 因而允许暂时返回空值；旧实现仍把 `platformDecision: null` 传给证据持久化层。持久化层会把出现过的 `platformDecision` 视为需要密封校验的证据并拒绝空值，所以后续进度事件无法落库：

- 内容列表即使唯一找到 Reel，也可能在写入成功回执前被该校验中断；
- 内容列表没有命中时，已经观察到的 `confirmation_pending` 等点击后状态也会丢失，最终只剩笼统的 `facebook_publish_outcome_unknown`。

修复后，可选平台决定只有在真实读到时才进入证据；唯一内容列表回读不再依赖提示条，可独立确认成功；没有唯一回执时，确认中、导航失败等点击后状态会保留原始状态和固定错误码，防重 claim 不会解除。新增两项测试分别覆盖“缺少平台决定但唯一回读成功”和“缺少平台决定但确认中状态必须保留”，两项均先在旧实现失败、再随最小修复通过。

本次修复后的验证结果：

- Facebook 发布与任务服务 `104/104` 项通过；
- 海外受影响范围 `233/233` 项通过；
- 完整离线回归 `3085/3085` 项通过，用时 `189.921s`；
- 源码编译、`git diff --check` 和离屏桌面界面均通过，界面返回 `NATIVE_DESKTOP_UI_OK`。

以上均为源码和离线证据。随后使用全新内容执行了一次新的真实预检：

- 内容包：`runtime/facebook-page-final-readback-20260831-v9/manifest.json`；
- 启动前数据库备份：`/Users/andy/Library/Application Support/一键发开发版/backups/20260831-130712-56968`；
- 任务：`103 / T08311307-B982`；
- 结果：`success / platform_form_verified`；
- 正确回读账号 `14` 对应的墨白 Page、全新视频 `facebook-page-final-readback-v9.mp4`、正文、公开范围和唯一可用最终按钮；
- `facebook_preflight_browser_opened=1`、`facebook_preflight_browser_closed=1`，关闭原因是 `preflight_completed`；
- `platformWriteOccurred=true` 只表示预检向表单临时选择了媒体，`finalActionTriggered=false`，没有 Reel ID、URL 或发布时间；
- 收尾后活动任务和有效未消费授权均为 `0`，源码联调标记和平台进程均无残留，数据库 `quick_check=ok`。

任务 `102` 继续保持 `ambiguous / blocksReplay=1`，不得重放。P9 任务 `103` 此时只证明修复后的新内容仍能通过正式账号预检。

## P9 正式结果与慢网络修复

大帅随后明确授权“公开发布任务 103 这条 Facebook Page P9 测试 Reel”。鞋匠为该任务生成并立即消费一次短期授权，并执行一次正式任务：

- 启动前数据库备份：`/Users/andy/Library/Application Support/一键发开发版/backups/20260831-132158-57280`；
- 正式任务：`104 / T08311321-A8EF`；
- 数据库只记录 `facebook_final_action_claimed=1`、`facebook_final_action_clicked=1`，最终按钮真实标签为“分享”，没有第二次点击；
- 点击后旧等待窗口内仍为 `composer_unchanged`，内容列表未唯一读到新 Reel；
- 最终状态为 `facebook_post_click_composer_unchanged / ambiguous`，没有 Reel ID、URL 或发布时间，`blocksReplay=1`。

随后只执行一次不包含上传和发布动作的只读核对，核对前备份为 `/Users/andy/Library/Application Support/一键发开发版/backups/20260831-132400-57340`。内容列表入口未能在旧等待时限内证明完整且主体一致，返回 `facebook_page_baseline_read_failed`；该核对没有改变任务状态，也没有再次点击。

现有事实不能证明 P9 已发布。结合大帅现场确认 Meta 页面从登录到完全加载需要一分钟以上，而源码当时的内容入口等待上限只有约 30 秒、最终点击后无新增 Reel 只等待 3 个 5 秒间隔，证据支持的判断是慢网络下程序过早结束会话；这不是对平台结果的事后推断。Meta 官方电脑端 Reel 流程最后只要求点击一次 Publish，因此源码不增加二次最终点击。

本轮新增两项红灯测试，分别证明旧实现无法等到第 5 次才出现的新 Reel，且默认入口与沉淀等待预算均不足两分钟。最小修复将 Page 内容入口等待和最终点击后的唯一 Reel 回读窗口提高到有界两分钟，其余主体、文案哈希、时间范围、唯一性和防重合同不变。修复后：

- 慢网络定向测试 `4/4` 通过；
- Facebook 与海外受影响范围 `255/255` 通过；
- 完整离线回归 `3087/3087` 通过，用时 `191.977s`；
- 源码编译、`git diff --check` 和离屏界面均通过，界面返回 `NATIVE_DESKTOP_UI_OK`。

为建立不可复用的新正式基线，鞋匠又执行了一次全新 P10 预检：

- 内容包：`runtime/facebook-page-slow-network-20260831-v10/manifest.json`；
- 启动前数据库备份：`/Users/andy/Library/Application Support/一键发开发版/backups/20260831-133837-59277`；
- 任务：`105 / T08311338-954B`；
- 结果：`success / platform_form_verified`；
- 正确回读墨白 Page、全新视频 `facebook-page-slow-network-v10.mp4`、正文、公开范围和唯一可用最终按钮；
- 预检浏览器只打开、正常关闭各 `1` 次，`finalActionTriggered=false`，没有 Reel ID、URL 或发布时间。

任务 `104` 已锁定且不得重放；任务 `105` 尚未获得正式发布授权。

本轮没有递增版本、提交 Git 或重新打包客户端。安装版仍不包含本修复。

## 最终点击后一次性安全诊断

任务 `102` 和 `104` 的现有数据只能证明最终动作各发生过一次，无法回放当时编辑页、弹窗、提示或请求结果。因此本轮不对这两个历史任务做事后推断，也不再用公开发布碰运气，而是先补齐下一个全新任务所需的一次性诊断链。

### 离线实现

- 最终表单已严格回读后，先在编辑页挂载诊断监听，然后才持久化 `final_action_claimed` 并允许一次最终点击。监听无法挂载时不会 claim，也不会点击。
- 最终点击后保持同一编辑页与同一认证上下文，分别在页面状态观察后和内容列表回读结束时采样。基线与点击后列表回读复用同一认证上下文中的一个独立只读页，不在已点击的编辑页上导航到后台列表。
- 诊断只允许以下字段：脱敏后的 Meta Page URL；弹窗与提示的角色、文本 SHA-256、文本长度和提示分类；最终按钮的安全标签、可见数和可用状态；弹出页的脱敏 URL；以及严格许可后的 Meta 主机、脱敏路径、请求方法、资源类型、HTTP 状态和成败。
- URL 只保留已授权 Page 的 `asset_id`，其他查询和 fragment 全部删除；路径只保留固定 Meta 路由词，任何未知、数字或不透明段统一变为 `:redacted`。实现不读取请求头、Cookie、令牌、请求正文或响应正文；选定弹窗与提示文本只在内存中计算 SHA-256 和长度，不持久化原文。畸形 URL 只会让采样降级为 `partial`，不会弄丢防重锁或打断回读。
- 诊断以唯一、幂等且不可改写的 `facebook_post_click_diagnostic_captured` 事件持久化，对外任务 JSON 投影为 `postClickDiagnostic`。事件必须绑定同一 Page、已点击 claim 和当前 worker 租约；它不改变 claim 状态、平台结论或 `blocksReplay`。点击后的局部采样/写入异常不能覆盖唯一 Reel 回读结果，也不能解除结果不明任务的防重锁。

### 自动化证据

- 新增测试先在旧实现上分别暴露缺少诊断事件、无法持久化、诊断挂载失败后已 claim、不安全路径未脱敏、畸形 URL 中断监听、中间进度写入异常丢失采样，以及后台列表独立页缺少显式回归保护；最小修复后均通过。
- Facebook 及共享核心受影响范围 `634/634` 通过，用时 `88.642s`。
- 完整离线回归 `3096/3096` 通过，用时 `187.259s`。
- 源码编译、`git diff --check` 和离屏桌面界面均通过，界面返回 `NATIVE_DESKTOP_UI_OK`。
- `tools/check_workstream_scope.py --stream overseas --base origin/main` 返回 `REVIEW_REQUIRED`；该工作树原有的共享核心、架构图、验收报告与本轮诊断测试同时在变更范围内，没有自行合并。

### 真实验收判断

离线实现完成时，源码已具备最后一次 Facebook Page 真实验收的代码前置能力，但尚没有一个可直接执行的正式验收任务：

- 任务 `102` 和 `104` 已消费最终动作且结果不明，继续 `blocksReplay=1`，不得重放。
- 任务 `105` 只完成历史预检，本轮没有为它创建或消费正式授权，也不得把它当作新验收的授权来源。
- 下一次真实验收必须先准备全新内容并重新预检；预检通过后，再由大帅针对新的具体正式任务单独明确授权。真实浏览器运行必须与 Instagram 测试错开，最终按钮仍最多点击一次。
- 新正式任务必须回读到 `facebook_post_click_diagnostic_captured` 事件，并同时保持独立后台列表只读核对和防重锁。只有取得唯一 Reel ID、规范链接、发布时间且主体/文案哈希/时间窗口全部匹配，才能验收为成功；否则仍保持结果不明并禁止重放。

上述离线实现与测试阶段没有打开真实 Facebook 或 Instagram 浏览器，没有上传、点击最终动作、只读核对现有 Facebook 内容、提交 Git、推送、合并或打包。

## P11 全新内容真实预检

大帅随后回复“执行”，授权范围承接上一条明确下一步：准备全新 Facebook 内容并只执行一次重新预检；这不构成正式公开发布授权。鞋匠先确认 Instagram 专用任务仍处于离线 TDD，写入错峰协调消息，再启动 Facebook 真实浏览器。

- 内容包：`runtime/facebook-page-post-click-diagnostic-20260831-v11/manifest.json`；
- 新视频：`facebook-page-post-click-diagnostic-v11.mp4`，SHA-256 为 `470d4b3ec960b0c18a204c23f122681301e2e5377b53a145a4987d0b9beacbb1`，与任务 `102`、`104`、`105` 的视频哈希均不同；
- 新文案 SHA-256：`adc35c66a5dee9ffe4564b74ad128201c83863131970cdd2c8119966bc57417d`，同样与任务 `102`、`104`、`105` 不同；
- 启动前数据库备份：`/Users/andy/Library/Application Support/一键发开发版/backups/facebook-p11-20260831-150705-71233`；
- 任务：`106 / T08311507-4F18`；
- 结果：`success / platform_form_verified`；
- 正确回读账号 `14` 对应的 Page `1200693729803807`、P11 视频、文案、公开范围和唯一可用最终按钮；表单快照哈希为 `0b9762571f1fc84eacf4c6bfd8d2d5c3eee0b270b4375d476028c374f627f7a0`；
- `platformWriteOccurred=true` 只表示预检向表单选择了临时媒体；`finalActionTriggered=false`，没有 Reel ID、链接或发布时间；
- 浏览器打开和正常关闭各 `1` 次，关闭原因为 `preflight_completed`；最终动作 claim、最终点击、正式 claim 和一次性授权计数均为 `0`；
- 收尾后活动任务为 `0`，源码联调标记已清除，没有该工作树残留进程，数据库 `quick_check=ok`。

任务 `106` 现在是唯一可用于下一次正式验收的具体预检候选，但仍未获得正式公开发布授权。若大帅明确授权该任务，才可以生成并立即消费绑定任务 `106` 的短期一次性授权；最终按钮仍最多点击一次，点击后必须取得安全诊断事件并通过独立列表页唯一回读。任务 `102`、`104` 和 `105` 的历史授权边界不变，均不得复用。

P11 预检之后仍未提交、推送、合并或打包，也没有操作现有 Facebook 平台内容。

## P11 正式验收与一次性诊断结果

大帅随后明确授权“确认公开发布任务 106 这条 Facebook Page P11 测试 Reel”。该授权只绑定任务 `106` 的 P11 内容，不复用任务 `102`、`104` 或 `105` 的任何历史授权。鞋匠确认 Instagram 任务没有运行真实浏览器，并通知其继续避让 Facebook 正式窗口后，才执行本次正式动作。

- 正式启动前数据库备份：`/Users/andy/Library/Application Support/一键发开发版/backups/facebook-p11-formal-20260831-151315-71928`；
- 预检任务：`106 / T08311507-4F18`；其短期一次性授权在 `2026-08-31T07:13:15.948139+00:00` 消费，不能再次使用；
- 正式任务：`107 / T08311513-338A`；
- 最终动作：`facebook_final_action_claimed=1`、`facebook_final_action_clicked=1`，点击时间为 `2026-08-31T07:13:52.996059+00:00`，按钮安全标签为“分享”；claim 随点击立即进入 `blocksReplay=1`，没有第二次点击；
- 终态：`failed / ambiguous / facebook_post_click_composer_unchanged`，没有 Reel ID、规范链接或发布时间，任务 `107` 永久禁止重放；
- 正式浏览器只打开 `1` 次，并一直保持同一编辑页与认证上下文，直到页面观察和独立内容列表只读回读全部结束后才以 `outcome_unknown` 关闭 `1` 次；期间没有另开发布浏览器，也没有在终态后再启动只读核对会话；
- 独立内容列表页没有找到同时匹配 Page、视频/文案哈希、时间窗口和唯一性的 Reel，因此不能把本次动作判定为成功或失败发布。

任务唯一持久化了 `facebook_post_click_diagnostic_captured` 事件。诊断包含 `post_click_observed` 和 `readback_finished` 两次采样，两次均显示：

- 页面仍为正确 Page `1200693729803807` 的 `reels_composer`；
- 没有可见 `dialog`、`alertdialog`、状态提示、错误提示或弹出页；
- 页面内唯一最终按钮仍为“分享”，`visibleCount=1`、`enabled=true`；
- 安全网络摘要共 `19` 条且无丢弃：`14` 个发往 `business.facebook.com/api/graphql/` 的 POST、`3` 个发往脱敏 AJAX 路径的 POST 和 `2` 个发往脱敏 AJAX 路径的 GET，HTTP 状态均为 `200`。

诊断从未读取或保存 Cookie、令牌、请求头、请求正文、响应正文或页面敏感正文。HTTP `200` 只能说明网络传输完成；GraphQL 仍可能在 HTTP `200` 内返回应用层错误，所以这些请求不能作为 Meta 接受发布的回执。

### 证据支持的诊断边界

本次证据足以排除以下已知本地原因：浏览器过早关闭、Meta 慢网络等待窗口不足、上传或表单未完成、点击后出现可见确认/错误而程序漏记，以及 HTTP 传输层失败。最终按钮在两次采样中持续可用，编辑器未变化，后台也没有唯一新 Reel；当前只能把原因缩小到 Meta 应用层结果或客户端点击后无动作，不能进一步断言是权限、限流、平台拒绝或发布成功。

在不读取 GraphQL 应用层结果的既有隐私边界下，没有证据支持继续改选择器、增加等待或再次点击。鞋匠因此没有进行猜测性代码修改，也没有发起新内容重试。若未来需要继续诊断，应先单独评审一种只在内存读取并仅保存允许字段、错误分类或哈希的 GraphQL 顶层结果分类方案；这不构成本次发布授权，也不能解除任务 `107` 的防重锁。

收尾核验显示任务事件各只有预期的一条，活动任务为 `0`，worker 已释放，源码联调标记已清除，无该工作树相关进程，数据库 `quick_check=ok`。本轮没有编辑或删除既有 Facebook 内容，没有提交、推送、合并或打包 Git。P11 的平台结果仍为不明，Facebook Page 正式发布仍未验收可用。

## 任务 107 后续离线根因追踪与诊断 v2

大帅要求继续诊断后，鞋匠没有重放任务 `107`，也没有再用公开发布碰运气。沿着最终点击、Playwright response 监听、诊断投影和事件持久化链路逐层追踪后，能够确认的是一个本地证据缺口，而不是 Meta 平台根因：任务 `107` 所用的 v1 监听只保存 URL、方法、资源类型和 HTTP 状态，GraphQL 操作身份与应用层结果当时已经被主动丢弃。历史事件无法重新生成这些信息，所以任务 `107` 仍只能保持 `ambiguous / blocksReplay=1`，不能事后升级为成功或确定失败。

### v2 离线实现

- 最终动作监听仍在 claim 和点击之前挂载，并在唯一最终点击之前显式标记应用层捕获边界；边界之前的 GraphQL 不进入 v2 结果。
- 仅对 `business.facebook.com/api/graphql/` 的 POST 请求，在内存读取请求 JSON 中的 `fb_api_req_friendly_name` 和 `doc_id`。两者原文不持久化，只保存 SHA-256 与长度，并据操作名分类为 Reel 发布候选 mutation、其他 mutation、query 或 unknown。
- 所有 mutation 都在内存读取顶层响应 JSON，以避免候选名称规则漏掉真实发布操作；query 响应正文不读取。持久化只允许 `data` 是否存在、顶层错误数量、数字错误码、瞬时错误数量、错误消息 SHA-256 与长度、应用层结果分类和采集状态。
- `data` 内容、错误消息原文、完整操作名、完整文档 ID、完整请求与响应、请求头、Cookie、令牌以及任何页面敏感正文均不会进入诊断事件、任务 JSON 或日志。严格投影器拒绝任何额外字段及畸形类型，v1 历史事件继续兼容读取。
- GraphQL 结果使用独立上限，不会因为普通网络摘要先达到 32 条而丢失；收尾时先封口并摘除监听，再等待已经开始的响应分类完成，随后才生成唯一诊断事件和关闭会话。封口后的迟到响应不会混入结果。
- 这些字段只用于定位应用层方向，不是平台发布回执。即使结果为 `data`，仍必须由独立内容列表唯一回读到 Reel ID、规范链接、发布时间并匹配主体、内容哈希与时间窗口，才可以判定成功。

### 红绿测试与回归

- 新增测试先分别在旧实现暴露：没有最终动作应用层边界、mutation 未读取而 query 难以区分、畸形错误结构可能泄漏或误判、普通网络上限吞掉 GraphQL、收口竞态混入迟到响应、投影器对畸形错误码处理不安全；最小实现后逐项转绿。
- 聚焦诊断、发布服务和任务服务最终复跑 `117/117` 通过，用时 `3.116s`；Facebook/Meta 相关模块串行 `365/365` 通过，用时 `87.721s`。
- 完整离线回归 `3101/3101` 通过，用时 `187.926s`；源码编译、`git diff --check` 和离屏桌面界面通过，界面返回 `NATIVE_DESKTOP_UI_OK`。
- Facebook Page 生命周期图已同步 v2 应用层分类与人工门：Archify 结构校验 `9/9`、0 error、0 warning；四档窗口无溢出，明暗图经人工检查未见重叠。冻结 JSON SHA-256 为 `d0b6b9cbb3272adaf4920d0328486205b19c18a180e3cecc9b807439c137a4aa`，HTML SHA-256 为 `00b337d64863ab685ca97caab9b114a63b16f4659fc9323c68a981b13c8d6c2d`。
- `tools/check_workstream_scope.py --stream overseas --base origin/main` 仍按预期返回 `REVIEW_REQUIRED`，因为当前工作树同时包含用户既有的共享核心、测试、报告和架构图修改；没有自动合并。

### 最后一次真实验收条件

当前源码已经具备下一条全新 Facebook Page 任务采集安全应用层分类的离线前置能力，但 Facebook Page 正式发布仍未验收可用。任务 `107` 的旧诊断不能被 v2 追溯补全，防重锁保持不变；任务 `102`、`104`、`105`、`106` 的历史授权也都不能复用。

如需最后一次真实验收，必须重新准备与历史任务不同的全新内容并完成新预检，再由大帅针对新预检任务明确授权；真实浏览器窗口继续与 Instagram 错开，最终按钮仍最多点击一次。只有唯一平台回读满足既定 ID、链接、发布时间、主体、内容哈希与时间窗口门槛，才能宣称通过。否则即使 v2 捕获到应用层分类，也仍按结果不明锁定，不进行第二次点击。

## 后续：Facebook Page API 零写入资格门

大帅决定先评估 API 路线，不再用公开发布碰运气。鞋匠因此增加了独立于浏览器发布器的只读探针：它从本地数据库以 SQLite `mode=ro` 读取目标账号，只向固定 Graph 根地址 GET `/me/permissions` 与 `/me/accounts?fields=id,name,tasks`；用户令牌只进入 `Authorization` 请求头，不进入 URL、参数、输出或日志，也不请求或投影 Page access token。

探针将证据严格分层：scope、精确 Page 和 `PROFILE_PLUS_CREATE_CONTENT` task 全部存在，只能得到 `pagePermissionCheckPassed=true`；由于这两次 GET 不能证明令牌由哪个 Meta App 签发，也不能证明 App Review 或生产授权，输出仍固定为 `qualified=false / appAuthorizationVerified=false / apiModeUsable=false`，CLI 退出码保持非零。手工令牌入口改为 `check-token`，只通过隐藏输入在当前进程内核对，明确返回 `credentialStored=false`，不会把未知 App 的令牌写入钥匙串。

正式账号 `14` 的零写入实跑回读到 Page「墨白」和固定 Page ID `1200693729803807`，当前为 `authorization_required`。因为本机没有该 API 路线的已核验用户授权，所以没有发起 Graph 请求，更没有打开浏览器或操作平台内容。聚焦测试 `17/17`、Facebook/Meta 串行测试 `382/382`、完整离线回归 `3118/3118` 通过；独立复审未发现阻断问题。

在地区门回读之前，这一结果只代表 API 资格尚未核验，而不是资格不通过；当时计划进入 Meta App 身份、产品配置与 OAuth 权限核对。后续实际入口结果以下方“Meta 开发者地区门回读”为准，完成前始终不得称 API 可用，也不得据此创建、上传或发布任何内容。

本次后续离线实现没有打开真实 Facebook 或 Instagram 浏览器，没有上传、公开点击、只读核对或更改现有 Facebook 内容，也没有提交、推送、合并、打包或安装。

### Meta 开发者地区门回读

大帅随后在 Meta 官方开发者入口直接回读到“Meta 开发者未对这一地区开放”。该页面位于 App 创建与 OAuth 之前，所以当前阻断点是 Meta 开发者准入门，而不是本地 Graph 探针、Page 权限或发布实现。提示本身没有公开具体判定字段，当前证据不能区分网络位置、账号地区或开发者资格，也不通过伪造地区、借用账号或其他规避方式继续。

因此，自建 Meta App/API 路线当前标记为外部门阻断，不能进入 scopes、Page task 或 App Review 实测。浏览器正式发布仍未验收可用，历史任务和授权仍不得重放。若改走已有 Meta 正式授权的第三方发布服务，属于新的账号授权、数据处理和可能付费范围，必须先单独做零连接可行性评估并由大帅明确决定。
