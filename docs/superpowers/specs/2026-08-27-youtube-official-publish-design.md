# YouTube 官方发布通道与账号资料修复设计

日期：2026-08-27

状态：待书面确认

目标版本：下一次集成发布版本（不在功能设计阶段提前改版本号）

## 1. 背景与当前事实

当前一键发已经完成 YouTube 官方桌面 OAuth 登录，并能在客户端重启后通过刷新令牌识别同一频道。现有能力仍有两个断点：

1. 频道接口已经返回头像信息，但 `YouTubeChannelIdentity` 只保留频道 ID 和频道名称，头像地址被丢弃；账号页又把 OAuth 账号的“刷新头像”和“打开后台”明确禁用了。
2. 当前 YouTube API 适配器只具备“私密上传并回读视频 ID”的最小能力。发布中心仍把 YouTube 交给旧的可见浏览器发布流程，受控 CLI/MCP 也没有 YouTube 专属发布设置和官方 API 终态回执。

因此本次目标不是让旧浏览器脚本多点几个按钮，而是建立一条只使用 YouTube 官方 API 的发布通道，并让桌面 UI、CLI 和 MCP 共用同一服务。

## 2. 目标

本次设计交付以下能力：

- 账号页可从 YouTube 官方频道信息刷新头像，并用系统默认浏览器打开对应 YouTube Studio。
- YouTube 支持四种发布方式：公开、不公开、私密、定时公开；默认私密。
- 上传时先建立私密视频，再设置自定义封面，最后按目标方式调整可见性，避免半成品直接公开。
- 桌面 UI、受控 CLI 和 MCP 使用同一个 YouTube 发布服务、同一份输入校验和同一组错误码。
- 内容项目继续交付纯净的结构化内容包；可见性、儿童内容声明、通知订阅者和排期属于本次发布任务，不写进正文文件。
- 每个成功结果都必须回读精确视频 ID、实际可见性和实际定时时间；本地调用成功不能冒充平台发布成功。
- 第一次真实验收只上传一个无害的私密测试视频，不执行公开发布。

## 3. 不在本次范围

- 不使用浏览器自动化作为 YouTube 上传或发布的回退方案。
- 不实现播放列表、直播、社区帖子、字幕、获利设置和批量删除。
- 不在结果不明时自动重新上传或重新调整可见性。
- 不把 Google Cookie、密码、刷新令牌或验证码放进内容包、命令行、数据库公开字段或日志。
- 不用私密测试证明公开发布已经通过；公开能力仍受 Google API 项目审核状态和当次用户授权约束。

## 4. 总体结构

架构图：

- `docs/architecture/youtube-official-publish.architecture.html`
- `docs/architecture/youtube-official-publish.lifecycle.html`

主流程如下：

1. 内容项目提供 `manifest.json`、明确 YouTube 账号 ID 和发布任务设置。
2. 受控发布服务完成本地检查，并生成包含账号、内容、封面、可见性和排期的发布指纹。
3. 正式执行必须消费一次性短期授权；预检不写入平台。
4. YouTube 服务从系统凭据库读取当前账号的刷新令牌，换取短期访问令牌。
5. 视频始终先以 `private` 上传，回读精确视频 ID。
6. 若任务提供封面，调用 `thumbnails.set`，成功后再继续。
7. 按任务要求保持私密、改为不公开、改为公开，或保持私密并写入 `publishAt`。
8. 再次用精确视频 ID 回读状态，只有目标状态一致才把任务记为成功。

任何阶段失败都要进入明确终态；一旦平台已经产生视频 ID，回执必须保留这个 ID，禁止盲目重传。

## 5. 账号头像与后台入口

### 5.1 频道身份

`channels.list(part=id,snippet,mine=true)` 的解析结果扩展为：

- `channel_id`
- `display_name`
- `avatar_url`

头像从 `snippet.thumbnails` 中选择尺寸最大的有效 HTTPS 地址。解析只接收当前授权账号唯一频道；无频道、多个冲突频道或频道 ID 与本地账号不一致时停止更新。

### 5.2 头像下载

