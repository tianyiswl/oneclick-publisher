# -*- coding: utf-8 -*-
"""抖音带货视频的收藏音乐选择与页面回读。

这里不调用抖音私有接口，也不读取会话、二维码或原始请求。音乐只能从一键发
受控浏览器中当前可见的“选择音乐 → 收藏”列表读取；如果页面结构、收藏标签、
首条音乐或选择后的回读不能唯一确认，就停止执行，而不是猜测点击。
"""

from __future__ import annotations

import asyncio
from hashlib import sha256
import re
from typing import Any, Mapping


FAVORITE_FIRST_MUSIC_MODE = "favorite-first"
# ``favorite-first`` 只为兼容已经生成的旧任务保留。抖音带货的新桌面
# 向导使用 ``favorite-manual``：用户从当前账号实际可见的收藏列表中亲自
# 选择一首，再由同一编辑会话回读确认。它不是一个“按名称搜索”的自由文本
# 参数，因此不会把任意歌曲名或过期列表项带进发布流程。
FAVORITE_MANUAL_MUSIC_MODE = "favorite-manual"
_NON_MUSIC_LABELS = {"选择音乐", "添加音乐", "收藏", "加载中"}
_MUSIC_DURATION = re.compile(r"^\d{1,2}:\d{2}$")
# 抖音音乐抽屉的“收藏”页签通常会先显示空容器，再异步填充卡片。不能以
# 固定 350ms 作为“收藏为空”的依据；同时也不能无限等待而卡住客户端。
_FAVORITE_MUSIC_ROW_WAIT_ATTEMPTS = 40
_FAVORITE_MUSIC_ROW_WAIT_SECONDS = 0.2


class DouyinMusicError(RuntimeError):
    """抖音音乐控件不能安全继续时抛出。"""


def _normalized(value: object) -> str:
    return " ".join(str(value or "").replace("\u200b", " ").split())


def validate_favorite_music_mode(value: object) -> str:
    """校验收藏音乐策略。

    旧任务可继续使用 ``favorite-first``；新的带货向导必须使用
    ``favorite-manual``，由用户在当前受控编辑会话中选择。两者之外的推荐、
    搜索或手写音乐名都不接受，避免猜测平台音乐控件。
    """

    mode = _normalized(value)
    if mode not in {FAVORITE_FIRST_MUSIC_MODE, FAVORITE_MANUAL_MUSIC_MODE}:
        raise DouyinMusicError("抖音带货音乐仅支持从收藏列表中选择")
    return mode


def normalize_music_readback(value: object) -> dict[str, str] | None:
    """收敛为任务可保存的最小音乐回读，不保留页面 HTML 或请求数据。"""

    if not isinstance(value, Mapping):
        return None
    title = _normalized(value.get("title") or value.get("name"))
    creator = _normalized(value.get("creator") or value.get("author") or value.get("artist"))
    duration = _normalized(value.get("duration"))
    music_id = _normalized(value.get("musicId") or value.get("music_id") or value.get("id"))
    # 异步抽屉加载时，容器标题“选择音乐”偶尔会与列表行一起被扫描到。
    # 它不是可选音乐，宁可停止也不能把界面标题写入发布任务。
    if (
        not title
        or title in _NON_MUSIC_LABELS
        or not creator
        or not _MUSIC_DURATION.fullmatch(duration)
        or duration == "00:00"
    ):
        return None
    # 有些新版列表没有暴露 data-music-id。此时只有标题、作者和时长三项在
    # 当前收藏列表内唯一时才接受，避免同名音乐被误认为同一条。
    identity = music_id or f"visible:{title}|{creator}|{duration}"
    return {
        "musicId": identity,
        "title": title,
        "creator": creator,
        "duration": duration,
        "source": "douyin-favorite-visible",
    }


