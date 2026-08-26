# Content Project Metrics Interface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让五个内容项目通过一键发本机 MCP/CLI 统一同步并读取各自发布作品的数据，避免重复登录、重复采集和跨项目数据混用。

**Architecture:** 受控发布请求携带经过白名单校验的 `projectId`，任务服务在创建发布任务的同一 SQLite 事务里登记项目归属。新的项目指标服务按账号执行北京时间当日复用、30 分钟失败冷却和进程内串行锁，并复用现有平台采集器与指标查询服务；MCP 和 CLI 只做薄适配。

**Tech Stack:** Python 3、SQLite、stdlib `threading`/`zoneinfo`、现有 MCP stdio 服务、`unittest`

**Spec:** `docs/superpowers/specs/2026-08-26-oneclick-content-project-metrics-interface-design.md`

## Global Constraints

- 不执行真实平台同步、预检或正式发布；本轮验证全部使用临时数据库和替身采集函数。
- 不接受或返回 Cookie、密码、验证码、会话路径、平台原始响应和数据库路径。
- 账号级数据必须标记 `scope=account`；作品级数据只能来自当前项目正式任务关联的作品。
- 缺失指标保持 `null`/`missing`，不能转换为 0。
- MCP 与 CLI 必须调用同一个 `ContentProjectGateway`，不能复制业务逻辑。
- 不增加视频号采集器，不自动同步评论正文，不升级版本，不打包。

---

### Task 1: 项目任务归属随发布任务原子保存

**Files:**
- Modify: `app_core/database.py`
- Modify: `app_core/controlled_publish.py`
- Modify: `app_core/task_service.py`
- Test: `test_controlled_publish.py`
- Test: `test_task_service.py`

**Interfaces:**
- Consumes: 受控请求顶层 `projectId: str`。
- Produces: `content_project_task_links(projectId, taskId, phase, createdAt)`；发布 payload 内部字段 `contentProjectId`；`task_service.project_task_link(task_id) -> dict | None`。

- [ ] **Step 1: 写请求校验与数据库归属的失败测试**

```python
def test_controlled_payload_carries_valid_project_id(self):
    payload = controlled_publish.build_controlled_payloads(
        {"projectId": "silicon-exploration", "manifestPath": str(self.manifest),
         "mode": "preflight", "targets": [self.douyin_target]},
        accounts=self.accounts,
    )[0]
    self.assertEqual(payload["contentProjectId"], "silicon-exploration")

def test_task_creation_atomically_records_project_phase(self):
    task = task_service.create_pending_task(
        [{**self.payload, "contentProjectId": "silicon-exploration"}],
        mode="oneclick_publish",
    )
    self.assertEqual(
        task_service.project_task_link(task["id"]),
        {"projectId": "silicon-exploration", "taskId": task["id"], "phase": "formal"},
    )
```

- [ ] **Step 2: 运行测试并确认因字段/表/函数不存在而失败**

Run: `python3 -m unittest test_controlled_publish.ControlledPublishTests.test_controlled_payload_carries_valid_project_id test_task_service.TaskServiceTests.test_task_creation_atomically_records_project_phase -v`

Expected: FAIL，分别指出 `contentProjectId` 或 `project_task_link` 尚不存在。

- [ ] **Step 3: 增加最小数据库和任务写入实现**

```python
_PROJECT_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{1,63}")

def _content_project_identity(payloads: list[dict], mode: str) -> tuple[str, str] | None:
    values = {str(item.get("contentProjectId") or "") for item in payloads}
    if values == {""}:
        return None
    if len(values) != 1 or not _PROJECT_ID_RE.fullmatch(next(iter(values))):
        raise ValueError("内容项目任务归属无效")
    phase = "formal" if mode == "oneclick_publish" else "preflight"
    return next(iter(values)), phase
```

在 `_insert_pending_task` 获得 `task_id` 后、事务提交前插入归属表；`database.ensure_schema()` 创建表、唯一键和 `ON DELETE CASCADE` 外键。`controlled_publish.build_controlled_payloads()` 只接受合法 `projectId` 并写入 `contentProjectId`。

