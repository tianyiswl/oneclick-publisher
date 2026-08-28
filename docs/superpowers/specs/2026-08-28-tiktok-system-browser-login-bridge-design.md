# TikTok 系统浏览器登录桥设计

日期：2026-08-28

状态：大帅已书面批准，待实施与真实登录验收

目标分支：`feature/overseas-login-publish-v2`

## 1. 故障与根因

TikTok 账号绑定当前调用链为：

`ui/account_page.py` → `ui/login_dialog.py` → `app_core/login_service.py` → `myUtils/login.py` → `utils/base_social_media.py`。

TikTok 路由在 `conf.USE_SYSTEM_BROWSER=False` 时启动 Playwright 捆绑的 Chrome for Testing。Google 官方会拦截由软件自动化控制的浏览器，因此使用 Google 身份的 TikTok 账号无法登录。当 TikTok 扫码确认仍回到该账号的 Google 联合身份链路时，也会出现相同拦截。

2026-08-28 真实尝试的准确结论为 `login_architecture_blocked`：未登录、未上传、未建立预检或表单检查任务、未产生平台写入、未点击 `Post`。

本规格取代 `docs/superpowers/specs/2026-08-28-tiktok-controlled-video-publish-design.md` 第 6 节中“在受控浏览器内完成首次绑定”的登录假设；该旧规格的账号主体核对、预检、表单填写和正式发布边界不变。

## 2. 设计目标

1. 用户可在正常系统 Chrome 中使用 TikTok 扫码、手机号、邮箱、用户名或 Google 联合身份完成登录。
2. 登录阶段不得由 Playwright、Chrome DevTools 或其他自动化控制。
3. 不读取用户日常 Chrome 资料夹，不保存 Google 或其他站点会话。
4. 仅在 TikTok 会话和公开账号 handle 均验证通过后，才原子写入正式账号库。
5. 成功、失败、取消、超时或客户端崩溃后，一键发自己创建的临时 Chrome 资料夹都必须进入可恢复的清理流程。
6. 后续静默检测、平台表单检查和正式发布继续复用现有受控浏览器与同一 TikTok 身份合同。

## 3. 不在本轮范围

- 不改用用户日常 Chrome 个人资料。
- 不从浏览器、命令行或内容包导入 Cookie、密码、验证码或二维码链接。
- 不关闭 Google/TikTok 安全检测，不伪装或绕过平台风控。
- 不在本轮接入 TikTok 官方 Login Kit 或 Content Posting API。官方路线需要开发者应用、回调、PKCE、权限与平台审核，作为未来生产路线单独实施。
- 不上传视频、不打开 TikTok Studio 发布表单、不点击 `Post`。本规格的首次真实验收只到“登录并重启后静默识别成功”。

## 4. 方案选择

### 4.1 采用：系统 Chrome + 临时独立资料夹

一键发为每次登录尝试创建自己的临时 Chrome 资料夹，用系统 Chrome 正常打开 TikTok 登录页。登录期间不开启远程调试、自动化或 Playwright。用户完成登录后关闭这个专用窗口，一键发再进入本地会话接收阶段。

优点：支持 Google 联合身份，不使用日常 Chrome 资料，可继续复用现有浏览器发布路由。

代价：必须增加两阶段登录、会话过滤、崩溃恢复和临时资料清理。

### 4.2 不采用：直接用用户已有 Chrome 资料夹

会引入资料库锁、账号串用、扩展干扰和敏感会话暴露，明确禁止。

### 4.3 未来路线：TikTok Desktop Login Kit

TikTok 官方已提供桌面 OAuth 2.0 + PKCE，但需要注册应用、回调 URI、令牌存储和权限审批。该方案最终更适合规模化客户，但不能在当前无生产应用授权的前提下解决本轮登录。

## 5. 组件边界

### 5.1 `app_core/overseas_tiktok_system_login.py`

新增海外专属服务，不将系统 Chrome 细节继续堆入 `myUtils/login.py`。它只负责：

- 解析受支持的系统 Chrome/Edge 可执行文件；
- 创建一次性尝试编号和私有临时资料夹；
- 使用子进程启动正常浏览器；
- 等待用户关闭专用窗口、取消或超时；
- 进入会话接收与账号核验；
- 执行安全清理与崩溃恢复。

登录阶段启动参数只允许正常资料隔离所需参数，例如独立 `--user-data-dir`、`--new-window`、`--no-first-run` 和禁止后台常驻。明确不得出现 `--remote-debugging-port`、`--remote-debugging-pipe`、`--enable-automation` 或 Playwright 启动器。

