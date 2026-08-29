# Facebook Page Controlled Reel Publishing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在一键发中新增一个只面向 Facebook Page 的单视频、立即公开 Reel 受控发布通道；桌面 UI、CLI 和 MCP 共用同一服务，并以精确 Page ID、一次性授权、最终按钮最多点击一次及同一 Page 唯一新增 Reel ID/URL 回读作为安全闭环。

**Architecture:** 登录阶段把一个 `type=9` 账号严格绑定为一个稳定 Page ID；受控服务从账号记录生成不可由调用方伪造的 Page 目标和发布意图指纹。预检与正式发布共用 Page 表单适配器，正式任务在启动浏览器前原子消费授权、创建任务并占用 Page 级防重 claim；点击前保存完整内容列表基线，点击后只以同一 Page 唯一新增 Reel 的平台 ID/URL 判成功。桌面 UI、CLI 和 MCP 只负责调用这一服务和读取统一任务回执，不复制浏览器逻辑。

**Tech Stack:** Python 3.12、PyQt6、SQLite、Playwright、Meta Business Suite 可见浏览器会话、`unittest`、现有 controlled publish / task service / content project gateway / MCP 基础设施。

**Spec:** `docs/superpowers/specs/2026-08-29-facebook-page-publish-design.md`

## Global Constraints

- 首轮只支持 Facebook Page；不支持个人主页、专业模式个人账号或 Instagram 联动。
- 首轮只允许一个正常 `type=9` Page 账号、一个本地视频、立即公开 Reel；定时、多视频、图文、封面、AI 声明、地点、商品、共创和广告均在打开浏览器前拒绝。
- 一个 Facebook Page V1 请求只能包含这一条 type 9 目标，不与其他平台目标混合执行。
- 调用方只能传一键发内部 `accountId`，不能传 Page ID、Cookie、密码、验证码、二维码、令牌或会话内容。
- 预检可能向平台写入临时上传内容，但绝不点击最终发布按钮；回执必须区分 `platformWriteOccurred` 与 `finalActionTriggered`。
- 正式发布必须绑定成功预检 task、预检回执哈希、完整发布意图指纹和一次性短期授权；Facebook V1 禁止 `direct`。Page 级重放保护另用不含本地 `accountId` 的 `facebookReplayFingerprint`，防止同一 Page 借另一条本地记录绕过。
- 最终按钮点击最多一次；进入 `final_action_claimed` 后未取得唯一作品回读时默认结果不明并阻止自动重发。
- 只有同一 Page 内容列表中唯一新增 Reel 的真实 `reelId`、URL 和发布时间回读成功，才能标记发布成功；页面跳转、通用成功文字或本地 task ID 均不算平台回执。
- `safe_failed` 和经只读核对证明未发布的 `confirmed_not_published` 保留历史但释放活动防重槽；`final_action_claimed`、`final_action_clicked`、`ambiguous` 和 `succeeded` 持续阻止自动重发。
- 人工验证必须继续同一浏览器上下文、同一 Page 和同一上传；等待状态的 `errorCode` 为空，动作码固定为 `facebook_verification_required`。
- 密码、Cookie、Page Access Token、验证码、二维码和完整 storage-state 不得进入命令行、内容包、任务 JSON、普通日志或 Git。
- 海外专属文件在当前功能分支提交；共享核心改动按“Page 身份/数据库”“受控任务/授权”“发布中心/接口”拆成独立共享提交，转交集成线审查，不在功能分支自行合并 `main`。
- 功能分支不升级版本号、不打包、不替换已安装客户端。
- 所有实现先写失败测试，再做最小实现；离线测试、源码启动、真实登录、真实预检和公开发布必须分层报告。
- 本计划的真实平台步骤只定义验收程序，不构成本轮登录、上传或公开发布授权。

---

## File Structure

### New files

- `app_core/overseas_meta_errors.py` — Facebook Page 稳定异常类型和安全回执投影。
- `app_core/overseas_meta_page_identity.py` — Page 发现、选择、精确激活和保存绑定核对。
- `app_core/overseas_meta_content.py` — 结构化标题/正文/话题到唯一 Meta caption 与哈希的纯函数。
- `uploader/meta_uploader/page_form.py` — Page、视频、文案、公开范围和最终按钮的共用填写/回读适配器。
- `uploader/meta_uploader/content_list.py` — 同一 Page 内容列表基线、分页完整性和唯一新增 Reel 回读。
- `test_facebook_page_identity.py` — Page 身份纯函数及页面发现合同。
- `test_facebook_page_content.py` — 最终 caption 顺序、话题规范化和哈希合同。
- `test_facebook_page_database.py` — 旧记录迁移、唯一索引和 Page 账号原子保存。
- `test_facebook_page_login.py` — Facebook-only 登录、0/1/多 Page 选择和重启核对。
- `test_facebook_page_controlled_publish.py` — 输入标准化、指纹、预检授权和原子 claim。
- `test_facebook_page_publish.py` — 共用表单、点击硬门、内容基线和唯一 Reel 回读。
- `test_facebook_page_publish_service.py` — Page 专用执行路由、worker 租约和上下文贯穿。
- `test_facebook_page_task_service.py` — 安全回执、生命周期、结果不明和只读核对。
- `docs/superpowers/reports/2026-08-30-facebook-page-publish-verification.md` — 离线、源码与真实平台分层验收证据。

### Modified overseas-owned files

- `app_core/overseas_browser_publish.py` — type 9 正式任务改走 Page 专用执行器并传入 `task_id`。
- `app_core/overseas_preflight.py` — type 9 预检改走同一 Page 表单适配器并传入 `task_id`。
- `uploader/meta_uploader/main.py` — Instagram 旧通道与 Facebook Page V1 拆分；type 9 不再以通用路由或文字判成功。
- `test_meta_browser_publish.py` — 保留 Instagram 兼容边界并证明 type 9 不再使用旧布尔确认。
- `test_overseas_publish_routing.py` — Facebook Page 预检/正式专用路由测试。

### Modified shared files — identity/database commit

- `app_core/database.py` — 旧 `type=9` Page 绑定迁移和非空 Page ID 部分唯一索引。
- `app_core/account_service.py` — 一 Page 一账号原子 upsert、可发布过滤和重新绑定状态。
- `app_core/login_service.py` — Facebook Page 独立登录会话和多 Page 选择回调。
- `app_core/account_browser_service.py` — 打开后台前精确激活已保存 Page。
- `app_core/oneclick_authorization.py` — 保存会话的只读检测返回精确 Page 身份，不再丢失为通用布尔值。
- `myUtils/login.py` — 停止 Facebook 登录无条件创建 Instagram/Facebook 两行。
- `myUtils/auth.py` — 保存会话核验返回 Page 身份，不再只返回通用布尔值。
- `ui/login_dialog.py` — Page 选择列表、取消和稳定错误提示。
- `ui/account_page.py` — 正常 Page 展示及需重新绑定红字状态。
- `test_account_detection_ui.py`、`test_overseas_integration.py` — 共享登录与账号界面回归。

### Modified shared files — controlled task/authorization commit

- `app_core/controlled_publish.py` — Facebook 输入合同、发布意图指纹、回执哈希、原子授权/任务/claim 和只读核对入口。
- `app_core/publish_service.py` — Page 专用预检/正式 worker 服务和 claim 强制检查。
- `app_core/task_service.py` — Facebook 生命周期事件、安全回执和任务投影。
- `utils/publish_observer.py` — Page task ID 和稳定进度事件贯穿，不记录页面敏感信息。
- `test_controlled_publish.py`、`test_controlled_publish_resilience.py`、`test_publish_service.py`、`test_task_service.py` — 共享任务回归。

### Modified shared files — UI/CLI/MCP commit

- `ui/publish_page.py` — Facebook 正式确认改走受控授权，不再写旧 Meta 布尔字段。
- `desktop_native_app.py` — 只读 Facebook 结果核对 CLI 动作和统一 JSON 输出。
- `app_core/controlled_publish_process.py` — 子进程契约与统一任务回读。
- `app_core/content_project_gateway.py` — Facebook direct 例外、预检/授权/正式/核对转发。
- `app_core/oneclick_mcp_server.py` — 不暴露 Page ID 或 Meta 布尔字段的同一服务工具合同。
- `test_publish_page.py`、`test_controlled_publish_process.py`、`test_content_project_gateway.py`、`test_oneclick_mcp_server.py` — 三入口收口回归。

---

### Task 1: Freeze Facebook Page Errors, Safe Receipts and Identity Contracts

**Files:**
- Create: `app_core/overseas_meta_errors.py`
- Create: `app_core/overseas_meta_page_identity.py`
- Create: `app_core/overseas_meta_content.py`
- Create: `test_facebook_page_identity.py`
- Create: `test_facebook_page_content.py`