def find_favorite_music_by_id(
    candidates: object,
    music_id: object,
) -> dict[str, str]:
    """从本次已打开的收藏抽屉中精确找回一条可点击音乐。

    本地缓存优先保存稳定的平台音乐 ID；新版页面未暴露该 ID 时，缓存保存
    歌曲名、作者、时长生成的稳定指纹。真正写入前仍必须在当前编辑会话的
    收藏抽屉中精确匹配同一 ID 或完整三字段唯一候选，拿到瞬态 ``marker``
    后才允许点击。缺失、过期或歧义项都不能退化为模糊匹配或“第一首”。
    """

    expected = _normalized(music_id)
    if not expected or expected.startswith(("visible:", "favorite-index:")):
        raise DouyinMusicError("抖音收藏音乐缺少可复用的平台身份，无法安全选择")
    if not isinstance(candidates, list):
        raise DouyinMusicError("抖音收藏音乐当前候选不可用，请刷新后重新选择")
    if expected.startswith("metadata:"):
        def metadata_identity(item: Mapping[str, str]) -> str:
            material = "\x1f".join(
                _normalized(item.get(key))
                for key in ("title", "creator", "duration")
            )
            return f"metadata:{sha256(material.encode('utf-8')).hexdigest()}"

        matches = [
            dict(item)
            for item in candidates
            if isinstance(item, Mapping) and metadata_identity(item) == expected
        ]
    else:
        matches = [
            dict(item)
            for item in candidates
            if isinstance(item, Mapping)
            and _normalized(item.get("musicId")) == expected
        ]
    if len(matches) != 1:
        raise DouyinMusicError("抖音收藏音乐缓存项在当前收藏列表中不唯一或已失效，请刷新后重新选择")
    return matches[0]


async def _unique_visible_text_control(page, label: str, purpose: str):
    """按实际视口去重定位一个精确文案控件。"""

    await page.evaluate(
        """() => document.querySelectorAll('[data-oneclick-music-control]')
            .forEach(node => node.removeAttribute('data-oneclick-music-control'))"""
    )
    locator = page.get_by_text(label, exact=True)
    controls: list[Any] = []
    identities: set[str] = set()
    for index in range(await locator.count()):
        node = locator.nth(index)
        try:
            if not await node.is_visible() or not await node.is_enabled():
                continue
            identity = await node.evaluate(
                """(node, expectedLabel) => {
                    const normalize = value => String(value || '')
                        .replace(/[\\u200b\\u00a0]/g, ' ')
                        .replace(/\\s+/g, ' ')
                        .trim();
                    const visible = element => {
                        if (!(element instanceof HTMLElement)) return false;
                        const rect = element.getBoundingClientRect();
                        const style = getComputedStyle(element);
                        return rect.width > 0 && rect.height > 0
                            && style.display !== 'none' && style.visibility !== 'hidden';
                    };
                    let root = node.closest(
                        'button,[role="button"],[role="tab"],label,a'
                    );
                    // 新版“添加音乐”是无 role 的 DIV：文字和外层按钮都
                    // 命中精确文案。选择文本一致且面积最大的祖先，令两次
                    // 命中收敛到同一个可点击容器。
                    if (!root) {
                        const candidates = [];
                        let current = node;
                        while (current && current !== document.body) {
                            if (visible(current)
                                && normalize(current.innerText || current.textContent) === expectedLabel) {
                                const rect = current.getBoundingClientRect();
                                if (rect.width >= 48 && rect.height >= 28) {
                                    candidates.push({ current, area: rect.width * rect.height });
                                }
                            }
                            current = current.parentElement;
                        }
                        candidates.sort((left, right) => right.area - left.area);
                        root = candidates[0]?.current || node;
                    }
                    const rect = root.getBoundingClientRect();
                    const key = `${root.tagName}|${root.className}|${Math.round(rect.x)}|${Math.round(rect.y)}`;
                    return key;
                }""",
                label,
            )
        except Exception:
            continue
        key = _normalized(identity)
        if not key or key in identities:
            continue
        identities.add(key)
        controls.append(node)
    if len(controls) != 1:
        raise DouyinMusicError(
            f"抖音页面未找到唯一可用的{purpose}控件（实际 {len(controls)} 个），已安全停止"
        )
    # 文案本身常是 span；真正接收事件的是外层按钮/role=button。只在精确
    # 唯一后标记该外层根节点，避免“文字点击成功但没有打开选择器”。
    try:
        await controls[0].evaluate(
            """(node, expectedLabel) => {
                const normalize = value => String(value || '')
                    .replace(/[\\u200b\\u00a0]/g, ' ')
                    .replace(/\\s+/g, ' ')
                    .trim();
                const visible = element => {
                    if (!(element instanceof HTMLElement)) return false;
                    const rect = element.getBoundingClientRect();
                    const style = getComputedStyle(element);
                    return rect.width > 0 && rect.height > 0
                        && style.display !== 'none' && style.visibility !== 'hidden';
                };
                let root = node.closest('button,[role="button"],[role="tab"],label,a');
                if (!root) {
                    const candidates = [];
                    let current = node;
                    while (current && current !== document.body) {
                        if (visible(current)
                            && normalize(current.innerText || current.textContent) === expectedLabel) {
                            const rect = current.getBoundingClientRect();
                            if (rect.width >= 48 && rect.height >= 28) {
                                candidates.push({ current, area: rect.width * rect.height });
                            }
                        }
                        current = current.parentElement;
                    }
                    candidates.sort((left, right) => right.area - left.area);
                    root = candidates[0]?.current || node;
                }
                root.dataset.oneclickMusicControl = 'active';
            }""",
            label,
        )
    except Exception as exc:
        raise DouyinMusicError(f"抖音{purpose}控件无法定位实际可点击根节点") from exc
    return page.locator('[data-oneclick-music-control="active"]')


