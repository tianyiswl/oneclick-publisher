# TikTok Controlled Video Publish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把现有 TikTok 浏览器源码收口为一条可由内容项目通过受控 CLI 调用的单账号、单视频、立即公开发布通道；默认预检完全不打开浏览器，首次真实验收只做到 `Post` 前，正式发布仍需要绑定当次快照的一次性授权。

**Architecture:** 在现有 `controlled_publish -> publish_service -> task_service` 主链中加入 TikTok 专用合同和执行服务，账号绑定与正式执行共用同一主体回读器，平台表单检查与正式发布共用同一上传、正文、官方话题、可见性和按钮就绪回读器。普通预检只调用本地合同；真实浏览器只在 `platform_form_check` 或正式任务中启动。最终点击事件在发生时立即落库，任务进程失联后据此进入结果不明并阻止自动重发。

**Tech Stack:** Python 3、PyQt6、SQLite、Playwright、现有本机受控发布 CLI、`unittest`、Archify 架构证据。

**Spec:** `docs/superpowers/specs/2026-08-28-tiktok-controlled-video-publish-design.md`

## Global Constraints

- 首轮只支持一个已保存 TikTok 账号、一条本地视频、立即公开发布。
- 默认 `preflight` 只能读本地 manifest、账号记录和素材文件；禁止创建 Playwright、访问 TikTok、上传素材或改写会话文件。
- `platform_form_check` 是明确的开发验收模式，会向平台上传并填写，但必须在 `Post` 前停止；它不能被普通内容项目默认调用。
- 本轮真实平台验收禁止点击 `Post`，因此不能报告“正式发布已验证”。
- 正式模式必须消费与预检快照完全一致、短期有效且只能使用一次的授权；输入变化后旧授权失效。
- 正文与结构化话题必须分开写入；话题只有选择唯一官方候选并回读为平台实体才算成功，普通 `#文字` 必须失败。
- 正常上传和填写不抢占前台；登录过期、CAPTCHA、二次验证、风控或未知确认出现时才显示同一浏览器给用户处理。
- `Post` 一旦点击，先写入 `tiktok_final_action_triggered` 事件；之后超时、中断或页面异常均进入 `ambiguous`，禁止自动再点。
- 不把 Cookie、密码、验证码、二维码内容、会话路径或完整正文写入 CLI 公共 JSON、任务投影、Git 或长期日志。
- 海外专属文件可以在本分支提交；账号管理、共享 CLI、任务数据库和通用发布服务的改动必须单独提交，留给集成线审查。
- 功能分支不升级版本号、不制作正式安装包、不直接合并 `main`。
- 实施采用测试阶梯：新失败测试 -> 最相关测试 -> 海外受影响测试 -> 全量测试与源码客户端冒烟。

---

## File Structure

### New files

- `app_core/overseas_tiktok_identity.py` — TikTok 稳定 handle 的归一化、页面回读、首次绑定和同一主体校验。
- `app_core/overseas_tiktok_publish.py` — TikTok 单视频合同、本地零写入预检、平台表单检查和正式执行入口。
- `test_overseas_tiktok_identity.py` — 主体归一化、首次绑定、串号和会话缺失测试。
- `test_overseas_tiktok_publish.py` — 严格输入、本地预检、表单检查、正式回执和异常结果测试。
- `docs/architecture/tiktok-controlled-video-publish.architecture.html`
- `docs/architecture/tiktok-controlled-video-publish.architecture.json`
- `docs/architecture/tiktok-controlled-video-publish.lifecycle.html`
- `docs/architecture/tiktok-controlled-video-publish.lifecycle.json`
- `docs/verification/2026-08-28-tiktok-controlled-video-publish.md` — 离线测试和首次真实 `Post` 前验收证据。

### Modified files

- `uploader/tk_uploader/main.py` — 唯一 TikTok Studio 表单适配器：普通正文、官方话题实体、公开范围、人工验证、最终动作和结果回读。
- `myUtils/login.py` — TikTok 登录成功后回读稳定主体并绑定账号记录。
- `app_core/account_service.py` — 静默检测 TikTok 登录态和同一主体，异常账号保持不可发布。
- `app_core/overseas_preflight.py` — type 6 改为本地零平台写入，其他海外平台保持原行为。
- `app_core/overseas_video_publish.py` — 移除 TikTok 路由，只保留尚未迁移的 YouTube 浏览器兼容入口。
- `app_core/controlled_publish.py` — TikTok 目标合同、`platform_form_check`、快照指纹、一次性授权、任务投影和重复提交保护。
- `app_core/publish_service.py` — type 6 显式路由到 TikTok 专用服务，并为开发表单检查建立独立任务模式。
- `app_core/task_service.py` — TikTok 回执白名单、最终动作后的失联收口和结果不明状态。
- `app_core/oneclick_capabilities.py` — 把 TikTok 默认预检说明改成“本地检查”，避免继续宣称每次都上传到 `Post` 前。
- `test_overseas_integration.py` — 证明 TikTok 默认预检不调用浏览器处理器。
- `test_overseas_video_publish.py` — TikTok 表单适配器、话题实体、人工验证和最终动作测试。
- `test_overseas_publish_routing.py` — type 6 三种模式路由测试。
- `test_controlled_publish.py` — TikTok 账号、设置、授权指纹、开发验收和防重复测试。
- `test_task_service.py` — TikTok 回执投影与失联任务收口测试。
- `SOURCE_OF_TRUTH.md` — 只登记真正通过的离线测试和真实平台 `Post` 前验收，不登记发布成功。

---

### Task 1: Create Architecture and Lifecycle Evidence Before Code

**Files:**
- Create: `docs/architecture/tiktok-controlled-video-publish.architecture.html`
- Create: `docs/architecture/tiktok-controlled-video-publish.architecture.json`
- Create: `docs/architecture/tiktok-controlled-video-publish.lifecycle.html`
- Create: `docs/architecture/tiktok-controlled-video-publish.lifecycle.json`

