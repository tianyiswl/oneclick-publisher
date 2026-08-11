# 抖音发布中心验证与 AI 声明实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use executing-plans to implement this plan task-by-task.

**目标：** 为标准发布中心的抖音正式发布补齐短信/扫码二次验证交互，修复“AI 生成内容”未写入平台的问题，并按“失败即停—修复—单项验证—从头全量验证”的顺序完成验收。

**架构：** 继续使用现有 `DouyinVerificationBroker` 和 `DouyinVerificationDialog`，验证码与二维码仅保存在当前进程内存中。标准抖音执行器向上传器注入验证回调，上传器点击发布后检测到挑战时暂停原 Playwright 会话；发布中心收到任务事件后打开原生验证窗口，完成验证后继续等待作品管理页回执。AI 声明沿现有公共载荷的 `aiGenerated` 字段传到上传器，并以编辑页回读“内容由AI生成”作为成功条件。

**技术栈：** Python 3、PyQt6、Playwright、unittest、现有任务事件与验证代理。

---

### 任务 1：补齐标准抖音验证协调链路

**文件：**
- 修改：`uploader/douyin_uploader/main.py`
- 修改：`app_core/douyin_publish_executor.py`
- 测试：`test_douyin_publish_executor.py`

1. 先增加失败测试，证明正式发布执行器会向上传器提供验证回调，并记录 `douyin_verification_required` 事件。
2. 让上传器将可选验证回调传给 `_wait_formal_publish_result`。
3. 在标准执行器中创建短信或二维码内存请求，等待原生客户端完成处理，再继续等待平台回执。
4. 验证取消、超时、错误验证码和二维码状态变化均安全停止，不误判为成功。

### 任务 2：把验证窗口接入发布中心

**文件：**
- 修改：`ui/publish_page.py`
- 测试：`test_douyin_verification_dialog.py`
- 测试：新增或扩展发布页相关测试

1. 为微信与抖音验证代理使用独立别名，避免请求串台。
2. 发布页监听 `douyin_verification_required` 事件，并在任务轮询期间主动检查请求，避免事件与界面轮询之间的时序丢失。
3. 同一任务只显示一个抖音验证窗口；任务结束后安全关闭引用。

### 任务 3：修复 AI 生成内容声明及回读

**文件：**
- 修改：`app_core/douyin_publish_executor.py`
- 必要时修改：`app_core/oneclick_preflight.py`
- 测试：`test_douyin_publish_executor.py`
- 测试：相关预检测试

1. 校验并规范化 `aiGenerated` 为布尔值。
2. 标准抖音发布将该字段传入 `DouYinVideo.ai_generated`，不再硬编码为 `False`。
3. 勾选时要求编辑页回读“内容由AI生成”，并将回读写入任务事件和发布回执。
4. 预发布检查同步覆盖声明填写与回读，但不点击正式发布、不保存草稿。

### 任务 4：单项与全量验证

**文件：**
- 测试：`test_douyin_publish_executor.py`
- 测试：`test_douyin_verification.py`
- 测试：`test_douyin_verification_dialog.py`
- 测试：与发布中心、抖音预检有关的测试文件

1. 运行验证代理、验证窗口、标准执行器和 AI 声明的单项测试。
2. 运行抖音发布中心相关完整自动化测试集合；出现首个失败即停止并修复。
3. 使用当前账号与测试素材执行一次全链路预发布检查，确认账号、素材、标题、文案、话题、定位和 AI 声明均有平台回读，且没有保存草稿或公开发布。
4. 如需验证最终点击发布后的真实短信/扫码挑战，停在公开发布授权边界，取得用户明确确认后再执行。
5. 全部通过后重启源码客户端，确认窗口正常显示。