**Interfaces:**
- Produces: `FacebookPagePublishError(error_code: str, message: str, *, receipt: Mapping[str, object] | None = None, outcome_ambiguous: bool = False)`.
- Produces: `project_facebook_page_receipt(receipt: object) -> dict[str, object]`.
- Produces: `FacebookPageIdentity(page_id: str, page_name: str, avatar_url: str = "", can_manage_content: bool = False)`.
- Produces: `normalize_facebook_page_id(value: object) -> str`.
- Produces: `resolve_facebook_page_selection(pages: Iterable[FacebookPageIdentity], selected_page_id: str | None = None) -> FacebookPageIdentity`.
- Produces: `async def discover_manageable_facebook_pages(page) -> tuple[FacebookPageIdentity, ...]`.
- Produces: `async def validate_facebook_page_binding(page, account: Mapping[str, Any]) -> FacebookPageIdentity`.
- Produces: `async def activate_saved_facebook_page(page, expected_page_id: str) -> FacebookPageIdentity`.
- Produces: `facebook_page_v1_enabled(environ: Mapping[str, str] = os.environ) -> bool`; only exact `ONECLICK_ENABLE_FACEBOOK_PAGE_V1=1` enables it.
- Produces: `build_facebook_page_caption(*, title: str, body: str, topics: Iterable[str]) -> str`.
- Produces: `facebook_page_caption_sha256(caption: str) -> str`.
- Contract: Page 名称只用于显示；所有选择和核对以规范化非空 Page ID 为准；安全回执只能保留设计批准的非敏感字段。

- [ ] **Step 1: Write the failing error and identity tests**

```python
class FacebookPageIdentityTests(unittest.IsolatedAsyncioTestCase):
    def test_zero_pages_is_not_found(self):
        with self.assertRaises(FacebookPagePublishError) as raised:
            resolve_facebook_page_selection(())
        self.assertEqual(raised.exception.error_code, "facebook_page_not_found")

    def test_multiple_pages_require_an_explicit_page_id(self):
        pages = (
            FacebookPageIdentity("1001", "同名主页", can_manage_content=True),
            FacebookPageIdentity("1002", "同名主页", can_manage_content=True),
        )
        with self.assertRaises(FacebookPagePublishError) as raised:
            resolve_facebook_page_selection(pages)
        self.assertEqual(raised.exception.error_code, "facebook_page_selection_required")

    def test_same_name_never_substitutes_for_saved_page_id(self):
        pages = (
            FacebookPageIdentity("1001", "同名主页", can_manage_content=True),
            FacebookPageIdentity("1002", "同名主页", can_manage_content=True),
        )
        selected = resolve_facebook_page_selection(pages, "1002")
        self.assertEqual(selected.page_id, "1002")
```

Add exact cases for one Page auto-selection, unknown selected Page, duplicate Page IDs, blank/non-scalar Page IDs, missing content permission, saved Page mismatch, exact Page activation, and a receipt containing Cookie/token/QR/body fields whose projection drops every forbidden field.
Add feature-gate cases proving the default is disabled, only exact `ONECLICK_ENABLE_FACEBOOK_PAGE_V1=1` enables it, and request/manifest/content data cannot override the process-local gate.
In `test_facebook_page_content.py`, freeze this exact caption contract: non-empty title, body and topic line are joined with one blank line; structured topics strip one leading `#`, reject invalid empty/multiline values, de-duplicate exact normalized topics in first-seen order, and render as one space-separated hashtag line. Title/body text is preserved without semantic de-duplication. Raw body `@`/`#` remains plain text and never creates a verified mention/topic receipt. The caption hash is SHA-256 of the final UTF-8 caption bytes.

- [ ] **Step 2: Run the focused test and prove the modules are missing**

Run: `../../.venv/bin/python -m unittest -v test_facebook_page_identity test_facebook_page_content`

Expected: FAIL with `ModuleNotFoundError` for `app_core.overseas_meta_page_identity` or `app_core.overseas_meta_errors`.

- [ ] **Step 3: Implement the stable exception and receipt allowlist**

```python
class FacebookPagePublishError(RuntimeError):
    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        receipt: Mapping[str, object] | None = None,
        outcome_ambiguous: bool = False,
    ) -> None:
        self.error_code = str(error_code)
        self.public_message = str(message)
        self.receipt = project_facebook_page_receipt(receipt or {})
        self.outcome_ambiguous = bool(outcome_ambiguous)
        super().__init__(self.public_message)
```

`project_facebook_page_receipt` may retain only `accountId`, `pageId`, `pageName`, `videoName`, `videoSize`, `videoSha256`, `captionSha256`, `visibility`, `phase`, `platformWriteOccurred`, `finalActionTriggered`, `finalButtonEnabled`, `reelId`, `url`, `publishedAt`, `baselineHash` and `formSnapshotHash`. It ignores every non-allowlisted key, including Cookie、token、二维码、验证码、完整正文或 storage-state；an allowlisted field with a nested/invalid type must raise instead of being serialized.

- [ ] **Step 4: Implement Page identity normalization and pure selection**

```python
@dataclass(frozen=True, slots=True)
class FacebookPageIdentity:
    page_id: str
    page_name: str
    avatar_url: str = ""
    can_manage_content: bool = False


def normalize_facebook_page_id(value: object) -> str:
    page_id = str(value or "").strip()
    if not page_id or not page_id.isdigit():
        raise FacebookPagePublishError(
            "facebook_page_identity_mismatch",
            "Facebook Page 身份无效，已停止操作。",
        )
    return page_id
```

`resolve_facebook_page_selection` must deduplicate exact Page IDs, reject all entries without content permission, auto-select only when exactly one manageable Page exists, and require an explicit ID when multiple Page records remain. It must never select by name or list position.

- [ ] **Step 5: Implement injectable DOM discovery, validation and activation**

Keep all selectors and page reads inside `discover_manageable_facebook_pages`, `validate_facebook_page_binding` and `activate_saved_facebook_page`. Accept an injected Playwright-like `page` so tests use fakes. Activation must read the selected Page ID back after the switch and raise `facebook_page_identity_mismatch` or `facebook_page_content_permission_missing` before any upload.

- [ ] **Step 6: Run the focused identity and caption-contract tests**

Run: `../../.venv/bin/python -m unittest -v test_facebook_page_identity test_facebook_page_content`

Expected: PASS for 0/1/multiple Page selection, same-name separation, permission loss, identity mismatch, exact activation, safe receipt projection, final-caption ordering and caption hashing.

- [ ] **Step 7: Commit the overseas-only domain contract**

```bash
git add \
  app_core/overseas_meta_errors.py \
  app_core/overseas_meta_page_identity.py \
  app_core/overseas_meta_content.py \
  test_facebook_page_identity.py \
  test_facebook_page_content.py
git commit -m "feat(overseas): define Facebook Page identity contract"
```

---

### Task 2: Migrate Legacy Page Rows and Persist One Page per Account

**Files:**
- Modify: `app_core/database.py`
- Modify: `app_core/account_service.py`
- Create: `test_facebook_page_database.py`
- Modify: `test_account_detection_ui.py`

**Interfaces:**
- Produces: database migration for a partial unique index on non-empty `type=9` `accountReference`.
- Produces: `validate_saved_facebook_page_account(account: Mapping[str, Any]) -> str`.
- Produces: `save_facebook_page_browser_account(*, profile_name: str, storage_file_name: str, identity: FacebookPageIdentity, record_id: int | None = None, expected_account: Mapping[str, Any] | None = None, avatar_file_name: str | None = None) -> int`.
- Changes: `list_publishable_accounts()` excludes type 9 rows unless `status=1`, Page ID is non-empty and a session reference exists.
- Contract: migration preserves all rows and shared session/avatar references; it never deletes an old account or creates a type 8 Instagram row.

- [ ] **Step 1: Write the failing migration and persistence tests**

```python
class FacebookPageDatabaseTests(unittest.TestCase):
    def test_legacy_blank_page_reference_is_preserved_but_not_publishable(self):
        account_id = self.insert_account(type=9, status=1, account_reference="")
        ensure_schema()
        row = self.read_account(account_id)
        self.assertEqual(row["status"], 0)
        self.assertEqual(row["accountReference"], "")
        self.assertNotIn(account_id, self.publishable_ids())

    def test_duplicate_page_reference_keeps_lowest_id_and_requires_rebind_for_later_rows(self):
        first = self.insert_account(type=9, status=1, account_reference=" 1001 ")
        second = self.insert_account(type=9, status=1, account_reference="1001")
        ensure_schema()
        self.assertEqual(self.read_account(first)["accountReference"], "1001")
        self.assertEqual(self.read_account(second)["accountReference"], "")
        self.assertEqual(self.read_account(second)["status"], 0)
```

Add exact cases for idempotent migration, database rejection of a second active type 9 Page ID, another platform using the same reference, repeated save of the same Page updating one row, a bound record refusing a different Page, a legacy blank record binding once, session-file reference-counted deletion, and proof no type 8 row is inserted.

- [ ] **Step 2: Run the focused test and prove current schema accepts duplicates**

Run: `../../.venv/bin/python -m unittest -v test_facebook_page_database`

Expected: FAIL because legacy Page rows remain publishable, the unique index is absent, or `save_facebook_page_browser_account` does not exist.

- [ ] **Step 3: Implement the non-destructive migration**

Inside `database.ensure_schema()` perform these operations in the existing schema transaction:

1. Trim non-empty type 9 `accountReference` values.
2. Mark blank-reference type 9 rows `status=0` without clearing session, avatar, remark or name.
3. For each duplicated non-empty Page ID keep the lowest account row ID; clear only the later rows' `accountReference` and mark them `status=0`.
4. Create:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_info_facebook_page_reference
ON user_info(accountReference)
WHERE type = 9
  AND accountReference IS NOT NULL
  AND TRIM(accountReference) <> '';