**Interfaces:**
- Documents: content project -> controlled CLI -> controlled publish -> publish service -> TikTok service -> uploader -> TikTok Studio.
- Documents: saved account/session -> identity reader -> account record/reference.
- Documents: `local_preflight_passed -> platform_form_verified` and `local_preflight_passed -> final_action_triggered -> platform_accepted -> published_readback_confirmed`.
- Documents: verification wait, explicit failure, ambiguous outcome, lease expiry, and duplicate blocking.

- [ ] **Step 1: Invoke Archify and read its complete instructions**

Use the `archify` skill because this change crosses an external platform, persistent sessions, asynchronous task state, retry protection, and a manual confirmation point. Build from the actual files named above; do not draw a proposed second automation stack.

- [ ] **Step 2: Generate the component architecture artifacts**

The architecture diagram must include these exact nodes and boundaries:

```text
Content Project / Desktop UI
  -> Controlled CLI request
  -> app_core.controlled_publish
  -> app_core.publish_service
  -> app_core.overseas_tiktok_publish
  -> uploader.tk_uploader.TiktokVideo
  -> TikTok Studio

Account DB -> TikTok Identity Reader <- Isolated Session File
Task DB <- events / receipts / heartbeat <- Publish Service
User Verification <-> Same Playwright Page
```

Mark `platform_form_check` as a platform write that stops before the final action. Mark Cookie/session bytes as private data that never cross into request or receipt JSON.

- [ ] **Step 3: Generate the lifecycle artifacts**

The lifecycle diagram must include these exact terminal distinctions:

```text
local_preflight_passed
platform_form_verified
failed_before_final_action
waiting_user_verification
final_action_triggered
platform_accepted
published_readback_confirmed
ambiguous
```

Show that `final_action_triggered` cannot transition back to an automatic retry path. Show lease expiry before the final action as `failed`, and lease expiry after it as `ambiguous`.

- [ ] **Step 4: Validate that all four artifacts are non-empty and contain the required boundaries**

Run:

```bash
test -s docs/architecture/tiktok-controlled-video-publish.architecture.html
test -s docs/architecture/tiktok-controlled-video-publish.architecture.json
test -s docs/architecture/tiktok-controlled-video-publish.lifecycle.html
test -s docs/architecture/tiktok-controlled-video-publish.lifecycle.json
rg -n "controlled_publish|overseas_tiktok_publish|TikTok Studio|platform_form_check|final_action_triggered|ambiguous|User Verification" docs/architecture/tiktok-controlled-video-publish.*
```

Expected: all `test -s` commands exit 0, and every required node/state appears in the generated sources.

- [ ] **Step 5: Commit architecture evidence only**

```bash
git add docs/architecture/tiktok-controlled-video-publish.*
git commit -m "docs(overseas): map TikTok controlled publish lifecycle"
```

---

### Task 2: Add the Stable TikTok Identity Contract

**Files:**
- Create: `app_core/overseas_tiktok_identity.py`
- Create: `test_overseas_tiktok_identity.py`

**Interfaces:**
- Produces: `TikTokIdentity(handle: str, display_name: str, profile_url: str)`.
- Produces: `TikTokIdentityError(error_code: str, message: str)`.
- Produces: `normalize_tiktok_handle(value: object) -> str`.
- Produces: `read_tiktok_identity(page) -> TikTokIdentity`.
- Produces: `validate_identity_binding(account: Mapping[str, Any], identity: TikTokIdentity, *, allow_initial_bind: bool) -> str`.

- [ ] **Step 1: Write failing pure identity tests**

```python
class TikTokIdentityTests(unittest.TestCase):
    def test_normalize_handle_accepts_profile_url_and_at_prefix(self):
        self.assertEqual(normalize_tiktok_handle("https://www.tiktok.com/@Test.User"), "test.user")
        self.assertEqual(normalize_tiktok_handle("@Test.User"), "test.user")

    def test_existing_binding_rejects_another_logged_in_handle(self):
        account = {"accountReference": "expected.user"}
        identity = TikTokIdentity("other.user", "Other", "https://www.tiktok.com/@other.user")
        with self.assertRaises(TikTokIdentityError) as raised:
            validate_identity_binding(account, identity, allow_initial_bind=False)
        self.assertEqual(raised.exception.error_code, "tiktok_account_identity_mismatch")

    def test_empty_legacy_binding_can_be_initialized_once(self):
        identity = TikTokIdentity("expected.user", "Expected", "https://www.tiktok.com/@expected.user")
        self.assertEqual(
            validate_identity_binding({"accountReference": ""}, identity, allow_initial_bind=True),
            "expected.user",
        )
```

Add an async fake-page test where exactly one visible profile link has `href="/@expected.user"`, and rejection tests for zero or conflicting visible profile handles.

- [ ] **Step 2: Run the focused test and confirm the missing-module failure**

Run: `.venv/bin/python -m unittest -v test_overseas_tiktok_identity`

Expected: FAIL with `ModuleNotFoundError: app_core.overseas_tiktok_identity`.

- [ ] **Step 3: Implement normalization, public errors, and unique-page readback**

```python
@dataclass(frozen=True, slots=True)
class TikTokIdentity:
    handle: str
    display_name: str
    profile_url: str


class TikTokIdentityError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = str(error_code)
        self.public_message = str(message)
        super().__init__(message)


def validate_identity_binding(
    account: Mapping[str, Any],
    identity: TikTokIdentity,
    *,
    allow_initial_bind: bool,
) -> str:
    expected = normalize_tiktok_handle(account.get("accountReference"))
    actual = normalize_tiktok_handle(identity.handle)
    if not actual:
        raise TikTokIdentityError("tiktok_account_invalid", "TikTok 页面没有返回稳定账号标识")
    if not expected and allow_initial_bind:
        return actual
    if not expected or expected != actual:
        raise TikTokIdentityError("tiktok_account_identity_mismatch", "当前 TikTok 登录主体与已保存账号不一致")
    return actual
```

