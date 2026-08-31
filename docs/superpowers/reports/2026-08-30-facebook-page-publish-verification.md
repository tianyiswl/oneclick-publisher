# Facebook Page V1 本地验证与集成交接记录

日期：2026-08-30（Asia/Shanghai）

## 结论

> 本文件保留的是功能分支在 `7baa0d2` 时的历史验证快照，不再代表最终集成结果。最终集成验收以 [Facebook Page V1 本地集成验收记录](./2026-08-30-facebook-page-v1-integration-acceptance.md) 为准；其中代码候选为 `731abbe`、58 个提交、60 个变更文件、全仓 `3034/3034` 通过。

Facebook Page V1 的功能分支代码合同、离线自动测试和离屏客户端构造门禁曾在本快照通过。特性开关在未设置进程环境变量时保持关闭。本记录只证明当时的功能分支源码及离线合同，不证明真实 Facebook Page 登录、真实 Page 身份回读、Meta 平台表单、视频上传、平台预检、正式动作、公开发布、安装包或已安装客户端可用。

本轮没有打开真实 Facebook 浏览器，没有登录 Facebook，没有选择或回读真实 Page，没有上传视频，没有运行真实平台预检，没有触发正式或公开发布。也没有 merge、push、改版本、打包或替换已安装客户端。

## 验证对象

- 分支：feature/facebook-page-publish-v1
- origin/main 与 merge base：36320e4cdc6fd5370ef7fd01c8c260b550cb7341
- 设计提交：8127c78ff9947c826d4d4717f20e312700f605b1
- 批准计划提交：112126e1abf4ec0474cbada42fe1b42d20bdf24b
- 本次验证的最终代码 HEAD（本报告刷新前）：7baa0d2aab4aff0d13d5dab9b30a4ab460e7d5ab
- 最终代码 HEAD 相对 origin/main 的提交数：40；其中设计/计划 2 个、Tasks 1–10 与实现/hardening 37 个、上一轮 Task 11 文档提交 1 个。
- 本报告刷新前，git status --porcelain=v1 --untracked-files=no 无输出，exit 0。

## 离线门禁

| 门禁 | 精确结果 | unittest 用时 | 进程用时 | exit |
| --- | --- | ---: | ---: | ---: |
| 八组 Facebook Page 新测试 | 224/224 通过 | 32.174s | 32.48s | 0 |
| 13 组受影响回归 | 383/383 通过 | 10.834s | 11.59s | 0 |
| 全仓 unittest discover -v | 2952/2952 通过 | 132.176s | 132.92s | 0 |
| 离屏源码客户端 | NATIVE_DESKTOP_UI_OK；仅有一条 Qt Sans Serif 字体别名提示 | — | 0.91s | 0 |
| git diff --check | 无输出 | — | 0.01s | 0 |
| git diff --check origin/main...HEAD | 无输出 | — | 0.04s | 0 |
| 精确敏感词扫描 | 59 行命中，全部分类如下，没有实际密钥值 | — | 0.01s | 0 |
| 设计/计划占位词扫描 | 0 命中；rg 的“无匹配”返回码 | — | 0.00s | 1 |
| 海外工作线范围检查 | 最终代码 HEAD：55 路径（11 owned / 17 shared / 27 outside）；刷新既有报告后路径计数不变；REVIEW_REQUIRED | — | 0.05s | 2 |
| 默认关闭特性开关探针 | ENV_PRESENT=False；FACEBOOK_PAGE_V1_ENABLED=False | — | 0.04s | 0 |

### Gate 1：全部新增 Page 测试

~~~bash
../../.venv/bin/python -m unittest -v \
  test_facebook_page_identity \
  test_facebook_page_content \
  test_facebook_page_database \
  test_facebook_page_login \
  test_facebook_page_controlled_publish \
  test_facebook_page_publish \
  test_facebook_page_publish_service \
  test_facebook_page_task_service
~~~

结果：Ran 224 tests in 32.174s；OK；real 32.48s；exit 0。

### Gate 2：受影响回归

