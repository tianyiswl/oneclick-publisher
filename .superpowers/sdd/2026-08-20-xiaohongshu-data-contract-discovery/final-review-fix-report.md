# 小红书数据合同发现：Final Review Fix Report

## Status

- Fix status：`READY_FOR_SCOPED_REREVIEW`
- Important：`6/6` 已分别完成命名 RED → 最小完整 GREEN。
- 基线 SHA：`cb6cd1537721c8b43c34631e8dd18d760d88bd07`
- 实现 SHA：`98ca370a9bd084662bb161ae3b8911458ca20ba9`
- 事实结论：仍为 `adapter_contract_incomplete`。
- 本轮没有访问账号、浏览器或网络，没有执行真实探测，没有运行 full suite。
- 两个既有 Minor 继续 defer：Task 3 报告命令占位符、临时文件 `unlink` best-effort。

## I1 — 未知短结构 key 不得原样泄露

命名测试：
`test_unknown_short_structural_keys_are_redacted_at_both_boundaries`

RED：

```text
$ .venv/bin/python -m unittest -v test_xiaohongshu_data_contract_verifier.XiaohongshuDataContractVerifierTests.test_unknown_short_structural_keys_are_redacted_at_both_boundaries
FAIL
实际 keyPaths 仍包含 data.andy、data.items[].mars；期望固定 :key 模板。
Ran 1 test
FAILED (failures=1)
```

GREEN：只保留由受控语义集合组成的显式静态结构 key；所有其他 exact built-in
字符串 key 在采集边界和最终 sanitizer 边界都统一为 `:key`。`note_id/item_id/content_id`
只保留静态字段名，不保留 scalar 值；宽字典与循环旧断言同步校准。

```text
$ .venv/bin/python -m unittest -v ...test_unknown_short_structural_keys_are_redacted_at_both_boundaries ...test_probe_bounds_wide_dict_and_samples_builtin_list
Ran 2 tests
OK
```

## I2 — 未知官方 JSON endpoint 保留为安全未分类信号

命名测试：
`test_unknown_official_json_path_is_redacted_retained_and_unclassified`

RED：

```text
$ .venv/bin/python -m unittest -v test_xiaohongshu_data_contract_verifier.XiaohongshuDataContractVerifierTests.test_unknown_official_json_path_is_redacted_retained_and_unclassified
FAIL
_response_shape() 对 /api/galaxy/creator/analytics 返回 None。
Ran 1 test
FAILED (failures=1)
```

GREEN：未知合法 segment 不再因先验 segment 词表被丢弃，而是固定模板化为
`:segment`；已识别动态 ID 固定为 `:id`，query 永不进入 path。示例被保留为
`/api/:segment/creator/:segment`，但 `_classify_shape()` 固定返回 `unclassified`，
`phases=[]`，不能晋升合同证据。完整审核 path → phase 表默认为只读空映射。

```text
$ .venv/bin/python -m unittest -v ...test_unknown_official_json_path_is_redacted_retained_and_unclassified
Ran 1 test
OK
```

## I3 — 分类必须同时满足审核完整 path 与同祖先结构语义

命名测试：
`test_classifier_requires_reviewed_path_and_common_ancestor_semantics`

RED：

```text
$ .venv/bin/python -m unittest -v test_xiaohongshu_data_contract_verifier.XiaohongshuDataContractVerifierTests.test_classifier_requires_reviewed_path_and_common_ancestor_semantics
profile alone、ID + 无关 items、ID + window.view_count 及其组合被 flatten token 误分类；
redacted identifier placeholder 也可被当作证据。
Ran 1 test
FAILED (failures=5)
```

GREEN：

- path 必须精确命中显式审核的完整 template，含 `:segment` 的发现 path 永不分类；生产映射保持为空。
- account 必须在审核 account path 上同时存在受控 interval/trend scope 与 numeric metric。
- content list 的受控内容 ID 必须位于真实 `items/list/notes/feeds` 列表元素祖先下。
- content lifetime 的受控内容 ID 与 numeric metric 必须共享同一 entity 祖先，并出现受控 `lifetime/cumulative/all_time/total` 语义；`window/interval/range/period/daily` 排除。
- 每条 response 独立分类；不同 response 不能拼 token；`:key/:content_identifier/:identifier` 等 redacted placeholder 不能作为证据。