`read_tiktok_identity` must prefer a stable profile link `/@handle`; display name and avatar may supplement UI but may not satisfy identity by themselves. It must reject zero or multiple conflicting handles instead of guessing.

- [ ] **Step 4: Run focused tests**

Run: `.venv/bin/python -m unittest -v test_overseas_tiktok_identity`

Expected: PASS for URL normalization, initial bind, exact match, mismatch, zero candidate, and conflicting candidate cases.

- [ ] **Step 5: Commit the overseas-only identity core**

```bash
git add app_core/overseas_tiktok_identity.py test_overseas_tiktok_identity.py
git commit -m "feat(overseas): add TikTok account identity contract"
```

---

### Task 3: Bind Login and Silent Account Validation to the Same Identity

**Files:**
- Modify: `myUtils/login.py:499-634`
- Modify: `app_core/account_service.py:557-640`
- Modify: `test_overseas_tiktok_identity.py`
- Modify: `test_overseas_integration.py`

**Interfaces:**
- Consumes: `read_tiktok_identity`, `validate_identity_binding` from Task 2.
- Produces: `persist_tiktok_identity(account_id: int, identity: TikTokIdentity, *, allow_initial_bind: bool) -> None` in `app_core/overseas_tiktok_identity.py`.
- Changes: a saved TikTok browser account must have non-empty `accountReference` before formal publishing.

- [ ] **Step 1: Add failing login and validation routing tests**

```python
def test_tiktok_login_persists_stable_handle_after_account_row_exists(self):
    with patch("myUtils.login.read_tiktok_identity", new=AsyncMock(return_value=self.identity)), \
         patch("myUtils.login.save_login_account", return_value=61), \
         patch("myUtils.login.persist_tiktok_identity") as persist:
        result = asyncio.run(self.run_completed_browser_login(platform_type=6))
    self.assertIsNotNone(result)
    persist.assert_called_once_with(61, self.identity, allow_initial_bind=True)

def test_validate_accounts_marks_tiktok_identity_mismatch_invalid(self):
    with patch("app_core.account_service.validate_saved_tiktok_account") as validate:
        validate.side_effect = TikTokIdentityError(
            "tiktok_account_identity_mismatch", "当前主体不一致"
        )
        result = account_service.validate_accounts([61])
    self.assertEqual(result["accounts"][0]["status"], 0)
    self.assertEqual(result["authIssues"][61], "tiktok_account_identity_mismatch")
```

Also cover a missing session (`tiktok_session_missing`) and expired login (`tiktok_session_expired`).

- [ ] **Step 2: Run the focused tests and confirm routing failures**

Run:

```bash
.venv/bin/python -m unittest -v test_overseas_tiktok_identity test_overseas_integration
```

Expected: FAIL because login and account validation do not yet call the TikTok identity service.

- [ ] **Step 3: Persist identity after successful login without exposing the session**

In `_browser_cookie_gen`, for `platform_type == 6`, read the identity from the already authenticated page, save the normal account row, then bind the returned account ID:

```python
tiktok_identity = await read_tiktok_identity(page) if platform_type == 6 else None
account_id = save_login_account(...)
if account_id and tiktok_identity is not None:
    persist_tiktok_identity(account_id, tiktok_identity, allow_initial_bind=True)
```

If identity readback fails, delete the newly written session and do not leave a normal TikTok account row. Update mode must preserve the old binding until the new page proves the same handle.

- [ ] **Step 4: Route TikTok silent validation through the identity service**

Add `validate_saved_tiktok_account(account) -> TikTokIdentity` to the identity module. It must launch the existing isolated saved session without revealing the window, read the current handle, compare it with `accountReference`, and close all browser resources. In `account_service.validate_accounts`, route browser account type 6 to it before the generic `verify_saved_session` fallback.

The resulting account status rules are:

```text
same handle -> status=1
session missing / expired -> status=0 and stable auth issue
different handle -> status=0, preserve existing accountReference
```

- [ ] **Step 5: Run focused and account UI tests**

Run:

```bash
.venv/bin/python -m unittest -v test_overseas_tiktok_identity test_overseas_integration test_account_detection_ui
```

Expected: PASS; mismatched/expired TikTok accounts remain selectable only as red abnormal rows where the existing UI shows managed accounts, and they are excluded from publishable accounts.

- [ ] **Step 6: Commit shared account integration separately**

```bash
git add myUtils/login.py app_core/account_service.py app_core/overseas_tiktok_identity.py test_overseas_tiktok_identity.py test_overseas_integration.py
git commit -m "feat(shared): bind TikTok sessions to stable account identity"
```

This commit intentionally touches shared account code and must be reviewed by the integration line before merge.

---

### Task 4: Add the Strict TikTok Contract and Zero-Write Local Preflight

**Files:**
- Create: `app_core/overseas_tiktok_publish.py`
- Create: `test_overseas_tiktok_publish.py`
- Modify: `app_core/overseas_preflight.py:18-222`
- Modify: `test_overseas_integration.py`

**Interfaces:**
- Produces: `TikTokPublishError(error_code: str, message: str, *, receipt: Mapping[str, Any] | None = None, outcome_ambiguous: bool = False)`.
- Produces: `validate_tiktok_payload(payload: Mapping[str, Any], *, mode: str) -> dict[str, Any]`.
- Produces: `run_tiktok_local_preflight(payload: Mapping[str, Any]) -> dict[str, Any]`.
- Modes: exactly `preflight`, `platform_form_check`, `formal`.

- [ ] **Step 1: Write failing strict-contract tests**

