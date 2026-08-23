# 抖音评论洞察只读合同观察报告（2026-08-23）

## 本次结果

- 状态：`observed_unverified`
- 固定错误码：`comment_content_unavailable`
- 账号选择：使用稳定顺序中的第一个可用 `type=3` 真实登录会话；未记录任何账号标识。
- 官网 JSON 候选响应数：`41`
- 脱敏结构字段名数量：`663`
- 出现的分页候选字段名：`billboard_data.has_more`、`course_list[].offset`、`cursor`、`has_more`、`offset`
- 观察到的官网导航路径数量：`2`
- 作品列表合同：未验证
- 评论列表合同：未验证
- 合同选择：未冻结
- 资源关闭回执：`closed=true`，`aliveResourceCount=0`
- 生产清单：未创建 `app_core/platform_data_douyin_comment_contract.json`

## 结论

本次只打开 `https://creator.douyin.com`，并被动观察官网页面自己发起的 HTTPS JSON 响应。没有重放或合成评论请求，没有读取 DOM 正文，没有记录查询串、请求头、Cookie、响应值、作品信息或评论正文，也没有执行回复、点赞、删除、发布等平台写操作。

观察结果没有同时证明作品列表响应、评论列表响应、导航模板和分页动作属于同一份完整真实合同。因此功能继续失败关闭，下游只能使用测试中注入的已验证合同夹具，不能生成或加载生产合同。