```

Run the migration twice in the idempotency test and assert the second pass makes no data change.

- [ ] **Step 4: Implement strict account validation and atomic Page upsert**

`save_facebook_page_browser_account` must use `BEGIN IMMEDIATE`, normalize the identity from Task 1, update the existing row for the same Page, bind one explicitly supplied legacy blank row, or insert one new type 9 row. If `record_id` or `expected_account` is already bound to a different Page, raise `facebook_page_identity_mismatch` and roll back. Never call the old Meta routine that creates both type 8 and type 9 rows.

- [ ] **Step 5: Tighten publishable-account filtering and UI health projection**

Expose a derived `needsPageRebind` flag for type 9 rows with a blank Page ID. Keep the existing red abnormal styling contract. A Page row is publishable only with `status=1`, non-empty Page ID and a usable session reference.

- [ ] **Step 6: Run the focused and affected account tests**

Run:

```bash
../../.venv/bin/python -m unittest -v \
  test_facebook_page_database \
  test_account_detection_ui
```

Expected: PASS; old rows remain visible, duplicate Page IDs cannot create two healthy accounts, and no type 8 row appears.

- [ ] **Step 7: Commit the first shared-core slice**

```bash
git add app_core/database.py app_core/account_service.py test_facebook_page_database.py test_account_detection_ui.py
git commit -m "feat(accounts): bind Facebook rows to unique Page IDs"
```

Record this commit hash in the implementation handoff as a shared identity/database commit requiring integration-line review.

---

### Task 3: Split Facebook Page Login and Revalidate the Saved Page after Restart

**Files:**
- Modify: `app_core/login_service.py`
- Modify: `app_core/account_browser_service.py`
- Modify: `app_core/oneclick_authorization.py`
- Modify: `myUtils/login.py`
- Modify: `myUtils/auth.py`
- Modify: `ui/login_dialog.py`
- Modify: `ui/account_page.py`
- Create: `test_facebook_page_login.py`
- Modify: `test_overseas_integration.py`
- Modify: `test_account_detection_ui.py`
- Modify: `test_oneclick_authorization.py`

**Interfaces:**
- Produces: `FacebookPageSelectionRequest(pages: tuple[FacebookPageIdentity, ...])`.
- Changes: `RecoveredOverseasLoginSession` keeps `platform_type=9` and supports `select_facebook_page(page_id: str) -> None`.
- Changes: Facebook Page appears as a separate login option; type 9 no longer maps to type 8.
- Changes: the Facebook Page login option is hidden/disabled unless `facebook_page_v1_enabled()` is true.
- Changes: saved-session validation returns the exact Page identity or a stable Page error instead of one generic boolean.
- Contract: 0 Page fails, 1 Page auto-binds, multiple Pages pause for explicit selection, cancel saves nothing, and restart/open-backend must activate and read back the saved Page ID.

- [ ] **Step 1: Write failing service and UI login tests**

```python
class FacebookPageLoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_facebook_only_login_never_creates_an_instagram_row(self):
        session = self.make_session(discovered_pages=[self.page("1001", "测试 Page")])
        await session.wait_until_complete()
        self.assertEqual(self.saved_platform_types(), [9])

    async def test_multiple_pages_pause_until_the_user_selects_an_exact_id(self):
        session = self.make_session(
            discovered_pages=[self.page("1001", "同名"), self.page("1002", "同名")]
        )
        request = await session.next_selection_request()
        self.assertEqual([item.page_id for item in request.pages], ["1001", "1002"])
        session.select_facebook_page("1002")
        await session.wait_until_complete()
        self.assertEqual(self.saved_page_id(), "1002")
```

Add exact cases for zero Page, cancel, unknown selection, permission missing, session save only after selection, updated login preserving the original Page, restart returning the same Page, mismatch marking the account abnormal, open-backend activation, and an unbound legacy row refusing backend/publish operations.
Also assert `_verify_saved_session_async` returns the exact bound Page identity for type 9 and never collapses a mismatch or permission loss into a successful generic boolean.
Assert the default-off gate hides the new login entry and prevents session startup; test-only developer opt-in may expose it without changing production defaults.

- [ ] **Step 2: Run the login tests and prove type 9 still routes through type 8**

Run:

```bash
../../.venv/bin/python -m unittest -v \
  test_facebook_page_login \
  test_overseas_integration \
  test_account_detection_ui \
  test_oneclick_authorization
```

Expected: FAIL because `login_platform_type(9)` returns 8, Meta saving creates two rows, or multiple Page selection has no typed callback.

- [ ] **Step 3: Split the login route and persistence call**

Keep Meta's reusable browser-session acquisition, but route type 9 into Page discovery from Task 1 and Page persistence from Task 2. Remove the unconditional “Instagram + Facebook” save behavior for the Facebook Page entry. Preserve the existing Instagram type 8 behavior when the user explicitly chooses Instagram.

- [ ] **Step 4: Add the typed multiple-Page selection handshake**

`RecoveredOverseasLoginSession` must emit `FacebookPageSelectionRequest` without serializing page credentials, accept only an exact discovered Page ID, and save nothing until selection completes. `ui/login_dialog.py` must display Page name plus only a short Page ID tail, and map cancel to a clean cancelled state rather than an abnormal account.

- [ ] **Step 5: Reuse exact Page validation for account checks and opening the backend**

Change saved-session validation and `account_browser_service` so type 9 activates `accountReference`, reads the active Page ID back and only then reports normal or opens the Page backend. Permission loss returns `facebook_page_content_permission_missing`; a different Page returns `facebook_page_identity_mismatch`; a blank legacy binding remains red and requires rebind.

- [ ] **Step 6: Run the focused login and account tests**

Run the command from Step 2 again.

Expected: PASS; Facebook-only login creates exactly one type 9 row, multiple Page selection is explicit, and restart/backend checks bind the same Page ID.

- [ ] **Step 7: Commit the shared login slice**

```bash
git add \
  app_core/login_service.py \
  app_core/account_browser_service.py \
  app_core/oneclick_authorization.py \
  myUtils/login.py \
  myUtils/auth.py \
  ui/login_dialog.py \
  ui/account_page.py \
  test_facebook_page_login.py \
  test_overseas_integration.py \
  test_account_detection_ui.py \
  test_oneclick_authorization.py
git commit -m "feat(accounts): add Facebook Page-only login flow"
```

Record this commit separately from the database commit so integration review can reject or amend the UI/login slice without losing the migration.

---

### Task 4: Normalize the Page-only Publish Request and Stable Intent Fingerprint

**Files:**
- Modify: `app_core/controlled_publish.py`
- Create: `test_facebook_page_controlled_publish.py`
- Modify: `test_controlled_publish.py`

**Interfaces:**
- Produces: `publish_intent_fingerprint(payloads: Iterable[Mapping[str, Any]]) -> str`.
- Produces: `facebook_replay_fingerprint(payloads: Iterable[Mapping[str, Any]]) -> str`.
- Keeps: `scope_fingerprint()` as a compatibility wrapper over the new stable function.
- Changes: type 9 standardization resolves `facebookExpectedPageReference` only from the saved account.
- Changes: standardization calls `build_facebook_page_caption()` once and stores `facebookFinalCaption` plus `facebookCaptionSha256`; every downstream component consumes these fields without rebuilding them.
- Changes: Facebook Page V1 `direct` returns `facebook_preflight_required` before a browser starts.
- Contract: `publish_intent_fingerprint` binds manifest/platform override, video byte hash, account ID, Page ID, final caption/topics, visibility and immediate intent；`facebook_replay_fingerprint` binds the same platform intent but excludes local account ID. Both ignore `runtimeMode`, task/auth IDs, old Meta booleans and all credentials.
- Contract: Page V1 standardization additionally requires `len(payloads) == 1` and that sole payload is type 9; a mixed Facebook + other-platform request fails before task/browser creation.

- [ ] **Step 1: Write failing request and fingerprint tests**

```python
class FacebookPageControlledPublishTests(unittest.TestCase):
    def test_page_reference_is_loaded_from_the_account_not_the_request(self):
        request = self.request(account_id=41, caller_page_id="attacker-page")
        payload = build_controlled_payloads(request)[0]
        self.assertEqual(payload["facebookExpectedPageReference"], "1001")
        self.assertNotEqual(payload["facebookExpectedPageReference"], "attacker-page")

    def test_intent_fingerprint_ignores_preflight_vs_formal_runtime_mode(self):
        preflight = self.valid_payload(runtime_mode="preflight")
        formal = self.valid_payload(runtime_mode="formal")
        self.assertEqual(
            publish_intent_fingerprint([preflight]),
            publish_intent_fingerprint([formal]),
        )

    def test_replay_fingerprint_cannot_be_bypassed_with_a_second_local_account_id(self):
        first = self.valid_payload(account_id=41, page_id="1001")
        second = self.valid_payload(account_id=99, page_id="1001")
        self.assertNotEqual(publish_intent_fingerprint([first]), publish_intent_fingerprint([second]))
        self.assertEqual(facebook_replay_fingerprint([first]), facebook_replay_fingerprint([second]))
