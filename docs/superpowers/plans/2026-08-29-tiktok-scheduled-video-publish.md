# TikTok Platform-Native Scheduled Video Publish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有 TikTok 单账号、单视频受控发布主链中增加只接受北京时间的 TikTok Studio 平台原生定时发布，并以平台排期回读而不是本地等待作为成功证据。

**Architecture:** 受控请求层继续使用唯一根级 `schedule` 合同，把排期加入快照、指纹和一次性授权；TikTok 专用合同层统一执行 30 分钟创建门与 15 分钟最终提交门；独立 `schedule_form` 模块只负责当前 TikTok Studio 定时控件的识别、填写和回读。立即与定时共用账号核对、上传、正文、官方话题、公开范围、人工验证和结果不明保护，最终动作按请求唯一解析为 `Post` 或 `Schedule`，绝无隐式降级。

**Tech Stack:** Python 3、`zoneinfo`、PyQt6、SQLite、Playwright、现有本机受控发布 CLI、`unittest`、Archify。

**Spec:** `docs/superpowers/specs/2026-08-29-tiktok-scheduled-video-publish-design.md`

## Global Constraints

- `schedule: null` 保持立即公开；非空排期只接受 `YYYY-MM-DD HH:mm` 和 `Asia/Shanghai`。
- 创建任务时排期至少提前 30 分钟、至多 10 天；点击最终按钮前必须仍至少提前 15 分钟、至多 10 天。
- TikTok 定时入口缺失、歧义、不可交互、时间回读不一致或最终按钮仍为 `Post` 时，必须在最终动作前失败；禁止改成立即发布。
- `platform_form_check` 会上传并填写真实页面，但必须停在 `Schedule` 前；普通 `preflight` 仍是零浏览器、零上传、零会话改写。
- 正式模式只允许点击一次 `Schedule`。最终动作后的超时、中断或页面异常进入 `ambiguous`，并阻止同一发布指纹自动重试。
- `scheduled_readback_confirmed` 只代表平台排期成功；到点前 `publishedAt` 必须为 `null`，不能写成已经公开。
- Cookie、密码、验证码、二维码内容和会话原文不得进入请求、回执、Git、构建包或长期日志。
- 功能分支不升级版本号、不制作正式安装包、不自行合并到 `main`。
- `app_core/controlled_publish.py` 属于共享核心，必须形成独立提交并留给集成线审查。
- 真实平台表单检查和正式排期都需要大帅在临界点另行允许；本计划的自动执行默认只到离线实现与验证。

---

## File Structure

### New files

- `app_core/tiktok_schedule_contract.py` — 共享纯时间合同：解析派生字段、北京时间归一化、30/15 分钟门和 10 天上限；不依赖海外实现、浏览器或数据库。
- `uploader/tk_uploader/schedule_form.py` — TikTok Studio 定时控件、最终动作和定时受理/列表回读适配器；不决定授权。
- `test_tiktok_schedule_contract.py` — 共享纯时间合同的边界、格式和时区测试。
- `test_overseas_tiktok_schedule_form.py` — 定时表单假页面、无入口、歧义、回读和无降级测试。
- `docs/verification/2026-08-29-tiktok-scheduled-video-publish.md` — 离线证据以及后续经授权真实表单/正式排期的分层回执。

### Modified files

- `docs/architecture/tiktok-controlled-video-publish.architecture.json/html` — 增加独立定时表单组件、北京时间边界和定时回读链。
- `docs/architecture/tiktok-controlled-video-publish.lifecycle.json/html` — 增加 `scheduled_accepted -> scheduled_readback_confirmed`，并修正表单核对先于最终动作。
- `app_core/controlled_publish.py` — TikTok 根级排期、创建时间门、派生负载、正式意图、指纹和任务投影。
- `test_controlled_publish.py` — 根级合同、创建时间门、授权失效、回执投影和共享回归。
- `QUALITY_GATES.md` — 把 `app_core/controlled_publish.py` 明确登记为共享核心。
- `tools/check_workstream_scope.py` / `test_workstream_scope.py` — 让范围检查与已批准的共享边界一致。
- `app_core/overseas_tiktok_publish.py` — TikTok 运行负载校验、排期快照、最终时间门、受理/回读状态和稳定错误码。
- `app_core/overseas_tiktok_errors.py` — 允许安全排期字段和定时阶段进入公开回执，不放宽敏感字段边界。
- `test_overseas_tiktok_publish.py` — 本地预检、表单检查、正式排期、结果不明和中断收口。
- `uploader/tk_uploader/main.py` — 在现有同页编排中接入定时表单，泛化唯一最终动作，不复制上传/正文/话题流程。
- `test_overseas_video_publish.py` — 上传器表单快照、最终动作和即时兼容测试。
- `app_core/publish_service.py` — 只在现有 TikTok 路由需要映射定时成功文案或稳定终态时做最小调整。
- `app_core/task_service.py` / `test_task_service.py` — 安全投影定时回执，并在最终动作后失联时收口为结果不明。
- `test_publish_service.py` — 证明薄编排层不会丢失排期回执；只有测试暴露缺口时才改 `publish_service.py`。
- `test_overseas_publish_routing.py` — TikTok 即时/定时仍路由到同一专用服务。
- `SOURCE_OF_TRUTH.md` — 只记录真正完成的本地或平台证据，不提前写“定时发布可用”。

---

### Task 0: Seal the Existing TikTok Baseline Before Schedule Work

**Files already modified before this plan:**
- `SOURCE_OF_TRUTH.md`
- `app_core/overseas_tiktok_identity.py`
- `app_core/overseas_tiktok_profile.py`
- `app_core/overseas_tiktok_publish.py`
- `app_core/overseas_tiktok_system_login.py`
- `test_account_detection_ui.py`
- `test_overseas_tiktok_identity.py`
- `test_overseas_tiktok_profile.py`
- `test_overseas_tiktok_publish.py`
- `test_overseas_tiktok_system_login.py`
- `test_overseas_video_publish.py`
- `ui/login_dialog.py`
- `uploader/tk_uploader/main.py`

**Purpose:** The current worktree contains about 2,000 lines of already-tested TikTok identity, public-profile, slow-network, topic-entity, and form-stage changes that overlap the schedule files. Preserve them in one reviewed baseline commit before any schedule edit; never reset or overwrite them.

- [ ] **Step 1: Confirm the pre-existing diff is still exactly bounded**

Run:

```bash
git status --short
git diff --stat
git diff --check
```

Expected before the baseline commit: only the files listed above are dirty apart from the already-committed schedule spec and plan. Stop if a new unrelated file appears.

- [ ] **Step 2: Rerun the existing TikTok baseline suites**

Run:

```bash
.venv/bin/python -m unittest -v \
  test_account_detection_ui \
  test_overseas_tiktok_identity \
  test_overseas_tiktok_profile \
  test_overseas_tiktok_system_login \
  test_overseas_tiktok_publish \
  test_overseas_video_publish
```

Plan-author evidence on 2026-08-29: `244/244` passed in `1.454s` (command wall time `1.547s`). Execution must rerun and record its own exact count; any failure blocks the baseline commit.

- [ ] **Step 3: Commit the verified overseas-owned baseline**

```bash
git add app_core/overseas_tiktok_identity.py app_core/overseas_tiktok_profile.py app_core/overseas_tiktok_publish.py app_core/overseas_tiktok_system_login.py test_overseas_tiktok_identity.py test_overseas_tiktok_profile.py test_overseas_tiktok_publish.py test_overseas_tiktok_system_login.py test_overseas_video_publish.py uploader/tk_uploader/main.py
git commit -m "feat(tiktok): harden identity and slow-page form checks"
```

Commit the already-existing shared UI/status changes separately for integration review:

```bash
git add SOURCE_OF_TRUTH.md test_account_detection_ui.py ui/login_dialog.py
git commit -m "feat(accounts): surface hardened TikTok login status"
```

If these exact changes were already committed by their original responsibility line before execution begins, do not create empty or duplicate commits; instead verify the listed tests and continue from that clean baseline. Neither shared commit may be merged directly from the feature line.

- [ ] **Step 4: Correct shared-scope governance before adding schedule modules**

Add failing `test_workstream_scope.py` assertions that `app_core/tiktok_schedule_contract.py`, `app_core/controlled_publish.py`, `app_core/task_service.py`, `test_tiktok_schedule_contract.py`, `test_account_detection_ui.py`, `test_controlled_publish.py`, `test_task_service.py`, and `test_publish_service.py` classify as `shared`. Add those paths to `SHARED_EXACT` and list the production modules under shared core in `QUALITY_GATES.md`.

Add a separate `test_overseas_stream_owns_tiktok_architecture_plan_and_verification_docs` assertion for these owned paths: the two `docs/architecture/tiktok-controlled-video-publish.*` pairs, this `docs/superpowers/specs/...tiktok...` spec, this `docs/superpowers/plans/...tiktok...` plan, and `docs/verification/2026-08-29-tiktok-scheduled-video-publish.md`. Extend `is_overseas_owned` only for `docs/` paths whose filename contains `tiktok` and whose parent is one of `architecture`, `verification`, `superpowers/specs`, or `superpowers/plans`; do not classify unrelated docs as overseas.

Run:

```bash
.venv/bin/python -m unittest -v test_workstream_scope
.venv/bin/python tools/check_workstream_scope.py --stream overseas --base origin/main
```

Expected: unit tests pass; the scope command exits `2` and labels the shared paths as `shared`, never `outside`.

```bash
git add QUALITY_GATES.md tools/check_workstream_scope.py test_workstream_scope.py
git commit -m "chore(scope): classify controlled TikTok core as shared"
```

This governance commit is shared and integration-review-only.

---

### Task 1: Update the TikTok Architecture and Lifecycle Evidence

**Files:**
- Modify: `docs/architecture/tiktok-controlled-video-publish.architecture.json`
- Regenerate: `docs/architecture/tiktok-controlled-video-publish.architecture.html`
- Modify: `docs/architecture/tiktok-controlled-video-publish.lifecycle.json`
- Regenerate: `docs/architecture/tiktok-controlled-video-publish.lifecycle.html`

**Interfaces:**
- Documents: `controlled_publish -> publish_service -> overseas_tiktok_publish -> TiktokVideo -> tiktok_schedule_form -> TikTok Studio`.
- Documents: immediate branch `platform_accepted -> published_readback_confirmed`.
- Documents: scheduled branch `scheduled_accepted -> scheduled_readback_confirmed`, with `publishedAt=null`.
- Documents: `tiktok_schedule_unavailable` has no transition to immediate `Post`.

- [ ] **Step 1: Read only the Archify schemas and examples required for the two existing diagram types**

Run:

```bash
sed -n '1,240p' /Users/andy/.codex/skills/archify/schemas/architecture.schema.json
sed -n '1,240p' /Users/andy/.codex/skills/archify/schemas/lifecycle.schema.json
sed -n '1,240p' /Users/andy/.codex/skills/archify/schemas/common.schema.json
ls /Users/andy/.codex/skills/archify/examples
```

