# YouTube 官方 OAuth 与 API 设计

## 结论

YouTube 首个可验证版本改用 Google 官方“桌面应用 OAuth 2.0 + YouTube Data API v3”。登录在系统默认浏览器完成，客户端只监听 `127.0.0.1` 随机端口接收一次性回调；发布适配器首版只允许单账号、单视频、立即、私密上传，并按本次返回的 `video ID` 回读结果。

现有 Playwright/YouTube Studio 浏览器通道保留，但必须由上层明确选择，OAuth/API 失败时不得静默切回浏览器，也不得自动再次创建视频。

架构图：[2026-08-23-youtube-oauth-api-v3.architecture.html](2026-08-23-youtube-oauth-api-v3.architecture.html)

## 要解决的缺口

当前 YouTube 登录依赖 Chrome for Testing 内的 Google 网页登录，真实验证已经被 Google 以“此浏览器或应用可能不安全”拒绝。因此：

- 没有可验证的登录成功；
- 没有客户端重启后的同一频道识别；
- 浏览器预检会先上传视频，不能作为零写入预检；
- 没有基于官方 `video ID` 的确定性结果回读。

## 范围

本功能提交只新增海外专属模块、测试、ADR 和海外状态文档：

- `app_core/overseas_youtube_oauth.py`
- `app_core/overseas_youtube_api.py`
- `test_overseas_youtube_oauth.py`
- `test_overseas_youtube_api.py`
- `docs/adr/2026-08-23-youtube-oauth-api-*`
- `docs/OVERSEAS_PLATFORM_STATUS.md`

账号页入口、账号表写入、发布页选择、全局任务路由、依赖与打包属于共享核心，必须另做独立接线提交并交给集成线，本功能提交不修改也不自行合并。

本阶段不处理 TikTok、Instagram Reels、Facebook Reels；不把 YouTube 局部通过写成其他平台可用。

## 登录与凭据

### 授权请求

- 使用系统默认浏览器打开 `https://accounts.google.com/o/oauth2/v2/auth`。
- OAuth 客户端类型为 Desktop app。
- 回调地址只允许 `http://127.0.0.1:<随机端口>/<随机路径>`，不监听局域网地址。
- 每次请求生成一次性的高熵 `state` 和 PKCE `code_verifier`，挑战算法固定为 `S256`。
- 初始 scope 只申请 `https://www.googleapis.com/auth/youtube.upload`；若真实频道识别或结果回读证明必须增加只读 scope，再单独评审并明确展示，不在失败时暗中扩权。
- 请求 `access_type=offline`，并用 `prompt=consent` 获取可供重启后刷新使用的 refresh token。

### 回调安全

- 只接收一次回调，并严格核对路径、`state`、`code` 或 Google 返回的拒绝原因。
- 拒绝授权、state 错配、重复回调、超时、取消、缺少 code 都立即停止。
- 授权码、state、code verifier、access token、refresh token 和完整回调 URL 不进入异常文本、结果对象、Git 或长期日志。

### 本机存储

- 海外模块只定义凭据存储接口，测试使用内存实现。
- 正式接线必须把 refresh token 放入 macOS Keychain 或 Windows Credential Locker；数据库只保存不可反解的凭据引用和稳定频道 ID。
- 不提供明文 JSON、Cookie 或环境变量的生产降级路径。
- Desktop app 的 `client_secret` 不能当作真正秘密；即使 Google 配置提供它，也不能因此降低 PKCE、state 和系统凭据库要求。

## 重启后的登录态检测

客户端启动后根据数据库中的凭据引用从系统凭据库读取 refresh token，交换短期 access token，再调用频道身份查询。只有取得唯一、稳定的 `channel ID` 才报告已登录；频道为空、多频道歧义、令牌被撤销、刷新失败或网络结果不明确都报告需要重新授权，不复用旧的“已登录”显示。

检测结果只返回：状态、频道 ID、频道显示名、scope 和过期时间等非敏感信息。

