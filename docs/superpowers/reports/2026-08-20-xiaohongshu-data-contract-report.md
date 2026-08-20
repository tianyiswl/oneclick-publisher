# 小红书数据只读事实合同报告

- 执行日期：2026-08-20
- 平台类型：`1`
- 探测模式：`execute`（经明确授权的一次真实只读探测命令）
- 终态：`failed`；命令退出码：`1`
- 固定码：`xiaohongshu_contracts_unobserved`

## 观测到的响应合同

第二次经明确授权的命令已通过账号选择门，实际进入账号首页和数据分析页。
被动捕获了 2 类官方 JSON 结构：一类仅含通用操作结果字段；另一类含
`data.total` 和一个长度为 0 的列表容器。旧探测器没有将每个响应与当时页面
阶段绑定，因此两类结构均不能安全晋级为数据合同。`phases` 仍为空，全部三个
必需阶段仍位于 `missingPhases`。此结论不触发重试，也不推测指标语义。

| 分类 | 观测状态 | 缺失的合同证据 |
| --- | --- | --- |
| `account_overview` | `missing` | 稳定 endpoint template、method/status、field path/type、人工确认的 metric meaning/time scope、无冲突候选 |
| `content_list` | `missing` | 稳定 endpoint template、method/status、field path/type、pagination fields、coverage semantics、无冲突候选 |
| `content_lifetime` | `missing` | 稳定 endpoint template、method/status、field path/type、人工确认的 lifetime metric meaning/time scope、无冲突候选 |

## 分页与覆盖语义

观测到 `data.total` 类分页字段和空列表，但旧证据缺少页面阶段绑定，
故仍无法确认内容列表的完整遍历能力或覆盖边界。

## 资源清理

- `cleanup.closed`: `true`
- `type(cleanup.aliveResourceCount)`: `int`
- `cleanup.aliveResourceCount`: `0`

清理断言在合同解释前已通过。

## 结论

`adapter_contract_incomplete`

精确缺失项：`account_overview`、`content_list`、`content_lifetime` 的上述合同证据；
本报告不包含账号、内容、指标或响应样本值。
