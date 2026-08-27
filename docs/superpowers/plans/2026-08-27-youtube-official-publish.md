# YouTube Official Publish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 YouTube OAuth 账号头像和后台入口，并建立桌面 UI、CLI、MCP 共用的官方 API 视频发布通道，支持私密、不公开、公开和定时公开。

**Architecture:** YouTube OAuth 账号只使用系统凭据库与 YouTube Data API，不再进入 Cookie 浏览器执行器。正式发布始终先创建私密视频并回读精确 ID，再设置封面和目标可见性，最后按同一 ID 回读终态；任何结果不明都保留视频 ID并禁止盲目重传。

**Tech Stack:** Python 3.11、PyQt6、SQLite、requests、Google OAuth 2.0、YouTube Data API v3、unittest、MCP stdio。

**Spec:** `docs/superpowers/specs/2026-08-27-youtube-official-publish-design.md`

## Global Constraints

- 执行前使用 `superpowers:using-git-worktrees` 从包含本计划的最新 `main` 提交创建 `integration/youtube-official-publish-v3`；该提交的设计基线是 `760cd03`，不得直接在 `main` 修改功能代码。
- 严格先写失败测试，再写最小实现；每项任务单独提交并完成审查。
- YouTube OAuth 账号不得进入 Cookie/Playwright 上传器，也不得把刷新令牌、访问令牌、客户端密钥、头像原始 URL 查询参数写入数据库、日志、Git 或任务回执。
- 内容包保持 `oneclick-content/v1` 的纯净内容字段；可见性、儿童内容声明、通知订阅者和排期只存在于发布目标设置。
- 默认可见性为 `private`；正式发布必须明确提供 `madeForKids`。
- 正式上传必须消费绑定账号、内容、封面、可见性和排期的一次性授权。
- 视频必须先以私密状态上传；提供自定义封面时，封面设置失败不得继续调整为公开或不公开。
- 已获得视频 ID 后失败不得重新上传；结果不明时进入明确终态并先做只读核对。
- 第一次真实平台验收只允许一个无害视频的私密上传；公开、不公开和定时公开不得在本计划内擅自真实执行。
- 功能实现、真实私密验收、版本升级、打包和安装是不同结果，必须分别记录。

---

## File Structure

### 新建文件

- `app_core/overseas_youtube_profile.py`：OAuth 频道资料刷新、安全头像下载和 Studio 地址生成。
- `app_core/overseas_youtube_publish.py`：YouTube 目标设置校验、视频状态/封面 API、私密优先发布编排和稳定错误码。
- `test_overseas_youtube_profile.py`：频道资料、头像安全和后台地址测试。
- `test_overseas_youtube_publish.py`：四种发布方式、封面、精确回读、结果不明和防重传测试。
- `docs/verification/youtube-private-publish-2026-08-27.md`：首次私密真实验收记录；只有实际验收后创建。

### 修改文件

- `app_core/overseas_youtube_api.py`：频道身份增加头像 URL；保留现有私密可恢复上传器。
- `app_core/overseas_youtube_oauth.py`、`app_core/overseas_youtube_login.py`：新增发布权限和同频道授权升级。
- `app_core/database.py`、`app_core/account_service.py`、`app_core/account_browser_service.py`：授权版本、可发布账号、头像和 Studio 入口。
- `app_core/controlled_publish.py`、`app_core/content_project_gateway.py`、`app_core/oneclick_mcp_server.py`：YouTube 任务设置、项目调用和 MCP 合同。
- `app_core/publish_service.py`、`app_core/task_service.py`：官方 API 路由、阶段、错误码与非敏感回执。
- `ui/account_page.py`、`ui/publish_page.py`：账号资料操作和四种发布设置。
- 对应 `test_*.py`、`docs/OVERSEAS_PLATFORM_STATUS.md`、`SOURCE_OF_TRUTH.md`、版本与构建文件。

## Design Coverage

- 设计第 1-4 节由全局约束、文件结构和所有任务共同落实。
- 第 5 节对应 Tasks 1-2；第 6 节对应 Task 3。
- 第 7 节对应 Tasks 4 与 8；第 8-10 节对应 Tasks 5-7。
- 第 11 节对应 Task 8；第 12 节对应 Task 9。
- 第 13 节对应 Task 10；第 14 节的迁移、防重传与回滚对应 Tasks 3、6、7、10、11。
- 第 15 节的最终验收由 Tasks 9-11 收口，且真实平台只验证私密上传。

---

### Task 1: Retain the Official YouTube Channel Avatar

**Files:**
- Modify: `app_core/overseas_youtube_api.py:111-126,256-305,963-970`
- Test: `test_overseas_youtube_api.py:YouTubeChannelIdentityTests`

**Interfaces:**
- Consumes: `YouTubeChannelIdentityClient.lookup_authenticated_channel(access_token: str)`。
- Produces: `YouTubeChannelIdentity(channel_id: str, display_name: str | None, avatar_url: str | None)`。

- [ ] **Step 1: Write the failing avatar selection tests**

```python
def test_lookup_keeps_largest_https_channel_avatar(self) -> None:
    client, _transport = self._lookup({
        "items": [{"id": "UC_stable_channel", "snippet": {
            "title": "Channel Name",
            "thumbnails": {
                "default": {"url": "https://yt3.ggpht.com/small", "width": 88},
                "high": {"url": "https://yt3.ggpht.com/high", "width": 800},
            },
        }}]
    })
    identity = client.lookup_authenticated_channel("access-token-secret")
    self.assertEqual(identity.avatar_url, "https://yt3.ggpht.com/high")

def test_lookup_ignores_non_https_or_malformed_avatar_candidates(self) -> None:
    client, _transport = self._lookup({
        "items": [{"id": "UC_stable_channel", "snippet": {
            "title": "Channel Name",
            "thumbnails": {
                "default": {"url": "http://example.test/avatar", "width": 88},
                "high": {"url": 123, "width": 800},
            },
        }}]
    })
    self.assertIsNone(client.lookup_authenticated_channel("token").avatar_url)
```

- [ ] **Step 2: Run the tests and confirm the new field is missing**

```bash
.venv/bin/python -m unittest -v \
  test_overseas_youtube_api.YouTubeChannelIdentityTests.test_lookup_keeps_largest_https_channel_avatar \
  test_overseas_youtube_api.YouTubeChannelIdentityTests.test_lookup_ignores_non_https_or_malformed_avatar_candidates
```

Expected: FAIL because `YouTubeChannelIdentity` has no `avatar_url`.

- [ ] **Step 3: Add the immutable field and deterministic selector**

```python
@dataclass(frozen=True, slots=True)
class YouTubeChannelIdentity:
    channel_id: str
    display_name: str | None = None
    avatar_url: str | None = None


def _channel_avatar_url(item: Mapping[str, object]) -> str | None:
    snippet = item.get("snippet")
    thumbnails = snippet.get("thumbnails") if isinstance(snippet, Mapping) else None
    if not isinstance(thumbnails, Mapping):
        return None
    candidates: list[tuple[int, str]] = []
    for raw in thumbnails.values():
        if not isinstance(raw, Mapping):
            continue
        url = raw.get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            continue
        width = raw.get("width")
        height = raw.get("height")
        score = max(
            width if type(width) is int and width > 0 else 0,
            height if type(height) is int and height > 0 else 0,
        )
        candidates.append((score, url))
    return max(candidates, default=(0, ""))[1] or None
```

Return `avatar_url=_channel_avatar_url(item)` and retain all secret-redaction properties.

- [ ] **Step 4: Run the complete YouTube API contract tests**

```bash
.venv/bin/python -m unittest -v test_overseas_youtube_api
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app_core/overseas_youtube_api.py test_overseas_youtube_api.py
git commit -m "feat: retain YouTube channel avatar identity"
```

