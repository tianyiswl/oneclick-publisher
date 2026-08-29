# TikTok 单视频平台原生定时发布设计

日期：2026-08-29

状态：对话设计与书面规格均已通过（2026-08-29）

目标分支：`feature/overseas-login-publish-v2`（功能分支不改版本号、不制作正式安装包）

## 1. 背景与当前事实

一键发已经具备 TikTok 单账号、单视频、本地零写入预检、真实平台表单检查和受控正式发布主链。当前真实证据只到 `platform_form_verified`：测试账号已经回读唯一视频、唯一正文、官方话题实体、公开范围和可用最终按钮，但没有点击 `Post`，因此不能宣称 TikTok 公开发布已经验证。

当前受控请求与 TikTok 专用发布服务会主动拒绝所有定时字段；上传器虽然保留 `publish_date` 参数，但没有识别、填写或回读 TikTok Studio 的定时控件。只移除拒绝逻辑会造成“请求看似支持、平台没有实际排期”的假能力，不能采用。

TikTok 官方网页定时能力允许在 Studio 上传页设置日期和时间；官方公开说明的时间窗口为发布前 15 分钟至 10 天，页面时间默认跟随电脑时区。TikTok Studio 的具体功能可能因账号、地区和页面版本不同而不可用，因此实现必须以当前页面能力回读为准，不能假定所有账号都有入口。

## 2. 目标

在现有单视频受控发布主链中增加 TikTok 平台原生定时发布：

- 内容项目、CLI 和桌面 UI 继续使用同一受控发布服务；
- 只接受显式 `Asia/Shanghai` 时间，不使用隐含系统时区；
- 浏览器上下文固定为 `Asia/Shanghai`，并从当前 TikTok Studio 页面精确回读日期、时间和最终动作；
- 定时入口不可用时明确失败，绝不降级成立即公开；
- `platform_form_check` 可以验证定时表单，但必须停在 `Schedule` 前；
- 正式模式只有在一次性授权和全部实时回读通过后才点击一次 `Schedule`；
- 平台明确接受并从定时内容列表回读同一作品和时间后，才记为“定时已确认”；
- 定时成功不能提前冒充作品已经公开。

## 3. 不在本轮范围

- 本地守护进程在目标时间再打开浏览器立即发布；
- 平台原生定时不可用时自动改用本地延迟提交；
- 多账号矩阵、多个视频、图文帖子、朋友可见或仅自己可见；
- 自动修改、取消或删除已排期作品；
- 自动等待到发布时间再把任务升级为已公开；
- 绕过登录、验证码、二次验证、风控或地区限制；
- TikTok 官方 Content Posting API；
- 在本轮设计、编码或自动测试阶段创建真实定时作品。

## 4. 路线选择

### 4.1 选择：TikTok Studio 平台原生定时

一键发在真实上传页启用定时、填写时间、回读后点击 `Schedule`。平台受理后，电脑和客户端无需保持运行。该路线复用现有浏览器会话、话题实体和正式授权边界，新增面最小。

### 4.2 不采用：本地等待后发布

本地延迟提交依赖目标时刻电脑在线、网络可用、登录有效且无人占用浏览器。慢网络、验证码和客户端退出都会造成明显时间误差，不属于平台原生排期。

### 4.3 不采用：平台定时与本地等待混合

两种路线对电脑依赖、成功回执和故障语义完全不同。自动降级可能把用户期望的“平台已排期”变成到点临时提交，甚至在定时控件失效时误发，因此必须失败关闭。

## 5. 请求合同与时间口径

### 5.1 唯一输入

TikTok 目标沿用受控接口的根级排期字段：

```json
{
  "platform": "TikTok",
  "accountId": 13,
  "schedule": {
    "localTime": "2026-09-01 09:00",
    "timezone": "Asia/Shanghai"
  },
  "settings": {
    "visibility": "public"
  }
}
```

