# TikTok 单视频受控发布通道设计

日期：2026-08-28

状态：方案已确认，书面规格待复核

目标版本：下一次海外功能集成版（功能分支不改版本号）

## 1. 背景与当前事实

一键发已有 TikTok 的本地浏览器会话、单视频上传、文案填写、公开范围设置和最终按钮前检查源码。当前还不能把这些源码等同于已验证的真实发布能力，原因是：

1. 现有预检会打开 TikTok Studio 并将视频发送到平台页面，不符合“默认预检不登录、不上传”的新要求。
2. 现有填写主要确认字面文案，还需确认结构化话题真的成为 TikTok 平台话题，不能用普通 `#文字` 冒充。
3. 还没有使用当前可用的 TikTok 账号，在新版平台页面完成登录持久化、客户端重启后识别、上传、字段回读和最终按钮前停止的真实验收。
4. 内容项目以后应调用一键发 CLI，不应通过模拟点击桌面客户端来发布。

本设计选择继续使用一键发自己的受控浏览器会话。TikTok 官方 Content Posting API 暂不作为本轮路线：官方路线需要开发者应用、`video.publish` 权限、用户授权和审核；未审核客户端还存在只能发到私密账号等限制。这条路线留作未来多客户规模化之后的备选，不阻塞当前客户端交付。

## 2. 本轮目标

首轮只完成一条稳定主链：

- 单个 TikTok 账号；
- 单个本地视频；
- 立即公开发布；
- 内容项目可通过受控 CLI 提交内容包和明确账号；
- 默认预检只做本地检查，不打开浏览器，不向 TikTok 上传视频；
- 正式发布必须绑定当次内容、账号和发布方式的一次性授权；
- 只有 TikTok 明确成功回执或精确作品回读才记为成功；
- 验证码、二次验证、风控或未知弹窗只唤起人工处理，不盲目绕过或重试。

## 3. 不在本轮范围

- TikTok 图文帖子、纯文字、多视频和多账号矩阵。
- 定时发布、朋友可见、仅自己可见。
- 自定义封面、合集、商品、位置、付费推广和删除作品。
- AI 生成内容声明的自动设置。内容包若明确要求 AI 声明，本轮应拒绝发布，不能静默忽略。
- 未经 TikTok 官方候选和平台实体回读的 `#话题` 或 `@提及`。首轮不接收独立提及字段。
- 将 Cookie、密码、验证码、二维码链接或会话文件放进内容包、命令行、Git 或长期日志。
- 在本轮开发验收中点击 `Post`。最终公开发布留到真实客户内容和当次明确授权。

## 4. 三条可选路线与结论

### 4.1 受控浏览器会话（本轮选择）

复用现有 TikTok Studio 发布页、本地账号会话和受控发布任务。优点是当前代码基础已存在，可以最快完成真实账号验证；代价是 TikTok Studio 页面改版时需要更新选择器和回读规则。

### 4.2 TikTok 官方 Content Posting API（未来备选）

优点是发布过程可以完全后台化，回执更稳定；代价是开发者应用、权限、用户授权、审核和未审核限制。当真实付费用户数量足以支撑平台审核成本时再启动。

### 4.3 直接复用用户 Chrome 个人资料（不采用）

这种做法开发快，但会把一键发和用户日常 Chrome 的全部账号、Cookie 和扩展放在同一边界内，容易发生资料库锁、账号串用和敏感信息泄漏，不符合现有会话隔离规则。

## 5. 内容包与任务合同

### 5.1 输入

桌面 UI 和 CLI 仍共用受控发布服务。TikTok 目标至少包含：

```json
{
  "platform": "TikTok",
  "accountId": 123,
  "schedule": null,
  "settings": {
    "visibility": "public"
  }
}
```

内容包使用结构化字段：

- 单个视频素材；
- 标题；
- 正文；
- 独立话题列表；
- TikTok 平台覆盖字段。

服务层只组合一次最终文案，标题、正文和话题不能在 UI、CLI 和上传器中分别重复拼接。发布指纹必须包含内容包哈希、视频哈希、账号 ID、标题、正文、话题、可见性和执行模式。

### 5.2 严格拒绝条件

以下任一情况在上传前返回稳定错误：

- 多个账号或多个视频；
- 定时、非公开范围、自定义封面或 AI 声明；
- 独立提及字段非空；
- 视频不存在、不可读或不符合客户端支持的基本格式；
- 结构化字段组合后超过当前客户端支持上限；
- 账号不存在、状态异常、平台不是 TikTok 或缺失会话引用。

## 6. 账号登录和同一主体核对

### 6.1 绑定账号

绑定时在一键发独立可见浏览器中打开 TikTok。用户自行完成账号、密码、扫码、二次验证和法律协议等必须人工步骤。登录后服务只保存独立会话文件和公开账号身份，不记录密码或验证码。

可信账号身份优先使用稳定的用户 ID 或公开 handle；显示名和头像只用于帮助用户识别，不单独用于正式发布的同一主体判定。

### 6.2 重启后检测