---

### Task 2: Refresh OAuth Profile and Open YouTube Studio

**Files:**
- Create: `app_core/overseas_youtube_profile.py`
- Create: `test_overseas_youtube_profile.py`
- Modify: `app_core/account_service.py:223-265,596-609,809-860`
- Modify: `app_core/account_browser_service.py:16-85`
- Modify: `ui/account_page.py:390-460,637-680`
- Test: `test_account_detection_ui.py`

**Interfaces:**
- Consumes: `validate_saved_youtube_oauth_account(...) -> YouTubeChannelIdentity` and Task 1 `avatar_url`。
- Produces: `refresh_youtube_oauth_profile(account, *, avatar_dir, downloader=None) -> dict[str, str]` and `youtube_studio_url(account) -> str`。

- [ ] **Step 1: Write failing security and profile tests**

```python
def test_studio_url_uses_saved_public_channel_id(self) -> None:
    self.assertEqual(
        youtube_studio_url({"accountReference": "UC_safe"}),
        "https://studio.youtube.com/channel/UC_safe",
    )

def test_avatar_downloader_rejects_unapproved_host_before_request(self) -> None:
    transport = FakeAvatarTransport()
    downloader = YouTubeAvatarDownloader(transport)
    with self.assertRaisesRegex(YouTubeProfileError, "^youtube_avatar_url_invalid$"):
        downloader.download("https://127.0.0.1/avatar.png", account_id=7)
    self.assertEqual(transport.calls, [])

def test_refresh_atomically_replaces_avatar(self) -> None:
    with tempfile.TemporaryDirectory() as raw:
        with patch(
            "app_core.overseas_youtube_profile.validate_saved_youtube_oauth_account",
            return_value=YouTubeChannelIdentity(
                "UC_safe", "Safe Channel", "https://yt3.ggpht.com/avatar"
            ),
        ):
            result = refresh_youtube_oauth_profile(
                youtube_account(7),
                avatar_dir=Path(raw),
                downloader=FakeAvatarDownloader(valid_png_bytes()),
            )
    self.assertEqual(result["avatarFileName"], "oneclick_account_7.png")
```

- [ ] **Step 2: Verify the module is absent**

```bash
.venv/bin/python -m unittest -v test_overseas_youtube_profile
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement URL validation, bounded download and atomic replacement**

```python
_APPROVED_AVATAR_HOSTS = frozenset({
    "yt3.ggpht.com", "yt3.googleusercontent.com", "lh3.googleusercontent.com"
})
MAX_AVATAR_BYTES = 5 * 1024 * 1024


def youtube_studio_url(account: Mapping[str, object]) -> str:
    channel_id = str(account.get("accountReference") or "").strip()
    if re.fullmatch(r"UC[A-Za-z0-9_-]+", channel_id):
        return f"https://studio.youtube.com/channel/{channel_id}"
    return "https://studio.youtube.com/"
```

`YouTubeAvatarDownloader.download` must require HTTPS, exact approved hostname, port 443/default, no user info and at most three validated redirects. It reads at most 5 MiB, decodes the image, re-encodes PNG, writes `.{name}.tmp`, flushes and `os.fsync`, then atomically replaces the old avatar. Failure preserves the old file.

- [ ] **Step 4: Route OAuth account actions**

```python
def refresh_account_avatar(account_id: int) -> dict:
    account = get_managed_account(int(account_id))
    if not account:
        return {}
    if account.get("authMode") == AUTH_MODE_YOUTUBE_OAUTH:
        profile = refresh_youtube_oauth_profile(account, avatar_dir=AVATAR_DIR)
        _save_youtube_public_profile(
            int(account_id), profile["displayName"], profile["avatarFileName"]
        )
    else:
        run_async_capture_account_avatar(int(account_id))
    return get_managed_account(int(account_id)) or {}
```

In `open_account_backend`, call `webbrowser.open(youtube_studio_url(account))` for OAuth accounts; keep the existing Playwright path for browser accounts. Remove OAuth action disabling from both account-page action menus and set accurate tooltips.

- [ ] **Step 5: Run profile and account UI tests**

```bash
.venv/bin/python -m unittest -v test_overseas_youtube_profile test_account_detection_ui
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app_core/overseas_youtube_profile.py app_core/account_service.py \
  app_core/account_browser_service.py ui/account_page.py \
  test_overseas_youtube_profile.py test_account_detection_ui.py
git commit -m "feat: refresh YouTube OAuth account profile"
```

---

### Task 3: Upgrade OAuth Scopes Without Rebinding the Channel

**Files:**
- Modify: `app_core/overseas_youtube_oauth.py:18-24,340-365,535-560`
- Modify: `app_core/overseas_youtube_login.py:21-38,130-335`
- Modify: `app_core/database.py:542-580`
- Modify: `app_core/account_service.py:223-380,531-590`
- Test: `test_overseas_youtube_oauth.py`
- Test: `test_overseas_youtube_login.py`
- Test: `test_overseas_integration.py`

**Interfaces:**
- Consumes: existing Keyring credential reference and saved channel ID。
- Produces: `YOUTUBE_OAUTH_SCOPE_VERSION = 2`, `oauthScopeVersion`, and redacted `YouTubeAuthorizedSession`。

- [ ] **Step 1: Write failing scope and migration tests**

```python
def test_required_scopes_include_force_ssl_for_status_updates(self) -> None:
    self.assertIn(
        "https://www.googleapis.com/auth/youtube.force-ssl",
        YOUTUBE_REQUIRED_SCOPES,
    )
    self.assertEqual(YOUTUBE_OAUTH_SCOPE_VERSION, 2)

def test_update_mode_rejects_different_channel_before_replacing_token(self) -> None:
    session, evidence = self._session(
        update_mode=True,
        existing_account=youtube_account(channel_id="UC_original", scope_version=1),
        channel_identity=YouTubeChannelIdentity("UC_other", "Other"),
    )
    with self.assertRaisesRegex(YouTubeOAuthLoginError, "^channel_identity_mismatch$"):
        session.run()
    self.assertEqual(evidence["saved_tokens"], [])
```

Add a database test proving legacy OAuth rows read `oauthScopeVersion=1` after schema migration.

- [ ] **Step 2: Verify scope/version failures**

```bash
.venv/bin/python -m unittest -v test_overseas_youtube_oauth test_overseas_youtube_login
```

Expected: FAIL because the new scope and version field are absent.

- [ ] **Step 3: Add scope constants and schema migration**

```python
YOUTUBE_FORCE_SSL_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"
YOUTUBE_REQUIRED_SCOPES = frozenset({
    YOUTUBE_READONLY_SCOPE, YOUTUBE_UPLOAD_SCOPE, YOUTUBE_FORCE_SSL_SCOPE
})
YOUTUBE_OAUTH_SCOPE = " ".join(sorted(YOUTUBE_REQUIRED_SCOPES))
YOUTUBE_OAUTH_SCOPE_VERSION = 2
```

Add `("oauthScopeVersion", "INTEGER NOT NULL DEFAULT 1")` to `user_info`. Include it in managed/publishable account queries. `save_youtube_oauth_account` writes version 2 only after the callback channel ID matches the existing record.

- [ ] **Step 4: Add a secret-safe authorized session**

```python
@dataclass(frozen=True, slots=True, repr=False)
class YouTubeAuthorizedSession:
    access_token: str = field(repr=False)
    identity: YouTubeChannelIdentity
    credential_reference: str


