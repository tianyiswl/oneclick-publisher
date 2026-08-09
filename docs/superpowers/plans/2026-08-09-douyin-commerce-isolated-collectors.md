# 抖音带货平台设置隔离采集会话 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将抖音带货平台设置改造成音乐、国内地点、本地点三个独立临时采集会话，并用批次代际、关闭屏障和结构化诊断阻止跨功能、跨批次页面状态污染。

**Architecture:** 新增纯状态模块和 `DouyinCommerceCollectorManager`，由它在同一客户端进程内组合三个独立 `DouyinCommerceSessionManager` 实例；每个实例拥有自己的 Playwright BrowserContext、Page 和 Session，所有平台动作再由协调器串行化。平台设置只保存本地候选和用户选择；进入预检或正式发布前关闭全部采集器，现有批量执行器继续为每条视频创建全新正式会话并逐项回读。

**Tech Stack:** Python 3.12、asyncio、threading、Playwright、PyQt6、unittest、PyInstaller、现有抖音带货会话管理器与批量执行器。

## Global Constraints

- 国内地点采集器进入平台设置自动启动；收藏音乐仅在点击刷新时启动；本地点仅在首次本地搜索时启动。
- 三个采集器必须使用独立 `BrowserContext + Page + Session`，不能共享 DOM、locator、浮层、候选列表或实时 Session ID。
- 三个采集器可以读取同一账号的登录状态文件，但不得复制、导出或记录 Cookie、二维码、验证码、页面 HTML和原始请求。
- 所有采集动作必须串行；不得并发刷新音乐和搜索地点，不得无限重试或通过高频请求规避平台频控。
- 地点搜索不得调用收藏音乐刷新，不得在同一采集 Page 内往返切换国内和本地。
- 每次进入平台设置创建唯一 `setupGenerationId`；旧代际关闭后的迟到回调不得修改 UI 或新批次选择。
- 正常完成、途中放弃、异常停止、重新登录、账号切换和客户端退出都必须执行统一 `cancel-and-close`。
- 当前批次的音乐和地点选择在放弃或发布完成后清空；账号级音乐缓存和地点预设只能恢复为未确认候选。
- 进入预检或发布前，存活采集器数量必须为 0；关闭屏障不完整时安全停止。
- 每条正式发布必须使用全新发布会话，先清理页面浮层和内存候选，再应用音乐、地点、声明和定时并逐项回读。
- 探针视频只用于未提交临时编辑页；自动测试和实机采集不得点击最终提交、保存草稿或公开发布。
- 真实单条正式发布验证必须由 Andy 另行明确确认，不能由本计划执行授权推断。
- 所有新增用户文案和代码注释使用中文；文件编码保持 UTF-8。

---

## 文件结构与职责

- Create `app_core/douyin_commerce_setup_state.py`：定义代际、采集器状态、合法状态转换和脱敏诊断事件；不导入 Playwright 或 Qt。
- Create `app_core/douyin_commerce_probe.py`：解析应用内置探针视频路径，生成不含用户内容的探针上传 payload。
- Create `app_core/douyin_commerce_collectors.py`：组合三个独立会话管理器，串行化动作，校验代际，执行单采集器重建与统一关闭。
- Modify `app_core/douyin_commerce_session.py`：移除地点依赖音乐预热；增加正式发布页设置基线清理入口。
- Modify `app_core/douyin_commerce_batch_executor.py`：每条上传后先建立干净设置基线，记录唯一正式 Session，并继续沿用逐项回读和平台回执硬门。
- Modify `ui/douyin_commerce_page.py`：使用 `setupGenerationId` 代替平台设置 `_session_id`，将音乐、地点、声明改成本地待应用状态，显示三个采集器状态和受控重试。
- Modify `ui/main_window.py`：客户端退出前调用抖音带货页统一关闭入口。
- Modify `tools/create_platform_test_assets.py`：生成固定的轻量探针 MP4 到 `ui/assets/`。
- Create `ui/assets/douyin-commerce-probe.mp4`：打包进 Mac/Windows 客户端的非敏感探针视频。
- Create `tools/verify_douyin_commerce_collectors.py`：开发者实机预提交验证器；默认只打印计划，必须显式传 `--execute` 才访问平台，且没有提交接口。
- Create `test_douyin_commerce_setup_state.py`：代际、迟到结果、状态转换和诊断脱敏测试。
- Create `test_douyin_commerce_probe.py`：探针文件、冻结/源码路径和 payload 清理测试。
- Create `test_douyin_commerce_collectors.py`：三会话隔离、懒启动、动作串行、受控重建和统一关闭测试。
- Modify `test_douyin_commerce_service.py`：地点不再预热音乐、UI 本地暂存、代际状态和关闭屏障回归。
- Modify `test_douyin_commerce_batch_executor.py`：逐视频全新 Session、基线清理顺序、关闭和失败归因测试。
- Modify `test_macos_build.py`、`test_windows_build.py`：探针资源进入构建 datas 且不进入运行数据目录。
- Modify `docs/DOUYIN_COMMERCE_WORKFLOW.md`：记录新平台设置会话边界、状态定义和实机验证边界。

### Task 1: 代际状态机与脱敏诊断契约

**Files:**
- Create: `app_core/douyin_commerce_setup_state.py`
- Create: `test_douyin_commerce_setup_state.py`

**Interfaces:**
- Produces: `SetupGenerationState`，固定值 `new`、`collecting`、`ready`、`closing_collectors`、`publishing`、`cancelling`、`paused_for_login`、`closed`。
- Produces: `CollectorType`，固定值 `domestic_location`、`favorite_music`、`local_location`。
- Produces: `CollectorState`，固定值 `not_started`、`starting`、`active`、`retrying`、`failed`、`closing`、`closed`。
- Produces: `CollectorSlot`、`SetupGeneration`、`CollectorDiagnosticEvent`。
- Produces: `new_setup_generation(account_id: int) -> SetupGeneration`。
- Produces: `SetupGeneration.accepts_result(generation_id: str, collector_type: CollectorType, instance_id: str) -> bool`。
- Produces: `CollectorDiagnosticEvent.to_public_dict() -> dict[str, object]`，不返回异常堆栈、账号文件或页面内容。

- [ ] **Step 1: 写代际转换、迟到结果和脱敏失败测试**

创建 `test_douyin_commerce_setup_state.py`，使用 `unittest.TestCase` 覆盖：