~~~bash
../../.venv/bin/python -m unittest -v \
  test_meta_browser_publish \
  test_overseas_publish_routing \
  test_overseas_video_publish \
  test_overseas_integration \
  test_account_detection_ui \
  test_controlled_publish \
  test_controlled_publish_resilience \
  test_publish_service \
  test_task_service \
  test_publish_page \
  test_controlled_publish_process \
  test_content_project_gateway \
  test_oneclick_mcp_server
~~~

结果：Ran 383 tests in 10.834s；OK；real 11.59s；exit 0。

### Gate 3：全仓离线测试

~~~bash
../../.venv/bin/python -m unittest discover -v
~~~

结果：Ran 2952 tests in 132.176s；OK；real 132.92s；exit 0。测试输出中的登录状态文案来自离线测试夹具，不是本轮真实平台登录或回读。

### Gate 4：离屏客户端

~~~bash
QT_QPA_PLATFORM=offscreen ../../.venv/bin/python desktop_native_app.py --ui-test
~~~

结果：NATIVE_DESKTOP_UI_OK；real 0.91s；exit 0。唯一额外输出是 Qt 为缺失的 Sans Serif 字体建立别名的 57ms 提示。该路径只构造离屏主窗口，没有启动真实浏览器、登录或发布。

### Gate 5：静态检查

~~~bash
git diff --check
git diff --check origin/main...HEAD
rg -n -i \
  'cookie\s*[=:]|access[_ -]?token\s*[=:]|password\s*[=:]|verificationCode|qr(Code|Data)|storageState' \
  app_core uploader ui desktop_native_app.py test_facebook_page_*.py
rg -n 'TO[D]O|TB[D]|PLACEHOL[D]ER|待[定]|待[补]' \
  docs/superpowers/specs/2026-08-29-facebook-page-publish-design.md \
  docs/superpowers/plans/2026-08-30-facebook-page-controlled-reel-publishing.md
~~~

两项 diff 检查均无输出、exit 0。占位词扫描为 0 命中，rg exit 1 表示“没有匹配”，不是失败。

敏感词扫描共 59 行。逐项分类如下：

| 文件与命中行 | 数量 | 分类 |
| --- | ---: | --- |
| app_core/controlled_publish.py:1801 | 1 | 本分支新增的禁止字段名 verificationCode；用于拒绝持久化敏感输入。 |
| test_facebook_page_identity.py:313,315 | 2 | 本分支新增的合成 qrCode/storageState 输入；测试安全投影必须丢弃。 |
| test_facebook_page_task_service.py:366,540,541,543,555,562,563,577,592,711,912 | 11 | 本分支新增的合成验证码、Cookie、token、路径及否定断言；只用于证明消息、回执和事件会脱敏。 |
| app_core/overseas_youtube_credentials.py:24 | 1 | 既有系统凭据库协议参数名；没有凭据字面值。 |
| app_core/overseas_youtube_login.py:46,342,356 | 3 | 既有短期运行时 token 字段、refresh_pending 哨兵和运行时赋值；没有真实 token。 |
| app_core/overseas_youtube_oauth.py:59,519,541 | 3 | 既有 OAuth 运行时字段/解析；repr 已关闭，没有真实 token。 |
| app_core/overseas_youtube_api.py:172,266,268,332,367,412,786,787 | 8 | 既有运行时参数、校验和赋值；没有真实 token。 |
| app_core/overseas_youtube_publish.py:110,114,119,342,365,399,606 | 7 | 既有运行时接口参数与短期会话传递；没有真实 token。 |
| app_core/overseas_tiktok_publish.py:729,740,753 | 3 | 既有 storage cookie 结构校验字段名；没有 Cookie 值。 |
| uploader/bilibili_uploader/main.py:68 | 1 | 既有运行时 access_token 变量读取；没有 token 字面值。 |
| uploader/xhs_uploader/xhs_login_qrcode.py:3,11,12,19,23,27,35,36,38 | 9 | 既有二维码登录辅助器的模块、函数和运行时状态标识；Git 中没有二维码或 Cookie 值。 |
| desktop_native_app.py:745,751 | 2 | 既有本机微信验证演示二维码生成器及固定 demo 文本，不是账号凭据。 |
| uploader/douyin_uploader/main.py:805 | 1 | 既有 QRCode 枚举名。 |
| ui/douyin_verification_dialog.py:113 | 1 | 既有输入控件 objectName，不是验证码值。 |
| app_core/oneclick_authorization.py:456,616 | 2 | 既有登录二维码 CSS 选择器。 |
| app_core/wechat_draft_executor.py:127；app_core/wechat_publish_executor.py:143 | 2 | 既有二维码节点识别正则。 |
| app_core/account_service.py:1362 | 1 | 既有头像候选排除二维码/图标的正则。 |
| app_core/task_service.py:283 | 1 | 既有 cookie= 脱敏触发词。 |

