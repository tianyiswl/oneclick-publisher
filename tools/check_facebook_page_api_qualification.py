# -*- coding: utf-8 -*-
"""Run the zero-write Facebook Page API qualification probe."""

from __future__ import annotations

import argparse
import getpass
import json
from pathlib import Path
import sys
from typing import Any, Callable, TextIO


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app_core.overseas_facebook_api_qualification import (  # noqa: E402
    DEFAULT_GRAPH_VERSION,
    FacebookPageApiQualificationClient,
    FacebookPageApiQualificationError,
    KeyringFacebookPageApiCredentialStore,
    check_supplied_user_access_token,
    load_saved_facebook_page_account_read_only,
    qualify_saved_facebook_page_account,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "只读核对 Facebook Page API scope 与内容创建任务；"
            "不会保存手工令牌，也不会上传、建草稿或发布。"
        )
    )
    parser.add_argument("action", choices=("check", "check-token"))
    parser.add_argument("--account-id", type=int, required=True)
    parser.add_argument("--graph-version", default=DEFAULT_GRAPH_VERSION)
    return parser


def _write_json(stdout: TextIO, value: dict[str, object]) -> None:
    stdout.write(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    stdout.write("\n")
    stdout.flush()


def run(
    argv: list[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    account_loader: Callable[
        [int], object | None
    ] = load_saved_facebook_page_account_read_only,
    credential_store: Any | None = None,
    client: FacebookPageApiQualificationClient | None = None,
    secret_reader: Callable[[str], str] = getpass.getpass,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        if int(args.account_id) <= 0:
            raise FacebookPageApiQualificationError(
                "facebook_api_saved_account_invalid",
                "Facebook Page 账号 ID 无效。",
            )
        account = account_loader(int(args.account_id))
        if account is None:
            raise FacebookPageApiQualificationError(
                "facebook_api_saved_account_invalid",
                "一键发中没有找到目标 Facebook Page 账号。",
            )
        probe = client or FacebookPageApiQualificationClient(
            graph_version=str(args.graph_version)
        )
        if args.action == "check":
            store = credential_store or KeyringFacebookPageApiCredentialStore()
            result = qualify_saved_facebook_page_account(
                account,
                credential_store=store,
                client=probe,
            )
        else:
            token = secret_reader(
                "粘贴由大帅自己的 Meta App 生成的用户 Access Token（仅内存核对，不回显、不保存）："
            )
            result = check_supplied_user_access_token(
                account,
                token,
                client=probe,
            )
        result["accountId"] = int(args.account_id)
        _write_json(stdout, result)
        return 0 if result.get("apiModeUsable") is True else 2
    except FacebookPageApiQualificationError as exc:
        _write_json(
            stdout,
            {
                "status": "failed",
                "qualified": False,
                "pagePermissionCheckPassed": False,
                "appAuthorizationVerified": False,
                "apiModeUsable": False,
                "errorCode": exc.code,
                "message": str(exc),
                "writeAttempted": False,
            },
        )
        return 2


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
