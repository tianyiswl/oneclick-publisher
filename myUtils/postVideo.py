import asyncio
import json
from datetime import datetime
from pathlib import Path

from conf import BASE_DIR
from PIL import Image
from playwright.async_api import async_playwright
from uploader.douyin_uploader.main import DouYinVideo
from uploader.ks_uploader.main import KSVideo
from uploader.tencent_uploader.main import TencentVideo
from uploader.xiaohongshu_uploader.main import XiaoHongShuVideo
from uploader.bilibili_uploader.playwright_main import BilibiliVideo
from uploader.meta_uploader.main import MetaReelVideo
from uploader.tk_uploader.main import TiktokVideo
from uploader.youtube_uploader.main import YouTubeVideo
from utils.base_social_media import (
    is_publish_background_mode,
    launch_publish_browser,
    new_publish_context,
    reveal_page_window,
    set_init_script,
)
from utils.constant import TencentZoneTypes
from utils.files_times import generate_schedule_time_next_day
from utils.publish_limits import (
    KUAISHOU_TAG_COUNT,
    XIAOHONGSHU_TAG_COUNT,
    get_publish_tag_limit,
    normalize_publish_tags,
)
from utils.publish_observer import publish_context, publish_event, publish_step


def _resolve_video_file_path(file_name):
    return str(Path(BASE_DIR / "videoFile" / file_name)) if file_name else None


def _parse_cover_ratio(target_ratio):
    if not target_ratio:
        return None
    try:
        width, height = (int(part) for part in str(target_ratio).split(":", 1))
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return width / height


def _prepare_cover_upload_path(path, target_ratio=None):
    if not path:
        return None
    source = Path(path)
    ratio = _parse_cover_ratio(target_ratio)
    if source.suffix.lower() in {".jpg", ".jpeg"} and ratio is None:
        return str(source)

    output_dir = Path(BASE_DIR / "videoFile" / ".prepared_covers")
    output_dir.mkdir(parents=True, exist_ok=True)
    ratio_suffix = (
        f"__{str(target_ratio).replace(':', 'x')}_crop_v3"
        if ratio
        else ""
    )
    output = output_dir / f"{source.stem}{ratio_suffix}.jpg"
    if output.exists() and output.stat().st_mtime >= source.stat().st_mtime:
        return str(output)

    with Image.open(source) as image:
        source_image = image.convert("RGB")
        if ratio:
            source_width, source_height = source_image.size
            source_ratio = source_width / source_height
            if source_ratio > ratio:
                target_width = round(source_height * ratio)
                prepared = source_image.crop(
                    (0, 0, target_width, source_height)
                )
            else:
                target_height = round(source_width / ratio)
                excess = source_height - target_height
                top = round(excess * 0.25)
                prepared = source_image.crop(
                    (0, top, source_width, top + target_height)
                )
            prepared.save(output, "JPEG", quality=95)
        else:
            source_image.save(output, "JPEG", quality=95)
    return str(output)


def _resolve_cover_file_path(file_name, prepare=False, target_ratio=None):
    path = _resolve_video_file_path(file_name)
    return (
        _prepare_cover_upload_path(path, target_ratio=target_ratio)
        if prepare and path
        else path
    )


def _resolve_cover_paths(cover_paths, prepare=False):
    if not isinstance(cover_paths, dict):
        return {}
    return {
        str(ratio): _resolve_cover_file_path(
            file_name,
            prepare=prepare,
            target_ratio=str(ratio) if prepare else None,
        )
        for ratio, file_name in cover_paths.items()
        if file_name
    }


def _merge_storage_states(account_files):
    merged_cookies = {}
    merged_origins = {}
    for account_file in account_files:
        storage_path = Path(account_file)
        if not storage_path.is_absolute():
            storage_path = Path(BASE_DIR / "cookiesFile" / storage_path)
        if not storage_path.exists():
            continue
        try:
            state = json.loads(storage_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"read account state failed file={account_file} err={e}")
            continue
        for cookie in state.get("cookies", []):
            cookie_key = (cookie.get("name"), cookie.get("domain"), cookie.get("path"))
            merged_cookies[cookie_key] = cookie
        for origin_state in state.get("origins", []):
            origin = origin_state.get("origin")
            if origin:
                merged_origins[origin] = origin_state
    return {
        "cookies": list(merged_cookies.values()),
        "origins": list(merged_origins.values()),
    }