合计：本分支新增 14 行，全部是拒绝名单或合成脱敏测试；origin/main 既有 45 行，全部是凭据处理的类型/变量、二维码识别或脱敏代码。扫描未发现写入 Git 的密码、Cookie、访问令牌、验证码、二维码数据或 storage state。

### Gate 6：工作线范围

~~~bash
../../.venv/bin/python tools/check_workstream_scope.py --stream overseas --base origin/main
~~~

在最终代码 HEAD 7baa0d2 上的结果：changed=55；11 owned / 17 shared / 27 outside；scope-check: REVIEW_REQUIRED；real 0.05s；exit 2。当前 55 路径已经包含 brief 唯一允许的验证报告和 SOURCE_OF_TRUTH 交接片段；本轮只刷新这两个既有路径，因此文档提交后 changed 和三类计数均保持不变。exit 2 是批准设计中共享核心和接口文件需要转交集成线的预期结果，不是绿色范围通过。

55 个路径已全部核对：

- owned（11）：app_core/overseas_browser_publish.py、app_core/overseas_meta_content.py、app_core/overseas_meta_errors.py、app_core/overseas_meta_page_identity.py、app_core/overseas_preflight.py、test_meta_browser_publish.py、test_overseas_integration.py、test_overseas_publish_routing.py、uploader/meta_uploader/content_list.py、uploader/meta_uploader/main.py、uploader/meta_uploader/page_form.py。
- shared（17）：app_core/account_browser_service.py、app_core/account_service.py、app_core/controlled_publish.py、app_core/database.py、app_core/login_service.py、app_core/oneclick_authorization.py、app_core/publish_service.py、app_core/task_service.py、desktop_native_app.py、myUtils/login.py、test_account_detection_ui.py、test_controlled_publish.py、test_publish_service.py、test_task_service.py、ui/account_page.py、ui/login_dialog.py、ui/publish_page.py。
- outside（25）：app_core/content_project_gateway.py、app_core/controlled_publish_process.py、app_core/oneclick_mcp_server.py、docs/architecture/facebook-page-publish.architecture.html、docs/architecture/facebook-page-publish.architecture.json、docs/architecture/facebook-page-publish.lifecycle.html、docs/architecture/facebook-page-publish.lifecycle.json、docs/superpowers/plans/2026-08-30-facebook-page-controlled-reel-publishing.md、docs/superpowers/specs/2026-08-29-facebook-page-publish-design.md、myUtils/auth.py、test_content_project_gateway.py、test_controlled_publish_process.py、test_controlled_publish_resilience.py、test_facebook_page_content.py、test_facebook_page_controlled_publish.py、test_facebook_page_database.py、test_facebook_page_identity.py、test_facebook_page_login.py、test_facebook_page_publish.py、test_facebook_page_publish_service.py、test_facebook_page_task_service.py、test_oneclick_authorization.py、test_oneclick_mcp_server.py、test_publish_page.py、utils/publish_observer.py。

outside 路径均有批准来源：6 个设计/架构/计划证据；Task 3 的 myUtils/auth.py 与 test_oneclick_authorization.py；Tasks 1–9 的 8 个 Facebook Page 测试和受控恢复测试；Task 8 的 publish observer；Task 10 的 Gateway、进程、MCP 及其接口测试。没有无关路径。

其中两条 Task 11 报告路径为：

- docs/superpowers/reports/2026-08-30-facebook-page-publish-verification.md
- docs/superpowers/reports/2026-08-30-facebook-page-publish-source-of-truth-handoff.md

两者均由 Task 11 brief 明确要求；本轮刷新没有增加第三个产品、测试、状态、版本或打包路径。

### Gate 7：特性开关默认关闭

使用未设置 ONECLICK_ENABLE_FACEBOOK_PAGE_V1 的独立子进程直接调用 facebook_page_v1_enabled()，结果为：