async def _wait_for_music_dialog(page):
    """等待唯一可见的“选择音乐”容器；兼容新版抽屉/浮层。

    不能假设抖音始终使用 ``modal``：当前新版会把音乐列表渲染为抽屉或
    portal。先识别标准 dialog/modal，再从精确“选择音乐”标题向上收敛到
    最小的可见浮层容器；始终要求唯一，避免误把整个编辑页当成音乐列表。
    """

    for _ in range(30):
        result = await page.evaluate(
            """() => {
                const normalize = value => String(value || '')
                    .replace(/[\\u200b\\u00a0]/g, ' ')
                    .replace(/\\s+/g, ' ')
                    .trim();
                const visible = node => {
                    if (!(node instanceof HTMLElement)) return false;
                    const rect = node.getBoundingClientRect();
                    const style = getComputedStyle(node);
                    return rect.width > 0 && rect.height > 0
                        && style.display !== 'none' && style.visibility !== 'hidden';
                };
                const visibleMusicOverlay = node => {
                    if (!visible(node) || !normalize(node.innerText).includes('选择音乐')) return false;
                    const rect = node.getBoundingClientRect();
                    return rect.width >= 300 && rect.height >= 180;
                };
                const roots = new Set(Array.from(document.querySelectorAll(
                    '[role="dialog"], [class*="modal"], [class*="Modal"], '
                    + '[class*="drawer"], [class*="Drawer"], [class*="sheet"], [class*="Sheet"]'
                )).filter(visibleMusicOverlay));
                const titleNodes = Array.from(document.querySelectorAll('*')).filter(node =>
                    visible(node) && normalize(node.innerText || node.textContent) === '选择音乐'
                );
                for (const title of titleNodes) {
                    let current = title.parentElement;
                    while (current && current !== document.body) {
                        if (visible(current)) {
                            const rect = current.getBoundingClientRect();
                            const style = getComputedStyle(current);
                            const className = String(current.className || '');
                            const isOverlay = /modal|drawer|sheet|dialog|popup|portal/i.test(className)
                                || ['fixed', 'absolute', 'sticky'].includes(style.position)
                                || (style.zIndex && style.zIndex !== 'auto');
                            if (isOverlay && rect.width >= 300 && rect.height >= 180
                                && normalize(current.innerText || current.textContent).includes('选择音乐')) {
                                roots.add(current);
                                break;
                            }
                        }
                        current = current.parentElement;
                    }
                }
                const candidates = Array.from(roots);
                // music-side-sheet-header-tab 也带 "sheet" class，但它只有
                // 页签标题，不含收藏列表。选择最外层受控容器，而非最内层。
                const outermost = candidates.filter(node => !candidates.some(
                    other => other !== node && other.contains(node)
                ));
                if (outermost.length !== 1) {
                    const diagnostic = outermost.slice(0, 4).map(node => {
                        const rect = node.getBoundingClientRect();
                        return `${node.tagName}.${String(node.className || '').slice(0, 80)}@${Math.round(rect.width)}x${Math.round(rect.height)}`;
                    });
                    return { count: outermost.length, marker: '', diagnostic };
                }
                const dialog = outermost[0];
                dialog.dataset.oneclickMusicDialog = 'active';
                return { count: 1, marker: 'active' };
            }"""
        )
        if isinstance(result, Mapping) and int(result.get("count") or 0) == 1:
            return page.locator('[data-oneclick-music-dialog="active"]')
        await page.wait_for_timeout(200)
    diagnostic = ""
    if isinstance(result, Mapping):
        candidates = [
            _normalized(value)
            for value in result.get("diagnostic") or []
            if _normalized(value)
        ]
        if candidates:
            diagnostic = f"（可见候选：{'；'.join(candidates)}）"
    raise DouyinMusicError(
        f"抖音“选择音乐”容器未能唯一显示{diagnostic}，已安全停止"
    )


