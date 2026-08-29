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
- `app_core/login_service.py`
- `app_core/account_service.py`
- `app_core/account_browser_service.py`
- `app_core/database.py`
- `app_core/paths.py`
- `app_core/publish_service.py`
- `app_core/publish_runtime.py`
- `app_core/tiktok_schedule_contract.py`
- `app_core/controlled_publish.py`
- `app_core/task_service.py`
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
- `myUtils/login.py`
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

### 内容包与平台实体

1. 用结构化 manifest 导入时，标题、正文、话题、提及、封面、AI 声明和平台覆盖分别进入目标字段；正文中不得出现发布包说明标题或平台章节。
2. 小红书话题必须位于完整正文之后并连续排列；全部插入后回读正文顺序和官方话题实体，位置错误返回稳定错误码并停止提交。
3. 抖音话题和提及必须来自官方候选并回读为平台实体；普通 `#文字`、`@文字` 存在不算成功，候选失败时不得降级为普通文字。
4. 预检和正式发布共用同一字段合同、实体识别和错误码；同一输入不得出现“预检按文字通过、正式按实体失败”。
5. 旧 schema 只能通过有测试的显式迁移进入新合同；无法迁移时给出可定位错误，不得静默污染正文。

### 公众号只保存草稿桥

1. 只接受重新校验通过的 V1.2 `release_ready` 冻结包，且包内必须明确 `publish_allowed=false`；交接文件不得携带正文、Cookie、Token、二维码或其他凭据。
2. 桌面开关默认关闭；只有用户首次确认“仅保存草稿，不发表、不群发、不定时发表”后才轮询本地收件箱，关闭开关后必须停止轮询。
3. 草稿执行只接受 `runtimeMode=wechat_draft`，并强制关闭群发和定时；不得导入正式发表策略、查找或点击发表控件。
4. 编辑页与草稿列表都必须回读同一账号；保存后只在同标题、同保存时间窗口且带封面的唯一草稿存在时成功，缺失或多条匹配都安全停止。
5. 交接、锁和回执必须原子写入；同一文章 ID 和冻结包哈希已存在终态回执时不得重复建草稿，并发任务不得重复消费同一交接。
6. 内容项目只消费文章 ID 和冻结包哈希完全匹配的 `draft_readback_confirmed` 回执；该证据不得改变文章的发表状态，也不得记为已经发表。
7. 离线测试和离屏界面通过只代表桥接源码存在；必须用下一篇正常 V1.2 包完成一次真实“保存草稿 + 后台草稿列表唯一回读”，才能记为平台草稿能力已验证。

### 内容项目后台直发

1. 默认直发只在用户明确确认当次内容、账号和排期后开启；授权必须绑定这三者，短期有效且只能消费一次。
2. 直发不要求独立平台预检 taskId，但正式会话内必须完成结构化字段写入、平台实体选择、结果回读和提交前门禁。
3. 正常且文案、按钮完全匹配的平台发表说明可按用户已选选项自动继续；二维码、短信验证、风控、法律协议或未知弹窗必须暂停。
4. 验证完成后必须继续同一浏览器会话，不得重建草稿、重填字段或再点一次最终发布。
5. 任务查询必须区分准备、等待验证、提交后核对、成功、明确失败和结果不明；不返回二维码像素、验证码或会话凭据。
6. 最终发布按钮已点击后结果不明，同一发布指纹必须禁止自动重试，先做只读后台核对。

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
6. 用“全国店名 / 省份加店名 / 城市加店名”三组词验证缓存互相隔离；更换搜索词后，手选地点保留，旧自动填入清除，候选不足的视频保持未选择。
7. 分页必须从仍存在的平台候选面板滚动到底部并点击平台“加载更多”；每次点击后记录平台批次、累计候选和是否新增，不能因本地缓存重复就提前宣告全部加载。
8. 地点导致正式失败后，下次搜索必须绕过或重新核验该缓存候选。

### 会话与批次恢复

1. 对音乐刷新、地点搜索、预检、正式发布分别验证：正常完成、失败、用户取消、放弃上传和客户端重启后，页面与临时会话均已关闭。
2. 第一批完成后立即开始第二批，结果不得依赖第一批遗留的下拉框、搜索面板、候选或自动填入状态。
3. 暂停发布后继续时，从明确的视频序号和平台阶段恢复；验证或验证码冷却结束后自动继续，不能重复提交已成功项目。
4. CLI 中断、工作线程异常和客户端退出必须让数据库任务在限定时间内进入明确终态；不得永久 `running/pending`。

## 四、集成和发布

1. 三条功能线可以并行开发，但一次只合并一条到集成线。
2. 每次合并共享核心后重新跑全量测试和离屏 UI；上一条线的结果不能替代本次。
3. 默认三线全部通过后由集成线统一递增版本；如用户明确批准部分发布，必须先在 `SOURCE_OF_TRUTH.md` 列明本次包含与排除的功能线，再递增版本。功能分支始终不修改版本。
4. 打包前核对应用界面、Mac 包、Windows 包名和构建产物版本一致。
5. Mac 真实启动和 Windows 候选包验证分别记录；Mac 通过不能代替 Windows 真机。
6. 每次交付新客户端自动递增版本，并验证应用内版本、包名、构建元数据和状态文件一致；现有包不得被未打包源码冒充为已更新。
7. 远程发布区默认只保留最新候选包；删除旧包前先列出准确对象并确认最新包可下载、哈希可读。