```python
def test_closed_generation_rejects_late_collector_result(self):
    generation = new_setup_generation(account_id=31)
    generation.transition(SetupGenerationState.COLLECTING)
    generation.activate_collector(
        CollectorType.DOMESTIC_LOCATION,
        instance_id="domestic-1",
        session_id="session-a",
    )
    generation.transition(SetupGenerationState.CANCELLING)
    generation.close()

    self.assertFalse(
        generation.accepts_result(
            generation.generation_id,
            CollectorType.DOMESTIC_LOCATION,
            "domestic-1",
        )
    )

def test_public_diagnostic_does_not_expose_sensitive_values(self):
    event = CollectorDiagnosticEvent(
        request_id="request-1",
        setup_generation_id="generation-1",
        collector_type=CollectorType.LOCAL_LOCATION,
        collector_instance_id="local-1",
        account_masked_id="account-31",
        phase="search",
        action="search_locations",
        scope="local",
        keyword="夜南香",
        attempt=1,
        candidate_count=0,
        duration_ms=1200,
        outcome="failed",
        error_code="candidate_panel_missing",
        cleanup_result="closed",
    )
    public = event.to_public_dict()
    self.assertEqual(public["errorCode"], "candidate_panel_missing")
    self.assertNotIn("cookie", str(public).casefold())
    self.assertNotIn("session-a", str(public))
```

再加入非法转换测试：`CLOSED -> COLLECTING`、`PUBLISHING -> READY` 必须抛出 `SetupGenerationStateError`；不同 `generation_id`、不同 `instance_id` 和 `FAILED/CLOSING/CLOSED` 采集器结果必须返回 `False`。

- [ ] **Step 2: 运行测试确认模块尚不存在**

Run: `python -m unittest test_douyin_commerce_setup_state -v`

Expected: FAIL，提示 `app_core.douyin_commerce_setup_state` 不存在。

- [ ] **Step 3: 实现状态枚举、采集器槽位和合法转换**

在新模块中定义显式转换表，不允许调用方直接猜测状态：

```python
_ALLOWED_TRANSITIONS = {
    SetupGenerationState.NEW: {
        SetupGenerationState.COLLECTING,
        SetupGenerationState.CANCELLING,
    },
    SetupGenerationState.COLLECTING: {
        SetupGenerationState.READY,
        SetupGenerationState.CLOSING_COLLECTORS,
        SetupGenerationState.CANCELLING,
        SetupGenerationState.PAUSED_FOR_LOGIN,
    },
    SetupGenerationState.READY: {
        SetupGenerationState.COLLECTING,
        SetupGenerationState.CLOSING_COLLECTORS,
        SetupGenerationState.CANCELLING,
        SetupGenerationState.PAUSED_FOR_LOGIN,
    },
    SetupGenerationState.CLOSING_COLLECTORS: {
        SetupGenerationState.PUBLISHING,
        SetupGenerationState.CLOSED,
    },
    SetupGenerationState.PUBLISHING: {SetupGenerationState.CLOSED},
    SetupGenerationState.CANCELLING: {SetupGenerationState.CLOSED},
    SetupGenerationState.PAUSED_FOR_LOGIN: {SetupGenerationState.CLOSED},
    SetupGenerationState.CLOSED: set(),
}
```

`SetupGeneration.activate_collector()` 必须更新指定槽位的 `instance_id`、`session_id` 和 `ACTIVE`；`accepts_result()` 只比较代际、类型、实例和状态，不读取 DOM。`close()` 必须把三个槽位全部标为 `CLOSED`、清空 `session_id`，再将代际置为 `CLOSED`。

- [ ] **Step 4: 实现诊断事件的安全投影**

`CollectorDiagnosticEvent` 使用冻结 dataclass；`to_public_dict()` 只输出设计文档锁定的驼峰字段。`keyword` 最多保留 80 个字符，`account_masked_id` 只能接受 `account-<整数>`，`error_code` 必须来自模块内 `COLLECTOR_ERROR_CODES`。未知错误统一映射为 `collector_unknown`，不得把原异常文本直接写入公开字典。

- [ ] **Step 5: 运行状态机测试确认通过**

Run: `python -m unittest test_douyin_commerce_setup_state -v`

Expected: PASS，非法转换被拒绝，迟到结果不能写入，公开诊断不含敏感值。

- [ ] **Step 6: 提交状态机契约**

```bash
git add app_core/douyin_commerce_setup_state.py test_douyin_commerce_setup_state.py
git commit -m "增加抖音平台设置代际状态机"
```

### Task 2: 内置非敏感探针视频与资源路径

**Files:**
- Modify: `tools/create_platform_test_assets.py`
- Create: `app_core/douyin_commerce_probe.py`
- Create: `ui/assets/douyin-commerce-probe.mp4`
- Create: `test_douyin_commerce_probe.py`
- Modify: `test_macos_build.py`
- Modify: `test_windows_build.py`

**Interfaces:**
- Produces: `DOUYIN_COMMERCE_PROBE_RELATIVE_PATH = Path("ui/assets/douyin-commerce-probe.mp4")`。
- Produces: `resolve_douyin_commerce_probe(resource_dir: Path | None = None) -> Path`。
- Produces: `build_probe_upload_payload(source: Mapping[str, Any], *, resource_dir: Path | None = None) -> dict[str, Any]`。
- Consumes: `conf.RESOURCE_DIR`；源码和 PyInstaller 冻结包都从只读资源目录读取，不写安装目录。

- [ ] **Step 1: 写探针存在性、体积、路径与 payload 清理失败测试**

创建测试：

```python
def test_probe_payload_replaces_user_content_and_keeps_only_account(self):
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        probe = root / DOUYIN_COMMERCE_PROBE_RELATIVE_PATH
        probe.parent.mkdir(parents=True)
        probe.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 2_048)
        payload = build_probe_upload_payload(
            {
                "type": 3,
                "workflow": "douyin-commerce",
                "commerceMode": "local-group-buy",
                "contentType": "video",
                "accountList": ["account.json"],
                "fileList": ["/private/user-video.mp4"],
                "title": "用户标题",
                "description": "用户文案",
                "tags": ["用户标签"],
                "selectedMusic": {"musicId": "old"},
                "locationPoi": {"poiId": "old"},
                "runtimeMode": "publish",
                "debugDryRun": False,
            },
            resource_dir=root,
        )
    self.assertEqual(payload["fileList"], [str(probe)])
    self.assertEqual(payload["title"], "一键发平台设置探针")
    self.assertEqual(payload["description"], "仅用于读取平台设置候选，不提交发布")
    self.assertEqual(payload["tags"], [])
    self.assertEqual(payload["runtimeMode"], "preflight")
    self.assertTrue(payload["debugDryRun"])
    self.assertNotIn("selectedMusic", payload)
    self.assertNotIn("locationPoi", payload)
```