def authorize_saved_youtube_account(
    account: Mapping[str, object],
    *,
    client_id: str,
    credential_store=None,
    client_secret_store=None,
    token_client=None,
    channel_client=None,
    require_publish_scope: bool = False,
) -> YouTubeAuthorizedSession:
    normalized_client_id = str(client_id or "").strip()
    if not normalized_client_id:
        raise YouTubeOAuthLoginError("youtube_oauth_client_not_configured")
    if (
        not isinstance(account, Mapping)
        or int(account.get("type") or 0) != 7
        or str(account.get("authMode") or "") != YOUTUBE_OAUTH_AUTH_MODE
    ):
        raise YouTubeOAuthLoginError("authorization_invalid")
    if require_publish_scope and int(account.get("oauthScopeVersion") or 1) < 2:
        raise YouTubeOAuthLoginError("youtube_oauth_scope_upgrade_required")
    reference = str(account.get("filePath") or "").strip()
    expected_channel = str(account.get("accountReference") or "").strip()
    if not reference or not expected_channel:
        raise YouTubeOAuthLoginError("credential_unavailable")
    store = credential_store or KeyringOAuthCredentialStore()
    secret_store = client_secret_store or KeyringOAuthClientSecretStore()
    transport = requests.Session()
    token_service = token_client or YouTubeOAuthTokenClient(
        transport, clock=time.time
    )
    channel_service = channel_client or YouTubeChannelIdentityClient(transport)
    refresh_token = store.load_refresh_token(reference)
    client_secret = secret_store.load_client_secret(normalized_client_id)
    if not refresh_token:
        raise YouTubeOAuthLoginError("credential_unavailable")
    if not client_secret:
        raise YouTubeOAuthLoginError(
            "youtube_oauth_client_secret_not_configured"
        )
    refreshed = token_service.refresh_access_token(
        client_id=normalized_client_id,
        client_secret=client_secret,
        existing_tokens=OAuthTokens(
            access_token="refresh_pending",
            refresh_token=refresh_token,
            expires_at=0.0,
            scope=YOUTUBE_OAUTH_SCOPE,
        ),
    )
    identity = channel_service.lookup_authenticated_channel(
        refreshed.access_token
    )
    if identity.channel_id != expected_channel:
        raise YouTubeOAuthLoginError("channel_identity_mismatch")
    if refreshed.refresh_token != refresh_token:
        store.save_refresh_token(reference, refreshed.refresh_token)
    return YouTubeAuthorizedSession(
        access_token=refreshed.access_token,
        identity=identity,
        credential_reference=reference,
    )
```

Wrap credential, token and channel lookup exceptions with the existing secret-safe
error mapping. `validate_saved_youtube_oauth_account` returns
`authorize_saved_youtube_account(..., require_publish_scope=False).identity`; the
publish executor uses `True`.

- [ ] **Step 5: Run OAuth and integration tests**

```bash
.venv/bin/python -m unittest -v \
  test_overseas_youtube_oauth test_overseas_youtube_login test_overseas_integration
```

Expected: PASS; secrets remain absent from repr, SQLite and logs.

- [ ] **Step 6: Commit**

```bash
git add app_core/overseas_youtube_oauth.py app_core/overseas_youtube_login.py \
  app_core/database.py app_core/account_service.py \
  test_overseas_youtube_oauth.py test_overseas_youtube_login.py \
  test_overseas_integration.py
git commit -m "feat: upgrade YouTube OAuth publish scopes"
```

---

### Task 4: Add YouTube Settings to Controlled UI, CLI and MCP Contracts

**Files:**
- Modify: `app_core/account_service.py:223-265`
- Modify: `app_core/controlled_publish.py:28-45,120-290`
- Modify: `app_core/content_project_gateway.py:120-410`
- Modify: `app_core/oneclick_mcp_server.py:75-160`
- Test: `test_controlled_publish.py`
- Test: `test_content_project_gateway.py`
- Test: `test_oneclick_mcp_server.py`

**Interfaces:**
- Consumes: Task 3 managed account rows with `authMode` and `oauthScopeVersion`。
- Produces: `list_publishable_accounts()`, controlled target `settings`, and MCP `settings: Mapping[str, Mapping[str, Any]] | None`。

- [ ] **Step 1: Add failing controlled-payload tests**

```python
def test_youtube_target_defaults_private_and_keeps_explicit_audience(self) -> None:
    payload = build_controlled_payloads(
        youtube_request(
            mode="preflight",
            settings={"visibility": "private", "madeForKids": False},
        ),
        accounts=[youtube_oauth_account()],
    )[0]
    self.assertEqual(payload["visibility"], "private")
    self.assertIs(payload["madeForKids"], False)
    self.assertTrue(payload["youtubeOfficialApi"])

def test_youtube_schedule_requires_scheduled_public(self) -> None:
    request = youtube_request(
        mode="preflight",
        schedule={"localTime": "2026-08-28 09:00", "timezone": "Asia/Shanghai"},
        settings={"visibility": "private", "madeForKids": False},
    )
    with self.assertRaises(ControlledPublishError) as raised:
        build_controlled_payloads(request, accounts=[youtube_oauth_account()])
    self.assertEqual(raised.exception.error_code, "youtube_schedule_invalid")

def test_youtube_settings_change_authorization_fingerprint(self) -> None:
    base = [{"type": 7, "accountIds": [12], "visibility": "private", "madeForKids": False}]
    changed = [{**base[0], "visibility": "unlisted"}]
    self.assertNotEqual(scope_fingerprint(base), scope_fingerprint(changed))
```

- [ ] **Step 2: Run the focused tests**

```bash
.venv/bin/python -m unittest -v \
  test_controlled_publish.ControlledPublishTests.test_youtube_target_defaults_private_and_keeps_explicit_audience \
  test_controlled_publish.ControlledPublishTests.test_youtube_schedule_requires_scheduled_public \
  test_controlled_publish.ControlledPublishTests.test_youtube_settings_change_authorization_fingerprint
```

Expected: FAIL because target `settings` is rejected and OAuth accounts are absent from publishing.

- [ ] **Step 3: Add platform-aware publishable accounts**

```python
def list_publishable_accounts() -> list[dict]:
    return [
        row
        for row in list_managed_accounts()
        if row.get("authMode") == AUTH_MODE_BROWSER
        or (
            int(row.get("type") or 0) == 7
            and row.get("authMode") == AUTH_MODE_YOUTUBE_OAUTH
        )
    ]
```

Keep old-scope YouTube rows visible so the UI can show “需要升级发布权限”; formal validation returns `youtube_oauth_scope_upgrade_required` rather than hiding the account.

- [ ] **Step 4: Normalize YouTube settings**

Add `settings` to `_TARGET_KEYS` and implement:

```python
_YOUTUBE_VISIBILITIES = frozenset({
    "private", "unlisted", "public", "scheduled_public"
})


def _youtube_settings(value: object, *, mode: str) -> dict[str, object]:
    settings = {} if value is None else _require_mapping(
        value, "youtube_settings_invalid", "YouTube 发布设置必须是对象"
    )
    if set(settings) - {"visibility", "madeForKids", "notifySubscribers"}:
        raise ControlledPublishError(
            "youtube_settings_invalid", "YouTube 发布设置包含不支持字段"
        )
    visibility = str(settings.get("visibility") or "private").strip().lower()
    if visibility not in _YOUTUBE_VISIBILITIES:
        raise ControlledPublishError("youtube_visibility_invalid", "YouTube 可见性无效")
    audience = settings.get("madeForKids")
    if mode in {"formal", "direct"} and type(audience) is not bool:
        raise ControlledPublishError(
            "youtube_audience_required", "YouTube 正式发布必须明确选择是否面向儿童"
        )
    if audience is not None and type(audience) is not bool:
        raise ControlledPublishError("youtube_audience_invalid", "YouTube 受众设置无效")
    notify = settings.get("notifySubscribers", True)
    if type(notify) is not bool:
        raise ControlledPublishError("youtube_notify_invalid", "YouTube 通知设置无效")
    return {
        "visibility": visibility,
        "madeForKids": audience,
        "notifySubscribers": notify,
    }
