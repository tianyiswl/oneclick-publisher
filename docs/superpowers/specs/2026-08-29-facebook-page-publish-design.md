# Facebook Page 单视频受控发布通道设计

日期：2026-08-29

状态：设计已完成，等待书面批准；尚未实施

目标版本：下一次海外功能集成版（功能分支不改版本号）

## 1. 背景与当前事实

一键发已经有 Meta Business Suite 的本地浏览器会话、视频上传器和通用受控发布任务，但当前实现不能证明“发布到了指定 Facebook Page”，也不能作为 Facebook Page 正式发布通道使用：

1. 登录入口把 Instagram 和 Facebook 合并为一个 Meta 账号，并强制两者同时可用。
2. 一次登录会无条件生成 Instagram 与 Facebook 两条账号记录，即使用户实际上只有 Facebook Page。
3. Facebook 账号记录没有保存稳定 Page ID；多个 Page 时无法精确选择，也无法防止会话切换到另一个 Page。
4. 现有上传器只勾选文字中含 `facebook` 的通用目标，不能区分同一 Meta 主体下的多个 Page。
5. 预检只要上传器没有抛异常就可能被判为成功，没有逐项回读 Page、视频、正文、公开范围和最终按钮。
6. CLI/MCP 的一次性授权没有绑定 Facebook Page 身份，当前 Meta 专用确认也没有被统一转换成服务端许可。
7. 现有成功判断接受通用提示或页面跳转，不能唯一回读同一 Page 新增的 Reel ID 或 URL。
8. 最终动作后的结果不明、人工验证、进程中断和防重复还没有形成稳定状态合同。

因此，本设计不是给现有 Meta 通道换一个名称，而是把“Facebook Page 身份、授权、填写、回读和防重复”贯穿登录到任务终态。

## 2. 本轮目标

首轮只完成一条最小但完整的主链：

- 只支持 Facebook Page，不支持个人主页或专业模式个人账号；
- 一个已绑定 Page、一个本地视频、立即公开发布；
- 在一键发独立可见浏览器中复用本地 Meta 会话；
- 登录后精确发现并绑定稳定 Page ID；
- 桌面 UI、受控 CLI 和 MCP 调用同一个发布服务；
- `preflight` 使用与正式发布相同的页面填写与回读逻辑，但绝不点击最终发布按钮；
- `formal` 必须持有与内容快照、账号和 Page ID 绑定的一次性短期授权；
- 最终发布按钮最多点击一次；
- 只有同一 Page 内容列表中唯一新增的 Reel ID/URL 回读成功，才记为发布成功；
- 结果不明时停止自动重试，先只读核对。

## 3. 不在本轮范围

- Facebook 个人主页、专业模式个人账号、群组和 Marketplace。
- Instagram 自动绑定、Instagram 同步发布或登录时自动生成 Instagram 账号记录。
- 多 Page 同时发布、多视频、图文、纯文字、Story 和直播。
- 定时发布、自定义封面、AI 内容声明、地点、商品、协作者和广告推广。
- Graph API、Page Access Token、应用审核和平台权限申请。
- 删除、隐藏、编辑或自动重发已经提交的作品。
- 后台无界面运行。首轮浏览器保持可见，平台验证和身份切换必须让用户看见。
- 在设计批准前登录 Facebook、上传视频或执行任何真实平台动作。

内容包若包含首轮不支持的设置，必须在打开浏览器前返回稳定错误，不能静默忽略。

## 4. 路线比较与结论

### 4.1 Meta Business Suite 受控浏览器（本轮选择）

复用一键发自己的隔离会话，在 Meta Business Suite 中选择精确 Page、填写视频表单并回读内容列表。该路线能利用当前代码和用户已有登录方式，最快形成可验证闭环；代价是 Meta 页面改版后需要更新选择器和回读合同。

### 4.2 Facebook Graph API（未来备选）

官方 API 更适合规模化、多客户和完全后台运行，但需要开发者应用、Page 权限、用户授权、审核和令牌治理。本轮没有这些前提，不能把未经审核的 API 路线当成当前可交付能力。

### 4.3 Facebook 个人主页自动发布（不采用）