再断言仓库真实探针以 `ftyp` MP4 标识开头、大小介于 1 KB 与 512 KB；缺失、空文件或超限时抛出 `DouyinCommerceProbeError`。构建测试断言 Mac/Windows spec 都包含整个 `ui/assets` 目录，并断言探针不位于 `demo-runtime`。

- [ ] **Step 2: 运行测试确认探针与模块尚不存在**

Run: `python -m unittest test_douyin_commerce_probe test_macos_build test_windows_build -v`

Expected: FAIL，提示探针模块或 MP4 文件不存在。

- [ ] **Step 3: 扩展素材生成脚本并生成固定探针**

在 `tools/create_platform_test_assets.py` 增加不依赖系统字体的 `create_douyin_commerce_probe()`：

```python
probe = ROOT / "ui" / "assets" / "douyin-commerce-probe.mp4"
command = [
    "ffmpeg", "-y",
    "-f", "lavfi", "-i", "color=c=0x17304d:s=360x640:r=15",
    "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
    "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-shortest", "-movflags", "+faststart", str(probe),
]
subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
```

在 `main()` 末尾调用一次，然后运行：

Run: `python tools/create_platform_test_assets.py`

Expected: `ui/assets/douyin-commerce-probe.mp4` 存在、可读取且小于 512 KB；脚本的其他预检素材仍写入已忽略的 `demo-runtime`。

- [ ] **Step 4: 实现探针资源解析和安全 payload**

`resolve_douyin_commerce_probe()` 以 `resource_dir or conf.RESOURCE_DIR` 为根，只拼接固定相对路径；调用 `resolve()` 后确认结果仍在资源根目录内。`build_probe_upload_payload()` 只保留账号和带货工作流身份字段，覆盖文件、标题、文案、标签和 dry-run 字段，并显式删除：`selectedMusic`、`locationPoi`、`locationKeyword`、`locationScope`、`contentDeclaration`、`scheduleTime`、`commerceStore`。

- [ ] **Step 5: 运行探针和构建资源测试确认通过**

Run: `python -m unittest test_douyin_commerce_probe test_macos_build test_windows_build -v`

Expected: PASS，源码/冻结资源根均可解析，探针不含用户 payload，现有构建 datas 自动包含该文件。

- [ ] **Step 6: 提交探针资源**

```bash
git add tools/create_platform_test_assets.py app_core/douyin_commerce_probe.py ui/assets/douyin-commerce-probe.mp4 test_douyin_commerce_probe.py test_macos_build.py test_windows_build.py
git commit -m "增加抖音平台设置内置探针"
```

### Task 3: 三采集器协调器、串行动作与受控重建

**Files:**
- Create: `app_core/douyin_commerce_collectors.py`
- Create: `test_douyin_commerce_collectors.py`

**Interfaces:**
- Consumes: Task 1 的 `SetupGeneration`、`CollectorType`、`CollectorState`、`CollectorDiagnosticEvent`。
- Consumes: Task 2 的 `build_probe_upload_payload()`。
- Produces: `DouyinCommerceCollectorError(RuntimeError)`。
- Produces: `DouyinCommerceCollectorManager(manager_factory=DouyinCommerceSessionManager, probe_payload_builder=build_probe_upload_payload, event_sink=None)`。
- Produces: `begin_generation(payload: Mapping[str, Any], *, on_progress=None) -> dict[str, object]`。
- Produces: `refresh_favorite_music(generation_id: str) -> list[dict[str, str]]`。
- Produces: `search_locations(generation_id: str, keyword: object, scope: object) -> list[dict[str, Any]]`。
- Produces: `retry_collector(generation_id: str, collector_type: object) -> dict[str, object]`。
- Produces: `close_generation(generation_id: str | None = None, *, reason: str) -> dict[str, object]`。
- Produces: `status(generation_id: str | None = None) -> dict[str, object]`。
- Produces: 模块单例 `commerce_collector_manager`。

- [ ] **Step 1: 写独立实例、自动/懒启动和固定范围失败测试**

在新测试文件创建 `FakeSessionManager`，每次构造获得唯一 `manager_id`，记录 `start_upload`、`refresh_favorite_music`、`search_locations`、`close` 调用，并返回唯一 `sessionId`。

```python
def test_begin_starts_only_domestic_then_music_and_local_start_lazily(self):
    factory = FakeManagerFactory()
    manager = DouyinCommerceCollectorManager(
        manager_factory=factory,
        probe_payload_builder=lambda payload: {**payload, "fileList": ["probe.mp4"]},
    )
    begun = manager.begin_generation(self.upload_payload)
    generation_id = begun["setupGenerationId"]

    self.assertEqual(len(factory.instances), 1)
    self.assertEqual(begun["collectors"]["domestic_location"], "active")
    self.assertEqual(begun["collectors"]["favorite_music"], "not_started")
    self.assertEqual(begun["collectors"]["local_location"], "not_started")

    manager.refresh_favorite_music(generation_id)
    manager.search_locations(generation_id, "夜南香", "local")

    self.assertEqual(len(factory.instances), 3)
    self.assertEqual(len({item.session_id for item in factory.instances}), 3)
    self.assertEqual(factory.instances[0].location_scopes, [])
    self.assertEqual(factory.instances[2].location_scopes, ["local"])
```

再测试国内关键词只进入国内实例，本地关键词只进入本地实例；同一实例收到另一范围时必须抛出 `collector_scope_mismatch`，不能调用底层 `search_locations`。

- [ ] **Step 2: 写动作串行、迟到结果和受控重建失败测试**

用两个 `threading.Event` 让国内搜索阻塞，在另一线程调用音乐刷新；断言国内动作释放前音乐底层调用未开始。关闭代际后再释放国内返回，断言调用方得到 `stale_result_discarded`，新代际状态未变化。

