# Direct Background Publish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 内容项目在对话中获得用户对“内容包 + 账号 + 排期”的一次明确授权后，可以直接创建后台正式发布任务；默认不再单独登录做平台预检，仅在扫码、验证码或未知平台弹窗时暂停等待用户。

**Architecture:** 保留现有 `preflight` 路由作为手动诊断工具，新增 `direct` 路由作为对话授权后的默认发布入口。`direct` 只跳过“独立的平台预检任务”，不跳过正式会话内的字段写入、回读和提交前检查。授权指纹绑定内容、账号和排期，创建后立即原子消费；UI、CLI 和 MCP 继续共用 `publish_service` 及各平台执行器。

**Tech Stack:** Python 3.12, SQLite, Playwright, FastMCP, unittest.

**Spec:** `docs/architecture/direct-background-publish.lifecycle.html`

---

### Task 1: 修正公众号普通发表确认弹窗的路由

**Files:**
- Modify: `test_wechat_publish_policy.py`
- Modify: `app_core/wechat_publish_policy.py`
- Modify: `app_core/wechat_publish_executor.py`

**Steps:**
1. 新增失败测试：当 `wechatGroupNotification=false` 且弹窗精确匹配“未开启群发通知”文案时应允许“继续发表”；文案或按钮改变时必须拒绝。
2. 运行 `test_wechat_publish_policy.py` 确认新测试按预期失败。
3. 让群发范围决策按用户实际选项分流，正式执行器只保留一个严格入口。
4. 重跑该测试文件并确认通过。

### Task 2: 新增不依赖独立预检的一次性直发授权

**Files:**
- Modify: `test_controlled_publish.py`
- Modify: `app_core/controlled_publish.py`

**Steps:**
1. 新增失败测试：`direct` 载荷必须为 `runtimeMode=publish` 、`debugDryRun=false` 、`backgroundMode=true`，且不携带旧预检 ID。
2. 新增失败测试：直发授权只能消费一次，且任何标题、正文、素材、账号或排期变化都必须拒绝。
3. 实现独立的直发授权表与原子创建/消费，不保存 Cookie、密码或正文。
4. 新增 `direct` 请求模式；保留旧 `preflight/formal` 兼容路由。
5. 增加同指纹已成功或已点击最终发布后的去重保护，禁止模糊状态自动重发。
6. 重跑 `test_controlled_publish.py` 并确认通过。

### Task 3: 把直发能力接入内容项目网关与 MCP

**Files:**
- Modify: `test_content_project_gateway.py`
- Modify: `test_oneclick_mcp_server.py`
- Modify: `test_silicon_evolution_auto_publish.py`
- Modify: `app_core/content_project_gateway.py`
- Modify: `app_core/oneclick_mcp_server.py`
- Modify: `app_core/silicon_evolution_auto_publish.py`
- Modify: `app_core/controlled_publish.py`

**Steps:**
1. 新增失败测试：通用内容项目可以通过明确授权的直发方法创建任务，不要求预检 taskId。
2. 新增失败测试：硅基进化旧的“预检 + 正式”参数仍然可用，但新默认路由为后台直发。
3. 在 MCP 工具中把用户对当次内容的明确“发布”指令转换为一次性直发授权，不接收 Cookie、密码或验证码。
4. 更新工具说明：独立平台预检仅用于人工诊断，不是直发的前置步骤。
5. 重跑三个相关测试文件。

### Task 4: 返回可轮询的等待验证状态

**Files:**
- Modify: `test_controlled_publish.py`
- Modify: `app_core/controlled_publish.py`
- Modify: `app_core/wechat_publish_executor.py`

**Steps:**
1. 新增失败测试：任务事件包含公众号验证、用户确认或提交后核对时，任务 JSON 返回稳定 `stage` 和不含敏感信息的 `userAction`。
2. 在任务投影中只读取事件类型和公开文案，不返回二维码图片、验证码或会话内容。
3. 确保验证完成后同一浏览器会话继续执行，不重新创建平台预检任务。
4. 重跑相关单测。

### Task 5: 同步文档并分层验证

**Files:**
- Modify: `SOURCE_OF_TRUTH.md`
- Modify: `QUALITY_GATES.md`
- Modify: `docs/architecture/content-project-publish-contract.md`
- Modify: `docs/architecture/direct-background-publish.lifecycle.json`

**Steps:**
1. 写清“独立预检”和“正式会话内提交前检查”的差异，以及对话明确发布指令的一次性授权语义。
2. 运行最相关测试；如果出现失败，立即停下并修复。
3. 运行受影响的发布、MCP、公众号执行器测试。
4. 收尾时再运行项目全量离线测试，并明确说明本轮没有真实平台正式发布验收。