Read one architecture example and one lifecycle example selected from that listing. Do not inspect Archify renderer internals.

- [ ] **Step 2: Edit the architecture candidate before any renderer inspection**

Replace the current state-like `local_preflight_result` component with a real component:

```json
{
  "id": "tiktok_schedule_form",
  "type": "backend",
  "label": "TikTok Schedule Form",
  "sublabel": "北京时间开关、日期时间、唯一 Schedule 与排期回读",
  "tag": "no fallback to Post",
  "sources": [
    {"path": "docs/superpowers/specs/2026-08-29-tiktok-scheduled-video-publish-design.md", "line": 125, "label": "已批准的平台原生定时模块边界"},
    {"path": "uploader/tk_uploader/main.py", "line": 610, "label": "现有表单快照与最终动作接线点"}
  ]
}
```

Update the main path to:

```text
controlled_publish -> publish_service -> overseas_tiktok_publish
-> TiktokVideo -> TikTok Schedule Form -> TikTok Studio
```

Keep the diagram at no more than 12 primary components. Add no edge from schedule failure to immediate publish.

- [ ] **Step 3: Edit the lifecycle candidate**

Make the actual form order explicit:

```text
local_preflight_passed
  -> platform_page_active
  -> platform_form_verified
  -> final_action_triggered
```

Then split the final branch:

```text
final_action_triggered -> platform_accepted -> published_readback_confirmed
final_action_triggered -> scheduled_accepted -> scheduled_readback_confirmed
final_action_triggered -> ambiguous -> duplicate_blocked
```

Label the final action “恰好一次 Post 或 Schedule”. Route all schedule control failures before the final action to `failed_before_final_action`.

- [ ] **Step 4: Validate and deliver both diagrams**

Run:

```bash
node /Users/andy/.codex/skills/archify/bin/archify.mjs validate architecture docs/architecture/tiktok-controlled-video-publish.architecture.json --quality showcase --json
node /Users/andy/.codex/skills/archify/bin/archify.mjs deliver architecture docs/architecture/tiktok-controlled-video-publish.architecture.json docs/architecture/tiktok-controlled-video-publish.architecture.html --quality showcase --json
node /Users/andy/.codex/skills/archify/bin/archify.mjs validate lifecycle docs/architecture/tiktok-controlled-video-publish.lifecycle.json --quality showcase --json
node /Users/andy/.codex/skills/archify/bin/archify.mjs deliver lifecycle docs/architecture/tiktok-controlled-video-publish.lifecycle.json docs/architecture/tiktok-controlled-video-publish.lifecycle.html --quality showcase --json
```

Expected: each validation reports all 9 artifact checks, 0 composition errors and 0 warnings; each delivery exits 0.

- [ ] **Step 5: Collect and inspect bounded desktop evidence**

Run:

```bash
node /Users/andy/.codex/skills/archify/bin/archify.mjs visual-check docs/architecture/tiktok-controlled-video-publish.architecture.html --json
node /Users/andy/.codex/skills/archify/bin/archify.mjs visual-check docs/architecture/tiktok-controlled-video-publish.lifecycle.html --json
```

Open the two generated contact sheets and verify no clipped text, crossing unrelated nodes, ambiguous edge or large empty lower band. Do not edit the HTML after `deliver`; if repair is needed, edit JSON and rerun validate/deliver.

- [ ] **Step 6: Commit architecture evidence only**

```bash
git add docs/architecture/tiktok-controlled-video-publish.architecture.json docs/architecture/tiktok-controlled-video-publish.architecture.html docs/architecture/tiktok-controlled-video-publish.lifecycle.json docs/architecture/tiktok-controlled-video-publish.lifecycle.html
git commit -m "docs(tiktok): map native scheduled publish"
```

---

### Task 2: Add the Shared Pure Beijing-Time Schedule Contract

**Files:**
- Create: `app_core/tiktok_schedule_contract.py`
- Create: `test_tiktok_schedule_contract.py`

**Interfaces:**
- Produces: `TikTokScheduleIntent(mode: str, local_time: str | None, timezone: str, scheduled_at: datetime | None)`.
- Produces: `TikTokScheduleContractError(error_code: str, public_message: str)`.
- Produces: `parse_tiktok_schedule_fields(*, enable_timer: object, schedule_time: object, schedule_timezone: object, daily_times: object) -> TikTokScheduleIntent`.
- Produces: `validate_tiktok_schedule_window(intent: TikTokScheduleIntent, *, now: datetime, minimum_lead: timedelta) -> None`.
- Depends on: Python `datetime`, `timedelta`, and `zoneinfo.ZoneInfo`; no app service imports.

- [ ] **Step 1: Write the failing pure contract tests**

```python
import os
import time
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app_core.tiktok_schedule_contract import (
    TikTokScheduleContractError,
    parse_tiktok_schedule_fields,
    validate_tiktok_schedule_window,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


class TikTokScheduleContractTests(unittest.TestCase):
    def test_immediate_and_scheduled_are_exactly_normalized(self):
        immediate = parse_tiktok_schedule_fields(
            enable_timer=False,
            schedule_time=None,
            schedule_timezone="Asia/Shanghai",
            daily_times=[],
        )
        self.assertEqual(immediate.mode, "immediate")
        self.assertIsNone(immediate.scheduled_at)

        scheduled = parse_tiktok_schedule_fields(
            enable_timer=True,
            schedule_time="2026-08-29 15:00",
            schedule_timezone="Asia/Shanghai",
            daily_times=["15:00"],
        )
        self.assertEqual(scheduled.mode, "platform_native")
        self.assertEqual(scheduled.scheduled_at.tzinfo, SHANGHAI)

    def test_creation_window_is_thirty_minutes_to_ten_days(self):
        now = datetime(2026, 8, 29, 14, 0, tzinfo=SHANGHAI)
        accepted = parse_tiktok_schedule_fields(
            enable_timer=True,
            schedule_time="2026-08-29 14:30",
            schedule_timezone="Asia/Shanghai",
            daily_times=["14:30"],
        )
        validate_tiktok_schedule_window(
            accepted,
            now=now,
            minimum_lead=timedelta(minutes=30),
        )

    def test_conflicting_or_wrong_timezone_fields_are_rejected(self):
        cases = (
            (True, "2026-08-29 15:00", "UTC", ["15:00"]),
            (True, "2026-08-29 15:00", "Asia/Shanghai", []),
            (False, "2026-08-29 15:00", "Asia/Shanghai", ["15:00"]),
        )
        for values in cases:
            with self.subTest(values=values), self.assertRaises(TikTokScheduleContractError):
                parse_tiktok_schedule_fields(
                    enable_timer=values[0],
                    schedule_time=values[1],
                    schedule_timezone=values[2],
                    daily_times=values[3],
                )
```

Add boundary tests for 29 minutes, exactly 30 minutes, exactly 10 days, 10 days plus 1 minute, naïve `now`, invalid date, duplicate daily times, and non-boolean `enable_timer`. Add `test_system_timezone_cannot_change_shanghai_result`: evaluate the same intent while the process `TZ` is `UTC` and `America/Los_Angeles`, and assert the normalized aware datetime and window result are identical.

- [ ] **Step 2: Run the focused test and confirm the missing-module failure**

Run: `.venv/bin/python -m unittest -v test_tiktok_schedule_contract`

Expected: FAIL with `ModuleNotFoundError: app_core.tiktok_schedule_contract`.

- [ ] **Step 3: Implement the minimal immutable schedule contract**

```python
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

SHANGHAI_NAME = "Asia/Shanghai"
SHANGHAI = ZoneInfo(SHANGHAI_NAME)
MAXIMUM_LEAD = timedelta(days=10)


@dataclass(frozen=True, slots=True)
class TikTokScheduleIntent:
    mode: str
    local_time: str | None
    timezone: str
    scheduled_at: datetime | None


class TikTokScheduleContractError(RuntimeError):
    def __init__(self, error_code: str, public_message: str) -> None:
        self.error_code = str(error_code)
        self.public_message = str(public_message)
        super().__init__(public_message)


def validate_tiktok_schedule_window(
    intent: TikTokScheduleIntent,
    *,
    now: datetime,
    minimum_lead: timedelta,
) -> None:
    if intent.mode == "immediate":
        return
    if now.tzinfo is None or intent.scheduled_at is None:
        raise TikTokScheduleContractError(
            "tiktok_schedule_invalid",
            "TikTok 排期和校验时钟必须包含北京时间",
        )
    delta = intent.scheduled_at - now.astimezone(SHANGHAI)
    if delta < minimum_lead or delta > MAXIMUM_LEAD:
        raise TikTokScheduleContractError(
            "tiktok_schedule_out_of_range",
            "TikTok 排期不在允许的时间窗口内",
        )


def parse_tiktok_schedule_fields(
    *,
    enable_timer: object,
    schedule_time: object,
    schedule_timezone: object,
    daily_times: object,
) -> TikTokScheduleIntent:
    if type(enable_timer) is not bool:
        raise TikTokScheduleContractError(
            "tiktok_schedule_invalid",
            "TikTok 排期开关类型无效",
        )
    if schedule_timezone != SHANGHAI_NAME or type(daily_times) is not list:
        raise TikTokScheduleContractError(
            "tiktok_schedule_invalid",
            "TikTok 排期时区或时间列表无效",
        )
    if not enable_timer:
        if schedule_time is not None or daily_times != []:
            raise TikTokScheduleContractError(
                "tiktok_schedule_invalid",
                "TikTok 立即发布与排期字段冲突",
            )
        return TikTokScheduleIntent("immediate", None, SHANGHAI_NAME, None)
    if type(schedule_time) is not str:
        raise TikTokScheduleContractError(
            "tiktok_schedule_invalid",
            "TikTok 排期时间格式无效",
        )
    try:
        parsed = datetime.strptime(schedule_time, "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise TikTokScheduleContractError(
            "tiktok_schedule_invalid",
            "TikTok 排期时间格式无效",
        ) from exc
    if parsed.strftime("%Y-%m-%d %H:%M") != schedule_time:
        raise TikTokScheduleContractError(
            "tiktok_schedule_invalid",
            "TikTok 排期时间格式无效",
        )
    local_time = schedule_time
    if daily_times != [local_time[-5:]]:
        raise TikTokScheduleContractError(
            "tiktok_schedule_invalid",
            "TikTok 排期时间列表与目标时间不一致",
        )
    return TikTokScheduleIntent(
        "platform_native",
        local_time,
        SHANGHAI_NAME,
        parsed.replace(tzinfo=SHANGHAI),
    )
```

Keep the parser exactly pure and fail-closed: no system timezone, app service, browser, database, aliases, coercion, or hidden defaults. In the system-timezone test, save `TZ`, set each test timezone, call `time.tzset()` when available, and restore both the environment and process timezone in `finally`.