```python
def test_retry_replaces_only_failed_collector(self):
    generation_id = self.manager.begin_generation(self.upload_payload)["setupGenerationId"]
    self.manager.refresh_favorite_music(generation_id)
    before = self.manager.status(generation_id)["collectorInstanceIds"]

    retried = self.manager.retry_collector(generation_id, "favorite_music")

    after = retried["collectorInstanceIds"]
    self.assertEqual(after["domestic_location"], before["domestic_location"])
    self.assertNotEqual(after["favorite_music"], before["favorite_music"])
```

统一关闭测试要求三个底层 `close(session_id)` 都被调用，即使第二个抛异常也继续关闭第三个；返回 `closed=False`、`aliveCollectorCount` 和每个 `cleanupResult`，不得吞掉关闭不完整事实。

- [ ] **Step 3: 运行测试确认协调器尚不存在**

Run: `python -m unittest test_douyin_commerce_collectors -v`

Expected: FAIL，提示 `app_core.douyin_commerce_collectors` 不存在。

- [ ] **Step 4: 实现代际运行时和可取消单一动作队列**

内部运行时使用以下结构，不把浏览器对象复制到状态模块：

```python
@dataclass
class _CollectorRuntime:
    collector_type: CollectorType
    manager: Any
    instance_id: str
    session_id: str = ""
    fixed_scope: str = ""

@dataclass
class _GenerationRuntime:
    generation: SetupGeneration
    probe_payload: dict[str, Any]
    collectors: dict[CollectorType, _CollectorRuntime]
```

协调器持有 `_state_lock = threading.RLock()` 和内部 `_CollectorActionQueue`。动作队列使用 `ThreadPoolExecutor(max_workers=1, thread_name_prefix="douyin-commerce-collector-action")`，为每个 future 保存 `generation_id/request_id`；`run()` 同步等待当前动作结果，`cancel_pending(generation_id)` 对尚未开始的 future 调用 `cancel()`，`wait_running(generation_id, timeout=15)` 只等待正在执行的一项。`begin_generation()` 先关闭旧代际，再创建新代际，并只通过队列调用 `_ensure_collector(DOMESTIC_LOCATION)`；`_ensure_collector()` 每次通过 `manager_factory()` 创建新管理器，绝不把同一实例放进两个槽位。

- [ ] **Step 5: 实现固定职责路由、结果代际校验和受控重建**

`refresh_favorite_music()` 只允许 `FAVORITE_MUSIC`，并把底层 `refresh_favorite_music(session_id)` 作为一个队列动作提交。`search_locations()` 将 `domestic` 映射到 `DOMESTIC_LOCATION`、`local` 映射到 `LOCAL_LOCATION`，比较槽位 `fixed_scope` 后再提交队列动作。底层返回后重新获取 `_state_lock` 并调用 `generation.accepts_result()`；失败时发出 `stale_result_discarded` 事件并抛出受控错误。

`retry_collector()` 必须作为一个不可拆分的队列动作关闭旧实例、清空该槽位、创建同类型新实例；本地和国内保持原固定范围，音乐保持无范围。不得改变另外两个实例 ID。

- [ ] **Step 6: 实现统一关闭和公开状态**

`close_generation()` 先在 `_state_lock` 内把代际转为 `CANCELLING` 或 `CLOSING_COLLECTORS`，使新动作在入队前被拒绝；随后调用 `cancel_pending(generation_id)`，最多等待当前动作 15 秒，再按国内、音乐、本地顺序调用底层 `close()`，在 `finally` 中把各槽位标为关闭。当前动作超时必须记为 `cleanup_incomplete`，不能宣称关闭成功。全部成功时返回：

```python
{
    "closed": True,
    "setupGenerationId": generation_id,
    "aliveCollectorCount": 0,
    "cleanupResults": {
        "domestic_location": "closed",
        "favorite_music": "closed",
        "local_location": "closed",
    },
}
```

未启动的槽位返回 `not_started`。任何关闭异常都返回 `closed=False` 并保留 `aliveCollectorCount`，供关闭屏障安全停止。

- [ ] **Step 7: 运行协调器测试确认通过**

Run: `python -m unittest test_douyin_commerce_collectors -v`

Expected: PASS，三个实例互异、动作串行、范围固定、迟到结果丢弃、单点重建不影响其他采集器、统一关闭不遗漏。

- [ ] **Step 8: 提交三采集器协调器**

```bash
git add app_core/douyin_commerce_collectors.py test_douyin_commerce_collectors.py
git commit -m "增加抖音平台设置隔离采集器"
```

### Task 4: 移除音乐预热地点并增加正式页干净基线

**Files:**
- Modify: `app_core/douyin_commerce_session.py:130-159, 550-690, 811-890`
- Modify: `test_douyin_commerce_service.py:4589-5200`

**Interfaces:**
- Removes: `_CommerceEditorSession.platform_dom_needs_music_warmup`。
- Preserves: `search_locations(session_id, keyword, scope) -> list[dict[str, Any]]`，但不得调用任何音乐方法。
- Produces: `prepare_publish_settings(session_id: str) -> dict[str, object]`，成功返回 `{"status": "clean", "sessionId": ..., "openLayerCount": 0}`。

- [ ] **Step 1: 写地点搜索绝不调用音乐刷新失败测试**

在 `DouyinCommerceSessionContractTests` 新增异步契约：构造有效 `_CommerceEditorSession`，patch `manager._refresh_favorite_music` 为 `AsyncMock`，patch `search_commerce_location_store_candidates` 返回一个完整地点。

```python
rows = await manager._search_locations("session-1", "夜南香", "domestic")

self.assertEqual(rows[0]["name"], "夜南香北京烤鸭")
refresh_music.assert_not_awaited()
search.assert_awaited_once_with(session.page, "夜南香", scope="domestic")
```

再模拟第一次 `pair-not-in-search-panel`、第二次成功；断言只执行有限等待和重新地点搜索，音乐刷新仍为零次。

- [ ] **Step 2: 写正式发布设置基线失败测试**

构造带旧音乐候选、地点候选和已展开地点 listbox 的会话，调用 `prepare_publish_settings` 后断言：

```python
self.assertEqual(result["openLayerCount"], 0)
self.assertEqual(session.music_candidates, [])
self.assertIsNone(session.selected_music)
self.assertEqual(session.commerce_location_candidates, [])
self.assertIsNone(session.location)
self.assertEqual(session.location_scope, "")
close_location.assert_awaited_once_with(session.page)
```