```

Add exact cases for one healthy type 9 account, blank/unhealthy/wrong-type account, two accounts, two videos, schedule, custom cover, AI statement, non-public visibility, Page ID change, account ID change, video bytes changed under the same path, caption/topic change, caller-supplied Page ID ignored/rejected, old Meta booleans ignored, and same Page intent blocked from bypassing the fingerprint with a second local account row.
Add a mixed Facebook + domestic/overseas target case that returns `facebook_unsupported_publish_setting` before any task or browser call.
Add exact title/body/topics cases proving the stored final caption and hash equal Task 1's pure function, and proving fingerprint, preflight and formal payloads all consume those exact values.

- [ ] **Step 2: Run the focused test and prove the current fingerprint lacks Page identity**

Run: `../../.venv/bin/python -m unittest -v test_facebook_page_controlled_publish`

Expected: FAIL because `publish_intent_fingerprint` does not exist, unsupported Facebook settings pass, or Page ID is absent from the hash.

- [ ] **Step 3: Implement the local hard gates before browser startup**

In the type 9 branch of `build_controlled_payloads`, first require the default-off developer gate, then require the entire request to contain exactly one type 9 payload, one account and one readable video, `schedule is None`, `visibility == "public"`, and no unsupported settings. Resolve and validate the stored Page ID with Task 2's validator. Build `facebookFinalCaption` and `facebookCaptionSha256` exactly once with Task 1's pure function. Use stable errors `facebook_page_feature_disabled`, `facebook_account_invalid`, `facebook_session_missing`, `facebook_video_file_invalid` and `facebook_unsupported_publish_setting`.

- [ ] **Step 4: Implement the canonical publish-intent fingerprint**

Build one deterministic JSON object per payload with sorted keys and compact separators, hash the video bytes rather than only the path, sort platform payloads deterministically, then SHA-256 the canonical array. Do not include `runtimeMode`, task IDs, authorization IDs, Page credentials, session paths, old Meta booleans or transient browser fields.

- [ ] **Step 5: Reject Facebook direct mode before any platform call**

The generic path may retain direct mode for already-approved platforms, but a type 9 Page request must return stable `facebook_preflight_required` before task worker or browser creation. The returned response must still use the normal controlled-publish JSON envelope.

- [ ] **Step 6: Run the focused and shared controlled-publish tests**

Run:

```bash
../../.venv/bin/python -m unittest -v \
  test_facebook_page_controlled_publish \
  test_controlled_publish
```

Expected: PASS; preflight/formal share the same intent fingerprint, Page/content changes alter it, and direct mode never reaches a browser.

- [ ] **Step 7: Commit the first controlled-task slice**

```bash
git add app_core/controlled_publish.py test_facebook_page_controlled_publish.py test_controlled_publish.py
git commit -m "feat(publish): bind Facebook requests to Page intent"
```

Record this as the first shared controlled-task commit for integration review.

---

### Task 5: Bind Preflight Evidence and Atomically Reserve the Formal Page Claim

**Files:**
- Modify: `app_core/controlled_publish.py`
- Modify: `app_core/task_service.py`
- Modify: `test_facebook_page_controlled_publish.py`
- Modify: `test_controlled_publish_resilience.py`
- Modify: `test_task_service.py`

**Interfaces:**
- Produces: `facebook_preflight_receipt_hash(conn: sqlite3.Connection, preflight_task_id: int, payloads: Iterable[Mapping[str, Any]]) -> str`.
- Produces: `_create_claimed_facebook_page_task(payloads: list[dict[str, Any]], *, preflight_task_id: int, authorization_id: str) -> dict[str, Any]`.
- Produces: `require_facebook_page_execution_claim(task_id: int, payloads: Iterable[Mapping[str, Any]]) -> None`.
- Produces: `mark_facebook_page_checkpoint(task_id: int, *, expected_state: str, new_state: str, receipt: Mapping[str, object]) -> None`.
- Changes: `controlled_publish_authorizations` gains `authorizationScope` and `preflightReceiptHash`.
- Produces: `facebook_page_publish_claims` with Page/replay active uniqueness, full authorization intent and explicit lifecycle states.
- Contract: authorization consumption, formal task creation, item binding and Page claim insertion commit or roll back together under `BEGIN IMMEDIATE`.

- [ ] **Step 1: Write failing preflight-evidence and authorization tests**

```python
def test_facebook_authorization_requires_verified_platform_form_receipt(self):
    task_id = self.completed_preflight(
        phase="platform_form_verified",
        receipt=self.verified_form_receipt(),
    )
    authorization = authorize_completed_check(task_id)
    row = self.read_authorization(authorization["authorizationId"])
    self.assertEqual(row["authorizationScope"], "formal")
    self.assertEqual(len(row["preflightReceiptHash"]), 64)

def test_changed_preflight_receipt_does_not_consume_authorization(self):
    task_id, authorization_id = self.authorized_preflight()
    self.mutate_preflight_receipt(task_id, page_id="different")
    with self.assertRaises(ControlledPublishError) as raised:
        self.submit_formal(task_id, authorization_id)
    self.assertEqual(raised.exception.error_code, "facebook_publish_authorization_invalid")
    self.assertIsNone(self.read_authorization(authorization_id)["consumedAt"])
```

Add exact cases for text-only/route-only “success”, wrong phase, missing final-button readback, changed form receipt, changed Page/intent, expired/consumed authorization and a caller-supplied receipt hash being ignored.

- [ ] **Step 2: Write failing transaction and concurrency tests**

Add these exact tests to `test_facebook_page_controlled_publish.py` and use two independent SQLite connections for the race:

- `test_two_facebook_formal_authorizations_compete_for_one_page_claim`
- `test_claim_blocks_same_page_intent_across_two_local_account_ids`
- `test_atomic_creation_rolls_back_authorization_when_task_insert_fails`
- `test_atomic_creation_rolls_back_task_and_claim_when_binding_fails`
- `test_authorization_task_item_and_claim_commit_together`
- `test_safe_failed_history_releases_active_slot_but_is_preserved`
- `test_confirmed_not_published_releases_active_slot_but_is_preserved`
- `test_final_action_claimed_clicked_ambiguous_and_succeeded_block_republish`

- [ ] **Step 3: Run the focused test and prove authorization/task creation is non-atomic**

Run:

```bash
../../.venv/bin/python -m unittest -v \
  test_facebook_page_controlled_publish \
  test_controlled_publish_resilience
```

Expected: FAIL because authorization rows lack the receipt hash/scope, task creation happens after a separate commit, or no Page claim exists.

- [ ] **Step 4: Add idempotent authorization and claim schema migrations**

Extend `controlled_publish_authorizations` using the project's `_add_columns` pattern:

```sql
authorizationScope TEXT NOT NULL DEFAULT 'formal'
preflightReceiptHash TEXT NOT NULL DEFAULT ''
```

Create:

```sql
CREATE TABLE IF NOT EXISTS facebook_page_publish_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pageReference TEXT NOT NULL,
    publishIntentFingerprint TEXT NOT NULL,
    replayFingerprint TEXT NOT NULL,
    preflightTaskId INTEGER NOT NULL,
    preflightReceiptHash TEXT NOT NULL,
    taskId INTEGER NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK(state IN (
        'reserved',
        'final_action_claimed',
        'final_action_clicked',
        'ambiguous',
        'succeeded',
        'confirmed_not_published',
        'safe_failed'
    )),
    blocksReplay INTEGER NOT NULL CHECK(
        (state IN ('safe_failed', 'confirmed_not_published') AND blocksReplay = 0)
        OR
        (state NOT IN ('safe_failed', 'confirmed_not_published') AND blocksReplay = 1)
    ),
    workerStartedAt TEXT,
    baselineJson TEXT NOT NULL DEFAULT '{}',
    baselineHash TEXT NOT NULL DEFAULT '',
    formSnapshotJson TEXT NOT NULL DEFAULT '{}',
    formSnapshotHash TEXT NOT NULL DEFAULT '',
    clickedAt TEXT NOT NULL DEFAULT '',
    platformDecisionJson TEXT NOT NULL DEFAULT '{}',
    receiptJson TEXT NOT NULL DEFAULT '{}',
    reelId TEXT NOT NULL DEFAULT '',
    reelUrl TEXT NOT NULL DEFAULT '',
    createdAt TEXT NOT NULL,
    updatedAt TEXT NOT NULL,
    FOREIGN KEY(taskId) REFERENCES publish_tasks(id),
    FOREIGN KEY(preflightTaskId) REFERENCES publish_tasks(id)
);
```

Add a partial unique index on `(pageReference, replayFingerprint)` where `blocksReplay=1`. Only transitions to `safe_failed` or `confirmed_not_published` may set `blocksReplay=0`; history rows are never deleted. The full `publishIntentFingerprint` remains stored and must match the authorization, while `replayFingerprint` excludes only the local account row ID.

`baselineJson` contains only Page ID and the complete pre-click list of `{reelId, url, publishedAt, captionSha256}`；`formSnapshotJson` contains only Page ID, video name/size/hash, caption hash, visibility and final-button label/readiness；`platformDecisionJson` contains only Page ID、`accepted|rejected_no_creation|unknown`、observed time and evidence hash. Their hashes must be recomputed and verified before recovery. Never persist full caption, DOM, error page HTML or session data in a claim. Only the internal Page DOM parser can create a platform decision; caller-provided receipt/request fields are ignored and cannot release a claim.

- [ ] **Step 5: Hash only a valid Facebook preflight receipt**

`facebook_preflight_receipt_hash` must load the completed preflight and task item from the database, project safe fields, and require all of:

- status success and phase `platform_form_verified`;
- same account/Page/intent;
- exact video and caption hashes;
- `visibility=public`;
- `finalButtonEnabled=true`;
- `finalActionTriggered=false`.

Hash the canonical safe projection. Never accept the hash or receipt from the formal caller.

- [ ] **Step 6: Implement the single-transaction formal reservation**

Within one `BEGIN IMMEDIATE` transaction:

1. Reload and validate the successful preflight.
2. Recompute its receipt hash and current publish-intent fingerprint.
3. Validate and consume the unexpired `formal` authorization.
4. Insert the pending formal task with item `accountId` and `authorizationSnapshotHash`.
5. Insert the `reserved`, `blocksReplay=1` Page claim for the Page and intent.
6. Commit only after the task ID is bound to the claim.

Map unique-index races to `facebook_duplicate_submit_blocked`. Any injected failure must roll back the task, item, claim and authorization consumption.

- [ ] **Step 7: Implement compare-and-swap claim transitions**

`mark_facebook_page_checkpoint` must update exactly one row whose state equals `expected_state`, store only projected safe baseline/form/decision/receipt data plus their integrity hashes, update `clickedAt` only after the click call returns, derive `blocksReplay` from a state table, and fail closed on skipped or repeated transitions. Never let caller data directly choose `blocksReplay`.

Freeze the legal CAS graph exactly:

- `reserved -> final_action_claimed | safe_failed`;
- `final_action_claimed -> final_action_clicked | ambiguous`;
- `final_action_clicked -> succeeded | ambiguous`;
- `ambiguous -> succeeded | confirmed_not_published`;
- `safe_failed`、`confirmed_not_published` and `succeeded` are immutable terminal claim states.

Every other edge or repeated transition must update zero rows and raise a stable internal lifecycle error. Add database tests proving invalid `(state, blocksReplay)` pairs cannot be inserted even if application code is bypassed.

- [ ] **Step 8: Run focused and shared task tests**

Run:

```bash
../../.venv/bin/python -m unittest -v \
  test_facebook_page_controlled_publish \
  test_controlled_publish_resilience \
  test_task_service