```python
class TikTokPublishContractTests(unittest.TestCase):
    def test_local_preflight_returns_snapshot_without_opening_browser(self):
        with patch("app_core.overseas_tiktok_publish.sync_playwright") as browser:
            result = run_tiktok_local_preflight(self.valid_payload())
        browser.assert_not_called()
        self.assertEqual(result["phase"], "local_preflight_passed")
        self.assertFalse(result["receipt"]["platformWriteOccurred"])
        self.assertFalse(result["receipt"]["finalActionTriggered"])

    def test_unsupported_setting_is_rejected_before_platform_access(self):
        for key, value in (
            ("enableTimer", True),
            ("visibility", "private"),
            ("aiGenerated", True),
            ("coverPath", "/tmp/cover.png"),
            ("mentions", ["someone"]),
        ):
            payload = self.valid_payload()
            payload[key] = value
            with self.assertRaises(TikTokPublishError) as raised:
                validate_tiktok_payload(payload, mode="preflight")
            self.assertEqual(raised.exception.error_code, "tiktok_unsupported_publish_setting")
```

Add cases for multiple accounts, multiple videos, missing/unsupported video, abnormal account, missing `accountReference`, raw `@name` in body, duplicate normalized topics, and combined content over 2200 characters.

- [ ] **Step 2: Run the new tests and confirm the missing-module failure**

Run: `.venv/bin/python -m unittest -v test_overseas_tiktok_publish`

Expected: FAIL with `ModuleNotFoundError: app_core.overseas_tiktok_publish`.

- [ ] **Step 3: Implement one canonical prepared payload**

```python
TIKTOK_CONTENT_LIMIT = 2200
TIKTOK_MODES = frozenset({"preflight", "platform_form_check", "formal"})


def validate_tiktok_payload(payload: Mapping[str, Any], *, mode: str) -> dict[str, Any]:
    if mode not in TIKTOK_MODES:
        raise TikTokPublishError("tiktok_unsupported_publish_setting", "TikTok 执行模式无效")
    # Require type=6, video, exactly one account/session and one local video.
    # Require immediate public, no cover, AI declaration, collection, schedule, or mentions.
    # Normalize title/body/topics once and reject over-limit content without truncation.
    return {
        "accountId": account_id,
        "accountFile": str(session_path),
        "expectedAccountReference": expected_handle,
        "videoPath": str(video_path.resolve()),
        "title": title,
        "body": body,
        "topics": topics,
        "plainCaption": "\n\n".join(part for part in (title, body) if part),
        "visibility": "public",
        "mode": mode,
    }
```

Do not copy session paths into returned receipts. The public snapshot may contain account ID, video SHA-256, normalized text SHA-256, topic names, visibility and mode; it must not contain full body text or Cookie/session bytes.

- [ ] **Step 4: Implement local preflight and reroute type 6 only**

```python
def run_tiktok_local_preflight(payload: Mapping[str, Any]) -> dict[str, Any]:
    prepared = validate_tiktok_payload(payload, mode="preflight")
    return {
        "type": 6,
        "ok": True,
        "phase": "local_preflight_passed",
        "message": "TikTok 本地预检通过；未打开平台、未上传视频",
        "receipt": {
            "accountId": prepared["accountId"],
            "visibility": "public",
            "platformWriteOccurred": False,
            "finalActionTriggered": False,
            "contentId": None,
            "contentUrl": None,
            "publishedAt": None,
        },
    }
```

Change `run_overseas_preflight_sync` so platform type 6 immediately calls this function and never reads `PREFLIGHT_HANDLERS[6]`. Leave YouTube and Meta behavior unchanged.

- [ ] **Step 5: Prove default TikTok preflight has zero platform activity**

Run:

```bash
.venv/bin/python -m unittest -v test_overseas_tiktok_publish test_overseas_integration
```

Expected: PASS; the TikTok handler, Playwright launcher, uploader, session saver, and browser reveal mocks each have zero calls during default preflight.

- [ ] **Step 6: Commit the overseas local-preflight slice**

```bash
git add app_core/overseas_tiktok_publish.py app_core/overseas_preflight.py test_overseas_tiktok_publish.py test_overseas_integration.py
git commit -m "feat(overseas): make TikTok preflight local only"
```

---

### Task 5: Make the TikTok Form Adapter Verify Plain Caption and Official Topic Entities

**Files:**
- Modify: `uploader/tk_uploader/main.py:121-434`
- Modify: `test_overseas_video_publish.py`

**Interfaces:**
- Changes: `TiktokVideo(..., expected_account_reference: str, execution_mode: str)`.
- Produces: `prepare_form(page, base) -> dict[str, Any]`.
- Produces: `_fill_plain_caption(page, base, plain_caption: str) -> object`.
- Produces: `_append_official_topics(page, base, editor, topics: Sequence[str]) -> list[str]`.
- Produces: `_read_topic_entities(editor) -> list[str]`.
- Produces: `_verify_form_snapshot(page, base) -> dict[str, Any]`.
- Produces: `submit_once(page, base) -> dict[str, Any]`.

- [ ] **Step 1: Replace legacy plain-`#text` expectations with failing entity tests**

```python
def test_prepare_form_keeps_plain_caption_first_and_topics_as_entities(self):
    uploader = self.uploader(title="Title", description="Body", tags=["AI", "效率"])
    receipt = asyncio.run(uploader.prepare_form(self.page, self.base))
    self.assertEqual(self.editor.normalized_text_before_topics, "Title\n\nBody")
    self.assertEqual(receipt["topicEntities"], ["AI", "效率"])
    self.assertEqual(receipt["plainCaption"], "Title\n\nBody")

def test_plain_hashtag_text_never_satisfies_topic_entity_readback(self):
    self.editor.inner_text_value = "Title Body #AI"
    self.editor.entity_nodes = []
    with self.assertRaises(TikTokPublishError) as raised:
        asyncio.run(self.uploader(tags=["AI"]).prepare_form(self.page, self.base))
    self.assertEqual(raised.exception.error_code, "tiktok_topic_entity_missing")
```