```text
$ .venv/bin/python -m unittest -v ...test_classifier_requires_reviewed_path_and_common_ancestor_semantics
Ran 1 test
OK
$ .venv/bin/python -m unittest -v <4 个分类相关聚焦测试>
Ran 4 tests
OK
```

## I4 — clean checkout 从已验证 repository root FD 创建报告树

命名测试：
`test_report_writer_creates_missing_sdd_tree_from_repository_root_fd`

RED：

```text
$ .venv/bin/python -m unittest -v test_xiaohongshu_data_contract_verifier.XiaohongshuDataContractVerifierTests.test_report_writer_creates_missing_sdd_tree_from_repository_root_fd
writer 未能在仅有 repository root 的 clean tree 创建 .superpowers/sdd/<plan>。
Ran 1 test
FAILED (failures=1)
```

GREEN：先以 `O_DIRECTORY|O_NOFOLLOW` 打开 repository root，并用 `fstat` 对照
no-follow stat 验证 device/inode；随后只通过 `mkdirat/openat(dir_fd=...)` 逐层创建并打开
`.superpowers/sdd/<plan>`，每层均 `O_NOFOLLOW + fstat(directory)`。临时文件仍在目标目录 FD
内创建并以 directory-FD `os.replace` 原子替换；symlink escape、父目录交换竞态、路径越界
继续固定失败。仅 runtime `probe-report.json` 保持现有精确 ignore。

```text
$ .venv/bin/python -m unittest -v <clean tree、symlink、race、path 四个 writer 聚焦测试>
Ran 4 tests
OK
```

## I5 — 调用 response.json 前完成单响应与累计字节门

命名测试：
`test_response_byte_budgets_reject_before_json_decode`

RED：

```text
$ .venv/bin/python -m unittest -v test_xiaohongshu_data_contract_verifier.XiaohongshuDataContractVerifierTests.test_response_byte_budgets_reject_before_json_decode
超限、缺失 Content-Length、畸形 Content-Length、压缩响应与累计超限仍调用 json()。
Ran 1 test
FAILED (failures=5)
```

GREEN：只信任 exact built-in `dict/str/int` 的官方 HTTPS host、2xx status、GET/POST、
JSON content type 与纯十进制 Content-Length；单响应上限 `1 MiB`，整次 retained batch 上限
`4 MiB`。缺失、畸形、0、超限或非 identity 编码全部在 `json()` 前拒绝；累计超限时清空
整批 retained responses 并停止接收，因此该批次任何 `json()` 都不会发生。监听器保存已验证 metadata，
后续不重新信任可变 header。实现未调用 response body/text。

```text
$ .venv/bin/python -m unittest -v ...test_response_byte_budgets_reject_before_json_decode
Ran 1 test
OK
$ .venv/bin/python -m unittest -v <5 个 body/listener/async 聚焦测试>
Ran 5 tests
OK
```

## I6 — CLI exit code 与终态矩阵一致

命名测试：
`test_main_exit_code_matches_terminal_status_matrix`

RED：

```text
$ .venv/bin/python -m unittest -v test_xiaohongshu_data_contract_verifier.XiaohongshuDataContractVerifierTests.test_main_exit_code_matches_terminal_status_matrix
partial_success 实际退出码 0，期望固定非零。
Ran 1 test
FAILED (failures=1)
```

GREEN：矩阵固定为 `plan=0`、execute `success=0`、execute
`partial_success/failed=1`、arguments `=2`、path/write `=1`；
`KeyboardInterrupt/SystemExit` 原样传播。带 report 的 success/partial/failed 测试同时断言
stdout JSON 与落盘 JSON 完全一致；writer 失败输出固定 failed 终态并返回 1。

