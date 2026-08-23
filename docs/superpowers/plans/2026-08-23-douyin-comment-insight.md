# Douyin Comment Insight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在一键发“数据监测”页为本人抖音作品增加手动、只读的评论同步与证据化选题洞察；不采集作者身份，不产生任何平台写操作。

**Architecture:** 数据监测分支先实现严格模型、独立评论存储、真实合同探测、短生命周期浏览器采集、两阶段事务、系统凭据库和 UI。`app_core/database.py` 只由集成线增加一处模式初始化调用。常规账号同步与评论正文同步完全分开，AI 失败不回滚评论。

**Tech Stack:** Python 3.11、PyQt6、SQLite、Playwright async API、`requests`、macOS Security.framework、Windows Credential Manager、`unittest`。

**Spec:** `docs/superpowers/specs/2026-08-23-douyin-comment-insight-design.md`

## Global Constraints

- 工作分支固定为 `feature/data-monitoring-v3`，工作目录固定为 `.worktrees/data-monitoring-v3`。
- 数据分支只修改 `QUALITY_GATES.md` 允许的数据专属路径。新增模块统一使用 `app_core/platform_data_*` 或 `*_data_collector.py` 命名。
- `app_core/database.py` 是共享核心；本计划中的接线步骤只在 `integration/oneclick-0.5.0` 执行并单独提交。
- `SOURCE_OF_TRUTH.md` 和 `QUALITY_GATES.md` 晚于设计文档，故覆盖设计第 13 节：功能分支保持 `0.4.2`，不改版本、不打包；最终集成发布时统一决定 `0.5.0`。
- 不猜抖音接口、路径、字段或分页方式。生产合同只能由同一已登录账号的官方页面 HTTPS JSON 响应生成；无合同时固定返回 `comment_content_unavailable`。
- 只允许 `creator.douyin.com`，禁止跨域响应、请求重放、Cookie 导出、DOM 正文采集以及回复、删除、点赞、置顶、私信、发布。
- 评论解析入口立即丢弃作者对象。平台评论 ID 只在内存中生成 SHA-256 匿名键，绝不落库、写日志或送给 AI。
- 外部 AI 只收到作品标题、本次局部序号 `C001...` 和评论正文；不发送账号、作品、评论键、计数、时间、路径、机器或登录信息。
- API Key 只进入 macOS Keychain 或 Windows Credential Manager。系统凭据库不可用时拒绝保存，评论同步仍可使用。
- 测试采用阶梯：先单项 RED/GREEN，再受影响模块，功能线收尾跑数据监测最低测试；共享接线后才跑全量和离屏 UI。
- 真实账号验收只读，不提交、不发布、不修改硅基探索生产目录。没有至少 5 条评论的可验证作品时，真实验收合法停在“合同未完成”。

## File Map

### Data branch — create

- `app_core/platform_data_comment_models.py`: 固定错误、匿名评论、分页批次、洞察结果模型。
- `app_core/platform_data_comment_store.py`: 三张表的模式函数、事务保存和只读查询。
- `app_core/platform_data_comment_contract.py`: 合同结构、脱敏观察、生产合同加载与校验。
- `app_core/platform_data_douyin_comment_contract.json`: 真实探测成功后生成的无值合同；探测未成功时不创建。
- `app_core/douyin_comment_data_collector.py`: 抖音评论短会话、分页、匿名化和资源清理。
- `app_core/platform_data_comment_service.py`: 作品归属校验、同步编排、两阶段提交和查询投影。
- `app_core/platform_data_comment_secret_store.py`: 两个平台的系统凭据库适配器。
- `app_core/platform_data_comment_ai.py`: OpenAI 兼容请求、结构和证据校验。
- `app_core/platform_data_comment_settings.py`: 非秘密 AI 地址和模型的 QSettings 读写。
- `test_platform_data_comment_models.py`
- `test_platform_data_comment_store.py`
- `test_platform_data_comment_contract.py`
- `test_douyin_comment_data_collector.py`
- `test_platform_data_comment_service.py`
- `test_platform_data_comment_ai.py`

### Data branch — modify

- `app_core/douyin_data_collector.py`: 只在生产合同存在时补齐本人作品列表和作品累计指标。
- `app_core/platform_data_sync.py`: 允许采集器把“账号总览已得、作品未得”的批次用独立浏览器会话补全；不触发评论读取。
- `ui/data_monitor_page.py`: 作品单选、评论洞察面板、AI 设置和后台任务生命周期。
- `test_douyin_data_collector.py`
- `test_platform_data_sync.py`
- `test_data_monitor_page.py`

### Integration branch — modify once

- `app_core/database.py`: 在现有 `platform_contents` 建表后调用 `create_comment_schema(conn)`。
- `test_platform_data_comment_database_integration.py`: 真实 `ensure_schema()` 接线回归。

---

### Task 1: Strict anonymous comment and insight models

**Files:**
- Create: `app_core/platform_data_comment_models.py`
- Create: `test_platform_data_comment_models.py`

**Interfaces:**
- Consumes: built-in Python scalars from a validated platform response.
- Produces: `CommentRecord`, `CommentPage`, `CommentCollectionBatch`, `CommentClassification`, `TopicCandidate`, `InsightResult`, `CommentInsightFailure`.
- Public helper: `derive_comment_key(account_id: int, content_id: str, platform_comment_id: str) -> str`.

- [ ] **Step 1: Write failing model/privacy tests**

Add tests for exact built-in types, non-empty untrimmed body preservation, non-negative counts, timezone-aware ISO timestamps, six allowed labels, at most five candidates, evidence membership, duplicate keys, and deterministic anonymization:

