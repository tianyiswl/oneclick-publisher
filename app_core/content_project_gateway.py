# -*- coding: utf-8 -*-
"""内容项目调用一键发的本机安全网关。"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import account_service, controlled_publish, oneclick_capabilities
from .paths import USER_DATA_DIR
from .source_live_runtime import source_live_data_active, source_live_session_active


PROFILE_SCHEMA_VERSION = 1
DEFAULT_PROFILE_PATH = USER_DATA_DIR / "publish-profiles.json"
_PROJECT_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{1,63}")
_PLATFORM_TYPE_BY_NAME = {
    oneclick_capabilities.canonical_platform(name): platform_type
    for platform_type, name in account_service.PLATFORMS.items()
}


def _source_live_conflict() -> bool:
    return not source_live_data_active() and source_live_session_active(
        USER_DATA_DIR / "source-live-session.json"
    )


class ContentProjectGatewayError(ValueError):
    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = str(error_code)
        self.public_message = str(message)
        super().__init__(message)


def _mapping(value: object, error_code: str, message: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContentProjectGatewayError(error_code, message)
    return value


class PublishProfileStore:
    """本机保存“内容项目 -> 平台账号”映射。

    文件不保存 Cookie、密码、验证码或会话路径。
    """

    def __init__(self, path: Path | str = DEFAULT_PROFILE_PATH) -> None:
        self.path = Path(path).expanduser()

    def _read_root(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schemaVersion": PROFILE_SCHEMA_VERSION, "profiles": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContentProjectGatewayError(
                "content_project_profiles_invalid", "本机项目发布档案无法读取"
            ) from exc
        if (
            not isinstance(data, dict)
            or data.get("schemaVersion") != PROFILE_SCHEMA_VERSION
            or not isinstance(data.get("profiles"), list)
        ):
            raise ContentProjectGatewayError(
                "content_project_profiles_migration_required",
                "项目发布档案版本不兼容，需要先由一键发显式迁移",
            )
        return data

    def list(self) -> list[dict[str, Any]]:
        return [dict(profile) for profile in self._read_root()["profiles"]]

    def get(self, project_id: str) -> dict[str, Any]:
        for profile in self.list():
            if profile.get("projectId") == project_id:
                return profile
        raise ContentProjectGatewayError(
            "content_project_profile_not_found", f"未配置内容项目发布档案：{project_id}"
        )

    def save(self, profile: Mapping[str, Any]) -> dict[str, Any]:
        stored = dict(profile)
        root = self._read_root()
        profiles = [
            dict(item)
            for item in root["profiles"]
            if isinstance(item, Mapping) and item.get("projectId") != stored.get("projectId")
        ]
        profiles.append(stored)
        profiles.sort(key=lambda item: str(item.get("projectId") or ""))
        payload = json.dumps(
            {"schemaVersion": PROFILE_SCHEMA_VERSION, "profiles": profiles},
            ensure_ascii=False,
            indent=2,
        ) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
                temporary_path = Path(handle.name)
            temporary_path.replace(self.path)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()
        return stored


class ContentProjectGateway:
    """将项目标识解析成现有受控发布服务请求。"""

    def __init__(
        self,
        *,
        profile_store: PublishProfileStore | None = None,
        accounts_provider: Callable[[], Sequence[Mapping[str, Any]]] = account_service.list_accounts,
        submitter: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
        status_reader: Callable[[int], dict[str, Any]] = controlled_publish.task_status,
        authorizer: Callable[[int], dict[str, Any]] = controlled_publish.authorize_completed_preflight,
        runtime_conflict_checker: Callable[[], bool] = _source_live_conflict,
        silicon_submitter: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.profile_store = profile_store or PublishProfileStore()
        self.accounts_provider = accounts_provider
        if submitter is None:
            from .controlled_publish_process import submit_request_in_process

            submitter = submit_request_in_process
        self.submitter = submitter
        self.status_reader = status_reader
        self.authorizer = authorizer
        self.runtime_conflict_checker = runtime_conflict_checker
        if silicon_submitter is None:
            from .controlled_publish_process import (
                submit_silicon_evolution_request_in_process,
            )

            silicon_submitter = submit_silicon_evolution_request_in_process
        self.silicon_submitter = silicon_submitter

    def _ensure_platform_work_available(self) -> None:
        if self.runtime_conflict_checker():
            raise ContentProjectGatewayError(
                "source_live_session_active",
                "源码联调客户端正在共用正式账号数据，请先关闭后再创建平台任务",
            )

    def _account_rows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.accounts_provider()]

    def list_accounts(self) -> list[dict[str, Any]]:
        accounts = []
        for row in self._account_rows():
            platform_type = int(row.get("type") or 0)
            account_id = int(row.get("id") or 0)
            if platform_type <= 0 or account_id <= 0:
                continue
            accounts.append(
                {
                    "accountId": account_id,
                    "platform": account_service.PLATFORMS.get(
                        platform_type, f"平台{platform_type}"
                    ),
                    "platformType": platform_type,
                    "account": str(
                        row.get("profileName") or row.get("userName") or "未命名主体"
                    ),
                    "healthStatus": str(row.get("healthStatus") or "unknown"),
                    "statusText": str(row.get("statusText") or "未检测"),
                }
            )
        return accounts

    def list_profiles(self) -> list[dict[str, Any]]:
        return self.profile_store.list()

    def save_profile(
        self,
        project_id: str,
        display_name: str,
        targets: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        project_id = str(project_id or "").strip().lower()
        if not _PROJECT_ID_RE.fullmatch(project_id):
            raise ContentProjectGatewayError(
                "content_project_id_invalid",
                "项目标识必须是 2-64 位小写英文、数字、点、下划线或连字符",
            )
        display_name = str(display_name or "").strip()
        if not display_name:
            raise ContentProjectGatewayError(
                "content_project_display_name_required", "项目显示名称不能为空"
            )
        if not isinstance(targets, Sequence) or isinstance(targets, (str, bytes)) or not targets:
            raise ContentProjectGatewayError(
                "content_project_targets_required", "项目至少需要一个平台账号"
            )
        accounts_by_id = {
            int(row.get("id") or 0): row
            for row in self._account_rows()
            if int(row.get("id") or 0) > 0
        }
        normalized_targets: list[dict[str, Any]] = []
        seen: set[tuple[str, int]] = set()
        for raw_target in targets:
            target = _mapping(
                raw_target, "content_project_target_invalid", "项目平台目标必须是对象"
            )
            if set(target) - {"platform", "accountId"}:
                raise ContentProjectGatewayError(
                    "content_project_target_invalid", "项目平台目标包含不支持字段"
                )
            platform = oneclick_capabilities.canonical_platform(
                str(target.get("platform") or "").strip()
            )
            account_id = target.get("accountId")
            platform_type = _PLATFORM_TYPE_BY_NAME.get(platform)
            if platform_type is None or type(account_id) is not int or account_id <= 0:
                raise ContentProjectGatewayError(
                    "content_project_target_invalid", "平台和 accountId 必须明确有效"
                )
            account = accounts_by_id.get(account_id)
            if not account or int(account.get("type") or 0) != platform_type:
                raise ContentProjectGatewayError(
                    "content_project_account_mismatch",
                    f"账号 {account_id} 不属于 {platform}",
                )
            identity = (platform, account_id)
            if identity in seen:
                raise ContentProjectGatewayError(
                    "content_project_target_duplicate", "同一平台账号不能重复配置"
                )
            seen.add(identity)
            normalized_targets.append({"platform": platform, "accountId": account_id})
        return self.profile_store.save(
            {
                "schemaVersion": PROFILE_SCHEMA_VERSION,
                "projectId": project_id,
                "displayName": display_name,
                "targets": normalized_targets,
            }
        )

    def _request(
        self,
        project_id: str,
        manifest_path: str,
        mode: str,
        schedules: Mapping[str, object] | None,
    ) -> dict[str, Any]:
        profile = self.profile_store.get(str(project_id or "").strip().lower())
        normalized_schedules = {
            oneclick_capabilities.canonical_platform(str(platform)): schedule
            for platform, schedule in dict(schedules or {}).items()
        }
        configured_platforms = {
            str(target.get("platform") or "") for target in profile.get("targets") or []
        }
        extra_schedules = set(normalized_schedules) - configured_platforms
        if extra_schedules:
            raise ContentProjectGatewayError(
                "content_project_schedule_mismatch",
                "发布时间包含项目未配置的平台：" + "、".join(sorted(extra_schedules)),
            )
        targets = [
            {
                "platform": str(target["platform"]),
                "accountId": int(target["accountId"]),
                "schedule": normalized_schedules.get(str(target["platform"])),
            }
            for target in profile.get("targets") or []
        ]
        return {
            "manifestPath": str(manifest_path or "").strip(),
            "mode": mode,
            "targets": targets,
        }

    def preflight_content(
        self,
        project_id: str,
        manifest_path: str,
        schedules: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        self._ensure_platform_work_available()
        return self.submitter(
            self._request(project_id, manifest_path, "preflight", schedules)
        )

    def formal_publish(
        self,
        project_id: str,
        manifest_path: str,
        *,
        confirmed_preflight_task_id: int,
        authorization_id: str,
        schedules: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        self._ensure_platform_work_available()
        if (
            type(confirmed_preflight_task_id) is not int
            or confirmed_preflight_task_id <= 0
            or not str(authorization_id or "").strip()
        ):
            raise ContentProjectGatewayError(
                "content_project_authorization_required",
                "正式发布必须绑定全部成功的预检和一次性本地授权",
            )
        request = self._request(project_id, manifest_path, "formal", schedules)
        request.update(
            {
                "confirmedPreflightTaskId": confirmed_preflight_task_id,
                "authorizationId": str(authorization_id).strip(),
            }
        )
        return self.submitter(request)

    def task_status(self, task_id: int) -> dict[str, Any]:
        if type(task_id) is not int or task_id <= 0:
            raise ContentProjectGatewayError(
                "content_project_task_id_invalid", "taskId 必须是正整数"
            )
        return self.status_reader(task_id)

    def authorize_preflight(self, task_id: int) -> dict[str, Any]:
        if type(task_id) is not int or task_id <= 0:
            raise ContentProjectGatewayError(
                "content_project_task_id_invalid", "预检 taskId 必须是正整数"
            )
        return self.authorizer(task_id)

    def _silicon_evolution_account_id(self) -> int:
        profile = self.profile_store.get("silicon-evolution")
        targets = list(profile.get("targets") or [])
        if profile.get("displayName") != "硅基进化" or len(targets) != 1:
            raise ContentProjectGatewayError(
                "silicon_evolution_profile_invalid",
                "硅基进化必须只绑定一个公众号账号",
            )
        target = dict(targets[0])
        if oneclick_capabilities.canonical_platform(
            str(target.get("platform") or "")
        ) != oneclick_capabilities.canonical_platform("公众号"):
            raise ContentProjectGatewayError(
                "silicon_evolution_profile_invalid",
                "硅基进化自动直发目标必须是公众号",
            )
        account_id = int(target.get("accountId") or 0)
        matches = [
            row
            for row in self._account_rows()
            if int(row.get("id") or 0) == account_id
            and int(row.get("type") or 0) == 10
            and str(row.get("profileName") or row.get("userName") or "")
            == "硅基进化"
        ]
        if len(matches) != 1:
            raise ContentProjectGatewayError(
                "silicon_evolution_account_mismatch",
                "硅基进化公众号账号无法唯一匹配",
            )
        return account_id

    def _silicon_evolution_request(
        self,
        article_id: str,
        package_path: str,
        package_sha256: str,
        *,
        mode: str,
        confirmed_preflight_task_id: int | None = None,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "projectId": "silicon-evolution",
            "articleId": str(article_id or "").strip(),
            "packagePath": str(package_path or "").strip(),
            "packageSha256": str(package_sha256 or "").strip(),
            "accountId": self._silicon_evolution_account_id(),
            "mode": mode,
        }
        if confirmed_preflight_task_id is not None:
            request["confirmedPreflightTaskId"] = confirmed_preflight_task_id
        return request

    def preflight_silicon_evolution_release(
        self, article_id: str, package_path: str, package_sha256: str
    ) -> dict[str, Any]:
        self._ensure_platform_work_available()
        return self.silicon_submitter(
            self._silicon_evolution_request(
                article_id,
                package_path,
                package_sha256,
                mode="preflight",
            )
        )

    def auto_publish_silicon_evolution_release(
        self,
        article_id: str,
        package_path: str,
        package_sha256: str,
        *,
        confirmed_preflight_task_id: int,
    ) -> dict[str, Any]:
        self._ensure_platform_work_available()
        if type(confirmed_preflight_task_id) is not int or confirmed_preflight_task_id <= 0:
            raise ContentProjectGatewayError(
                "silicon_evolution_preflight_required",
                "自动直发必须绑定成功预检 taskId",
            )
        return self.silicon_submitter(
            self._silicon_evolution_request(
                article_id,
                package_path,
                package_sha256,
                mode="formal",
                confirmed_preflight_task_id=confirmed_preflight_task_id,
            )
        )