用户当前是个人 Facebook 账号并不等于发布目标应是个人主页。Page 提供明确的管理主体、内容列表和作品回读边界，更适合客户账号与矩阵发布。首轮禁止把个人主页当作 Page 猜测发布。

## 5. 架构证据

本设计涉及外部平台、持久化会话、异步任务、失败恢复和人工确认，已建立两份 Archify 证据：

- [Facebook Page 发布架构图](../../architecture/facebook-page-publish.architecture.html)
- [Facebook Page 发布状态生命周期图](../../architecture/facebook-page-publish.lifecycle.html)

对应可审计源文件：

- `docs/architecture/facebook-page-publish.architecture.json`
- `docs/architecture/facebook-page-publish.lifecycle.json`

图中明确标出内容项目、桌面 UI、CLI/MCP、共用发布服务、Page 身份服务、Meta 上传器、本地会话、任务数据库、Meta Business Suite、内容列表回读、人工验证点和结果不明防重复状态。架构图是设计与审查证据，不替代自动测试和真实平台验收。

## 6. Facebook Page 账号身份合同

### 6.1 一条账号记录只代表一个 Page

Facebook Page 使用现有 `type=9`，一条记录只绑定一个 Page：

| 业务字段 | 本地字段 | 规则 |
| --- | --- | --- |
| `pageId` | `accountReference` | 必填；稳定身份和授权绑定主键 |
| `pageName` | `userName` | 平台回读名称，只用于显示与辅助核对 |
| `profileName` | `profileName` | 用户在一键发中的主体名称 |
| `avatarFileName` | `avatarPath` | 可空；只保存本地文件名 |
| `sessionRef` | `filePath` | 必填；只保存本地会话文件的不透明引用 |
| `status` | 现有账号状态字段 | 正常、异常或需重新绑定 |

首轮不新增 `pageUrl` 数据列。后台链接由保存的 `pageId` 和当前平台路由生成，避免把易变化 URL 当稳定身份。若真实页面无法用 Page ID 安全生成入口，再在实施评审中单独迁移字段，不把 URL 塞进其他含义不符的列。

同一 Meta 会话可以管理多个 Page；这些 Page 可以安全共用同一个 `sessionRef`，但每个 Page 必须分别保存为独立 `type=9` 账号，并分别参与发布授权和回读。

同一个非空 `pageId` 在当前数据库中只能对应一条有效 `type=9` 记录。实施时必须为 `(type=9, accountReference=pageId)` 建立幂等 upsert 和数据库级唯一约束；重复登录同一 Page 只能更新原记录，不能创建第二个 accountId。发布意图和防重复以 `pageId` 为主体，不能通过换一个本地 accountId 绕过。

### 6.2 登录后的 Page 发现

账号入口改为单独的“Facebook Page”，不再把 `type=9` 映射成 Instagram/Meta `type=8`：

- 发现 0 个可管理 Page：停止并返回 `facebook_page_not_found`；
- 发现 1 个 Page：自动绑定并向用户显示 Page 名称；
- 发现多个 Page：必须显示 Page ID/名称选择，用户明确选择一个后才保存；未选择返回 `facebook_page_selection_required`；
- 首轮一次登录只绑定一个 Page。用户需要添加另一个 Page 时重新执行绑定流程，避免批量绑定扩大首轮 UI 和重复记录风险。

登录只验证 Facebook Page 可管理，不要求 Instagram 存在，也不自动创建 Instagram 行。

### 6.3 同一 Page 核对

登录保存、重启检测、打开后台、预检和正式发布都必须用 `pageId` 比对同一 Page。显示名称、头像、页面中出现“Facebook”文字或通用 Business Suite 路由都不能单独通过身份校验。

会话仍有效但目标 Page 已不可管理时，账号标记异常并返回 `facebook_page_content_permission_missing`。当前会话切换到其他 Page 或页面回读的 Page ID 不一致时，返回 `facebook_page_identity_mismatch`，不得猜测、替换或继续发布。

### 6.4 旧数据迁移

旧 `type=9` 记录若没有 `accountReference/pageId`：

- 保留原记录和会话文件，不删除、不改成 Instagram；
- 状态标记为“需重新绑定”；
- 账号管理中以异常颜色显示，并提供重新绑定入口；
- 重新绑定时必须由页面回读 Page 列表并由用户选择，不能用旧名称自动猜 Page；
- 未完成重新绑定前不能用于预检或正式发布。