```python
def test_comment_id_is_hashed_and_raw_id_never_enters_record(self):
    key = derive_comment_key(12, "work-7", "platform-comment-99")
    self.assertRegex(key, r"^[0-9a-f]{64}$")
    self.assertNotIn("platform-comment-99", key)
    record = CommentRecord(
        content_id="work-7",
        comment_key=key,
        body="  这条内容保留原始空格  ",
        like_count=3,
        reply_count=1,
        commented_at="2026-08-23T10:20:30+08:00",
        observed_at="2026-08-23T10:21:00+08:00",
    )
    self.assertEqual(record.body, "  这条内容保留原始空格  ")
    self.assertFalse(hasattr(record, "author_id"))
    self.assertFalse(hasattr(record, "platform_comment_id"))

def test_insight_rejects_unknown_or_empty_evidence(self):
    known = frozenset({"a" * 64})
    with self.assertRaises(CommentInsightFailure) as raised:
        InsightResult(
            classifications=(
                CommentClassification("a" * 64, ("追问",)),
            ),
            candidates=(
                TopicCandidate("下一期", "回答读者问题", ("b" * 64,)),
            ),
            known_comment_keys=known,
        )
    self.assertEqual(raised.exception.error_code, "comment_ai_evidence_invalid")
```

- [ ] **Step 2: Run the named tests and verify RED**

```bash
/Users/andy/Documents/Codex/2026-07-28/new-chat/outputs/一键发桌面UI基座/.venv/bin/python \
  -m unittest -v test_platform_data_comment_models
```

Expected: import fails because the model module does not exist.

- [ ] **Step 3: Implement the minimal strict model layer**

Use fixed public error codes and no arbitrary exception text:

```python
ALLOWED_COMMENT_LABELS = frozenset(
    {"质疑", "认同", "真实经历", "追问", "选题建议", "其他"}
)
ALLOWED_COMMENT_WARNING_CODES = frozenset({"", "comment_limit_reached"})
ALLOWED_COMMENT_ERROR_CODES = frozenset({
    "comment_content_unavailable", "comment_login_required",
    "comment_verification_required", "comment_access_denied",
    "comment_payload_invalid", "comment_sync_timeout",
    "comment_sync_cancelled", "comment_ai_not_configured",
    "comment_ai_timeout", "comment_ai_service_unavailable",
    "comment_ai_response_invalid", "comment_ai_evidence_invalid",
})

class CommentInsightFailure(RuntimeError):
    def __init__(self, error_code: str, *, retryable: bool = False) -> None:
        code = error_code if error_code in ALLOWED_COMMENT_ERROR_CODES else "comment_payload_invalid"
        self.error_code = code
        self.retryable = bool(retryable)
        super().__init__(code)

def derive_comment_key(account_id: int, content_id: str, platform_comment_id: str) -> str:
    if type(account_id) is not int or account_id <= 0:
        raise CommentInsightFailure("comment_payload_invalid")
    if type(content_id) is not str or not content_id.strip():
        raise CommentInsightFailure("comment_payload_invalid")
    if type(platform_comment_id) is not str or not platform_comment_id.strip():
        raise CommentInsightFailure("comment_payload_invalid")
    source = f"{account_id}\x1f{content_id}\x1f{platform_comment_id}".encode("utf-8")
    return hashlib.sha256(source).hexdigest()
```

Dataclass validation must use exact built-in types (`type(value) is ...`), reject booleans as integers, require UTC offset on timestamps, preserve valid body text byte-for-byte, and reject duplicate comment/evidence keys.

`CommentCollectionBatch` must expose `platform_type`, `source_mode`, `content_id`, `comments`, `accepted_count`, `rejected_count`, `page_count`, `stop_reason`, `warning_code`, `platform_observed_at` and `cleanup_receipt`. `accepted_count` equals the deduplicated comments tuple length. `warning_code` is `comment_limit_reached` only when the accepted count reaches 100 before a verified platform end.

- [ ] **Step 4: Run the module and verify GREEN**

Run the Step 2 command. Expected: terminal status `OK`.

- [ ] **Step 5: Commit the model task**

```bash
git add app_core/platform_data_comment_models.py test_platform_data_comment_models.py
git commit -m "增加匿名评论洞察模型"
```

---

### Task 2: Comment schema helper and atomic repository

**Files:**
- Create: `app_core/platform_data_comment_store.py`
- Create: `test_platform_data_comment_store.py`

**Interfaces:**
- `create_comment_schema(conn: sqlite3.Connection) -> None`
- `content_for_comment_sync(conn, account_id: int, content_id: str) -> dict | None`
- `known_comment_keys(conn, account_id: int, content_id: str) -> frozenset[str]`
- `persist_comment_batch(conn, account_id: int, batch: CommentCollectionBatch) -> dict`
- `record_failed_comment_sync(...) -> dict`
- `list_comment_rows(...) -> list[dict]`
- `persist_insight_result(...) -> dict`
- `latest_insight(...) -> dict | None`

- [ ] **Step 1: Write failing migration and transaction tests**

Use a temporary SQLite database with minimal `user_info` and `platform_contents` tables. Prove:

1. all three tables and query indexes exist;
2. account/content ownership is enforced before a run starts;
3. sync-run finalization and comment upserts commit together;
4. an injected failure rolls back the run and every comment;
5. known comments update counts and `lastSeenAt` without duplicating;
6. absent historical comments remain;
7. raw platform IDs and author columns do not exist;
8. AI failure rows do not modify saved comments.