def _parse_schedule_time(schedule_time):
    if isinstance(schedule_time, datetime):
        return schedule_time
    if not schedule_time:
        return None
    try:
        return datetime.fromisoformat(str(schedule_time).strip().replace("T", " "))
    except ValueError as exc:
        raise ValueError(f"定时发布时间格式不正确：{schedule_time}") from exc


def _option_bool(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _build_publish_datetimes(file_count, enable_timer, videos_per_day, daily_times, start_days, jitter_minutes, schedule_time=None):
    if enable_timer and schedule_time:
        parsed_schedule_time = _parse_schedule_time(schedule_time)
        return [parsed_schedule_time for _ in range(file_count)]
    if enable_timer:
        videos_per_day = int(videos_per_day or 1)
        start_days = int(start_days or 0)
        jitter_minutes = int(jitter_minutes or 0)
        schedules = generate_schedule_time_next_day(
            file_count,
            videos_per_day,
            daily_times,
            start_days=start_days,
            jitter_minutes=jitter_minutes,
        )
        print(
            "[schedule] generated:",
            [schedule.strftime("%Y-%m-%d %H:%M") for schedule in schedules],
            f"videos_per_day={videos_per_day}",
            f"daily_times={daily_times}",
            f"start_days={start_days}",
            f"jitter={jitter_minutes}",
        )
        return schedules
    return [0 for _ in range(file_count)]


def _attach_publish_options(app, data, publish_datetime):
    app.collection_name = str(data.get("collectionName") or "").strip()
    app.short_title = str(data.get("shortTitle") or "").strip()
    app.original_declaration = _option_bool(data.get("originalDeclaration"))
    app.ai_generated = _option_bool(data.get("aiGenerated"))
    app.sync_to_toutiao = _option_bool(data.get("syncToToutiao"))
    app.visibility = str(data.get("visibility") or "public")
    app.publish_date = 0 if _option_bool(data.get("saveDraftOnly")) else publish_datetime
    return app


def _make_platform_app(data, file_path, publish_datetime, cookie_path, dry_run=True):
    platform_type = int(data.get("type"))
    save_draft_only = bool(data.get("saveDraftOnly"))
    title = data.get("title") or data.get("biliTitle") or ""
    all_tags = normalize_publish_tags(data.get("tags"), max_count=1000)
    if platform_type == 4 and len(all_tags) > KUAISHOU_TAG_COUNT:
        raise ValueError(
            f"快手最多允许 {KUAISHOU_TAG_COUNT} 个话题，当前为 {len(all_tags)} 个"
        )
    tags = all_tags[:get_publish_tag_limit(platform_type)]
    category = data.get("category")
    if category == 0:
        category = None
    cover_path = data.get("coverPath")
    cover_paths = data.get("coverPaths") if isinstance(data.get("coverPaths"), dict) else {}

    if platform_type == 1:
        thumb = _resolve_video_file_path(cover_path)
        thumbs = _resolve_cover_paths(cover_paths)
        return _attach_publish_options(XiaoHongShuVideo(
            title,
            file_path,
            tags,
            publish_datetime,
            cookie_path,
            thumbnail_path=thumb,
            thumbnail_paths=thumbs,
            dry_run=dry_run,
            save_draft_only=save_draft_only,
            description=data.get("description"),
        ), data, publish_datetime)
    if platform_type == 2:
        thumb = _resolve_video_file_path(cover_path)
        thumbs = _resolve_cover_paths(cover_paths)
        return _attach_publish_options(TencentVideo(
            title,
            file_path,
            tags,
            publish_datetime,
            cookie_path,
            category,
            thumbnail_path=thumb,
            thumbnail_paths=thumbs,
            dry_run=dry_run,
            save_draft_only=save_draft_only,
            description=data.get("description"),
        ), data, publish_datetime)
    if platform_type == 3:
        thumb = _resolve_cover_file_path(cover_path, prepare=True)
        thumbs = _resolve_cover_paths(cover_paths, prepare=True)
        return _attach_publish_options(DouYinVideo(
            title,
            file_path,
            tags,
            publish_datetime,
            cookie_path,
            category=category,
            thumbnail_path=thumb,
            thumbnail_paths=thumbs,
            dry_run=dry_run,
            save_draft_only=save_draft_only,
            description=data.get("description"),
        ), data, publish_datetime)
    if platform_type == 4:
        thumbs = _resolve_cover_paths(cover_paths)
        thumb = thumbs.get("3:4") or thumbs.get("4:3") or _resolve_video_file_path(cover_path)
        return _attach_publish_options(KSVideo(
            title,
            file_path,
            tags,
            publish_datetime,
            cookie_path,
            thumbnail_path=thumb,
            thumbnail_paths=thumbs,
            dry_run=dry_run,
            save_draft_only=save_draft_only,
            description=data.get("description"),
        ), data, publish_datetime)
    if platform_type == 5:
        thumbs = _resolve_cover_paths(cover_paths, prepare=True)
        bili_cover_path = thumbs.get("4:3") or thumbs.get("16:9") or _resolve_video_file_path(cover_path)
        return _attach_publish_options(BilibiliVideo(
            title,
            file_path,
            tags,
            publish_datetime,
            cookie_path,
            thumbnail_path=bili_cover_path,
            thumbnail_paths=thumbs,
            desc=data.get("biliDesc"),
            bili_type=data.get("biliType"),
            partition=data.get("biliPartition"),
            dry_run=dry_run,
            save_draft_only=save_draft_only,
        ), data, publish_datetime)
    if platform_type == 6:
        thumbs = _resolve_cover_paths(cover_paths)
        thumbnail = thumbs.get("9:16") or thumbs.get("3:4") or _resolve_video_file_path(cover_path)
        return _attach_publish_options(TiktokVideo(
            title,
            file_path,
            tags,
            publish_datetime,
            cookie_path,
            description=data.get("description"),
            thumbnail_path=thumbnail,
            thumbnail_paths=thumbs,
            dry_run=dry_run,
        ), data, publish_datetime)
    if platform_type == 7:
        thumbs = _resolve_cover_paths(cover_paths)
        thumbnail = thumbs.get("4:3") or thumbs.get("16:9") or _resolve_video_file_path(cover_path)
        return _attach_publish_options(YouTubeVideo(
            title,
            file_path,
            tags,
            cookie_path,
            description=data.get("description"),
            thumbnail_path=thumbnail,
            thumbnail_paths=thumbs,
            visibility=data.get("visibility") or "private",
            dry_run=dry_run,
        ), data, publish_datetime)
    if platform_type in (8, 9):
        thumbs = _resolve_cover_paths(cover_paths)
        thumbnail = thumbs.get("9:16") or thumbs.get("3:4") or _resolve_video_file_path(cover_path)
        return _attach_publish_options(MetaReelVideo(
            title,
            file_path,
            tags,
            cookie_path,
            target_platform="instagram" if platform_type == 8 else "facebook",
            description=data.get("description"),
            thumbnail_path=thumbnail,
            thumbnail_paths=thumbs,
            dry_run=dry_run,
            publish_confirmed=_option_bool(
                data.get("metaBrowserPublishConfirmed")
            ),
            automation_acknowledged=_option_bool(
                data.get("metaBrowserAutomationAcknowledged")
            ),
        ), data, publish_datetime)
    raise ValueError(f"unsupported platform: {platform_type}")


def _platform_publish_url(platform_type):
    return {
        1: "https://creator.xiaohongshu.com/publish/publish?from=homepage&target=video",
        2: "https://channels.weixin.qq.com/platform/post/create",
        3: "https://creator.douyin.com/creator-micro/content/post/video?enter_from=publish_page",
        4: "https://cp.kuaishou.com/article/publish/video",
        5: "https://member.bilibili.com/platform/upload/video/frame?page_from=creative_home_top_upload",
        6: "https://www.tiktok.com/tiktokstudio/upload?lang=en",
        7: "https://www.youtube.com/upload",
        8: "https://business.facebook.com/latest/composer/",
        9: "https://business.facebook.com/latest/composer/",
    }.get(platform_type)


def _platform_name(platform_type):
    return {
        1: "小红书",
        2: "视频号",
        3: "抖音",
        4: "快手",
        5: "B站",
        6: "TikTok",
        7: "YouTube",
        8: "Instagram Reels",
        9: "Facebook Reels",
    }.get(platform_type, f"平台{platform_type}")


async def _run_observed_upload(app, playwright, platform_name):
    with publish_step("platform_upload", f"{platform_name}发布流程"):
        await app.upload(playwright)


async def _run_observed_main(app, platform_name):
    with publish_step("platform_upload", f"{platform_name}发布流程"):
        return await app.main()


def _run_observed_platform_main(app, platform_type, file_path, cookie_path, dry_run):
    platform_name = _platform_name(platform_type)
    with publish_context(
        platform_type=platform_type,
        platform_name=platform_name,
        file_path=str(file_path),
        account_file=str(cookie_path),
        dry_run=dry_run,
    ):
        try:
            publish_event("platform_item_start", f"{platform_name}开始处理当前素材")
            result = asyncio.run(_run_observed_main(app, platform_name), debug=False)
        except Exception as e:
            publish_event("platform_item_failed", f"{platform_name}当前素材处理失败：{e}", level="error", error=str(e))
            raise
        else:
            publish_event("platform_item_success", f"{platform_name}当前素材处理完成")
            return result


async def _prepare_batch_pages(context, jobs):
    pages = []
    for job in jobs:
        platform_type = job["platform_type"]
        platform_name = _platform_name(platform_type)
        page = await context.new_page()
        pages.append(page)
        url = _platform_publish_url(platform_type)
        if not url:
            continue
        with publish_context(
            platform_type=platform_type,
            platform_name=platform_name,
            file_path=str(job["file_path"]),
            account_file=str(job["cookie_path"]),
        ):
            publish_event("preload_publish_page", f"正在预打开{platform_name}发布页面", url=url)
            try:
                await page.goto(url, wait_until="commit", timeout=30000)
                await page.wait_for_timeout(300)
                publish_event("preload_publish_page_success", f"{platform_name}发布页面预打开完成", url=url)
            except Exception as e:
                publish_event("preload_publish_page_failed", f"{platform_name}发布页面预打开失败：{e}", level="warning", url=url, error=str(e))
                print(f"[postVideoBatch] preload page failed type={platform_type}: {e}")
    return pages


async def _hold_failed_foreground_draft_session(
    browser,
    pages,
    results,
) -> bool:
    if is_publish_background_mode():
        return False
    failed_indexes = [
        index
        for index, result in enumerate(results)
        if not result.get("ok")
    ]
    if not failed_indexes:
        return False

    failed_index = failed_indexes[0]
    if failed_index >= len(pages):
        return False
    page = pages[failed_index]
    if page.is_closed():
        return False

    await reveal_page_window(page)
    message = (
        "平台草稿保存失败，浏览器已停在失败页面供人工检查；"
        "检查完成后请手动关闭自动化浏览器。"
    )
    publish_event("draft_debug_hold", message, level="warning")
    while browser.is_connected():
        if page.is_closed():
            break
        await asyncio.sleep(1)
    return True


async def _hold_failed_foreground_publish_session(
    browser,
    pages,
    results,
) -> bool:
    """正式发布异常时保留失败页，供用户处理验证或未知提示。"""

    if is_publish_background_mode():
        return False
    failed_indexes = [
        index
        for index, result in enumerate(results)
        if not result.get("ok")
    ]
    if not failed_indexes:
        return False

    failed_index = failed_indexes[0]
    if failed_index >= len(pages):
        return False
    page = pages[failed_index]
    if page.is_closed():
        return False

    await reveal_page_window(page)
    publish_event(
        "publish_intervention_hold",
        "正式发布遇到需要处理的验证或提示，浏览器已停在失败页面；"
        "完成处理后请手动关闭自动化浏览器。",
        level="warning",
    )
    while browser.is_connected():
        if page.is_closed():
            break
        await asyncio.sleep(1)
    return True


async def _post_video_batch_tabs_async(data_list, dry_run=True, save_draft_only=False):
    async with async_playwright() as playwright:
        browser = await launch_publish_browser(playwright)
        jobs = []
        results = []
        context = None
        pages = []

        try:
            for data in data_list:
                platform_type = int(data.get("type"))
                files = [str(Path(BASE_DIR / "videoFile" / file)) for file in data.get("fileList", [])]
                cookies = [Path(BASE_DIR / "cookiesFile" / file) for file in data.get("accountList", [])]
                publish_datetimes = _build_publish_datetimes(
                    len(files),
                    data.get("enableTimer"),
                    data.get("videosPerDay"),
                    data.get("dailyTimes"),
                    data.get("startDays"),
                    data.get("timeJitterMinutes", 0),
                    schedule_time=data.get("scheduleTime"),
                )

                for index, file_path in enumerate(files):
                    for cookie_path in cookies:
                        jobs.append({
                            "data": data,
                            "platform_type": platform_type,
                            "file_path": file_path,
                            "cookie_path": cookie_path,
                            "publish_datetime": publish_datetimes[index],
                        })

            # Keep one browser window with multiple tabs. Put video account states last
            # so their stricter local/origin state wins when origins overlap.
            merged_cookie_paths = [
                job["cookie_path"]
                for job in sorted(jobs, key=lambda item: 1 if item["platform_type"] == 2 else 0)
            ]
            context = await new_publish_context(
                browser,
                storage_state=_merge_storage_states(merged_cookie_paths),
            )
            context = await set_init_script(context)

            pages = await _prepare_batch_pages(context, jobs)
            failed_platforms = set()
            for job, page in zip(jobs, pages):
                platform_type = job["platform_type"]
                platform_name = _platform_name(platform_type)
                with publish_context(
                    platform_type=platform_type,
                    platform_name=platform_name,
                    file_path=str(job["file_path"]),
                    account_file=str(job["cookie_path"]),
                    dry_run=dry_run,
                ):
                    if platform_type in failed_platforms:
                        publish_event("platform_skipped", f"{platform_name}已跳过：同平台前序执行项失败")
                        continue

                    app = _make_platform_app(
                        job["data"],
                        job["file_path"],
                        job["publish_datetime"],
                        job["cookie_path"],
                        dry_run=dry_run,
                    )
                    app.save_draft_only = bool(
                        save_draft_only or job["data"].get("saveDraftOnly")
                    )
                    app.external_page = page
                    app.external_context = context
                    app.external_browser = browser
                    try:
                        publish_event("platform_item_start", f"{platform_name}开始处理当前素材")
                        await _run_observed_upload(app, playwright, platform_name)
                        publish_event("platform_item_success", f"{platform_name}当前素材处理完成")
                        results.append({
                            "type": platform_type,
                            "ok": True,
                            "message": None,
                        })
                    except Exception as e:
                        publish_event("platform_item_failed", f"{platform_name}当前素材处理失败：{e}", level="error", error=str(e))
                        print(f"[postVideoBatch] platform {'dry-run' if dry_run else 'publish'} failed type={platform_type}: {e}")
                        results.append({
                            "type": platform_type,
                            "ok": False,
                            "message": str(e),
                        })
                        failed_platforms.add(platform_type)

            if dry_run and pages:
                if is_publish_background_mode():
                    message = "后台预发布检查完成，浏览器会话将自动关闭。"
                    publish_event("dry_run_ready", message)
                    print(f"[postVideoBatch] {message}")
                else:
                    await reveal_page_window(pages[0])
                    print("[postVideoBatch] dry-run tabs ready, waiting for manual browser close...")
                    while browser.is_connected():
                        if pages and all(page.is_closed() for page in pages):
                            break
                        await asyncio.sleep(1)
            elif save_draft_only:
                held_for_debug = await _hold_failed_foreground_draft_session(
                    browser,
                    pages,
                    results,
                )
                if not held_for_debug:
                    publish_event(
                        "draft_batch_ready",
                        "所选平台草稿保存流程已结束，浏览器会话将自动关闭。",
                    )
            else:
                await _hold_failed_foreground_publish_session(
                    browser,
                    pages,
                    results,
                )
        finally:
            if context:
                try:
                    await context.close()
                except Exception:
                    pass
            try:
                await browser.close()
            except Exception:
                pass
        return results


def post_video_batch_dry_run_tabs(data_list):
    return asyncio.run(_post_video_batch_tabs_async(data_list, dry_run=True), debug=False)


def post_video_batch_tabs(data_list, dry_run=False):
    return asyncio.run(_post_video_batch_tabs_async(data_list, dry_run=dry_run), debug=False)


def post_video_batch_draft_tabs(data_list):
    draft_payloads = []
    for data in data_list:
        payload = dict(data)
        payload["debugDryRun"] = False
        payload["saveDraftOnly"] = True
        payload["runtimeMode"] = "draft"
        draft_payloads.append(payload)
    return asyncio.run(
        _post_video_batch_tabs_async(
            draft_payloads,
            dry_run=False,
            save_draft_only=True,
        ),
        debug=False,
    )


def post_video_tencent(title,files,tags,account_file,category=TencentZoneTypes.LIFESTYLE.value,enableTimer=False,videos_per_day = 1, daily_times=None,start_days = 0, cover_path: str | None = None, cover_paths: dict | None = None, schedule_time=None, jitter_minutes=0, dry_run=False, dry_run_hold_browser=True, save_draft_only=False):
    tags = normalize_publish_tags(tags)
    # 生成文件的完整路径
    account_file = [Path(BASE_DIR / "cookiesFile" / file) for file in account_file]
    files = [Path(BASE_DIR / "videoFile" / file) for file in files]
    publish_datetimes = _build_publish_datetimes(
        len(files), enableTimer, videos_per_day, daily_times, start_days, jitter_minutes, schedule_time
    )
    for index, file in enumerate(files):
        for cookie in account_file:
            print(f"文件路径{str(file)}")
            # 打印视频文件名、标题和 hashtag
            print(f"视频文件名：{file}")
            print(f"标题：{title}")
            print(f"Hashtag：{tags}")
            thumb = _resolve_video_file_path(cover_path)
            thumbs = _resolve_cover_paths(cover_paths)
            app = TencentVideo(title, str(file), tags, publish_datetimes[index], cookie, category,
                               thumbnail_path=thumb, thumbnail_paths=thumbs, dry_run=dry_run,
                               dry_run_hold_browser=dry_run_hold_browser,
                               save_draft_only=save_draft_only)
            _run_observed_platform_main(app, 2, file, cookie, dry_run)


def post_video_DouYin(title,files,tags,account_file,category=TencentZoneTypes.LIFESTYLE.value,enableTimer=False,videos_per_day = 1, daily_times=None,start_days = 0, cover_path: str | None = None, cover_paths: dict | None = None, schedule_time=None, jitter_minutes=0, dry_run=False, dry_run_hold_browser=True, save_draft_only=False):
    tags = normalize_publish_tags(tags)
    # 生成文件的完整路径
    account_file = [Path(BASE_DIR / "cookiesFile" / file) for file in account_file]
    files = [Path(BASE_DIR / "videoFile" / file) for file in files]
    publish_datetimes = _build_publish_datetimes(
        len(files), enableTimer, videos_per_day, daily_times, start_days, jitter_minutes, schedule_time
    )
    for index, file in enumerate(files):
        for cookie in account_file:
            print(f"文件路径{str(file)}")
            # 打印视频文件名、标题和 hashtag
            print(f"视频文件名：{file}")
            print(f"标题：{title}")
            print(f"Hashtag：{tags}")
            thumb = _resolve_cover_file_path(cover_path, prepare=True)
            thumbs = _resolve_cover_paths(cover_paths, prepare=True)
            app = DouYinVideo(title, str(file), tags, publish_datetimes[index], cookie, category=category,
                              thumbnail_path=thumb, thumbnail_paths=thumbs, dry_run=dry_run,
                              dry_run_hold_browser=dry_run_hold_browser,
                              save_draft_only=save_draft_only)
            _run_observed_platform_main(app, 3, file, cookie, dry_run)


def post_video_ks(title,files,tags,account_file,category=TencentZoneTypes.LIFESTYLE.value,enableTimer=False,videos_per_day = 1, daily_times=None,start_days = 0, cover_path: str | None = None, cover_paths: dict | None = None, schedule_time=None, jitter_minutes=0, dry_run=False, dry_run_hold_browser=True, save_draft_only=False):
    tags = normalize_publish_tags(tags, max_count=KUAISHOU_TAG_COUNT)
    # 生成文件的完整路径
    account_file = [Path(BASE_DIR / "cookiesFile" / file) for file in account_file]
    files = [Path(BASE_DIR / "videoFile" / file) for file in files]
    publish_datetimes = _build_publish_datetimes(
        len(files), enableTimer, videos_per_day, daily_times, start_days, jitter_minutes, schedule_time
    )
    for index, file in enumerate(files):
        for cookie in account_file:
            print(f"文件路径{str(file)}")
            # 打印视频文件名、标题和 hashtag
            print(f"视频文件名：{file}")
            print(f"标题：{title}")
            print(f"Hashtag：{tags}")
            thumbs = _resolve_cover_paths(cover_paths)
            thumb = thumbs.get("3:4") or thumbs.get("4:3") or _resolve_video_file_path(cover_path)
            app = KSVideo(
                title,
                str(file),
                tags,
                publish_datetimes[index],
                cookie,
                thumbnail_path=thumb,
                thumbnail_paths=thumbs,
                dry_run=dry_run,
                dry_run_hold_browser=dry_run_hold_browser,
                save_draft_only=save_draft_only,
            )
            _run_observed_platform_main(app, 4, file, cookie, dry_run)

def post_video_xhs(title,files,tags,account_file,category=TencentZoneTypes.LIFESTYLE.value,enableTimer=False,videos_per_day = 1, daily_times=None,start_days = 0, cover_path: str | None = None, cover_paths: dict | None = None, schedule_time=None, jitter_minutes=0, dry_run=False, dry_run_hold_browser=True, save_draft_only=False):
    tags = normalize_publish_tags(tags, max_count=XIAOHONGSHU_TAG_COUNT)
    # 生成文件的完整路径
    account_file = [Path(BASE_DIR / "cookiesFile" / file) for file in account_file]
    files = [Path(BASE_DIR / "videoFile" / file) for file in files]
    file_num = len(files)
    publish_datetimes = _build_publish_datetimes(
        file_num, enableTimer, videos_per_day, daily_times, start_days, jitter_minutes, schedule_time
    )
    for index, file in enumerate(files):
        for cookie in account_file:
            # 打印视频文件名、标题和 hashtag
            print(f"视频文件名：{file}")
            print(f"标题：{title}")
            print(f"Hashtag：{tags}")
            thumb = _resolve_video_file_path(cover_path)
            thumbs = _resolve_cover_paths(cover_paths)
            app = XiaoHongShuVideo(title, file, tags, publish_datetimes[index], cookie, thumbnail_path=thumb, thumbnail_paths=thumbs, dry_run=dry_run, dry_run_hold_browser=dry_run_hold_browser, save_draft_only=save_draft_only)
            _run_observed_platform_main(app, 1, file, cookie, dry_run)


def post_video_bilibili(title, files, tags, account_file, category=None, enableTimer=False, videos_per_day=1, daily_times=None, start_days=0,
                        desc: str | None = None, bili_type: str | None = None, bili_partition: str | None = None,
                        cover_path: str | None = None, cover_paths: dict | None = None,
                        schedule_time: str | None = None, jitter_minutes=0, dry_run=False, dry_run_hold_browser=True,
                        save_draft_only=False):
    tags = normalize_publish_tags(tags)
    account_file = [Path(BASE_DIR / "cookiesFile" / file) for file in account_file]
    files = [Path(BASE_DIR / "videoFile" / file) for file in files]
    publish_datetimes = _build_publish_datetimes(
        len(files), enableTimer, videos_per_day, daily_times, start_days, jitter_minutes, schedule_time
    )
    for index, file in enumerate(files):
        for cookie in account_file:
            print(f"文件路径{str(file)}")
            print(f"标题：{title}")
            print(f"Hashtag：{tags}")
            thumbs = _resolve_cover_paths(cover_paths)
            bili_cover_path = thumbs.get("4:3") or thumbs.get("16:9") or _resolve_video_file_path(cover_path)
            app = BilibiliVideo(title, str(file), tags, publish_datetimes[index], cookie,
                                thumbnail_path=bili_cover_path, thumbnail_paths=thumbs,
                                desc=desc, bili_type=bili_type, partition=bili_partition, dry_run=dry_run,
                                dry_run_hold_browser=dry_run_hold_browser,
                                save_draft_only=save_draft_only)
            _run_observed_platform_main(app, 5, file, cookie, dry_run)


def _post_video_overseas(
    platform_type,
    title,
    files,
    tags,
    account_file,
    enable_timer=False,
    videos_per_day=1,
    daily_times=None,
    start_days=0,
    *,
    description=None,
    cover_path=None,
    cover_paths=None,
    schedule_time=None,
    jitter_minutes=0,
    dry_run=True,
    dry_run_hold_browser=True,
    publish_confirmed=False,
    automation_acknowledged=False,
):
    tags = normalize_publish_tags(tags, max_count=get_publish_tag_limit(platform_type))
    account_files = [Path(BASE_DIR / "cookiesFile" / file) for file in account_file]
    video_files = [Path(BASE_DIR / "videoFile" / file) for file in files]
    publish_datetimes = _build_publish_datetimes(
        len(video_files),
        enable_timer,
        videos_per_day,
        daily_times,
        start_days,
        jitter_minutes,
        schedule_time,
    )
    data = {
        "type": platform_type,
        "title": title,
        "description": description or "",
        "tags": tags,
        "coverPath": cover_path,
        "coverPaths": cover_paths or {},
        "visibility": "private",
        "metaBrowserPublishConfirmed": publish_confirmed,
        "metaBrowserAutomationAcknowledged": automation_acknowledged,
    }
    results = []
    for index, file in enumerate(video_files):
        for cookie in account_files:
            app = _make_platform_app(data, str(file), publish_datetimes[index], cookie, dry_run=dry_run)
            app.dry_run_hold_browser = dry_run_hold_browser
            results.append(
                _run_observed_platform_main(
                    app,
                    platform_type,
                    file,
                    cookie,
                    dry_run,
                )
            )
    return results


def post_video_tiktok(title, files, tags, account_file, category=None, enableTimer=False, videos_per_day=1, daily_times=None, start_days=0, description=None, cover_path=None, cover_paths=None, schedule_time=None, jitter_minutes=0, dry_run=True, dry_run_hold_browser=True):
    return _post_video_overseas(6, title, files, tags, account_file, enableTimer, videos_per_day, daily_times, start_days, description=description, cover_path=cover_path, cover_paths=cover_paths, schedule_time=schedule_time, jitter_minutes=jitter_minutes, dry_run=dry_run, dry_run_hold_browser=dry_run_hold_browser)


def post_video_youtube(title, files, tags, account_file, category=None, enableTimer=False, videos_per_day=1, daily_times=None, start_days=0, description=None, cover_path=None, cover_paths=None, schedule_time=None, jitter_minutes=0, dry_run=True, dry_run_hold_browser=True):
    return _post_video_overseas(7, title, files, tags, account_file, enableTimer, videos_per_day, daily_times, start_days, description=description, cover_path=cover_path, cover_paths=cover_paths, schedule_time=schedule_time, jitter_minutes=jitter_minutes, dry_run=dry_run, dry_run_hold_browser=dry_run_hold_browser)


def post_video_instagram(title, files, tags, account_file, category=None, enableTimer=False, videos_per_day=1, daily_times=None, start_days=0, description=None, cover_path=None, cover_paths=None, schedule_time=None, jitter_minutes=0, dry_run=True, dry_run_hold_browser=True, publish_confirmed=False, automation_acknowledged=False):
    return _post_video_overseas(8, title, files, tags, account_file, enableTimer, videos_per_day, daily_times, start_days, description=description, cover_path=cover_path, cover_paths=cover_paths, schedule_time=schedule_time, jitter_minutes=jitter_minutes, dry_run=dry_run, dry_run_hold_browser=dry_run_hold_browser, publish_confirmed=publish_confirmed, automation_acknowledged=automation_acknowledged)


def post_video_facebook(title, files, tags, account_file, category=None, enableTimer=False, videos_per_day=1, daily_times=None, start_days=0, description=None, cover_path=None, cover_paths=None, schedule_time=None, jitter_minutes=0, dry_run=True, dry_run_hold_browser=True, publish_confirmed=False, automation_acknowledged=False):
    return _post_video_overseas(9, title, files, tags, account_file, enableTimer, videos_per_day, daily_times, start_days, description=description, cover_path=cover_path, cover_paths=cover_paths, schedule_time=schedule_time, jitter_minutes=jitter_minutes, dry_run=dry_run, dry_run_hold_browser=dry_run_hold_browser, publish_confirmed=publish_confirmed, automation_acknowledged=automation_acknowledged)



# post_video("333",["demo.mp4"],"d","d")
# post_video_DouYin("333",["demo.mp4"],"d","d")
