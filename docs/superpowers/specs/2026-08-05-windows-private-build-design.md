# 一键发 Windows x64 私有构建设计

## 目标

在 GitHub Actions 的 Windows Runner 上生成可下载的“一键发”Windows x64 客户端 ZIP。产物用于内部验收，不创建 GitHub Release，不把任何账号、会话、素材或运行数据写入 Git 或安装包。

## 已锁定决策

- 平台：Windows x64，构建环境为 `windows-latest`。
- 分发：仅 GitHub Actions Artifact，保留 30 天；不创建公开 Release。
- 触发：仅 `workflow_dispatch` 手动触发，避免每次源码提交自动消耗构建额度。
- 打包：PyInstaller `one-dir`，可执行文件采用独立项目 ASCII 标识 `YiJianFa.exe`；应用窗口与 Artifact 显示名均使用“一键发”。
- 浏览器：随包携带 Playwright Chromium、headless shell 与 ffmpeg；发布执行仍使用一键发自己的本地运行目录。
- 签名：首版不伪造 Authenticode 签名。没有 Windows 代码签名证书时，产物会明确标记为未签名测试包。

## 架构

### Windows 构建脚本

新增 `tools/build_windows.py`，只允许在 Windows 上执行。脚本读取 `app_core.branding.APP_VERSION`，从 Playwright 的 `browsers.json` 解析当前依赖的 Chromium、headless shell 与 ffmpeg 修订号，并从 Windows 缓存复制对应目录。

PyInstaller 生成目录型应用 `release/windows-YYYYMMDD-v<版本>/YiJianFa/`。脚本将浏览器资源放在 PyInstaller 的 `_internal/runtime/playwright-browsers/` 下；运行时 `_MEIPASS` 正是 `_internal`，因此现有 `utils.base_social_media._configure_bundled_playwright_browsers()` 能找到该目录，无需读取系统浏览器、Cookie 或开发机路径。

脚本随后执行 `YiJianFa.exe --ui-test` 和 `YiJianFa.exe --browser-self-test`，将通过的目录打包为 ASCII 文件名 ZIP，并以标准库 `zipfile` 扫描禁止项。ZIP 内不得出现 `demo-runtime/`、Cookie、storage state、数据库、OAuth token 或媒体素材目录。脚本输出 ZIP 绝对路径、SHA-256 和 `WINDOWS_BUILD_OK`。

### GitHub Actions

新增 `.github/workflows/build-windows.yml`：

1. 手动选择当前分支或指定分支执行。
2. 使用 Python 3.12，安装 `requirements-oneclick.txt` 与 PyInstaller。
3. 执行 `python -m playwright install chromium`，确保浏览器缓存与当前 Playwright 版本一致。
4. 执行 `python tools/build_windows.py`。
5. 上传 ZIP 与同名 SHA-256 文件为单个 Actions Artifact，保留 30 天，并在构建摘要显示文件名和校验值。

工作流权限仅为 `contents: read`，不会访问账号、密钥或平台后台。

## 安全边界

- 只打包源码、只读界面资源、依赖和浏览器二进制；运行时目录、账号资料、Cookie、数据库、日志、素材和本地预检夹具均不进入包。
- CI 只运行离屏 UI 与本地浏览器页面自检，不登录、不上传、不建草稿、不预检、不发布。
- Actions 不创建 Release、tag 或其他公开分发对象。
- Windows 客户端的首次运行可能受 SmartScreen 提示影响；这是未使用 Authenticode 证书的可见系统提示，不代表安装包已被签名。

## 验收标准

- 离线测试覆盖：Windows 浏览器缓存目录解析、缺少浏览器资源时安全失败、浏览器资源复制目标、ZIP 禁止项扫描与 Windows ZIP 命名。
- 代码检查：构建脚本可被 macOS 开发环境导入并运行离线测试，但 `main()` 在非 Windows 上明确拒绝真实构建。
- 远端回执：Actions 构建任务成功，日志同时包含 `NATIVE_DESKTOP_UI_OK`、`NATIVE_DESKTOP_BROWSER_OK` 与 `WINDOWS_BUILD_OK`；Artifact 中存在 ZIP 与 SHA-256 文件。

## 非目标

- 不生成 Windows 安装程序、MSI、自动更新器或数字签名。
- 不修改任何平台发布、登录、账号管理或业务数据库逻辑。
- 不上传构建产物到 Git、Git LFS、GitHub Release 或外部存储。