- [ ] **Step 4: 运行 Task 1 测试并确认通过**

Run: `python3 -m unittest test_controlled_publish test_task_service -v`

Expected: PASS。

- [ ] **Step 5: 提交 Task 1**

```bash
git add app_core/database.py app_core/controlled_publish.py app_core/task_service.py test_controlled_publish.py test_task_service.py
git commit -m "feat: persist content project task ownership"
```

### Task 2: 账号同步复用、冷却和状态查询

**Files:**
- Create: `app_core/content_project_metrics.py`
- Create: `test_content_project_metrics.py`

**Interfaces:**
- Consumes: `profile: Mapping[str, Any]`、`sync_account: Callable[[int], dict]`、现有同步运行表。
- Produces: `ContentProjectMetricsService.sync_project(project_id, profile) -> dict`；`ContentProjectMetricsService.sync_status(project_id, profile) -> dict`。

- [ ] **Step 1: 写同日复用、失败冷却和逐账号继续的失败测试**

```python
def test_two_projects_reuse_same_account_success_on_same_beijing_day(self):
    first = self.service.sync_project("project-a", self.profile)
    second = self.service.sync_project("project-b", self.profile)
    self.assertEqual(first["accounts"][0]["action"], "synced")
    self.assertEqual(second["accounts"][0]["action"], "reused")
    self.assertEqual(self.calls, [31])

def test_recent_failure_returns_cooldown_without_calling_collector(self):
    self.insert_run(status="failed", finished_at=self.now.isoformat())
    result = self.service.sync_project("project-a", self.profile)
    self.assertEqual(result["accounts"][0]["action"], "cooldown")
    self.assertEqual(self.calls, [])

def test_one_account_failure_does_not_skip_next_account(self):
    result = self.service.sync_project("project-a", self.two_account_profile)
    self.assertEqual([row["status"] for row in result["accounts"]], ["failed", "success"])
```

- [ ] **Step 2: 运行测试并确认服务尚不存在**

Run: `python3 -m unittest test_content_project_metrics -v`

Expected: FAIL，导入 `app_core.content_project_metrics` 失败。

- [ ] **Step 3: 实现最小同步策略服务**

```python
class ContentProjectMetricsService:
    def __init__(self, *, connect_factory=database.connect,
                 sync_account=platform_data_sync.sync_account_data,
                 now=lambda: datetime.now(_BEIJING)):
        self.connect_factory = connect_factory
        self.sync_account = sync_account
        self.now = now

    def sync_project(self, project_id: str, profile: Mapping[str, Any]) -> dict:
        rows = []
        for target in profile["targets"]:
            rows.append(self._sync_target(int(target["accountId"]), str(target["platform"])))
        return {"projectId": project_id, "accounts": rows}
```

使用模块级 `dict[int, threading.Lock]` 保护同一账号；加锁后重新读取最新运行，避免两个项目同时越过第一次检查。北京时间当日 `success/partial_success` 返回 `reused`；最近失败不足 30 分钟返回 `cooldown`；其他情况调用一次现有 `sync_account_data`。所有输出只投影稳定字段。

- [ ] **Step 4: 运行新服务测试并确认通过**

Run: `python3 -m unittest test_content_project_metrics -v`

Expected: PASS。

- [ ] **Step 5: 提交 Task 2**

```bash
git add app_core/content_project_metrics.py test_content_project_metrics.py
git commit -m "feat: orchestrate project metric synchronization"
```

### Task 3: 项目隔离的账号与作品指标查询

**Files:**
- Modify: `app_core/content_project_metrics.py`
- Modify: `test_content_project_metrics.py`

**Interfaces:**
- Consumes: `content_project_task_links`、`publish_tasks`、`publish_task_items`、`user_info`、`platform_contents`、`platform_metric_snapshots`、`platform_data_service.account_period_summary`。
- Produces: `ContentProjectMetricsService.get_project_metrics(project_id, profile, days) -> dict`。

- [ ] **Step 1: 写项目隔离、缺失 ID 和 null 指标测试**

