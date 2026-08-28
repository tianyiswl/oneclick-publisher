# Task 5 离线回归与源码联调启动就绪记录

状态：`NEEDS_CONTEXT`

本记录只覆盖真实登录前的离线检查和启动器静态复核。未启动源码客户端、未读取已安装账号数据、未打开系统浏览器或平台页面；没有选择素材、创建任务、调用 `platform_form_check` 或点击 `Post`。

## 2026-08-28 离线结果

- 工作树提交：`e73e808`。
- Step 1（TikTok 登录聚焦梯）：`126/126` 通过。
  - 命令：`../../.venv/bin/python -m unittest -v test_overseas_tiktok_session_scope test_overseas_tiktok_system_login test_overseas_tiktok_identity test_overseas_integration test_account_detection_ui`
  - 测试以替身验证系统浏览器命令、会话净化、身份绑定和 UI 生命周期；本次执行没有创建真实系统浏览器进程或访问平台。
- Step 2（海外发布和路由回归）：`132/132` 通过。
  - 命令：`../../.venv/bin/python -m unittest -v test_overseas_tiktok_publish test_overseas_video_publish test_overseas_publish_routing test_overseas_youtube_login test_overseas_youtube_publish test_meta_browser_publish`
  - 测试均为本地替身/合同检查；本次执行没有到达任何平台。
- Step 3：指定六个文件的 `py_compile` 通过；`git diff --check` 无输出；使用 `QT_QPA_PLATFORM=offscreen` 的完整发现式回归为 `2450/2450` 通过（`77.775s`）。
- 完整回归有一条未失败的 `ResourceWarning`，不得称为无警告通过：
  - 测试：`test_douyin_commerce_collectors.DouyinCommerceCollectorManagerTests.test_untrusted_exception_string_methods_are_never_called_by_diagnostics`
  - 输出：`ResourceWarning: unclosed event loop <_UnixSelectorEventLoop running=False closed=False debug=False>`；运行未提供对象分配 traceback，仅提示可用 tracemalloc 获取。

## 源码联调启动器静态就绪

- `tools/run_source_live.py --page accounts` 的参数入口存在，并会在启动真实界面前检查正式客户端/受控发布冲突、创建正常备份、写入独占标记；客户端退出后清除自己的标记。
- 完整回归已覆盖启动器/运行时相关测试。尚未执行该启动命令，因此没有创建备份、标记或客户端进程，也没有读取正式账号数据。
- 结论：启动器静态就绪，真实启动仍须由控制器发起，并先确保已安装客户端已关闭。

## 等待的唯一上下文

控制器启动 `../../.venv/bin/python tools/run_source_live.py --page accounts` 后，需要用户在专用系统 Chrome 窗口完成 TikTok/Google 凭据、设备确认或 CAPTCHA（如出现）。完成并关闭该专用窗口后，才能继续执行本任务规定的本地 TikTok-only 结构检查、账号保存和重启同主体回读。

## 真实登录失败边界（待修复）

- macOS 系统 Google Chrome 已在独立 `0700` 临时 profile 内完成人工登录；关闭该专用窗口后，本地候选回读返回 `tiktok_session_expired`。
- Chrome 终端诊断仅显示 macOS 钥匙串查询未获授权、加密不可用和令牌加密失败；临时 profile 因而未能持久保存登录态。未记录任何凭据、Cookie、存储值或会话文件路径。
- 取舍：采用 Chromium 官方测试常用的 `--use-mock-keychain`，只在 macOS 的系统 Google Chrome 临时登录命令中加入。该参数使专用临时 profile 不依赖用户钥匙串；不会写入正式 Chrome profile，也不改变 Windows、Linux 或 Edge 命令。修复后必须重新完成一次人工登录验收。

## 修复后的离线验证

- 先新增 macOS Chrome 专用参数测试，未修改实现时按预期失败；随后只修改系统浏览器命令构造逻辑。
- Task 2 生命周期套件：`44/44` 通过。
- Task 3 会话范围、生命周期和身份套件：`69/69` 通过。
- Task 5 TikTok 聚焦套件：`127/127` 通过。
- 三个套件均只使用本地替身，不会启动真实浏览器或到达平台。真实登录、保存账号、TikTok-only 状态结构检查和重启回读仍未重新执行。

## 审查补强：mock keychain fail-closed

- 新增拒绝测试并先按预期失败：普通目录、缺失目录和 POSIX `0755` 的 profile 都曾会被 macOS Chrome 命令静默带上 `--use-mock-keychain`。
- 现已复用受控目录解析：macOS Google Chrome 只有在 profile 精确属于 `login-staging/tiktok/<32 位小写十六进制>/chrome-profile`、所有相关路径均非软链接且存在、profile 在 POSIX 下为 `0700` 时，才会加入该参数；任何不符情形均返回 `tiktok_login_cleanup_failed`，不产生降级命令。
- 由 `create_login_attempt` 创建的合法 profile 仍通过。Windows、Linux 和 Edge 均不触发该校验或新增参数。
- 补强后离线验证：Task 2 `45/45`、Task 3 `70/70`、TikTok 聚焦 `128/128`。没有启动真实浏览器或平台。

