# -*- coding: utf-8 -*-
"""内容项目调用一键发的本机安全网关。"""

from __future__ import annotations

import json
import os
import re
import tempfile
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import account_service, content_bundle, controlled_publish, oneclick_capabilities
from .paths import USER_DATA_DIR
from .content_project_metrics import ContentProjectMetricsService
from .overseas_meta_page_identity import facebook_page_v1_enabled
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
        accounts_provider: Callable[
            [], Sequence[Mapping[str, Any]]
        ] = account_service.list_publishable_accounts,
        submitter: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
        status_reader: Callable[[int], dict[str, Any]] = controlled_publish.task_status,
        authorizer: Callable[[int], dict[str, Any]] = controlled_publish.authorize_completed_check,
        direct_authorizer: Callable[
            [Mapping[str, Any]], dict[str, Any]
        ] = controlled_publish.authorize_direct_request,
        runtime_conflict_checker: Callable[[], bool] = _source_live_conflict,
        silicon_submitter: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
        silicon_direct_authorizer: Callable[
            [Mapping[str, Any]], dict[str, Any]
        ] = controlled_publish.authorize_silicon_evolution_direct_request,
        matrix_submitter: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
        metrics_service: ContentProjectMetricsService | None = None,
        formal_submitter: Callable[[int, str], dict[str, Any]] | None = None,
        reconciler: Callable[[int], dict[str, Any]] = (
            controlled_publish.reconcile_facebook_page_publish_outcome
        ),
        facebook_feature_enabled: Callable[[], bool] = facebook_page_v1_enabled,
    ) -> None:
        self.profile_store = profile_store or PublishProfileStore()
        self.accounts_provider = accounts_provider
        if submitter is None:
            from .controlled_publish_process import submit_request_in_process

            submitter = submit_request_in_process
        self.submitter = submitter
        self.status_reader = status_reader
        self.authorizer = authorizer
        self.direct_authorizer = direct_authorizer
        self.runtime_conflict_checker = runtime_conflict_checker
        if silicon_submitter is None:
            from .controlled_publish_process import (
                submit_silicon_evolution_request_in_process,
            )

            silicon_submitter = submit_silicon_evolution_request_in_process
        self.silicon_submitter = silicon_submitter
        self.silicon_direct_authorizer = silicon_direct_authorizer
        if matrix_submitter is None:
            from .controlled_publish_process import (
                submit_douyin_graphic_matrix_request_in_process,
            )

            matrix_submitter = submit_douyin_graphic_matrix_request_in_process
        self.matrix_submitter = matrix_submitter
        self.metrics_service = metrics_service or ContentProjectMetricsService()
        if formal_submitter is None:
            from .controlled_publish_process import submit_authorized_preflight_task

            formal_submitter = submit_authorized_preflight_task
        self.formal_submitter = formal_submitter
        self.reconciler = reconciler
        self.facebook_feature_enabled = facebook_feature_enabled

    def _ensure_platform_work_available(self) -> None:
        if self.runtime_conflict_checker():
            raise ContentProjectGatewayError(
                "source_live_session_active",
                "源码联调客户端正在共用正式账号数据，请先关闭后再创建平台任务",
            )

    def _account_rows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.accounts_provider()]

    def _facebook_page_available(self) -> bool:
        return bool(self.facebook_feature_enabled())

    @staticmethod
    def _profile_has_facebook_page(profile: Mapping[str, Any]) -> bool:
        return any(
            oneclick_capabilities.canonical_platform(
                str(target.get("platform") or "")
            )
            == "Facebook Reels"
            for target in profile.get("targets") or []
            if isinstance(target, Mapping)
        )

    def _require_facebook_page_available(self) -> None:
        if not self._facebook_page_available():
            raise ContentProjectGatewayError(
                "facebook_page_feature_disabled",
                "Facebook Page 发布功能尚未开启。",
            )

    def list_accounts(self) -> list[dict[str, Any]]:
        accounts = []
        for row in self._account_rows():
            platform_type = int(row.get("type") or 0)
            account_id = int(row.get("id") or 0)
            if platform_type <= 0 or account_id <= 0:
                continue
            if platform_type == 9 and not self._facebook_page_available():
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
            if platform_type == 9:
                self._require_facebook_page_available()
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
        settings: Mapping[str, Mapping[str, Any]] | None,
    ) -> dict[str, Any]:
        normalized_project_id = str(project_id or "").strip().lower()
        profile = self.profile_store.get(normalized_project_id)
        if self._profile_has_facebook_page(profile):
            self._require_facebook_page_available()
            if mode == "direct":
                raise ContentProjectGatewayError(
                    "facebook_preflight_required",
                    "Facebook Page 首版必须先完成受控预检。",
                )
        normalized_schedules = {
            oneclick_capabilities.canonical_platform(str(platform)): schedule
            for platform, schedule in dict(schedules or {}).items()
        }
        raw_settings = (
            {}
            if settings is None
            else _mapping(
                settings,
                "content_project_settings_invalid",
                "平台发布设置必须是对象",
            )
        )
        normalized_settings = {
            oneclick_capabilities.canonical_platform(str(platform)): _mapping(
                value,
                "content_project_settings_invalid",
                "每个平台发布设置必须是对象",
            )
            for platform, value in raw_settings.items()
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
        extra_settings = set(normalized_settings) - configured_platforms
        if extra_settings:
            raise ContentProjectGatewayError(
                "content_project_settings_mismatch",
                "发布设置包含项目未配置的平台："
                + "、".join(sorted(extra_settings)),
            )
        targets = []
        for target in profile.get("targets") or []:
            platform = str(target["platform"])
            normalized_target = {
                "platform": str(target["platform"]),
                "accountId": int(target["accountId"]),
                "schedule": normalized_schedules.get(platform),
            }
            if platform in normalized_settings:
                normalized_target["settings"] = dict(normalized_settings[platform])
            targets.append(normalized_target)
        return {
            "projectId": normalized_project_id,
            "manifestPath": str(manifest_path or "").strip(),
            "mode": mode,
            "targets": targets,
        }

    def _metrics_profile(self, project_id: str) -> tuple[str, dict[str, Any]]:
        normalized = str(project_id or "").strip().lower()
        if not _PROJECT_ID_RE.fullmatch(normalized):
            raise ContentProjectGatewayError(
                "content_project_id_invalid",
                "项目标识必须是 2-64 位小写英文、数字、点、下划线或连字符",
            )
        return normalized, self.profile_store.get(normalized)

    def sync_project_metrics(self, project_id: str) -> dict[str, Any]:
        self._ensure_platform_work_available()
        normalized, profile = self._metrics_profile(project_id)
        return self.metrics_service.sync_project(normalized, profile)

    def get_project_metrics(
        self, project_id: str, days: int = 1
    ) -> dict[str, Any]:
        normalized, profile = self._metrics_profile(project_id)
        return self.metrics_service.get_project_metrics(normalized, profile, days)

    def metrics_sync_status(self, project_id: str) -> dict[str, Any]:
        normalized, profile = self._metrics_profile(project_id)
        return self.metrics_service.sync_status(normalized, profile)

    def preflight_content(
        self,
        project_id: str,
        manifest_path: str,
        schedules: Mapping[str, object] | None = None,
        settings: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        self._ensure_platform_work_available()
        return self.submitter(
            self._request(
                project_id,
                manifest_path,
                "preflight",
                schedules,
                settings,
            )
        )

    def formal_publish(
        self,
        *,
        preflight_task_id: int,
        authorization_id: str,
    ) -> dict[str, Any]:
        self._ensure_platform_work_available()
        if (
            type(preflight_task_id) is not int
            or preflight_task_id <= 0
            or not str(authorization_id or "").strip()
        ):
            raise ContentProjectGatewayError(
                "content_project_authorization_required",
                "正式发布必须绑定全部成功的预检和一次性本地授权",
            )
        return self.formal_submitter(
            preflight_task_id,
            str(authorization_id).strip(),
        )

    def direct_publish_content(
        self,
        project_id: str,
        manifest_path: str,
        schedules: Mapping[str, object] | None = None,
        settings: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """用当次对话授权直接创建隐藏的正式发布任务。

        这里不创建独立平台预检任务，但各平台执行器仍必须
        在同一正式会话中完成字段写入、回读和提交前检查。
        """

        self._ensure_platform_work_available()
        request = self._request(
            project_id,
            manifest_path,
            "direct",
            schedules,
            settings,
        )
        grant = self.direct_authorizer(request)
        authorization_id = str(grant.get("authorizationId") or "").strip()
        if not authorization_id:
            raise ContentProjectGatewayError(
                "content_project_direct_authorization_failed",
                "本机没有生成可用的后台直发授权",
            )
        request["directAuthorizationId"] = authorization_id
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
        projection = self.status_reader(task_id)
        if any(
            int(item.get("platformType") or 0) == 9
            for item in projection.get("platforms") or []
            if isinstance(item, Mapping)
        ):
            self._require_facebook_page_available()
        return self.authorizer(task_id)

    def reconcile_publish_outcome(self, task_id: int) -> dict[str, Any]:
        """Read-only Page outcome reconciliation; no authorization is accepted."""

        if type(task_id) is not int or task_id <= 0:
            raise ContentProjectGatewayError(
                "content_project_task_id_invalid",
                "taskId 必须是正整数",
            )
        self._require_facebook_page_available()
        return self.reconciler(task_id)

    @staticmethod
    def _matrix_schedule(value: object) -> str:
        if value is None:
            return ""
        schedule = _mapping(
            value,
            "douyin_graphic_matrix_schedule_invalid",
            "抖音图文账号排期必须是对象",
        )
        if set(schedule) - {"localTime", "timezone"}:
            raise ContentProjectGatewayError(
                "douyin_graphic_matrix_schedule_invalid",
                "抖音图文账号排期包含不支持字段",
            )
        timezone = str(schedule.get("timezone") or "Asia/Shanghai").strip()
        if timezone != "Asia/Shanghai":
            raise ContentProjectGatewayError(
                "douyin_graphic_matrix_schedule_invalid",
                "抖音图文矩阵只使用北京时间 Asia/Shanghai",
            )
        local_time = str(schedule.get("localTime") or "").strip()
        try:
            parsed = datetime.strptime(local_time, "%Y-%m-%d %H:%M")
        except ValueError as exc:
            raise ContentProjectGatewayError(
                "douyin_graphic_matrix_schedule_invalid",
                "抖音图文发布时间必须使用 YYYY-MM-DD HH:MM",
            ) from exc
        return parsed.strftime("%Y-%m-%d %H:%M")

    def _douyin_graphic_matrix(
        self,
        manifest_path: str,
        targets: Sequence[Mapping[str, Any]],
        *,
        runtime_mode: str,
    ) -> dict[str, Any]:
        try:
            bundle = content_bundle.load_content_bundle(
                manifest_path,
                expected_type="article",
            )
        except content_bundle.ContentBundleError as exc:
            raise ContentProjectGatewayError(
                "douyin_graphic_matrix_bundle_invalid",
                str(exc),
            ) from exc
        if not isinstance(targets, Sequence) or isinstance(targets, (str, bytes)):
            raise ContentProjectGatewayError(
                "douyin_graphic_matrix_targets_invalid",
                "抖音图文矩阵账号必须是有序列表",
            )
        if not 1 <= len(targets) <= 20:
            raise ContentProjectGatewayError(
                "douyin_graphic_matrix_targets_invalid",
                "抖音图文矩阵账号数量必须为 1 至 20 个",
            )

        override = dict(bundle.get("platformOverrides", {}).get("抖音") or {})
        common = {
            "title": str(override.get("title") or bundle.get("commonTitle") or "").strip(),
            "body": str(override.get("body") or bundle.get("commonBody") or "").strip(),
            "tags": list(
                override.get("tags")
                if "tags" in override
                else bundle.get("commonTags") or []
            ),
        }
        if not common["title"] or not common["body"]:
            raise ContentProjectGatewayError(
                "douyin_graphic_matrix_content_invalid",
                "内容包必须提供可用于抖音的纯净标题和正文",
            )

        allowed = {"accountId", "title", "body", "tags", "schedule"}
        normalized_targets: list[dict[str, Any]] = []
        seen_accounts: set[int] = set()
        for position, raw_target in enumerate(targets, start=1):
            target = _mapping(
                raw_target,
                "douyin_graphic_matrix_target_invalid",
                "抖音图文账号配置必须是对象",
            )
            if set(target) - allowed:
                raise ContentProjectGatewayError(
                    "douyin_graphic_matrix_target_invalid",
                    "抖音图文账号配置包含不支持字段",
                )
            account_id = target.get("accountId")
            if type(account_id) is not int or account_id <= 0 or account_id in seen_accounts:
                raise ContentProjectGatewayError(
                    "douyin_graphic_matrix_target_invalid",
                    "抖音图文 accountId 必须是唯一正整数",
                )
            seen_accounts.add(account_id)
            tags = target.get("tags")
            if tags is not None and (
                not isinstance(tags, Sequence)
                or isinstance(tags, (str, bytes))
                or not all(isinstance(tag, str) for tag in tags)
            ):
                raise ContentProjectGatewayError(
                    "douyin_graphic_matrix_target_invalid",
                    "抖音图文账号话题必须是字符串列表",
                )
            schedule_time = self._matrix_schedule(target.get("schedule"))
            normalized_targets.append(
                {
                    "itemIndex": position,
                    "accountId": account_id,
                    "overrides": {
                        "title": target.get("title"),
                        "body": target.get("body"),
                        "tags": None if tags is None else list(tags),
                    },
                    "scheduleTime": schedule_time,
                    "scheduleOverridden": bool(schedule_time),
                }
            )

        manifest = Path(str(bundle["sourcePath"]))
        return {
            "schemaVersion": "oneclick-douyin-graphic-matrix/v1",
            "workflow": "douyin-graphic-matrix",
            "runtimeMode": runtime_mode,
            "content": {
                "manifestPath": str(manifest),
                "manifestSha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                "images": list(bundle.get("assetPaths") or []),
                "common": common,
            },
            "targets": normalized_targets,
        }

    def check_douyin_graphic_matrix(
        self,
        manifest_path: str,
        targets: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        self._ensure_platform_work_available()
        return self.matrix_submitter(
            self._douyin_graphic_matrix(
                manifest_path,
                targets,
                runtime_mode="local_check",
            )
        )

    def authorize_douyin_graphic_matrix(self, task_id: int) -> dict[str, Any]:
        if type(task_id) is not int or task_id <= 0:
            raise ContentProjectGatewayError(
                "content_project_task_id_invalid",
                "本地检查 taskId 必须是正整数",
            )
        return self.authorizer(task_id)

    def publish_douyin_graphic_matrix(
        self,
        manifest_path: str,
        targets: Sequence[Mapping[str, Any]],
        *,
        confirmed_check_task_id: int,
        authorization_id: str,
    ) -> dict[str, Any]:
        self._ensure_platform_work_available()
        if (
            type(confirmed_check_task_id) is not int
            or confirmed_check_task_id <= 0
            or not str(authorization_id or "").strip()
        ):
            raise ContentProjectGatewayError(
                "content_project_authorization_required",
                "图文矩阵正式发布必须绑定成功的本地检查和一次性授权",
            )
        matrix = self._douyin_graphic_matrix(
            manifest_path,
            targets,
            runtime_mode="publish",
        )
        matrix.update(
            {
                "confirmedCheckTaskId": confirmed_check_task_id,
                "authorizationId": str(authorization_id).strip(),
            }
        )
        return self.matrix_submitter(matrix)

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
        confirmed_preflight_task_id: int | None = None,
    ) -> dict[str, Any]:
        self._ensure_platform_work_available()
        if confirmed_preflight_task_id is not None:
            if (
                type(confirmed_preflight_task_id) is not int
                or confirmed_preflight_task_id <= 0
            ):
                raise ContentProjectGatewayError(
                    "silicon_evolution_preflight_required",
                    "兼容模式的预检 taskId 无效",
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

        request = self._silicon_evolution_request(
            article_id,
            package_path,
            package_sha256,
            mode="direct",
        )
        grant = self.silicon_direct_authorizer(request)
        authorization_id = str(grant.get("authorizationId") or "").strip()
        if not authorization_id:
            raise ContentProjectGatewayError(
                "silicon_evolution_direct_authorization_failed",
                "本机没有生成可用的硅基进化直发授权",
            )
        request["directAuthorizationId"] = authorization_id
        return self.silicon_submitter(request)