迁移必须幂等；共享会话文件仍被其他账号引用时，删除单条账号不得删除会话文件。

## 7. 桌面界面设计

### 7.1 账号管理

- 登录入口显示“Facebook Page”，与 Instagram 分开。
- 多 Page 发现后显示明确选择列表；每项至少包含 Page 名称和脱敏后的 Page ID 尾段。
- 正常账号显示 Page 名称；异常或需重新绑定账号沿用项目规则，以红色字体提示。
- “检测登录”和“打开后台”都绑定保存的 Page ID；不能打开通用 Composer 后就声称目标正确。
- 重新登录若发现 Page ID 与旧绑定不一致，保留旧记录并要求用户重新选择，不静默覆盖。

### 7.2 发布中心

Facebook Page 首轮只显示并接受：

- 一个 Facebook Page 账号；
- 一个视频；
- 标题/正文/结构化话题按 Meta 当前支持方式组合后的最终文案；
- 立即发布；
- 公开范围。

定时、封面、AI 声明等控件在未支持时应隐藏或明确禁用，并显示“当前版本暂不支持”，不能把用户选择传给上传器后静默丢弃。

## 8. 统一受控接口合同

### 8.1 输入

桌面 UI、CLI 和 MCP 都调用 `publish_service` 的同一 Facebook Page 执行服务。受控输入至少包含：

```json
{
  "manifestPath": "/absolute/path/to/manifest.json",
  "mode": "preflight",
  "targets": [
    {
      "platform": "Facebook",
      "accountId": 123,
      "schedule": null,
      "settings": {
        "visibility": "public"
      }
    }
  ]
}
```

调用方只传一键发账号 ID，不直接传 Cookie、密码、Page Access Token 或任意 Page ID。服务层从账号记录解析 `facebookExpectedPageReference`，并将它写入标准化任务载荷、发布快照和授权指纹。

稳定的 `publishIntentFingerprint` 至少包含：

- manifest 与目标平台覆盖字段的规范化哈希；
- 视频文件哈希；
- 一键发账号 ID；
- Facebook Page ID；
- 最终文案和结构化话题；
- 公开范围；
- 立即/定时的发布意图。

`publishIntentFingerprint` 不包含瞬时运行阶段 `preflight/formal`，否则预检和正式任务天然无法匹配。正式授权另外绑定成功的 `preflightTaskId`、预检回执哈希、允许执行的 `formal` 范围和到期时间；调用模式仍必须单独校验。上述发布意图字段任一变化，或预检任务/回执不匹配时，已有正式授权必须失效。

### 8.2 输出

CLI、MCP 和桌面任务详情使用相同语义的稳定 JSON：

```json
{
  "taskId": 123,
  "platform": "Facebook",
  "phase": "published_readback_confirmed",
  "status": "success",
  "errorCode": "",
  "errorMessage": "",
  "receipt": {
    "accountId": 123,
    "pageId": "stored-page-id",
    "pageName": "Example Page",
    "reelId": "platform-reel-id",
    "url": "https://www.facebook.com/reel/...",
    "publishedAt": "2026-08-29T12:00:00+08:00",
    "visibility": "public",
    "platformWriteOccurred": true,
    "finalActionTriggered": true
  }
}
```

没有取得平台 Reel ID/URL 时保持 `null`，不能用本地任务 ID 或页面路由伪造平台回执。错误原文保存在任务结果中，但不得包含 Cookie、令牌、二维码、验证码、完整会话内容或不必要的正文。

### 8.3 原子正式任务占位

正式请求在启动浏览器前，必须在同一数据库事务内完成三件事：

1. 校验并消费一次性授权；
2. 创建正式任务记录；
3. 以 `(platform=Facebook, pageId, publishIntentFingerprint)` 创建唯一正式执行 claim。

只有事务整体提交成功的任务才能进入浏览器。并发请求、第二份授权或另一个本地 accountId 若命中同一 Page 和发布意图，必须返回 `facebook_duplicate_submit_blocked`。

claim 至少区分：

