# Task 4 report — 小红书受限只读短会话采集器

## 完成内容

- 新增 `XiaohongshuDataCollector`。直连入口固定返回允许浏览器兜底的 `direct_request_rejected`，不打开浏览器、不复制会话，也不发 HTTP 请求。
- 浏览器入口只从 `COOKIE_DIR / Path(filePath).name` 读取状态文件；缺失、链接或非普通文件固定返回 `session_state_missing`。每次采集创建一套短生命周期 browser/context/page，只导航创作首页、数据分析页和一条已从列表确认的作品详情页。
- 只接收 `creator.xiaohongshu.com` 的 HTTPS JSON 响应，且路径必须是四条已审核路径之一。响应以同一个 request 对象绑定当时页面阶段；迟到的首页请求不能被当前作品详情页误用。
- 单次会话有 60 秒总时限、5 秒清理预留、单响应 1 MiB、累计 4 MiB、100 条响应和 400 条请求上限。响应体在页面仍存活时按字节上限读取，原始 body 只保留在函数局部内存，不写日志或数据库。
- `finally` 独立关闭 page、context、browser、playwright；关闭失败统一覆盖正常或普通失败结果为 `browser_cleanup_incomplete`。`KeyboardInterrupt` / `SystemExit` 在全部关闭尝试后才继续抛出。
- 完成后才在采集器登记表加入平台 `1` 的惰性工厂；`registered_platform_types()` 现在为 `(1, 3)`，`collector_for_platform(1)` 可实例化真实采集器。

## TDD 与验证

先新增生命周期测试，指定 RED 命令因 `app_core.xiaohongshu_data_collector` 不存在而报 `ModuleNotFoundError`。实现后以下测试全部通过，所有浏览器对象均为 fake：

```text
../../.venv/bin/python -m unittest -v test_xiaohongshu_data_collector
Ran 22 tests
OK
```

```text
QT_QPA_PLATFORM=offscreen ../../.venv/bin/python -m unittest -v \
  test_xiaohongshu_data_collector test_douyin_data_collector \
  test_platform_data_service test_platform_data_sync test_data_monitor_page
Ran 107 tests
OK
```

`py_compile`（新采集器、合同、注册表和测试）及 `git diff --check` 均通过。

## 审查关注

- 本任务只完成本地 fake 会话验证，未打开真实小红书浏览器、未访问账号或网络，因此不构成真实同步、平台数据回读或业务结果证明。
- 本次没有改动同步编排层；Task 5 才负责让平台中性编排调用新增采集器并把批次写入数据库。
- 宿主对完整 `unittest discover` 的长命令会脱离当前命令回执，无法取得可靠退出码；未将其计为全量测试通过。
