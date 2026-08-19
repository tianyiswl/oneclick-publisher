# 一键发平台数据会话采集设计

## 1. 目标与范围

本轮交付统一的平台数据存储、同步记录和数据监测页，并以抖音作为第一个真实适配器。

V1 只读取已登录账号本人在官方创作者后台可见的数据，不发布、不修改、不删除任何平台内容。数据同步采用：

1. 优先复用一键发已经保存的 storage_state，通过受限 HTTP 会话读取。
2. 对 HTTP 200 之外，还必须验证平台业务状态和指标结构。
3. 直连接口要求动态签名时，短时启动无窗口 Chromium，让官方页面自行产生签名请求并捕获其 JSON 响应。
4. 拿到可信响应或确认失败后立即关闭浏览器上下文。

V1 不包含：

- 其他九个平台的真实适配器；
- 评论全文、私信、收入明细或个人隐私数据；
- 长期常驻浏览器；
- 逆向实现 a_bogus、绕过验证码或规避平台风控；
- 后台定时调度和云端上传；
- 把缺失、未授权或解析失败的数据记为 0。

## 2. 已验证事实

项目已有抖音登录流程，会把完整 Playwright storage_state 保存到一键发本地账号目录。该状态含 Cookie，并可能包含 localStorage。

2026-08-20 使用现有登录状态做了只读探测：

- 数据总览接口返回 HTTP 200，外层 status_code=0，但各指标内部均为 status_code=8；
- 两个作品列表接口均返回 HTTP 200、业务状态 status_code=8 用户未登录；
- 旧版数据面板接口返回业务成功信封，但指标内容为空。

因此 HTTP 状态、外层状态或空数组都不能单独证明同步成功。抖音部分数据请求依赖页面生成的动态参数或签名，纯 Cookie 只能作为第一条尝试路径。

## 3. 方案比较与决定

### 方案 A：纯 Cookie 直连

资源消耗最低，但对当前抖音作品与指标接口无法取得可信数据。若直接落地会产生“同步成功但全是空值”的假成功。

### 方案 B：所有同步均使用浏览器页面和 DOM

兼容性较高，但资源重、解析脆弱，也会重复现有发布自动化的复杂度。

### 方案 C：会话直连优先，官方签名请求兜底

默认用 HTTP 会话；只有业务状态表明直连不可用时，才用短时静默浏览器捕获官方页面自己的只读数据响应。数据层不感知采集细节。

采用方案 C。它保留直连的性能优势，同时不伪造签名、不绕过风控，也不会用空响应冒充真实数据。

## 4. 组件边界

### 4.1 platform_data_models.py

定义只含公开业务字段的不可变模型：

- MetricPoint：实体类型、实体键、标准指标键、平台原始指标键、数值、单位、观察时间；
- CollectionBatch：平台、账号、来源模式、指标列表；
- CollectionFailure：固定错误码与可重试标记。

模型不得包含 Cookie、Token、请求头、完整响应、DOM、HTML、平台会话 ID 或签名参数。

### 4.2 platform_data_service.py

负责：

- 创建和完成同步运行记录；
- 原子写入规范化指标快照；
- 查询账号最新指标、趋势和最近同步状态；
- 把不存在的指标保持为缺失，而不是补零；
- 对 UI 只返回白名单字段。

服务不发网络请求，也不启动浏览器。

### 4.3 platform_data_collectors.py

提供适配器协议和注册表：

    class PlatformDataCollector(Protocol):
        def collect(self, account: dict) -> CollectionBatch: ...

V1 只注册平台类型 3 的抖音适配器。其他平台返回固定 collector_not_available，不生成空快照。

### 4.4 douyin_data_collector.py

抖音适配器分为两个受控阶段。

阶段一：direct_session

- 只从账号记录定位本地 storage_state；
- 将 douyin.com 域 Cookie 注入临时 requests.Session；
- User-Agent、Origin、Referer 和请求体由代码中的端点白名单定义；
- 只允许预先登记的 GET/POST 只读请求；
- 同时校验 HTTP、顶层业务状态、子指标业务状态和必要字段；
- 业务状态 8、签名缺失、空信封或畸形结构不得写入数据。

阶段二：browser_signed

- 仅在直连明确返回“需要官方页面签名”时触发；
- 使用现有固定 Chromium 和同一 storage_state 创建独立无窗口上下文；
- 打开抖音创作者数据页，监听白名单接口响应；
- 只读取 JSON 响应，不点击、不填写、不执行页面内 fetch 重放；
- 取得一份完整可信响应即关闭页面、上下文、浏览器和 Playwright；
- 登录页、验证码、安全验证、超时或进程关闭不完整均固定失败，不继续重试。

阶段二不尝试绕过平台验证。需要人工重新登录时返回 login_required。

### 4.5 DataMonitorPage

用新的正式页面替换侧栏“数据监测 · 开发中”。V1 页面包含：

- 账号选择；
- 平台与同步来源状态；
- “立即同步”按钮；
- 本次同步进度；
- 最近同步时间和固定失败原因；
- 核心指标卡；
- 近 30 日趋势表；
- 缺失指标显示 —，不显示 0；
- “重新登录”提示仅在 login_required 时出现。