如果音乐选择器仍处于打开状态，方法必须先调用现有关闭音乐抽屉能力；关闭失败时抛出 `DouyinCommerceSessionError("正式发布页旧浮层未能清理")`，不得继续应用设置。

- [ ] **Step 3: 运行专项测试确认现有音乐预热被命中**

Run: `python -m unittest test_douyin_commerce_service.DouyinCommerceSessionContractTests -v`

Expected: FAIL，现有 `_search_locations` 调用了 `_refresh_favorite_music`，且 `prepare_publish_settings` 尚不存在。

- [ ] **Step 4: 删除音乐预热字段和搜索耦合**

从 dataclass、`_start_upload()` 和 `_search_locations()` 删除 `platform_dom_needs_music_warmup`。保留搜索前/成功后/失败后的地点候选关闭。对于第一次 `pair-not-in-search-panel`，只执行：

```python
if attempt == 0 and transient_scope_failure:
    await session.page.wait_for_timeout(1_500)
    continue
```

第二次仍失败时按原错误安全停止。不要调用 `_refresh_favorite_music()`，不要重新设置任何音乐预热标记。

- [ ] **Step 5: 实现正式发布页设置基线清理**

增加同步公开入口和异步实现：

```python
def prepare_publish_settings(self, session_id: str) -> dict[str, object]:
    return self._call(self._prepare_publish_settings(session_id))
```

异步方法先取得当前会话，关闭该会话自己的音乐抽屉和地点候选层，再清空仅存在内存的音乐/地点候选、选择、范围、声明、预检指纹和定时回读。页面结构无法唯一清理时抛错；不得把“清空内存”冒充页面浮层关闭成功。

- [ ] **Step 6: 运行会话与地点 DOM 回归**

Run: `python -m unittest test_douyin_commerce_service.DouyinCommerceSessionContractTests test_douyin_commerce_service.DouyinCommerceLocationDomTests -v`

Expected: PASS，地点不依赖音乐，瞬态地点面板只重试一次，正式页基线可验证为干净。

- [ ] **Step 7: 提交会话解耦**

```bash
git add app_core/douyin_commerce_session.py test_douyin_commerce_service.py
git commit -m "移除抖音地点对音乐预热的依赖"
```

### Task 5: 平台设置 UI 改为本地暂存和三采集器状态

**Files:**
- Modify: `ui/douyin_commerce_page.py:411-620, 1320-1510, 4460-5065`
- Modify: `test_douyin_commerce_service.py:2707-4560, 6844-8015`

**Interfaces:**
- Consumes: `commerce_collector_manager.begin_generation()`、`refresh_favorite_music()`、`search_locations()`、`retry_collector()`、`status()`。
- Produces: `DouyinCommercePage._setup_generation_id: str`，只代表平台设置代际，不作为正式发布 Session ID。
- Produces: `_start_setup_generation(payload: dict) -> None`、`_setup_generation_succeeded(result: object) -> None`。
- Produces: `_stage_music_selection(candidate: dict[str, str]) -> None`、`_stage_location_selection(candidate: dict[str, str]) -> None`、`_stage_declaration_selection(value: str) -> None`。
- Produces: `_render_collector_status(status: Mapping[str, Any]) -> None`、`_retry_last_failed_collector() -> None`。

- [ ] **Step 1: 写进入平台设置只自动启动国内采集器失败测试**

patch `commerce_collector_manager.begin_generation` 返回代际和三个状态，调用 `continue_after_content()` 后触发成功回调：

```python
self.assertEqual(self.page._setup_generation_id, "generation-a")
self.assertEqual(self.page.pages.currentIndex(), 1)
begin.assert_called_once()
self.assertNotIn(self.first_video_path, begin.call_args.args[0]["fileList"])
self.assertEqual(self.page.domestic_collector_status.text(), "国内地点：可用")
self.assertEqual(self.page.music_collector_status.text(), "收藏音乐：点击刷新后启动")
self.assertEqual(self.page.local_collector_status.text(), "本地点：首次搜索时启动")
```

这里不直接断言探针路径字符串，而是断言 UI 没有调用 `commerce_session_manager.start_upload`，且协调器负责替换用户文件。

- [ ] **Step 2: 写音乐、地点和声明只本地暂存失败测试**

音乐刷新测试令协调器返回候选，点击候选后断言 `_selected_music` 更新，同时：

```python
legacy_manager.select_favorite_music.assert_not_called()
legacy_manager.select_cached_favorite_music.assert_not_called()
self.assertIn("发布时重新核验", self.page.music_status.text())
```

国内搜索调用 `collector_manager.search_locations(generation_id, keyword, "domestic")`；本地搜索第一次才令状态出现 `local_location=active`。选择地点只更新 `_batch_locations` 或 `_selected_location_data`，不得调用 `apply_location`。选择声明只更新 `_confirmed_declaration`，不得调用 `select_content_declaration`。

- [ ] **Step 3: 写任意顺序和单采集器重试 UI 失败测试**

分别执行“音乐→国内→本地”“国内→音乐→本地”“本地→国内→音乐”，断言调用都携带同一 `setupGenerationId` 且 UI 选择互不清空。模拟本地错误码后点击“重试本地点”，断言只调用：

```python
collector_manager.retry_collector.assert_called_once_with(
    "generation-a", "local_location"
)
```

音乐和国内候选仍保留；重试成功后本地候选清空，等待用户重新搜索。

- [ ] **Step 4: 运行 UI 专项测试确认仍使用共享 `_session_id`**

Run: `python -m unittest test_douyin_commerce_service.DouyinCommerceUiTests test_douyin_commerce_service.DouyinCommerceBatchUiTests -v`

Expected: FAIL，当前页面仍上传第一条用户视频并调用共享会话的音乐、地点和声明写入。

- [ ] **Step 5: 替换平台设置入口和状态字段**

在 `__init__` 增加：

```python
self._setup_generation_id = ""
self._collector_status: dict[str, object] = {}
self._last_failed_collector_type = ""
self._staged_music_confirmed = False
self._staged_location_confirmed = False
self._staged_declaration_confirmed = False
```

`continue_after_content()` 对 1 至 20 条视频统一调用 `_start_setup_generation(payload)`；不再以第一条用户视频建立设置会话，也不在平台设置阶段调用 `_start_content_sync()`。内容或账号变化时先关闭旧代际，再创建新代际。