- `schedule: null`：沿用立即公开语义；
- 非空 `schedule`：表示 TikTok 平台原生定时公开；
- `localTime` 必须为 `YYYY-MM-DD HH:mm`；
- `timezone` 必须精确等于 `Asia/Shanghai`；
- 不接受 `scheduledAt`、`publishAt`、嵌套 `scheduleTime` 或其他别名；
- 现有运行负载中的 `enableTimer`、`scheduleTime`、`scheduleTimezone` 和 `dailyTimes` 只能由受控服务从根级合同派生，不能成为第二套用户输入。

### 5.2 时间窗口

- 创建任务时，目标时间必须至少比当前北京时间晚 30 分钟，为慢网络、视频上传和平台处理预留时间；
- 创建任务时，目标时间不得超过当前北京时间 10 天；
- 点击最终按钮前再次按同一北京时间快照核验，目标时间必须仍至少晚 15 分钟且不超过 10 天；
- 上传过慢导致剩余时间不足时，在最终动作前失败，不自动改时间、不立即发布；
- 所有比较使用带时区的时间对象，禁止以无时区字符串或系统本地时间参与授权判断。

### 5.3 授权与防重复

排期的本地时间、时区和发布方式必须进入：

- 本地预检快照；
- 发布指纹；
- 一次性正式授权；
- 任务项和最终回执。

账号、内容、视频、话题、可见性或排期任一变化，旧预检和旧授权失效。相同指纹已经触发 `Schedule`、已经受理、已经回读或结果不明时，禁止自动再次提交。

## 6. 模块边界

### 6.1 受控请求层

`app_core/controlled_publish.py` 负责：

- 继续只接受根级 `{localTime, timezone}`；
- 允许 TikTok 的合法单次排期；
- 派生唯一运行负载；
- 将排期加入快照、指纹、授权和查询 JSON；
- 保持立即发布与其他平台的既有合同不变。

该文件属于共享核心，功能分支中的改动必须单独提交，交由集成线审查后进入 `main`。

### 6.2 TikTok 专用合同层

`app_core/overseas_tiktok_publish.py` 负责：

- 把现有“只允许立即发布”校验改为“立即或单次平台定时”二选一；
- 拒绝冲突、别名、错误时区和越界时间；
- 输出归一化的 `scheduleMode`、`scheduledAt` 和 `scheduleTimezone`；
- 在本地预检回执中保留排期，但不创建浏览器；
- 在真实表单快照与授权快照之间比较同一排期。

### 6.3 TikTok 定时表单模块

新增一个位于 `uploader/tk_uploader/` 下的独立定时表单模块，职责限定为：

1. 在当前 TikTok Studio 上传页识别唯一、可见、可交互的定时入口；
2. 启用定时并回读开关状态；
3. 识别日期和时间控件，写入北京时间；
4. 从同一控件精确回读日期、时间和时区语义；
5. 确认最终动作已经从立即发布变为 `Schedule`；
6. 点击后识别平台明确受理反馈；
7. 从内容管理页按账号、视频/文案指纹和时间窗口只读核对唯一排期。

定时入口不存在、命中多个控件、只读、写入失败或回读不一致时安全停止。模块不决定是否有权点击最终按钮，也不保存账号凭据。

### 6.4 上传器编排

`uploader/tk_uploader/main.py` 继续编排同一 Playwright 页面：

1. 核对账号；
2. 上传视频；
3. 填写唯一正文和官方话题实体；
4. 设置公开范围；
5. 若请求定时，调用独立定时模块并回读；
6. 生成完整表单快照；
7. `platform_form_check` 在最终按钮前停止；
8. 正式模式在授权复核后只点击一次 `Schedule` 或 `Post`。

立即与定时必须共用账号、上传、正文、话题、公开范围、人工验证和结果不明保护，不能复制两套上传器。

### 6.5 浏览器时区

现有 `new_publish_context` 已固定 `timezone_id="Asia/Shanghai"`。TikTok 主链必须继续通过该入口创建上下文；测试要证明定时流程没有绕过或覆盖该值。

## 7. 页面操作与回读合同

### 7.1 定时入口

优先依据控件语义、标签和可见状态识别，不能只依赖坐标或单个过时 class。若页面显示该账号没有定时能力，返回稳定错误，不尝试立即发布。

### 7.2 日期和时间

