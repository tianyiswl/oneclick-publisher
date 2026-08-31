# Instagram 专业账号登录与 Reels 受控发布 V1

## 范围与基线

- 只实现 Instagram，不修改或处理 Facebook Page 发布问题。
- 功能分支：`feature/instagram-publish-v1`。
- 基线：`c08647f8043852a6535dcab571e611da7d3c4f88`，包含当前 YouTube、TikTok 与 Meta 公共基础设施。
- 功能工作树与 Facebook 诊断工作树完全分离，不重置、不丢弃、不混入其它任务修改。

## 核心决定

1. Instagram V1 只接受一个已绑定的 Business 或 Creator 账号、一个视频和一个本地封面。
2. SQLite 只保存本地账号 ID、稳定 Instagram 用户 ID、username、账号类型、关联 Page 与本地会话文件引用。不在任务、日志或回执中保存密码、Cookie、Token、验证码或原始 HTML。
3. 登录保存前必须从两个 Instagram 管理页面回读到同一稳定主体、专业账号类型和关联 Page。重启后以稳定 Instagram 用户 ID 识别同一账号。
4. UI、CLI 和本地接口共用 `overseas_instagram_service` 与 `publish_service`，不允许旧 Meta 布尔确认绕过 Instagram 专用服务。
5. `preflight` 是零平台写入的本地预检。`platform_form_check` 会在单独授权后上传并填写平台表单，但不点击 Share。`formal` 必须续接该已回读表单会话，并消费绑定具体预检任务的一次性授权。
6. 预检与正式动作复用同一份字段和回读合同。正式点击前再回读当前表单；任一字段变化都在点击前安全停止。

## 内容与回读合同

- caption 由标题、正文和结构化话题确定性组合，以 SHA-256 绑定。V1 不接受无法回读平台实体的原始 `@`。
- 视频和封面以文件字节 SHA-256 绑定；同路径内容变化会使预检失效。
- V1 只允许可精确回读的公开 Reel，必须明确 `shareToFeed`。
- 支持立即和平台原生定时；定时意图以带时区 ISO 时间与 IANA 时区共同绑定。
- 平台表单预检必须回读 Instagram 主体、账号类型、关联 Page、视频、caption、话题、封面、公开范围、`shareToFeed`、定时设置和唯一可用最终按钮。

## Claim 与任务状态

```text
local_preflight_passed
  -> platform_form_verified
  -> reserved
  -> final_action_claimed
  -> final_action_clicked
  -> platform_accepted
  -> published_readback_confirmed | scheduled_readback_confirmed
```

- `reserved` 由正式任务、具体预检、Instagram 主体、内容意图和表单快照共同绑定。
- 执行器在点击前先把 `final_action_claimed` 提交到 SQLite，再向适配器发出一次点击命令。进程在两个动作之间中断时，也会锁定结果并禁止重放。
- 点击前明确失败进入 `safe_failed`，不会冒充触发过最终动作。
- 点击后没有唯一新媒体 ID、链接或定时条目时进入 `ambiguous`，`blocksReplay=1`。后续只允许读后台列表或作品详情，不再填写、上传或点击。
- 成功回执至少包含稳定任务阶段、错误码或平台原文、Instagram 媒体 ID、链接、发布时间或定时时间。

## 授权门

| 外部动作 | 所需授权 | V1 处理 |
| --- | --- | --- |
| 真实 Instagram 登录与会话保存 | 单独登录授权 | 授权前不启动浏览器 |
| 上传、填写与表单回读 | 绑定具体内容的预检授权 | 必须保持 `finalActionTriggered=false` |
| 点击 Share 或 Schedule | 绑定具体成功预检的新一次授权 | 最多点击一次 |
| 点击后核对 | 原正式任务的只读恢复 | 禁止重放不可逆动作 |

## 当前实现与未验收项

- 已实现：稳定身份合同、本地绑定表、重启读回、内容意图与哈希、零写入本地预检、表单快照、点击权 Claim、同会话正式状态机、结果不明禁止重放和唯一内容回读。
- 已接入共享服务候选：UI/CLI 的 Instagram 本地预检进入同一服务；旧 Meta 正式通道在浏览器前失败关闭。该共享提交必须由集成线审查，不在功能线自行合并。
- 未实现：当前 Meta Business Suite 页面的 Instagram 身份探针、精确主体激活、表单选择器和长驻会话 worker 接线。
- 未验收：真实登录、重启后同账号识别、正确后台、真实上传和表单回读、最终分享、Instagram 媒体 ID、链接及定时回执。在取得这些证据前，不得宣称 Instagram 真实登录或发布可用。