async def _music_picker_page(page):
    """确认“添加音乐”是否在当前页或唯一的新页打开选择器。

    新版创作中心有时会把音乐选择器放到同一页面的 portal，也有时会新开一个
    受控标签页。这里只按页面可见文案“选择音乐”识别，不把其它后台页当作
    音乐页，也不依据 URL 或私有接口猜测。
    """

    last_page_count = 0
    for _ in range(30):
        candidates = []
        pages = [item for item in page.context.pages if not item.is_closed()]
        last_page_count = len(pages)
        for candidate in pages:
            try:
                has_picker = await candidate.evaluate(
                    """() => {
                        const visible = node => {
                            if (!(node instanceof HTMLElement)) return false;
                            const rect = node.getBoundingClientRect();
                            const style = getComputedStyle(node);
                            return rect.width > 0 && rect.height > 0
                                && style.display !== 'none' && style.visibility !== 'hidden';
                        };
                        return Array.from(document.querySelectorAll('*')).some(node =>
                            visible(node)
                            && String(node.innerText || node.textContent || '')
                                .replace(/[\\u200b\\u00a0]/g, ' ')
                                .replace(/\\s+/g, ' ')
                                .trim() === '选择音乐'
                        );
                    }"""
                )
            except Exception:
                continue
            if has_picker is True:
                candidates.append(candidate)
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise DouyinMusicError("抖音同时出现多个“选择音乐”页面，已安全停止")
        await page.wait_for_timeout(200)
    raise DouyinMusicError(
        "抖音点击“添加音乐”后未打开可见的“选择音乐”页面"
        f"（当前受控页面 {last_page_count} 个），已安全停止"
    )


async def _open_favorite_tab(page, dialog) -> None:
    """打开收藏标签。标签不唯一或不可操作时不继续。"""

    marker = None
    # 音乐抽屉首次打开会先显示“加载中”。收藏页签并不总是与标题同时
    # 挂载，等待一个有上限的可见控件，而不是凭固定睡眠猜测页面就绪。
    for _ in range(30):
        marker = await dialog.evaluate(
            """dialog => {
            const normalize = value => String(value || '')
                .replace(/[\\u200b\\u00a0]/g, ' ')
                .replace(/\\s+/g, ' ')
                .trim();
            const visible = node => {
                const rect = node.getBoundingClientRect();
                const style = getComputedStyle(node);
                return rect.width > 0 && rect.height > 0
                    && style.display !== 'none' && style.visibility !== 'hidden';
            };
            const roots = [];
            const seen = new Set();
            for (const node of dialog.querySelectorAll('*')) {
                if (!visible(node) || normalize(node.innerText || node.textContent) !== '收藏') continue;
                let root = node.closest('[role="tab"],button,[role="button"],a');
                if (!root) {
                    const candidates = [];
                    let current = node;
                    while (current && current !== dialog) {
                        if (visible(current)
                            && normalize(current.innerText || current.textContent) === '收藏') {
                            const rect = current.getBoundingClientRect();
                            if (rect.width >= 32 && rect.height >= 24) {
                                candidates.push({ current, area: rect.width * rect.height });
                            }
                        }
                        current = current.parentElement;
                    }
                    candidates.sort((left, right) => right.area - left.area);
                    root = candidates[0]?.current || node;
                }
                if (!visible(root)) continue;
                const rect = root.getBoundingClientRect();
                const key = `${root.tagName}|${root.className}|${Math.round(rect.x)}|${Math.round(rect.y)}`;
                if (seen.has(key)) continue;
                seen.add(key);
                roots.push(root);
            }
            if (roots.length !== 1) return { count: roots.length, marker: '' };
            roots[0].dataset.oneclickFavoriteMusicTab = 'active';
            return { count: 1, marker: 'active' };
        }"""
        )
        if isinstance(marker, Mapping) and int(marker.get("count") or 0) == 1:
            break
        await page.wait_for_timeout(200)
    if not isinstance(marker, Mapping) or int(marker.get("count") or 0) != 1:
        raise DouyinMusicError("抖音音乐弹层未找到唯一可点击的“收藏”标签，已安全停止")
    tab = page.locator('[data-oneclick-favorite-music-tab="active"]')
    try:
        await tab.scroll_into_view_if_needed(timeout=5_000)
        await tab.click(timeout=8_000)
    except Exception as exc:
        raise DouyinMusicError("抖音“收藏”音乐标签无法安全打开") from exc
    await page.wait_for_timeout(350)