- [ ] **Step 4: Run the focused test**

Run: `.venv/bin/python -m unittest -v test_tiktok_schedule_contract`

Expected: PASS.

- [ ] **Step 5: Commit the pure contract**

```bash
git add app_core/tiktok_schedule_contract.py test_tiktok_schedule_contract.py
git commit -m "feat(controlled): add TikTok schedule time contract"
```

This neutral module is shared core so that `controlled_publish.py` never imports an overseas-owned implementation. Keep this commit integration-review-only.

---

### Task 3: Extend the Shared Controlled Request and Authorization Contract

**Files:**
- Modify: `app_core/controlled_publish.py`
- Modify: `test_controlled_publish.py`

**Interfaces:**
- Consumes: `TikTokScheduleIntent`, `parse_tiktok_schedule_fields`, `validate_tiktok_schedule_window` from Task 2.
- Extends: `build_controlled_payloads(request, *, accounts=None, schedule_now: datetime | None = None) -> list[dict[str, Any]]`.
- Produces scheduled payload fields: `enableTimer=True`, `scheduleMode="platform_native"`, `scheduledAt`, `scheduleTime`, `scheduleTimezone="Asia/Shanghai"`, and `dailyTimes=[HH:mm]`.
- Preserves the existing intent state machine: formal immediate and formal scheduled requests both use `tiktokExecutionIntent="formal_public"`; the normalized `scheduleMode` distinguishes their publish action.

- [ ] **Step 1: Replace the old TikTok schedule-rejection tests with failing acceptance and boundary tests**

```python
from app_core import controlled_publish
from zoneinfo import ZoneInfo


def test_tiktok_target_accepts_one_beijing_platform_schedule(self):
    now = datetime(2026, 8, 29, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    with tempfile.TemporaryDirectory() as temporary:
        manifest = self._tiktok_bundle(Path(temporary))
        request = self._tiktok_request(manifest)
        request["targets"][0]["schedule"] = {
            "localTime": "2026-08-29 15:00",
            "timezone": "Asia/Shanghai",
        }
        payload = build_controlled_payloads(
            request,
            accounts=[self._tiktok_account()],
            schedule_now=now,
        )[0]
    self.assertTrue(payload["enableTimer"])
    self.assertEqual(payload["scheduleTime"], "2026-08-29 15:00")
    self.assertEqual(payload["dailyTimes"], ["15:00"])
    self.assertEqual(payload["scheduleMode"], "platform_native")
    self.assertEqual(payload["scheduledAt"], "2026-08-29 15:00")
    self.assertEqual(payload["tiktokExecutionIntent"], "formal_public")


def test_tiktok_schedule_change_invalidates_preflight_authorization_without_consuming_it(self):
    with tempfile.TemporaryDirectory() as temporary, patch.object(
        database,
        "DB_PATH",
        Path(temporary) / "database.db",
    ), patch.object(
        controlled_publish,
        "_shanghai_now",
        return_value=datetime(2026, 8, 29, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    ):
        database.ensure_schema()
        schedule = {"localTime": "2026-08-29 15:00", "timezone": "Asia/Shanghai"}
        manifest, preflight, grants = self._seed_tiktok_preflight(
            Path(temporary),
            authorization_count=1,
            target_schedule=schedule,
        )
        formal = self._tiktok_request(
            manifest,
            mode="formal",
            confirmedPreflightTaskId=preflight["id"],
            authorizationId=grants[0]["authorizationId"],
        )
        formal["targets"][0]["schedule"] = {
            "localTime": "2026-08-29 15:01",
            "timezone": "Asia/Shanghai",
        }
        with patch(
            "app_core.account_service.list_publishable_accounts",
            return_value=[self._tiktok_account()],
        ), self.assertRaises(ControlledPublishError) as raised:
            submit_request(formal)
        with database.connect() as conn:
            authorization = conn.execute(
                "SELECT consumedAt FROM controlled_publish_authorizations WHERE authorizationId = ?",
                (grants[0]["authorizationId"],),
            ).fetchone()
            formal_tasks = conn.execute(
                "SELECT COUNT(*) FROM publish_tasks WHERE mode = 'oneclick_publish'"
            ).fetchone()[0]
    self.assertEqual(raised.exception.error_code, "controlled_authorization_scope_mismatch")
    self.assertIsNone(authorization["consumedAt"])
    self.assertEqual(formal_tasks, 0)
```

Extend `_seed_tiktok_preflight(..., target_schedule: Mapping[str, str] | None = None)` so the preflight payload, successful local-preflight event, and authorization all bind the same normalized schedule. Add `_shanghai_now()` as the only production clock seam used when `schedule_now` is omitted. Add tests for 29 minutes, 10 days plus 1 minute, wrong timezone, extra schedule key, immediate compatibility, `project_task(task)["platforms"][0]["scheduledAt"]`, and `publishedAt` remaining absent/null for a scheduled receipt.

Add one table-driven alias test that rejects every forbidden user-input shape separately: sibling/root `scheduledAt`, sibling/root `publishAt`, nested target `scheduleTime`, nested target `scheduledAt`, nested target `publishAt`, runtime `enableTimer`, runtime `scheduleTime`, runtime `scheduleTimezone`, and runtime `dailyTimes`. Assert each fails with `tiktok_schedule_invalid` before task creation.

- [ ] **Step 2: Run the shared focused tests and confirm failure**

Run:

```bash
.venv/bin/python -m unittest -v \
  test_controlled_publish.ControlledPublishTests.test_tiktok_target_accepts_one_beijing_platform_schedule \
  test_controlled_publish.ControlledPublishTests.test_tiktok_schedule_change_invalidates_preflight_authorization_without_consuming_it
```

Expected: FAIL because TikTok targets with a schedule are still rejected and `schedule_now` is not accepted.

- [ ] **Step 3: Implement the minimal shared contract change**

Add `schedule_now` only as a deterministic test seam by changing the exact signature to `build_controlled_payloads(request: Mapping[str, Any], *, accounts: Iterable[Mapping[str, Any]] | None = None, schedule_now: datetime | None = None) -> list[dict[str, Any]]`. Production callers omit it; tests inject an aware Shanghai timestamp.

Keep generic `_schedule()` unchanged for every other platform. For TikTok, add `_tiktok_target_schedule(value, *, now)` that accepts only `None` or a mapping whose key set is exactly `{"localTime", "timezone"}`; `{}` is invalid. Translate all target-shape, format, and timezone failures to `ControlledPublishError("tiktok_schedule_invalid", "TikTok 排期字段、格式或时区无效")`, construct the Task 2 intent from the derived fields, and enforce the 30-minute creation gate. Remove both existing TikTok “只支持立即公开发布” branches. Set:

```python
payload["scheduleMode"] = schedule_intent.mode
payload["scheduledAt"] = schedule_intent.local_time
payload["tiktokExecutionIntent"] = (
    "platform_form_check" if mode == "platform_form_check" else "formal_public"
)
```

Add `scheduleMode` and `scheduledAt` to the existing fingerprint alongside `scheduleTime` and `scheduleTimezone`; do not hash session-file contents or Cookie values. Keep single-TikTok formal-claim detection on the existing `formal_public` intent and use `scheduleMode` for immediate/scheduled branching.

- [ ] **Step 4: Run the focused and full shared contract tests**

Run:

```bash
.venv/bin/python -m unittest -v test_controlled_publish
```

Expected: PASS.

- [ ] **Step 5: Rerun the full controlled contract after scope classification**

Run:

```bash
.venv/bin/python -m unittest -v test_controlled_publish test_workstream_scope
.venv/bin/python tools/check_workstream_scope.py --stream overseas --base origin/main
```

Expected: tests pass; scope checking still exits `2` because the feature branch contains deliberately isolated shared changes.

- [ ] **Step 6: Commit the shared contract by itself**

```bash
git add app_core/controlled_publish.py test_controlled_publish.py
git commit -m "feat(controlled): allow native TikTok schedules"
```

---

### Task 4: Add TikTok Local Schedule Validation and Snapshot Fields

**Files:**
- Modify: `app_core/overseas_tiktok_publish.py`
- Modify: `app_core/overseas_tiktok_errors.py`
- Modify: `test_overseas_tiktok_publish.py`

**Interfaces:**
- Consumes: Task 2 schedule contract.
- Extends: `validate_tiktok_payload(payload, *, mode: str, now: datetime | None = None) -> dict[str, Any]`.
- Extends: `run_tiktok_local_preflight(payload, *, now: datetime | None = None) -> dict[str, Any]`.
- Produces prepared fields: `scheduleMode`, `scheduledAt`, `scheduleTimezone`.
- Produces local receipt fields with `platformWriteOccurred=False`, `finalActionTriggered=False`, `publishedAt=None`.

- [ ] **Step 1: Write failing service-level schedule tests**

```python
from datetime import datetime
from zoneinfo import ZoneInfo


def test_scheduled_preflight_returns_schedule_snapshot_without_platform_write(self):
    now = datetime(2026, 8, 29, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    payload = self.payload(
        enableTimer=True,
        scheduleTime="2026-08-29 15:00",
        scheduleTimezone="Asia/Shanghai",
        dailyTimes=["15:00"],
    )
    prepared = self.validate(payload, mode="preflight", now=now)
    self.assertEqual(prepared["scheduleMode"], "platform_native")
    self.assertEqual(prepared["scheduledAt"], "2026-08-29 15:00")


def test_scheduled_local_preflight_never_loads_playwright(self):
    payload = self.payload(
        enableTimer=True,
        scheduleTime="2026-08-29 15:00",
        scheduleTimezone="Asia/Shanghai",
        dailyTimes=["15:00"],
    )
    with patch.object(
        overseas_tiktok_publish,
        "_load_async_playwright_factory",
        side_effect=AssertionError("local preflight must remain offline"),
    ):
        result = overseas_tiktok_publish.run_tiktok_local_preflight(
            payload,
            now=datetime(2026, 8, 29, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    self.assertFalse(result["receipt"]["platformWriteOccurred"])
    self.assertEqual(result["receipt"]["scheduledAt"], "2026-08-29 15:00")
    self.assertIsNone(result["receipt"]["publishedAt"])
```

Update the existing tests that require immediate defaults so they now accept exactly one internally consistent scheduled payload while continuing to reject nested aliases, multiple times, bad types and unsupported settings.

Extend the existing `TikTokPublishContractTests.validate` helper before adding these tests:

```python
def validate(
    self,
    payload: dict | None = None,
    *,
    mode: str = "preflight",
    now: datetime | None = None,
    account=_DEFAULT_ACCOUNT,
):
    # Preserve the existing account and COOKIE_DIR patches.
    return overseas_tiktok_publish.validate_tiktok_payload(
        payload or self.payload(),
        mode=mode,
        now=now,
    )
```

- [ ] **Step 2: Run focused tests and confirm the old rejection**

Run:

```bash
.venv/bin/python -m unittest -v \
  test_overseas_tiktok_publish.TikTokPublishContractTests
```

