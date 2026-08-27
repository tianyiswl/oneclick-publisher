# Local Controlled Publish Interface Implementation Plan

**Goal:** 让 Codex 通过本机 CLI 创建和查询一键发预检任务，并在用户于对话中一次确认后用短期一次性授权继续正式发布；同时修复 008 的导入、文案、抖音正文清空和小红书封面问题。

**Architecture:** 新增请求归一化、授权和任务投影模块，复用现有 `publish_service` 与 SQLite；桌面 UI 和 CLI 共享服务。内容包加载器承担旧 schema 显式迁移，平台执行器只处理已经净化的字段。

**Spec:** `docs/superpowers/specs/2026-08-24-local-controlled-publish-interface-design.md`

## Task 1: 固定 008 回归合同

- 在 `test_content_bundle.py` 增加当前/旧 AI 声明、平台覆盖和预览 Markdown 测试。
- 在发布页测试中增加国内、海外、混合按钮文案和导入字段测试。
- 先运行命名测试确认失败，再做最小修复。

## Task 2: 修复内容包与预检文案

- `app_core/content_bundle.py` 显式迁移旧 AI 声明并提供纯净平台字段。
- `ui/publish_page.py` 不再把平台预览文档写入通用正文；按实际平台生成按钮和说明。
- 运行 Task 1 测试和发布页受影响测试。

## Task 3: 本地受控请求、授权和状态投影

- 新增 `app_core/controlled_publish.py`：严格请求模型、规范化指纹、稳定 JSON 投影。
- 新增 `app_core/publish_authorization.py`：SQLite 授权表、10 分钟 TTL、原子一次消费。
- 在 `publish_service` 提取 UI/CLI 共用入口；在 `desktop_native_app.py` 增加 create/status/authorize/worker 子命令。
- 测试缺省预检、明确账号、混合 schedule、过期/重放/篡改拒绝和任务回读。

## Task 4: 抖音正文清空

- 为 macOS 与 Windows/Linux 全选键写失败测试。
- 编辑器按运行平台使用 `Meta+A` 或 `Control+A`，清空后回读再写入。
- 运行抖音上传器相关测试。

## Task 5: 小红书当前封面入口

- 使用已有登录态只读打开当前视频发布页，记录脱敏页面阶段和可见封面控件。
- 先把当前 DOM 合同写成适配器测试，再更新选择器和唯一性诊断。
- 只完成封面入口回读预检，不点击最终发布。

## Task 6: 008 预检与收尾

- 用兼容包加明确账号和分平台 schedule 创建预检任务并轮询。
- 分别记录抖音、小红书的 task ID、预检状态和固定错误；不触发正式公开发布。
- 运行受影响模块测试，最后再跑完整测试和离屏 UI；更新架构图、验证报告和当前事实文件。