- 写入前先读当前值；
- 按目标北京时间写入一次；
- 日期、小时和分钟分别归一化后比较；
- 控件显示的时间与授权快照必须完全一致；
- 页面自动纠正、四舍五入、跨日或时区偏移都视为不一致；
- 最终按钮前重新回读一次，防止上传处理或页面重挂载清除排期。

### 7.3 最终动作

立即发布要求唯一可用 `Post`；定时发布要求唯一可用 `Schedule`。定时请求若仍只出现 `Post`，返回错误并停止。按钮点击必须通过已有一次性授权和单次消费保护。

### 7.4 平台受理与只读核对

点击 `Schedule` 后至少取得一项明确受理信号，并继续尝试从 TikTok Studio 内容管理页读取同一排期。唯一匹配条件至少包括：

- 同一已核对账号；
- 同一视频/文案发布指纹可识别信息；
- 同一 `Asia/Shanghai` 排期；
- 位于本次提交后的合理时间窗口。

明确受理后可以短暂进入 `scheduled_accepted` 继续做有界只读核对；若在限定窗口内仍不能唯一回读，则进入 `ambiguous` 并结束任务。任何情况都不得再点 `Schedule`，也不得永久停留在 `running/pending`，并始终保留防重复锁。

## 8. 状态与回执

### 8.1 状态层级

- `local_preflight_passed`：本地合同通过，没有打开 TikTok；
- `platform_form_verified`：真实页面已上传并精确回读定时表单，未点 `Schedule`；
- `final_action_triggered`：已点击一次 `Schedule`；
- `scheduled_accepted`：TikTok 明确接受排期；
- `scheduled_readback_confirmed`：内容管理页唯一回读到同一作品和时间；
- `published_readback_confirmed`：到点后另一次只读同步确认作品已经公开；
- `failed`：最终动作前出现明确错误；
- `ambiguous`：最终动作已触发，但结果不足以安全定性。

正式定时任务在 `scheduled_readback_confirmed` 后即可作为“平台排期成功”终止，不需要让 CLI 持续运行到实际发布时间。此终态仍不代表已经公开，`publishedAt` 必须保持空值。

### 8.2 回执示例

```json
{
  "taskId": 123,
  "platform": "TikTok",
  "phase": "scheduled_readback_confirmed",
  "status": "success",
  "errorCode": "",
  "errorMessage": "",
  "receipt": {
    "accountId": 13,
    "visibility": "public",
    "scheduleMode": "platform_native",
    "scheduledAt": "2026-09-01 09:00",
    "scheduleTimezone": "Asia/Shanghai",
    "platformWriteOccurred": true,
    "finalActionTriggered": true,
    "platformAccepted": true,
    "scheduledReadbackConfirmed": true,
    "contentId": null,
    "contentUrl": null,
    "publishedAt": null
  }
}
```

没有作品 ID 或链接时保持 `null`，不能生成本地伪 ID。

## 9. 错误码与终态

新增或固定以下错误码：

- `tiktok_schedule_invalid`：格式、字段或时区无效；
- `tiktok_schedule_out_of_range`：创建或最终提交时不在允许窗口；
- `tiktok_schedule_unavailable`：当前账号或页面没有可用定时入口；
- `tiktok_schedule_control_ambiguous`：入口、日期、时间或最终按钮不唯一；
- `tiktok_schedule_readback_mismatch`：平台显示值与授权快照不一致；
- `tiktok_schedule_final_action_unavailable`：定时请求没有唯一可用 `Schedule`；
- `tiktok_schedule_outcome_unknown`：已点击 `Schedule`，但无法证明受理或明确失败。

最终动作前的错误进入 `failed`，修正内容并重新预检后可以重试。最终动作后的未知结果进入 `ambiguous`，相同指纹只允许只读核对。CLI 中断、浏览器崩溃、工作线程异常或租约过期必须按是否已触发最终动作进入 `failed` 或 `ambiguous`，不能永久 `running/pending`。

## 10. 人工验证边界

登录过期、扫码、验证码、二次验证、风控或未知法律提示继续沿用现有人工门：