Expected: FAIL because `_validate_immediate_schedule` rejects scheduled fields.

- [ ] **Step 3: Replace immediate-only validation with the shared schedule contract**

Translate `TikTokScheduleContractError` without changing its stable code:

```python
try:
    schedule_intent = parse_tiktok_schedule_fields(
        enable_timer=payload.get("enableTimer"),
        schedule_time=payload.get("scheduleTime"),
        schedule_timezone=payload.get("scheduleTimezone"),
        daily_times=payload.get("dailyTimes"),
    )
except TikTokScheduleContractError as exc:
    _fail(exc.error_code, exc.public_message)
```

Retain strict root-only alias checks. The returned prepared snapshot must contain only normalized string/time fields, never a Cookie or storage-state value.

Apply the 30-minute minimum in `preflight`, where this layer can act as a direct local-contract entry. For `platform_form_check` and `formal`, require at least 15 minutes at platform-entry because the controlled request layer already enforced 30 minutes when the task was created; formal mode must still run the separate Step 4 check immediately before the click. Apply the 10-day maximum in every mode. Add a test proving a formal task with 20 minutes remaining is not incorrectly rejected by a second 30-minute gate.

- [ ] **Step 4: Add the final-action time gate helper**

```python
def _shanghai_now() -> datetime:
    return datetime.now(SHANGHAI)


def validate_tiktok_final_schedule_window(
    prepared: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> None:
    intent = _prepared_schedule_intent(prepared)
    validate_tiktok_schedule_window(
        intent,
        now=now or _shanghai_now(),
        minimum_lead=timedelta(minutes=15),
    )
```

Define the prepared-intent converter used above; it must never re-read raw request aliases:

```python
def _prepared_schedule_intent(prepared: Mapping[str, Any]) -> TikTokScheduleIntent:
    mode = prepared.get("scheduleMode")
    if mode == "immediate":
        return TikTokScheduleIntent("immediate", None, SHANGHAI_NAME, None)
    if (
        mode != "platform_native"
        or type(prepared.get("scheduledAt")) is not str
        or prepared.get("scheduleTimezone") != SHANGHAI_NAME
    ):
        _fail("tiktok_schedule_invalid", "TikTok 排期快照无效")
    return parse_tiktok_schedule_fields(
        enable_timer=True,
        schedule_time=prepared["scheduledAt"],
        schedule_timezone=prepared["scheduleTimezone"],
        daily_times=[prepared["scheduledAt"][-5:]],
    )
```

Immediate mode is a no-op. Scheduled mode returns `tiktok_schedule_out_of_range` before any final click when upload time has consumed the safety window.

Extend the neutral receipt sanitizer with exact validators for `scheduleMode`, `scheduledAt`, `scheduleTimezone`, `scheduleToggleEnabled`, `platformAccepted`, and `scheduledReadbackConfirmed`, plus the `scheduled_accepted` and `scheduled_readback_confirmed` phases. `scheduledAt` accepts only `YYYY-MM-DD HH:mm`; `scheduleTimezone` accepts only `Asia/Shanghai`; the three state flags accept only real booleans; no new arbitrary strings or nested objects may enter a public error receipt.

- [ ] **Step 5: Run the contract and existing platform-sync suites**

Run:

```bash
.venv/bin/python -m unittest -v \
  test_tiktok_schedule_contract \
  test_overseas_tiktok_publish.TikTokPublishContractTests \
  test_overseas_tiktok_publish.TikTokPlatformSyncTests
```

Expected: PASS, with all existing immediate tests unchanged except their former blanket schedule rejection assertions.

- [ ] **Step 6: Commit the TikTok service contract**

```bash
git add app_core/overseas_tiktok_publish.py app_core/overseas_tiktok_errors.py test_overseas_tiktok_publish.py
git commit -m "feat(tiktok): validate native schedule snapshots"
```

---

### Task 5: Build the Isolated TikTok Studio Schedule Form Adapter

**Files:**
- Create: `uploader/tk_uploader/schedule_form.py`
- Create: `test_overseas_tiktok_schedule_form.py`

**Interfaces:**
- Produces immutable `TikTokScheduleTarget`, `TikTokScheduleFormSnapshot`, `TikTokScheduleAcceptance`, `TikTokScheduledContentExpectation`, and `TikTokScheduledContentReadback` records.
- Produces: `TikTokScheduleForm(page, *, resolve_base, wait_for_manual_intervention, monotonic=None, now=None, sleep=None)`; the last three are deterministic test seams with safe production defaults.
- Produces methods: `configure(target)`, `verify(target)`, `final_button(target)`, `wait_for_acceptance(target)`, and `readback_scheduled_content(expected)`.
- Raises: `TikTokPublishError` using only the stable schedule error codes approved in the spec.

- [ ] **Step 1: Write failing semantic fake-page tests**

```python
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from app_core.overseas_tiktok_errors import TikTokPublishError
from uploader.tk_uploader.schedule_form import (
    TikTokScheduleForm,
    TikTokScheduleTarget,
    TikTokScheduledContentExpectation,
)


class TikTokScheduleFormTests(unittest.IsolatedAsyncioTestCase):
    async def test_configures_and_reads_exact_beijing_schedule(self):
        page, bases = scheduled_form_page(
            toggle_count=1,
            date_value="2026-08-29",
            time_value="15:00",
            final_button="Schedule",
        )
        form = TikTokScheduleForm(
            page,
            resolve_base=AsyncMock(side_effect=bases),
            wait_for_manual_intervention=AsyncMock(),
        )
        snapshot = await form.configure(
            TikTokScheduleTarget("2026-08-29 15:00", "Asia/Shanghai")
        )
        self.assertEqual(snapshot.scheduled_at, "2026-08-29 15:00")
        self.assertTrue(snapshot.toggle_enabled)
        self.assertEqual(snapshot.final_action_label, "Schedule")
        self.assertTrue(snapshot.final_action_ready)

    async def test_missing_schedule_control_never_falls_back_to_post(self):
        page, bases = scheduled_form_page(toggle_count=0, final_button="Post")
        form = TikTokScheduleForm(
            page,
            resolve_base=AsyncMock(side_effect=bases),
            wait_for_manual_intervention=AsyncMock(),
        )
        with self.assertRaises(TikTokPublishError) as raised:
            await form.configure(
                TikTokScheduleTarget("2026-08-29 15:00", "Asia/Shanghai")
            )
        self.assertEqual(raised.exception.error_code, "tiktok_schedule_unavailable")
```

Add tests for two visible toggles, disabled controls, date/time normalization mismatch, a page that rewrites the minute, final action still named `Post`, and exact Chinese/English `Schedule` labels. Add a remount test whose `resolve_base` returns a new base after the toggle, date and time writes; assert every public method reacquires the active base. Fakes must support current iframe/page scope behavior without using coordinates.

Define the test fixture locally instead of leaving an implicit helper. `FakeScheduleControl` stores `node_id`, semantic `role`, label, value, visible/enabled/editable/read_only flags, and click/fill counters; its async `click`, `fill`, `input_value`, `inner_text`, `get_attribute`, `is_visible`, `is_enabled`, and `is_editable` methods update/read those fields. `FakeScheduleBase.locator(selector)` returns a deduplicated locator over controls registered for that exact bounded selector. `FakeSchedulePage` stores the current route and bounded alert/status text. Define:

```python
def scheduled_form_page(
    *,
    toggle_count: int = 1,
    toggle_enabled: bool = False,
    date_value: str = "2026-08-29",
    time_value: str = "15:00",
    final_button: str = "Schedule",
    remount_after_writes: bool = False,
) -> tuple[FakeSchedulePage, list[FakeScheduleBase]]:
    """Return one fake page plus the exact base sequence consumed by resolve_base."""
```

The helper must register only the semantic selectors declared in the production adapter; it must not special-case production methods. When `remount_after_writes=True`, old controls become detached and the next base contains new node IDs with the last written values, proving each operation reacquires the active base.

Extend the fake page for the two post-click methods instead of mocking them away. It must support a bounded route, alert/status locators, `goto`, and scheduled-content rows. Define a deterministic fake clock whose `monotonic()` returns a float, `now()` returns an aware Shanghai datetime, and async `sleep(seconds)` advances both values without real waiting. Add these tests, all calling the real adapter methods:

- `test_wait_for_acceptance_requires_explicit_scheduled_signal`: an explicit scheduled-success alert returns one acceptance with the injected aware time.
- `test_wait_for_acceptance_rejection_and_timeout_are_terminal`: explicit rejection raises the stable rejection boundary; no evidence through the fake deadline raises `tiktok_schedule_outcome_unknown` with `outcome_ambiguous=True`.
- `test_readback_returns_only_one_exact_row`: one row with exact account reference, complete normalized caption SHA-256, exact Beijing time/timezone, and observation after `submitted_after` returns its optional ID/URL.
- `test_readback_zero_or_multiple_rows_is_not_success`: zero rows through the deadline and two exact rows both return `None`.
- `test_readback_rejects_each_mismatched_identity_field`: table-drive account reference, caption hash, scheduled time, timezone, and pre-submit observation mismatch; every case returns `None`.
- `test_readback_never_clicks_row_actions`: fake edit/publish/retry/delete controls raise if touched and remain untouched.

- [ ] **Step 2: Run the focused test and confirm the missing-module failure**

Run: `.venv/bin/python -m unittest -v test_overseas_tiktok_schedule_form`

Expected: FAIL with `ModuleNotFoundError: uploader.tk_uploader.schedule_form`.

- [ ] **Step 3: Implement semantic selectors and unique-control resolution**

Start `schedule_form.py` with explicit standard-library imports for `asyncio`, `hashlib`, `time`, `unicodedata`, `dataclass`, aware `datetime`, `ZoneInfo`, and the `typing` callables/literals used by the signatures, plus `TikTokPublishError` from the existing error boundary. Do not import the desktop UI, account database, controlled request service, or a browser launcher.

Define the immutable records exactly:

```python
@dataclass(frozen=True, slots=True)
class TikTokScheduleTarget:
    scheduled_at: str
    timezone: Literal["Asia/Shanghai"]


@dataclass(frozen=True, slots=True)
class TikTokScheduleFormSnapshot:
    schedule_mode: Literal["platform_native"]
    scheduled_at: str
    timezone: Literal["Asia/Shanghai"]
    toggle_enabled: bool
    final_action_label: Literal["Schedule"]
    final_action_ready: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "scheduleMode": self.schedule_mode,
            "scheduledAt": self.scheduled_at,
            "scheduleTimezone": self.timezone,
            "scheduleToggleEnabled": self.toggle_enabled,
            "finalActionLabel": self.final_action_label,
            "finalActionReady": self.final_action_ready,
        }


@dataclass(frozen=True, slots=True)
class TikTokScheduleAcceptance:
    evidence: str
    accepted_at: datetime


@dataclass(frozen=True, slots=True)
class TikTokScheduledContentExpectation:
    account_reference: str
    expected_caption: str
    caption_sha256: str
    target: TikTokScheduleTarget
    submitted_after: datetime


@dataclass(frozen=True, slots=True)
class TikTokScheduledContentReadback:
    content_id: str | None
    content_url: str | None
    scheduled_at: str
    schedule_timezone: Literal["Asia/Shanghai"]
    evidence: str
```