## 零写入预检

API 路线的预检不调用 `videos.insert`，只检查：

- 精确一个账号和一个本地视频文件；
- 标题非空且最多 100 个字符；
- 描述 UTF-8 长度不超过 5000 字节；
- 可见性固定为 `private`；
- 不支持定时、公开、不公开、播放列表、自定义封面和 AI 声明；
- 分类、标签、儿童受众和通知订阅者字段可序列化；
- 当前 access token 可用且唯一频道身份可读。

首版不支持的字段必须明确报错，不能静默忽略。

## 私密上传与结果回读

### 外部动作门

`videos.insert` 会真实创建视频。调用前必须同时满足本地预检通过和上层传入当次明确授权标志；没有授权只能返回预检结果。

在 Google 项目未经审核期间，首个真实测试固定上传为 `private`。不开放 `public` 或 `unlisted`，也不把私密保存表述为公开发布。

### 可恢复上传

1. 调用 `videos.insert?uploadType=resumable` 创建一次上传会话。
2. 从响应 `Location` 读取当前会话 URI；它按敏感信息处理，不写入长期日志。
3. 上传中断时只查询或续传同一个会话。
4. 当是否创建成功不明确时返回 `outcome_unknown`，禁止再发第二次 `videos.insert`。

### 回读门

上传响应必须给出非空 `video ID`。随后只按这个 ID 调用 `videos.list`，并核对：

- `video ID` 与本次响应一致；
- `snippet.channelId` 与授权频道一致；
- 标题与请求一致；
- `status.privacyStatus` 为 `private`。

只有全部一致才进入 `uploaded_private` 或 `processed_private`。处理仍在进行时返回 `processing`，不是失败也不是公开发布；拒绝、处理失败、字段不一致或读回不明确分别返回明确失败状态。

## 状态与失败策略

最小状态序列：

`local_preflight` → `upload_started` → `uploaded_private` → `processing` → `processed_private`

失败状态：

- `authorization_denied`
- `authorization_invalid`
- `credential_unavailable`
- `channel_ambiguous`
- `upload_failed`
- `processing_failed`
- `rejected`
- `readback_mismatch`
- `outcome_unknown`

所有未知结果安全停止。API 与浏览器通道之间不自动回退，正式上传不做盲重试。

## 依赖和实现约束

海外功能模块使用现有 `requests` 与 Python 标准库完成协议层，HTTP 会话、时钟、浏览器打开器和凭据存储均可注入，以便离线测试。OS 凭据库的具体依赖留给共享接线提交，不在海外模块内新增不安全替代品。

## 验收

### 本地交付验收

- OAuth URL、PKCE、state 单次消费、拒绝/超时、令牌刷新和脱敏测试通过；
- 私密预检、频道身份、可恢复会话、精确 ID 回读、无自动回退/重复创建测试通过；
- 海外专属测试和受影响模块测试通过；
- `tools/check_workstream_scope.py --stream overseas --base origin/main` 无 `shared` 或 `outside`；
- 架构图通过 showcase 结构校验和 1440/2048 明暗主题可视检查。

### 真实平台验收

需要大帅另行完成或授权 Google Cloud 项目与 OAuth 同意屏幕配置后才能进行：

1. 系统浏览器授权成功并识别测试频道；
2. 重启客户端后仍识别同一 `channel ID`；
3. 零写入预检通过；
4. 获得当次上传授权后，上传一个私密测试视频；
5. 按本次 `video ID` 回读频道、标题和私密可见性。

任何验证码、两步验证、风控、法律协议或不明确结果都停止等待人工处理。

## 官方依据

- [Google OAuth 2.0 for Mobile & Desktop Apps](https://developers.google.com/identity/protocols/oauth2/native-app)
- [YouTube Data API: videos.insert](https://developers.google.com/youtube/v3/docs/videos/insert)
- [YouTube resumable upload protocol](https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol)