```

Expected: PASS; two concurrent formal requests yield one task/claim, rollback leaves authorization reusable, and every history state has the approved replay behavior.

- [ ] **Step 9: Commit the shared authorization/claim slice**

```bash
git add \
  app_core/controlled_publish.py \
  app_core/task_service.py \
  test_facebook_page_controlled_publish.py \
  test_controlled_publish_resilience.py \
  test_task_service.py
git commit -m "feat(publish): reserve Facebook Page formal claims atomically"
```

Record this as the second shared controlled-task commit for integration review.

---

### Task 6: Build One Shared Facebook Page Form Fill-and-Readback Adapter

**Files:**
- Create: `uploader/meta_uploader/page_form.py`
- Create: `test_facebook_page_publish.py`
- Modify: `uploader/meta_uploader/main.py`
- Modify: `test_meta_browser_publish.py`

**Interfaces:**
- Produces: `FacebookPageFormExpectation(page_id: str, content_kind: Literal["reel"], video_name: str, video_size: int, video_sha256: str, caption: str, visibility: Literal["public"])`.
- Produces: `FacebookPageFormSnapshot(page_id: str, content_kind: str, video_name: str, video_count: int, caption: str, visibility: str, final_action_label: str, final_action_ready: bool)`.
- Produces: `canonical_meta_caption(value: object) -> str`.
- Produces: `FacebookPageFormAdapter(page, *, wait_for_verification: Callable[..., Awaitable[None]])`.
- Produces: `open_fresh_reel_composer(expected_page_id: str) -> None`.
- Produces: `select_expected_page(expected_page_id: str) -> FacebookPageIdentity`.
- Produces: `fill_and_readback(expected: FacebookPageFormExpectation) -> FacebookPageFormSnapshot`.
- Produces: `final_action_button() -> Any`.
- Contract: preflight and formal both call `fill_and_readback`; the adapter never clicks the final button.

- [ ] **Step 1: Write failing Page form contract tests**

```python
class FacebookPageFormTests(unittest.IsolatedAsyncioTestCase):
    async def test_fill_and_readback_requires_exact_page_video_caption_public_and_button(self):
        adapter = self.adapter(
            active_page_id="1001",
            video_names=["clip.mp4"],
            editor_before="",
            editor_after="正文 #标签",
            visibility="public",
            final_button_enabled=True,
        )
        snapshot = await adapter.fill_and_readback(self.expectation(page_id="1001"))
        self.assertEqual(snapshot.page_id, "1001")
        self.assertEqual(snapshot.video_count, 1)
        self.assertTrue(snapshot.final_action_ready)
        self.assertEqual(self.final_button_click_count, 0)
```

Add exact cases for same-name/different-ID Page, current Page switch mismatch, a restored old draft/media, editor not empty before fill, caption order/duplicate mismatch, no video, two videos, wrong file name, upload incomplete, non-public visibility, missing/disabled/ambiguous final button, verification pause returning to the same Page, and preflight never clicking.
Add a normal-video-post or generic composer route case that is rejected because the page cannot read back `content_kind == "reel"`.

- [ ] **Step 2: Run the focused test and prove the common adapter is absent**

Run: `../../.venv/bin/python -m unittest -v test_facebook_page_publish.FacebookPageFormTests`

Expected: FAIL because `uploader.meta_uploader.page_form` does not exist.

- [ ] **Step 3: Implement caption canonicalization and exact Page selection**

`canonical_meta_caption` must normalize platform line endings and allowed editor spacing without reordering, removing duplicates or inventing topic entities. Page selection must call Task 1's exact Page activation and compare the returned Page ID, never just a visible Facebook label.

- [ ] **Step 4: Implement clear-before-fill, single upload and exact readback**

The adapter must:

1. Navigate through the Page-bound “create Reel” entry into a fresh composer.
2. Read back the Facebook Reel composer kind/route; reject a normal post/video composer.
3. Read back zero attached media and no old user text before modifying the form. A restored draft fails safely and is not deleted or overwritten.
4. Clear the editor and verify the readback is empty.
5. Upload the expected local file once.
6. Wait for one corresponding completed video preview and reject a second preview.
7. Write the final caption exactly once.
8. Read back normalized full caption in original order.
9. Select and read back `public`.
10. Locate one final action button and record label/readiness without clicking.

Map any mismatch to `facebook_upload_failed` or `facebook_page_form_readback_failed` with a safe receipt.

- [ ] **Step 5: Keep Instagram compatibility but remove type 9 generic success behavior**

In `uploader/meta_uploader/main.py`, retain the old type 8 Instagram path. Route type 9 into the new adapter and remove dependence on generic destination text, `/latest/content` route changes or generic Meta success text. The type 9 path must not read the two legacy Meta confirmation booleans.

- [ ] **Step 6: Run focused form and Meta regression tests**

Run:

```bash
../../.venv/bin/python -m unittest -v \
  test_facebook_page_publish.FacebookPageFormTests \
  test_meta_browser_publish
```

Expected: PASS; Page form readback is exact, preflight clicks zero times, and Instagram's existing type 8 contract remains intact.

- [ ] **Step 7: Commit the overseas-only form adapter**

```bash
git add \
  uploader/meta_uploader/page_form.py \
  uploader/meta_uploader/main.py \
  test_facebook_page_publish.py \
  test_meta_browser_publish.py
git commit -m "feat(overseas): verify Facebook Page Reel form"
```

---

### Task 7: Capture a Complete Same-Page Baseline and Match One New Reel

**Files:**
- Create: `uploader/meta_uploader/content_list.py`
- Modify: `test_facebook_page_publish.py`

**Interfaces:**
- Produces: `FacebookReelRow(page_id: str, reel_id: str, url: str, caption_sha256: str, published_at: str)`.
- Produces: `FacebookPageContentBaseline(page_id: str, rows: tuple[FacebookReelRow, ...], captured_at: str, snapshot_sha256: str)`.
- Produces: `FacebookReelReceipt(page_id: str, reel_id: str, url: str, published_at: str)`.
- Produces: `FacebookReelMatch(status: Literal["none", "mismatch", "unique"], receipt: FacebookReelReceipt | None, new_count: int, matching_count: int)`.
- Produces: `match_unique_new_facebook_reel(*, baseline: FacebookPageContentBaseline, current_rows: Iterable[FacebookReelRow], expected_page_id: str, expected_caption_sha256: str, clicked_at: str) -> FacebookReelMatch`.
- Produces: `FacebookPageContentReader(context, *, wait_for_verification: Callable[..., Awaitable[None]])` with `capture_baseline()` and `readback_unique_reel()`.
- Produces: `FacebookPlatformDecision(kind: Literal["accepted", "rejected_no_creation", "unknown"], page_id: str, observed_at: str, evidence_sha256: str)` from the internal Page DOM parser only.
- Contract: a valid baseline is an explicitly complete read of the same Page; an empty but explicitly complete list is valid, while incomplete pagination, wrong Page or duplicate IDs is invalid.

- [ ] **Step 1: Write failing baseline and unique-match tests**

```python
class FacebookPageContentListTests(unittest.IsolatedAsyncioTestCase):
    def test_exactly_one_new_same_page_reel_is_success(self):
        baseline = self.baseline(rows=[self.row("old", caption="旧内容")])
        match = match_unique_new_facebook_reel(
            baseline=baseline,
            current_rows=[
                self.row("old", caption="旧内容"),
                self.row("new", caption=self.expected_caption, url="https://www.facebook.com/reel/new"),
            ],
            expected_page_id="1001",
            expected_caption_sha256=self.expected_caption_hash,
            clicked_at=self.clicked_at,
        )
        self.assertEqual(match.status, "unique")
        self.assertEqual(match.receipt.reel_id, "new")
