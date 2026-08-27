# -*- coding: utf-8 -*-
"""一键发本机 MCP 适配器。

仅使用 stdio，不监听网络端口；不接受 Cookie、密码或验证码参数。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from mcp.server import MCPServer

from .content_project_gateway import ContentProjectGateway


def _error(exc: Exception) -> dict[str, Any]:
    return {
        "ok": False,
        "errorCode": str(getattr(exc, "error_code", "oneclick_mcp_internal_error")),
        "errorText": str(getattr(exc, "public_message", f"{type(exc).__name__}：{exc}")),
    }


def _call(key: str, operation) -> dict[str, Any]:
    try:
        return {"ok": True, key: operation()}
    except Exception as exc:
        return _error(exc)


def create_server(gateway: ContentProjectGateway | None = None) -> MCPServer:
    gateway = gateway or ContentProjectGateway()
    server = MCPServer(
        name="yijianfa-local",
        title="一键发本机受控发布",
        description="让本机内容项目通过同一发布服务执行后台直发、手动诊断预检和任务查询。",
        instructions=(
            "用户对当次内容、账号和排期明确说‘发布’后，"
            "默认调用 oneclick_direct_publish_content；该工具在本机内部创建并立即消费一次性授权。"
            "oneclick_preflight_content 只用于用户明确要求的平台诊断，不是默认发布前置步骤。"
            "扫码、验证码或未知平台弹窗由任务状态返回给用户，不得传入本工具。"
        ),
        version="1",
    )

    @server.tool(
        name="oneclick_list_accounts",
        description="只读列出本机一键发可选账号；返回值不含登录会话路径或凭据。",
        structured_output=True,
    )
    def list_accounts() -> dict[str, Any]:
        return _call("accounts", gateway.list_accounts)

    @server.tool(
        name="oneclick_list_publish_profiles",
        description="只读列出内容项目与平台账号的本机映射。",
        structured_output=True,
    )
    def list_publish_profiles() -> dict[str, Any]:
        return _call("profiles", gateway.list_profiles)

    @server.tool(
        name="oneclick_save_publish_profile",
        description="在本机保存内容项目到明确平台账号 ID 的映射；不会上传或发布。",
        structured_output=True,
    )
    def save_publish_profile(
        project_id: str,
        display_name: str,
        targets: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        return _call(
            "profile",
            lambda: gateway.save_profile(project_id, display_name, targets),
        )

    @server.tool(
        name="oneclick_preflight_content",
        description="对内容包和项目已配置账号执行预检；不做正式提交。",
        structured_output=True,
    )
    def preflight_content(
        project_id: str,
        manifest_path: str,
        schedules: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return _call(
            "task",
            lambda: gateway.preflight_content(project_id, manifest_path, schedules),
        )

    @server.tool(
        name="oneclick_direct_publish_content",
        description=(
            "仅在用户已对当次内容、账号和排期明确确认发布后调用；"
            "后台只运行一次正式浏览器会话，不先创建独立平台预检。"
        ),
        structured_output=True,
    )
    def direct_publish_content(
        project_id: str,
        manifest_path: str,
        schedules: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return _call(
            "task",
            lambda: gateway.direct_publish_content(
                project_id,
                manifest_path,
                schedules,
            ),
        )

    @server.tool(
        name="oneclick_task_status",
        description="按 taskId 只读查询阶段、平台状态、错误和回执。",
        structured_output=True,
    )
    def task_status(task_id: int) -> dict[str, Any]:
        return _call("task", lambda: gateway.task_status(task_id))

    @server.tool(
        name="oneclick_authorize_preflight",
        description=(
            "只能在用户已明确确认本次正式发布后调用；"
            "为全部成功的预检生成短期、一次性授权，本工具本身不提交平台。"
        ),
        structured_output=True,
    )
    def authorize_preflight(task_id: int) -> dict[str, Any]:
        return _call("authorization", lambda: gateway.authorize_preflight(task_id))

    @server.tool(
        name="oneclick_formal_publish",
        description=(
            "使用已全部成功的预检 taskId 和对应一次性授权正式提交；"
            "不允许绕过用户确认。"
        ),
        structured_output=True,
    )
    def formal_publish(
        project_id: str,
        manifest_path: str,
        confirmed_preflight_task_id: int,
        authorization_id: str,
        schedules: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return _call(
            "task",
            lambda: gateway.formal_publish(
                project_id,
                manifest_path,
                confirmed_preflight_task_id=confirmed_preflight_task_id,
                authorization_id=authorization_id,
                schedules=schedules,
            ),
        )

    @server.tool(
        name="oneclick_check_douyin_graphic_matrix",
        description=(
            "读取结构化图文内容包并逐账号执行本地字段检查；"
            "不会打开抖音，也不会提交平台。"
        ),
        structured_output=True,
    )
    def check_douyin_graphic_matrix(
        manifest_path: str,
        targets: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        return _call(
            "task",
            lambda: gateway.check_douyin_graphic_matrix(manifest_path, targets),
        )

    @server.tool(
        name="oneclick_authorize_douyin_graphic_matrix",
        description=(
            "只能在用户明确确认当次正式发布后调用；"
            "为全部成功的图文矩阵本地检查创建短期一次性授权。"
        ),
        structured_output=True,
    )
    def authorize_douyin_graphic_matrix(task_id: int) -> dict[str, Any]:
        return _call(
            "authorization",
            lambda: gateway.authorize_douyin_graphic_matrix(task_id),
        )

    @server.tool(
        name="oneclick_publish_douyin_graphic_matrix",
        description=(
            "按本地检查中的相同内容、账号顺序和排期执行抖音图文矩阵正式发布；"
            "必须携带对应的一次性授权。"
        ),
        structured_output=True,
    )
    def publish_douyin_graphic_matrix(
        manifest_path: str,
        targets: Sequence[Mapping[str, Any]],
        confirmed_check_task_id: int,
        authorization_id: str,
    ) -> dict[str, Any]:
        return _call(
            "task",
            lambda: gateway.publish_douyin_graphic_matrix(
                manifest_path,
                targets,
                confirmed_check_task_id=confirmed_check_task_id,
                authorization_id=authorization_id,
            ),
        )

    @server.tool(
        name="oneclick_sync_project_metrics",
        description=(
            "按内容项目已配置账号执行一次只读数据同步；"
            "同一账号当天成功后复用，近期失败处于冷却时不会重复访问平台。"
        ),
        structured_output=True,
    )
    def sync_project_metrics(project_id: str) -> dict[str, Any]:
        return _call(
            "sync", lambda: gateway.sync_project_metrics(project_id)
        )

    @server.tool(
        name="oneclick_get_project_metrics",
        description="读取项目账号汇总和该项目正式发布作品的数据；不会访问平台。",
        structured_output=True,
    )
    def get_project_metrics(
        project_id: str, days: int = 1
    ) -> dict[str, Any]:
        return _call(
            "metrics", lambda: gateway.get_project_metrics(project_id, days)
        )

    @server.tool(
        name="oneclick_metrics_sync_status",
        description="只读查询项目账号最近同步、当日复用和失败冷却状态。",
        structured_output=True,
    )
    def metrics_sync_status(project_id: str) -> dict[str, Any]:
        return _call(
            "status", lambda: gateway.metrics_sync_status(project_id)
        )

    @server.tool(
        name="oneclick_preflight_silicon_evolution_release",
        description="重验硅基进化 V1.2 冻结包并执行公众号预检；不会发表。",
        structured_output=True,
    )
    def preflight_silicon_evolution_release(
        article_id: str,
        package_path: str,
        package_sha256: str,
    ) -> dict[str, Any]:
        return _call(
            "task",
            lambda: gateway.preflight_silicon_evolution_release(
                article_id,
                package_path,
                package_sha256,
            ),
        )

    @server.tool(
        name="oneclick_auto_publish_silicon_evolution_release",
        description=(
            "用户明确确认后，对硅基进化已冻结文章执行一次后台正式提交；"
            "默认不要求独立平台预检，传入 confirmed_preflight_task_id 时仅作旧流程兼容。"
        ),
        structured_output=True,
    )
    def auto_publish_silicon_evolution_release(
        article_id: str,
        package_path: str,
        package_sha256: str,
        confirmed_preflight_task_id: int | None = None,
    ) -> dict[str, Any]:
        return _call(
            "task",
            lambda: gateway.auto_publish_silicon_evolution_release(
                article_id,
                package_path,
                package_sha256,
                confirmed_preflight_task_id=confirmed_preflight_task_id,
            ),
        )

    return server


def run_stdio_server() -> None:
    create_server().run(transport="stdio")
