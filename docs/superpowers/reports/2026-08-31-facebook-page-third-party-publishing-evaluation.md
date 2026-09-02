# Facebook Page 第三方发布方案评估

日期：2026-08-31（Asia/Shanghai）

## 结论

一键发应把 **Zernio（原 Late）** 作为唯一首轮受控候选，把 **Ayrshare** 作为高价回执兜底方案。当前不应直接改用 Buffer、Make、Metricool、Upload-Post、Hootsuite、Sprout 或 Zapier 做正式发布。

Zernio 最贴合当前真正的故障约束：它明确支持 Facebook Page Reel，可把本地视频通过预签名地址直接上传，发布接口有约 5 分钟请求幂等和 24 小时内容防重，并提供逐平台状态、公开链接及签名 webhook；前两个连接账号免费且无需信用卡。它仍有两个待真实资格核验的缺口：

1. 当前地区和账号能否完成 Zernio 托管的 Meta OAuth，官方没有公开地区矩阵；
2. 文档保证 `platformPostUrl`，但没有明确保证 Facebook 原生 Reel ID，因此正式验收必须继续以唯一链接和 Facebook Page 后台独立回读为准。

本次只查阅官方公开资料和当前源码契约，没有注册服务、登录、连接 Facebook、授权 Page、创建凭据、上传素材、付费、建草稿或发布内容。

## 不可放宽的验收门

第三方服务只能替换传输通道，不能降低一键发已有安全门：

- 只允许 Facebook Page Reel，不接受普通视频流帖子冒充 Reel；
- 账号连接、内容预检和正式公开发布是三次不同授权；
- 正式请求必须绑定一个全新任务、全新视频与文案哈希、目标 Page 和一次性授权；
- 请求一旦进入第三方或 Meta 的最终发布阶段，立即保留 `blocksReplay`；
- HTTP `200`、第三方任务已接收、队列成功或第三方内部 `sent` 均不能单独证明 Facebook 已发布；
- 成功至少需要第三方逐平台终态、非空唯一 Facebook 链接，并由独立只读回读确认链接对应目标 Page 的 Reel；若能同时取得原生 Reel ID则一并保存；
- 超时、断网、第三方 `error` 但可能已发布、空链接或后台未唯一命中，一律保持 `ambiguous`，禁止自动重发；
- 不记录 Cookie、Meta token、第三方 API key、请求头、正文原文或响应原文；凭据只能进入系统安全存储；
- 任务 `102`、`104`、`107` 继续结果不明并禁止重放，任务 `105`、`106` 和历史授权不得复用。

## 候选分层

| 方案 | Page Reel | 本地视频 | 唯一回执 | 防重 | 当前费用 | 结论 |
| --- | --- | --- | --- | --- | --- | --- |
| **Zernio（原 Late）** | 明确支持 `contentType: reel` | 预签名直传，最高 5 GB | 逐平台状态、`platformPostUrl`、签名 webhook；原生 Reel ID 未明确 | 约 5 分钟请求幂等 + 24 小时内容哈希防重 | 前 2 个账号免费 | **首轮受控候选** |
| **Ayrshare** | 明确支持 `faceBookOptions.reels: true` | 内置小文件/大文件上传 | 明确返回 Facebook `id`、`postUrl`；异常时有延迟核验 URL | 支持 `idempotencyKey` 拒绝重复 ID | Premium $149/月 | **高价兜底** |
| **Metricool** | 官方产品支持 Reel，API 可排程 | Swagger 有预签名上传，但可能需要单独开通 | Provider 状态含网络 ID、公开 URL和细分状态；无公开出站发布 webhook | 未见与一键发同等级的发布幂等契约 | Advanced 起 $67/月 | 只在 Zernio/Ayrshare 不合适时复核 |
| **Make** | 连接器明确有 `Publish a Reel` | Webhook/场景传文件；Free 5 MB、Core 100 MB | 公开文档未承诺 Reel ID/URL；默认 webhook `200` 只代表入队 | 需自行用场景数据存储实现端到端防重 | Free；Core 约 $9/月起（年付） | 成熟但回执不足，暂不首选 |
| **Buffer** | API 枚举明确含 Facebook `reel` | API 只接受长期公开 URL，不上传本地文件 | Buffer ID、`sentAt`、`externalLink`；无原生 Reel ID或发布 webhook | 未见创建帖子幂等键 | Free 可用 | 媒体托管和回执缺口过大 |
| **Upload-Post** | 明确支持 Facebook Page `REELS` | multipart 可直传本地文件 | 可返回 `video_reel_id`、平台 ID与 URL，支持状态/webhook | 未见正式幂等键或内容防重 | Free 10次/月；Basic $24/月 | 功能合适，但供应商成熟度与防重不足 |
| **Hootsuite** | UI 支持 Reel，但公开 Publish API 未见 Reel 类型 | API 有媒体上传 | API 基础回执强 | 未形成 Reel 专用证据 | Standard 起 $99/月 | **淘汰：不能证明 API 发的是 Reel** |
| **Zapier** | 原生动作只有普通 Page Video，发到 stream | 可由 Zap 处理 | webhook 接收不等于下游发表 | 无 Page Reel 成品契约 | Professional 起 $19.99/月（年付） | **淘汰：没有 Page Reel 动作** |
| **Sprout Social** | UI 支持 Reel | UI 可上传 | 无可依赖的公开 Reel 发布 API契约 | 不适用 | API位于 Advanced，$399/席/月 | **淘汰：成本和公开接口均不合适** |