```

For platform 7 require `authMode=youtube_oauth`, exactly one account and one video; formal/direct also require scope version 2. Set `youtubeOfficialApi=True` and `backgroundMode=True`. A schedule is valid only for `scheduled_public`; that visibility must carry a schedule.

- [ ] **Step 5: Extend project gateway and MCP arguments**

Extend `_request`, `preflight_content`, `formal_publish`, and `direct_publish_content` with:

```python
settings: Mapping[str, Mapping[str, Any]] | None = None
```

Normalize settings by canonical platform name, reject keys for platforms not configured in the profile, and attach the platform settings to each target. Change the default account provider to `account_service.list_publishable_accounts`. Add the same optional argument to the three MCP publish tools.

- [ ] **Step 6: Add gateway/MCP forwarding tests**

```python
def test_direct_publish_forwards_youtube_settings(self) -> None:
    gateway.direct_publish_content(
        "youtube-test",
        "/content/manifest.json",
        settings={
            "YouTube": {
                "visibility": "private",
                "madeForKids": False,
                "notifySubscribers": False,
            }
        },
    )
    target = submitted[0]["targets"][0]
    self.assertEqual(target["settings"]["visibility"], "private")
    self.assertIs(target["settings"]["madeForKids"], False)
```

Update the fake MCP gateway to record `settings`; assert the generated tool schema accepts it and the tool forwards it unchanged.

- [ ] **Step 7: Run controlled contract tests**

```bash
.venv/bin/python -m unittest -v \
  test_controlled_publish test_content_project_gateway test_oneclick_mcp_server
```

Expected: PASS; content manifest remains unchanged and default YouTube visibility is private.

- [ ] **Step 8: Commit**

```bash
git add app_core/account_service.py app_core/controlled_publish.py \
  app_core/content_project_gateway.py app_core/oneclick_mcp_server.py \
  test_controlled_publish.py test_content_project_gateway.py \
  test_oneclick_mcp_server.py