`_setup_generation_succeeded()` 只接受包含非空 `setupGenerationId` 且国内采集器为 `active` 的结果；随后进入平台设置页。`needs_login` 仍路由到账号管理，不打开前台浏览器。

- [ ] **Step 6: 将音乐、地点和声明改为本地待应用状态**

音乐候选点击后调用 `_stage_music_selection()`：规范化音乐公开字段、设置 `_selected_music` 和 `_staged_music_confirmed=True`，文案显示“本地已选择，发布时重新核验”。缓存音乐同样只能成为候选，不调用平台选择。

地点搜索改调协调器；候选选择继续保存完整 `poiId/name/address` 与固定 `locationScope`，设置 `_staged_location_confirmed=True`，不在采集页应用地点。声明选择仅规范化并保存本地值。`collect_batch_payload()` 继续使用这些公开字段，最终由批量执行器重新写入正式页。

- [ ] **Step 7: 增加三个状态标签和受控重试入口**

在平台设置卡片顶部增加 `domestic_collector_status`、`music_collector_status`、`local_collector_status` 三个 `QLabel`，以及默认隐藏的 `retry_collector_button`。按钮文案由失败类型决定：“重试国内地点”“重试收藏音乐”“重试本地点”。`_render_collector_status()` 只显示状态、候选数量、耗时和错误码，不显示 Session ID 或底层异常全文。

- [ ] **Step 8: 运行 UI 专项测试确认通过**

Run: `QT_QPA_PLATFORM=offscreen python -m unittest test_douyin_commerce_service.DouyinCommerceUiTests test_douyin_commerce_service.DouyinCommerceBatchUiTests -v`

Expected: PASS，国内自动启动、音乐/本地懒启动、三种顺序互不污染、平台设置没有立即写入、单采集器可重试。

- [ ] **Step 9: 提交平台设置 UI 迁移**

```bash
git add ui/douyin_commerce_page.py test_douyin_commerce_service.py
git commit -m "改造抖音平台设置为隔离采集"
```

### Task 6: 统一关闭、放弃重置和正式发布前屏障

**Files:**
- Modify: `ui/douyin_commerce_page.py:4074-4260, 5260-5450`
- Modify: `ui/main_window.py:665-670`
- Modify: `app_core/douyin_commerce_batch_executor.py:480-705`
- Modify: `test_douyin_commerce_service.py`
- Modify: `test_douyin_commerce_batch_executor.py`
- Modify: `test_main_window.py`

**Interfaces:**
- Produces: `DouyinCommercePage._close_setup_generation(reason: str) -> dict[str, object]`。
- Produces: `DouyinCommercePage._run_after_collector_barrier(operation: Callable[[], object], *, reason: str) -> object`。
- Produces: `DouyinCommercePage.shutdown() -> None`。
- Consumes: `commerce_collector_manager.close_generation()` 返回的 `closed` 与 `aliveCollectorCount`。
- Consumes: Task 4 的 `commerce_session_manager.prepare_publish_settings(session_id)`。

- [ ] **Step 1: 写放弃、重登录、退出和新批次统一关闭失败测试**

为以下入口分别建立测试并断言调用相同协调器关闭方法：

```python
self.page._setup_generation_id = "generation-a"
self.page._abandon_session(silent=True)
close.assert_called_once_with("generation-a", reason="user_abandon")
self.assertEqual(self.page._setup_generation_id, "")
self.assertIsNone(self.page._selected_music)
self.assertEqual(self.page._batch_locations, {})
```

其余原因固定为：登录失效 `login_required`、账号/内容变化 `generation_replaced`、正常发布完成 `publish_completed`、客户端退出 `client_shutdown`。客户端退出测试调用 `MainWindow.closeEvent`，断言 `douyin_commerce.shutdown()` 发生在 `account_browser_service.close_all_backend_sessions(wait=True)` 之前。

- [ ] **Step 2: 写预检/发布关闭屏障失败测试**

模拟 `close_generation()` 返回 `closed=False, aliveCollectorCount=1`，调用 `start_batch_preflight()` 或 `start_batch_publish()` 后断言批量执行器没有启动，UI 显示“平台设置临时会话未完全关闭，已安全停止”。成功返回必须清空 `_setup_generation_id`，再调用执行器。

- [ ] **Step 3: 写逐视频干净基线和唯一 Session 失败测试**

扩展 `FakeCommerceSessionManager` 记录顺序：

```python
self.assertEqual(
    manager.calls[:5],
    [
        ("start_upload", "video-1.mp4"),
        ("prepare_publish_settings", "session-1"),
        ("select_cached_favorite_music", "session-1"),
        ("select_content_declaration", "session-1"),
        ("search_locations", "session-1"),
    ],
)
self.assertEqual(manager.started_session_ids, ["session-1", "session-2"])
self.assertEqual(manager.closed_session_ids, ["session-1", "session-2"])
```

`prepare_publish_settings` 抛错时，本条记为失败并关闭该 Session，音乐/地点/声明方法均不得调用；下一条仍按既有连续失败策略处理。

- [ ] **Step 4: 运行关闭屏障和执行器测试确认失败**

Run: `QT_QPA_PLATFORM=offscreen python -m unittest test_douyin_commerce_batch_executor test_douyin_commerce_service.DouyinCommerceBatchUiTests test_main_window -v`

Expected: FAIL，当前 UI 没有代际关闭屏障，执行器也未调用正式页基线清理。

- [ ] **Step 5: 实现统一关闭和屏障包装器**

`_close_setup_generation()` 在没有活动代际时返回已关闭零计数；有活动代际时调用协调器并只在 `closed is True and aliveCollectorCount == 0` 时清空 ID。`_run_after_collector_barrier()` 的固定逻辑为：

```python
result = self._close_setup_generation(reason)
if result.get("closed") is not True or int(result.get("aliveCollectorCount") or 0) != 0:
    raise RuntimeError("平台设置临时会话未完全关闭，已安全停止")
return operation()
```

预检和发布的后台 callable 必须先执行该包装器，再调用批量执行器；不要在 Qt 主线程等待浏览器关闭。

- [ ] **Step 6: 统一放弃、登录、完成和客户端退出路径**

