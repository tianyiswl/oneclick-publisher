# Task 7 fix round 1 command receipts

Date: 2026-09-03

## Clean snapshot identity

- Worktree: `/private/tmp/oneclick-task7-fix1-663b68e`
- Initial HEAD: `663b68e970a2cf9e4dc103bd6f3a9d49529b9ff4`
- Final validated HEAD: `606925f28fcf9fddfab01c83349e6c831d3a2d43`
- Final validated tree: `e671320f3737b9b7c3217347ed41622c0428f87b`
- Branch commit with identical tree: `696ab17324e7bbe0c2fda6997a42032728df93eb`
- Every final command below printed an empty `git status --porcelain` before and after execution.

## Baseline and diagnostic runs

1. Clean `663b68e` directed suite:
   - command: `ONECLICK_MONTAGE_EVIDENCE_DIR=<fix-evidence>/integration-batch QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest -v test_montage_models test_narration_service test_montage_runtime test_montage_service test_automatic_montage_page test_montage_narration_integration test_main_window`
   - result: `Ran 75 tests in 6.761s`, `OK`; real Mac integration was not skipped.
2. Clean `663b68e` offscreen UI with an isolated user-data directory:
   - result: `NATIVE_DESKTOP_UI_OK`, exit 0.
3. Clean `663b68e` full suite with `YIJIANFA_USER_DATA_DIR` set globally:
   - result: `Ran 3173 tests`, 1 failure in `test_frozen_windows_uses_local_app_data_outside_bundle`.
   - interpretation: invalid test invocation for the Windows default-path contract because the command intentionally overrode that contract; not accepted as a pass.
4. Clean `663b68e` full suite without that global override:
   - result: `3173/3173`, `190.820s`, exit 0.
5. Clean `663b68e` source launch with `--page montage`:
   - result: argparse rejected `montage`, exit 2; this established that the committed source client was not wired to the page.
6. First full suite after the source-client wiring:
   - result: `Ran 3174 tests in 205.661s`, 3 failures.
   - failures: old fixed navigation indices in two account UI tests and one Douyin commerce UI test.
   - repair: update expected positions after inserting the montage page; the 3 exact tests then passed.

## Final accepted runs on `606925f`

### Directed montage and real Mac integration

```bash
ONECLICK_MONTAGE_EVIDENCE_DIR='<fix-evidence>/integration-batch-head-606925f' \
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest -v \
  test_montage_models test_narration_service test_montage_runtime \
  test_montage_service test_automatic_montage_page \
  test_montage_narration_integration test_main_window
```

Result: `Ran 76 tests in 7.348s`, `OK`, exit 0; real Mac integration was not skipped. Saved output (incidental trailing spaces normalized): `command-output/directed-unittest-606925f.log`, SHA-256 `46a6fccb14499e3a38f50ffc1ce393ffb1b73fba0dc9651f542abefdba8137eb`.

### Full suite

```bash
.venv/bin/python -m unittest
```

Result: `Ran 3174 tests in 203.833s`, `OK`, exit 0. Saved output (incidental trailing spaces normalized): `command-output/full-unittest-606925f.log`, SHA-256 `1687bc40ab5595a0ff9bb980a5691c44ec7cda5ed4e678f760a38917417803dc`.

### Offscreen source UI

```bash
YIJIANFA_USER_DATA_DIR='<fix-evidence>/ui-test-head-606925f' \
QT_QPA_PLATFORM=offscreen .venv/bin/python desktop_native_app.py --ui-test
```

Result: `NATIVE_DESKTOP_UI_OK`, exit 0. Saved output (incidental trailing spaces normalized): `command-output/offscreen-ui-606925f.log`, SHA-256 `b27e5e308d427673dac5a1d8427de0461a26213723c771dd764b16b6bab8cc02`.

### Source client launch

```bash
YIJIANFA_USER_DATA_DIR='<fix-evidence>/runtime-user-data' \
PYTHONUNBUFFERED=1 .venv/bin/python -u desktop_native_app.py --page montage
```

Result: process PID `82411`; `lsof -a -p 82411 -d cwd -Fn` returned `/private/tmp/oneclick-task7-fix1-663b68e`. The client remains running. QuickTime points to `<fix-evidence>/runtime-user-data/automatic-montage/M0903163951-A0F528/001/video.mp4`, playback is off, elapsed time is `00:00`, and timeline value is `0`.

## Archify commands

For each of `architecture` and `lifecycle`:

```bash
node bin/archify.mjs validate <type> <json> --quality showcase --json
node bin/archify.mjs deliver <type> <json> <html> --quality showcase --json
node bin/archify.mjs visual-check <html> --json
```

Both validate/deliver receipts: `9/9 showcase`, `0 errors`, `0 warnings`, exit 0. Both visual checks: pass, four containment sizes pass, four captures pass, `visualReview: pending`. The eight minimum/maximum light/dark screenshots were manually inspected with no correction required. Saved JSON output is under `command-output/`.

## Isolation and readback

- Neutral source SHA-256: `0a22d13e042c59c6ca141d2f221408c8aafdf76f2580eb99a03a001684c12b81`, `b066c908f05c53fa5901ac74d003b3b310c909f11f9b23175974f2de353e49ae`.
- Isolated batch: `M0903163951-A0F528`, `success=3`, `failed=0`, 1080×1920, 7509ms.
- Narration master SHA-256: `8ccf56dc517f05462d97853f27bc3fa79430e8d41f4ef2d7ae9e9234cd0187f8`; stream SHA-256: `6f8bba7d196a00e4e0cdbe764707cd1598293d39694a40b8101d0c71d8cfe9b0`.
- Output SHA-256: `4665d288fd2caae1635fe5bd2355cb3ad22e13aa5a974412a7f27b44c4d351ee`, `5f657948ff313588a8e585593d6544331e28323ce2027633df150ffb29e34e16`, `d4e524a8bd7a250a61bb0849847b51d48b2f2baa86a4156911f40065894b974b`.
- The exact narration text has zero matches in all batch JSON. `request.json` contains only the two isolated runtime source copies and safe parameters.
- Old formal-data batch `M0903160612-1765F3` is invalid acceptance evidence and remains unchanged; its receipt SHA-256 is `b79eb04adef337c3ec7b5de91aca2cb4dd89c0e9cb8333bb65c026e2a01bf3f4`.