真实验收需要关闭并重启源码客户端，再用保存会话做一次只读检测：

- 仍然处于已登录状态；
- 回读的稳定账号标识与本地记录一致；
- 状态、显示名和公开头像可以正确更新；
- 异常账号在账号选择中使用红色字体标记，不能进入正式发布。

## 7. 两种预检边界

### 7.1 日常本地预检

内容项目和桌面 UI 的默认 `preflight` 必须是零平台写入：

1. 解析并验证结构化 manifest；
2. 核对单账号、单视频和公开立即发布范围；
3. 检查视频文件、本地字段和已保存账号记录；
4. 生成发布快照和指纹；
5. 返回本地预检结果。

此模式不创建 Playwright 浏览器、不读取 TikTok Studio 、不上传视频、不修改会话文件。本地预检成功只代表内容和任务可以进入正式会话，不代表平台页面仍兼容。

### 7.2 开发验收的平台表单检查

为了在不发布的前提下证明当前 TikTok Studio 仍可用，保留一个明确的开发验收模式 `platform_form_check`：

1. 必须使用明确的专用验收输入和当次用户允许；
2. 会打开真实 TikTok Studio 并上传一个视频；
3. 填写结构化文案和话题，设置公开范围，再逐项回读；
4. 确认 `Post` 按钮已就绪后必须停止，不允许触发最终发布；
5. 关闭页面和临时会话，回执明确标记 `platformWriteOccurred=true` 和 `finalActionTriggered=false`。

这个模式只用于首次真实验收和平台改版后的定向回归，不是每篇内容的默认预检。

## 8. 正式发布流程

1. CLI 或桌面 UI 提交结构化内容包、TikTok 账号 ID 和 `preflight`。
2. 本地预检成功后，大帅可在当前对话确认内容、账号和立即公开发布。
3. 一键发生成与该快照完全绑定、短期有效、只能消费一次的本地授权。
4. 正式任务先做实时账号身份核对；不一致时在上传前停止。
5. 打开 TikTok Studio，上传视频，等待平台确认上传完成。
6. 填写标题和正文组成的普通文案；每个话题必须逐个选择 TikTok 官方候选，并回读为平台话题实体。
7. 设置并回读公开范围，确认不存在未支持的 AI 声明、定时、封面或提及请求。
8. 在最终按钮前再比对一次当前页面值与授权快照。任何不一致都停止，不点击 `Post`。
9. 仅在正式模式、授权消费成功且所有回读通过时，点击一次 `Post`。
10. 等待明确平台成功反馈，并尽可能用精确作品标识或内容管理页做只读回读。

输入快照在预检后发生任何变化时，旧授权必须失效。结果不明时不得自动再点一次 `Post`。

## 9. 话题、文案与字段回读

### 9.1 文案

字段填写必须使用当前编辑器真正可交互的唯一控件。填写前先清空并回读为空，再写入一次目标文案。完成后比较归一化文本、顺序和重复片段，不能只检查“文字存在”。

### 9.2 话题

每个结构化话题的稳定操作是：

1. 将光标放到已回读正文的末尾；
2. 输入该话题的候选搜索文本；
3. 等待候选列表稳定；
4. 只选择与目标唯一匹配的官方候选；
5. 回读编辑器中的平台实体节点、实体数量和顺序。

无候选、多个歧义候选或回读为普通文字时，返回 `tiktok_topic_entity_missing` 并在最终按钮前停止。不允许保留普通 `#文字` 继续发布。

## 10. 人工验证和浏览器可见性

正常上传和填写阶段尽量不抢占用户当前窗口。但 TikTok 出现登录过期、扫码、CAPTCHA、二次验证、风控确认或未知弹窗时，必须：

1. 将同一浏览器会话显示给用户；
2. 任务状态改为 `waiting_user_verification`；
3. CLI 只返回平台、阶段、人工操作类型和超时时间，不返回二维码像素、验证码或会话凭据；
4. 用户处理完后继续当前页面和当前上传，不重建任务，不重复上传；
5. 超时或页面不可恢复时进入明确失败或需要人工核对的终态。

## 11. 状态、回执和重试保护

### 11.1 状态

至少区分：

- `local_preflight_passed`：本地内容检查通过；
- `waiting_user_verification`：需要用户处理平台验证；
- `platform_form_verified`：真实页面的上传、字段和最终按钮前检查通过，但没有发布；
- `final_action_triggered`：已点击一次 `Post`，正在等待平台结果；
- `platform_accepted`：平台给出明确成功反馈；
- `published_readback_confirmed`：精确作品或内容列表回读成功；
- `failed`：有明确错误且没有发生需核对的平台结果；
- `ambiguous`：已发生平台写入或最终点击，但结果无法证明。

不能用本地预检成功、上传完成、按钮可用或程序退出码 `0` 冒充已发布。

### 11.2 回执

CLI 和桌面 UI 使用同一语义：