Start with bounded semantic selector groups:

```python
SCHEDULE_TOGGLE_SELECTORS = (
    '[data-e2e*="schedule" i] [role="switch"]',
    '[role="switch"][aria-label*="schedule" i]',
    '[role="switch"][aria-label*="定时"]',
)
SCHEDULE_DATE_SELECTORS = (
    '[data-e2e*="schedule" i] input[type="date"]',
    'input[aria-label*="date" i]',
    'input[aria-label*="日期"]',
)
SCHEDULE_TIME_SELECTORS = (
    '[data-e2e*="schedule" i] input[type="time"]',
    'input[aria-label*="time" i]',
    'input[aria-label*="时间"]',
)
SCHEDULED_CONTENT_ROUTES = (
    "https://www.tiktok.com/tiktokstudio/content",
    "https://www.tiktok.com/tiktokstudio/posts",
)
SCHEDULED_ROW_SELECTORS = (
    '[data-e2e="content-row"]',
    '[data-e2e="post-item"]',
    '[role="row"][data-schedule-time]',
)
```

The row adapter may read only bounded row fields: caption/title text, scheduled status/time text or attributes, account reference from the already-verified page context, and content anchor ID/URL. It may not enumerate arbitrary page JSON, cookies, local storage, or hidden credential fields. The fake rows expose exactly this contract.

Define label normalization in the module and test it directly:

```python
def _normalized_label(value: object) -> str:
    return " ".join(
        unicodedata.normalize("NFKC", str(value or "")).split()
    ).casefold()
```

Resolve exactly one control per semantic role. Date/time inputs must be visible, enabled, editable, and not read-only; switches and final-action buttons must be visible and enabled. If the real page uses a button/calendar instead of an input, add a separate semantic adapter proven by a fixture; do not weaken uniqueness.

Map failures exactly: no usable schedule entry to `tiktok_schedule_unavailable`; multiple matching entries, dates, times, or final buttons to `tiktok_schedule_control_ambiguous`; same-control value mismatch after writing to `tiktok_schedule_readback_mismatch`; no unique enabled `Schedule` at the final boundary to `tiktok_schedule_final_action_unavailable`. Add one assertion for every code to the fake-page suite.

- [ ] **Step 4: Implement exact write/readback and final-action resolution**

Use this exact constructor, with production-safe defaults and injectable test time:

```python
def __init__(
    self,
    page: Any,
    *,
    resolve_base: Callable[[], Awaitable[Any]],
    wait_for_manual_intervention: Callable[[Any], Awaitable[None]],
    monotonic: Callable[[], float] | None = None,
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> None:
    self._monotonic = monotonic or time.monotonic
    self._now = now or _shanghai_now
    self._sleep = sleep or asyncio.sleep
```

Define `_shanghai_now()` as `datetime.now(ZoneInfo("Asia/Shanghai"))`. Expose the five async methods listed under **Interfaces** with their record types and 120-second bounded defaults. Tests inject all three seams; production never changes them.

Add a bounded `FINAL_ACTION_SELECTORS` tuple for semantic button candidates (role/button plus exact English/Chinese labels) and implement `_visible_enabled_final_buttons(base)` locally in this module. That helper must query only those bounded selectors, deduplicate the same DOM node, read its visible label, and return only visible, enabled controls; do not scan every clickable element on the page.

Implement final-action resolution with this fail-closed shape:

```python
async def final_button(self, target: TikTokScheduleTarget):
    base = await self._resolve_base()
    candidates = await _visible_enabled_final_buttons(base)
    matches = [
        button
        for button, label in candidates
        if _normalized_label(label) in {"schedule", "定时发布", "排期"}
    ]
    immediate_matches = [
        button
        for button, label in candidates
        if _normalized_label(label) in {"post", "发布"}
    ]
    if not matches:
        raise TikTokPublishError(
            "tiktok_schedule_final_action_unavailable",
            "TikTok 定时请求没有可用的 Schedule 按钮",
        )
    if len(matches) > 1 or immediate_matches:
        raise TikTokPublishError(
            "tiktok_schedule_control_ambiguous",
            "TikTok 定时请求的最终动作不唯一",
        )
    return matches[0]
```

Each control step must call `await self._resolve_base()` again because enabling the toggle or choosing a date can remount the React subtree. `final_button` returns exactly one visible, enabled `Schedule` control and never clicks it. `readback_scheduled_content` returns a record only when account reference, complete-caption SHA-256, target Beijing time, and post-submit observation window identify exactly one row; zero or multiple matches return `None`.

Implement the five public methods with these exact signatures and stages:

```python
async def configure(self, target: TikTokScheduleTarget) -> TikTokScheduleFormSnapshot
async def verify(self, target: TikTokScheduleTarget) -> TikTokScheduleFormSnapshot
async def final_button(self, target: TikTokScheduleTarget) -> Any
async def wait_for_acceptance(self, target: TikTokScheduleTarget) -> TikTokScheduleAcceptance
async def readback_scheduled_content(
    self,
    expected: TikTokScheduledContentExpectation,
) -> TikTokScheduledContentReadback | None
```

`configure` resolves one visible/enabled switch, clicks only when its `aria-checked`/checked state is false, reacquires the base, and requires the state to be true. It then resolves and reads one date input, writes `YYYY-MM-DD`, reacquires and reads it back; repeats for the `HH:mm` time input; then returns `await verify(target)`. `verify` independently reacquires and checks switch=true, exact date, exact time, and the unique Schedule button, returning the full snapshot including `toggle_enabled=True`. Neither method clicks a final action.

`wait_for_acceptance` polls for at most 120 seconds using `self._monotonic`/`self._sleep`, calling the existing manual-intervention callback on every cycle. It returns only on a bounded explicit scheduled-success alert/status; arriving at a content route without an explicit scheduled state is not acceptance. An explicit rejection raises the stable platform-rejection boundary, and timeout raises `tiktok_schedule_outcome_unknown` with `outcome_ambiguous=True`.

`readback_scheduled_content` navigates once to the first bounded TikTok Studio content route that loads, then polls for at most 120 seconds with the same seams. For each row it derives the stable account reference, complete normalized caption, UTF-8 SHA-256, displayed schedule time/timezone, content ID/URL when present, and `observed_at=self._now()`. It returns a record only for exactly one row matching all fields and `observed_at >= expected.submitted_after`; zero rows continue until timeout and multiple rows immediately return `None`. It never clicks an edit, publish, schedule, retry, or delete control.

`wait_for_acceptance` may accept only a bounded explicit platform success message. A navigation change by itself is not success. Explicit platform rejection raises the existing stable rejection error. Timeout or unreadable feedback after the click is `tiktok_schedule_outcome_unknown`, never a retry signal.

The adapter must read the values from the same semantic controls it wrote. Neither `configure` nor `verify` may click the final action.

- [ ] **Step 5: Run all form-adapter tests**

Run:

```bash
.venv/bin/python -m unittest -v \
  test_overseas_tiktok_schedule_form \
  test_overseas_video_publish.TikTokFormAdapterTests
```

Expected: PASS.

- [ ] **Step 6: Commit the isolated adapter**

```bash
git add uploader/tk_uploader/schedule_form.py test_overseas_tiktok_schedule_form.py
git commit -m "feat(tiktok): add native schedule form adapter"
```

---

### Task 6: Integrate Schedule Form, Final Action, Receipts, and Readback

**Files:**
- Modify: `uploader/tk_uploader/main.py`
- Modify: `test_overseas_video_publish.py`
- Modify: `app_core/overseas_tiktok_publish.py`
- Modify: `test_overseas_tiktok_publish.py`
- Modify: `test_overseas_publish_routing.py`

**Interfaces:**
- Consumes: Task 4 prepared `scheduleMode/scheduledAt/scheduleTimezone`.
- Consumes: Task 5 `TikTokScheduleForm` and its immutable records.
- Extends the existing `TiktokVideo` constructor by preserving its current `publish_date` position and adding the keyword `schedule_timezone: str = "Asia/Shanghai"`. Only exact minute strings activate scheduling; `None` and integer `0` mean immediate, while every other type/value fails closed.
- Adds strict runtime hooks `authorized_snapshot_validator` and `schedule_checkpoint_observer`; neither may swallow an exception.
- Produces form receipt: `scheduleMode`, `scheduledAt`, `scheduleTimezone`, `scheduleToggleEnabled`, `finalActionLabel`, `finalActionReady`.
- Produces formal phases: immediate existing phases or `scheduled_accepted` / `scheduled_readback_confirmed`.

- [ ] **Step 1: Write failing uploader integration tests**

```python
def test_prepare_form_configures_schedule_after_visibility_and_before_snapshot(self):
    page = FakeTikTokPage()
    app = self.uploader(publish_date="2026-08-29 15:00")
    target = TikTokScheduleTarget("2026-08-29 15:00", "Asia/Shanghai")
    snapshot = TikTokScheduleFormSnapshot(
        schedule_mode="platform_native",
        scheduled_at="2026-08-29 15:00",
        timezone="Asia/Shanghai",
        toggle_enabled=True,
        final_action_label="Schedule",
        final_action_ready=True,
    )
    form = AsyncMock(spec=TikTokScheduleForm)
    form.configure.return_value = snapshot
    form.verify.return_value = snapshot
    with patch.object(
        tiktok_uploader,
        "TikTokScheduleForm",
        return_value=form,
    ):
        receipt = asyncio.run(app.prepare_form(page, page.base))
    form.configure.assert_awaited_once_with(target)
    form.verify.assert_awaited_once_with(target)
    self.assertEqual(receipt["scheduledAt"], "2026-08-29 15:00")


def test_scheduled_submit_clicks_schedule_once_and_never_post(self):
    clicks = []
    schedule_button = FakeTikTokLeaf(
        text="Schedule",
        on_click=lambda: clicks.append("schedule"),
    )
    page = FakeTikTokPage()
    app = self.uploader(
        publish_date="2026-08-29 15:00",
        execution_mode="formal",
    )
    app.publish_confirmed = True
    form = AsyncMock(spec=TikTokScheduleForm)
    form.final_button.return_value = schedule_button
    form.wait_for_acceptance.return_value = TikTokScheduleAcceptance(
        evidence="platform_feedback:scheduled",
        accepted_at=datetime(2026, 8, 29, 14, 1, tzinfo=SHANGHAI),
    )
    form.readback_scheduled_content.return_value = TikTokScheduledContentReadback(
        content_id=None,
        content_url=None,
        scheduled_at="2026-08-29 15:00",
        schedule_timezone="Asia/Shanghai",
        evidence="scheduled_list:unique",
    )
    app._schedule_form = form
    app._schedule_target = TikTokScheduleTarget(
        "2026-08-29 15:00",
        "Asia/Shanghai",
    )
    app.authorized_snapshot_validator = lambda snapshot: snapshot
    app.schedule_checkpoint_observer = lambda stage: None
    app._post_button = AsyncMock(
        side_effect=AssertionError("scheduled mode must not resolve Post")
    )
    final_action_clicked_at = datetime(2026, 8, 29, 14, 1, tzinfo=SHANGHAI)
    with patch.object(
        tiktok_uploader,
        "_uploader_shanghai_now",
        return_value=final_action_clicked_at,
    ), patch.object(
        app,
        "_verify_form_snapshot",
        new=AsyncMock(return_value={"finalActionLabel": "Schedule"}),
    ):
        result = asyncio.run(app.submit_once(page, page.base))
    self.assertEqual(clicks, ["schedule"])
    self.assertEqual(result["phase"], "scheduled_readback_confirmed")
```

