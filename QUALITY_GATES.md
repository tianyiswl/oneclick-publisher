# 一键发三线质量门

## 一、文件边界

### 数据监测线

主要专属范围：

- `app_core/platform_data_*`
- `app_core/*_data_collector.py`
- `app_core/domestic_data_collector.py`
- `ui/data_monitor_page.py`
- 对应 `test_*data*`、数据架构、数据报告和验收记录

### 海外登录发布线

主要专属范围：

- `app_core/overseas/**`
- `app_core/overseas_*`
- `uploader/tk_uploader/**`
- `uploader/youtube_uploader/**`
- `uploader/meta_uploader/**`
- `ui/overseas_authorization_dialog.py`
- 对应海外测试、状态文档和 ADR

### 国内定位线

主要专属范围：

- `app_core/douyin_location*`
- `app_core/douyin_commerce*`
- `app_core/_douyin_commerce*`
- `app_core/xhs_location*`（小红书视频定位，仅限视频场景）
- `ui/douyin_commerce_page.py`
- 对应抖音地点、带货测试、设计和验收记录，以及文件名含 `xhs-location`/`xhs_location` 的小红书视频定位测试、设计和验收记录

### 共享核心，仅集成线可直接修改

- `AGENTS.md`
- `SOURCE_OF_TRUTH.md`
- `QUALITY_GATES.md`
- `README.md`
- `requirements-oneclick.txt`
- `conf.py`
- `desktop_native_app.py`
- `app_core/account_service.py`
- `app_core/account_browser_service.py`
- `app_core/database.py`
- `app_core/paths.py`
- `app_core/publish_service.py`
- `app_core/oneclick_preflight.py`
- `app_core/xhs_native_adapter.py`
- `app_core/xhs_publish_executor.py`
- `app_core/oneclick_authorization.py`
- `app_core/oneclick_capabilities.py`
- `app_core/branding.py`
- `ui/account_page.py`
- `ui/login_dialog.py`
- `ui/publish_page.py`
- `ui/main_window.py`
- `myUtils/postVideo.py`
- `tools/build_*`
- `.github/**`

功能线确需修改共享核心时，必须把共享修改拆成独立提交；该提交不能由功能线自行合并。

提交前检查（同时检查分支已提交、暂存、未暂存和未跟踪文件）：

```bash
.venv/bin/python tools/check_workstream_scope.py --stream data --base origin/main
.venv/bin/python tools/check_workstream_scope.py --stream overseas --base origin/main
.venv/bin/python tools/check_workstream_scope.py --stream location --base origin/main
```

输出出现 `shared` 或 `outside` 时，退出码为 `2`，必须转交集成线判断。

## 二、自动测试

### 每次功能提交

先运行对应专属测试，再运行受影响模块。最低入口：

```bash
# 数据监测
.venv/bin/python -m unittest -v \
  test_platform_data_service test_platform_data_sync test_data_monitor_page \
  test_douyin_data_collector test_xiaohongshu_data_collector \
  test_wechat_data_collector test_bilibili_data_collector test_kuaishou_data_collector

# 海外登录发布
.venv/bin/python -m unittest -v \
  test_overseas_integration test_overseas_publish_routing \
  test_overseas_video_publish test_meta_browser_publish

# 国内定位
.venv/bin/python -m unittest -v \
  test_douyin_location test_douyin_location_cache \
  test_douyin_location_preset_service test_douyin_commerce_service \
  test_douyin_commerce_collectors test_douyin_commerce_batch_service \
  test_douyin_commerce_batch_executor
```

### 每次共享核心合并

必须完整运行：

```bash
.venv/bin/python -m unittest discover -p 'test_*.py'
QT_QPA_PLATFORM=offscreen .venv/bin/python desktop_native_app.py --ui-test
git diff --check
```

局部通过不能写成全量通过。测试数量随功能变化，以本次完整命令输出为准。

## 三、真实运行验收

### 数据监测

1. 同一真实账号连续同步两次，不自动重试掩盖失败。
2. 两次运行 ID 不同，指标和作品归属当前账号。
3. SQLite 能读回最新批次，界面刷新后显示同一批次。
4. 未提供字段保持 `null/—`，不写成 `0`。
5. 浏览器、页面和临时会话全部关闭；失败不清空上次成功数据。

### 海外登录发布

1. 专用测试账号完成可见登录，重启客户端后静默检测仍成功。
2. 单账号、单视频完成字段和素材预检，停在最终按钮前。
3. 只有获得当次明确授权才执行最终动作。
4. 平台明确成功回执或内容列表读到目标内容后才记成功。
5. 验证码、两步验证、风控、未知提示或结果不明确时安全停止，不自动盲重试。

### 国内定位

1. 按账号、平台、范围和完整关键词隔离缓存。
2. 候选必须回读地点名称、完整地址和平台可读标识；歧义时停止。
3. 预检和正式提交都重新从当前编辑页核验，不以缓存命中代替写入。
4. 真实发布必须另获当次授权；平台回执或作品管理页读回后才记成功。
5. 只对已经确认支持定位的平台显示并传递字段，不用一个通用字段假装全平台支持。

## 四、集成和发布

1. 三条功能线可以并行开发，但一次只合并一条到集成线。
2. 每次合并共享核心后重新跑全量测试和离屏 UI；上一条线的结果不能替代本次。
3. 三线全部通过后，集成线统一递增版本；当前 `0.4.2` 不在功能分支改动。
4. 打包前核对应用界面、Mac 包、Windows 包名和构建产物版本一致。
5. Mac 真实启动和 Windows 候选包验证分别记录；Mac 通过不能代替 Windows 真机。