- `reserved`：正式任务已原子占位，尚未进入最终动作临界点；
- `final_action_claimed`：已在点击前保存 Page 内容基线和最终页面快照，进入保守不可重放边界；
- `final_action_clicked`：浏览器点击调用已经返回；
- `ambiguous`：进入不可重放边界后进程中断或结果无法确认；
- `succeeded`：唯一 Reel 回读成功；
- `safe_failed`：有持久化证据证明未进入最终动作，可在新预检和新授权后重试。

claim 历史不能靠删除掩盖。只有 `safe_failed` 不再占用活动防重槽；`final_action_claimed`、`final_action_clicked`、`ambiguous` 和 `succeeded` 都持续阻止自动重发。

## 9. 本地检查、平台预检与正式发布

### 9.1 打开平台前的本地检查

无论 `preflight` 还是 `formal`，都先完成：

1. 解析结构化 manifest；
2. 核对单 Page、单视频、立即公开发布范围；
3. 验证账号状态、Page ID 和会话引用；
4. 验证视频存在、可读、哈希可计算；
5. 拒绝定时、封面、AI 声明等未支持设置；
6. 生成不可变发布快照和指纹。

本地检查失败时不得打开浏览器。

### 9.2 平台预检

`mode=preflight` 使用与正式发布相同的 Page 核对、上传、表单填写和字段回读实现：

1. 打开可见 Meta Business Suite 会话；
2. 回读并确认目标 Page ID；
3. 选择 Facebook Page 作为唯一发布目标；
4. 上传一个视频并等待平台确认完成；
5. 填写最终文案、设置公开范围；
6. 回读目标 Page、视频、完整文案、公开范围和最终按钮可用状态；
7. 在最终发布按钮前停止，绝不点击。

预检可能向平台上传临时素材或形成未提交草稿，因此回执必须标记 `platformWriteOccurred=true`、`finalActionTriggered=false`。本轮不自动删除平台临时内容，避免把测试清理变成新的破坏性动作。

预检成功只代表当前 Page 表单在最终按钮前已核对，不代表已经发布。

### 9.3 正式发布

`mode=formal` 必须携带由客户端为同一快照生成、短期有效且只能消费一次的授权凭证：

1. 完成第 8.3 节的原子事务，并确认发布意图指纹、账号 ID、Page ID 和正式授权范围完全一致；
2. 重新实时核对 Page 身份和会话权限；
3. 使用与预检相同的上传、填写与回读合同；
4. 在最终按钮前再次比对页面值与授权快照；
5. 用同一浏览器上下文的只读页面取得目标 Page 已发布内容列表基线；基线不完整或 Page 不一致时返回 `facebook_page_baseline_read_failed`，不得点击；
6. 在事务中把 claim 更新为 `final_action_claimed`，保存基线哈希和最终页面快照；
7. 最多点击一次最终发布按钮，点击调用返回后立即记录 `facebook_final_action_clicked`；
8. 等待平台明确反馈，并从同一 Page 的已发布内容列表做只读回读；
9. 只有出现一个与点击前基线相比唯一新增、且内容指纹和时间窗口匹配的 Reel，才写入成功回执。

CLI/MCP 不能通过传入布尔字段伪造 Meta 专用确认；内部执行许可只能由一次性授权消费结果生成。UI 的人工确认也必须转换成同一种内部许可，不能维护第二套发布逻辑。

## 10. 页面填写与逐字段回读

### 10.1 Page 目标

上传器只能选择与 `facebookExpectedPageReference` 对应的 Page。仅勾选“Facebook”、仅看到 Page 名称或只有一个可见复选框都不足以通过；必须从当前页面可获得的稳定标识回读同一 Page。

发现 0 个、多个歧义目标或身份不一致时停止，不点击最终按钮。

### 10.2 视频

上传前记录本地文件名、大小和哈希。平台页完成上传后至少回读文件名或可对应的唯一视频预览状态，并确认没有第二个意外素材。只看到“上传完成”通用文字不能单独通过。

### 10.3 文案与话题

服务层只生成一次最终文案。填写前清空编辑器并回读为空，随后写入一次并比较归一化后的完整文本、顺序和重复片段。

