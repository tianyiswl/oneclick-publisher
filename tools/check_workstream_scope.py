# -*- coding: utf-8 -*-
"""检查三条功能分支是否越过文件边界。"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import PurePosixPath


STREAMS = ("data", "overseas", "location", "integration")

SHARED_EXACT = {
    "AGENTS.md",
    "SOURCE_OF_TRUTH.md",
    "QUALITY_GATES.md",
    "README.md",
    "requirements-oneclick.txt",
    "conf.py",
    "desktop_native_app.py",
    "app_core/login_service.py",
    "app_core/account_service.py",
    "app_core/account_browser_service.py",
    "app_core/database.py",
    "app_core/paths.py",
    "app_core/publish_service.py",
    "app_core/publish_runtime.py",
    "app_core/tiktok_schedule_contract.py",
    "app_core/controlled_publish.py",
    "app_core/task_service.py",
    "app_core/oneclick_preflight.py",
    "app_core/xhs_native_adapter.py",
    "app_core/xhs_publish_executor.py",
    "app_core/oneclick_authorization.py",
    "app_core/oneclick_capabilities.py",
    "app_core/branding.py",
    "ui/account_page.py",
    "ui/login_dialog.py",
    "ui/publish_page.py",
    "ui/main_window.py",
    "myUtils/postVideo.py",
    "myUtils/login.py",
    "tools/check_workstream_scope.py",
    "test_tiktok_schedule_contract.py",
    "test_account_detection_ui.py",
    "test_controlled_publish.py",
    "test_task_service.py",
    "test_publish_service.py",
    "test_oneclick_capabilities.py",
    "test_tiktok_login_startup_wiring.py",
    "test_workstream_scope.py",
    ".superpowers/sdd/2026-08-28-tiktok-system-browser-login-bridge/progress.md",
    ".superpowers/sdd/2026-08-28-tiktok-system-browser-login-bridge/task-5-report.md",
}

SHARED_PREFIXES = (
    ".github/",
    "tools/build_",
)


def normalize_path(value: str) -> str:
    path = value.strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return str(PurePosixPath(path)) if path else ""


def is_shared(path: str) -> bool:
    return path in SHARED_EXACT or path.startswith(SHARED_PREFIXES)


def is_data_owned(path: str) -> bool:
    name = PurePosixPath(path).name
    if path.startswith("app_core/platform_data_"):
        return True
    if path == "app_core/domestic_data_collector.py":
        return True
    if path.startswith("app_core/") and name.endswith("_data_collector.py"):
        return True
    if path == "ui/data_monitor_page.py":
        return True
    if path.startswith("test_") and (
        "data_monitor" in path
        or "platform_data" in path
        or path.endswith("_data_collector.py")
    ):
        return True
    lowered = path.lower()
    if path.startswith("docs/") and any(
        marker in lowered
        for marker in ("data-monitoring", "data_monitoring", "data-contract", "comment-insight")
    ):
        return True
    return False


def is_overseas_owned(path: str) -> bool:
    if path.startswith("app_core/overseas"):
        return True
    if path.startswith(("uploader/tk_uploader/", "uploader/youtube_uploader/", "uploader/meta_uploader/")):
        return True
    if path == "ui/overseas_authorization_dialog.py":
        return True
    if path.startswith("test_overseas_") or path == "test_meta_browser_publish.py":
        return True
    if path.startswith("docs/OVERSEAS_"):
        return True
    if path.startswith("docs/adr/") and any(
        marker in path.lower() for marker in ("tiktok", "youtube", "meta", "overseas")
    ):
        return True
    document = PurePosixPath(path)
    if (
        path.startswith("docs/")
        and "tiktok" in document.name.lower()
        and str(document.parent)
        in {
            "docs/architecture",
            "docs/verification",
            "docs/superpowers/specs",
            "docs/superpowers/plans",
        }
    ):
        return True
    return False


def is_location_owned(path: str) -> bool:
    if path.startswith(("app_core/douyin_location", "app_core/douyin_commerce", "app_core/_douyin_commerce")):
        return True
    if path.startswith("app_core/xhs_location"):
        return True
    if path == "ui/douyin_commerce_page.py":
        return True
    if path.startswith("test_douyin_location") or path.startswith("test_douyin_commerce"):
        return True
    if path.startswith("test_xhs_location"):
        return True
    lowered = path.lower()
    if path.startswith("docs/") and (
        "douyin_commerce" in lowered
        or "douyin-commerce" in lowered
        or "douyin_location" in lowered
        or "douyin-location" in lowered
        or "xhs-video-location" in lowered
        or "xhs-location" in lowered
        or "xhs_location" in lowered
    ):
        return True
    return False


def classify_path(path: str, stream: str) -> str:
    normalized = normalize_path(path)
    if stream == "integration":
        return "owned"
    if is_shared(normalized):
        return "shared"
    owned_checks = {
        "data": is_data_owned,
        "overseas": is_overseas_owned,
        "location": is_location_owned,
    }
    return "owned" if owned_checks[stream](normalized) else "outside"


def _git_name_list(command: list[str]) -> list[str]:
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "git diff failed"
        raise RuntimeError(message)
    return [normalize_path(line) for line in result.stdout.splitlines() if line.strip()]


def changed_files(base: str) -> list[str]:
    commands = (
        ["git", "diff", "--name-only", "--diff-filter=ACDMRTUXB", f"{base}...HEAD"],
        ["git", "diff", "--name-only", "--diff-filter=ACDMRTUXB"],
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACDMRTUXB"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    )
    files: set[str] = set()
    for command in commands:
        files.update(_git_name_list(command))
    return sorted(files)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stream", required=True, choices=STREAMS)
    parser.add_argument("--base", default="origin/main")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        files = changed_files(args.base)
    except RuntimeError as exc:
        print(f"scope-check-error: {exc}", file=sys.stderr)
        return 1

    groups = {"owned": [], "shared": [], "outside": []}
    for path in files:
        groups[classify_path(path, args.stream)].append(path)

    print(f"stream={args.stream} base={args.base} changed={len(files)}")
    for label in ("owned", "shared", "outside"):
        for path in groups[label]:
            print(f"{label}: {path}")

    if groups["shared"] or groups["outside"]:
        print("scope-check: REVIEW_REQUIRED")
        return 2
    print("scope-check: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