```python
def test_batch_failure_rolls_back_run_and_comments(self):
    conn = prepared_connection()
    batch = valid_batch(two_comments=True)
    with patch.object(store, "_finalize_run", side_effect=sqlite3.OperationalError("boom")):
        with self.assertRaises(sqlite3.OperationalError):
            with conn:
                store.persist_comment_batch(conn, 12, batch)
    self.assertEqual(conn.execute(
        "SELECT COUNT(*) FROM platform_comment_sync_runs"
    ).fetchone()[0], 0)
    self.assertEqual(conn.execute(
        "SELECT COUNT(*) FROM platform_comments"
    ).fetchone()[0], 0)
```

- [ ] **Step 2: Verify RED**

```bash
/Users/andy/Documents/Codex/2026-07-28/new-chat/outputs/一键发桌面UI基座/.venv/bin/python \
  -m unittest -v test_platform_data_comment_store
```

Expected: import fails because the repository does not exist.

- [ ] **Step 3: Implement exact schema and repository**

`create_comment_schema()` must create these identities:

```sql
CREATE TABLE IF NOT EXISTS platform_comment_sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    accountId INTEGER NOT NULL,
    platformType INTEGER NOT NULL CHECK(platformType = 3),
    contentId TEXT NOT NULL,
    sourceMode TEXT NOT NULL CHECK(sourceMode IN ('direct_session', 'browser_signed')),
    status TEXT NOT NULL CHECK(status IN ('running', 'success', 'failed', 'cancelled')),
    errorCode TEXT NOT NULL DEFAULT '',
    acceptedCount INTEGER NOT NULL DEFAULT 0 CHECK(acceptedCount >= 0),
    insertedCount INTEGER NOT NULL DEFAULT 0 CHECK(insertedCount >= 0),
    updatedCount INTEGER NOT NULL DEFAULT 0 CHECK(updatedCount >= 0),
    rejectedCount INTEGER NOT NULL DEFAULT 0 CHECK(rejectedCount >= 0),
    pageCount INTEGER NOT NULL DEFAULT 0 CHECK(pageCount >= 0),
    stopReason TEXT NOT NULL DEFAULT '' CHECK(stopReason IN ('', 'known_comment', 'limit_reached', 'platform_end')),
    startedAt TEXT NOT NULL,
    finishedAt TEXT,
    FOREIGN KEY(accountId) REFERENCES user_info(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS platform_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    accountId INTEGER NOT NULL,
    platformType INTEGER NOT NULL CHECK(platformType = 3),
    contentId TEXT NOT NULL,
    commentKey TEXT NOT NULL,
    body TEXT NOT NULL,
    likeCount INTEGER NOT NULL CHECK(likeCount >= 0),
    replyCount INTEGER NOT NULL CHECK(replyCount >= 0),
    commentedAt TEXT NOT NULL,
    firstSeenAt TEXT NOT NULL,
    lastSeenAt TEXT NOT NULL,
    lastSyncRunId INTEGER,
    UNIQUE(accountId, platformType, contentId, commentKey),
    FOREIGN KEY(accountId) REFERENCES user_info(id) ON DELETE CASCADE,
    FOREIGN KEY(lastSyncRunId) REFERENCES platform_comment_sync_runs(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS comment_insight_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    accountId INTEGER NOT NULL,
    platformType INTEGER NOT NULL CHECK(platformType = 3),
    contentId TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running', 'success', 'failed', 'skipped')),
    errorCode TEXT NOT NULL DEFAULT '',
    providerType TEXT NOT NULL CHECK(providerType = 'openai_compatible'),
    modelName TEXT NOT NULL,
    promptVersion TEXT NOT NULL,
    schemaVersion INTEGER NOT NULL,
    inputFingerprint TEXT NOT NULL,
    commentCount INTEGER NOT NULL CHECK(commentCount >= 0),
    classificationsJson TEXT NOT NULL,
    candidatesJson TEXT NOT NULL,
    startedAt TEXT NOT NULL,
    finishedAt TEXT,
    FOREIGN KEY(accountId) REFERENCES user_info(id) ON DELETE CASCADE
);
```

Add indexes for `(accountId, contentId, id DESC)`, `(accountId, contentId, commentedAt DESC, id DESC)`, and latest insight lookup. `persist_comment_batch()` must be called inside a connection transaction and must not commit internally.

- [ ] **Step 4: Verify GREEN and query plans**

Run Step 2, then assert `EXPLAIN QUERY PLAN` uses the comment and insight indexes for latest-page reads.

- [ ] **Step 5: Commit the store task**

```bash
git add app_core/platform_data_comment_store.py test_platform_data_comment_store.py
git commit -m "增加评论同步事务存储"
```

---

### Task 3: Passive real-contract probe and fail-closed manifest

**Files:**
- Create: `app_core/platform_data_comment_contract.py`
- Create: `test_platform_data_comment_contract.py`
- Create only after verified real evidence: `app_core/platform_data_douyin_comment_contract.json`
- Update: `docs/superpowers/reports/2026-08-23-douyin-comment-insight-contract-report.md`

**Interfaces:**
- `observe_contract_responses(account: dict, report: Callable[[dict], None] | None) -> ContractObservation`
- `sanitize_contract_observation(value: object) -> dict`
- `freeze_verified_contract(observation: ContractObservation, selection: ContractSelection, destination: Path) -> DouyinCommentContract`
- `load_verified_contract(path: Path = DEFAULT_CONTRACT_PATH) -> DouyinCommentContract`

- [ ] **Step 1: Write failing security and manifest tests**

Fake official responses must prove that only scheme, host, path, HTTP method, structural key paths, built-in field types and pagination field names survive. Query strings, bodies, values, IDs, headers, cookies, titles, comments and custom container subclasses must disappear.