- 显示同一浏览器页面并暂停当前任务；
- 用户完成后继续当前页面，不重复上传和填写；
- 不把 Cookie、二维码像素、验证码或会话内容写入 JSON、日志、内容包或 Git；
- 超时后进入明确终态；
- 人工验证不等于授权最终动作。

## 11. 自动测试

### 11.1 合同与时间

- `schedule: null` 保持立即发布兼容；
- 合法北京时间排期被归一化并进入负载；
- 错误时区、错误格式、未知别名、冲突字段和多时间列表均被拒绝；
- 创建时少于 30 分钟或超过 10 天被拒绝；
- 最终提交时少于 15 分钟被拒绝；
- 夏令时或系统时区不能改变北京时间结果；
- 排期变化会改变快照和发布指纹，并使旧授权失效。

### 11.2 页面模块

使用可控假页面覆盖：

- 唯一定时入口、开关、日期、时间和 `Schedule` 按钮；
- 无入口、多个入口、不可交互和页面重挂载；
- 日期时间写入和精确回读；
- 平台自动纠正、跨日、时间偏移和最终按钮仍为 `Post`；
- `platform_form_check` 从不点击最终动作；
- 正式模式只点击一次；
- 点击后成功反馈、定时列表唯一回读和结果不明。

### 11.3 编排与恢复

- 本地预检证明没有创建浏览器、没有上传、没有改会话文件；
- 立即与定时共用上传、话题实体、可见性和人工验证；
- 任务查询返回稳定排期字段和状态；
- 一个异常不能留下永久 `running/pending`；
- 最终动作后的进程中断触发 `ambiguous` 和防重复；
- 海外定向套件、受影响模块、完整离线回归和离屏客户端依次通过。

## 12. 真实平台验收

### 12.1 定时表单检查

实现完成并获得大帅当次允许后，使用当前 TikTok 测试账号和专用测试视频执行一次 `platform_form_check`：

1. 排期选择未来 30 分钟至 10 天内的北京时间；
2. 上传并回读正文、官方话题、公开范围和定时控件；
3. 确认最终按钮为 `Schedule`；
4. 在按钮前停止并关闭页面；
5. 回执必须为 `platform_form_verified`、`finalActionTriggered=false`。

该验收会向上传页发送测试视频，但不会创建定时作品，只能证明当前页面的定时表单可用。

### 12.2 正式平台排期

真正证明“TikTok 定时发布可用”必须另获大帅对具体内容、账号和时间的明确授权：

1. 重新完成同一内容快照的本地预检；
2. 生成并消费一次性正式授权；
3. 点击一次 `Schedule`；
4. 取得平台受理；
5. 从内容管理页唯一回读同一作品和排期。

在这一步完成前，状态只能写成“源码和自动测试完成”或“真实定时表单已验证”，不能写成“定时发布已验证”。

## 13. 架构与分支治理

实施前同步更新：

- `docs/architecture/tiktok-controlled-video-publish.architecture.json/html`；
- `docs/architecture/tiktok-controlled-video-publish.lifecycle.json/html`。

架构图必须显示平台原生定时模块、北京时间边界、`Schedule` 人工授权点、定时受理、定时列表回读、结果不明和防重复状态。

功能分支允许修改 TikTok 专用服务、上传器、TikTok 测试、规格、状态文档和架构证据。共享受控请求、数据库、任务服务或桌面 UI 的改动必须拆成独立提交并由集成线审查。功能分支不修改应用版本、不制作正式安装包、不自行合并到 `main`。

## 14. 完成口径

本轮分四层报告：

1. **设计完成**：书面规格和架构图通过审核；
2. **本地实现完成**：相关测试、受影响模块和完整回归通过；
3. **定时表单已验证**：真实 `platform_form_check` 回读排期并停在 `Schedule` 前；
4. **平台定时已验证**：另获正式授权后，平台受理并从内容管理页回读同一排期。

任何低层结果都不能替代更高层。

## 15. 官方参考

- [TikTok Video Scheduler 官方说明](https://ads.tiktok.com/business/en-US/blog/introducing-video-scheduler-now-you-can-plan-tiktoks-in-advance)
- [TikTok Studio 创作者工具说明](https://support.tiktok.com/en/using-tiktok/creating-videos/creator-tools-on-tiktok)