~~~bash
env -u ONECLICK_ENABLE_FACEBOOK_PAGE_V1 ../../.venv/bin/python -c 'import os; from app_core.overseas_meta_page_identity import facebook_page_v1_enabled; present = "ONECLICK_ENABLE_FACEBOOK_PAGE_V1" in os.environ; enabled = facebook_page_v1_enabled(); print("ENV_PRESENT=%s" % present); print("FACEBOOK_PAGE_V1_ENABLED=%s" % enabled); raise SystemExit(0 if (not present and enabled is False) else 1)'
~~~

~~~text
ENV_PRESENT=False
FACEBOOK_PAGE_V1_ENABLED=False
~~~

exit 0，real 0.04s。探针没有写环境配置、数据库或其他持久设置。

## Tasks 1–10 与最终 hardening 的完整依赖顺序

标记 S 表示提交触及 scope checker 的 shared 文件；O 表示只触及海外专属或计划明确的 outside 文件。下面 37 个实现、测试与 hardening 提交是依赖顺序。集成线不能只摘取 S 子集，因为后续共享修复依赖其间的海外合同、执行器和测试。分支历史中的上一轮 Task 11 文档提交 5ff02afaee64d1558191e45d00d4c1383a41c92b 位于第 30 与 31 项之间，只创建报告，不是生产依赖；其旧测试数字由本次刷新取代。

