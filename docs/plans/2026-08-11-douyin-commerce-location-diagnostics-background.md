# Douyin Commerce Location Diagnostics and Background Mode Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 修复抖音带货正式发布无法恢复英文混排地点的问题，并提供任务级详细日志、自动日志分段和可切换的前后台发布模式。

**Architecture:** 在界面选择层保存原始搜索词，在批次契约层白名单化字段，在执行器和页面适配层完成有界重搜与诊断。日志清理由批次完成状态驱动，前后台选择通过批次载荷传入每条正式会话。

**Tech Stack:** Python 3.13、PyQt6、Playwright、unittest。

---

### Task 1: 地点搜索意图贯穿批次载荷

**Files:**
- Modify: `ui/douyin_commerce_page.py`
- Modify: `app_core/douyin_commerce_batch_service.py`
- Modify: `app_core/douyin_commerce_service.py`
- Modify: `app_core/douyin_commerce_batch_executor.py`
- Test: `test_douyin_commerce_service.py`

**Steps:**
1. 添加失败测试，断言视频地点预设保存 `searchKeyword`，批次校验和单条载荷保留该字段。
2. 添加失败测试，断言 `JOYMARK` 原始词优先，旧预设可提取英文品牌短词。
3. 实现最小字段传递和有界关键词生成。
4. 运行地点载荷与关键词相关测试。

### Task 2: 地点恢复详细诊断

**Files:**
- Modify: `app_core/douyin_commerce_batch_executor.py`
- Modify: `app_core/douyin_commerce_service.py`
- Test: `test_douyin_commerce_service.py`

**Steps:**
1. 添加失败测试，断言每个关键词的搜索失败和候选不匹配会写入明确日志。
2. 在批量执行器记录视频、范围、目标和有序关键词。
3. 在页面适配层记录每次搜索结果数量及匹配失败原因。
4. 将固定地点错误码翻译为包含下一步建议的中文诊断。
5. 运行地点原子应用与批量失败汇总测试。

### Task 3: 新任务自动清空客户端日志

**Files:**
- Modify: `ui/douyin_commerce_page.py`
- Test: `test_douyin_commerce_service.py`

**Steps:**
1. 添加失败测试：批次结束不立即清空；下一次建立平台设置代际时清空。
2. 增加下一任务清理标记，并仅调用 `runtime_log_bus().clear()`。
3. 清空后写入新的任务起始提示。
4. 运行执行日志面板与批次完成状态测试。

### Task 4: 默认后台运行与可见诊断模式

**Files:**
- Modify: `ui/douyin_commerce_page.py`
- Modify: `app_core/douyin_commerce_batch_service.py`
- Modify: `app_core/douyin_commerce_service.py`
- Modify: `app_core/douyin_commerce_session.py`
- Test: `test_douyin_commerce_service.py`

**Steps:**
1. 添加失败测试：复选框默认勾选，批次和单条载荷保留 `backgroundMode`。
2. 添加失败测试：后台为 headless，取消后台为可见窗口。
3. 在检查页统一底部操作栏加入复选框及说明。
4. 将 `backgroundMode` 传入正式上传和提交上下文，移除硬编码后台模式。
5. 运行界面、浏览器启动策略和提交回执测试。

### Task 5: 回归验收

**Files:**
- Test: `test_douyin_commerce_service.py`
- Test: `test_douyin_publish_executor.py`

**Steps:**
1. 运行新增测试及地点、批次、日志、后台模式相关测试。
2. 运行抖音带货完整测试文件。
3. 运行源码客户端自检，确认复选框布局和日志面板正常。
4. 检查 Git 差异，确认不包含 `marketing/` 等用户文件。