git commit -m "feat: add controlled YouTube publish settings"
```

---

### Task 5: Add Thumbnail and Video Status API Contracts

**Files:**
- Create: `app_core/overseas_youtube_publish.py`
- Create: `test_overseas_youtube_publish.py`
- Modify: `app_core/overseas_youtube_api.py` only if a shared safe parser is needed.

**Interfaces:**
- Consumes: exact private video ID returned by `YouTubePrivateUploadAdapter`。
- Produces: `YouTubePublishSettings`, `YouTubeVideoStatus`, `ValidatedYouTubePublish`, and `YouTubeVideoManagementClient`。

- [ ] **Step 1: Write failing local validation tests**

```python
def test_scheduled_public_converts_beijing_time_to_utc(self) -> None:
    checked = validate_youtube_publish_payload(
        youtube_payload(
            visibility="scheduled_public",
            enableTimer=True,
            scheduleTime="2026-08-28 09:00",
            scheduleTimezone="Asia/Shanghai",
            madeForKids=False,
        ),
        now=datetime(2026, 8, 27, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    self.assertEqual(checked.settings.publish_at, "2026-08-28T01:00:00Z")

def test_custom_thumbnail_over_two_megabytes_is_rejected(self) -> None:
    cover = self.root / "cover.png"
    cover.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    with self.assertRaisesRegex(YouTubeOfficialPublishError, "^youtube_thumbnail_invalid$"):
        validate_youtube_publish_payload(youtube_payload(coverPath=str(cover)))
```

- [ ] **Step 2: Verify the module is absent**

```bash
.venv/bin/python -m unittest -v \
  test_overseas_youtube_publish.YouTubePublishValidationTests
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement immutable validation contracts**

```python
@dataclass(frozen=True, slots=True)
class YouTubePublishSettings:
    visibility: str
    made_for_kids: bool
    notify_subscribers: bool
    publish_at: str | None
    thumbnail_path: Path | None


@dataclass(frozen=True, slots=True)
class ValidatedYouTubePublish:
    private_upload: ValidatedYouTubeUpload
    settings: YouTubePublishSettings
    account_id: int
    credential_reference: str
    expected_channel_id: str
```

Require one OAuth account, one video, explicit Boolean audience, supported visibility, JPEG/PNG thumbnail no larger than 2 MiB, and a scheduled time at least 15 minutes in the future. Call existing `local_preflight` with an internal private-only `YouTubeUploadRequest`; final visibility must not weaken the current private upload adapter.

- [ ] **Step 4: Write failing management-client tests**

```python
def test_scheduled_update_targets_exact_video_id(self) -> None:
    transport = FakeManagementTransport(
        get_responses=[video_status_response("private", publish_at=None)],
        put_responses=[video_status_response("private", "2026-08-28T01:00:00Z")],
    )
    client = YouTubeVideoManagementClient(transport)
    result = client.apply_visibility(
        "access-secret",
        video_id="video-1",
        target_visibility="scheduled_public",
        publish_at="2026-08-28T01:00:00Z",
        made_for_kids=False,
    )
    body = transport.put_calls[0]["json"]
    self.assertEqual(body["id"], "video-1")
    self.assertEqual(body["status"]["privacyStatus"], "private")
    self.assertEqual(body["status"]["publishAt"], "2026-08-28T01:00:00Z")
    self.assertEqual(result.publish_at, "2026-08-28T01:00:00Z")

def test_thumbnail_posts_media_to_exact_video_id(self) -> None:
    transport = FakeManagementTransport(post_responses=[FakeResponse(200, {})])
    client = YouTubeVideoManagementClient(transport)
    client.set_thumbnail("access-secret", video_id="video-1", path=self.cover)
    self.assertEqual(
        transport.post_calls[0]["params"],
        {"videoId": "video-1", "uploadType": "media"},
    )
    self.assertNotIn("access-secret", repr(client))
```

- [ ] **Step 5: Implement exact-ID status, thumbnail and visibility methods**

```python
class YouTubeVideoManagementClient:
    def read_status(self, access_token: str, *, video_id: str) -> YouTubeVideoStatus:
        response = self._transport.get(
            YOUTUBE_VIDEOS_ENDPOINT,
            headers=_bearer(access_token),
            params={"part": "id,snippet,status,processingDetails", "id": video_id},
            timeout=self._timeout_seconds,
            allow_redirects=False,
        )
        return _parse_exact_video_status(response, video_id=video_id)

    def set_thumbnail(self, access_token: str, *, video_id: str, path: Path) -> None:
        response = self._transport.post(
            YOUTUBE_THUMBNAILS_UPLOAD_ENDPOINT,
            headers={**_bearer(access_token), "Content-Type": _thumbnail_mime(path)},
            params={"videoId": video_id, "uploadType": "media"},
            data=path.read_bytes(),
            timeout=self._timeout_seconds,
            allow_redirects=False,
        )
        if response.status_code != 200:
            raise YouTubeOfficialPublishError("youtube_thumbnail_forbidden")
```

`apply_visibility` first reads the current status, preserves supported existing fields, changes only `privacyStatus`, `publishAt`, and `selfDeclaredMadeForKids`, then sends `PUT videos.update(part=status)` with the exact ID. Never include raw provider response bodies in exceptions.

- [ ] **Step 6: Run all management tests**

```bash
.venv/bin/python -m unittest -v test_overseas_youtube_publish
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app_core/overseas_youtube_publish.py test_overseas_youtube_publish.py \
  app_core/overseas_youtube_api.py
git commit -m "feat: add YouTube video management API"
```

---

### Task 6: Orchestrate Private-First Official Publishing

**Files:**
- Modify: `app_core/overseas_youtube_publish.py`
- Modify: `test_overseas_youtube_publish.py`

**Interfaces:**
- Consumes: `authorize_saved_youtube_account(..., require_publish_scope=True)`, `YouTubePrivateUploadAdapter`, and Task 5 management client。
- Produces: `run_youtube_preflight_sync(payload) -> dict` and `run_youtube_publish_sync(payload, *, task_id=0, progress=None, dependencies=None) -> dict`。

- [ ] **Step 1: Write failing private-first orchestration test**

```python
def test_uploads_private_then_thumbnail_then_unlisted_and_reads_back(self) -> None:
    deps = fake_dependencies(final_visibility="unlisted")
    result = run_youtube_publish_sync(
        youtube_payload(visibility="unlisted", madeForKids=False),
        task_id=41,
        dependencies=deps,
    )
    self.assertEqual(
        deps.operations,
        ["authorize", "upload_private", "read_private", "set_thumbnail", "apply_unlisted", "read_final"],
    )
    self.assertEqual(result["receipt"]["videoId"], "video-1")
    self.assertEqual(result["receipt"]["visibility"], "unlisted")
```

- [ ] **Step 2: Write failing safety tests**

```python
def test_thumbnail_failure_leaves_private_and_never_applies_visibility(self) -> None:
    deps = fake_dependencies(thumbnail_error="youtube_thumbnail_forbidden")
    with self.assertRaises(YouTubeOfficialPublishError) as raised:
        run_youtube_publish_sync(
            youtube_payload(visibility="public", madeForKids=False),
            dependencies=deps,
        )
    self.assertEqual(raised.exception.error_code, "youtube_thumbnail_forbidden")
    self.assertEqual(raised.exception.receipt["videoId"], "video-1")
    self.assertNotIn("apply_public", deps.operations)

def test_unknown_upload_outcome_never_starts_second_upload(self) -> None:
    deps = fake_dependencies(upload_error="outcome_unknown")
    with self.assertRaises(YouTubeOfficialPublishError) as raised:
        run_youtube_publish_sync(youtube_payload(madeForKids=False), dependencies=deps)
    self.assertEqual(raised.exception.error_code, "youtube_upload_outcome_unknown")
    self.assertEqual(deps.upload_attempts, 1)
```

- [ ] **Step 3: Verify orchestration functions are absent**

```bash
.venv/bin/python -m unittest -v \
  test_overseas_youtube_publish.YouTubeOfficialPublishServiceTests
```

Expected: FAIL.

- [ ] **Step 4: Implement zero-write preflight**

```python
def run_youtube_preflight_sync(payload: Mapping[str, Any]) -> dict[str, Any]:
    checked = validate_youtube_publish_payload(payload)
    account = _exact_youtube_account(checked.account_id)
    session = authorize_saved_youtube_account(
        account,
        client_id=YOUTUBE_OAUTH_CLIENT_ID,
        require_publish_scope=True,
    )
    if session.identity.channel_id != checked.expected_channel_id:
        raise YouTubeOfficialPublishError("youtube_channel_identity_mismatch")
    return {
        "ok": True,
        "message": "YouTube 本地与只读账号检查通过，未创建视频",
        "receipt": {
            "visibility": checked.settings.visibility,
            "scheduledAt": checked.settings.publish_at,
            "platformMutation": "none",
        },
    }
```

Tests must assert no upload adapter or management write call occurs.

- [ ] **Step 5: Implement formal state machine**

```python
def run_youtube_publish_sync(
    payload: Mapping[str, Any],
    *,
    task_id: int = 0,
    progress: Callable[[str, Mapping[str, object]], None] | None = None,
    dependencies: YouTubePublishDependencies | None = None,
) -> dict[str, Any]:
    dependencies = dependencies or build_default_youtube_publish_dependencies()
    checked = validate_youtube_publish_payload(payload)
    session = dependencies.authorize(checked)
    _emit(progress, "uploading_private")
    video_id = dependencies.upload_private(checked.private_upload, session)
    receipt = {"videoId": video_id, "visibility": "private"}
    _emit(progress, "uploaded_private", receipt)
    if checked.settings.thumbnail_path is not None:
        _emit(progress, "setting_thumbnail", receipt)
        dependencies.video_client.set_thumbnail(
            session.access_token,
            video_id=video_id,
            path=checked.settings.thumbnail_path,
        )
        receipt["thumbnailApplied"] = True
    _emit(progress, "applying_visibility", receipt)
    dependencies.video_client.apply_visibility(
        session.access_token,
        video_id=video_id,
        target_visibility=checked.settings.visibility,
        publish_at=checked.settings.publish_at,
        made_for_kids=checked.settings.made_for_kids,
    )
    _emit(progress, "verifying", receipt)
    final = dependencies.video_client.read_status(
        session.access_token, video_id=video_id
    )
    _require_expected_final_status(final, checked.settings)
    return _success_result(final, receipt, checked.settings)
```

Map low-level failures to design error codes. A public/unlisted request that reads back private returns `youtube_api_project_private_only`. Any exception after ID creation carries the safe receipt.

- [ ] **Step 6: Run executor tests**

```bash
.venv/bin/python -m unittest -v \
  test_overseas_youtube_publish test_overseas_youtube_api test_overseas_youtube_login
```

Expected: PASS; every formal path starts private and every success has exact-ID readback.

- [ ] **Step 7: Commit**

```bash
git add app_core/overseas_youtube_publish.py test_overseas_youtube_publish.py
git commit -m "feat: orchestrate safe YouTube official publishing"
```

---

### Task 7: Route Publish Service and Persist Stable Receipts

**Files:**
- Modify: `app_core/publish_service.py:1-80,120-300,680-850`
- Modify: `app_core/task_service.py:44-170,1998-2090`
- Modify: `app_core/controlled_publish.py:577-725`
- Test: `test_overseas_publish_routing.py`
- Test: `test_publish_service.py`
- Test: `test_task_service.py`
- Test: `test_controlled_publish.py`

**Interfaces:**
- Consumes: Task 6 official preflight and formal functions。
- Produces: stored `errorCode`, allowlisted `receiptJson`, and stable projected YouTube task JSON。

- [ ] **Step 1: Write failing official-route tests**

```python
def test_youtube_oauth_preflight_uses_zero_write_official_service(self) -> None:
    payload = youtube_oauth_payload(self.video, mode="preflight")
    result = {"ok": True, "message": "只读检查通过", "receipt": {"platformMutation": "none"}}
    with (
        patch.object(
            publish_service.youtube_publish,
            "run_youtube_preflight_sync",
            return_value=result,
        ) as official,
        patch.object(
            publish_service.overseas_preflight,
            "run_overseas_preflight_sync",
        ) as browser,
        patch.object(publish_service.task_service, "mark_task_running"),
        patch.object(publish_service.task_service, "record_task_event"),
        patch.object(publish_service.task_service, "mark_platform_result"),
        patch.object(publish_service.task_service, "fail_active_task"),
    ):
        publish_service._run_preflight({"id": 101}, [payload])
    official.assert_called_once_with(payload)
    browser.assert_not_called()

def test_youtube_oauth_publish_uses_official_service_not_browser(self) -> None:
    payload = youtube_oauth_payload(self.video, mode="publish")
    result = {
        "ok": True,
        "message": "私密视频回读成功",
        "receipt": {"videoId": "video-1", "visibility": "private"},
    }
    with (
        patch.object(
            publish_service.youtube_publish,
            "run_youtube_publish_sync",
            return_value=result,
        ) as official,
        patch.object(
            publish_service.overseas_video_publish,
            "run_overseas_video_publish_sync",
        ) as browser,
        patch.object(publish_service.task_service, "mark_task_running"),
        patch.object(publish_service.task_service, "record_task_event"),
        patch.object(publish_service.task_service, "mark_platform_result"),
        patch.object(publish_service.task_service, "fail_active_task"),
    ):
        publish_service._run_publish({"id": 102}, [payload])
    official.assert_called_once()
    browser.assert_not_called()
```

- [ ] **Step 2: Write failing receipt projection test**

```python
def test_youtube_receipt_round_trips_without_tokens(self) -> None:
    mark_platform_result(
        task_id,
        7,
        ok=True,
        message="YouTube 私密上传成功",
        content_type="video",
        event_type="platform_publish",
        error_code="",
        receipt={
            "videoId": "video-1",
            "studioUrl": "https://studio.youtube.com/video/video-1/edit",
            "visibility": "private",
            "thumbnailApplied": True,
            "accessToken": "must-not-persist",
        },
    )
    platform = project_task(get_task(task_id))["platforms"][0]
    self.assertEqual(platform["receipt"]["videoId"], "video-1")
    self.assertNotIn("accessToken", platform["receipt"])
```

- [ ] **Step 3: Run focused tests and confirm old routing**

```bash
.venv/bin/python -m unittest -v \
  test_overseas_publish_routing test_task_service test_controlled_publish
```

Expected: FAIL because platform 7 still uses the browser executor and generic items do not store receipt JSON.

- [ ] **Step 4: Route platform 7 separately**

In `_validate_payloads`, validate TikTok with the existing browser contract and YouTube with `validate_youtube_publish_payload`. In execution:

```python
elif platform_type == 7 and payload.get("youtubeOfficialApi") is True:
    result = youtube_publish.run_youtube_preflight_sync(payload)
elif platform_type in {6, 8, 9}:
    result = overseas_preflight.run_overseas_preflight_sync(payload)
```

Formal route:

```python
elif platform_type == 7 and payload.get("youtubeOfficialApi") is True:
    result = youtube_publish.run_youtube_publish_sync(
        payload,
        task_id=int(task["id"]),
        progress=lambda phase, receipt: _record_youtube_progress(
            int(task["id"]), phase, receipt
        ),
    )
elif platform_type == 6:
    result = overseas_video_publish.run_overseas_video_publish_sync(payload)
```

- [ ] **Step 5: Persist allowlisted receipt fields**

Extend `mark_platform_result` with optional `error_code` and `receipt` keywords. Allow only:

```python
_YOUTUBE_RECEIPT_FIELDS = frozenset({
    "videoId", "studioUrl", "watchUrl", "visibility", "scheduledAt",
    "processingStatus", "thumbnailApplied", "platformMutation",
})
```

Store `receiptJson`, `errorCode`, `platformPostId=videoId`, and `postUrl=watchUrl`. Parse `receiptJson` for every task mode in `project_task`, prefer stored error code, and retain legacy message parsing for historical tasks.

- [ ] **Step 6: Preserve known IDs on worker loss**

Record `youtube_uploaded_private` with the safe receipt as soon as the ID exists. If the worker exits before final readback, finish with `youtube_manual_reconciliation_required` and the known ID instead of the generic no-terminal error. Keep current stale-worker lease cleanup for controlled tasks.

- [ ] **Step 7: Run service and resilience tests**

```bash
.venv/bin/python -m unittest -v \
  test_overseas_publish_routing test_publish_service test_task_service \
  test_controlled_publish test_controlled_publish_resilience
```

Expected: PASS; TikTok and Meta keep current browser routes, YouTube OAuth never uses them.

- [ ] **Step 8: Commit**

```bash
git add app_core/publish_service.py app_core/task_service.py \
  app_core/controlled_publish.py test_overseas_publish_routing.py \
  test_publish_service.py test_task_service.py test_controlled_publish.py
git commit -m "feat: route YouTube through official publish service"
```

---

### Task 8: Expose Safe YouTube Settings in the Desktop UI

**Files:**
- Modify: `ui/publish_page.py:1121-1168,1190-1225,1338-1380,1620-1665,4370-4700,5610-6065`
- Modify: `test_publish_page.py`
- Modify: `test_publish_media_selection.py`
- Modify: `test_publish_preflight_copy.py`

**Interfaces:**
- Consumes: Task 4 payload fields `visibility`, `madeForKids`, `notifySubscribers`, `enableTimer`, `scheduleTime`, `scheduleTimezone`。
- Produces: explicit YouTube selections feeding the same service as CLI/MCP。

`refresh_accounts` must read `account_service.list_publishable_accounts()` so
the OAuth channel appears in the publish center without changing the legacy
`list_accounts()` browser-session contract.

- [ ] **Step 1: Write failing UI tests**

```python
def test_youtube_defaults_private_and_requires_explicit_audience(self) -> None:
    page = PublishPage()
    self.assertEqual(page.platform_visibility[7].currentData(), "private")
    self.assertIsNone(page.youtube_made_for_kids.currentData())
    self._dispose_page(page)

def test_youtube_scheduled_public_requires_own_schedule(self) -> None:
    page = youtube_page_with_one_selected_oauth_account()
    page.platform_visibility[7].setCurrentIndex(
        page.platform_visibility[7].findData("scheduled_public")
    )
    with self.assertRaisesRegex(ValueError, "YouTube 定时公开必须设置发布时间"):
        page.collect_payloads(runtime_mode="publish")
    self._dispose_page(page)

def test_youtube_private_does_not_inherit_common_schedule(self) -> None:
    page = youtube_page_with_one_selected_oauth_account()
    enable_common_schedule(page, "2026-08-28", "09:00")
    select_youtube_audience(page, made_for_kids=False)
    payload = page.collect_payloads(runtime_mode="publish")[0]
    self.assertEqual(payload["visibility"], "private")
    self.assertFalse(payload["enableTimer"])
```

- [ ] **Step 2: Run UI tests and confirm current defaults are wrong**

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest -v test_publish_page
```

Expected: FAIL because YouTube currently inherits the public common setting and audience is not an explicit three-state choice.

- [ ] **Step 3: Build the four-state controls**

```python
visibility = QComboBox()
visibility.addItem("私密（推荐）", "private")
visibility.addItem("不公开", "unlisted")
visibility.addItem("公开", "public")
visibility.addItem("定时公开", "scheduled_public")
self.platform_visibility[7] = visibility

self.youtube_made_for_kids = QComboBox()
self.youtube_made_for_kids.addItem("请选择", None)
self.youtube_made_for_kids.addItem("不面向儿童", False)
self.youtube_made_for_kids.addItem("面向儿童", True)
```

Enable the platform 7 schedule controls only while visibility is `scheduled_public`. Selecting another visibility unchecks and disables only the YouTube-specific schedule; common settings for other platforms remain untouched.

- [ ] **Step 4: Generate a strict YouTube payload**

```python
if platform_type == 7:
    visibility = str(self.platform_visibility[7].currentData() or "private")
    audience = self.youtube_made_for_kids.currentData()
    if runtime_mode == "publish" and type(audience) is not bool:
        raise ValueError("YouTube 正式发布必须明确选择是否面向儿童")
    if visibility == "scheduled_public":
        if not self.platform_schedule_enabled[7].isChecked():
            raise ValueError("YouTube 定时公开必须设置发布时间")
        schedule_time = self._validated_schedule_time(
            self._platform_schedule_time(7), "YouTube"
        )
    else:
        schedule_time = ""
    payload.update({
        "youtubeOfficialApi": True,
        "visibility": visibility,
        "madeForKids": audience,
        "notifySubscribers": self.youtube_notify_subscribers.isChecked(),
        "enableTimer": bool(schedule_time),
        "scheduleTime": schedule_time or None,
        "scheduleTimezone": "Asia/Shanghai",
        "backgroundMode": True,
    })
```

Remove YouTube from code setting `overseasVideoPublishConfirmed` or forcing `backgroundMode=False`; TikTok keeps those browser requirements.

- [ ] **Step 5: Save, restore and summarize fields**

Save `youtubeVisibility`, `youtubeMadeForKids`, `youtubeNotifySubscribers`, and YouTube schedule fields in the existing local draft structure. Restore missing legacy audience as “请选择”, not false. Confirmation summary must show exact mode, audience, notification and time. Progress copy must say “官方 API 后台处理”.

- [ ] **Step 6: Run affected UI tests**

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest -v \
  test_publish_page test_publish_media_selection test_publish_preflight_copy
```

Expected: PASS; other platform visibility and scheduling remain unchanged.

- [ ] **Step 7: Commit**

```bash
git add ui/publish_page.py test_publish_page.py \
  test_publish_media_selection.py test_publish_preflight_copy.py
git commit -m "feat: expose safe YouTube publish settings"
```

---

### Task 9: Review, Regression and Secret-Safety Verification

**Files:**
- Modify only when a test exposes a concrete defect in Tasks 1-8.
- Do not modify version files in this task.

**Interfaces:**
- Consumes: complete source implementation。
- Produces: reviewed locally verified source candidate; no platform mutation。

- [ ] **Step 1: Run the YouTube and shared-core suite**

```bash
.venv/bin/python -m unittest -v \
  test_overseas_youtube_api test_overseas_youtube_oauth \
  test_overseas_youtube_login test_overseas_youtube_profile \
  test_overseas_youtube_publish test_overseas_integration \
  test_overseas_publish_routing test_account_detection_ui \
  test_controlled_publish test_controlled_publish_process \
  test_controlled_publish_resilience test_content_project_gateway \
  test_oneclick_mcp_server test_publish_service test_task_service test_publish_page
```

Expected: PASS.

- [ ] **Step 2: Run project-required full regression**

```bash
.venv/bin/python -m unittest discover -p 'test_*.py'
QT_QPA_PLATFORM=offscreen .venv/bin/python desktop_native_app.py --ui-test
git diff --check
```

Expected: all tests pass, UI prints `NATIVE_DESKTOP_UI_OK`, and diff check prints nothing.

- [ ] **Step 3: Run scope and secret scans**

```bash
.venv/bin/python tools/check_workstream_scope.py --stream overseas --base 760cd03 || true
git diff 760cd03 -- . \
  | rg -n 'ya29\.[A-Za-z0-9_-]{20,}|1//[A-Za-z0-9_-]{20,}|GOCSPX-[A-Za-z0-9_-]{20,}' \
  && exit 1 || true
```

Expected: shared files are acceptable only because this is the integration branch and must match this plan; the secret scan prints no credential value.

- [ ] **Step 4: Perform two-stage review**

First compare implementation to all 15 spec sections. Then review code quality, safe URL handling, exact-ID readback, task termination and backwards compatibility. Every fix starts with a failing regression test and lands as a small follow-up commit.

- [ ] **Step 5: Record source candidate state**

```bash
git status --short
git log --oneline 760cd03..HEAD
```

Expected: only planned files are tracked; unrelated main-worktree files are absent from the isolated worktree.

---

### Task 10: Perform One Explicitly Authorized Private Platform Acceptance

**Files:**
- Create after the real run: `docs/verification/youtube-private-publish-2026-08-27.md`
- Modify after the real run: `docs/OVERSEAS_PLATFORM_STATUS.md`
- Modify after the real run: `SOURCE_OF_TRUTH.md`
- Runtime-only, do not commit: `build/youtube-private-acceptance/`

**Interfaces:**
- Consumes: the real OAuth account with scope version 2, one generated harmless video, one generated test cover, and the controlled CLI from Task 7.
- Produces: one private video ID, exact-ID API readback, Studio readback, and a redacted verification record.

- [ ] **Step 1: Prepare deterministic harmless assets without platform access**

```bash
mkdir -p build/youtube-private-acceptance
ffmpeg -y -f lavfi -i color=c=0x111827:s=1280x720:d=3 \
  -c:v libx264 -pix_fmt yuv420p \
  build/youtube-private-acceptance/youtube-private-test.mp4
ffmpeg -y -f lavfi -i color=c=0x111827:s=1200x900 \
  -frames:v 1 build/youtube-private-acceptance/youtube-private-cover.png
shasum -a 256 build/youtube-private-acceptance/youtube-private-test.mp4 \
  build/youtube-private-acceptance/youtube-private-cover.png
```

Create `正文.md` with `一键发 YouTube 官方私密通道验收，不公开。` and a
`oneclick-content/v1` manifest with:

```json
{
  "schemaVersion": "oneclick-content/v1",
  "contentType": "video",
  "title": "一键发 YouTube 私密通道验收 2026-08-27",
  "bodyFile": "正文.md",
  "tags": [],
  "assets": ["youtube-private-test.mp4"],
  "covers": {"4:3": "youtube-private-cover.png"},
  "preferredPlatforms": ["YouTube"],
  "debugDryRun": false,
  "publishAllowed": true
}
```

Run `load_content_bundle` and `validate_youtube_publish_payload` locally before
opening any browser or calling Google.

- [ ] **Step 2: Upgrade the saved account permission and prove identity stability**

From the source-development launcher, choose “升级 YouTube 发布权限” for the
existing account. After the single system-browser OAuth flow, require all of:

- saved `accountReference` is unchanged;
- `oauthScopeVersion == 2`;
- silent account validation returns the same channel ID;
- no token or callback value appears in logs or SQLite.

If the returned channel differs, stop with `youtube_channel_identity_mismatch`
and leave the existing credential untouched.

- [ ] **Step 3: Verify avatar and Studio before upload**

Refresh the OAuth account avatar once and require a newly written local PNG that
the account page can decode. Click “打开后台” once and verify the system browser
opens `https://studio.youtube.com/channel/{saved_channel_id}`. These are read-only
checks and do not create a video.

Quit the source-development client and confirm the installed client is also
closed before starting the controlled CLI. The CLI must be the only process using
the shared production user-data directory during Steps 4-7.

- [ ] **Step 4: Run the real zero-write preflight through the CLI**

Create a controlled request using the actual account ID:

```json
{
  "manifestPath": "/absolute/path/build/youtube-private-acceptance/manifest.json",
  "mode": "preflight",
  "targets": [{
    "platform": "YouTube",
    "accountId": 0,
    "schedule": null,
    "settings": {
      "visibility": "private",
      "madeForKids": false,
      "notifySubscribers": false
    }
  }]
}
```

Replace `accountId: 0` with the ID printed by the local account catalog before
saving the runtime request. Then run:

```bash
YIJIANFA_USER_DATA_DIR="$HOME/Library/Application Support/一键发" \
  .venv/bin/python desktop_native_app.py \
  --controlled-publish-action create \
  --controlled-publish-request build/youtube-private-acceptance/preflight.json
```

Expected: terminal success with `platformMutation=none`; no video ID exists.

- [ ] **Step 5: Stop for fresh explicit authorization**

Show Andy the exact account display name and saved channel ID, title, video and
cover SHA256, `visibility=private`, `madeForKids=false`, and the successful
preflight task ID. State that the next action uploads exactly one private video.
Do not treat design approval, test approval, account login or old authorization
as permission for this platform write.

- [ ] **Step 6: After authorization, create and consume one authorization**

Create the authorization from the successful preflight task:

```bash
YIJIANFA_USER_DATA_DIR="$HOME/Library/Application Support/一键发" \
  .venv/bin/python desktop_native_app.py \
  --controlled-publish-action authorize \
  --controlled-publish-task-id PREFLIGHT_TASK_ID
```

Create `formal.json` from the exact preflight request by changing only:

```json
{
  "mode": "formal",
  "confirmedPreflightTaskId": 0,
  "authorizationId": "ONE_TIME_AUTHORIZATION_ID"
}
```

Replace `0` with the preflight task ID and copy the returned one-time ID. Run
exactly once:

```bash
YIJIANFA_USER_DATA_DIR="$HOME/Library/Application Support/一键发" \
  .venv/bin/python desktop_native_app.py \
  --controlled-publish-action create \
  --controlled-publish-request build/youtube-private-acceptance/formal.json
```

Do not rerun on timeout, interruption or ambiguous output.

- [ ] **Step 7: Require two independent readbacks of the same video**

The task JSON must contain the allowlisted receipt fields and satisfy:

```python
assert result["status"] == "success"
assert receipt["videoId"]
assert receipt["visibility"] == "private"
assert receipt["thumbnailApplied"] is True
assert receipt["platformMutation"] == "private_upload"
```

Then use the “打开后台” system-browser entry and confirm the Studio content list
shows the same title and private status. Do not change visibility, delete the
video or perform another upload.

- [ ] **Step 8: Record only redacted evidence**

Write `docs/verification/youtube-private-publish-2026-08-27.md` with the account
display name, masked channel ID, task ID, masked video ID, private status,
thumbnail result, processing status, timestamps and whether Studio matched.
Do not record URLs containing query strings, credentials or raw Google replies.

Update `docs/OVERSEAS_PLATFORM_STATUS.md` and `SOURCE_OF_TRUTH.md` to say exactly
“one private upload verified” if and only if both readbacks pass. Public,
unlisted and scheduled publishing remain locally tested only.

- [ ] **Step 9: Commit the verification evidence**

```bash
git add docs/verification/youtube-private-publish-2026-08-27.md \
  docs/OVERSEAS_PLATFORM_STATUS.md SOURCE_OF_TRUTH.md
git commit -m "docs: record private YouTube publish verification"
```

---

### Task 11: Bump Version, Build, Install and Deliver Both Platforms

**Files:**
- Create: `test_branding.py`
- Modify: `app_core/branding.py:10`
- Modify: `.github/workflows/build-windows.yml:98-115`
- Modify: `SOURCE_OF_TRUTH.md`
- Create: `docs/superpowers/reports/2026-08-27-oneclick-0.5.23-macos-build-verification.md`
- Create after Windows completes: `docs/superpowers/reports/2026-08-27-oneclick-0.5.23-windows-build-verification.md`

**Interfaces:**
- Consumes: source candidate that passed Tasks 9 and 10.
- Produces: version `0.5.23`, signed Mac candidate and installed app, Windows x64 candidate with SHA256, and evidence that distinguishes build/install from platform publishing.

- [ ] **Step 1: Write the failing release-version test**

```python
import unittest

from app_core.branding import APP_VERSION


class BrandingVersionTests(unittest.TestCase):
    def test_current_integration_release_is_0_5_23(self) -> None:
        self.assertEqual(APP_VERSION, "0.5.23")


if __name__ == "__main__":
    unittest.main()
```

```bash
.venv/bin/python -m unittest -v test_branding
```

Expected: FAIL with current `0.5.22`.

- [ ] **Step 2: Bump the application and correct Windows release copy**

Set:

```python
APP_VERSION = "0.5.23"
```

Change the Windows prerelease note from “海外功能未包含” to a factual candidate
summary that includes YouTube official OAuth publishing and does not claim
public/unlisted/scheduled real verification.

- [ ] **Step 3: Re-run the full release gate**

```bash
.venv/bin/python -m unittest discover -p 'test_*.py'
QT_QPA_PLATFORM=offscreen .venv/bin/python desktop_native_app.py --ui-test
.venv/bin/python -m unittest -v test_branding test_macos_build test_windows_build
git diff --check
```

Expected: all tests pass and UI prints `NATIVE_DESKTOP_UI_OK`.

- [ ] **Step 4: Commit the versioned release source**

```bash
git add app_core/branding.py .github/workflows/build-windows.yml test_branding.py
git commit -m "release: bump oneclick to 0.5.23"
```

- [ ] **Step 5: Build and inspect the Mac candidate**

```bash
.venv/bin/python tools/build_macos.py --date 20260827
codesign --verify --deep --strict release/macos-20260827-v0.5.23/一键发.app
defaults read \
  "$(pwd)/release/macos-20260827-v0.5.23/一键发.app/Contents/Info" \
  CFBundleShortVersionString
shasum -a 256 release/一键发_0.5.23_macOS_arm64_20260827.zip
```

Expected: `MACOS_BUILD_OK`, version `0.5.23`, valid ad-hoc signature and one
recorded ZIP SHA256.

- [ ] **Step 6: Install with a recoverable backup and verify the packaged app**

Stop the source client and installed client. Move the current
`/Users/andy/文件/一键发/一键发.app` to the exact versioned backup name
`一键发_0.5.22_backup.app`, copy in the `0.5.23` app, and launch it once. Verify
the About dialog reports `0.5.23`, the OAuth account still identifies the same
channel, its avatar is visible, and “打开后台” works. Do not upload another video.

If any packaged check fails, quit `0.5.23`, move it aside as
`一键发_0.5.23_failed.app`, restore `一键发_0.5.22_backup.app`, and record the
failure instead of claiming installation success.

- [ ] **Step 7: Record and commit Mac build/install evidence**

Write the report with source commit, ZIP path, SHA256, signature result, bundle
version, packaged UI/browser self-tests, installed path, backup path, OAuth
identity readback and explicit statement that packaging caused no second upload.
Update `SOURCE_OF_TRUTH.md` with the same factual layer.

```bash
git add SOURCE_OF_TRUTH.md \
  docs/superpowers/reports/2026-08-27-oneclick-0.5.23-macos-build-verification.md
git commit -m "docs: record oneclick 0.5.23 Mac build"
```

- [ ] **Step 8: Merge through the integration line and push**

Use `superpowers:finishing-a-development-branch`. Merge
`integration/youtube-official-publish-v3` into current `main` without touching
the unrelated untracked main-worktree files. Re-run the release gate from Step 3
on merged `main`; only then push `main` and record the pushed commit hash.

- [ ] **Step 9: Build Windows from the pushed commit**

Dispatch `.github/workflows/build-windows.yml` for build date `20260827` and
delivery `prerelease`. Wait for the job to finish, download
`YiJianFa_0.5.23_Windows_x64_20260827.zip` and its `.sha256`, and verify:

```bash
shasum -a 256 YiJianFa_0.5.23_Windows_x64_20260827.zip
unzip -l YiJianFa_0.5.23_Windows_x64_20260827.zip \
  | rg -n 'cookiesFile|storage_state|database\.db|oauth_token|refresh_token|seller_tools' \
  && exit 1 || true
```

Expected: the local SHA256 equals the downloaded `.sha256` and the forbidden
marker scan prints nothing.

- [ ] **Step 10: Record Windows evidence and final source truth**

Write the Windows report with workflow run ID, pushed commit, candidate tag,
download path, SHA256, build self-tests and explicit “not installed/tested on a
real Windows desktop” unless that separate runtime test has occurred. Update
`SOURCE_OF_TRUTH.md`, commit and push the evidence.

```bash
git add SOURCE_OF_TRUTH.md \
  docs/superpowers/reports/2026-08-27-oneclick-0.5.23-windows-build-verification.md
git commit -m "docs: record oneclick 0.5.23 Windows build"
git push origin main
```

The delivery is complete only when the source commit, Mac ZIP/install evidence,
Windows ZIP/SHA256 evidence and the single private YouTube video receipt are all
recorded separately. None of these proves public, unlisted or scheduled YouTube
publishing in production.