def _normalize_favorite_music_rows(raw: object) -> list[dict[str, str]]:
    """把页面扫描结果收敛为可选择的收藏音乐行。

    这一步不接触浏览器，专门保留为可离线回归的安全边界：没有歌曲名、作者、
    时长或稳定可读身份的候选一律不接受；平台 ID 重复也不能靠猜测排序消解。
    """

    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    if not isinstance(raw, list):
        return rows
    for item in raw:
        normalized = normalize_music_readback(item)
        if not normalized:
            continue
        platform_identity = _normalized(
            item.get("musicId") if isinstance(item, Mapping) else ""
        )
        # 收藏页可能为同一首音乐渲染两个独立的可见虚拟列表项，而页面并未
        # 暴露 data-music-id。首版规则是选择视觉排序第一项：无平台 ID 时
        # 以当前可见顺序作为本次任务的最小补充身份，仍由该行的选中态回读。
        # 真正暴露的 platform ID 重复则仍是不可消解的歧义。
        if not platform_identity:
            normalized["musicId"] = (
                f"{normalized['musicId']}|favorite-index:{len(rows) + 1}"
            )
        identity = normalized["musicId"]
        if identity in seen:
            raise DouyinMusicError("抖音收藏音乐列表出现重复身份，无法安全选择第一首")
        seen.add(identity)
        marker = _normalized(item.get("marker") if isinstance(item, Mapping) else "")
        if not marker:
            continue
        normalized["marker"] = marker
        rows.append(normalized)
    return rows


async def _scan_favorite_music_rows(dialog) -> object:
    """仅读取当前音乐抽屉已渲染的收藏卡片，不点击或选择音乐。"""

    return await dialog.evaluate(
        """dialog => {
            const normalize = value => String(value || '')
                .replace(/[\\u200b\\u00a0]/g, ' ')
                .replace(/\\s+/g, ' ')
                .trim();
            const visible = node => {
                if (!(node instanceof HTMLElement)) return false;
                const rect = node.getBoundingClientRect();
                const style = getComputedStyle(node);
                return rect.width > 0 && rect.height > 0
                    && style.display !== 'none' && style.visibility !== 'hidden';
            };
            const duration = /^\\d{1,2}:\\d{2}$/;
            const rows = [];
            const seen = new Set();
            const cards = Array.from(dialog.querySelectorAll('div')).filter(node => {
                if (!visible(node)) return false;
                // 新旧版卡片哈希后缀不同，不能把“后缀中不能再有连字符”当作
                // 结构前提。后续仍会用四个语义节点把左/右子容器排除掉。
                return Array.from(node.classList || []).some(name =>
                    /^card-container(?:-[^\\s]+)?$/.test(name)
                );
            });
            for (const root of cards) {
                const titleNode = root.querySelector('[class*="song-name-"]');
                const creatorNode = root.querySelector('[class*="song-author"]');
                const durationNode = root.querySelector('[class*="song-duration"]');
                const applyButtons = Array.from(
                    root.querySelectorAll('button[class*="apply-btn"]')
                ).filter(button => normalize(button.innerText || button.textContent) === '使用');
                // 当前抖音收藏列表把“使用”按钮做成卡片 hover 后才显示。
                // 这里先确认它确实是该音乐卡片的唯一操作入口，但不能要求
                // 初始时可见；真正点击前会先显式 hover 并再次检查可见性。
                if (!titleNode || !creatorNode || !durationNode || applyButtons.length !== 1
                    || !visible(titleNode) || !visible(creatorNode)
                    || !visible(durationNode)) continue;
                const title = normalize(titleNode.innerText || titleNode.textContent);
                const creator = normalize(creatorNode.innerText || creatorNode.textContent);
                const time = normalize(durationNode.innerText || durationNode.textContent);
                if (!title || !creator || !duration.test(time) || time === '00:00') continue;
                const rect = root.getBoundingClientRect();
                const rootKey = `${root.tagName}|${root.className}|${Math.round(rect.x)}|${Math.round(rect.y)}|${title}|${creator}|${time}`;
                if (seen.has(rootKey)) continue;
                seen.add(rootKey);
                const musicId = normalize(
                    root.getAttribute('data-music-id') || root.getAttribute('data-id')
                    || root.getAttribute('data-item-id')
                );
                const marker = `favorite-${rows.length}`;
                root.dataset.oneclickFavoriteMusicRow = marker;
                rows.push({ marker, musicId, title, creator, duration: time, y: String(rect.y) });
            }
            return rows.sort((left, right) => Number(left.y) - Number(right.y));
        }"""
    )