Add failures for zero candidates (`tiktok_topic_candidate_missing`), multiple exact candidates (`tiktok_topic_candidate_ambiguous`), duplicate/wrong-order entities, editor count not equal to one, clear readback not empty, and final caption mismatch.

- [ ] **Step 2: Run uploader tests and confirm the legacy behavior fails them**

Run: `.venv/bin/python -m unittest -v test_overseas_video_publish`

Expected: FAIL because `_caption()` appends plain hashtags and `_fill_caption()` only compares flattened text.

- [ ] **Step 3: Split plain-caption writing from topic selection**

Implement this invariant:

```python
plain_caption = "\n\n".join(part.strip() for part in (title, description) if part.strip())
await editor.click()
await page.keyboard.press("ControlOrMeta+A")
await page.keyboard.press("Backspace")
if normalize(await read_editor_text(editor)):
    raise TikTokPublishError("tiktok_caption_clear_failed", "TikTok 文案框未能清空")
await page.keyboard.insert_text(plain_caption)
if normalize(await read_editor_text(editor)) != normalize(plain_caption):
    raise TikTokPublishError("tiktok_caption_readback_mismatch", "TikTok 正文回读不一致")
```

The editor locator must resolve to exactly one visible interactive editor. Do not catch a readback mismatch and silently try another editor; only selector-not-found may advance to the next selector.

- [ ] **Step 4: Append each structured topic at the real editor end**

For every normalized topic:

1. click the verified editor and press `ControlOrMeta+End`;
2. insert one separating space when needed, then type `#<topic>`;
3. wait until one candidate list is stable;
4. normalize visible candidate labels and require exactly one exact match;
5. click that candidate;
6. read editor entity nodes and require the target topic to appear exactly once at the expected index.

Candidate and entity selector families must be kept in named tuples in one module. The first real `platform_form_check` must inspect current TikTok Studio and update those tuples if the current DOM differs; do not add a plain-text fallback.

- [ ] **Step 5: Stop revealing the browser during normal preparation**

Remove the unconditional `reveal_page_window(page)` from `upload()`. Keep the browser headed but initially hidden/off-screen so that `_wait_for_manual_intervention()` can reveal the same page. Add tests proving ordinary preparation never calls `reveal_page_window`, while a security-intervention marker calls it once and resumes the same page without a second `_upload_file` call.

- [ ] **Step 6: Run focused adapter tests**

Run: `.venv/bin/python -m unittest -v test_overseas_video_publish`

Expected: PASS for unique editor, exact clear/write/readback, ordered official topic entities, public visibility, hidden normal flow, same-session verification resume, and one-click final action.

- [ ] **Step 7: Commit the overseas form adapter**

```bash
git add uploader/tk_uploader/main.py test_overseas_video_publish.py
git commit -m "feat(overseas): verify TikTok caption and topic entities"
```

---

### Task 6: Run Platform Form Check and Formal Publish Through One TikTok Service

**Files:**
- Modify: `app_core/overseas_tiktok_publish.py`
- Modify: `app_core/overseas_video_publish.py`
- Modify: `test_overseas_tiktok_publish.py`
- Modify: `test_overseas_publish_routing.py`

**Interfaces:**
- Produces: `run_tiktok_platform_sync(payload: Mapping[str, Any], *, mode: str, task_id: int) -> dict[str, Any]`.
- Consumes: `TiktokVideo.prepare_form` from Task 5 for both form check and formal mode.
- Formal-only: `TiktokVideo.submit_once` after exact account/form snapshot validation.

- [ ] **Step 1: Add failing shared-path and receipt tests**

```python
def test_platform_form_check_prepares_once_and_never_submits(self):
    uploader = self.fake_uploader()
    with patch("app_core.overseas_tiktok_publish.TiktokVideo", return_value=uploader):
        result = run_tiktok_platform_sync(self.payload(), mode="platform_form_check", task_id=71)
    uploader.prepare_form.assert_awaited_once()
    uploader.submit_once.assert_not_awaited()
    self.assertEqual(result["phase"], "platform_form_verified")
    self.assertTrue(result["receipt"]["platformWriteOccurred"])
    self.assertFalse(result["receipt"]["finalActionTriggered"])

def test_formal_uses_same_prepared_snapshot_then_submits_once(self):
    uploader = self.fake_uploader()
    with patch("app_core.overseas_tiktok_publish.TiktokVideo", return_value=uploader):
        result = run_tiktok_platform_sync(self.formal_payload(), mode="formal", task_id=72)
    uploader.prepare_form.assert_awaited_once()
    uploader.submit_once.assert_awaited_once()
    self.assertIn(result["phase"], {"platform_accepted", "published_readback_confirmed"})
```

Add an identity mismatch before upload, form snapshot mismatch before `Post`, manual verification event, explicit platform failure, success feedback, exact content readback, and unverified post result.

- [ ] **Step 2: Run focused tests and confirm the new runner is missing**

Run:

```bash
.venv/bin/python -m unittest -v test_overseas_tiktok_publish test_overseas_publish_routing
```

Expected: FAIL because `run_tiktok_platform_sync` and explicit type-6 routing do not exist.

- [ ] **Step 3: Implement one async browser session for both modes**

The runner must:

```python
prepared = validate_tiktok_payload(payload, mode=mode)
identity = await read_tiktok_identity(page)
validate_identity_binding(account_snapshot, identity, allow_initial_bind=False)
form_receipt = await uploader.prepare_form(page, base)
verify_authorized_snapshot(prepared, form_receipt)
if mode == "platform_form_check":
    return platform_form_receipt(form_receipt)
return await uploader.submit_once(page, base)
```