如果 Meta 当前页面将结构化话题作为普通文案接受，回读合同必须明确其实际平台形态；不能把不存在的官方话题实体伪报为已选择。正文中原始 `@` 不得在没有独立提及字段和官方回读的情况下冒充有效提及。

### 10.4 公开范围与最终按钮

首轮只接受 `public`。平台页必须回读公开范围和最终按钮可用状态；任一字段不一致返回 `facebook_page_form_readback_failed` 并停止。

## 11. 人工验证和浏览器可见性

正常上传和填写使用可见浏览器。出现登录过期、扫码、二次验证、CAPTCHA、安全检查、Page 权限确认或无法识别的阻断弹窗时：

1. 保留并显示同一个浏览器页面；
2. 任务进入 `waiting_user_verification`；
3. CLI/MCP 返回任务 ID、平台、阶段、人工操作类型和有效等待时间；
4. 不返回二维码像素、验证码、Cookie 或会话文件；
5. 用户完成验证后继续同一 Page 和同一上传，不重复上传；
6. 超时或页面不可恢复时，依据是否已点击最终按钮进入明确失败或结果不明。

## 12. 状态机、结果核对与防重复

### 12.1 关键状态

- `local_validation_passed`：本地内容、账号和范围检查通过；
- `waiting_user_verification`：等待用户处理 Meta 验证；
- `platform_form_verified`：真实 Page 表单回读通过，最终按钮未点击；
- `final_action_claimed`：点击前基线和页面快照已持久化，进入保守不可重放边界；
- `final_action_clicked`：浏览器点击调用已经返回；
- `platform_accepted`：Meta 页面出现明确接受反馈；
- `published_readback_confirmed`：同一 Page 唯一新增 Reel 回读成功；
- `failed`：有明确错误且没有需核对的最终动作；
- `ambiguous`：最终动作已触发但无法证明成功或失败。

### 12.2 唯一成功标准

正式任务在 `final_action_claimed` 之前保存同一 Page 已发布内容列表的完整只读基线。基线失败是点击前硬门。点击后必须取得：

- 相同 Page ID；
- 点击后新增且唯一的 Reel ID/URL；
- 与当前内容指纹相符的标题/文案或视频特征；
- 合理的发布时间窗口。

通用成功提示、页面跳转、进入内容列表、上传完成、最终按钮消失或程序退出码 `0` 都不能单独证明发布成功。

### 12.3 结果不明与进程恢复

进入 `final_action_claimed` 后，如果没有取得唯一 Reel 回读，也没有平台明确失败证据：

- 任务进入 `ambiguous`；
- 返回 `facebook_publish_outcome_unknown`；
- 同一发布指纹自动重发被阻止；
- 首选只读重新核对同一 Page 内容列表；
- 只有确认没有发布且用户重新授权，才允许新正式任务。

CLI 中断、工作进程退出、线程异常或任务租约过期不得留下永久 `running`：claim 仍为 `reserved` 且事件记录证明未进入最终临界点时进入 `safe_failed`；已经 `final_action_claimed` 时即使没有 `final_action_clicked` 证据，也保守进入 `ambiguous`。

## 13. 稳定错误码

首轮冻结以下语义：

- `facebook_account_invalid`：账号不存在、类型不符或本地状态异常；
- `facebook_session_missing`：账号缺少可用本地 Meta 会话引用；
- `facebook_page_not_found`：当前会话没有可管理的 Facebook Page；
- `facebook_page_selection_required`：发现多个 Page 但用户尚未选择；
- `facebook_page_identity_mismatch`：保存的 Page ID 与真实页面不一致；
- `facebook_page_content_permission_missing`：目标 Page 存在但当前会话没有所需内容权限；
- `facebook_video_file_invalid`：视频不存在、不可读或不满足首轮输入合同；
- `facebook_unsupported_publish_setting`：请求包含定时、封面、AI 声明等未支持设置；
- `facebook_publish_authorization_invalid`：授权过期、已消费或与预检、Page、发布意图不匹配；
- `facebook_duplicate_submit_blocked`：同一 Page 和发布意图已有活动、不明或成功 claim；
- `facebook_upload_failed`：平台没有完成唯一视频上传；
- `facebook_page_baseline_read_failed`：最终点击前无法取得同一 Page 的完整内容列表基线；
- `facebook_page_form_readback_failed`：Page、视频、文案、公开范围或最终按钮回读不一致；
- `facebook_verification_required`：仅作为 `waiting_user_verification` 的 `actionRequired.code`，不是终态失败；
- `facebook_verification_timeout`：人工验证超时且未进入最终动作；
- `facebook_publish_rejected`：最终动作后平台给出明确拒绝，并经只读核对确认未发布；
- `facebook_publish_readback_mismatch`：存在新增内容，但不能唯一匹配本任务；
- `facebook_publish_outcome_unknown`：进入最终动作临界点后无法证明成功或失败。