```

Add exact cases for explicit empty baseline, incomplete pagination, wrong Page, duplicate row IDs, a generic content route with no new row, one unrelated new row, two matching new rows, matching caption on a different Page, new row before click time, missing Reel ID, missing canonical URL and readback verification that resumes the same context. Zero new rows must return `status="none"`; any visible new rows that cannot resolve to one target must return `status="mismatch"`.
Add a truncated-list-caption case: the list row alone cannot match; the reader must open the new Reel's read-only detail view and hash the complete normalized caption, otherwise the visible candidate remains a mismatch.
Add a decision-source test proving a forged `rejected_no_creation` in request/receipt cannot create a trusted `FacebookPlatformDecision` or release an ambiguous claim.

- [ ] **Step 2: Run the content-list tests and prove unique platform readback is absent**

Run: `../../.venv/bin/python -m unittest -v test_facebook_page_publish.FacebookPageContentListTests`

Expected: FAIL because `uploader.meta_uploader.content_list` does not exist.

- [ ] **Step 3: Implement immutable rows and deterministic baseline hashing**

Normalize row Page IDs, Reel IDs, canonical Reel URLs and timestamps, and hash each normalized caption immediately so the durable baseline never needs full caption text. Sort the complete safe row projection by Reel ID before hashing. `capture_baseline` must fail with `facebook_page_baseline_read_failed` if it cannot prove the Page ID or pagination completeness; it may return an empty tuple only when the UI explicitly proves there are no Page contents and pagination is complete.

- [ ] **Step 4: Implement the unique new-Reel matcher**

Subtract baseline Reel IDs from the current complete same-Page list. If no new IDs exist, return `status="none"` without pretending delayed indexing proves failure. For every new candidate, open its read-only detail view and obtain the complete normalized caption; never hash a truncated list preview. Filter by full caption hash and publish time not earlier than the recorded click. Exactly one candidate with non-empty platform Reel ID and canonical Facebook Reel URL returns `unique`; any visible new rows that cannot resolve to one target return `mismatch`.

- [ ] **Step 5: Implement read-only list navigation in the existing browser context**

The reader must use the same authenticated context as the composer, activate and re-read the expected Page ID, paginate until an explicit terminal condition, and never click a create/publish/delete action. Verification handling may pause but must resume in the same context.

- [ ] **Step 6: Run all Page uploader tests**

Run: `../../.venv/bin/python -m unittest -v test_facebook_page_publish`

Expected: PASS for Page form, complete baseline, unique Reel success and all ambiguous/mismatch cases.

- [ ] **Step 7: Commit the overseas-only content readback**

```bash
git add uploader/meta_uploader/content_list.py test_facebook_page_publish.py
git commit -m "feat(overseas): read back unique Facebook Page Reels"
```

---

### Task 8: Route Preflight and Formal through One Page Executor and One-click Boundary

**Files:**
- Modify: `app_core/overseas_preflight.py`
- Modify: `app_core/overseas_browser_publish.py`
- Modify: `app_core/publish_service.py`
- Modify: `utils/publish_observer.py`
- Modify: `test_overseas_publish_routing.py`
- Create: `test_facebook_page_publish_service.py`
- Modify: `test_publish_service.py`

**Interfaces:**
- Produces: `run_facebook_page_preflight_sync(payload: dict[str, Any], *, task_id: int) -> dict[str, Any]`.
- Produces: `run_facebook_page_publish_sync(payload: dict[str, Any], *, task_id: int, progress: Callable[[str, Mapping[str, object]], None]) -> dict[str, Any]`.
- Produces: `start_controlled_facebook_publish(task_id: int) -> dict[str, Any]`.
- Changes: Page preflight and formal use the same adapter/context contract and carry `task_id` into `publish_context`.
- Contract: a formal type 9 worker cannot start without a valid reserved claim; claim checkpoint commits precede final click; click count is at most one.

- [ ] **Step 1: Write failing routing and worker-claim tests**

```python
class FacebookPagePublishServiceTests(unittest.TestCase):
    def test_generic_desktop_publish_rejects_formal_facebook_without_claim(self):
        with self.assertRaises(PublishServiceError) as raised:
            start_desktop_publish([self.formal_facebook_payload_without_claim()])
        self.assertEqual(raised.exception.error_code, "facebook_publish_authorization_invalid")

    def test_preflight_and_formal_receive_the_same_page_form_adapter(self):
        self.run_preflight()
        self.run_formal()
        self.assertEqual(self.form_adapter_classes, [FacebookPageFormAdapter, FacebookPageFormAdapter])
```

Add exact cases for one worker start per claim, task ID in publish observer events, preflight returning `platform_form_verified`, preflight final-click count zero, worker startup failure before final claim becoming `safe_failed`, baseline failure preventing click, final-action claim persisted before click, click-return event persisted immediately after click, click exception after entering claim becoming `ambiguous`, and formal runner ignoring/removing caller confirmation booleans.
Add a test that changes one stable formal form field after a successful preflight; the formal snapshot must differ from the authorized preflight snapshot and stop before `final_action_claimed`.

- [ ] **Step 2: Run the focused service tests and prove type 9 uses the old generic runner**

Run:

```bash
../../.venv/bin/python -m unittest -v \
  test_facebook_page_publish_service \
  test_overseas_publish_routing \
  test_publish_service
```

Expected: FAIL because Page preflight/formal do not receive `task_id`, a claim is not required, or old Meta flags are still mandatory.

- [ ] **Step 3: Implement one Page preflight executor**

The preflight executor must:

1. Validate the local payload and saved Page binding.
2. Launch the visible isolated Meta session.
3. Activate/read the exact Page ID.
4. Run `FacebookPageFormAdapter.fill_and_readback`.
5. Return phase `platform_form_verified`, `platformWriteOccurred=true`, `finalActionTriggered=false` and safe hashes.
6. Never call `final_action_button().click()` and never create a formal claim.

- [ ] **Step 4: Implement the controlled formal executor**

The formal executor must:

1. Require the reserved claim before browser startup.
2. Acquire one worker lease.
3. Revalidate the same Page and rerun the same form adapter.
4. Compare Page ID, Reel kind, video/caption hashes, visibility and final-button readiness with the authorized preflight snapshot; any drift stops before the final boundary.
5. Capture a complete same-Page content baseline.
6. CAS `reserved -> final_action_claimed` with safe baseline/form snapshot JSON and their hashes.
7. Resolve one enabled final button and invoke `click()` once.
8. Immediately after the click await returns, CAS `final_action_claimed -> final_action_clicked` with durable `clickedAt`.
9. Record platform-accepted feedback without treating it as success.
10. Read back the new-Reel match: `unique` transitions to `succeeded`; `none` becomes `ambiguous` with `facebook_publish_outcome_unknown`; `mismatch` becomes `ambiguous` with `facebook_publish_readback_mismatch`.

Never retry the click automatically. Any failure after `final_action_claimed` is ambiguous unless a later read-only reconciliation proves no target Reel.

- [ ] **Step 5: Add the Page-specific publish-service entry and task context**

`start_controlled_facebook_publish` must own lease acquisition and worker startup. The legacy `start_desktop_publish(payloads)` entry must always reject type 9 formal payloads because it has no trusted pre-created task ID; it may still create Page preflight tasks. Only `submit_authorized_preflight_task()` may atomically create a formal task/claim and then call `start_controlled_facebook_publish(task_id)`. Pass `task_id` through preflight/formal runners and `publish_context` so every durable phase is written to the same task.

- [ ] **Step 6: Run focused service and routing tests**

Run the command from Step 2 again.

Expected: PASS; preflight and formal share one form contract, formal cannot start without claim, baseline gates the click, and no path invokes the final button more than once.

- [ ] **Step 7: Commit overseas and shared service changes separately**

First commit overseas-owned files:

```bash
git add \
  app_core/overseas_preflight.py \
  app_core/overseas_browser_publish.py \
  test_overseas_publish_routing.py \
  test_facebook_page_publish_service.py
git commit -m "feat(overseas): run controlled Facebook Page workers"
```

Then commit shared publish service files:

```bash
git add app_core/publish_service.py utils/publish_observer.py test_publish_service.py
git commit -m "feat(publish): enforce Facebook Page worker claims"
```

Record the second hash as a shared service commit requiring integration-line review.

---

### Task 9: Persist Facebook Lifecycle, Recover Stale Workers and Reconcile Read-only

**Files:**
- Modify: `app_core/task_service.py`
- Modify: `app_core/controlled_publish.py`
- Create: `test_facebook_page_task_service.py`
- Modify: `test_task_service.py`
- Modify: `test_controlled_publish_resilience.py`

**Interfaces:**
- Produces: `record_facebook_progress(task_id: int, *, phase: str, message: str, receipt: Mapping[str, object]) -> None`.
- Produces: `mark_facebook_result(task_id: int, *, ok: bool, message: str, error_code: str = "", receipt: Mapping[str, object] | None = None) -> None`.
- Produces: `reconcile_stale_facebook_page_claim(task_id: int) -> bool`.
- Produces: `reconcile_facebook_page_publish_outcome(task_id: int) -> dict[str, Any]`.
- Changes: `project_task()` exposes the same safe Facebook phases and receipts to UI, CLI and MCP.
- Contract: progress update, claim transition, task item, task event and worker heartbeat are one transaction; stale or interrupted tasks never remain permanently `running`.

- [ ] **Step 1: Write failing receipt-projection and lifecycle tests**

```python
class FacebookPageTaskServiceTests(unittest.TestCase):
    def test_waiting_verification_has_action_required_and_no_error_code(self):
        task_id = self.facebook_task(phase="waiting_user_verification")
        record_facebook_progress(
            task_id,
            phase="waiting_user_verification",
            message="请在可见窗口完成验证",
            receipt={"pageId": "1001", "verificationCode": "forbidden"},
        )
        item = project_task(task_id)["items"][0]
        self.assertEqual(item["status"], "waiting_user_verification")
        self.assertEqual(item["errorCode"], "")
        self.assertEqual(item["actionRequired"]["code"], "facebook_verification_required")
        self.assertNotIn("verificationCode", json.dumps(item, ensure_ascii=False))