These tests stay as normal `def` methods in the existing `unittest.TestCase` and drive coroutines with `asyncio.run(...)`; do not add bare `async def` methods to that class. Import `hashlib`, aware `datetime`, `SHANGHAI`, and the schedule-form record types explicitly. Extend the existing `TikTokFormAdapterTests.uploader` helper with a `publish_date: object = None` keyword and pass it into `TiktokVideo`. Keep the existing immediate `Post` tests as regression coverage. Add a test proving `platform_form_check` may resolve/read back the final button but never clicks either final button.

In the scheduled submit test, construct the exact post-submit expectation and lock the one-readback contract:

```python
expected_caption = app._caption()
expectation = TikTokScheduledContentExpectation(
    account_reference=app.expected_account_reference,
    expected_caption=expected_caption,
    caption_sha256=hashlib.sha256(expected_caption.encode("utf-8")).hexdigest(),
    target=app._schedule_target,
    submitted_after=final_action_clicked_at,
)
form.wait_for_acceptance.assert_awaited_once_with(app._schedule_target)
form.readback_scheduled_content.assert_awaited_once_with(expectation)
app._post_button.assert_not_awaited()
```

Define in `main.py`:

```python
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _uploader_shanghai_now() -> datetime:
    return datetime.now(SHANGHAI)
```

Patch `_uploader_shanghai_now` in the test to a deterministic aware `final_action_clicked_at`. The uploader captures that exact seam immediately before invoking the real Schedule control's `click()`, then constructs the expectation with the captured pre-click time. This is the only scheduled-list DOM readback in the flow.

Patch `app._post_button` itself with `AsyncMock(side_effect=AssertionError("scheduled mode must not resolve Post"))` before calling `submit_once`; then `assert_not_awaited()` is a real mock assertion rather than an assumption about a bound method. Add `test_scheduled_click_exception_is_outcome_unknown`: make the Schedule control's `click()` raise, assert the final-action observer was called once, no second click occurred, and the raised error is `tiktok_schedule_outcome_unknown` with `outcome_ambiguous=True`.

Add `test_publish_date_activation_is_type_strict`: `None` and integer `0` stay immediate and never construct `TikTokScheduleForm`; exact `"2026-08-29 15:00"` constructs one target; aware/naïve `datetime`, `False`, `0.0`, empty/malformed strings, and any other nonzero value raise `tiktok_schedule_invalid` before page work. This preserves the current `myUtils/postVideo.py` immediate `0` caller while refusing legacy datetime scheduling that was never supported by the approved contract.

Add `test_schedule_checkpoint_write_failure_blocks_success_and_readback`: make real `wait_for_acceptance` return, then make `schedule_checkpoint_observer("scheduled_accepted")` raise; assert `readback_scheduled_content` is not awaited, no success result is returned, and the surfaced error is `tiktok_schedule_outcome_unknown` with `outcome_ambiguous=True`.

Add `test_schedule_mutation_after_prepare_is_blocked_before_click`: let `prepare_form` return the authorized schedule, mutate the fake live date/time/toggle, then invoke `submit_once`; its fresh `_verify_form_snapshot` must be passed to `authorized_snapshot_validator`, which raises `tiktok_form_snapshot_mismatch`; assert Schedule/Post are never clicked and no final-action event fires.

- [ ] **Step 2: Write failing platform-service state and receipt tests**

```python
def test_scheduled_form_check_stops_before_schedule_and_returns_exact_time(self):
    run = self.run_sync(
        mode="platform_form_check",
        prepared_changes={
            "scheduleMode": "platform_native",
            "scheduledAt": "2026-08-29 15:00",
            "scheduleTimezone": "Asia/Shanghai",
        },
    )
    self.assertEqual(run.result["phase"], "platform_form_verified")
    self.assertEqual(run.result["receipt"]["scheduledAt"], "2026-08-29 15:00")
    self.assertFalse(run.result["receipt"]["finalActionTriggered"])


def test_scheduled_formal_requires_readback_or_becomes_ambiguous(self):
    uploader = self.fake_uploader(
        submit_result={
            "status": "scheduled",
            "phase": "scheduled_accepted",
            "receipt": {
                "scheduledAt": "2026-08-29 15:00",
                "scheduleTimezone": "Asia/Shanghai",
                "platformAccepted": True,
                "scheduledReadbackConfirmed": False,
                "publishedAt": None,
            },
        }
    )
    with self.assertRaises(TikTokPublishError) as raised:
        self.run_sync(
            mode="formal",
            uploader=uploader,
            prepared_changes={
                "scheduleMode": "platform_native",
                "scheduledAt": "2026-08-29 15:00",
                "scheduleTimezone": "Asia/Shanghai",
            },
        )
    self.assertEqual(raised.exception.error_code, "tiktok_schedule_outcome_unknown")
    self.assertTrue(raised.exception.outcome_ambiguous)
```

Extend `TikTokPlatformSyncTests.prepared` with `**changes`, and extend `run_sync` with `prepared_changes: Mapping[str, Any] | None = None` plus an aware `schedule_now` fixture. Patch the small `_shanghai_now()` clock seam during these tests so no assertion depends on the wall clock. Add a success case with `scheduledAt` equal to the authorized snapshot and `publishedAt=None`, a mismatched-time failure, a final 15-minute gate failure before click, an immediate-public regression, and `test_scheduled_acceptance_event_is_persisted_before_list_readback`: have the uploader emit `scheduled_accepted`, block/raise before unique list readback, and assert `tiktok_scheduled_accepted` was recorded before the terminal ambiguous result.

Add `test_publish_context_factory_defaults_to_shanghai` using a fake browser whose `new_context` is an `AsyncMock`; call the real `utils.base_social_media.new_publish_context` and assert `timezone_id="Asia/Shanghai"`. Also expose the patched `new_publish_context` mock from `run_sync` and assert the TikTok path never supplies a conflicting `timezone_id` override.

- [ ] **Step 3: Run the new integration tests and confirm failure**

Run:

```bash
.venv/bin/python -m unittest -v \
  test_overseas_video_publish.TikTokFormAdapterTests \
  test_overseas_tiktok_publish.TikTokPlatformSyncTests
```

Expected: FAIL because the uploader still passes `publish_date=0`, resolves only `_post_button`, and requires `publishedAt` for all successful readback.

- [ ] **Step 4: Integrate the schedule adapter without duplicating the form path**

Add `hashlib`, `datetime`, and `ZoneInfo` to `uploader/tk_uploader/main.py` imports, plus the five Task 5 record/adapter types. Keep `re` for the strict constructor format check.

Change the existing uploader construction from `publish_date=0` to:

```python
uploader = uploader_class(
    str(prepared["title"]),
    str(prepared["videoPath"]),
    list(prepared["topics"]),
    prepared.get("scheduledAt"),
    str(prepared["accountFile"]),
    description=str(prepared["body"]),
    schedule_timezone=str(prepared["scheduleTimezone"]),
    dry_run=mode == "platform_form_check",
    dry_run_hold_browser=False,
    expected_account_reference=str(prepared["expectedAccountReference"]),
    execution_mode=mode,
)
```

In `prepare_form`, call schedule configuration only after public visibility has been read back and before final-action readiness/snapshot. Immediate mode must not query schedule controls.

Normalize `publish_date` once in `TiktokVideo.__init__`, without truthiness:

```python
if publish_date is None or (type(publish_date) is int and publish_date == 0):
    self.publish_date = None
elif type(publish_date) is str and re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", publish_date):
    try:
        parsed_publish_date = datetime.strptime(publish_date, "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise TikTokPublishError(
            "tiktok_schedule_invalid",
            "TikTok 排期必须是有效的北京时间",
        ) from exc
    if parsed_publish_date.strftime("%Y-%m-%d %H:%M") != publish_date:
        raise TikTokPublishError(
            "tiktok_schedule_invalid",
            "TikTok 排期必须是精确到分钟的北京时间字符串",
        )
    self.publish_date = publish_date
else:
    raise TikTokPublishError(
        "tiktok_schedule_invalid",
        "TikTok 排期必须是精确到分钟的北京时间字符串",
    )
```

Initialize `self.schedule_timezone = schedule_timezone`, `self._schedule_form = None`, `self._schedule_target = None`, `self.authorized_snapshot_validator = None`, and `self.schedule_checkpoint_observer = None`. This keeps the existing immediate path safe: `_final_action_button` must not raise `AttributeError` merely because no schedule adapter was created. A non-`None` `publish_date` also requires `schedule_timezone == "Asia/Shanghai"` before page work.

Create one adapter per scheduled upload and keep it on the uploader for `submit_once`:

```python
self._schedule_form = TikTokScheduleForm(
    page,
    resolve_base=lambda: self._base(page),
    wait_for_manual_intervention=self._wait_for_manual_intervention,
)
self._schedule_target = TikTokScheduleTarget(
    str(self.publish_date),
    self.schedule_timezone,
)
await self._schedule_form.configure(self._schedule_target)
```

Call `verify` again after upload readiness and before producing the final form snapshot. The adapter, not `main.py`, owns schedule-control selectors and scheduled-list DOM parsing.
Merge `verified_schedule.as_dict()` into the existing caption/topic/visibility snapshot so the service compares one complete authorized form receipt.

Wire the click-time authorization check explicitly. The service sets:

```python
uploader.authorized_snapshot_validator = (
    lambda live: _verify_authorized_form_snapshot(prepared, live)
)
```

In scheduled `submit_once`, after the fresh `snapshot = await self._verify_form_snapshot(page, base)` and before resolving/clicking the final button, require this hook to be callable and invoke it synchronously with `snapshot`. It must compare all schedule fields: `scheduleMode`, `scheduledAt`, `scheduleTimezone`, `scheduleToggleEnabled=True`, `finalActionLabel`, and `finalActionReady`, in addition to the existing caption, topics, visibility, and account checks. A missing hook or any mismatch stops with `tiktok_form_snapshot_mismatch` before a platform write. Do not reuse only the older snapshot returned by `prepare_form`.