`_abandon_session()` 不再只看 `_session_id`；即使采集器尚在启动也要调用关闭。清理后复用 `_reset_platform_settings_after_abandon()`，保留账号、视频、标题、文案和标签，清空音乐、地点、范围搜索、声明、逐条排期覆盖及批量发布方式。

`_batch_publish_succeeded()` 在任务汇总和任务明细已保存后调用 `_close_setup_generation("publish_completed")` 并清空当前批次平台选择。`shutdown()` 不弹窗，只尽力关闭当前代际和旧正式 Session。`MainWindow.closeEvent()` 首先调用 `self.douyin_commerce.shutdown()`。

- [ ] **Step 7: 在正式执行器中加入基线清理调用**

`_run_item()` 取得 `session_id` 后立即调用：

```python
baseline = self._manager.prepare_publish_settings(session_id)
if not isinstance(baseline, Mapping) or baseline.get("status") != "clean":
    raise DouyinCommerceBatchExecutorError("抖音正式发布页未取得干净设置基线")
```

之后才选择缓存音乐、声明、搜索并应用地点、同步定时和预检。保留 `finally` 每条关闭逻辑、验证暂停、回执待核对和连续失败自动暂停规则。

- [ ] **Step 8: 运行关闭和正式执行回归确认通过**

Run: `QT_QPA_PLATFORM=offscreen python -m unittest test_douyin_commerce_batch_executor test_douyin_commerce_service.DouyinCommerceBatchUiTests test_main_window -v`

Expected: PASS，所有结束路径关闭代际，关闭不完整阻止执行，每条正式 Session 唯一且先清理后写入。

- [ ] **Step 9: 提交生命周期屏障**

```bash
git add ui/douyin_commerce_page.py ui/main_window.py app_core/douyin_commerce_batch_executor.py test_douyin_commerce_service.py test_douyin_commerce_batch_executor.py test_main_window.py
git commit -m "增加抖音采集会话关闭屏障"
```

### Task 7: 结构化日志、客户端诊断摘要与失败归因

**Files:**
- Modify: `app_core/douyin_commerce_collectors.py`
- Modify: `ui/douyin_commerce_page.py`
- Modify: `test_douyin_commerce_collectors.py`
- Modify: `test_douyin_commerce_service.py`

**Interfaces:**
- Consumes: Task 1 的 `CollectorDiagnosticEvent`。
- Produces: `DouyinCommerceCollectorManager.recent_diagnostics(generation_id: str, limit: int = 50) -> list[dict[str, object]]`。
- Produces: `DouyinCommercePage._copy_collector_diagnostics() -> None`。
- Produces: 错误码到中文短说明的固定映射 `COLLECTOR_ERROR_COPY`。

- [ ] **Step 1: 写每次动作字段完整和日志脱敏失败测试**

对成功国内搜索、音乐刷新失败、本地重试、旧结果丢弃和关闭不完整分别断言事件包含：`timestamp/requestId/setupGenerationId/collectorType/collectorInstanceId/accountMaskedId/phase/action/scope/keyword/attempt/candidateCount/durationMs/outcome/errorCode/cleanupResult`。

向底层异常传入包含 `cookiesFile/account.json`、验证码数字和长 DOM 文本的消息，断言 `recent_diagnostics()` 和本机日志安全摘要里都不存在；错误只能映射到固定 `errorCode`。

- [ ] **Step 2: 写客户端复制摘要失败测试**

patch `recent_diagnostics()` 返回两条公开事件，调用 `_copy_collector_diagnostics()`，断言剪贴板文本包含代际短编号、采集器、关键词、候选数、耗时、错误码和清理结果，不包含 `sessionId`、账号文件、Cookie 或 DOM。

- [ ] **Step 3: 运行诊断测试确认接口尚不完整**

Run: `QT_QPA_PLATFORM=offscreen python -m unittest test_douyin_commerce_collectors test_douyin_commerce_service.DouyinCommerceUiTests -v`

Expected: FAIL，缺少诊断环形缓冲区或复制入口。

- [ ] **Step 4: 实现固定错误分类和有限诊断缓冲区**

协调器只保留当前代际最近 200 条 `CollectorDiagnosticEvent.to_public_dict()`，新代际创建时清空内存缓冲区。异常按以下固定优先级分类：登录语义→`login_required`，范围不唯一→`scope_not_confirmed`，候选面板缺失→`candidate_panel_missing`，多面板/多目标→`candidate_ambiguous`，空候选→`candidate_empty`，频控/服务降级→`rate_limited_or_degraded`，关闭失败→`cleanup_incomplete`，其余→`collector_unknown`。本机开发日志也只能写经过 `_safe_diagnostic_detail()` 处理的 180 字摘要：账号文件路径替换为 `<account-state>`，连续 4 位以上数字替换为 `<redacted-number>`，`cookie/token/html/dom` 后的内容替换为 `<redacted>`；原异常全文不得写入日志或 UI。

- [ ] **Step 5: 实现 UI 诊断摘要和复制按钮**

在三个状态标签旁增加 `copy_collector_diagnostics_button`，文本为“复制诊断摘要”。按事件时间输出每行：

```text
13:42:10 | 批次 8f31c9 | 本地点 | search_locations | 关键词=夜南香 | 候选=0 | 1200ms | candidate_panel_missing | cleanup=closed
```

没有事件时复制“当前批次暂无采集诊断”。复制完成按钮短暂显示“诊断已复制”，不弹出阻塞对话框。

- [ ] **Step 6: 运行诊断测试确认通过**

Run: `QT_QPA_PLATFORM=offscreen python -m unittest test_douyin_commerce_collectors test_douyin_commerce_service.DouyinCommerceUiTests -v`

Expected: PASS，成功和失败动作可定位，公开摘要默认脱敏，关闭结果可见。

- [ ] **Step 7: 提交诊断能力**

```bash
git add app_core/douyin_commerce_collectors.py ui/douyin_commerce_page.py test_douyin_commerce_collectors.py test_douyin_commerce_service.py
git commit -m "增加抖音采集会话诊断日志"
```

### Task 8: 自动回归、两代际实机预提交验证器与文档

**Files:**
- Create: `tools/verify_douyin_commerce_collectors.py`
- Modify: `test_douyin_commerce_collectors.py`
- Modify: `docs/DOUYIN_COMMERCE_WORKFLOW.md`

