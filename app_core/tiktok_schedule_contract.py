from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


SHANGHAI_NAME = "Asia/Shanghai"
SHANGHAI = ZoneInfo(SHANGHAI_NAME)
MAXIMUM_LEAD = timedelta(days=10)


@dataclass(frozen=True, slots=True)
class TikTokScheduleIntent:
    mode: str
    local_time: str | None
    timezone: str
    scheduled_at: datetime | None


class TikTokScheduleContractError(RuntimeError):
    def __init__(self, error_code: str, public_message: str) -> None:
        self.error_code = str(error_code)
        self.public_message = str(public_message)
        super().__init__(public_message)


def validate_tiktok_schedule_window(
    intent: TikTokScheduleIntent,
    *,
    now: datetime,
    minimum_lead: timedelta,
) -> None:
    if intent.mode == "immediate":
        return
    if now.tzinfo is None or intent.scheduled_at is None:
        raise TikTokScheduleContractError(
            "tiktok_schedule_invalid",
            "TikTok 排期和校验时钟必须包含北京时间",
        )
    delta = intent.scheduled_at - now.astimezone(SHANGHAI)
    if delta < minimum_lead or delta > MAXIMUM_LEAD:
        raise TikTokScheduleContractError(
            "tiktok_schedule_out_of_range",
            "TikTok 排期不在允许的时间窗口内",
        )


def parse_tiktok_schedule_fields(
    *,
    enable_timer: object,
    schedule_time: object,
    schedule_timezone: object,
    daily_times: object,
) -> TikTokScheduleIntent:
    if type(enable_timer) is not bool:
        raise TikTokScheduleContractError(
            "tiktok_schedule_invalid",
            "TikTok 排期开关类型无效",
        )
    if schedule_timezone != SHANGHAI_NAME or type(daily_times) is not list:
        raise TikTokScheduleContractError(
            "tiktok_schedule_invalid",
            "TikTok 排期时区或时间列表无效",
        )
    if not enable_timer:
        if schedule_time is not None or daily_times != []:
            raise TikTokScheduleContractError(
                "tiktok_schedule_invalid",
                "TikTok 立即发布与排期字段冲突",
            )
        return TikTokScheduleIntent("immediate", None, SHANGHAI_NAME, None)
    if type(schedule_time) is not str:
        raise TikTokScheduleContractError(
            "tiktok_schedule_invalid",
            "TikTok 排期时间格式无效",
        )
    try:
        parsed = datetime.strptime(schedule_time, "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise TikTokScheduleContractError(
            "tiktok_schedule_invalid",
            "TikTok 排期时间格式无效",
        ) from exc
    if parsed.strftime("%Y-%m-%d %H:%M") != schedule_time:
        raise TikTokScheduleContractError(
            "tiktok_schedule_invalid",
            "TikTok 排期时间格式无效",
        )
    local_time = schedule_time
    if daily_times != [local_time[-5:]]:
        raise TikTokScheduleContractError(
            "tiktok_schedule_invalid",
            "TikTok 排期时间列表与目标时间不一致",
        )
    return TikTokScheduleIntent(
        "platform_native",
        local_time,
        SHANGHAI_NAME,
        parsed.replace(tzinfo=SHANGHAI),
    )