```

Add exact cases for safe receipt allowlist, every approved phase, success requiring Reel ID/URL, platform feedback without Reel remaining non-success, stable error-code preservation, Page mismatch, and one transaction rolling back event/item/claim together on injected failure.

- [ ] **Step 2: Write failing stale-worker and read-only-reconciliation tests**

Add these exact cases:

- stale `reserved` with no final-boundary event becomes `safe_failed`, records `facebook_worker_interrupted` and releases replay slot;
- stale `final_action_claimed` becomes `ambiguous` and blocks replay;
- stale `final_action_clicked` becomes `ambiguous` and blocks replay;
- interrupted worker never leaves task `running` after reconciliation;
- ambiguous task with one matching new Reel becomes `succeeded` without another click;
- ambiguous task with an explicit platform rejection/no-creation signal plus a complete same-Page read proving no target Reel becomes `confirmed_not_published` and releases the slot;
- mere absence of a Reel, even in one complete list read, remains `ambiguous` because platform indexing may be delayed;
- ambiguous task with incomplete list or unrelated/multiple new rows remains `ambiguous`;
- read-only reconciliation never opens a composer, uploads or calls a final action;
- corrupted baseline/form JSON or a hash mismatch cannot be used for reconciliation and remains `ambiguous`;
- persisted baseline/form/decision JSON contains no full caption, DOM, session path, Cookie, token or verification value;
- a `succeeded`, `safe_failed` or `confirmed_not_published` task cannot be silently rewritten by a second reconciliation.

- [ ] **Step 3: Run the focused lifecycle tests and prove Facebook events are currently projected generically**

Run:

```bash
../../.venv/bin/python -m unittest -v \
  test_facebook_page_task_service \
  test_task_service \
  test_controlled_publish_resilience
```

Expected: FAIL because Meta receipts lose Page/Reel fields, progress is not durable, or stale claims cannot reach a terminal state.

- [ ] **Step 4: Implement transactional progress and result writers**

Use Task 1's safe projection. In one database transaction update the Page claim by CAS, task item phase/status/receipt, task event and worker heartbeat. Define one transition table in code; do not duplicate transition rules in the UI or uploader. `mark_facebook_result(ok=True)` must reject any receipt without exact Page ID, Reel ID, canonical Reel URL and published time.

- [ ] **Step 5: Project stable Facebook task JSON**

Return stable `phase`, `status`, `errorCode`, `errorMessage`, safe `receipt` and optional `actionRequired`. Preserve the platform's useful error text only after redaction. Do not expose session paths, credentials, verification values, full caption or browser DOM.

- [ ] **Step 6: Implement lease recovery based on the persisted final boundary**

When a lease expires or a worker disappears:

- `reserved` with no final-boundary evidence → `safe_failed`, `errorCode=facebook_worker_interrupted`, `blocksReplay=0`;
- `final_action_claimed` or `final_action_clicked` → `ambiguous`, `blocksReplay=1`, task error `facebook_publish_outcome_unknown`;
- `succeeded` remains immutable.

The recovery path must be idempotent and safe under two reconcilers running concurrently.

- [ ] **Step 7: Implement explicit read-only outcome reconciliation**

Only allow it for `ambiguous`, `final_action_claimed` or `final_action_clicked`. First verify the hashes of persisted `baselineJson`, `formSnapshotJson` and any `platformDecisionJson`; a mismatch fails closed. Reopen the saved session in read-only content-list mode, activate the exact Page and reuse the stored safe baseline plus durable `clickedAt` with Task 7's matcher. One unique target Reel → `succeeded`. `confirmed_not_published` additionally requires an explicit platform rejection/no-creation signal and a complete same-Page read after the bounded settlement window; a list that merely lacks the Reel is not proof and remains `ambiguous`. Never clear history or create a new formal authorization.

- [ ] **Step 8: Run focused lifecycle and resilience tests**

Run the command from Step 3 again.

Expected: PASS; every interrupted task reaches a conservative terminal state, reconciliation never clicks, and only platform Reel evidence can produce success.

- [ ] **Step 9: Commit the shared task lifecycle slice**

```bash
git add \
  app_core/task_service.py \
  app_core/controlled_publish.py \
  test_facebook_page_task_service.py \
  test_task_service.py \
  test_controlled_publish_resilience.py
git commit -m "feat(tasks): persist Facebook Page publish outcomes"
```

Record this as the final shared controlled-task commit requiring integration-line review.

---

### Task 10: Converge Desktop UI, CLI, Gateway and MCP on the Same Service

**Files:**
- Modify: `ui/publish_page.py`
- Modify: `desktop_native_app.py`
- Modify: `app_core/controlled_publish_process.py`
- Modify: `app_core/content_project_gateway.py`
- Modify: `app_core/oneclick_mcp_server.py`
- Modify: `test_publish_page.py`
- Modify: `test_controlled_publish_process.py`
- Modify: `test_content_project_gateway.py`
- Modify: `test_oneclick_mcp_server.py`

**Interfaces:**
- Produces: `submit_authorized_preflight_task(preflight_task_id: int, authorization_id: str) -> dict[str, Any]` as the only Facebook formal submission entry.
- Produces: a CLI read-only reconcile action accepting only `taskId`.
- Changes: Gateway/MCP formal calls forward only preflight task ID and authorization ID; Page ID and Meta confirmation booleans are never public arguments.
- Changes: UI formal confirmation creates/uses the same one-time authorization and submission method as CLI/MCP.
- Contract: all three entry points return the same task envelope and start exactly one `start_controlled_facebook_publish` service call.

- [ ] **Step 1: Write failing UI convergence tests**

```python
def test_facebook_ui_formal_uses_preflight_authorization_not_meta_flags(self):
    page = self.make_publish_page_with_successful_facebook_preflight()
    page.start_formal_publish_from_task()
    self.assertEqual(self.authorize_call_count, 1)
    self.assertEqual(self.submit_authorized_call_count, 1)
    self.assertNotIn("metaBrowserPublishConfirmed", self.last_payload)
    self.assertNotIn("metaBrowserAutomationAcknowledged", self.last_payload)
```

Add exact cases for UI preflight using standard payloads, user cancel creating no authorization, unsupported settings blocked before confirmation, Page name/ID tail displayed from the saved account, and waiting verification rendering one clear action without marking failure.

- [ ] **Step 2: Write failing CLI/Gateway/MCP contract tests**

Add exact cases:

- CLI schema does not accept Meta confirmation booleans, Page ID, Cookie or token;
- MCP tool schemas do not expose those fields;
- Facebook direct returns `facebook_preflight_required` before browser startup;
- formal Gateway forwards only `preflightTaskId` and `authorizationId`;
- UI/CLI/MCP each reach the same controlled start function once;
- status returns identical safe receipt shape over CLI and MCP;
- read-only reconcile accepts only task ID and cannot be called for a normal preflight;
- an old generic Facebook UI task cannot bypass claim enforcement.

- [ ] **Step 3: Run the focused interface tests and prove the UI currently bypasses controlled authorization**

Run:

```bash
../../.venv/bin/python -m unittest -v \
  test_publish_page \
  test_controlled_publish_process \
  test_content_project_gateway \
  test_oneclick_mcp_server
```

Expected: FAIL because `publish_page.py` writes old booleans and directly calls `start_desktop_publish`, or because Facebook direct is still accepted.

- [ ] **Step 4: Replace the UI's Facebook formal bypass**

Keep the user's final confirmation dialog, but convert confirmation into `authorize_completed_check(preflight_task_id)` followed by `submit_authorized_preflight_task(...)`. Remove type 9 writes to `metaBrowserPublishConfirmed` and `metaBrowserAutomationAcknowledged`. Do not alter the Instagram type 8 compatibility path in this slice.

- [ ] **Step 5: Add the Page exception and read-only reconcile to CLI/Gateway/MCP**

Keep existing generic tool names and response envelopes. Before dispatch, detect a type 9 target and reject direct mode. Formal accepts only preflight task ID and authorization ID. Add one read-only reconciliation action/tool that accepts only task ID, calls `reconcile_facebook_page_publish_outcome` and returns current task projection.

- [ ] **Step 6: Enforce the existing default-off Facebook Page feature flag at every public entry**

Reuse `facebook_page_v1_enabled()` introduced in Task 1. Task 3 already hides/disables login by default; this step applies the same gate to publish-center UI, CLI, Gateway and MCP. Before true platform validation, normal customers must not see a usable Facebook Page entry. Tests and exact process-local `ONECLICK_ENABLE_FACEBOOK_PAGE_V1=1` may expose login/preflight. Turning the flag off must preserve accounts, sessions, tasks and claims; it only blocks new Page operations.

- [ ] **Step 7: Run focused interface and UI tests**

Run the command from Step 3 again.

Expected: PASS; no public interface accepts sensitive/forged Page controls, all entry points share one service, and the feature remains default-off.

- [ ] **Step 8: Commit the shared UI/CLI/MCP slice**

```bash
git add \
  ui/publish_page.py \
  desktop_native_app.py \
  app_core/controlled_publish_process.py \
  app_core/content_project_gateway.py \
  app_core/oneclick_mcp_server.py \
  test_publish_page.py \
  test_controlled_publish_process.py \
  test_content_project_gateway.py \
  test_oneclick_mcp_server.py