async def _favorite_music_rows(
    dialog,
    *,
    attempts: int = _FAVORITE_MUSIC_ROW_WAIT_ATTEMPTS,
    interval_seconds: float = _FAVORITE_MUSIC_ROW_WAIT_SECONDS,
) -> list[dict[str, str]]:
    """读取收藏列表中可见音乐卡片；相同可见身份会被视为歧义。

    新版抽屉存在 ``00:00 / 00:00`` 的音频预览区域和“推荐”等页签。它们
    不能再作为泛化“有时长的行”处理。只接受同时具备歌曲名、作者、时长和
    “使用”按钮的可见音乐卡片，防止把抽屉标题或播放器状态误写为音乐。
    """

    safe_attempts = max(int(attempts), 1)
    safe_interval = max(float(interval_seconds), 0.0)
    for index in range(safe_attempts):
        raw = await _scan_favorite_music_rows(dialog)
        rows = _normalize_favorite_music_rows(raw)
        if rows:
            return rows
        if index + 1 < safe_attempts:
            await asyncio.sleep(safe_interval)
    seconds = safe_attempts * safe_interval
    raise DouyinMusicError(
        f"抖音收藏列表在 {seconds:.0f} 秒内没有出现可唯一回读的音乐，已安全停止"
    )


async def _selection_is_readable(
    page,
    dialog,
    music: Mapping[str, str],
    *,
    marker: str,
) -> bool:
    """只接受“使用”后抽屉关闭、编辑页可见回显的音乐。"""

    result = await page.evaluate(
        """({ title, creator, duration, marker }) => {
            const normalize = value => String(value || '')
                .replace(/[\\u200b\\u00a0]/g, ' ')
                .replace(/\\s+/g, ' ')
                .trim();
            const visible = node => {
                if (!(node instanceof HTMLElement)) return false;
                const rect = node.getBoundingClientRect();
                const style = getComputedStyle(node);
                return rect.width > 0 && rect.height > 0
                    && style.display !== 'none' && style.visibility !== 'hidden';
            };
            const drawers = Array.from(document.querySelectorAll(
                '[role="dialog"], [class*="modal"], [class*="Modal"], [class*="sidesheet"], [class*="SideSheet"]'
            )).filter(node => visible(node) && normalize(node.innerText).includes('选择音乐'));
            if (drawers.length) return false;
            const outsideText = normalize(Array.from(document.body.querySelectorAll('*'))
                .filter(node => visible(node) && !node.closest(
                    '[role="dialog"], [class*="modal"], [class*="Modal"], [class*="sidesheet"], [class*="SideSheet"]'
                ))
                .map(node => node.childElementCount === 0 ? node.innerText || node.textContent : '')
                .join(' '));
            return Boolean(title && outsideText.includes(title)
                && ((creator && outsideText.includes(creator)) || (duration && outsideText.includes(duration))));
        }""",
        {
            "title": music["title"],
            "creator": music["creator"],
            "duration": music["duration"],
            "marker": marker,
        },
    )
    return result is True


