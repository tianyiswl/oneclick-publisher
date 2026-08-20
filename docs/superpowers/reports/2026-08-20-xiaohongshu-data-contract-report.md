# 小红书数据只读事实合同报告

- 执行日期：2026-08-20
- 平台类型：`1`
- 终态：`failed`
- 固定码：`xiaohongshu_account_selection_required`

## 观测到的响应合同

本次未观测到可记录的官方 JSON 响应，因此没有可报告的 endpoint path、method、结构 key names/types 或 pagination keys。
按当前 execute 报告语义，实际观测阶段为 `phases=[]`，缺失阶段为
`missingPhases=[account_overview, content_list, content_lifetime]`。这是对已有账号选择门失败事实的字段纠偏，不代表重新执行了真实探测。

| 分类 | 观测状态 |
| --- | --- |
| `account_overview` | 缺失 |
| `content_list` | 缺失 |
| `content_lifetime` | 缺失 |

## 分页能力

未观测到 pagination keys，无法确认完整遍历在技术上可行。

## 资源清理

- `cleanup.closed`: `true`
- `cleanup.aliveResourceCount`: `0`（内置整数）

## 结论

`adapter_contract_incomplete`

缺失 classes：`account_overview`、`content_list`、`content_lifetime`。本报告不包含响应样本 values，不推测 metric meanings。