git commit -m "feat(publish): expose one controlled Facebook Page service"
```

Record this as the dedicated shared UI/interface commit requiring integration-line review.

---

### Task 11: Run Offline Gates, Document Exact Evidence and Prepare Integration Handoff

**Files:**
- Create: `docs/superpowers/reports/2026-08-30-facebook-page-publish-verification.md`
- Modify: `SOURCE_OF_TRUTH.md`
- Verify: all files changed by Tasks 1–10

**Contract:** this task may claim only local contracts and test results. It must not claim Facebook login, platform-form validation, public publish or package availability.

- [ ] **Step 1: Run all new Facebook Page tests together**

Run:

```bash
../../.venv/bin/python -m unittest -v \
  test_facebook_page_identity \
  test_facebook_page_content \
  test_facebook_page_database \
  test_facebook_page_login \
  test_facebook_page_controlled_publish \
  test_facebook_page_publish \
  test_facebook_page_publish_service \
  test_facebook_page_task_service
```

Expected: PASS with the exact test count recorded in the verification report.

- [ ] **Step 2: Run affected overseas, account, controlled-task and interface regressions**

Run:

```bash
../../.venv/bin/python -m unittest -v \
  test_meta_browser_publish \
  test_overseas_publish_routing \
  test_overseas_video_publish \
  test_overseas_integration \
  test_account_detection_ui \
  test_controlled_publish \
  test_controlled_publish_resilience \
  test_publish_service \
  test_task_service \
  test_publish_page \
  test_controlled_publish_process \
  test_content_project_gateway \
  test_oneclick_mcp_server
```

Expected: PASS with no regression in Instagram, YouTube, TikTok or existing domestic flows covered by these suites.

- [ ] **Step 3: Run the full offline test suite**

Run: `../../.venv/bin/python -m unittest discover -v`

Expected: PASS; if unrelated pre-existing failures appear, record exact names and prove they reproduce on the base branch before deciding whether this feature is blocked.

- [ ] **Step 4: Run the offscreen source-client smoke test**

Run:

```bash
QT_QPA_PLATFORM=offscreen ../../.venv/bin/python desktop_native_app.py --ui-test
```

Expected: exit 0; main window constructs without opening a browser or triggering login/publish.

- [ ] **Step 5: Run static hygiene and secret scans**

Run:

```bash
git diff --check
git diff --check origin/main...HEAD
rg -n -i \
  'cookie\s*[=:]|access[_ -]?token\s*[=:]|password\s*[=:]|verificationCode|qr(Code|Data)|storageState' \
  app_core uploader ui desktop_native_app.py test_facebook_page_*.py
rg -n 'TO[D]O|TB[D]|PLACEHOL[D]ER|待[定]|待[补]' \
  docs/superpowers/specs/2026-08-29-facebook-page-publish-design.md \
  docs/superpowers/plans/2026-08-30-facebook-page-controlled-reel-publishing.md
```

Expected: `git diff --check` has no output; every secret-scan hit is a deliberate rejection/redaction assertion, never a stored value; the documentation placeholder scan has no output.

- [ ] **Step 6: Run the workstream scope checker and classify shared commits correctly**

Run:

```bash
../../.venv/bin/python tools/check_workstream_scope.py --stream overseas --base origin/main
```

Expected: because this approved design intentionally changes shared core, the checker may exit 2 and list those shared files. Treat that output as the required integration transfer list, not as a green feature-scope result. Verify every shared path belongs to one of the separately recorded shared commits and no unrelated path appears.

- [ ] **Step 7: Write the verification report without overstating platform status**

Record:

- branch and commit hashes;
- each new and affected test command, count and exit status;
- offscreen smoke result;
- scope-check output and the exact shared commit handoff order;
- feature flag default state;
- explicit statement that no Facebook login, upload, preflight or formal publish occurred;
- one next action: integration review of shared commits before true platform validation.

- [ ] **Step 8: Update SOURCE_OF_TRUTH with only confirmed local facts**

Add a dated entry that says Facebook Page V1 local contracts are implemented only if all gates above passed. Keep login, platform-form and public-publish status unverified. Do not change app version or packaging status.

- [ ] **Step 9: Commit verification evidence separately**

```bash
git add \
  docs/superpowers/reports/2026-08-30-facebook-page-publish-verification.md \
  SOURCE_OF_TRUTH.md
git commit -m "docs(facebook): record local Page publish verification"
```

- [ ] **Step 10: Prepare the ordered integration handoff**

List every shared commit hash in dependency order:

1. Page identity/database.
2. Page login/UI.
3. Request/fingerprint.
4. Authorization/claim.
5. Publish-service enforcement.
6. Task lifecycle/recovery.
7. UI/CLI/MCP convergence.

The integration line must cherry-pick/review these commits, rerun the shared full suite, and only then enable a developer-only true-platform validation build. The feature branch must not merge itself to `main` and must not package.

---

### Task 12: Perform Layered True-platform Validation under Fresh Explicit Gates

**Files:**
- Modify after each authorized layer: `docs/superpowers/reports/2026-08-30-facebook-page-publish-verification.md`
- Modify after each authorized layer: `SOURCE_OF_TRUTH.md`

**Contract:** each layer requires its own explicit authorization at the moment of the external action. Passing one layer does not authorize the next.

Run every true-platform layer only after the integration line has accepted all shared commits, using the same exact integrated commit/build recorded in the verification report. Evidence produced from the unintegrated feature worktree cannot certify the integrated client.

- [ ] **Step 1: Stop and obtain authorization for Facebook Page login only**

Before opening Facebook, present the exact Page-login action and state that it will create/update one local Page binding but will not upload or publish. Do not proceed from implementation-plan approval alone.

- [ ] **Step 2: Validate one Page binding and restart readback**

With authorization, use the visible login flow, discover manageable Pages, let the user select an exact Page if more than one exists, save one type 9 row, close the source client, restart it, and run the read-only account check. Record only Page ID hash/tail, Page name, account ID, status and timestamps; do not record credentials.

Expected evidence: the same Page ID/name/avatar is read after restart, no Instagram row is auto-created, and no video is uploaded. At this point the maximum claim is “Facebook Page 登录可用”.

- [ ] **Step 3: Stop and obtain authorization for one real platform preflight**

Explain that preflight opens Meta Business Suite and may upload a temporary video or leave an unsubmitted draft, but will not click the final button. Require an explicit test-video path/content package and target account ID.

- [ ] **Step 4: Run one real preflight and read back every form field**

Run through the controlled CLI or UI with `mode=preflight`. Verify exact Page ID, one video, full caption order, public visibility, one enabled final button, `platformWriteOccurred=true` where applicable and `finalActionTriggered=false`. Read the task back through the same task-status contract.

Expected evidence: phase `platform_form_verified`, zero final-button clicks and no Reel ID/URL. At this point the maximum claim is “Facebook Page 真实发布前表单回读通过”.

- [ ] **Step 5: Stop and obtain separate authorization for one public test Reel**

Show the exact Page, video, final caption, visibility and immediate-public action. Create a fresh one-time authorization bound to the successful preflight. Do not reuse an expired/consumed authorization or infer consent from the preflight.

- [ ] **Step 6: Execute one formal publish and read back the unique Reel**

Run one controlled formal task. If verification is required, pause for the user and continue the same browser context. Record the pre-click baseline, one `final_action_claimed`, at most one `facebook_final_action_clicked`, then read the same Page content list. Success requires one new matching Reel with real platform ID, canonical URL and timestamp.

Expected evidence: task phase `published_readback_confirmed`, status success and the same safe receipt over UI/CLI/MCP task status. Only now may the project say “Facebook Page 公开发布已验证”.

- [ ] **Step 7: Handle any inconclusive result without re-publishing**

If the worker stops after `final_action_claimed`, mark the task ambiguous, block replay and run only the explicit read-only reconciliation. Do not create another formal task until reconciliation proves `confirmed_not_published` and the user provides a new preflight plus a new authorization.

- [ ] **Step 8: Record true-platform evidence as a separate commit**

After the authorized layer ends, update the verification report and SOURCE_OF_TRUTH with exact achieved layer, task IDs, safe receipt fields and unresolved risks. Do not include screenshots or logs containing credentials. Commit the evidence without version bump or packaging:

```bash
git add \
  docs/superpowers/reports/2026-08-30-facebook-page-publish-verification.md \
  SOURCE_OF_TRUTH.md
git commit -m "docs(facebook): record true Page validation evidence"
```

---

## Definition of Done

The implementation is locally complete only when Tasks 1–11 pass and the shared commits have been accepted by the integration line. Facebook Page public publishing is externally verified only when Task 12 reaches Step 6 with a real same-Page Reel ID/URL readback. A green offline suite, a visible browser, a successful login, a filled form or a generic Meta success message cannot substitute for that result.