Use the same context and page through identity, upload, fields, optional manual verification, and final action. Do not save a changed session in `preflight`; form check/formal may refresh the existing isolated session only after identity remains matched.

- [ ] **Step 4: Emit stable events before and after the final action**

Required task events:

```text
tiktok_platform_form_started
tiktok_waiting_user_verification
tiktok_user_verification_resolved
tiktok_platform_form_verified
tiktok_final_action_triggered
tiktok_platform_accepted
tiktok_published_readback_confirmed
tiktok_publish_outcome_ambiguous
```

`tiktok_final_action_triggered` must be written immediately before the single `Post` click. If anything after that point raises without explicit platform rejection, raise `TikTokPublishError("tiktok_publish_outcome_unknown", ..., outcome_ambiguous=True, receipt={"finalActionTriggered": True, ...})`.

- [ ] **Step 5: Remove TikTok from the legacy combined formal route**

Keep `app_core/overseas_video_publish.py` as the compatibility entry for YouTube only. Type 6 must not remain callable through both `HANDLERS` and the new service.

- [ ] **Step 6: Run focused service and routing tests**

Run:

```bash
.venv/bin/python -m unittest -v test_overseas_tiktok_publish test_overseas_publish_routing test_overseas_video_publish
```

Expected: PASS; every type-6 mode has exactly one route and formal/form-check share the same `prepare_form` call.

- [ ] **Step 7: Commit the overseas execution service**

```bash
git add app_core/overseas_tiktok_publish.py app_core/overseas_video_publish.py test_overseas_tiktok_publish.py test_overseas_publish_routing.py
git commit -m "feat(overseas): add TikTok controlled execution modes"
```

---

### Task 7: Integrate Controlled CLI, Authorization, Receipts, and Duplicate Protection

**Files:**
- Modify: `app_core/controlled_publish.py:32-410,691-1085`
- Modify: `app_core/publish_service.py:61-430,677-1075`
- Modify: `app_core/task_service.py:52-180,1955-2245`
- Modify: `test_controlled_publish.py`
- Modify: `test_task_service.py`
- Modify: `test_overseas_publish_routing.py`

**Interfaces:**
- Adds controlled request mode: `platform_form_check`.
- Adds request key: `platformFormCheckConfirmed`, required to be literal `true` for that mode.
- Adds runtime mode: `platform_form_check` and task mode: `oneclick_platform_form_check`.
- Adds type-6 expected identity field: `tiktokExpectedAccountReference`.
- Adds TikTok receipt projection fields listed below.

- [ ] **Step 1: Add failing controlled request tests**

```python
def test_tiktok_preflight_builds_local_only_payload(self):
    payload = build_controlled_payloads(self.tiktok_request(mode="preflight"), accounts=[self.tiktok_account])[0]
    self.assertEqual(payload["runtimeMode"], "preflight")
    self.assertTrue(payload["debugDryRun"])
    self.assertFalse(payload["backgroundMode"])
    self.assertEqual(payload["tiktokExpectedAccountReference"], "expected.user")

def test_platform_form_check_requires_explicit_confirmation(self):
    request = self.tiktok_request(mode="platform_form_check")
    with self.assertRaises(ControlledPublishError) as raised:
        build_controlled_payloads(request, accounts=[self.tiktok_account])
    self.assertEqual(raised.exception.error_code, "tiktok_platform_form_check_confirmation_required")

def test_tiktok_fingerprint_changes_when_body_topic_video_or_account_identity_changes(self):
    base = build_controlled_payloads(self.tiktok_request(), accounts=[self.tiktok_account])
    for changed in self.changed_tiktok_payloads(base):
        self.assertNotEqual(scope_fingerprint(base), scope_fingerprint(changed))
```

Add failures for account status not 1, missing `accountReference`, non-browser auth mode, raw `@` body, more than one TikTok target, non-public settings, any schedule, and formal authorization mismatch.
Add a test that TikTok rejects the generic `direct` mode in this first release; only a successful local preflight followed by `formal` may reach the final action.

- [ ] **Step 2: Add failing receipt and stale-task tests**

```python
def test_tiktok_receipt_projection_keeps_only_public_fields(self):
    task_service.mark_platform_result(
        self.task_id, 6, ok=True, message="TikTok 平台已接受",
        content_type="video", event_type="platform_publish",
        receipt={
            "accountId": 61,
            "visibility": "public",
            "platformWriteOccurred": True,
            "finalActionTriggered": True,
            "contentId": "741234",
            "contentUrl": "https://www.tiktok.com/@expected.user/video/741234",
            "publishedAt": "2026-08-28T10:00:00+08:00",
            "cookie": "must-not-survive",
        },
    )
    receipt = json.loads(task_service.get_task(self.task_id)["items"][0]["receiptJson"])
    self.assertNotIn("cookie", receipt)
    self.assertEqual(receipt["contentId"], "741234")

def test_stale_tiktok_task_after_final_action_becomes_ambiguous(self):
    self.record_event("tiktok_final_action_triggered")
    self.expire_worker_lease()
    self.assertTrue(task_service.reconcile_stale_controlled_task(self.task_id, now=self.later))
    item = task_service.get_task(self.task_id)["items"][0]
    self.assertEqual(item["errorCode"], "tiktok_publish_outcome_unknown")
```

Also prove that a stale task without the final event becomes an ordinary failed task and that the same fingerprint is blocked after either `tiktok_final_action_triggered` or `tiktok_publish_outcome_ambiguous`.

- [ ] **Step 3: Run the focused shared tests and confirm failures**

Run:

```bash
.venv/bin/python -m unittest -v test_controlled_publish test_task_service test_overseas_publish_routing
```

Expected: FAIL because the new mode, TikTok receipt projection, event-aware stale reconciliation, and duplicate rules are absent.