等待人工验证时 `status=waiting_user_verification`、`errorCode` 为空，并通过 `actionRequired.code=facebook_verification_required` 提示用户。平台错误原文保留为 `errorMessage` 供诊断，但不得用动态页面原文替代稳定 `errorCode`。

## 14. 安全与隐私边界

- 密码、Cookie、验证码、二维码、Page Access Token 和会话内容不得出现在命令行参数、内容包、Git、任务 JSON 或普通日志中。
- CLI/MCP 只接受一键发内部账号 ID；会话凭据只由客户端既有凭据存储解析。
- Page ID 可作为非秘密的稳定身份进入任务快照和回执；用户界面默认只展示必要尾段。
- 正式发布必须在最终动作前具有当次一次性授权；开放本地接口不能绕过人工边界。
- 最终动作之后禁止自动重发；任何只读核对不得修改、删除或编辑平台内容。
- 真实正式验收会产生公开 Page Reel，必须在当次再次取得大帅明确授权。

## 15. 计划代码边界与集成规则

海外登录发布线可直接修改并形成专属提交：

- `app_core/overseas_meta_page_identity.py`：Page 发现、选择、同一主体核对和旧记录迁移；
- `app_core/overseas_meta_errors.py`：稳定错误类型和错误码映射；
- `app_core/overseas_browser_publish.py`
- `app_core/overseas_preflight.py`
- `uploader/meta_uploader/main.py`
- Facebook/Meta 海外定向测试、架构证据和状态文档。

以下共享核心或海外专属范围之外的文件，若确需修改，必须按主题拆成独立共享提交，运行共享核心质量门，并转交集成线审查合并；海外功能线不得自行把这些提交合入 `main`：

- 账号与登录：`app_core/account_service.py`、`app_core/login_service.py`、`app_core/account_browser_service.py`、`myUtils/login.py`、`myUtils/auth.py`、`myUtils/avatar.py`、`ui/login_dialog.py`、`ui/account_page.py`；
- 任务与授权：`app_core/database.py`、`app_core/oneclick_authorization.py`、`app_core/controlled_publish.py`、`app_core/publish_service.py`、`app_core/task_service.py`；
- UI 与接口：`ui/publish_page.py`、`desktop_native_app.py`、`app_core/content_project_gateway.py`、`app_core/oneclick_mcp_server.py`。

建议至少拆成“Page 身份/数据库共享提交”“受控任务/授权共享提交”“发布中心与接口共享提交”三组，不能把海外上传器和全部共享核心混成一个不可审查提交。MCP 与 Gateway 继续使用现有通用转发结构，只补标准化字段和契约测试，不复制浏览器自动化。UI、CLI、MCP 必须共用同一 Page 身份服务、同一上传器和同一成功回读标准。

## 16. 实施切片

设计获书面批准后，按以下顺序实施：

1. **Page 身份与登录**：拆分 Facebook Page 登录入口，完成 0/1/多 Page 发现、精确绑定、旧记录需重新绑定和测试。
2. **受控任务与授权**：建立 Page 级唯一记录和原子正式执行 claim，把 Page ID 写入标准化任务、快照、发布意图指纹和回执，统一 UI/CLI/MCP 内部许可。
3. **页面填写与预检**：实现 Page 精确选择、视频/文案/公开范围逐项回读和最终按钮前停止。
4. **正式动作与回读**：持久化最终点击事件、唯一 Reel 只读回读、结果不明和防重复状态。
5. **界面与全量回归**：接入账号管理和发布中心，运行海外定向测试及受影响模块测试。
6. **真实验收**：先做 Facebook Page 登录和预检；正式公开测试视频必须另行取得当次授权。