```python
def test_sanitized_observation_has_structure_but_no_values(self):
    source = response_shape(
        url="https://creator.douyin.com/verified/comment/list?cursor=secret",
        keys=("data.comments[].comment_id", "data.comments[].text", "data.cursor"),
        sample={"text": "private comment", "author": {"uid": "private"}},
    )
    safe = sanitize_contract_observation({"responses": [source]})
    encoded = json.dumps(safe, ensure_ascii=False)
    self.assertIn("/verified/comment/list", encoded)
    self.assertIn("data.comments[].text", encoded)
    for forbidden in ("secret", "private comment", "private", "uid"):
        self.assertNotIn(forbidden, encoded)

def test_missing_or_unverified_manifest_fails_closed(self):
    with self.assertRaises(CommentInsightFailure) as raised:
        load_verified_contract(Path(self.tempdir.name) / "missing.json")
    self.assertEqual(raised.exception.error_code, "comment_content_unavailable")
```

- [ ] **Step 2: Verify RED**

```bash
/Users/andy/Documents/Codex/2026-07-28/new-chat/outputs/一键发桌面UI基座/.venv/bin/python \
  -m unittest -v test_platform_data_comment_contract
```

- [ ] **Step 3: Implement the passive observer**

The observer must:

- open only `https://creator.douyin.com` with the saved Playwright storage state;
- install the response listener before navigation;
- accept only HTTPS JSON from the exact creator host;
- cap response count, response bytes, key-path count and total wall time;
- allow official UI navigation to works and one work's comment page but never read DOM text as data;
- store only sanitized structure in the report;
- close page, context, browser and Playwright on success, failure, timeout, cancellation, `KeyboardInterrupt` and `SystemExit`.

The frozen manifest is accepted only when `verified` is the built-in boolean `true`. Keep the production shape explicit without embedding guessed path or field values in source:

```python
@dataclass(frozen=True, slots=True)
class ContractSelection:
    content_response_path: str
    content_navigation_template: str
    content_list_field: str
    content_id_field: str
    content_title_field: str
    content_cover_field: str
    content_published_at_field: str
    content_status_field: str
    content_type_field: str
    content_metric_fields: tuple[tuple[str, str], ...]
    content_cursor_field: str
    content_has_more_field: str
    comment_response_path: str
    comment_navigation_template: str
    comment_list_field: str
    comment_id_field: str
    comment_content_id_field: str
    comment_parent_id_field: str
    comment_body_field: str
    comment_like_count_field: str
    comment_reply_count_field: str
    comment_commented_at_field: str
    comment_cursor_field: str
    comment_has_more_field: str
    comment_pagination_trigger: str
```

`freeze_verified_contract()` writes `schemaVersion`, `verified`, `creatorHost`, `contentList` and `commentList` from this exact selection. Every selected path, field and navigation template must be present in the same real sanitized observation; otherwise it raises `comment_content_unavailable` and writes no production manifest.

- [ ] **Step 4: Verify GREEN with fake Playwright cleanup tests**

Run Step 2. Add timeout, cross-host, non-JSON, oversized-body, missing-pagination, and cleanup-failure cases; all must return fixed errors without leaking exception text.

- [ ] **Step 5: Run the authorized real read-only observation**

Use the already authorized logged-in Douyin account. The result must be one of:

- verified work-list and comment-list contracts with `cleanup.closed=true` and `aliveResourceCount=0`, followed by generated production manifest; or
- `comment_content_unavailable`, `comment_login_required`, `comment_verification_required` or `comment_access_denied`, with no production manifest created.

Write only the sanitized status, field names, counts, fixed error and cleanup receipt to `docs/superpowers/reports/2026-08-23-douyin-comment-insight-contract-report.md`.

- [ ] **Step 6: Commit probe code and truthful evidence**

```bash
git add app_core/platform_data_comment_contract.py \
  test_platform_data_comment_contract.py \
  docs/superpowers/reports/2026-08-23-douyin-comment-insight-contract-report.md
if test -f app_core/platform_data_douyin_comment_contract.json; then
  git add app_core/platform_data_douyin_comment_contract.json
fi
git commit -m "增加抖音评论只读合同探测"
```

---

### Task 4: Verified Douyin work list in normal data sync

**Files:**
- Modify: `app_core/douyin_data_collector.py`
- Modify: `app_core/platform_data_sync.py`
- Modify: `test_douyin_data_collector.py`
- Modify: `test_platform_data_sync.py`

**Interfaces:**
- `DouyinDataCollector.complete_content_data(account: dict, account_batch: CollectionBatch, report=None) -> CollectionBatch`
- `parse_verified_content_payload(contract: DouyinCommentContract, payload: object, account_id: int, observed_at: str) -> tuple[tuple[ContentRecord, ...], tuple[MetricPoint, ...], str]`

- [ ] **Step 1: Write failing content completion tests**

Cover strict host/path match, stable content ID, all accepted content fields, cumulative content metrics, pagination, duplicate IDs, missing/unknown values, cross-account late results and no contract.

```python
def test_normal_sync_completes_work_list_without_reading_comments(self):
    collector = verified_collector(
        overview=valid_overview(),
        content_pages=(valid_content_page(),),
    )
    batch = collector.complete_content_data(
        self.account,
        collector.collect_direct(self.account),
    )
    self.assertTrue(batch.content_data_available)
    self.assertEqual([item.content_id for item in batch.contents], ["work-7"])
    self.assertEqual(
        {point.metric_key for point in batch.metrics if point.entity_type == "content"},
        {"views", "likes", "comments", "shares"},
    )
    self.assertEqual(collector.comment_response_count, 0)
```

- [ ] **Step 2: Verify RED**