async def _complete_selected_music_if_needed(page, dialog, marker: str) -> bool:
    """仅在首条音乐已有明确选中态时，点击唯一的确认/使用控件。

    有些抖音页面点选音乐后自动回到编辑页，另一些会保留“选择音乐”弹层并
    出现“使用”或“完成”。这里绝不在未选中时猜测点击，也不匹配模糊文案。
    """

    state = await page.evaluate(
        """marker => {
            const normalize = value => String(value || '')
                .replace(/[\\u200b\\u00a0]/g, ' ')
                .replace(/\\s+/g, ' ')
                .trim();
            const visible = node => {
                if (!(node instanceof HTMLElement)) return false;
                const rect = node.getBoundingClientRect();
                const style = getComputedStyle(node);
                return rect.width > 0 && rect.height > 0
                    && style.display !== 'none' && style.visibility !== 'hidden';
            };
            const row = document.querySelector(
                `[data-oneclick-favorite-music-row="${CSS.escape(marker)}"]`
            );
            if (!row || !visible(row)) return { selected: false, actions: [] };
            const selected = [row, ...row.querySelectorAll('*')].some(node => {
                const checked = node.getAttribute('aria-selected') || node.getAttribute('data-selected')
                    || node.getAttribute('data-checked') || '';
                const input = node.matches?.('input[type="radio"],input[type="checkbox"]') ? node : null;
                return checked === 'true' || Boolean(input?.checked)
                    || /(?:^|[-_\\s])(selected|checked|active)(?:$|[-_\\s])/i.test(String(node.className || ''));
            });
            if (!selected) return { selected: false, actions: [] };
            const dialog = row.closest('[role="dialog"], [class*="modal"], [class*="Modal"]');
            if (!dialog || !visible(dialog)) return { selected: true, actions: [] };
            const actions = [];
            const seen = new Set();
            for (const node of dialog.querySelectorAll('button,[role="button"]')) {
                if (!visible(node) || node.hasAttribute('disabled') || node.getAttribute('aria-disabled') === 'true') continue;
                const label = normalize(node.innerText || node.textContent);
                if (!['使用', '确定', '完成'].includes(label)) continue;
                const rect = node.getBoundingClientRect();
                const key = `${label}|${Math.round(rect.x)}|${Math.round(rect.y)}`;
                if (seen.has(key)) continue;
                seen.add(key);
                node.dataset.oneclickFavoriteMusicConfirm = key;
                actions.push(key);
            }
            return { selected: true, actions };
        }""",
        marker,
    )
    if not isinstance(state, Mapping) or state.get("selected") is not True:
        return False
    actions = [
        _normalized(value)
        for value in state.get("actions") or []
        if _normalized(value)
    ]
    if not actions:
        return False
    if len(actions) != 1:
        raise DouyinMusicError("抖音音乐选中后出现多个确认控件，无法安全继续")
    control = page.locator(
        f'[data-oneclick-favorite-music-confirm="{actions[0]}"]'
    )
    try:
        await control.scroll_into_view_if_needed(timeout=5_000)
        await control.click(timeout=8_000)
    except Exception as exc:
        raise DouyinMusicError("抖音已选收藏音乐但确认控件无法安全点击") from exc
    return True


async def _close_selected_music_picker(page, dialog) -> None:
    """关闭已完成选择的新版音乐抽屉，并确认遮罩已经消失。

    只有音乐已由列表选中态回读确认后才调用。新版抽屉的关闭按钮有稳定的
    ``semi-sidesheet-close`` 语义 class；不使用坐标、Escape 或泛化的“×”
    文案，避免误点编辑器其它控件。
    """

    controls = dialog.locator("button.semi-sidesheet-close")
    visible = []
    for index in range(await controls.count()):
        node = controls.nth(index)
        try:
            if await node.is_visible() and await node.is_enabled():
                visible.append(node)
        except Exception:
            continue
    if len(visible) != 1:
        raise DouyinMusicError(
            f"抖音收藏音乐已选中，但音乐抽屉关闭控件不唯一（实际 {len(visible)} 个）"
        )
    try:
        await visible[0].click(timeout=8_000)
    except Exception as exc:
        raise DouyinMusicError("抖音收藏音乐已选中，但音乐抽屉无法安全关闭") from exc

    for _ in range(30):
        try:
            if not await dialog.is_visible():
                return
        except Exception:
            return
        await page.wait_for_timeout(200)
    raise DouyinMusicError(
        "抖音收藏音乐已选中，但关闭抽屉后遮罩仍存在，未继续后续字段"
    )


