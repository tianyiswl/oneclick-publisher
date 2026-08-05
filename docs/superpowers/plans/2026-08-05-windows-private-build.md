# 一键发 Windows x64 私有构建 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 通过 GitHub Actions 的 Windows Runner 生成并提供可下载的“一键发”Windows x64 ZIP，不创建公开 Release，也不打包本机敏感数据。

**Architecture:** `tools/build_windows.py` 负责 Windows 本机构建、浏览器资源复制、离线自检、ZIP 安全扫描和校验值；`.github/workflows/build-windows.yml` 负责在 `windows-latest` 安装依赖、调用脚本并上传私有 Artifact。应用运行时继续从 PyInstaller `_internal/runtime/playwright-browsers` 读取内置 Playwright 浏览器，不改变发布业务逻辑。

**Tech Stack:** Python 3.12、PyInstaller 6.21.0、PyQt6、Playwright 1.60.0、GitHub Actions、unittest。

## Global Constraints

- 构建仅支持 Windows x64；macOS 只运行离线测试，不能声称生成 Windows 可执行文件。
- 构建产物目录与可执行文件采用 ASCII 名称 `Fashetai`，避免 Windows 解压路径兼容问题；用户可见 Artifact 名称仍为“一键发 Windows x64”。
- 内置 Playwright 资源仅包括 `chromium`、`chromium-headless-shell` 与 `ffmpeg` 当前修订；缺少任一资源必须安全失败。
- 不把 `demo-runtime/`、Cookie、storage state、数据库、OAuth token、素材或日志打包；禁止项由 ZIP 内容扫描强制阻断。
- 不新增平台登录、上传、草稿、预检或发布动作；构建时只执行离屏 UI 与本地浏览器页面自检。
- 工作流只能手动触发，权限固定为 `contents: read`，Artifact 保留 30 天，不创建 Release 或 tag。
- 首版无 Authenticode 证书，不伪造“已签名”状态。

---

### Task 1: 为 Windows 构建工具写失败的离线测试

**Files:**
- Create: `test_windows_build.py`
- Create: `tools/build_windows.py`

**Interfaces:**
- Produces: `archive_filename(build_date: str, version: str) -> str`
- Produces: `resolve_playwright_browser_dirs(browser_cache: Path, browsers_manifest: Path) -> list[Path]`
- Produces: `bundle_playwright_browsers(dist_root: Path, browser_dirs: list[Path]) -> Path`
- Produces: `assert_archive_safe(zip_path: Path) -> None`

- [ ] **Step 1: 写失败测试**

```python
from tools.build_windows import archive_filename

def test_archive_filename_is_ascii_and_versioned(self):
    self.assertEqual(
        archive_filename("20260805", "0.4.1"),
        "Fashetai_0.4.1_Windows_x64_20260805.zip",
    )
```

再用临时 `browsers.json` 与目录构造测试：三个必需目录按修订号解析；缺少 `chromium-<revision>` 时抛出 `RuntimeError`；复制目标必须为 `Fashetai/_internal/runtime/playwright-browsers/`；含 `cookiesFile/` 的 ZIP 必须抛出 `RuntimeError`。

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m unittest test_windows_build -v`

Expected: FAIL，因为 `tools.build_windows` 尚不存在。

- [ ] **Step 3: 实现最小 Windows 构建帮助函数**

```python
def archive_filename(build_date: str, version: str) -> str:
    return f"Fashetai_{version}_Windows_x64_{build_date}.zip"

def bundle_playwright_browsers(dist_root: Path, browser_dirs: list[Path]) -> Path:
    target_root = dist_root / "_internal" / "runtime" / "playwright-browsers"
    target_root.mkdir(parents=True, exist_ok=True)
    for source in browser_dirs:
        shutil.copytree(source, target_root / source.name)
    return target_root
```

`resolve_playwright_browser_dirs` 复用 macOS 的 manifest 驱动逻辑，但默认 Windows 缓存位置是 `%LOCALAPPDATA%/ms-playwright`；`assert_archive_safe` 使用标准库 `zipfile.ZipFile` 按文件名扫描禁止项。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m unittest test_windows_build -v`

Expected: PASS，且不启动 PyInstaller、浏览器或平台。

- [ ] **Step 5: 提交**

```bash
git add tools/build_windows.py test_windows_build.py
git commit -m "feat: 增加 Windows 客户端构建工具"
```

### Task 2: 实现 Windows PyInstaller 构建主流程

**Files:**
- Modify: `tools/build_windows.py`
- Modify: `test_windows_build.py`

**Interfaces:**
- Consumes: `APP_VERSION`、`APP_EXECUTABLE_NAME`、`desktop_native_app.py`、Playwright manifest 与缓存目录。
- Produces: `release/windows-YYYYMMDD-v<version>/Fashetai/Fashetai.exe`
- Produces: `release/Fashetai_<version>_Windows_x64_YYYYMMDD.zip` 与同名 `.sha256`