```bash
/Users/andy/Documents/Codex/2026-07-28/new-chat/outputs/一键发桌面UI基座/.venv/bin/python \
  -m unittest -v test_douyin_data_collector test_platform_data_sync
```

- [ ] **Step 3: Add the opt-in completion hook**

After an ordinary collector returns a valid account batch, `platform_data_sync.sync_account_data()` may call only an explicit collector hook:

```python
complete_content = getattr(collector, "complete_content_data", None)
if (
    batch.platform_type == 3
    and not batch.content_data_available
    and callable(complete_content)
):
    batch = complete_content(account, batch, report=report)
```

`DouyinDataCollector.complete_content_data()` must leave current behavior unchanged when no verified manifest exists: return the original account batch with `content_list_unavailable`. When a verified manifest exists, it opens an independent short browser session, passively captures the official work list, merges account and content metrics, and closes all resources.

Do not import or call the comment collector from normal sync.

- [ ] **Step 4: Verify GREEN and existing Douyin behavior**

Run Step 2. Confirm the old no-manifest tests still return `content_list_unavailable` and no extra request.

- [ ] **Step 5: Commit work-list support**

```bash
git add app_core/douyin_data_collector.py app_core/platform_data_sync.py \
  test_douyin_data_collector.py test_platform_data_sync.py
git commit -m "接入已验证抖音作品列表"
```

---

### Task 5: Short-lived comment collector, pagination and privacy

**Files:**
- Create: `app_core/douyin_comment_data_collector.py`
- Create: `test_douyin_comment_data_collector.py`

**Interfaces:**
- `DouyinCommentDataCollector.collect(account: dict, content_id: str, known_keys: frozenset[str], limit: int = 100, report=None) -> CommentCollectionBatch`
- `parse_comment_page(contract, payload, account_id: int, content_id: str, observed_at: str) -> CommentPage`

- [ ] **Step 1: Write failing parser and lifecycle tests**

Test first sync, repeated page items, author-field discard, top-level only, non-negative counts, ID hashing, content mismatch, known-key stop, platform end, exact 100 limit, missing pagination, timeout, login/verification/access-denied mappings, cross-host responses, and cleanup on every exit.

```python
def test_author_fields_are_discarded_before_batch_leaves_parser(self):
    page = parse_comment_page(
        verified_contract(),
        valid_comment_payload(author={"uid": "private", "nickname": "private"}),
        account_id=12,
        content_id="work-7",
        observed_at="2026-08-23T10:21:00+08:00",
    )
    encoded = repr(page)
    self.assertNotIn("private", encoded)
    self.assertEqual(len(page.comments), 1)
    self.assertEqual(page.comments[0].body, "为什么会这样？")

def test_second_sync_stops_after_first_known_comment(self):
    known = derive_comment_key(12, "work-7", "old-2")
    batch = collector_with_pages(
        first_page(comment_ids=("new-1", "old-2"), has_more=True),
        second_page(comment_ids=("must-not-read",), has_more=False),
    ).collect(self.account, "work-7", frozenset({known}))
    self.assertEqual(batch.stop_reason, "known_comment")
    self.assertEqual(batch.page_count, 1)
```

- [ ] **Step 2: Verify RED**

```bash
/Users/andy/Documents/Codex/2026-07-28/new-chat/outputs/一键发桌面UI基座/.venv/bin/python \
  -m unittest -v test_douyin_comment_data_collector
```

- [ ] **Step 3: Implement passive pagination and resource barrier**

The collector must install the response listener before navigating to the verified `navigationTemplate`. It may trigger only the verified official UI pagination control recorded in the manifest. Each response must match exact host/path/method and content ID. It must never send a synthetic comments request.

When a row's anonymous key is already known, include that row once in the returned batch so its likes, replies and `lastSeenAt` can update, set `stop_reason="known_comment"`, and do not trigger the next page. A malformed individual row increments `rejected_count` and is omitted; a response with no valid rows and no verified platform-end marker fails with `comment_payload_invalid`.

For each accepted row:

```python
platform_comment_id = required_platform_id(raw_row, contract.comment_id_field)
comment_key = derive_comment_key(account_id, content_id, platform_comment_id)
record = CommentRecord(
    content_id=content_id,
    comment_key=comment_key,
    body=required_body(raw_row, contract.comment_body_field),
    like_count=required_non_negative_int(raw_row, contract.comment_like_count_field),
    reply_count=required_non_negative_int(raw_row, contract.comment_reply_count_field),
    commented_at=required_platform_time(raw_row, contract.comment_time_field),
    observed_at=observed_at,
)
del platform_comment_id
```

Do not copy the raw row, author object or response after parsing. `KeyboardInterrupt` and `SystemExit` propagate only after page/context/browser/Playwright cleanup attempts finish.

- [ ] **Step 4: Verify GREEN and cleanup evidence**

Run Step 2. Every success and failure test must assert `page.closed == context.closed == browser.closed == playwright.stopped == 1` or the equivalent fixed cleanup failure receipt.

- [ ] **Step 5: Commit the collector**

```bash
git add app_core/douyin_comment_data_collector.py test_douyin_comment_data_collector.py
git commit -m "增加抖音评论只读采集器"
```

---

### Task 6: Manual sync service and independent comment transaction

**Files:**
- Create: `app_core/platform_data_comment_service.py`
- Create: `test_platform_data_comment_service.py`

**Interfaces:**
- `sync_comments(account_id: int, content_id: str, report=None, collector_factory=...) -> dict`
- `comment_panel_payload(account_id: int, content_id: str, label: str = "全部", limit: int = 100) -> dict`
- `record_comment_ai_outcome(...) -> dict`

- [ ] **Step 1: Write failing orchestration tests**

Prove:

- selected content must belong to account `platformType=3`;
- known keys are loaded before collection;
- the comment batch commits before AI is called;
- collector failure preserves previous comments and records one failed run;
- repeated click is deduped by UI key, not silently retried in service;
- AI unconfigured/timeout/failure does not hide or roll back saved comments;
- only fixed progress stages escape the service.

```python
def test_comments_commit_before_ai_failure(self):
    ai = Mock()
    ai.analyze.side_effect = CommentInsightFailure("comment_ai_timeout", retryable=True)
    result = service.sync_comments(
        12, "work-7",
        collector_factory=lambda: fixed_collector(valid_batch()),
        ai_provider_factory=lambda: ai,
    )
    self.assertEqual(result["status"], "success")
    self.assertEqual(result["aiStatus"], "failed")
    self.assertEqual(result["aiErrorCode"], "comment_ai_timeout")
    self.assertEqual(len(service.comment_panel_payload(12, "work-7")["comments"]), 2)
```

- [ ] **Step 2: Verify RED**

```bash
/Users/andy/Documents/Codex/2026-07-28/new-chat/outputs/一键发桌面UI基座/.venv/bin/python \
  -m unittest -v test_platform_data_comment_service
```

- [ ] **Step 3: Implement two transactions and controlled results**

Use fixed progress stages only:

```python
COMMENT_PROGRESS = {
    "preparing": "正在准备只读会话",
    "collecting": "正在读取评论",
    "validating": "正在校验评论",
    "persisting": "正在保存评论",
    "insight": "正在生成洞察",
    "completed": "评论同步完成",
    "failed": "评论同步未完成",
}
```

Transaction order:

```python
with database.connect() as conn:
    content = store.content_for_comment_sync(conn, account_id, content_id)
    known = store.known_comment_keys(conn, account_id, content_id)
batch = collector.collect(account, content_id, known, report=report)
with database.connect() as conn:
    receipt = store.persist_comment_batch(conn, account_id, batch)
ai_receipt = _run_optional_ai_after_commit(account_id, content, report)
return public_sync_result(receipt, ai_receipt)
```

The service must never accept a title as a content locator. Error results include only fixed code, counts and cleanup receipt.

- [ ] **Step 4: Verify GREEN and existing service regressions**

```bash
/Users/andy/Documents/Codex/2026-07-28/new-chat/outputs/一键发桌面UI基座/.venv/bin/python \
  -m unittest -v test_platform_data_comment_service test_platform_data_service test_platform_data_sync
```

- [ ] **Step 5: Commit the service**

```bash
git add app_core/platform_data_comment_service.py test_platform_data_comment_service.py
git commit -m "编排手动评论同步事务"
```

---

### Task 7: Native secret vault and evidence-validating AI adapter

**Files:**
- Create: `app_core/platform_data_comment_secret_store.py`
- Create: `app_core/platform_data_comment_settings.py`
- Create: `app_core/platform_data_comment_ai.py`
- Create: `test_platform_data_comment_ai.py`
- Modify: `test_platform_data_comment_service.py`

**Interfaces:**
- `CommentSecretStore.read() -> str | None`
- `CommentSecretStore.write(secret: str) -> None`
- `CommentSecretStore.delete() -> None`
- `load_ai_settings(settings: QSettings) -> CommentAiSettings | None`
- `save_ai_settings(settings: QSettings, value: CommentAiSettings) -> None`
- `OpenAiCompatibleCommentProvider.analyze(title: str, comments: tuple[CommentRecord, ...]) -> InsightResult`

- [ ] **Step 1: Write failing secret and outbound-boundary tests**

Use injected fake native backends. Prove:

- macOS calls Security.framework through `ctypes`, not a command containing the secret;
- Windows calls Credential Manager through `ctypes`;
- unsupported platform and native failure refuse save;
- QSettings contains base URL/model only;
- HTTPS, no user-info/query/fragment, and a non-empty model are required;
- request contains title, `C001...`, bodies and schema only;
- request excludes account/content/comment keys, counts, timestamps and identity;
- raw request/response and API key never reach SQLite or logs;
- classification refs exactly cover the submitted refs;
- max five candidates and every candidate has valid non-duplicate evidence.

```python
def test_ai_request_contains_only_allowed_fields(self):
    session = RecordingSession(valid_ai_response())
    provider = OpenAiCompatibleCommentProvider(
        settings=CommentAiSettings("https://ai.example.com/v1", "model-x"),
        secret="sk-private",
        session_factory=lambda: session,
    )
    result = provider.analyze("作品标题", (comment("a" * 64, "为什么？"),))
    encoded = json.dumps(session.json_body, ensure_ascii=False)
    self.assertIn("作品标题", encoded)
    self.assertIn("C001", encoded)
    self.assertIn("为什么？", encoded)
    for forbidden in ("a" * 64, "accountId", "contentId", "likeCount", "sk-private"):
        self.assertNotIn(forbidden, encoded)
    self.assertEqual(result.candidates[0].evidence_keys, ("a" * 64,))
```

- [ ] **Step 2: Verify RED**

```bash
/Users/andy/Documents/Codex/2026-07-28/new-chat/outputs/一键发桌面UI基座/.venv/bin/python \
  -m unittest -v test_platform_data_comment_ai
```

- [ ] **Step 3: Implement native backends and settings**

Use service name `com.oneclickpublisher.comment-insight` and account name `default`. The native wrapper must pass secret bytes through memory pointers, zero mutable buffers after calls, free native returned buffers, and raise only `comment_ai_not_configured` on public boundaries.

Store nonsecret keys under:

```python
SETTINGS_GROUP = "commentInsightAi"
BASE_URL_KEY = f"{SETTINGS_GROUP}/baseUrl"
MODEL_KEY = f"{SETTINGS_GROUP}/model"
```

- [ ] **Step 4: Implement exact AI response contract**

Send one JSON chat request to `{normalized_base_url}/chat/completions` with timeout `(10, 45)`. Parse only:

```json
{
  "classifications": [
    {"ref": "C001", "labels": ["追问"]}
  ],
  "candidates": [
    {"title": "下一期题目", "reason": "回答高频追问", "evidenceRefs": ["C001"]}
  ]
}
```

Map local refs back to `commentKey` only after full response validation. Drop the raw response object before returning. Any unknown field, label, ref, duplicate ref, empty evidence, sixth candidate or malformed scalar fails the entire AI run.

- [ ] **Step 5: Verify GREEN and service separation**

```bash
/Users/andy/Documents/Codex/2026-07-28/new-chat/outputs/一键发桌面UI基座/.venv/bin/python \
  -m unittest -v test_platform_data_comment_ai test_platform_data_comment_service
```

- [ ] **Step 6: Commit native secret and AI support**

```bash
git add app_core/platform_data_comment_secret_store.py \
  app_core/platform_data_comment_settings.py \
  app_core/platform_data_comment_ai.py \
  test_platform_data_comment_ai.py test_platform_data_comment_service.py
git commit -m "增加安全评论洞察适配器"
```

---

### Task 8: Data Monitor comment insight panel and lifecycle

**Files:**
- Modify: `ui/data_monitor_page.py`
- Modify: `test_data_monitor_page.py`

**Interfaces:**
- `_ContentTable.selected_content() -> dict | None`
- `DataMonitorPage._start_comment_sync() -> None`
- `DataMonitorPage._render_comment_panel() -> None`
- `_CommentAiSettingsDialog`: nonsecret fields plus masked secret entry; save uses native vault.

- [ ] **Step 1: Write failing UI tests**

Cover:

1. works table uses single-row selection and stores stable `contentId` in `Qt.ItemDataRole.UserRole`;
2. no selection disables “同步最新评论”;
3. non-Douyin platform hides/disables the panel;
4. Douyin content unavailable shows the exact unavailable message;
5. selecting a work loads only its comments and latest insight;
6. fixed progress stages update the UI; untrusted worker messages do not;
7. late completion from a previous work/account never overwrites current selection;
8. duplicate clicks create one background task;
9. category filtering and evidence expansion use local comment rows;
10. labels, buttons and menus contain no reply/delete/like/publish action;
11. shutdown includes both `platform-data-sync` and `platform-comment-sync` task prefixes.

```python
def test_comment_sync_is_keyed_by_platform_account_and_content(self):
    pool = QueuedPool()
    runner = BackgroundTaskRunner()
    runner.pool = pool
    page = self._page(contents=available_douyin_contents(), runner=runner)
    page.content_table.selectRow(0)
    page.comment_sync_button.click()
    page.comment_sync_button.click()
    self.assertEqual(len(pool.tasks), 1)
    self.assertEqual(
        list(runner.active),
        ["platform-comment-sync:3:12:work-7"],
    )
```

- [ ] **Step 2: Verify RED**

```bash
QT_QPA_PLATFORM=offscreen \
/Users/andy/Documents/Codex/2026-07-28/new-chat/outputs/一键发桌面UI基座/.venv/bin/python \
  -m unittest -v test_data_monitor_page
```

- [ ] **Step 3: Implement selection and panel**

In `_ContentTable.set_payload()` store controlled data only:

```python
title_item = self._text_cell(item.get("title"))
title_item.setData(Qt.ItemDataRole.UserRole, item.get("contentId"))
self.setItem(row_index, 0, title_item)
```

Set `SingleSelection`. Add a panel below the works table with selected title, last sync time, “同步最新评论”, “AI 设置”, fixed status label, six-label filter, comment table, and expandable topic candidates. Comment rows show only body, likes, replies, time and classification.

Run comment tasks with:

```python
key = f"platform-comment-sync:{platform_type}:{account_id}:{content_id}"
self.runner.run(
    key,
    with_progress=lambda report: platform_data_comment_service.sync_comments(
        account_id, content_id, report=report
    ),
    on_started=started,
    on_progress=progressed,
    on_success=completed,
    on_error=failed,
    on_finished=finished,
)
```

Only refresh the panel when `(platform_type, account_id, content_id)` still equals the current selection.

- [ ] **Step 4: Implement settings dialog without exposing existing key**

The dialog may show only “已配置/未配置”; it must never read the existing secret into the line edit. A non-empty new key replaces the vault value. An explicit “清除密钥” action deletes it. Closing the dialog does nothing.

- [ ] **Step 5: Verify GREEN and existing UI behavior**

Run Step 2, then:

```bash
QT_QPA_PLATFORM=offscreen \
/Users/andy/Documents/Codex/2026-07-28/new-chat/outputs/一键发桌面UI基座/.venv/bin/python \
  -m unittest -v test_data_monitor_page test_platform_data_sync
```

- [ ] **Step 6: Commit the UI**

```bash
git add ui/data_monitor_page.py test_data_monitor_page.py
git commit -m "增加数据监测评论洞察面板"
```

---

### Task 9: Scope gate, integration hook, regression ladder and real read-only acceptance

**Files:**
- Data branch: all files from Tasks 1–8.
- Integration branch modify: `app_core/database.py`
- Integration branch create: `test_platform_data_comment_database_integration.py`
- Update: `docs/superpowers/reports/2026-08-23-douyin-comment-insight-verification.md`