| 顺序 | Task | 类别 | commit | subject |
| ---: | --- | --- | --- | --- |
| 1 | 1 | O | e38ab776e37da8ba03b37148e704c428d51a4a07 | feat(overseas): define Facebook Page identity contract |
| 2 | 1 | O | fbaf70dd6598747d638c4c1a389af0a767ba174a | fix(overseas): verify active Facebook Page binding |
| 3 | 2 | S | f1d439bfe783d89a94b6d4400163449f0bcdf14e | feat(accounts): bind Facebook rows to unique Page IDs |
| 4 | 2 | S | 8fe14370441e52ce3207fc58e99fd57d5deb7e4b | fix(accounts): normalize Facebook Page IDs consistently |
| 5 | 3 | S | 695f108c52a62abfc8533102a2d75de06b786f16 | feat(accounts): add Facebook Page-only login flow |
| 6 | 3 | S | ffd3b2108f33e7ef6488976b471e865d4be4cb0c | fix(accounts): harden Facebook Page login failures |
| 7 | 4 | S | 7d68b7a836e7c8dec129bfecc83c4f8fa5456fbf | feat(publish): bind Facebook requests to Page intent |
| 8 | 4 | O | 449dcb1bcd083926bcf8663630c3a6e635d0740a | test(publish): make unreadable Page video portable |
| 9 | 5 | S | 58a1083cd5f68e2f750ffe1b7e086529d297c18c | feat(publish): reserve Facebook Page formal claims atomically |
| 10 | 5 | S | b33cabc1d1f9022798d8df297db8c6e255873ab0 | fix(publish): require trusted Facebook Page recovery evidence |
| 11 | 6 | O | 9f37fd6587caacad18fbb894c8f6fc1c792a11ad | feat(overseas): verify Facebook Page Reel form |
| 12 | 6 | O | 0e4d8c8beb0c538f5aafb4316cc4319a08748b71 | fix(overseas): harden Facebook Page Reel form |
| 13 | 7 | O | fff4a7389eb1935a6935983547f137b8963cf554 | feat(overseas): read back unique Facebook Page Reels |
| 14 | 7 | O | 2c3f1062fc4c4c89c27162b1b8febcbe2960c6e1 | fix(overseas): seal Facebook Page readback evidence |
| 15 | 7 | O | d417c1c3920621cc1fd4517dbd075fa62ae45e02 | fix(overseas): require complete load-more prefix |
| 16 | 8 | O | 8bf69b5acdcc8ea1eba81cb5c1cac1c848ff73ce | feat(overseas): run controlled Facebook Page workers |
| 17 | 8 | S | 7ead58a2b99453e6d662b9e21ddf763bf60a014a | feat(publish): enforce Facebook Page worker claims |
| 18 | 8 | S | af62be51a544389f3184f12f133947ac21365456 | fix(publish): close Facebook Page worker consistency gaps |
| 19 | 8 | S | bfd3417779f3e004dd7c36d5390215a11f8cbfb3 | fix(publish): preserve concurrent Facebook worker leases |
| 20 | 9 | S | 8afb3550d6c660936cf25581dd1c34f4c1b87029 | feat(tasks): persist Facebook Page publish outcomes |
| 21 | 9 | S | 43cb3edf825ad90bc330a36a99ee773d9b851647 | fix(tasks): harden Facebook Page outcome recovery |
| 22 | 9 | S | 50ee3db7af06c9b82d4c4c5f78d2bc532bb6785b | fix(publish): persist Facebook Page decisions atomically |
| 23 | 9 | O | f6c8077011f042329baafbdbb4100afd71f37e40 | fix(publish): keep Facebook lifecycle events atomic |
| 24 | 9 | O | 39a82766588863c0ce7f20c41cd4d01c03ac63b1 | test(publish): exercise real Facebook formal authorization |
| 25 | 10 | S | 34f699a83b91c9593597b5c75421fa13f4d401bd | feat(publish): expose one controlled Facebook Page service |
| 26 | 10 | S | c3f8a4a298fbbf33e48402e105aa303020490999 | fix(publish): enforce one Facebook Page formal entry |
| 27 | hardening A | S | 246101575d67a642ec6390b1fb1e182bc687bef4 | fix(facebook): bind authorization to canonical Page form evidence |
| 28 | hardening A | S | 732f7adc1e87de7e63db3263d2b977c29484fd5a | test(facebook): use verified Page form receipts |
| 29 | hardening B | S | 669c654715ce701e4ea46668a23105865e36ba21 | fix(accounts): harden Facebook Page activation and backend launch |
| 30 | hardening B | S | e1551d10314e4529475eb731a1150c737630c70a | fix(accounts): stop late Facebook backend workers safely |
| 31 | final broad fix B | O | 6f238f23672a8b202ca7fccd5233cd3cc8b0e52f | fix(overseas): preserve Facebook Page video paths |
| 32 | final broad fix A | S | 5f065669f73ac469e76065d24cbc71f2e38eec6f | fix(publish): close Facebook Page cross-entry gaps |
| 33 | drift fixture | S（仅测试） | 4f5e234c95dddab17f4d055de966d911830cb577 | test(tiktok): freeze scheduled claim fixtures |
| 34 | final broad fix A2 | S | 89dd690a7f27f696471cbfcac53b465d9c960205 | fix(facebook): reject malformed Page platform types |
| 35 | final broad fix B2 | S | 922c0b16a80e573ff9184b179d0188bee480235a | fix(facebook): keep Page video paths ephemeral |
| 36 | final broad fix B2 review | S | 651e2d4ee43e1fe3c4be3bba01974b6da68dc5a4 | fix(facebook): bind runtime media to Page snapshot |
| 37 | final broad fix B2 review | S | 7baa0d2aab4aff0d13d5dab9b30a4ab460e7d5ab | fix(facebook): reject aliased Page runtime media |

第 33 项 4f5e234 只修改 test_controlled_publish.py，把两个固定日期的 TikTok 排期 claim 夹具冻结到确定的上海时间；它修复全仓测试的时间漂移，没有任何生产代码改动，也不改变 Facebook Page 能力边界。scope checker 仍因该测试文件属于 shared 而将其标为 S。

25 个 shared-touch 提交的相对顺序是：

~~~text
f1d439b -> 8fe1437 -> 695f108 -> ffd3b21 -> 7d68b7a ->
58a1083 -> b33cabc -> 7ead58a -> af62be5 -> bfd3417 ->
8afb355 -> 43cb3ed -> 50ee3db -> 34f699a -> c3f8a4a ->
2461015 -> 732f7ad -> 669c654 -> e1551d1 -> 5f06566 ->
4f5e234 -> 89dd690 -> 922c0b1 -> 651e2d4 -> 7baa0d2
~~~

这只是共享审查索引，不是可单独 cherry-pick 的精简列表；实际集成应按上表 37 个提交的完整依赖顺序处理，并同时审查历史中的 Task 11 文档提交及本次最终刷新提交。

## 本地已验证的能力边界