价格为 2026-08-31 官方公开页显示的美元标价，不含税；结账价和地区可购性仍须在实际注册前复核。

## 首选方案的边界

### Zernio 的优势

- [Facebook 平台文档](https://docs.zernio.com/platforms/facebook)明确 Page Reel、单视频与 Reel caption/title 字段；
- [媒体上传文档](https://docs.zernio.com/guides/media-uploads)提供预签名直传，本地视频无需另建公开对象存储，临时上传默认 7 天过期；
- [创建帖子接口](https://docs.zernio.com/posts/create-post)提供草稿、定时和立即发布三种明确状态，并为立即发布结果提供 `platformPostUrl`；
- 同一接口提供两层防重：同一 `x-request-id` 在约 5 分钟内只返回原帖子；相同平台、账号、内容和媒体 URL 在 24 小时内返回 `409` 与原帖子 ID；
- [Webhook 文档](https://docs.zernio.com/webhooks)有逐平台成功/失败事件、稳定事件 ID、HMAC-SHA256 签名和重投语义；
- [价格页](https://docs.zernio.com/pricing)显示前两个连接账号免费、无需信用卡，足够核验一个 Facebook Page；
- [连接流程](https://docs.zernio.com/guides/connecting-accounts)由 Zernio 托管 OAuth 与 Facebook Page 选择，不要求大帅进入 Meta Developer 创建 App。

### Zernio 的风险

- [Facebook 权限说明](https://docs.zernio.com/platforms/facebook)显示连接时会一次请求发布、互动、消息、洞察和 Business Manager 等多项权限，且不能逐项缩小；这比“一键发只发 Page Reel”所需权限宽；
- 官方文档明确承诺公开链接，但未明确承诺 Facebook 原生 Reel ID；
- 服务刚从 Late 更名为 Zernio，供应商规模和长期稳定性弱于 Buffer、Make 等成熟平台；[更名说明](https://zernio.com/rebrand)称 API、基础设施和凭据兼容不变，但这仍是供应商自述；
- [隐私政策](https://zernio.com/privacy-policy)只给出通用的删号后 30 天删除或匿名化承诺，没有清楚列出 Facebook token、已发布媒体和运行日志各自的精确保留期；
- 官方没有提供中国大陆或本账号可完成 Meta OAuth 的明确清单，地区可用性只能通过一次连接资格核验确认。

这些风险不阻止零发布资格核验，但在取得真实回执前阻止把它称为可用方案。

## Ayrshare 为什么只做兜底

[Ayrshare Facebook 文档](https://www.ayrshare.com/docs/apis/post/social-networks/facebook)明确支持 Page Reel，并且[发布接口](https://www.ayrshare.com/docs/apis/post/post)示例能返回 Facebook 原生 ID和 URL，也支持 `idempotencyKey`。它的优势是 Meta 异常处理证据最完整：官方明确警告 Facebook 可能“返回错误但实际已发布”，这时会给出 `verifyReelsUrl` 和建议核验时间。该行为与一键发当前事故高度相关，因此若采用 Ayrshare，必须使用核验 URL和独立 Page 回读，绝不能照官方一般建议自动再发一次。

它不作为首选的主要原因是[最低 Premium 方案 $149/月](https://www.ayrshare.com/pricing/)，而当前只需一个 Page；多用户托管连接和 webhook 还要更高方案。技术回执更强，但单 Page 验证阶段成本不合理。

## 推荐的后续顺序

1. 大帅若选择 Zernio，先单独授权“仅做账号创建与 Facebook Page 连接资格核验”；该动作不上传、不建草稿、不发布。
2. 连接通过后，鞋匠再离线实现 `ZernioFacebookPageAdapter`、凭据安全存储、内容校验、上传清理、请求幂等、24小时防重映射、逐平台状态轮询、签名 webhook和现有 `blocksReplay` 状态转换，并完成自动化测试。
3. 离线门通过后，用 Zernio content-only validate 或第三方草稿做非公开验证；仍不得发布到 Facebook。
4. 只有取得全新内容、全新一键发预检任务和大帅针对该具体任务的新明确授权，才允许一次真实 Page Reel 发布。
5. 真实验收只有在 `platformPostUrl` 非空且 Facebook Page 独立回读唯一命中时通过；否则保持结果不明并禁止重放。若 Zernio不能稳定提供这层证据，再评估付费 Ayrshare，不在其他候选之间反复碰运气。

## 当前状态

第三方路线的资料评估已完成，**具备进入“零发布连接资格核验”的条件，但不具备公开发布验收条件**。下一步仍需要大帅针对“注册 Zernio 并只连接 Page、不上传、不发布”作出单独明确授权。