- [ ] **Step 4: Build the TikTok target payload before any task starts**

In `build_controlled_payloads`:

```python
if platform_type == 6:
    if tiktok_target_count > 1:
        raise ControlledPublishError("tiktok_target_invalid", "一次 TikTok 任务只能选择一个账号")
    if int(account.get("status") or 0) != 1 or not str(account.get("accountReference") or "").strip():
        raise ControlledPublishError("tiktok_account_invalid", "TikTok 账号未通过同一主体检测")
    if enable_timer or settings_visibility != "public":
        raise ControlledPublishError("tiktok_unsupported_publish_setting", "TikTok 首轮只支持立即公开发布")
    payload["tiktokExpectedAccountReference"] = str(account["accountReference"])
    payload["tiktokControlledPublish"] = True
```

Set `runtimeMode` to `platform_form_check` only for the explicit mode. Set `overseasVideoPublishConfirmed=True` only for `formal`, never for preflight or form check; reject generic `direct` for type 6 in this first release. Add `tiktokExecutionIntent="formal_public"` to both the TikTok preflight and its matching formal payload, and `tiktokExecutionIntent="platform_form_check"` to the developer check. Include `tiktokExpectedAccountReference` and `tiktokExecutionIntent` in `scope_fingerprint`; this keeps the approved preflight/formal pair equal while preventing a form-check snapshot from being reused as formal authorization.

- [ ] **Step 5: Add the explicit form-check task path**

`submit_request` accepts `platform_form_check` only when:

```json
{
  "mode": "platform_form_check",
  "platformFormCheckConfirmed": true,
  "targets": [{"platform": "TikTok", "accountId": 61, "schedule": null, "settings": {"visibility": "public"}}]
}
```

It creates `oneclick_platform_form_check` and calls the same TikTok service without generating or consuming formal authorization. Mixed-platform form checks are rejected. `start_desktop_publish` must create a separate worker label and must not classify this mode as normal preflight or formal publish.

- [ ] **Step 6: Route results and stable errors through the shared task service**

Add a TikTok receipt whitelist containing exactly:

```python
_TIKTOK_RECEIPT_FIELDS = {
    "accountId", "visibility", "platformWriteOccurred", "finalActionTriggered",
    "contentId", "contentUrl", "publishedAt", "topicEntities", "phase",
}
```

Map `contentId/contentUrl/publishedAt` to existing item fields. Add `tiktok_publish_failed` as the type-6 fallback in `_failure_error_code`; preserve a concrete `TikTokPublishError.error_code` when present.

When the exception has `outcome_ambiguous=True`, the task item is terminal failed with `errorCode=tiktok_publish_outcome_unknown`, the public task `stage` is `ambiguous`, the receipt keeps `finalActionTriggered=true`, and no remaining final action is attempted for the same fingerprint.

- [ ] **Step 7: Extend project-task status without exposing sensitive data**

`project_task` must map:

```text
tiktok local-preflight success receipt -> stage=local_preflight_passed
tiktok_waiting_user_verification -> stage=waiting_verification, userAction.type=tiktok_verification
tiktok_platform_form_verified -> stage=platform_form_verified
tiktok_final_action_triggered -> stage=reconciling
tiktok_platform_accepted -> stage=platform_accepted
tiktok_published_readback_confirmed -> stage=published_readback_confirmed
tiktok_publish_outcome_ambiguous -> stage=ambiguous
```

The user-action message may say which platform and that the same browser is waiting; it must not contain QR pixels, session paths, handles beyond the public saved account label, or full post text.

- [ ] **Step 8: Run controlled and task tests**

Run:

```bash
.venv/bin/python -m unittest -v test_controlled_publish test_task_service test_overseas_publish_routing
```

Expected: PASS for local preflight, explicit form check, formal authorization consumption, fingerprint invalidation, safe receipt projection, manual verification projection, stale worker closure, ambiguous outcome, and duplicate blocking.

- [ ] **Step 9: Commit shared controlled-publish integration separately**

```bash
git add app_core/controlled_publish.py app_core/publish_service.py app_core/task_service.py test_controlled_publish.py test_task_service.py test_overseas_publish_routing.py
git commit -m "feat(shared): route TikTok through controlled publish service"
```

This commit intentionally touches shared task and CLI paths and must be reviewed by the integration line before merge.

---

### Task 8: Align Capability Text and Complete Offline Regression

**Files:**
- Modify: `app_core/oneclick_capabilities.py:152-155`
- Modify: `SOURCE_OF_TRUTH.md`
- Modify: relevant tests from Tasks 2-7 if regression exposes contract gaps.

**Interfaces:**
- Changes user-facing TikTok capability note from platform-upload preflight to local-only default check.
- Records exact offline evidence without claiming real publishing.

- [ ] **Step 1: Add a failing capability-text assertion**

In the existing capabilities test, require the TikTok video note to contain “默认本地检查” and to exclude wording that says every preflight uploads to TikTok or stops at `Post`.

- [ ] **Step 2: Run the focused overseas suite**

Run:

```bash
.venv/bin/python -m unittest -v \
  test_overseas_tiktok_identity \
  test_overseas_tiktok_publish \
  test_overseas_integration \
  test_overseas_publish_routing \
  test_overseas_video_publish \
  test_meta_browser_publish \
  test_controlled_publish \
  test_task_service \
  test_account_detection_ui
```

Expected: PASS. A local preflight test must still assert zero browser/upload/session-write calls.

- [ ] **Step 3: Run the full repository regression and source-client smoke test**

Run:

```bash
.venv/bin/python -m unittest discover -p 'test_*.py'
QT_QPA_PLATFORM=offscreen .venv/bin/python desktop_native_app.py --ui-test
git diff --check
```