本轮只确认以下本地事实：

- 稳定 Page ID 选择、权限与同一 Page 核对合同，以及默认关闭的进程级特性开关；
- 旧 type 9 记录的非破坏迁移、Page ID 唯一性与可发布账号过滤；
- 单 Page、单视频、立即公开的输入拒绝门、最终 caption、内容/重放指纹；
- 精确整数平台类型 9、跨桌面/CLI/Gateway/MCP 入口的一致元数据拒绝门，以及等价内容的跨入口防重合同；字符串、浮点、布尔或别名类型不会进入 Page 会话；
- 预检回执哈希、一次性授权、正式任务和 Page claim 的原子合同；
- Page 表单与内容列表的可注入离线适配器、完整基线、唯一新 Reel 匹配合同；
- 视频绝对路径只在运行时从冻结 manifest 或受管 VIDEO_DIR 重新校验后传给上传边界；数据库和公共投影只保留 basename/hash/size，缺失、移动、变更或文件系统别名会在 Page 会话、claim 或授权消费前安全停止；
- worker 租约、最终动作单击边界、结果不明、防重放、终态恢复和只读核对合同；
- 桌面 UI、CLI、Gateway 和 MCP 的同一服务接线及安全任务投影；
- 上述八组 Page 测试 224/224、13 组受影响测试 383/383、全仓测试 2952/2952 与离屏窗口构造均通过。

下列层级仍未验证：

- 真实 Facebook 登录；
- 真实 Page ID、名称、头像和权限的重启后回读；
- Meta Business Suite 当前页面的 Page、Reel、视频、完整文案、公开范围和最终按钮回读；
- 真实视频上传或可能留下临时草稿的平台预检；
- 正式最终动作、平台受理、公开发布、Reel ID/URL/发布时间回读；
- 开发验证包、正式安装包或已安装客户端包含并可用。

## 唯一下一步

接下来由集成线按上述完整依赖顺序审查/cherry-pick 37 个实现、测试与 hardening 提交，并审查两次 Task 11 文档提交；在集成线重新运行共享全量测试、离屏 UI、diff/敏感信息/范围门禁并确认无回归后，才可准备默认仍关闭的开发者专用真实平台验证构建。任何 Facebook 登录、平台预检、上传或公开发布都仍需到 Task 12 对应层级重新取得明确授权。

## 2026-08-30 0.5.25 安装后真实预检

- 大帅要求继续真实测试后，使用已安装 `/Users/andy/文件/一键发/一键发.app` `0.5.25`，仅在本次 CLI 进程通过 `ONECLICK_ENABLE_FACEBOOK_PAGE_V1=1` 受控开启功能。
- 新测试内容包为 `facebook-page-public-reel-verification-20260830-v2`；复用既有无害测试视频字节，但改用新标题、正文和发布指纹，没有重放任务 `89`。
- 第一次请求因缺少内容包必填封面在本地返回 `controlled_manifest_invalid`，未创建任务、未打开浏览器、未上传；补入 3:4 封面后才执行一次平台预检。
- 预检任务 `90 / T08302217-F23E` 成功，阶段为 `platform_form_verified`。同次回执确认账号 `14`、Page ID 尾号 `3807`、视频 `facebook-page-v2.mp4`、视频大小 `1796730`、视频 SHA256 `1a9313387ce2344ad22d77b70f8991293d8625486aae05257d6b69a68b2e7541`、文案 SHA256 `4d16982ed5755f892fd2236858d28eb031de6135bbb1df7e7409724910149c4d`、公开范围和唯一可用最终按钮。
- 回执明确 `platformWriteOccurred=true`、`finalActionTriggered=false`；没有 Reel ID、URL 或发布时间，未执行公开发布。下一层必须展示精确 Page、视频、最终文案与公开范围，并重新取得一次正式发布授权。

## 2026-08-30 0.5.25 正式任务 91 与只读收口