每个切片先写失败测试，再做最小实现；不得为了通过测试保留“同时生成 Instagram 行”等旧错误行为。

## 17. 验收标准

### 17.1 离线验收

- Facebook-only 会话没有 Instagram 时可以完成登录。
- 0 个、1 个、多个 Page 的发现与选择分别符合合同。
- 登录只新增选中的 `type=9` Page 记录，且稳定保存 Page ID。
- 重复绑定同一 Page 只能更新同一记录，数据库不能出现两个有效 accountId。
- 同名不同 Page、会话切换 Page 和 Page 权限丢失都不能误判正常。
- 旧 type=9 缺 Page ID 时保留记录并标记需重新绑定，不能进入发布。
- CLI/MCP 未授权时在浏览器前失败；授权消费后只能发布绑定的 Page 和快照。
- Page ID、内容、视频、范围或正式授权范围变化会使授权失效。
- 预检与正式共用不含运行阶段的发布意图指纹；正式授权另外绑定成功预检 taskId 和回执哈希。
- 授权消费、正式任务创建和 Page 级唯一 claim 在同一事务完成，并发请求只能有一个成功。
- UI、CLI、MCP 最终只调用一次相同 Facebook Page 服务。
- 预检与正式共用 Page、视频、文案、公开范围和最终按钮回读逻辑。
- 预检绝不点击最终按钮，并明确报告可能发生的平台临时写入。
- 自定义封面、定时、AI 声明等未支持设置在浏览器前失败。
- 最终按钮最多点击一次；只发生页面跳转不能判成功。
- 同一 Page 的点击前内容列表基线读取失败时不得进入最终动作。
- `final_action_claimed` 与 `final_action_clicked` 分开记录；两者之间进程中断进入结果不明。
- 最终点击后超时进入 `ambiguous` 并阻止同指纹自动重发。
- 人工验证结束后继续同一 Page、同一页面和同一上传。
- CLI 中断、线程异常和租约过期后任务进入明确终态。
- 成功回执包含同一 Page 的真实 Reel ID、URL 和发布时间。
- 海外定向测试和受影响模块测试通过；局部通过不得写成全量验证完成。

### 17.2 真实平台验收

真实验收分层报告，不能越级：

1. **登录验收**：在可见窗口登录并绑定一个 Facebook Page；关闭并重启源码客户端后，只读回读同一 Page ID、名称和头像。
2. **平台预检**：使用一个明确测试视频上传并填写表单，回读 Page、视频、文案、公开范围和最终按钮，在最终动作前停止。
3. **正式公开验收**：只有大帅另行明确授权一个可公开测试视频后，点击一次最终按钮；从同一 Page 已发布内容列表取得唯一 Reel ID/URL。
4. **结果不明演练**：用离线或受控模拟验证进程中断和只读核对，不为演练再次公开发布。

完成第 1 项只能写“Facebook Page 登录可用”；完成第 2 项只能写“真实发布前表单回读通过”；只有第 3 项取得平台回执后，才能写“Facebook Page 公开发布已验证”。

## 18. 回滚与兼容

- 新 Page 绑定逻辑用独立服务和特性开关接入；真实验收前默认不向普通用户开放正式发布。
- 旧 Meta 共用会话文件继续保留，不自动删除；旧 Facebook 行只标记需重新绑定。
- 若平台页面改版导致 Page 身份或表单回读失效，关闭 Facebook Page 发布入口，保留账号数据和任务回执。
- 回滚代码不能清理会话文件、删除 Page 记录或把失败任务改写成成功。
- Phase 1 稳定后再单独设计定时发布、封面和 Instagram 联动，不在本轮顺带扩张。

## 19. 参考

- [Facebook Page 访问权限说明](https://www.facebook.com/help/289207354498410/)
- [在个人资料和 Page 之间切换](https://www.facebook.com/help/510247025775149/)
- [Meta Business Suite 发布内容](https://www.facebook.com/business/help/942827662903020)
- [Facebook Reels 业务帮助](https://www.facebook.com/business/help/794942355314453)
- [Meta Business Suite 内容列表](https://www.facebook.com/business/help/500155770399970)