## 真实登录第二轮后的修复（待第三轮验收）

- 新证据：`--use-mock-keychain` 后，源码日志不再出现钥匙串或加密失败；但 macOS 点击最后一个窗口的红色关闭后，受控 Chrome 进程仍存活，流程停在等待退出。控制器终止该受控进程后，首次身份回读仍返回 `tiktok_session_expired`，未保存账号。
- 先红后绿：新增完成事件、取消优先、受控进程未退出时 fail-closed、UI 按钮状态、延迟 profile link，以及登录/挑战路由与 Studio 选择器失败的测试。新增测试在实现前均因缺少接口或行为而失败；实现后全部通过。
- 现在 TikTok 登录框只会在系统 Chrome 已打开或等待退出时启用“完成登录并保存”。它只请求优雅结束本次受控 Chrome 进程，进程退出后仍会执行原有的 TikTok-only 会话和两次身份核验；按钮点击本身不代表登录成功。取消仍优先且丢弃本次登录。若专用进程未能退出，固定失败为 `tiktok_login_cleanup_failed`，不升级为成功。
- 主体回读现在最多采样 5 次、每次间隔 0.15 秒，要求同一公开 handle 连续两次一致；多个 handle 仍立即拒绝。登录/挑战路由上的无主体回读为 `tiktok_session_expired`；已认证 Studio 路由上仍无唯一 handle 为 `tiktok_account_invalid`。诊断不保存 URL、页面文本、Cookie 或账号秘密。
- 离线验证：Task 2 `50/50`；Task 3 `76/76`；账号 UI `20/20`；TikTok 聚焦 `135/135`。所有测试均为本地替身，没有启动真实浏览器、上传或发布。

## 真实登录第三轮后的裁定与修复（待第四轮验收）

- 新证据：用户保持系统 Chrome 的 TikTok 登录窗口并点击“完成登录并保存”后，客户端报 `tiktok_login_cleanup_failed`。随后只读确认本次受控 Chrome 进程实际已退出，但临时 staging 下仍保留一个目录。
- 根因：完成动作把普通轮询间隔 `0.2` 秒错误地当作 Chrome 优雅退出等待上限，进程在稍后自然退出时已被过早判定失败。
- 裁定：完成动作使用独立、默认最多 `5` 秒且可注入的优雅退出等待；只对本次受控进程发送终止请求，不调用 kill，不影响日常 Chrome。等待内退出返回正常关闭并继续既有核验；持续不退仍为 `tiktok_login_cleanup_failed` 并保留 staging。取消和总超时的既有安全语义不变。
- 先红后绿：新增“约 1 秒后退出”替身测试，旧实现按预期返回 `cleanup_failed`；修复后返回 `closed`。既有“从不退出且不 kill”“只操作 owned process”和取消优先测试仍通过。
- 本轮离线验证：TikTok 聚焦 `136/136`；海外受影响回归 `132/132`；账号 UI `20/20`。没有启动真实浏览器、上传、发布、推送或合并。

## 真实登录第八轮后的会话状态裁定（仍待真实验收）

- 新事实：用户已在新的受控系统 Chrome 窗口完成 TikTok 登录并点击“完成登录并保存”；约 28 秒后客户端以 `tiktok_session_missing` 停止，账号总数仍为 `11`，没有写入账号。终端未提供应用级安全诊断，未输出或检查任何 Cookie 值、凭据或敏感页面内容。
- 根因：候选接收在 TikTok-only 过滤后要求固定的私有登录 Cookie 名称。该名称集既不能证明浏览器确实没有保存状态，也可能因平台名称变动而把有 TikTok 状态的已登录临时 profile 过早归为 `tiktok_session_missing`。
- 裁定：过滤后没有 TikTok 域 Cookie 时，固定以 `tiktok_session_missing` 停止，并记录 `tiktok_cookie_present=false` 和阶段 `tiktok_persistent_state`。有 TikTok 域 Cookie 时该布尔值只表示候选状态存在，绝不等同认证；系统仍关闭 persistent profile，并在两个独立 blank context 中分别只读主页公开 handle。只有两次同一唯一 handle 才能继续保存。纯游客状态会在 blank identity 阶段以固定错误码停止，绝不写入账号。
- 实现提交：`958815a`（`fix(tiktok): verify unknown session cookies by identity`）。先红后绿：新增未知名称 TikTok 状态进入两次 blank 核验、无 TikTok 状态仍为 missing、游客状态缺少公开身份时停止的替身测试；不记录名称或值。
- 离线验证：TikTok 聚焦 `158/158`；受影响海外回归 `132/132`；语法与空白检查通过。所有测试均为本地替身，未启动真实浏览器、未访问平台、未上传、未进入表单、未发布。
- 剩余边界：仍需由控制器在新源码下进行一次人工真实登录、保存及重启同 handle 回读。此修复不把任何 Cookie 当成已登录证明；若两个 blank context 不能稳定读到同一公开 handle，仍安全拒绝且不保存账号。