```python
def test_project_query_never_returns_other_projects_content(self):
    result = self.service.get_project_metrics("project-a", self.profile, 7)
    self.assertEqual([item["contentId"] for item in result["contents"]], ["work-a"])

def test_success_item_without_platform_id_is_missing_not_published(self):
    result = self.service.get_project_metrics("project-a", self.profile, 1)
    item = next(row for row in result["contents"] if row["taskId"] == self.no_id_task)
    self.assertIsNone(item["contentId"])
    self.assertEqual(item["availability"], "missing")
    self.assertTrue(all(value is None for value in item["metrics"].values()))

def test_preflight_and_failed_tasks_do_not_enter_project_contents(self):
    result = self.service.get_project_metrics("project-a", self.profile, 1)
    self.assertNotIn(self.preflight_task, {row["taskId"] for row in result["contents"]})
    self.assertNotIn(self.failed_task, {row["taskId"] for row in result["contents"]})
```

- [ ] **Step 2: 运行新增测试并确认查询方法不存在**

Run: `python3 -m unittest test_content_project_metrics.ContentProjectMetricsTests.test_project_query_never_returns_other_projects_content test_content_project_metrics.ContentProjectMetricsTests.test_success_item_without_platform_id_is_missing_not_published test_content_project_metrics.ContentProjectMetricsTests.test_preflight_and_failed_tasks_do_not_enter_project_contents -v`

Expected: FAIL，`get_project_metrics` 尚不存在。

- [ ] **Step 3: 实现严格联表查询与公开投影**

```python
def get_project_metrics(self, project_id: str, profile: Mapping[str, Any], days: int) -> dict:
    if type(days) is not int or days not in {1, 7, 30}:
        raise ContentProjectMetricsError("metric_payload_invalid", "days 只允许 1、7、30")
    accounts = [self._account_metrics(target, days) for target in profile["targets"]]
    contents = self._project_contents(project_id, profile)
    return {"projectId": project_id, "days": days, "accounts": accounts, "contents": contents}
```

作品查询只接受 `link.phase='formal'`、`task.mode='oneclick_publish'`、任务和条目均为 `success`、账号仍属于项目目标。平台 ID 为空时只返回当前项目任务的 `missing` 项；非空时以 `accountId + platformType + contentId` 查最新累计指标，未返回的白名单指标填 `None`。

- [ ] **Step 4: 运行项目指标服务完整测试**

Run: `python3 -m unittest test_content_project_metrics test_platform_data_service -v`

Expected: PASS。

- [ ] **Step 5: 提交 Task 3**

```bash
git add app_core/content_project_metrics.py test_content_project_metrics.py
git commit -m "feat: query project scoped platform metrics"
```

### Task 4: 网关登记和 MCP 工具

**Files:**
- Modify: `app_core/content_project_gateway.py`
- Modify: `app_core/oneclick_mcp_server.py`
- Modify: `test_content_project_gateway.py`
- Modify: `test_oneclick_mcp_server.py`

**Interfaces:**
- Consumes: `ContentProjectMetricsService` 三个公开方法。
- Produces: `ContentProjectGateway.sync_project_metrics(project_id)`、`get_project_metrics(project_id, days=1)`、`metrics_sync_status(project_id)`；MCP 工具 `oneclick_sync_project_metrics`、`oneclick_get_project_metrics`、`oneclick_metrics_sync_status`。

- [ ] **Step 1: 写网关和 MCP 合同失败测试**

```python
def test_gateway_metrics_methods_resolve_saved_profile(self):
    self.assertEqual(gateway.sync_project_metrics("silicon-exploration")["projectId"], "silicon-exploration")
    self.assertEqual(gateway.get_project_metrics("silicon-exploration", 7)["days"], 7)

def test_mcp_contract_exposes_metrics_without_sensitive_args(self):
    tools = {tool.name: tool for tool in asyncio.run(create_server(_Gateway()).list_tools())}
    self.assertIn("oneclick_sync_project_metrics", tools)
    self.assertIn("oneclick_get_project_metrics", tools)
    self.assertIn("oneclick_metrics_sync_status", tools)
```

- [ ] **Step 2: 运行测试并确认三种工具尚不存在**

Run: `python3 -m unittest test_content_project_gateway test_oneclick_mcp_server -v`

