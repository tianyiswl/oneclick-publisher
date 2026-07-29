"""一键发账号授权入口，只委派给一键发自身的官方页面执行器。"""

from .oneclick_authorization import start_authorization


def start_login(
    platform_type: int,
    profile_name: str,
    *,
    update_mode: bool = False,
    record_id: int | None = None,
    background_mode: bool = False,
):
    """用户显式点击后打开官方授权页；不支持隐藏登录，避免误导性后台操作。"""

    del background_mode
    return start_authorization(
        platform_type,
        profile_name,
        update_mode=update_mode,
        record_id=record_id,
    )