### 5.2 `app_core/overseas_tiktok_session_scope.py`

新增纯函数会话过滤边界：

- Cookie 域名只允许 `tiktok.com` 或 `.tiktok.com` 后缀；
- 站点存储 origin 只允许明确 TikTok HTTPS origin；
- 任何 Google、YouTube、Facebook 或其他域的 Cookie/origin 都不能进入候选会话；
- 过滤后会话在新的空白浏览器上验证成功前，不能保存账号。

如果 TikTok 未来真实要求新的非 TikTok 域，必须根据当次真实证据显式审查并增加定向测试，不得使用宽泛后缀或“保留全部”降级。

### 5.3 现有路由调整

- `app_core/login_service.py`：TikTok 类型改用新系统浏览器会话；YouTube、Meta 和国内平台路由不变。
- `ui/login_dialog.py`：显示“系统 Chrome 登录”、“请在登录完成后关闭专用窗口”、会话核验、清理和终态；保留取消。
- `app_core/overseas_tiktok_identity.py`：复用公开 handle 回读和主体一致性检查，不重写第二套身份规则。
- `myUtils/login.py`：保留其他现有平台的历史登录逻辑，TikTok 新路由不再使用 `_browser_cookie_gen(6)`。

`ui/login_dialog.py` 和 `app_core/login_service.py` 属于共享核心文件，必须拆成单独共享提交，交由集成线审查后合并。功能分支不改版本号。

## 6. 临时资料与会话交接

### 6.1 临时目录

路径固定在一键发用户数据根下的专用目录，例如：

`<BASE_DIR>/login-staging/tiktok/<attempt-id>/chrome-profile`

要求：

- `<attempt-id>` 只由程序生成，不接受命令行或用户路径；
- 创建后在 POSIX 系统上限制为当前用户可读写访问；
- 清理前必须用解析后路径确认目标仍位于专用 staging 根目录内；
- 不允许 `~`、`$HOME`、应用数据根、默认 Chrome 目录或其他外部目录成为清理目标。

### 6.2 两阶段流程

**阶段 A：人工登录**

1. 创建 staging 尝试记录和临时 Chrome 资料夹。
2. 用系统 Chrome 打开 TikTok 官方登录页。
3. 用户在 Chrome 中完成登录、扫码、Google 身份或必要人工验证。
4. 用户关闭该专用 Chrome 窗口；子进程退出即是“可开始本地核验”，不是“登录已成功”。

**阶段 B：本地接收与核验**

1. 等待 Chrome 资料库锁释放；超时则失败，不强制解锁。
2. 在受控本地上下文重新打开该临时资料夹，不再执行 Google 登录。
3. 只读回会话状态，先过滤为 TikTok-only 候选状态。过滤后没有任何 TikTok 域 Cookie 时按 `tiktok_session_missing` 停止；有 TikTok Cookie 仅表示可交给下一步核验，不能单独证明已登录或保存账号。这样不会因固定 Cookie 名称变化把真实状态过早判为缺失。
4. 关闭该临时资料后，在两个彼此独立的新空白上下文中分别加载同一候选状态，打开 TikTok 首页的只读页面；每个上下文均在页面内从 `script#__UNIVERSAL_DATA_FOR_REHYDRATION__` 的固定 `__DEFAULT_SCOPE__ → webapp.app-context → user → uniqueId/unique_id` 路径解析，并且只把两个公开用户名字段或固定安全状态返回给 Python。不得读取或保存 uid、secUid、Cookie、昵称、页面全文或查询参数。
5. 两个空白上下文必须读到同一唯一 handle；新绑定必须取得该 handle，更新登录必须与旧记录主体一致。
6. 将过滤后候选文件写入私有临时文件。
7. 先清理原 Chrome 临时资料夹；清理成功后才原子替换会话文件和账号记录。

这一顺序保证 Google 会话不会因“账号已保存”而长期留在一键发数据目录。

## 7. 会话和日志安全

- TikTok storage-state 延续现有本机敏感文件权限；不进入 Git、内容包、CLI 参数或长期日志。
- 日志只记录尝试 ID、阶段、安全错误码、是否清理完成和公开 TikTok handle。
- 不记录浏览器历史、Cookie 名值、Google 邮箱、TikTok 登录标识、验证码、二维码或完整页面响应。
- 客户端启动时只按路径和年龄检查自己的 staging 目录；不打开、导入或保存其中的帐号数据。过期尝试只进入安全清理。

## 8. 状态与错误码

登录窗口至少区分：