**Interfaces:**
- Produces: `build_verification_sequences(keyword: str, domestic_count: int = 20) -> list[dict[str, object]]`。
- Produces: CLI 参数 `--account-id <int>`、`--keyword <str>`、`--execute`、`--output <path>`。
- Default: 不传 `--execute` 时只打印测试序列，不读取账号状态、不启动浏览器。
- Safety: CLI 不导入批量执行器，不调用 `preflight()`、`submit()` 或任务成功写入器。

- [ ] **Step 1: 写验证器默认零平台动作和序列完整性失败测试**

```python
def test_verifier_default_mode_only_prints_plan(self):
    with patch("tools.verify_douyin_commerce_collectors.commerce_collector_manager") as manager:
        result = main(["--account-id", "31", "--keyword", "夜南香"])
    self.assertEqual(result, 0)
    manager.begin_generation.assert_not_called()

def test_verification_sequences_cover_two_generations_and_three_orders(self):
    sequences = build_verification_sequences("夜南香", domestic_count=20)
    self.assertEqual({row["order"] for row in sequences}, {
        "music-domestic-local",
        "domestic-music-local",
        "local-domestic-music",
    })
    self.assertEqual(
        [row["domesticSearchCount"] for row in sequences],
        [20, 20, 1],
    )
    self.assertEqual({row["ending"] for row in sequences}, {"normal", "abandon"})
```

再用 fake 协调器执行 `--execute`，断言每个代际结束都调用 `close_generation()`，报告的 `finalSubmitCount`、`draftSaveCount`、`publicPublishCount` 均为 0。

- [ ] **Step 2: 运行验证器测试确认模块尚不存在**

Run: `python -m unittest test_douyin_commerce_collectors -v`

Expected: FAIL，提示验证器模块或序列函数不存在。

- [ ] **Step 3: 实现只读验证序列和脱敏 JSON 报告**

CLI 通过 `account_service.list_accounts()` 精确取得 `type == 3`、`id == account_id`、`status == 1` 的一条记录，只把其 `filePath` 放入探针源 payload。执行顺序固定包含：

1. 代际 A：音乐→20 次国内→2 次本地→正常关闭；
2. 代际 B：国内→音乐→本地→关闭并按“放弃”清理；
3. 代际 C：本地→国内→音乐，用于第三种顺序；完成后关闭。

为避免高频风险，动作之间使用协调器既有串行锁，并在 CLI 层设置至少 800 ms 间隔；任何 `login_required`、`rate_limited_or_degraded` 或账号验证立即停止后续序列。报告只保存公开诊断、各代际实例 ID 的哈希短值、候选数量、关闭结果和零提交计数；不保存账号文件名、Cookie、页面文本或完整 Session ID。

- [ ] **Step 4: 更新工作流文档**

在 `docs/DOUYIN_COMMERCE_WORKFLOW.md` 增加“平台设置隔离采集”章节，明确三采集器启动时机、代际关闭屏障、本地选择与平台回读的状态差异、单采集器重试、放弃后清理和验证器的零提交边界。删除任何“地点需要先刷新音乐”的旧说明。

- [ ] **Step 5: 运行专项与完整自动回归**

Run: `python -m unittest test_douyin_commerce_setup_state test_douyin_commerce_probe test_douyin_commerce_collectors test_douyin_commerce_batch_executor test_douyin_commerce_service test_macos_build test_windows_build -v`

Expected: PASS。

Run: `python -m unittest discover -v`

Expected: PASS，现有完整测试无回归。

Run: `python -m py_compile app_core/douyin_commerce_setup_state.py app_core/douyin_commerce_probe.py app_core/douyin_commerce_collectors.py app_core/douyin_commerce_session.py app_core/douyin_commerce_batch_executor.py ui/douyin_commerce_page.py tools/verify_douyin_commerce_collectors.py`

Expected: 退出码 0。

Run: `QT_QPA_PLATFORM=offscreen python desktop_native_app.py --ui-test`

Expected: 输出 `NATIVE_DESKTOP_UI_OK`。

Run: `git diff --check`

Expected: 退出码 0。

- [ ] **Step 6: 提交验证器和文档**

```bash
git add tools/verify_douyin_commerce_collectors.py test_douyin_commerce_collectors.py docs/DOUYIN_COMMERCE_WORKFLOW.md
git commit -m "增加抖音隔离采集实机验证器"
```

- [ ] **Step 7: 停止并请求实机预提交授权**

向 Andy 报告自动测试、提交列表和当前客户端状态，明确说明下一步会打开三个临时抖音编辑会话、上传内置探针、执行候选读取并放弃，但不会进入预检或最终提交。只有收到本次明确确认后才运行：

```bash
python tools/verify_douyin_commerce_collectors.py --account-id <已确认账号ID> --keyword 夜南香 --execute --output outputs/douyin-commerce-collector-verification.json
```

不从旧的“同意测试”“设计通过”或本计划执行授权推断实机授权。

- [ ] **Step 8: 在授权后执行两代际实机预提交并核对零污染门**

执行后必须核对报告：旧 Session 复用数 0、迟到结果写入数 0、代际结束存活采集器数 0、国内/本地串页数 0、最终提交数 0、草稿保存数 0、公开发布数 0。出现验证码、账号验证、频控或平台能力缺失时立即停止并保存已完成部分，不扩大请求。

- [ ] **Step 9: 更新项目记忆并提交最终文档修订**

把实际自动测试数量、实机报告路径、是否触发账号验证、三个零提交计数和仍存风险写入代码迁移优化项目记忆及新会话摘要；若实机暴露设计内遗漏，只提交针对该遗漏的测试和修复，不把实机“曾返回候选”写成正式发布成功。

```bash
git add docs/DOUYIN_COMMERCE_WORKFLOW.md
git commit -m "记录抖音隔离采集验证结果"
```

## 最终完成证据

1. `git log` 中至少包含状态机、探针、采集器、会话解耦、UI 迁移、关闭屏障、诊断和验证器八个可独立审查的提交。
2. 完整 `unittest discover`、`py_compile`、离屏 UI 自检和 `git diff --check` 全部通过。
3. 同一客户端连续完成正常结束与放弃重来两个代际，且三种功能顺序都经过验证。
4. 旧 Session 复用、迟到结果写入、结束后存活采集器、国内/本地串页均为 0。
5. 探针最终提交、草稿保存和公开发布均为 0。
6. 正式发布成功仍只接受逐视频平台回执；本计划不会把探针、缓存、预检或本地选择写成发布证据。
