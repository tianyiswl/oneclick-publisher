# 一键发

一键发是独立的桌面端多平台内容发布工作台。它复用成熟桌面页面结构，账号授权、素材管理、发布中心和任务记录均在一键发内完成；恢复的蚁小二代码只作为平台能力与字段规则的移植参考，不是运行依赖。

## 当前能力

- 账号管理：在一键发内打开官方平台页面完成登录，并自动保存一键发自己的本地会话。
- 素材管理：按主体分类管理视频与图片素材。
- 发布中心：视频发布、图文发布、文字发布三种入口；按内容类型过滤可用平台和账号。
- 任务记录：显示短任务号、内容类型与预检结果。
- 国内六平台预检：小红书、视频号、抖音、快手、B站、公众号。预检只会上传测试素材或填写字段并回读，不会保存草稿、预览或发布。
- 海外平台：已接入 TikTok、YouTube、Instagram Reels 和 Facebook Reels 的视频账号登录、静默检测与浏览器预检；同时接入恢复源码中的官方 OAuth/API 能力。YouTube、Instagram、Facebook 的正式通道需要二次确认和平台结果回读；TikTok 官方通道当前只上传到收件箱，不等同于公开发布。详见 `docs/OVERSEAS_PLATFORM_STATUS.md`。
- 小红书内置执行器：图文与视频共用一键发账号会话、字段规则和回执判断，不启动蚁小二客户端，也不调用 `yxer`、蚁小二网关或私有签名服务。

账号登录正常和预检成功均不等同于内容已发布。只有一键发对该平台明确开放正式通道、获得用户本次授权，并回读到平台成功回执后，才能标记为已发布或已定时。

## 本地运行

```bash
python -m pip install -r requirements-oneclick.txt
python desktop_native_app.py --page publish
```

离屏界面回归：

```bash
QT_QPA_PLATFORM=offscreen python desktop_native_app.py --ui-test
```

## 数据边界

本仓库不会提交账号资料、登录会话、Cookie、OAuth 令牌、数据库、素材、头像、日志或授权状态。浏览器会话与业务运行数据位于 `demo-runtime/`；海外官方授权配置和令牌位于 macOS 的 `~/Library/Application Support/一键发/`。两类数据均不会进入 Git 或应用安装包。