- [ ] **Step 5: Generalize final-action instrumentation**

Add one uploader resolver and use it from readiness checks, snapshot verification, and `submit_once`:

```python
async def _final_action_button(self, base):
    if self._schedule_form is not None and self._schedule_target is not None:
        return await self._schedule_form.final_button(self._schedule_target)
    return await self._post_button(base)
```

Replace service instrumentation of `_post_button` with instrumentation of `_final_action_button`. Record the existing generic event `tiktok_final_action_triggered` before the click and include `finalAction="schedule"` or `"post"` in the private receipt. Keep one `_submit_consumed` guard for both modes.

Put the final 15-minute gate in the synchronous trigger that `_FinalActionButton.click()` invokes immediately before the actual DOM click:

```python
def trigger_final_action() -> None:
    if final_state["triggered"]:
        return
    validate_tiktok_final_schedule_window(
        prepared,
        now=_shanghai_now(),
    )
    _record_tiktok_event(
        task_id,
        "tiktok_final_action_triggered",
        "TikTok 最终动作即将执行，已禁止自动重试",
    )
    final_state["triggered"] = True
```

The time check must pass before recording the final-action event and before calling the real button. Do not check only at `submit_once` entry because form revalidation or manual security verification may consume the remaining buffer.

Do not reuse `_emit_form_stage` for durable state because that diagnostic helper intentionally swallows observer errors. Add a separate strict checkpoint hook:

```python
def _emit_schedule_checkpoint(self, stage: str) -> None:
    observer = self.schedule_checkpoint_observer
    if not callable(observer):
        raise TikTokPublishError(
            "tiktok_schedule_outcome_unknown",
            "TikTok 排期受理状态无法持久化",
            outcome_ambiguous=True,
        )
    observer(stage)  # Deliberately do not catch persistence failures.
```

After `wait_for_acceptance(target)` returns and before scheduled-list readback begins, the uploader calls `_emit_schedule_checkpoint("scheduled_accepted")` exactly once. Install the strict service observer explicitly:

```python
def persist_schedule_checkpoint(stage: str) -> None:
    if stage != "scheduled_accepted":
        raise TikTokPublishError(
            "tiktok_schedule_outcome_unknown",
            "TikTok 排期检查点无效",
            outcome_ambiguous=True,
        )
    _record_tiktok_event(
        task_id,
        "tiktok_scheduled_accepted",
        "TikTok 已明确受理排期，正在只读核对内容列表",
    )


uploader.schedule_checkpoint_observer = persist_schedule_checkpoint
```

This accepts only the fixed stage and synchronously records no raw platform text. If the database/event write raises, convert the escaping exception at the post-click boundary to `tiktok_schedule_outcome_unknown`, skip list readback, and never return success. The task event plus the normalized stored payload is the durable proof used by Task 7 if the worker dies before the final receipt; a route change or timeout must never emit it.

- [ ] **Step 6: Split immediate and scheduled result verification**

Keep `_exact_content_readback` for already-public immediate posts. Add a strict scheduled readback that requires the authorized time and forbids premature `publishedAt`:

```python
def _exact_scheduled_readback(
    result: Mapping[str, Any],
    *,
    expected_scheduled_at: str,
) -> dict[str, Any] | None:
    source = result.get("receipt") if isinstance(result.get("receipt"), Mapping) else result
    if (
        result.get("status") != "scheduled"
        or result.get("phase") != "scheduled_readback_confirmed"
        or source.get("scheduleMode") != "platform_native"
        or source.get("scheduledAt") != expected_scheduled_at
        or source.get("scheduleTimezone") != "Asia/Shanghai"
        or source.get("platformAccepted") is not True
        or source.get("scheduledReadbackConfirmed") is not True
        or source.get("publishedAt") is not None
    ):
        return None
    return {
        "contentId": source.get("contentId"),
        "contentUrl": source.get("contentUrl"),
        "scheduleMode": "platform_native",
        "scheduledAt": expected_scheduled_at,
        "scheduleTimezone": "Asia/Shanghai",
        "platformAccepted": True,
        "scheduledReadbackConfirmed": True,
        "publishedAt": None,
}
```

The scheduled branch in `submit_once` uses this exact order after the fresh authorized snapshot passes:

```python
button = await self._final_action_button(base)
submitted_after = _uploader_shanghai_now()
await button.click()
acceptance = await self._schedule_form.wait_for_acceptance(self._schedule_target)
self._emit_schedule_checkpoint("scheduled_accepted")
expected_caption = self._caption()
expectation = TikTokScheduledContentExpectation(
    account_reference=self.expected_account_reference,
    expected_caption=expected_caption,
    caption_sha256=hashlib.sha256(expected_caption.encode("utf-8")).hexdigest(),
    target=self._schedule_target,
    submitted_after=submitted_after,
)
readback = await self._schedule_form.readback_scheduled_content(expectation)
```

Any exception from the real click through checkpoint/list readback is after the final-action trigger and therefore must preserve the single-use guard and surface as scheduled outcome unknown unless it is a proven explicit platform rejection. Never call either post-click method twice.

`TiktokVideo.submit_once` must call `wait_for_acceptance(target)` once and `readback_scheduled_content(expectation)` once, then return one structured result to the service. The scheduled result schema is fixed: top level `status`, `phase`, `evidence`, `formSnapshot`, and `receipt`; the receipt contains only `scheduleMode`, `scheduledAt`, `scheduleTimezone`, `platformAccepted`, `scheduledReadbackConfirmed`, `contentId`, `contentUrl`, and `publishedAt`. An explicit schedule acceptance enters `scheduled_accepted` only after `wait_for_acceptance` succeeds and before the bounded list readback starts, using the persisted observer above. If the DOM click raises after `tiktok_final_action_triggered`, if acceptance cannot be proved, or if unique readback is still absent at the deadline, raise `tiktok_schedule_outcome_unknown` with `outcome_ambiguous=True` and end the task. `_exact_scheduled_readback` is only a pure validation of that returned dictionary; the service does not parse TikTok DOM or perform a second platform-list readback.

- [ ] **Step 7: Run the TikTok affected suites**

Run:

```bash
.venv/bin/python -m unittest -v \
  test_tiktok_schedule_contract \
  test_overseas_tiktok_schedule_form \
  test_overseas_tiktok_publish \
  test_overseas_video_publish \
  test_overseas_publish_routing
```

Expected: PASS.

- [ ] **Step 8: Commit TikTok runtime integration**

```bash
git add uploader/tk_uploader/main.py test_overseas_video_publish.py app_core/overseas_tiktok_publish.py test_overseas_tiktok_publish.py test_overseas_publish_routing.py
git commit -m "feat(tiktok): submit and read native schedules"
```

Only add `test_overseas_publish_routing.py` if it actually changed. `app_core/publish_service.py` remains reserved for Task 7's failing passthrough test. Never use `git add -A` in the dirty worktree.

---

### Task 7: Persist Safe Scheduled Receipts and Recover Interrupted Tasks

**Files:**
- Modify: `app_core/task_service.py`
- Modify: `test_task_service.py`
- Modify: `test_publish_service.py`
- Modify only after a failing passthrough test: `app_core/publish_service.py`

**Interfaces:**
- Extends `_TIKTOK_RECEIPT_FIELDS` with `scheduleMode`, `scheduledAt`, `scheduleTimezone`, `platformAccepted`, and `scheduledReadbackConfirmed`.
- Extends `_TIKTOK_RECEIPT_PHASES` with `scheduled_accepted` and `scheduled_readback_confirmed`.
- Reconciles a stale scheduled task after `tiktok_final_action_triggered` as `tiktok_schedule_outcome_unknown` with `phase="ambiguous"`, exact schedule fields, and no `publishedAt`; a prior `tiktok_scheduled_accepted` event additionally restores `platformAccepted=True`.
- Preserves existing immediate TikTok and YouTube reconciliation behavior.

- [ ] **Step 1: Write failing receipt-projection and stale-worker tests**

Extend `TikTokTaskServiceTests._task` with `scheduled: bool = False`; when true, add the normalized schedule fields to its single TikTok payload. Add:

```python
def test_scheduled_tiktok_receipt_keeps_safe_schedule_and_no_published_at(self):
    task = self._task(scheduled=True)
    task_service.mark_platform_result(
        task["id"],
        6,
        ok=True,
        message="TikTok 定时内容已唯一回读",
        content_type="video",
        event_type="tiktok_scheduled_readback_confirmed",
        receipt={
            "accountId": 61,
            "visibility": "public",
            "scheduleMode": "platform_native",
            "scheduledAt": "2026-08-30 09:00",
            "scheduleTimezone": "Asia/Shanghai",
            "platformWriteOccurred": True,
            "finalActionTriggered": True,
            "platformAccepted": True,
            "scheduledReadbackConfirmed": True,
            "publishedAt": None,
            "phase": "scheduled_readback_confirmed",
            "cookie": "must-not-survive",
        },
    )
    item = task_service.get_task(task["id"])["items"][0]
    receipt = json.loads(item["receiptJson"])
    self.assertEqual(receipt["scheduledAt"], "2026-08-30 09:00")
    self.assertIsNone(receipt["publishedAt"])
    self.assertNotIn("cookie", receipt)
    self.assertEqual(item["publishedAt"], "")
```

Add `test_stale_scheduled_tiktok_after_final_action_is_ambiguous_and_retains_schedule`: create a scheduled task, record `tiktok_final_action_triggered`, expire the worker, reconcile it, and assert `tiktok_schedule_outcome_unknown`, exact schedule fields including `scheduleMode="platform_native"`, `finalActionTriggered=True`, `platformAccepted` not true, `phase="ambiguous"`, and no `publishedAt`. Add `test_stale_scheduled_tiktok_after_acceptance_retains_acceptance_without_claiming_readback`: record both `tiktok_final_action_triggered` and `tiktok_scheduled_accepted`, reconcile, and assert `platformAccepted=True`, `scheduledReadbackConfirmed` not true, and the same ambiguous terminal state. Keep the existing immediate stale test expecting `tiktok_publish_outcome_unknown`.

- [ ] **Step 2: Run the focused shared tests and confirm failure**

Run:

```bash
.venv/bin/python -m unittest -v test_task_service.TikTokTaskServiceTests
```

Expected: FAIL because scheduled fields/phases are not yet projected and the stale-worker branch still emits the immediate-publish error code.

- [ ] **Step 3: Implement strict projection and mode-aware reconciliation**

Reuse `TikTokPublishError("tiktok_receipt_projection", "TikTok receipt projection", receipt=receipt).receipt` as the scalar safety boundary, then retain only keys in `_TIKTOK_RECEIPT_FIELDS`. In stale reconciliation, inspect the normalized stored payload:

```python
scheduled_tiktok = (
    is_tiktok_task
    and payloads[0].get("scheduleMode") == "platform_native"
    and payloads[0].get("scheduleTimezone") == "Asia/Shanghai"
    and type(payloads[0].get("scheduledAt")) is str
)
unknown_code = (
    "tiktok_schedule_outcome_unknown"
    if scheduled_tiktok
    else "tiktok_publish_outcome_unknown"
)
```

For scheduled stale tasks, copy `scheduleMode="platform_native"`, `scheduledAt`, and `scheduleTimezone` from the already-normalized payload into the ambiguous receipt. Query for `tiktok_scheduled_accepted`; set `platformAccepted=True` only when that persisted event or a prior sanitized receipt proves acceptance, and never infer `scheduledReadbackConfirmed=True`. Do not create a local content ID or URL.

- [ ] **Step 4: Prove the thin publish service preserves the result**

Add `PublishServiceTikTokScheduleTests` to `test_publish_service.py` with this exact setup and execution shape:

```python
class PublishServiceTikTokScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(
            database, "DB_PATH", Path(self.tempdir.name) / "database.db"
        )
        self.db_patch.start()
        database.ensure_schema()

    def tearDown(self):
        self.db_patch.stop()
        self.tempdir.cleanup()

    def test_scheduled_result_is_persisted_without_published_at(self):
        payload = {
            "type": 6,
            "contentType": "video",
            "runtimeMode": "publish",
            "accountList": ["tiktok.json"],
            "scheduleMode": "platform_native",
            "scheduledAt": "2026-08-30 09:00",
            "scheduleTimezone": "Asia/Shanghai",
        }
        task = task_service.create_pending_task([payload], mode="oneclick_publish")
        result = {
            "ok": True,
            "status": "scheduled",
            "phase": "scheduled_readback_confirmed",
            "message": "TikTok 定时内容已唯一回读",
            "receipt": {
                "scheduleMode": "platform_native",
                "scheduledAt": "2026-08-30 09:00",
                "scheduleTimezone": "Asia/Shanghai",
                "platformAccepted": True,
                "scheduledReadbackConfirmed": True,
                "contentId": None,
                "contentUrl": None,
                "publishedAt": None,
            },
        }
        with patch.object(
            publish_service.overseas_tiktok_publish,
            "run_tiktok_platform_sync",
            return_value=result,
        ) as run_tiktok:
            publish_service._run_publish(task, [payload])
        saved = task_service.get_task(task["id"])
        item = saved["items"][0]
        receipt = json.loads(item["receiptJson"])
        run_tiktok.assert_called_once_with(
            payload, mode="formal", task_id=int(task["id"])
        )
        self.assertEqual(saved["status"], "success")
        self.assertEqual(receipt["scheduleMode"], "platform_native")
        self.assertEqual(receipt["scheduledAt"], "2026-08-30 09:00")
        self.assertEqual(receipt["scheduleTimezone"], "Asia/Shanghai")
        self.assertTrue(receipt["platformAccepted"])
        self.assertTrue(receipt["scheduledReadbackConfirmed"])
        self.assertEqual(item["publishedAt"], "")
```

Add `import json` at the top of `test_publish_service.py`. This calls the real thin orchestration for one scheduled task, then reads the task item from `task_service.get_task`; the mock assertion also proves there is no second TikTok service call.

Run: `.venv/bin/python -m unittest -v test_publish_service.PublishServiceTikTokScheduleTests`

Expected: PASS against the current thin orchestration. If it fails because the service drops or rewrites a safe receipt field, make the smallest fix in `app_core/publish_service.py`, rerun this exact test, and include that file in Step 6's shared commit.

- [ ] **Step 5: Run the shared affected tests**

Run:

```bash
.venv/bin/python -m unittest -v \
  test_task_service.TikTokTaskServiceTests \
  test_publish_service.PublishServiceTikTokScheduleTests \
  test_controlled_publish
```

Expected: PASS; both scheduled and immediate stale-task tests reach a terminal task state, and neither leaves an item `pending` or `running`.

- [ ] **Step 6: Commit shared persistence separately**

```bash
git add app_core/task_service.py test_task_service.py test_publish_service.py app_core/publish_service.py
git commit -m "feat(tasks): persist TikTok scheduled receipts"
```

Add `app_core/publish_service.py` only if Step 4 produced and then closed a failing passthrough test. This shared commit remains integration-review-only.

---

### Task 8: Verify Recovery, Scope, Full Regression, and Local Evidence

**Files:**
- Modify: `docs/architecture/tiktok-controlled-video-publish.architecture.json/html`
- Modify: `docs/architecture/tiktok-controlled-video-publish.lifecycle.json/html`
- Modify: `docs/verification/2026-08-29-tiktok-scheduled-video-publish.md`
- Modify: `SOURCE_OF_TRUTH.md`
- Test only: `test_task_service.py`, `test_controlled_publish.py`, `test_publish_service.py`, repository-wide `test_*.py`

**Interfaces:**
- Verifies: final-action-before/after interruption maps to `failed` / `ambiguous` using existing task reconciliation.
- Verifies: project task JSON exposes `scheduledAt` but does not invent `publishedAt`.
- Records: exact commands, counts, commit, limitations and no-platform-action statement.

- [ ] **Step 1: Refresh architecture evidence to the implemented source**

Use `rg -n` to locate the implemented schedule contract, `TikTokScheduleForm`, uploader `_final_action_button`, service final-time gate, scheduled readback verifier, and task-service stale reconciliation. Replace Task 1's spec/old-source references with those exact current source lines, then rerun both Archify `validate`, `deliver`, and `visual-check` commands from Task 1. Expected: 9 checks, 0 errors, 0 warnings for each diagram, with no clipped nodes in either contact sheet.

- [ ] **Step 2: Run focused task, controlled, and routing suites**

Run:

```bash
.venv/bin/python -m unittest -v \
  test_task_service \
  test_controlled_publish \
  test_publish_service \
  test_overseas_publish_routing \
  test_tiktok_schedule_contract \
  test_overseas_tiktok_schedule_form \
  test_overseas_tiktok_publish \
  test_overseas_video_publish
```

Expected: PASS.

- [ ] **Step 3: Run the explicit overseas/TikTok affected suite**

Run:

```bash
.venv/bin/python -m unittest -v \
  test_account_detection_ui \
  test_controlled_publish \
  test_oneclick_authorization \
  test_oneclick_capabilities \
  test_overseas_integration \
  test_overseas_publish_routing \
  test_overseas_tiktok_identity \
  test_overseas_tiktok_profile \
  test_overseas_tiktok_publish \
  test_tiktok_schedule_contract \
  test_overseas_tiktok_schedule_form \
  test_overseas_tiktok_session_scope \
  test_overseas_tiktok_system_login \
  test_overseas_video_publish \
  test_task_service \
  test_tiktok_login_startup_wiring
```

Expected: PASS. Record the exact count and duration from this run.

- [ ] **Step 4: Run full offline regression and source UI smoke**

Run:

```bash
.venv/bin/python -m unittest discover -p 'test_*.py'
QT_QPA_PLATFORM=offscreen .venv/bin/python desktop_native_app.py --ui-test
git diff --check origin/main...HEAD
git diff --check
.venv/bin/python tools/check_workstream_scope.py --stream overseas --base origin/main
```

Expected: all tests pass, UI reports `NATIVE_DESKTOP_UI_OK`, both diff checks are empty. The scope checker exits `2` only because approved shared files are `REVIEW_REQUIRED`; every TikTok architecture/spec/plan/verification path is `owned`, and any `outside` result blocks completion.

- [ ] **Step 5: Write exact local verification evidence**

Create `docs/verification/2026-08-29-tiktok-scheduled-video-publish.md` with:

```markdown
# TikTok 平台原生定时发布验证

## 本地证据
- 分支与提交
- 聚焦测试结果、数量与耗时
- 海外受影响测试结果、数量与耗时
- 完整回归结果、数量与耗时
- 离屏客户端返回值

## 平台动作
- 本轮未打开 TikTok Studio、未上传视频、未点击 Post/Schedule。

## 尚未验证
- 当前账号定时入口真实可用性。
- TikTok 对正式 Schedule 的受理与定时列表回读。
```

Fill every evidence bullet with the exact output collected in Steps 2–4. If any command has no reliable count or return value, stop and rerun it rather than committing an incomplete evidence document.

- [ ] **Step 6: Update current facts without overclaiming**

Add one `SOURCE_OF_TRUTH.md` entry that says only: source implementation completed, exact local test counts, no real TikTok form check, no `Schedule` click, and no formal schedule receipt. Do not change the application version.

- [ ] **Step 7: Commit verification evidence**

```bash
git add docs/architecture/tiktok-controlled-video-publish.architecture.json docs/architecture/tiktok-controlled-video-publish.architecture.html docs/architecture/tiktok-controlled-video-publish.lifecycle.json docs/architecture/tiktok-controlled-video-publish.lifecycle.html docs/verification/2026-08-29-tiktok-scheduled-video-publish.md SOURCE_OF_TRUTH.md
git commit -m "docs(tiktok): record scheduled publish offline evidence"
```

---

### Task 9: Stop at the Real-Platform Authorization Gate

**Files:**
- No source modification before authorization.
- The implementation run ends at this gate; a later separately authorized platform run may append verified evidence to `docs/verification/2026-08-29-tiktok-scheduled-video-publish.md` and `SOURCE_OF_TRUTH.md`.

**Interfaces:**
- Requires for a later run: an exact readable manifest, a currently normal TikTok account ID, a specifically approved future `Asia/Shanghai` time, and `mode="platform_form_check"` with `platformFormCheckConfirmed=true`.
- Must return: `platform_form_verified`, exact `scheduledAt`, and `finalActionTriggered=false`.

- [ ] **Step 1: Stop and request a separate real-platform form-check authorization**

Report the completed offline evidence and ask大帅是否允许：使用当前 TikTok 测试账号、一个专用测试视频和未来 30 分钟至 10 天内的北京时间，执行一次会上传但不会点击 `Schedule` 的 `platform_form_check`。

Do not create the request, open TikTok Studio or upload until that authorization is explicit.

- [ ] **Step 2: Hand control back without preparing a platform request**

The completion report must state that the next action is a separate real-platform `platform_form_check`, that it uploads a video but does not click `Schedule`, and that the exact manifest, account ID and future time must be approved in that later turn. Do not reuse task 78 or account 13 by assumption; re-read current records only after authorization.

---

## Execution Stop Conditions

Stop immediately and report rather than broaden scope when any of the following occurs:

- A needed change falls outside the overseas allowlist or cannot be isolated from unrelated dirty files.
- The live page has no unique schedule control or uses semantics not represented by the approved design.
- The target time is inside the 30-minute creation buffer or 15-minute final buffer.
- TikTok still presents `Post` for a scheduled request.
- A final action may already have been triggered.
- A test requires weakening existing account, topic entity, visibility, authorization, or duplicate-submit gates.
- The only way to proceed would be storing or exposing Cookie, password, QR, verification code, or raw session state.