- [ ] **Step 1: 写失败测试**

```python
def test_windows_spec_uses_ascii_executable_and_runtime_target(self):
    spec = build_windows.render_spec(Path("C:/work"))
    self.assertIn("name='Fashetai'", spec)
    self.assertIn("desktop_native_app.py", spec)
```

另写 `assert_windows_platform("darwin")` 必须抛出明确错误的测试，确保 macOS 不会误称完成真实 Windows 构建。

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m unittest test_windows_build.WindowsBuildTests.test_windows_spec_uses_ascii_executable_and_runtime_target -v`

Expected: FAIL，因为 `render_spec` 与平台门禁尚未实现。

- [ ] **Step 3: 实现构建主流程**

实现 `main()`：验证 `sys.platform == "win32"`、调用 `python -m PyInstaller --noconfirm --clean`、把 UI 资源与 `stealth.min.js` 纳入 `_internal`、复制浏览器资源、运行 `Fashetai.exe --ui-test` 和 `--browser-self-test`、创建 ZIP 与 SHA-256 文本。PyInstaller spec 使用 `COLLECT`，不使用 macOS `BUNDLE`、`codesign` 或 `ditto`。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m unittest test_windows_build test_macos_build -v`

Expected: PASS；在 macOS 调用 `python tools/build_windows.py` 失败并明确提示需 Windows 环境。

- [ ] **Step 5: 提交**

```bash
git add tools/build_windows.py test_windows_build.py
git commit -m "feat: 完成 Windows 客户端打包流程"
```

### Task 3: 增加手动 Windows CI 构建与 Artifact 回执

**Files:**
- Create: `.github/workflows/build-windows.yml`
- Modify: `test_windows_build.py`

**Interfaces:**
- Consumes: 当前分支源码与 `tools/build_windows.py`。
- Produces: 名为 `一键发-Windows-x64-<run_number>` 的 GitHub Actions Artifact，包含 ZIP 与 SHA-256。

- [ ] **Step 1: 写失败测试**

```python
def test_windows_workflow_is_manual_and_uploads_private_artifact(self):
    workflow = Path(".github/workflows/build-windows.yml").read_text(encoding="utf-8")
    self.assertIn("workflow_dispatch", workflow)
    self.assertIn("windows-latest", workflow)
    self.assertIn("actions/upload-artifact", workflow)
    self.assertIn("retention-days: 30", workflow)
    self.assertNotIn("release/create-release", workflow)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m unittest test_windows_build.WindowsBuildTests.test_windows_workflow_is_manual_and_uploads_private_artifact -v`

Expected: FAIL，因为 workflow 文件尚不存在。

- [ ] **Step 3: 实现最小工作流**

```yaml
on:
  workflow_dispatch:
permissions:
  contents: read
jobs:
  build:
    runs-on: windows-latest
```

步骤固定为 checkout、Python 3.12、安装项目依赖和 PyInstaller、安装 Chromium、执行构建脚本、上传 `release/Fashetai_*_Windows_x64_*.zip` 及对应 SHA-256。只使用 `actions/upload-artifact@v4`，不调用 GitHub Release API。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m unittest test_windows_build -v`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add .github/workflows/build-windows.yml test_windows_build.py
git commit -m "ci: 增加 Windows 客户端手动构建"
```

### Task 4: 完整验证、推送与远端构建回读

**Files:**
- Verify: `tools/build_windows.py`
- Verify: `test_windows_build.py`
- Verify: `.github/workflows/build-windows.yml`

- [ ] **Step 1: 运行完整本地回归**

Run:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest
.venv/bin/python -m py_compile tools/build_windows.py test_windows_build.py
git diff --check
```

Expected: 全部测试通过、Python 可编译、无差异格式错误。

- [ ] **Step 2: 推送当前分支**

Run:

```bash
git push origin codex/finalize-macos-feedback
```

Expected: 远端分支 SHA 与本地 HEAD 一致。

- [ ] **Step 3: 手动触发并跟踪 Windows 构建**

Run:

```bash
gh workflow run "构建 Windows 客户端" --ref codex/finalize-macos-feedback
gh run watch <run-id> --exit-status
```

Expected: Windows Runner 成功，日志包含 `NATIVE_DESKTOP_UI_OK`、`NATIVE_DESKTOP_BROWSER_OK`、`WINDOWS_BUILD_OK`。

- [ ] **Step 4: 回读 Artifact**

Run:

```bash
gh run view <run-id> --json conclusion,artifacts,url
```

Expected: Artifact 包含 ZIP 与 SHA-256，保留 30 天；无 GitHub Release、无平台发布动作。