同步必须通过现有后台任务执行器运行，Qt 主线程只接收白名单结果。重复点击同一账号同步时拒绝第二个任务。

## 5. 数据库

新增两张表。

### platform_data_sync_runs

| 字段 | 含义 |
| --- | --- |
| id | 运行主键 |
| accountId | 一键发账号 ID |
| platformType | 平台类型 |
| sourceMode | direct_session 或 browser_signed |
| status | running、success、failed |
| errorCode | 固定错误码，可空 |
| metricCount | 本次可信指标数 |
| startedAt | 开始时间 |
| finishedAt | 完成时间 |

### platform_metric_snapshots

| 字段 | 含义 |
| --- | --- |
| id | 快照主键 |
| syncRunId | 所属运行 |
| accountId | 一键发账号 ID |
| platformType | 平台类型 |
| entityType | V1 固定 account，以后可扩为 content |
| entityKey | V1 使用账号稳定本地键，不保存 Cookie 文件名 |
| metricKey | 一键发标准指标键 |
| rawMetricKey | 平台原始指标名 |
| metricValue | 数值；缺失时不创建记录 |
| metricUnit | count、ratio 等 |
| observedAt | 平台数据观察时间 |
| createdAt | 本地写入时间 |

同一运行、实体和指标只允许一条记录。同步运行和指标快照在同一事务中完成；任何解析或落库失败均不留下 success 运行。

## 6. 标准指标

V1 只接纳抖音响应中能明确确认含义的指标，候选标准键为：

- views：播放；
- likes：点赞；
- comments：评论；
- shares：分享；
- followers_total：粉丝总数；
- followers_net：净增粉；
- profile_visits：主页访问。

适配器维护平台字段映射。未知字段忽略并记录内部计数，不把未知结构或原始 JSON 写入数据库。若本次没有任何可信指标，整次同步失败为 metric_payload_empty。

## 7. 错误与状态

公开错误码限定为：

- collector_not_available
- session_state_missing
- login_required
- direct_request_rejected
- browser_signature_timeout
- browser_cleanup_incomplete
- metric_payload_invalid
- metric_payload_empty
- sync_persist_failed

日志只记录平台、账号本地数字 ID、阶段、固定错误码、耗时和指标数量。异常原文、URL 查询串、Cookie 文件路径、响应体和账号身份不进入日志或任务明细。

直连失败只有在确认属于签名或会话语义问题时才进入浏览器兜底。网络故障不会无上限重试；单阶段最多一次请求，用户可再次点击同步。

## 8. 安全边界

- 数据采集端点使用代码白名单，禁止传入任意 URL；
- 只允许明确登记的 GET 或 POST 只读接口；
- 不实现通用 Cookie 导出接口；
- 不在 CLI、进程参数、日志或数据库中输出 Cookie；
- 登录状态文件继续位于现有用户数据目录并保持 Git 忽略；
- 数据响应经过字段白名单投影后才离开适配器；
- 浏览器兜底必须在 finally 中严格关闭；
- 验证码、滑块或账号风控出现时停止并交还账号管理流程。

## 9. 数据流

    用户点击立即同步
      → 后台任务读取账号白名单字段
      → 抖音适配器尝试 direct_session
      → 可信响应：规范化指标
      → 签名或会话语义拒绝：browser_signed 捕获官方响应
      → 可信响应：规范化指标
      → platform_data_service 原子写运行和快照
      → UI 重读数据库并显示指标、趋势和来源

任一阶段未取得可信响应时，只写失败运行，不写指标快照；旧的成功快照继续保留，UI 同时显示“上次成功数据”和“本次同步失败”，避免失败把历史数据清空。

## 10. 测试与验收

按测试阶梯执行：

1. 模型与数据库单测：
   - 缺失不补零；
   - 同步运行与快照原子写；
   - 敏感字段无法进入公开模型。
2. 抖音直连适配器单测：
   - Cookie 域隔离；
   - HTTP 200、内层状态 8 必须失败；
   - 空指标不算成功；
   - 白名单指标正确归一化。
3. 浏览器兜底单测：
   - 只监听白名单接口；
   - 成功响应后关闭全部资源；
   - 登录、验证、超时固定失败；
   - 不执行页面内 fetch 和写操作。
4. UI 单测：
   - 后台同步不阻塞主线程；
   - 重复点击受控；
   - 缺失显示 —；
   - 失败保留上次成功数据；
   - 侧栏正式打开数据监测页。
5. 受影响模块测试；
6. 收尾时一次全量测试。

运行闭环验收必须使用用户自己的已登录抖音账号：

- 数据同步期间没有可见浏览器窗口；
- 至少一个可信指标写入数据库并在数据监测页显示；
- 页面数值能与同一时刻抖音创作者后台人工可见值对应；
- 若直连返回伪成功空信封，客户端明确失败而不是显示 0；
- 同步完成后没有残留 Chromium 或 Playwright 会话。

离线单元测试和模拟响应只能证明交付闭环，不能冒充真实平台数据已获取。