Expected: FAIL，缺少网关方法和 MCP 工具。

- [ ] **Step 3: 让网关与 MCP 只做薄适配**

```python
def sync_project_metrics(self, project_id: str) -> dict[str, Any]:
    normalized, profile = self._metrics_profile(project_id)
    return self.metrics_service.sync_project(normalized, profile)

@server.tool(name="oneclick_get_project_metrics", structured_output=True)
def get_project_metrics(project_id: str, days: int = 1) -> dict[str, Any]:
    return _call("metrics", lambda: gateway.get_project_metrics(project_id, days))
```

同步方法沿用 `_ensure_platform_work_available()`，纯状态和查询方法不得触发该检查或平台访问。`_request()` 必须把规范化 `projectId` 写入受控请求，确保 Task 1 的原子登记实际生效。

- [ ] **Step 4: 运行网关和 MCP 测试**

Run: `python3 -m unittest test_content_project_gateway test_oneclick_mcp_server -v`

Expected: PASS，MCP schema 中仍不出现敏感参数。

- [ ] **Step 5: 提交 Task 4**

```bash
git add app_core/content_project_gateway.py app_core/oneclick_mcp_server.py test_content_project_gateway.py test_oneclick_mcp_server.py
git commit -m "feat: expose project metrics over local mcp"
```

### Task 5: CLI 兼容入口和最终回归

**Files:**
- Modify: `desktop_native_app.py`
- Create: `test_content_project_metrics_cli.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: `ContentProjectGateway` 三个公开方法。
- Produces: `--controlled-publish-action metrics-sync|metrics-get|metrics-status`；`--content-project-id`；`--metrics-days`。

- [ ] **Step 1: 写 CLI stdout 稳定 JSON 的失败测试**

```python
def test_metrics_get_cli_returns_one_json_document_without_ui(self):
    completed = subprocess.run(
        [sys.executable, "desktop_native_app.py", "--controlled-publish-action", "metrics-get",
         "--content-project-id", "silicon-exploration", "--metrics-days", "7"],
        cwd=ROOT, env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        text=True, capture_output=True,
    )
    payload = json.loads(completed.stdout)
    self.assertEqual(payload["projectId"], "silicon-exploration")
    self.assertEqual(payload["days"], 7)
```

- [ ] **Step 2: 运行 CLI 测试并确认 action 不被解析器接受**

Run: `python3 -m unittest test_content_project_metrics_cli -v`

Expected: FAIL，argparse 拒绝 `metrics-get`。

- [ ] **Step 3: 增加共用网关的 CLI 分支和 README 示例**

```python
if action in {"metrics-sync", "metrics-get", "metrics-status"}:
    gateway = ContentProjectGateway()
    operation = {
        "metrics-sync": lambda: gateway.sync_project_metrics(args.content_project_id),
        "metrics-get": lambda: gateway.get_project_metrics(args.content_project_id, args.metrics_days),
        "metrics-status": lambda: gateway.metrics_sync_status(args.content_project_id),
    }[action]
    _controlled_json(operation())
    return 0
```

CLI 只输出一份 JSON；日志继续重定向到 stderr。README 说明内容项目默认使用 MCP，CLI 仅用于排错和兼容自动化。

- [ ] **Step 4: 运行测试阶梯和离屏客户端**

Run: `python3 -m unittest test_content_project_metrics_cli test_content_project_gateway test_oneclick_mcp_server test_content_project_metrics test_controlled_publish test_task_service test_platform_data_service test_platform_data_sync -v`

Expected: PASS。

Run: `QT_QPA_PLATFORM=offscreen python3 desktop_native_app.py --self-test`

Expected: 输出 `NATIVE_DESKTOP_SELF_TEST_OK`。

Run: `python3 -m unittest discover -v`

Expected: PASS；如发现问题，停止全量测试，修复后先重跑对应单项，最后再重跑全量。

- [ ] **Step 5: 检查差异并提交 Task 5**

```bash
git diff --check
git status --short
git add desktop_native_app.py test_content_project_metrics_cli.py README.md
git commit -m "feat: add project metrics cli gateway"
```