```json
{
  "taskId": 123,
  "platform": "TikTok",
  "phase": "platform_form_verified",
  "status": "success",
  "errorCode": "",
  "errorMessage": "",
  "receipt": {
    "accountId": 123,
    "visibility": "public",
    "platformWriteOccurred": true,
    "finalActionTriggered": false,
    "contentId": null,
    "contentUrl": null,
    "publishedAt": null
  }
}
```

正式任务若取得平台作品 ID 或链接，必须保存在回执中。没有取得时保持 `null`，不伪造本地 ID。

### 11.3 防重复

- `Post` 未点击前的明确本地失败可以在内容修正并重新预检后重试。
- `Post` 已点击后任何超时、进程中断或页面异常都进入 `ambiguous`，先只读核对，不自动再点击。
- 同一发布指纹存在 `final_action_triggered`、`platform_accepted`、`published_readback_confirmed` 或 `ambiguous` 记录时，新正式任务默认拒绝执行。
- CLI 中断、工作进程退出或租约过期不得留下永久 `running`；依据是否已触发最终动作进入 `failed` 或 `ambiguous`。

## 12. 稳定错误码

至少包含：

- `tiktok_account_invalid`
- `tiktok_session_missing`
- `tiktok_session_expired`
- `tiktok_account_identity_mismatch`
- `tiktok_video_file_invalid`
- `tiktok_content_too_long`
- `tiktok_unsupported_publish_setting`
- `tiktok_upload_failed`
- `tiktok_editor_not_unique`
- `tiktok_caption_clear_failed`
- `tiktok_caption_readback_mismatch`
- `tiktok_topic_candidate_missing`
- `tiktok_topic_candidate_ambiguous`
- `tiktok_topic_entity_missing`
- `tiktok_visibility_readback_mismatch`
- `tiktok_post_not_ready`
- `tiktok_manual_verification_required`
- `tiktok_final_submit_failed`
- `tiktok_publish_outcome_unknown`
- `tiktok_publish_readback_mismatch`
- `tiktok_duplicate_submit_blocked`

错误原文保留在本地受控运行记录中，对外 JSON 保留必要定位信息，不包含 Cookie、二维码、验证码或完整内容正文。

## 13. 代码边界

由于这条通道涉及外部平台、持久化会话、异步任务、失败恢复和人工验证，实施前需使用 Archify 建立两份与实际代码一致的架构证据：

- `docs/architecture/tiktok-controlled-video-publish.architecture.html/json`；
- `docs/architecture/tiktok-controlled-video-publish.lifecycle.html/json`。

图中必须明确标出内容项目、受控 CLI、共用发布服务、TikTok 上传器、本地会话、任务数据库、TikTok Studio、结果回读、人工验证点和防重复状态转换。架构图是实施和审查证据，不代替自动测试和真实平台验收。

海外功能分支可直接修改：

- `app_core/overseas/**`
- `app_core/overseas_*`
- `uploader/tk_uploader/**`
- TikTok/海外发布定向测试、状态文档和架构证据。

如果需要调整共享受控 CLI、数据库、账号管理或发布中心界面，必须将共享文件拆成独立提交，经集成线审查后再进入 `main`。功能分支不修改版本号，不直接制作对外客户端。

桌面 UI、CLI 和开发验收入口必须复用同一 TikTok 服务和同一字段回读实现，不允许再写一套不同标准的页面自动化。

## 14. 验收标准

### 14.1 离线验收

- 默认 TikTok `preflight` 通过测试证明没有创建浏览器、没有上传和没有改写会话文件。
- 多账号、多视频、定时、非公开、AI 声明、封面和提及均在上传前被拒绝。
- 预检快照任一字段变化后，正式授权失效。
- 正式发布与 `platform_form_check` 共用账号核对、上传、字段填写、话题实体和可见性回读逻辑。
- 普通 `#文字` 不能通过话题实体验证。
- 人工验证完成后在同一会话继续，不重复上传。
- 最终点击后的超时进入 `ambiguous`，并触发指纹级防重复。
- CLI 中断、线程异常和租约过期后任务进入明确终态。
- 海外定向测试和受影响模块测试通过，不用局部结果冒充全量回归。

### 14.2 真实平台验收

使用大帅已确认的 TikTok 测试账号，仅完成：

1. 可见登录并保存独立会话；
2. 关闭并重启源码客户端后，静默检测仍回读同一账号；
3. 默认本地预检没有打开浏览器、没有向平台上传；
4. 另外一次明确的 `platform_form_check` 完成视频上传、文案顺序、每个话题实体、公开范围和 `Post` 就绪回读；
5. 在 `Post` 前停止并关闭页面，确认回执为 `platform_form_verified`、`finalActionTriggered=false`。

本轮不点击 `Post`，因此验收结论只能写成“TikTok 真实登录和发布前表单回读通过”，不能写成“TikTok 已发布或公开发布已验证”。

## 15. 参考

- [TikTok Content Posting API 入门](https://developers.tiktok.com/docs/en/content-posting-api-get-started)
- [TikTok Direct Post 接口](https://developers.tiktok.com/docs/en/content-posting-api-reference-direct-post)
- [TikTok 应用审核指南](https://developers.tiktok.com/docs/en/app-review-guidelines)