OAuth 账号刷新头像不再读取 Cookie 文件，也不启动浏览器。服务使用短期访问令牌回读频道，再通过 HTTPS 下载公开头像：

- 只允许 Google/YouTube 官方头像域名及其合法跳转目标；拒绝本地地址、非 HTTPS 和未知域名。
- 限制响应大小和图片类型，解码成功后转存为客户端统一格式。
- 先写临时文件，再原子替换正式头像，失败时保留旧头像。
- 日志只记录账号 ID、频道 ID、阶段和安全错误码，不记录带查询参数的头像地址。

数据库仍只保存本地头像路径和更新时间，不保存访问令牌。

### 5.3 打开后台

OAuth 账号的“打开后台”调用系统默认浏览器打开：

`https://studio.youtube.com/channel/{channel_id}`

若本地没有可信频道 ID，则打开 `https://studio.youtube.com/`，由 Google 当前登录态决定频道。该操作不启动一键发的 Cookie 浏览器，也不传递凭据。

## 6. OAuth 权限升级

当前账号只申请：

- `youtube.readonly`
- `youtube.upload`

这足以读取频道和上传私密视频，但不能可靠执行后续 `videos.update`。为了实现“先私密、后设置最终可见性”，需要新增 `youtube.force-ssl`。该权限范围较宽，是官方视频状态更新接口的要求；一键发内部仍只开放明确允许的频道读取、上传、封面设置、视频状态读取和可见性更新，不提供删除接口。

现有 YouTube 账号需要进行一次重新授权：

1. 客户端检测本地授权版本或实际权限集合不足。
2. 显示“需要升级 YouTube 发布权限”，不能显示为普通掉线。
3. 用户在系统浏览器完成授权。
4. 客户端用新令牌回读频道 ID；只有与原账号频道 ID 完全一致时才原子替换旧刷新令牌。
5. 频道不一致时拒绝替换，避免把账号记录绑定到另一个频道。

相关官方接口与限制参考：

