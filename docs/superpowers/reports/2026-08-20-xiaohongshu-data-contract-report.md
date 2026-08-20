# 小红书数据只读事实合同报告

- 执行日期：2026-08-20
- 平台类型：`1`
- 探测模式：`execute`（经明确授权的一次真实只读探测命令）
- 终态：`failed`；命令退出码：`1`
- 固定码：`xiaohongshu_account_selection_required`

## 观测到的响应合同

本次命令在账号选择门前停止；未启动浏览器短会话，未发生页面导航或
被动响应观测，因此未捕获可记录的官方 JSON 响应。净化报告中的
`responses` 为空，`phases` 为空，且全部三个必需阶段位于 `missingPhases`。
因此没有可报告的 endpoint template、method、field path、built-in type、metric
scope、pagination 字段或 coverage 语义。此结论不触发重试，也不推测缺失的数据合同。

| 分类 | 观测状态 | 缺失的合同证据 |
| --- | --- | --- |
| `account_overview` | `missing` | 稳定 endpoint template、method/status、field path/type、人工确认的 metric meaning/time scope、无冲突候选 |
| `content_list` | `missing` | 稳定 endpoint template、method/status、field path/type、pagination fields、coverage semantics、无冲突候选 |
| `content_lifetime` | `missing` | 稳定 endpoint template、method/status、field path/type、人工确认的 lifetime metric meaning/time scope、无冲突候选 |

## 分页与覆盖语义

未观测到 pagination fields，故无法确认内容列表的完整遍历能力或覆盖边界。

## 资源清理

- `cleanup.closed`: `true`
- `type(cleanup.aliveResourceCount)`: `int`
- `cleanup.aliveResourceCount`: `0`

清理断言在合同解释前已通过。

## 结论

`adapter_contract_incomplete`

精确缺失项：`account_overview`、`content_list`、`content_lifetime` 的上述合同证据；
本报告不包含账号、内容、指标或响应样本值。