async def open_favorite_music_choices(page) -> tuple[Any, Any, list[dict[str, str]]]:
    """打开“添加音乐 → 收藏”，返回当前会话可见的可选音乐。

    返回值中的 ``marker`` 仅用于同一浏览器内存会话的后续精确点击，调用方
    不得把它写入任务、日志或内容包。用户界面只应展示歌曲名称、作者、时长
    和可读身份。
    """
    control = await _unique_visible_text_control(page, "添加音乐", "添加音乐")
    try:
        await control.scroll_into_view_if_needed(timeout=5_000)
        await control.click(timeout=8_000)
    except Exception as exc:
        raise DouyinMusicError("抖音“添加音乐”控件无法安全点击") from exc
    picker_page = await _music_picker_page(page)
    dialog = await _wait_for_music_dialog(picker_page)
    await _open_favorite_tab(picker_page, dialog)
    candidates = await _favorite_music_rows(dialog)
    return picker_page, dialog, [dict(item) for item in candidates]


def _sanitize_music_choice(value: Mapping[str, str]) -> dict[str, str]:
    """移除瞬态 DOM 标记，仅保留任务允许记录的音乐回读。"""

    result = normalize_music_readback(value)
    if not result:
        raise DouyinMusicError("抖音收藏音乐缺少可验证的名称、作者或时长")
    return result


async def select_favorite_music_choice(
    page,
    picker_page,
    dialog,
    candidate: Mapping[str, str],
) -> dict[str, str]:
    """选择用户指定的当前收藏音乐，并由编辑页回读确认。

    ``candidate`` 必须来自 :func:`open_favorite_music_choices` 的同一调用。若
    音乐抽屉重新渲染、行标记失效、按钮不唯一或平台没有回读，一律停止；不
    会用标题近似匹配或退回选择第一首。
    """

    selected = dict(candidate)
    marker = _normalized(selected.pop("marker", ""))
    if not marker:
        raise DouyinMusicError("抖音收藏音乐缺少当前会话选择标记，已安全停止")
    sanitized = _sanitize_music_choice(selected)
    row = picker_page.locator(f'[data-oneclick-favorite-music-row="{marker}"]')
    try:
        await row.scroll_into_view_if_needed(timeout=5_000)
        # 抖音当前收藏页仅在音乐卡片 hover 时展示“使用”。这不是坐标或
        # 模糊文本点击：下面仍要求该首音乐卡片内恰有一个可见、可用且文案
        # 精确为“使用”的按钮，才会继续。
        await row.hover(timeout=5_000)
        await picker_page.wait_for_timeout(120)
    except Exception as exc:
        raise DouyinMusicError("抖音收藏所选音乐卡片无法安全悬停") from exc

    apply_buttons = row.locator('button[class*="apply-btn"]')
    visible_apply_buttons: list[Any] = []
    for index in range(await apply_buttons.count()):
        button = apply_buttons.nth(index)
        try:
            if (
                await button.is_visible()
                and await button.is_enabled()
                and _normalized(await button.inner_text()) == "使用"
            ):
                visible_apply_buttons.append(button)
        except Exception:
            continue
    if len(visible_apply_buttons) != 1:
        raise DouyinMusicError(
            f"抖音收藏所选音乐未找到唯一“使用”按钮（实际 {len(visible_apply_buttons)} 个）"
        )
    try:
        await visible_apply_buttons[0].scroll_into_view_if_needed(timeout=5_000)
        await visible_apply_buttons[0].click(timeout=8_000)
    except Exception as exc:
        raise DouyinMusicError("抖音收藏所选音乐的“使用”按钮无法安全点击") from exc

    for _ in range(30):
        if await _selection_is_readable(
            picker_page, dialog, sanitized, marker=marker
        ):
            # 已由抽屉回读到所选音乐后，必须主动关闭它。否则编辑页仍被
            # 抽屉遮罩拦截，后续地点、声明等独立设置会在同一会话中被误判
            # 为不可操作，迫使用户取消并重新上传视频。
            await _close_selected_music_picker(picker_page, dialog)
            return sanitized
        await picker_page.wait_for_timeout(200)
    raise DouyinMusicError("抖音收藏所选音乐选择后未能由页面回读确认，已安全停止")


async def select_first_favorite_music(page) -> dict[str, str]:
    """兼容旧任务：选择收藏列表第一首并由页面回读确认。

    新的抖音带货向导不会调用这个函数，而是先展示
    :func:`open_favorite_music_choices` 的候选，再调用
    :func:`select_favorite_music_choice`。
    """

    picker_page, dialog, candidates = await open_favorite_music_choices(page)
    if not candidates:
        raise DouyinMusicError("抖音收藏列表没有可选择的音乐，已安全停止")
    return await select_favorite_music_choice(
        page, picker_page, dialog, candidates[0]
    )