```text
$ .venv/bin/python -m unittest -v ...test_main_exit_code_matches_terminal_status_matrix
Ran 1 test
OK
$ .venv/bin/python -m unittest -v <10 个 main/terminal 聚焦测试>
Ran 10 tests
OK
```

## 最终限定验证

```text
$ .venv/bin/python -m unittest test_xiaohongshu_data_contract_verifier -v
Ran 51 tests in 0.086s
OK
exit_code=0

$ .venv/bin/python -m unittest test_douyin_data_collector test_platform_data_sync test_platform_data_service test_data_monitor_page -v
Ran 77 tests in 0.430s
OK
exit_code=0

$ .venv/bin/python -m py_compile tools/verify_xiaohongshu_data_contract.py test_xiaohongshu_data_contract_verifier.py
exit_code=0; output=<empty>

$ rg -n "Cookie|Authorization|Set-Cookie|response\\.text|response\\.body|page\\.evaluate|context\\.request" tools/verify_xiaohongshu_data_contract.py
exit_code=1; output=<empty>; matches=0

$ rg -n -i <credential/query/personal/UUID/phone patterns> probe-report.json factual-report.md
exit_code=1; output=<empty>; matches=0

$ git diff --cached --check cb6cd1537721c8b43c34631e8dd18d760d88bd07 --
exit_code=0; output=<empty>
```

说明：报告敏感扫描首次命令在 shell 引号解析阶段失败，未执行任何扫描；修正后的有效命令仅执行
一次并得到上列零匹配结果。未重复 verifier、V2 或 pycompile。

## 自审：交叉约束与既有硬边界

| 边界 | 自审结果 |
| --- | --- |
| I1 + I2 | 未知官方 JSON shape 保留；未知 path segment 仅 `:segment/:id`，未知结构 key 仅 `:key`，均不泄露原文。 |
| I2 + I3 | `:segment` path 只作发现信号；只有完整审核 path 与审核结构 key 可分类，默认审核表为空。 |
| 默认零动作 | 空 argv 仍只输出固定 plan，不导入账号、数据库、Playwright，不写 report。 |
| 账号门 | exact built-in `int` 的唯一 `type=1/status=1/id>0` 账号门未改。 |
| 被动 only | 仍只监听响应并唯一导航官方 home；无 click/fill/fetch/replay/evaluate/request/upload/publish。 |
| 预算 | navigation `30s`、总预算 `60s`、结构节点/深宽/响应上限保留，并新增 body bytes 门。 |
| cleanup | page → context → browser → Playwright 逆序 best-effort；进程控制异常在全清理尝试后传播。 |
| writer | 从验证 root FD 逐层创建，禁止 symlink/越界；directory-FD 原子替换；失败终态固定非零。 |
| 事实报告 | 既有报告仍是账号选择门的历史事实：responses/phases 为空、三类 missing、`adapter_contract_incomplete`；本轮未声称重跑。 |

## 风险与安全停点

- 生产 `_REVIEWED_CONTRACT_PATH_TEMPLATES` 刻意为空，因此当前任何被动发现响应都不能让合同变为 success；这是保守停点，不是适配器 ready。
- 真实 Playwright 响应若缺少可信 Content-Length、使用压缩编码或超过预算，将保守不采集并保持 unclassified/failed；可能降低发现覆盖率，但不会绕过内存边界。
- 本轮没有新的真实官方 endpoint、字段、分页或账号证据；ignored 的历史 `probe-report.json` 未修改，不能作为本轮运行证据。
- 两个 Minor 按要求未修复；除此之外未发现新的 scoped 阻塞项。
- 工作树原有未跟踪 `.venv` 保留且未提交。

## 闭环与唯一下一步

已达到本轮工程交付闭环：六个 Important 的命名 RED/GREEN、限定离线回归、编译与静态边界检查均完成。
未达到真实运行或平台结果闭环。

唯一下一步：控制器以 `cb6cd1537721c8b43c34631e8dd18d760d88bd07` 到包含本报告的最终
HEAD 做一次 scoped rereview；完成证据是 6/6 Important 均判定 addressed，且不把当前状态升级为
`adapter_contract_ready`。