- 大帅在查看任务 `90` 的 Page、视频、最终文案和公开范围后，明确授权公开发布这一条测试 Reel。正式任务 `91 / T08302223-B1A1` 消费一次性授权，并在 `22:24:02` 记录一次 `facebook_final_action_clicked`；没有第二次点击或重放。
- 平台未返回 Reel ID、URL 或发布时间。Meta Business Suite“已发布、定时、草稿”、精确文案搜索和通知均未找到目标；公开 Page Reels 直达链接返回“页面无法显示”。任务只能保持 `facebook_publish_outcome_unknown / ambiguous`，`blocksReplay=1`。
- 当前源码修复表头先于数据行导致的提前判空、短暂“暂无内容”覆盖延迟数据行、相对时间采样漂移、平台分钟级时间导致的同分钟误判、matcher 与 claim 二次校验不一致，以及未知结果核对后的 worker token 残留。修复后再次只读核对任务 `91`，仍未唯一命中 Reel；worker token、PID 和心跳已清空，防重锁未释放。
- 内容列表整类 `39/39`、Facebook Page 八模块 `318/318`、完整仓库 `3068/3068` 通过；离屏客户端返回 `NATIVE_DESKTOP_UI_OK`，涉及文件编译检查和 `git diff --check` 也通过。
- 当前不得宣称已发布或确定未发布，也不得重放任务 `91`。源码修复已进入 `0.5.26` Mac 包，并已在备份 `0.5.25` 后替换安装和启动；安装不改变任务 `91` 的结果不明与防重边界。

## 2026-08-30 0.5.26 全新内容预检任务 92

- 大帅明确要求“全新内容预检”后，新建内容包 `runtime/facebook-page-public-reel-verification-20260830-v3`。测试包复用既有无害视频字节，但使用新的项目编号、文件名、标题、正文和标签；新发布指纹 `a4002f18836748aaf1132e07fc4b217142838aa4a8cf51f918be2e489c7242ea` 与新重放指纹 `5f5a1de76e8061e50dd5b52b18c99912845c72e3c7323e5288ca7a65f06ed0b6` 均未命中任务 `89/91` 的防重锁。
- 执行前确认账号 `14` 为正常 Facebook Page 主体“墨白”，没有活动发布任务、未消费预检授权或未消费直发授权；旧任务 `89/91` 继续保持 `ambiguous / blocksReplay=1`。
- 安装版 `0.5.26` 预检任务 `92 / T08302346-D164` 于 `23:46:32` 创建，`23:47:19` 完成，状态为 `success / platform_form_verified`。回执确认 Page 尾号 `3807`、视频 `facebook-page-v3.mp4`、视频 SHA256 `1a9313387ce2344ad22d77b70f8991293d8625486aae05257d6b69a68b2e7541`、文案 SHA256 `27483e77bdb0fb0f2ab6dc333a1798ff25087aec8c8847e3fcaa024a52275e08`、公开范围和唯一可用最终按钮。
- 回执明确 `platformWriteOccurred=true`、`finalActionTriggered=false`；没有 Reel ID、URL 或发布时间。数据库回读任务 `92` 没有最终动作事件、没有正式 claim，也没有生成一次性授权。
- 任务结束后受控 CLI 和内置浏览器均已关闭，活动任务和有效授权均为 `0`。本节只证明平台表单预检成功，不代表已发布。

## 2026-08-30/31 正式任务 93 与只读收口

- 大帅在看到任务 `92` 的精确 Page、视频、文案和公开范围后，明确确认公开发布这一条测试 Reel。系统只创建并消费一次绑定任务 `92` 的短期授权；正式任务为 `93 / T08302355-84A9`。
- 任务 `93` 在 `23:55:53` 记录一次 `facebook_final_action_clicked`，点击总数严格为 `1`。平台随后没有返回 Reel ID、URL 或发布时间，于 `23:56:47` 进入 `facebook_publish_outcome_unknown / ambiguous`；claim 为 `blocksReplay=1`。
- 只执行一次受控 `reconcile`，仍返回无唯一命中。随后在已登录 Chrome 中把 Meta Business Suite 已发布列表日期范围扩大到太平洋时间 `2026-08-30`，按唯一编号 `FB0526-P3` 精确搜索仍无结果；Page Reels 直达页返回“页面无法显示”。
- 当前只能确认“最终动作触发一次，但平台结果不明”。不能宣称已发布，也不能宣称确定未发布；不得再次提交同一内容。脱敏回执保存在 `runtime/facebook-page-public-reel-verification-20260830-v3/formal-result.json`。