- `opening_system_browser`：正在启动系统浏览器；
- `waiting_user_login`：用户正在官方页面登录；
- `waiting_browser_exit`：已提示用户关闭专用窗口；
- `validating_tiktok_session`：正在本地过滤和核验会话；
- `cleaning_login_attempt`：正在清理临时资料；
- `success`、`cancelled`、`failed`：明确终态。

稳定错误码：

- `tiktok_system_browser_unavailable`
- `tiktok_login_attempt_timeout`
- `tiktok_login_cancelled`
- `tiktok_login_profile_busy`
- `tiktok_session_scope_invalid`
- `tiktok_session_missing`
- `tiktok_session_expired`
- `tiktok_identity_missing`
- `tiktok_account_identity_ambiguous`
- `tiktok_account_invalid`
- `tiktok_account_identity_mismatch`
- `tiktok_login_cleanup_failed`
- `tiktok_login_commit_failed`

窗口被关闭不等于成功。只有 TikTok-only 会话、唯一公开 handle、临时资料清理和原子保存全部完成才返回成功。

## 9. 崩溃、取消与清理

- 用户取消时，只终止本次尝试的专用 Chrome 子进程；不关闭日常 Chrome。
- 清理只针对经严格路径验证的 `<BASE_DIR>/login-staging/tiktok/<attempt-id>`。
- 清理失败时不保存账号，返回 `tiktok_login_cleanup_failed`，并在下次客户端启动时优先重试清理。
- 启动恢复不会从崩溃的 Chrome 资料中无声恢复或建立账号；过期 staging 只能被清理。
- 新登录开始前必须确认同一账号没有仍在执行的登录尝试。

## 10. 测试设计

### 10.1 纯本地测试

1. TikTok 路由启动系统 Chrome，YouTube、Meta 和国内平台路由不变。
2. 浏览器命令只使用独立 staging 路径，且不含 remote debugging、automation 或用户默认资料。
3. 会话过滤保留 TikTok 域 Cookie/origin，删除 Google 和所有非允许域。
4. 候选会话不能在身份验证前写入正式目录或数据库。
5. 新绑定和更新登录都使用现有 handle 一致性规则。
6. 账号保存、会话替换或清理任一步失败时，不留下可发布的半成品账号。
7. 用户取消只终止当次子进程和目录，不影响其他 Chrome 窗口。
8. 路径穿越、符号链接、应用数据根或日常 Chrome 路径都不能成为清理目标。
9. 超时、崩溃和重启恢复有明确终态，不留永久“登录中”。
10. 日志和 UI 消息不包含 Cookie 名值、Google 账号、验证码、二维码或完整 storage-state。

### 10.2 真实验收

使用当前 TikTok 测试账号，只执行一次登录验收：

1. 从源码客户端点击绑定 TikTok。
2. 系统 Chrome 以一键发专用临时资料夹打开；窗口不显示 Chrome for Testing。
3. 用户通过原有 Google 联合身份或 TikTok 官方方式完成登录并关闭专用窗口。
4. 一键发回读唯一 TikTok handle，账号列表中只新增一条正常 TikTok 账号。
5. 正式会话文件中没有 Google 或非 TikTok 域的 Cookie/origin。
6. 本次 staging 目录已清理，日常 Chrome 窗口、历史和其他登录不受影响。
7. 关闭并重启源码客户端，静默检测仍回读同一 TikTok handle，账号状态正常。

验收到此结束。不上传测试视频，不创建 `platform_form_check`，不点击 `Post`。

## 11. 架构证据

- 架构规格：`docs/architecture/tiktok-system-browser-login-bridge.architecture.json`
- 可交互 HTML：`docs/architecture/tiktok-system-browser-login-bridge.html`
- Archify showcase 验证：9/9 检查通过，组成错误 0，警告 0。
- 视觉检查：1440×900、1600×1000、1920×1080 和 2048×1320 均无横向或纵向溢出；亮色与深色截图已人工检查。

## 12. 当前证据边界

本设计和架构图仅证明方案已形成可实施合同。当前尚未修改登录源码，尚未通过自动测试，也尚未用系统 Chrome 完成真实 TikTok 登录。

## 13. 参考

- [Google：由软件自动化控制的浏览器可能被拦截](https://support.google.com/accounts/answer/7675428)
- [Chrome 136：远程调试需使用非默认用户数据目录](https://developer.chrome.com/blog/remote-debugging-port)
- [TikTok Login Kit Desktop](https://developers.tiktok.com/docs/en/login-kit-desktop)
