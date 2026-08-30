# Integration I3 report — Page credential cleanup and exact refresh identity

## Status

Completed on `integration/facebook-page-v1-local` within the I3-owned login, account service, account UI and directly related test files.

## Fixes

- A successful Facebook Page relogin now performs best-effort cleanup only after the replacement account row has committed. The prior storage-state file is removed only when no account row references its exact stored name and the file is a non-symlink basename whose resolved parent is the managed `cookiesFile` directory. Shared files, absolute/nested values, and symlinks resolving outside the managed directory are preserved. Cleanup errors do not roll back or invalidate the committed replacement row.
- With Facebook Page V1 disabled, both account-page refresh menu entries are disabled, direct UI invocation is rejected before a worker starts, and `account_service.refresh_account_avatar` rejects direct callers with `facebook_page_feature_disabled`.
- With the feature enabled, Page refresh validates the saved row, passes its exact `accountReference` to the existing Page activation/readback contract, rejects a different or unavailable Page, downloads only a bounded image from an allowlisted Meta HTTPS host, and conditionally writes the returned Page name and local avatar only if the complete saved account snapshot is still unchanged. Failure leaves the stored display name and avatar reference unchanged.

## TDD evidence

Each required behavior was added first and observed RED before the corresponding production edit:

1. Successful relogin left the zero-reference old managed session present (`True` instead of `False`), while the same test fixed the preservation contract for a shared session and an outside-target symlink.
2. Simulated old-file unlink failure escaped instead of remaining best-effort, even though the newly committed `filePath` was already present.
3. With the feature flag off, the UI refresh action remained enabled, direct UI refresh started a worker, and the service did not raise `facebook_page_feature_disabled`.
4. With the feature flag on, refresh still used the generic current-page avatar path, did not update the exact Page name/avatar, did not reject a wrong Page, and had no exact identity reader passing the stored Page ID.

After the minimal production changes, all focused tests were GREEN.

## Verification

Fresh affected-suite run:

```text
test_facebook_page_login
test_facebook_page_identity
test_facebook_page_database
test_account_detection_ui
test_overseas_integration
Ran 138 tests in 1.050s
OK
```

`python -m py_compile` passed for all five changed Python modules/tests. `git diff --check` passed.

## Boundaries and concerns

- No default flag, version, package, publish/form, task/authorization file, credential store, secret, login, Facebook browser, preflight, upload, final action, merge, push or real platform state was changed or exercised.
- This is source and offline-test verification only. A separately authorized real Page account run is still required to verify the current Meta composer identity DOM and avatar CDN response shape; any unsupported avatar redirect or host fails safely without changing the stored display identity.

## Fix round 1 — alias-safe cleanup and locked final deletion

Review found that the initial cleanup queried only the prior `filePath` string and closed SQLite before unlinking. A non-Page account could therefore reference the same managed file through `./name`, an absolute managed path, surrounding whitespace or another abnormal alias without being counted. A competing writer could also add a reference after the query but before deletion.

### TDD evidence

Before the correction, two focused tests were RED:

1. Relative, absolute-managed and whitespace aliases all left `False` file-existence results instead of the required `True`; the extended case also covers an unsafe NUL-bearing possible alias.
2. A competing SQLite connection committed one new reference during the unlink hook, producing `(committed=True, fileExists=False, references=1)` instead of `(False, False, 0)`.

### Correction

- The cleanup now scans every account `filePath`, strips only for conservative alias recognition, resolves relative and absolute paths against the real managed credential root, compares resolved paths and existing-file identity, and vetoes deletion when a value cannot be normalized safely.
- The final all-account reference scan and unlink now execute inside one `BEGIN IMMEDIATE` transaction. A competing reference write cannot commit inside that interval. Database/normalization/unlink failures remain best-effort and do not affect the already committed replacement account row.

### Verification

The four focused cleanup tests passed after the correction: zero-reference deletion, exact/shared/outside preservation, cleanup-failure isolation, alias preservation and locked concurrent-write coverage. The same five affected suites listed above then passed `140/140` in `0.973s`.