Expected: all tests pass, UI smoke exits 0, and `git diff --check` prints nothing. If the full suite has an unrelated pre-existing failure, save the exact test and baseline evidence; do not label the whole suite passed.

- [ ] **Step 4: Update factual status only**

Record in `SOURCE_OF_TRUTH.md`:

- exact commits tested;
- focused and full test counts/results;
- source UI smoke result;
- default TikTok preflight proven local-only;
- real platform form check not yet run at this point;
- formal public posting not verified.

Do not bump the version and do not add package paths.

- [ ] **Step 5: Run the workstream scope checker**

Run:

```bash
.venv/bin/python tools/check_workstream_scope.py --stream overseas --base origin/main
```

Expected: overseas files pass; shared account/CLI/task files may produce `REVIEW_REQUIRED`. Treat that as an integration-review requirement, not as permission to self-merge.

- [ ] **Step 6: Commit capability wording and verified status**

```bash
git add app_core/oneclick_capabilities.py SOURCE_OF_TRUTH.md
git commit -m "docs(overseas): record TikTok local preflight boundary"
```

---

### Task 9: Perform the Authorized Real Platform Check Without Posting

**Files:**
- Create: `docs/verification/2026-08-28-tiktok-controlled-video-publish.md`
- Modify: `SOURCE_OF_TRUTH.md`
- Modify: `uploader/tk_uploader/main.py` and its focused tests only if the current TikTok DOM disproves a selector assumption.

**Interfaces:**
- Uses the current saved TikTok test account.
- Uses controlled CLI `preflight`, task status readback, and explicit `platform_form_check`.
- Must not use `formal`, `direct`, or click `Post`.

- [ ] **Step 1: Close competing clients and start the source client against the shared test data**

Confirm no packaged client or another source process owns the account database/session files. Start the source client using the project’s established shared-source test-data mode; do not copy Cookie files into the worktree or request JSON.

- [ ] **Step 2: Re-login or update the current TikTok account if needed**

Use Account Management. The user completes any credentials, QR, CAPTCHA, or legal prompt. After login, verify the account row has a non-empty stable `accountReference`, status 1, correct display name, and refreshed public avatar.

- [ ] **Step 3: Restart the source client and run silent account validation**

Expected evidence:

```text
same saved account ID
same accountReference handle
status=1 after restart
no visible browser unless TikTok requires user verification
```

If identity differs, stop with `tiktok_account_identity_mismatch`; do not upload.

- [ ] **Step 4: Run a normal controlled preflight and prove it is local only**

Create a request JSON containing the approved test manifest, one TikTok account ID, `mode=preflight`, no schedule, and `visibility=public`. Run the existing controlled CLI and poll the returned `taskId`.

Expected terminal JSON:

```json
{
  "phase": "preflight",
  "status": "success",
  "stage": "local_preflight_passed",
  "platforms": [{
    "platform": "TikTok",
    "status": "success",
    "receipt": {
      "visibility": "public",
      "platformWriteOccurred": false,
      "finalActionTriggered": false
    }
  }]
}
```

Also confirm no TikTok window opens and no video appears in TikTok Studio from this step.

- [ ] **Step 5: Run one explicitly confirmed platform form check**

Submit the same manifest/account with:

```json
{
  "mode": "platform_form_check",
  "platformFormCheckConfirmed": true
}
```

The session must upload one test video, clear and write the plain caption once, select each official topic, read back ordered topic entities, set/read public visibility, and prove `Post` is enabled. Then it must stop and close without clicking `Post`.

Expected terminal receipt:

```json
{
  "phase": "platform_form_verified",
  "status": "success",
  "receipt": {
    "visibility": "public",
    "platformWriteOccurred": true,
    "finalActionTriggered": false,
    "contentId": null,
    "contentUrl": null,
    "publishedAt": null
  }
}
```

- [ ] **Step 6: If current DOM requires selector correction, use TDD before editing**

Capture only non-sensitive DOM structure needed to identify editor/candidate/entity/visibility/Post controls. Add a failing fake-page regression that reproduces the observed structure, make the smallest selector/readback correction, rerun:

```bash
.venv/bin/python -m unittest -v test_overseas_video_publish test_overseas_tiktok_publish
```

Then repeat only `platform_form_check`; never switch to formal mode to prove a selector.

- [ ] **Step 7: Write the verification report and factual status**

The report must record:

- source commit;
- local preflight task ID and receipt;
- form-check task ID and receipt;
- saved account ID only, not Cookie/session path;
- whether manual verification appeared;
- topic entities and public visibility readback result;
- `finalActionTriggered=false`;
- explicit statement: “未点击 Post，未验证公开发布”。

Update `SOURCE_OF_TRUTH.md` with the same boundary.

- [ ] **Step 8: Commit real pre-submit evidence without secrets**

```bash
git add docs/verification/2026-08-28-tiktok-controlled-video-publish.md SOURCE_OF_TRUTH.md
git commit -m "test(overseas): verify TikTok form before final post"
```

---

## Final Review Gate

- [ ] Re-read the approved spec and verify every requirement maps to one task and one test or real-platform check.
- [ ] Run `rg -n "TB[D]|TO[D]O|implement[ ]later|暂时[先]|后续[再]补" docs/superpowers/plans/2026-08-28-tiktok-controlled-video-publish.md` and require no matches.
- [ ] Verify the interface names are consistent across tasks: `TikTokIdentity`, `TikTokIdentityError`, `TikTokPublishError`, `validate_tiktok_payload`, `run_tiktok_local_preflight`, `run_tiktok_platform_sync`, `platform_form_check`, `tiktok_final_action_triggered`.
- [ ] Verify no task instructs a public TikTok post during this implementation acceptance.
- [ ] Verify shared-file commits remain separate and are marked for integration review.
- [ ] Verify the final report says exactly what passed locally, what passed on the real form, and that formal publishing remains unverified.