**Interfaces:**
- Shared hook: `create_comment_schema(conn)` called exactly once during `database.ensure_schema()` after `platform_contents` exists.
- Acceptance report records local tests, real sync IDs/counts, UI/DB readback, cleanup and missing gates without comment bodies or identity.

- [ ] **Step 1: Run focused comment tests on the data branch**

```bash
/Users/andy/Documents/Codex/2026-07-28/new-chat/outputs/一键发桌面UI基座/.venv/bin/python \
  -m unittest -v \
  test_platform_data_comment_models \
  test_platform_data_comment_store \
  test_platform_data_comment_contract \
  test_douyin_comment_data_collector \
  test_platform_data_comment_service \
  test_platform_data_comment_ai \
  test_douyin_data_collector \
  test_platform_data_sync \
  test_data_monitor_page
```

Expected: all named modules end `OK`. Stop at the first failing module, repair it, rerun that module, then continue.

- [ ] **Step 2: Run the mandatory data-monitoring suite**

```bash
/Users/andy/Documents/Codex/2026-07-28/new-chat/outputs/一键发桌面UI基座/.venv/bin/python \
  -m unittest -v \
  test_platform_data_service test_platform_data_sync test_data_monitor_page \
  test_douyin_data_collector test_xiaohongshu_data_collector \
  test_wechat_data_collector test_bilibili_data_collector test_kuaishou_data_collector
git diff --check
```

- [ ] **Step 3: Run the data branch scope checker**

```bash
cd /Users/andy/Documents/Codex/2026-07-28/new-chat/outputs/一键发桌面UI基座/.worktrees/data-monitoring-v3
/Users/andy/Documents/Codex/2026-07-28/new-chat/outputs/一键发桌面UI基座/.venv/bin/python \
  /Users/andy/Documents/Codex/2026-07-28/new-chat/outputs/一键发桌面UI基座/tools/check_workstream_scope.py \
  --stream data --base origin/main
```

Expected: `scope-check: OK`. Any `shared` or `outside` path blocks data-branch completion.

- [ ] **Step 4: Create the shared hook only on the integration branch**

In `app_core/database.py`:

```python
from .platform_data_comment_store import create_comment_schema
```

After the existing `platform_contents` table and index are created:

```python
create_comment_schema(conn)
```

Add an integration test that patches `DB_PATH`, runs `database.ensure_schema()` twice, and verifies all comment tables/indexes exist once without deleting pre-existing platform data.

- [ ] **Step 5: Run shared-core gates on the integration branch**

```bash
.venv/bin/python -m unittest -v test_platform_data_comment_database_integration
.venv/bin/python -m unittest discover -p 'test_*.py'
QT_QPA_PLATFORM=offscreen .venv/bin/python desktop_native_app.py --ui-test
git diff --check
```

Record exact test counts from this run. Do not reuse the earlier `1454/1454` result.

- [ ] **Step 6: Commit the integration hook separately**

```bash
git add app_core/database.py test_platform_data_comment_database_integration.py
git commit -m "接入评论洞察数据库模式"
```

Do not bump version or package here unless the integration owner confirms all release lines are ready. When that separate release gate is reached, use the unified candidate `0.5.0` and verify app/Mac/Windows/build names together.

- [ ] **Step 7: Run authorized real read-only acceptance when the contract exists**

Select one owned Douyin work with at least five visible comments and run manual sync twice. Verify:

- two distinct local run IDs;
- body, likes, replies and time match the official page for at least five rows;
- second sync creates no duplicate and updates encountered counts;
- DB schema and UI contain no avatar, nickname, user ID or raw platform comment ID;
- AI unconfigured or intentionally failed still leaves comments visible;
- configured AI yields at most five candidates and every evidence expansion points to a stored original comment;
- no reply/delete/like/publish action occurred;
- browser/page/context/Playwright cleanup reads closed with zero live resources.

If no eligible work exists, record the exact fixed blocker and retain status “local implementation verified; real Douyin availability unverified”.

- [ ] **Step 8: Write the truthful verification report**

Update `docs/superpowers/reports/2026-08-23-douyin-comment-insight-verification.md` with:

- branch and commit IDs;
- focused/data/full test commands and counts;
- scope check result;
- contract status;
- first/second sync run IDs and aggregate counts only;
- DB/UI/privacy/cleanup results;
- AI configured or unconfigured status;
- precise remaining blocker.

Never include comment bodies, account IDs, content IDs, hashes, paths, cookies, headers, secrets or raw responses.

- [ ] **Step 9: Commit the verification evidence on its owning branch**

```bash
git add docs/superpowers/reports/2026-08-23-douyin-comment-insight-verification.md
git commit -m "记录抖音评论洞察验收结果"
```

---

## Plan Self-Review

- [ ] Every runtime data path is read-only and requires manual comment sync.
- [ ] Normal account sync never calls the comment collector.
- [ ] No author identity or raw platform comment ID exists in models, DB, UI, logs, reports or AI requests.
- [ ] Missing real contract fails closed; no endpoint, field or selector is guessed.
- [ ] First sync caps at 100; later sync stops after the first known key; history is never deleted by absence.
- [ ] Comment persistence and AI persistence are separate transactions.
- [ ] Every AI candidate has existing local evidence and there are at most five.
- [ ] API Key exists only in the native vault; QSettings stores only HTTPS base URL and model.
- [ ] Late background results cannot cross platform/account/content selection.
- [ ] Data branch changes only owned files; `database.py` remains an integration-only commit.
- [ ] Feature branch stays on version `0.4.2`; version/package work remains a separate integration release gate.
- [ ] Local tests, real platform availability and publishing state are reported as different facts.
- [ ] No step contains an unfinished marker, an unverified production constant or a platform write action.
