# 抖音评论洞察本地验收记录

## 当前范围

- 分支：`feature/data-monitoring-v3`
- 功能提交：`f69be4850533f0e4ac69a0b274c64bc153bb4f85`
- 本记录只覆盖 data 分支本地门禁；未进入共享集成、真实平台或发布阶段。

## 本地测试

- `python -m unittest -v test_platform_data_comment_models`：13 项通过。
- `python -m unittest -v test_platform_data_comment_store`：9 项通过。
- `python -m unittest -v test_platform_data_comment_contract`：29 项通过。
- `python -m unittest -v test_douyin_comment_data_collector`：54 项通过。
- `python -m unittest -v test_platform_data_comment_service`：29 项通过。
- `python -m unittest -v test_platform_data_comment_ai`：65 项通过。
- `python -m unittest -v test_douyin_data_collector`：52 项通过。
- `python -m unittest -v test_platform_data_sync`：28 项通过。
- `python -m unittest -v test_data_monitor_page`：106 项通过。
- 上述焦点模块合计：385 项通过，0 项失败。
- `python -m unittest -v test_data_monitor_page test_platform_data_comment_service test_platform_data_comment_ai`：200 项通过，0 项失败。
- `python -m unittest -v test_platform_data_service test_platform_data_sync test_data_monitor_page test_douyin_data_collector test_xiaohongshu_data_collector test_wechat_data_collector test_bilibili_data_collector test_kuaishou_data_collector`：301 项通过，0 项失败。
- `git diff --check`：通过。

## 范围检查

- data 分支范围检查：通过。
- 共识别 27 个变更文件，全部属于 data 分支允许范围；没有 shared 或 outside 文件。

## 尚未执行的真实与集成验收

- 生产评论合同和生产清单均不存在，因此没有执行真实只读同步，也没有产生真实同步批次或平台回读结果。
- 共享数据库建表入口尚未接入；本分支没有修改共享数据库入口，也没有创建数据库集成测试。
- 未执行真实 AI 服务验收，未读写系统凭据，未执行原生凭据环境验收。
- 未执行集成分支要求的全量测试和离屏客户端启动验收。

## 下一处集成阻碍

集成分支需要接入共享数据库建表入口并新增对应集成测试；完成后重新执行集成分支的全量测试和离屏客户端启动验收。生产合同仍不存在时，真实只读同步继续记为不可执行，不能写成已验证。