- [Videos 资源与 `publishAt` 约束](https://developers.google.com/youtube/v3/docs/videos)
- [`videos.update` 授权与字段更新规则](https://developers.google.com/youtube/v3/docs/videos/update)
- [`thumbnails.set` 文件与权限要求](https://developers.google.com/youtube/v3/docs/thumbnails/set)

## 7. 内容包与发布任务合同

### 7.1 内容包保持纯净

`oneclick-content/v1` 内容包继续保存：

- 视频素材
- 标题
- 正文/简介
- 话题
- 封面
- 平台覆盖字段

可见性和排期不是作品正文，不进入 `platformOverrides`，避免同一内容包在不同账号和不同日期使用时互相污染。

### 7.2 YouTube 目标设置

受控目标增加可选 `settings`：

```json
{
  "platform": "YouTube",
  "accountId": 12,
  "schedule": null,
  "settings": {
    "visibility": "private",
    "madeForKids": false,
    "notifySubscribers": true
  }
}
```

`visibility` 允许：

- `private`
- `unlisted`
- `public`
- `scheduled_public`

规则：

- 未提供 `visibility` 时默认为 `private`。
- `scheduled_public` 必须提供带时区的 `schedule`；其他三种方式不得携带启用的排期。
- 定时时间必须晚于当前时间并留出安全缓冲，防止平台把已经过去或过近的时间立即公开。
- 正式任务必须明确提供 `madeForKids`；桌面界面的勾选框会把用户选择写成明确布尔值，不能由程序猜测内容是否面向儿童。
- `notifySubscribers` 只影响正式公开或定时公开；私密与不公开仍保留设置，但平台不适用时不伪造生效回执。

这些设置必须进入发布指纹。修改可见性、儿童声明或排期后，旧预检和旧一次性授权自动失效。

### 7.3 账号选择

不修改全局 `account_service.list_accounts()` 的旧浏览器账号语义。新增发布账号查询能力：

- YouTube 官方 API 路由可选择 `authMode=youtube_oauth` 且状态正常的账号。
- TikTok、Meta 和国内平台仍沿用各自既有账号筛选。
- OAuth 账号绝不被误送给 Cookie 浏览器执行器。

## 8. 发布状态机

### 8.1 预检

预检只做不改变平台内容的检查：

- 账号存在且频道一致。
- 权限集合满足发布要求。
- 视频存在、可读、格式和大小在客户端支持范围。
- 标题、简介和话题符合本地限制。
- 封面存在时必须为 JPEG/PNG、大小不超过官方限制，并能成功解码。
- 定时时间有效并可转换为 RFC 3339。
- Google API 项目配置存在。

预检可以做只读频道和配额可用性探测，但不得创建测试视频。

### 8.2 正式执行

正式执行分为：

1. `uploading_private`：使用可恢复上传创建私密视频。
2. `uploaded_private`：回读并保存精确视频 ID。
3. `setting_thumbnail`：有自定义封面时设置封面并确认接口成功。
4. `applying_visibility`：在读取当前完整状态后，仅对允许的状态字段做更新。
5. `verifying`：按视频 ID 回读实际可见性、定时时间和处理状态。
6. `success`、`failed` 或 `ambiguous`：写入终态。

自定义封面若已明确提供，设置失败就停止在私密状态，不允许忽略封面继续公开。没有提供封面时可使用 YouTube 自动生成的缩略图。

### 8.3 四种目标状态

- 私密：保持 `privacyStatus=private`，不写 `publishAt`。
- 不公开：更新为 `privacyStatus=unlisted`。
- 公开：更新为 `privacyStatus=public`。
- 定时公开：保持 `privacyStatus=private`，写入未来的 `publishAt`。

官方说明要求 `publishAt` 只能用于仍为私密且从未公开的视频；过去时间可能导致立即公开，因此本地必须提前拦截。

### 8.4 Google 项目审核限制

Google 对部分未通过审核的 API 项目会把上传内容强制设为私密。若用户请求公开或不公开，但精确回读仍为私密：

- 返回 `youtube_api_project_private_only`。
- 保留视频 ID 和 Studio 地址。
- 不把任务记为成功，不自动重复更新。

## 9. 失败处理与稳定错误码

至少提供以下稳定错误码：

- `youtube_oauth_scope_upgrade_required`
- `youtube_channel_identity_mismatch`
- `youtube_avatar_fetch_failed`
- `youtube_video_file_invalid`
- `youtube_thumbnail_invalid`
- `youtube_thumbnail_forbidden`
- `youtube_schedule_invalid`
- `youtube_upload_failed`
- `youtube_upload_outcome_unknown`
- `youtube_visibility_update_failed`
- `youtube_api_project_private_only`
- `youtube_readback_mismatch`
- `youtube_manual_reconciliation_required`

处理原则：

- 创建视频前失败可以安全重试。
- 已获得视频 ID 后失败，回执必须保留视频 ID；修复后只允许针对该 ID 继续封面或状态步骤，不能重新上传。
- 平台写入请求超时且不知道是否生效时，任务进入 `ambiguous`，先按账号和视频 ID 做只读核对。
- CLI 中断、线程异常或客户端退出不能让任务永久 `running`；租约过期后进入需要核对的终态。
- 任一平台任务失败不能让同批其他平台永久 `pending`。

## 10. 任务回执

桌面 UI、CLI 和 MCP 返回相同语义的 JSON：

```json
{
  "taskId": 123,
  "platform": "YouTube",
  "phase": "verifying",
  "status": "success",
  "errorCode": "",
  "errorMessage": "",
  "platformError": "",
  "receipt": {
    "videoId": "example",
    "studioUrl": "https://studio.youtube.com/video/example/edit",
    "visibility": "private",
    "scheduledAt": null,
    "processingStatus": "uploaded",
    "thumbnailApplied": true
  }
}
```

`success` 只表示精确 ID 的实际状态与任务目标一致。上传接口返回 2xx、取得 ID、封面接口返回成功或本地线程退出，都不能单独升级为发布成功。

## 11. 界面行为

### 11.1 账号管理

- YouTube OAuth 账号启用“刷新头像”和“打开后台”。
- 权限不足显示“需要升级发布权限”。
- 掉线、频道冲突、权限不足分别显示，不再统一写成登录失败。

### 11.2 发布中心

- 选择 YouTube 后显示可见性：私密（默认）、不公开、公开、定时公开。
- 选择定时公开后才显示排期控件。
- 儿童内容声明必须明确选择。
- 显示上传、封面、可见性和最终回读四个阶段，不用浏览器窗口代替进度反馈。
- 需要重新授权时只打开一次系统浏览器；完成后自动继续原任务的检查，不要求用户重新填写内容。

## 12. 测试策略

实施时严格先写失败测试，再写实现。

### 12.1 单元测试

- 频道头像选择：多尺寸、缺失、异常协议、频道冲突。
- 头像下载：域名跳转、大小、类型、原子替换、失败保留旧文件。
- 系统浏览器打开正确 Studio 地址。
- OAuth 权限升级、同频道替换和不同频道拒绝。
- YouTube 目标设置校验及发布指纹变化。
- OAuth 账号只进入 YouTube 官方发布路由，不进入旧浏览器路由。
- 私密上传、封面、四种可见性、定时转换和精确回读。
- 审核项目强制私密、回读不一致、网络超时、进程中断和防重复。

### 12.2 服务与入口测试

- UI、CLI、MCP 三个入口调用同一服务。
- 预检全程不创建视频。
- 正式任务必须消费匹配的一次性授权。
- 结果 JSON 字段、阶段、终态和错误码稳定。
- 同批其他平台不因 YouTube 失败而永久等待。

### 12.3 回归测试

- 先跑 YouTube 定向测试。
- 再跑海外发布、账号页、受控发布和任务恢复模块。
- 共享核心合并后跑完整离线测试、离屏客户端和双端构建安全测试。

## 13. 真实验收顺序

1. 在源码联调客户端对现有专用测试频道完成一次权限升级，回读频道 ID 不变。
2. 刷新头像并确认账号页显示更新；点击“打开后台”进入正确频道的 Studio。
3. 用一个无害短视频和测试封面执行私密发布。
4. 按视频 ID 回读 `privacyStatus=private`，并在 Studio 内容列表看到同一视频。
5. 记录视频 ID、私密状态、封面结果和处理状态；不执行公开、不公开或定时公开。
6. 公开、不公开、定时公开只先做离线接口测试；后续必须由大帅对具体测试内容和方式单独授权，并先确认 Google API 项目审核能力。

这次真实验收达到的是“私密上传运行成功”，不能写成“YouTube 四种方式均已真实发布验证”。

## 14. 迁移与回滚

- 保留现有 TikTok/Meta 浏览器发布逻辑；YouTube OAuth 账号只走新官方 API 服务，没有静默浏览器回退。
- 旧 YouTube 账号记录保留，首次发布前提示一次权限升级。
- 新服务可以通过功能开关关闭；关闭后账号登录、头像刷新和打开 Studio 仍可使用，发布入口明确显示未启用。
- 若封面或可见性步骤失败，平台上已经创建的视频保持私密，并把视频 ID 交给用户人工核对；不删除、不重传。
- 正式客户端发布时统一递增版本；源码、Git、打包、安装和真实平台结果分别记录。

## 15. 验收标准

书面设计通过后，代码阶段至少满足：

1. OAuth 账号头像可刷新，后台按钮可打开正确 Studio。
2. 旧账号权限不足时给出明确升级提示，同频道升级成功、不同频道安全停止。
3. YouTube 四种任务设置均能通过自动测试，默认私密。
4. UI、CLI、MCP 共用官方 API 发布服务，YouTube 不再进入旧浏览器执行器。
5. 私密上传、封面设置、可见性更新和精确回读都有稳定阶段与错误码。
6. 任何结果不明都不会自动重复上传；已生成的视频 ID 始终保留。
7. 用现有专用测试账号完成一次私密视频真实上传与 Studio 回读。
8. 完整离线回归、离屏客户端和构建安全测试通过后，才进入版本升级和打包。
