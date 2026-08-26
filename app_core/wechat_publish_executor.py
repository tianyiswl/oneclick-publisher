# -*- coding: utf-8 -*-
"""一键发公众号正式发表执行器。

该模块只在用户明确选择正式发布后运行。编辑器写入复用已验证的公众号
预检链路；最终阶段严格回读群发、定时、AI 声明与二维码状态，不猜测
控件，也不读取或持久化 Cookie 内容。
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path
import re
import tempfile
from typing import Any

from . import account_service, task_service
from .oneclick_preflight import (
    PreflightError,
    _WECHAT_HOME_URL,
    _account_for_payload,
    _storage_state,
    _wechat_preflight,
)
from .wechat_publish_policy import (
    build_publish_execution_record,
    decide_ai_source_declaration,
    decide_group_notification_scope_confirmation,
    decide_wechat_publish_options,
    normalize_wechat_publish_preferences,
    validate_silicon_evolution_auto_publish_readback,
)
from .wechat_verification import (
    TERMINAL_STATES,
    WechatVerificationError,
    validate_qr_image_bytes,
    verification_broker,
)


class WechatPublishError(RuntimeError):
    """公众号正式发表无法安全继续。"""

    def __init__(self, message: str, *, error_code: str = "wechat_publish_failed") -> None:
        super().__init__(message)
        self.error_code = error_code


_NAVIGATION_TRANSIENT_MARKERS = (
    "Execution context was destroyed",
    "Cannot find context with specified id",
    "Most likely the page has been closed",
)


def _normalized_text(value: object) -> str:
    """统一页面与任务回读文本，忽略空白差异。"""

    return " ".join(str(value or "").replace("\u200b", "").split())


def _is_transient_navigation_error(error: BaseException) -> bool:
    """判断是否为页面跳转中的暂态异常，而不是发表失败。

    公众号扫码成功后会从验证弹层立即跳转。Playwright 的
    ``page.wait_for_timeout`` 本身也会通过页面执行上下文计时，因此若在
    跳转瞬间调用，会把正常导航误报为 ``Page.evaluate`` 失败。
    """

    message = str(error)
    return any(marker in message for marker in _NAVIGATION_TRANSIENT_MARKERS)


async def _visible_nodes(locator) -> list[Any]:
    nodes: list[Any] = []
    for index in range(await locator.count()):
        node = locator.nth(index)
        try:
            if await node.is_visible():
                nodes.append(node)
        except Exception:
            continue
    return nodes


async def _page_state(page, expected_title: str) -> dict[str, Any]:
    """只回读可见页面状态；不把二维码、凭据或完整正文写入任务日志。"""

    return await page.evaluate(
        r"""expectedTitle => {
          const normalize = value => String(value || '').replace(/\s+/g, ' ').trim();
          const rendered = element => {
            if (!element) return false;
            const rect = element.getBoundingClientRect();
            const style = getComputedStyle(element);
            return rect.width > 0 && rect.height > 0
              && rect.bottom > 0 && rect.right > 0
              && style.display !== 'none' && style.visibility !== 'hidden'
              && Number(style.opacity || 1) > 0;
          };
          const unique = nodes => Array.from(new Set(nodes));
          const dialogs = unique(Array.from(document.querySelectorAll(
            '.weui-desktop-dialog,.weui-desktop-dialog__wrp,[role="dialog"],'
            + '.weui-desktop-popover'
          ))).filter(rendered).map(dialog => {
            const titleNode = dialog.querySelector(
              '.weui-desktop-dialog__title,.weui-desktop-dialog__hd h3,'
              + '[data-testid="dialog-title"],[role="heading"]'
            );
            const bodyNode = dialog.querySelector(
              '.weui-desktop-dialog__bd,[data-testid="dialog-body"]'
            );
            const buttons = unique(Array.from(dialog.querySelectorAll(
              'button,a,[role="button"],.weui-desktop-btn'
            ))).filter(rendered).map(node =>
              normalize(node.innerText || node.textContent)
            ).filter(Boolean);
            const title = normalize(titleNode?.innerText || titleNode?.textContent);
            let body = normalize(
              bodyNode?.innerText || bodyNode?.textContent || dialog.innerText
            );
            for (const button of buttons) {
              if (button) body = body.replace(button, ' ');
            }
            body = normalize(body);
            return {
              title: title.slice(0, 300),
              body: body.slice(0, 2400),
              text: normalize(dialog.innerText).slice(0, 2400),
              buttons,
            };
          }).filter(item => item.text || item.buttons.length);
          const qrNodes = Array.from(document.querySelectorAll(
            'img,canvas,[class*="qr"],[id*="qr"],[class*="scan"],[id*="scan"]'
          )).filter(node => {
            if (!rendered(node)) return false;
            const meta = [
              node.id || '', node.className || '', node.getAttribute?.('src') || '',
              node.getAttribute?.('alt') || '', node.getAttribute?.('aria-label') || ''
            ].join(' ').toLowerCase();
            return /(qrcode|qr_code|qr-|_qr|扫码|二维码|scan)/.test(meta);
          });
          qrNodes.forEach((node, index) =>
            node.setAttribute('data-oneclick-wechat-qr', String(index))
          );
          const bodyText = normalize(document.body.innerText);
          const qrText = ['扫码', '二维码', '微信扫一扫', '身份验证']
            .filter(marker => bodyText.includes(marker));
          const successMarkers = [
            '定时发表成功', '发表成功', '发布成功', '操作成功'
          ].filter(marker => bodyText.includes(marker));
          const links = Array.from(document.querySelectorAll('a[href]'))
            .filter(link => {
              const href = link.href || '';
              return href.includes('mp.weixin.qq.com/s')
                || normalize(link.innerText) === expectedTitle;
            }).map(link => ({
              text: normalize(link.innerText),
              href: link.href || '',
            }));
          const scheduledCards = [];
          const publishedCards = [];
          if (expectedTitle) {
            const titleNodes = Array.from(document.querySelectorAll(
              'a,p,span,div,h1,h2,h3,h4'
            )).filter(node =>
              rendered(node)
              && normalize(node.innerText || node.textContent) === expectedTitle
            );
            for (const titleNode of titleNodes) {
              let current = titleNode;
              for (let depth = 0; current && depth < 7; depth += 1) {
                const text = normalize(current.innerText || current.textContent);
                if (text.includes('定时发表') && text.includes(expectedTitle)) {
                  scheduledCards.push(text.slice(0, 1200));
                  break;
                }
                current = current.parentElement;
              }
            }
            for (const titleNode of titleNodes) {
              let current = titleNode;
              for (let depth = 0; current && depth < 9; depth += 1) {
                const text = normalize(current.innerText || current.textContent);
                if (text.includes('已发表') && text.includes(expectedTitle)) {
                  publishedCards.push(text.slice(0, 1200));
                  break;
                }
                current = current.parentElement;
              }
            }
          }
          return {
            url: location.href,
            pageTitle: document.title,
            dialogs,
            qrCount: qrNodes.length,
            qrText,
            successMarkers,
            links,
            scheduledCards: Array.from(new Set(scheduledCards)),
            publishedCards: Array.from(new Set(publishedCards)),
            titleVisible: bodyText.includes(expectedTitle),
            textHead: bodyText.slice(0, 1000),
            textTail: bodyText.slice(-1200),
          };
        }""",
        expected_title,
    )


async def _page_state_after_navigation(
    page,
    expected_title: str,
    *,
    timeout_seconds: float = 15,
) -> dict[str, Any]:
    """在扫码后的平台跳转期间有界重试页面回读。"""

    deadline = asyncio.get_running_loop().time() + float(timeout_seconds)
    last_error: Exception | None = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            return await _page_state(page, expected_title)
        except Exception as exc:
            message = str(exc)
            if not _is_transient_navigation_error(exc):
                raise
            last_error = exc
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=2_000)
            except Exception:
                pass
            # 不能用 page.wait_for_timeout：它会在导航中的旧执行上下文内
            # 运行，反而把正常扫码跳转报告成 Page.evaluate 失败。
            await asyncio.sleep(0.25)
    raise WechatPublishError(
        f"平台跳转后页面状态未在限定时间内稳定：{last_error or '未知导航错误'}"
    )


def _scheduled_home_readback(
    state: dict[str, Any],
    preferences: dict[str, Any],
) -> dict[str, Any]:
    """验证扫码后公众号首页的定时卡片，而不依赖瞬时成功提示。"""

    if not preferences.get("scheduledPublish"):
        return {"ok": False, "reason": "当前不是定时发表"}
    schedule_local = str(preferences.get("scheduleLocal") or "")
    try:
        scheduled_at = datetime.strptime(schedule_local, "%Y-%m-%d %H:%M")
    except ValueError:
        return {"ok": False, "reason": "定时时间格式无法回读"}
    date_label = _platform_date_label(scheduled_at.date())
    time_label = scheduled_at.strftime("%H:%M")
    require_group = bool(preferences.get("groupNotification"))
    for card in state.get("scheduledCards") or []:
        text = " ".join(str(card or "").split())
        if "定时发表" not in text:
            continue
        if date_label not in text or time_label not in text:
            continue
        if require_group and "已开启群发通知" not in text:
            continue
        return {
            "ok": True,
            "dateLabel": date_label,
            "time": time_label,
            "groupNotification": require_group,
        }
    return {
        "ok": False,
        "reason": "公众号首页未找到标题关联的准确定时卡片",
    }


def _immediate_home_readback(
    state: dict[str, Any],
    expected_title: str,
) -> dict[str, Any]:
    """验证公众号首页“近期发表”卡片，避免把已发表误记为失败。

    平台扫码后不保证保留传统的“发表成功”提示，有时会直接回到首页。
    只有同一标题位于可见的“已发表”卡片内，且能回读对应文章链接时，
    才把它当成正式发表成功；普通标题文本、草稿和定时卡片均不通过。
    """

    title = _normalized_text(expected_title)
    if not title:
        return {"ok": False, "reason": "缺少待回读文章标题"}
    cards = [
        " ".join(str(card or "").split())
        for card in state.get("publishedCards") or []
    ]
    matched = [
        card
        for card in cards
        if title in card and "已发表" in card
    ]
    if not matched:
        return {"ok": False, "reason": "公众号首页未找到标题关联的已发表卡片"}

    links = [
        item
        for item in state.get("links") or []
        if isinstance(item, dict)
        and _normalized_text(item.get("text")) == title
        and str(item.get("href") or "").startswith("https://mp.weixin.qq.com/s")
    ]
    if len(links) != 1:
        return {
            "ok": False,
            "reason": f"公众号首页文章链接不是唯一可回读结果（实际 {len(links)}）",
        }

    card = matched[0]
    time_match = re.search(r"(?:今天|昨天|\d{1,2}月\d{1,2}日|星期\S+)\s*(\d{1,2}:\d{2})\s*已发表", card)
    return {
        "ok": True,
        "card": card,
        "publishedAt": time_match.group(1) if time_match else "",
        "link": str(links[0]["href"]),
    }


async def _readback_immediate_publish_from_home(
    page,
    expected_title: str,
    *,
    attempts: int = 1,
    settle_seconds: float = 1.5,
) -> dict[str, Any]:
    """只读进入公众号首页，确认刚提交文章的已发表状态。

    发表/扫码后首页卡片可能需要短暂同步；只读重试不会点击发表、保存或
    改变文章字段。每次都要求标题与文章链接同时唯一匹配，不能把其它文章
    当作本次发表成功。
    """

    count = max(1, int(attempts))
    last_state: dict[str, Any] = {}
    last_error = ""
    for attempt in range(count):
        try:
            await page.goto(
                _WECHAT_HOME_URL,
                wait_until="domcontentloaded",
                timeout=45_000,
            )
            await asyncio.sleep(max(0.0, float(settle_seconds)))
            state = await _page_state_after_navigation(page, expected_title)
            last_state = state
            readback = _immediate_home_readback(state, expected_title)
            if readback.get("ok"):
                return {"readback": readback, "state": state}
            last_error = str(readback.get("reason") or "公众号首页尚未出现当前文章")
        except Exception as exc:
            if not _is_transient_navigation_error(exc):
                raise
            last_error = str(exc)
        if attempt + 1 < count:
            await asyncio.sleep(1.5)
    return {
        "readback": {"ok": False, "reason": last_error or "公众号首页未回读发表结果"},
        "state": last_state,
    }


async def _readback_scheduled_publish_from_home(
    page,
    expected_title: str,
    preferences: dict[str, Any],
    *,
    attempts: int = 1,
    settle_seconds: float = 1.5,
) -> dict[str, Any]:
    """只读回读本次定时发表的标题、时间与群发状态。"""

    count = max(1, int(attempts))
    last_state: dict[str, Any] = {}
    last_error = ""
    for attempt in range(count):
        try:
            await page.goto(
                _WECHAT_HOME_URL,
                wait_until="domcontentloaded",
                timeout=45_000,
            )
            await asyncio.sleep(max(0.0, float(settle_seconds)))
            state = await _page_state_after_navigation(page, expected_title)
            last_state = state
            readback = _scheduled_home_readback(state, preferences)
            if readback.get("ok"):
                return {"readback": readback, "state": state}
            last_error = str(readback.get("reason") or "公众号首页尚未出现本次定时发表")
        except Exception as exc:
            if not _is_transient_navigation_error(exc):
                raise
            last_error = str(exc)
        if attempt + 1 < count:
            await asyncio.sleep(1.5)
    return {
        "readback": {"ok": False, "reason": last_error or "公众号首页未回读定时发表结果"},
        "state": last_state,
    }


async def _recover_post_submit_result(
    page,
    *,
    title: str,
    preferences: dict[str, Any],
    ai_accepted: bool,
    preflight_message: str,
    execution_record: dict[str, Any],
) -> dict[str, Any] | None:
    """提交已发生后，仅以首页精确回读恢复导航瞬断的真实结果。

    此处不得重试发表按钮，也不得重建草稿。它仅在已经点击最终发表后，用
    当前同一受控会话打开公众号首页并按标题、链接或定时卡片回读。
    """

    if preferences.get("scheduledPublish"):
        home_result = await _readback_scheduled_publish_from_home(
            page,
            title,
            preferences,
            attempts=6,
            settle_seconds=2.0,
        )
        readback = dict(home_result.get("readback") or {})
        if not readback.get("ok"):
            return None
        schedule_text = str(preferences.get("scheduleLocal") or "")
        return {
            "ok": True,
            "message": f"公众号定时发表已由提交后首页回读确认：{schedule_text}",
            "actuallyPublished": False,
            "scheduled": True,
            "scheduledAt": schedule_text or None,
            "title": title,
            "links": [],
            "publishedAt": None,
            "aiDeclarationAccepted": ai_accepted,
            "preflightMessage": preflight_message,
            "executionRecord": execution_record,
            "recoveredAfterNavigation": True,
        }

    home_result = await _readback_immediate_publish_from_home(
        page,
        title,
        attempts=6,
        settle_seconds=2.0,
    )
    readback = dict(home_result.get("readback") or {})
    if not readback.get("ok"):
        return None
    return {
        "ok": True,
        "message": "公众号已正式发表并由提交后首页回读确认",
        "actuallyPublished": True,
        "scheduled": False,
        "scheduledAt": None,
        "title": title,
        "links": [{"text": title, "href": readback["link"]}],
        "publishedAt": readback.get("publishedAt") or None,
        "aiDeclarationAccepted": ai_accepted,
        "preflightMessage": preflight_message,
        "executionRecord": execution_record,
        "recoveredAfterNavigation": True,
    }


async def _close_publish_session(context, browser) -> None:
    """清理临时会话，但不能用清理异常覆盖已回读的发表结果。"""

    for resource in (context, browser):
        if resource is None:
            continue
        try:
            await resource.close()
        except Exception:
            # 平台跳转后 Playwright driver 可能先行断开；此时任务结果已经由
            # 平台页面回读确认，关闭失败不应把成功改写为失败。
            continue


def _sanitize_dialog_text(value: object) -> str:
    """保留可见提示语义，同时移除可能包含会话参数的 URL/令牌。"""

    text = " ".join(str(value or "").split())
    text = re.sub(r"https?://\S+", "[链接已省略]", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(?i)\b(token|cookie|session|ticket|auth|code)\s*[=:]\s*[^\s,;]+",
        lambda match: f"{match.group(1)}=[已省略]",
        text,
    )
    return text[:2400]


def _safe_dialog_report(state: dict[str, Any]) -> dict[str, Any]:
    """生成可写入本地任务事件的最小弹窗证据；二维码绝不落盘。"""

    if state.get("qrCount") or state.get("qrText"):
        return {
            "kind": "qr",
            "title": "需要微信验证",
            "body": "页面出现二维码或微信验证提示；二维码内容未记录",
            "buttons": [],
        }
    dialogs: list[dict[str, Any]] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    for raw in state.get("dialogs") or []:
        title = _sanitize_dialog_text(raw.get("title"))
        body = _sanitize_dialog_text(raw.get("body") or raw.get("text"))
        buttons = tuple(
            dict.fromkeys(
                _sanitize_dialog_text(item)
                for item in raw.get("buttons") or []
                if _sanitize_dialog_text(item)
            )
        )
        signature = (title, body, buttons)
        if signature in seen:
            continue
        seen.add(signature)
        dialogs.append(
            {
                "title": title,
                "body": body,
                "buttons": list(buttons),
            }
        )
    return {
        "kind": "dialog",
        "dialogs": dialogs,
    }


async def _pause_for_user(
    page,
    *,
    task_id: int,
    state: dict[str, Any],
) -> None:
    """保持真实页面前台暂停；不点击、刷新或关闭当前平台上下文。"""

    report = _safe_dialog_report(state)
    screenshot_path = ""
    if report.get("kind") != "qr":
        visible_dialogs = await _visible_nodes(
            page.locator(
                ".weui-desktop-dialog,[role='dialog']"
            )
        )
        if visible_dialogs:
            screenshot_path = str(
                Path(tempfile.gettempdir())
                / f"oneclick-wechat-confirm-task-{int(task_id)}.png"
            )
            await visible_dialogs[0].screenshot(
                path=screenshot_path,
                type="png",
            )
    if report.get("kind") == "qr":
        event_type = "wechat_publish_qr_required"
        message = "公众号定时发表需要微信扫码；二维码未记录，浏览器已保持前台"
    else:
        event_type = "wechat_publish_user_action_required"
        dialogs = list(report.get("dialogs") or [])
        message = (
            "公众号定时发表等待用户确认；"
            f"弹窗={dialogs}；"
            f"本机临时截图={screenshot_path or '未生成'}"
        )
    task_service.record_task_event(
        task_id,
        event_type,
        message,
        level="warning",
    )
    await page.bring_to_front()
    # 保持同一 Playwright 上下文和页面原样等待，外部终止进程才会关闭。
    await asyncio.Event().wait()


async def _final_options_snapshot(page) -> dict[str, Any]:
    """定位最终发表弹窗，回读开关、定时时间和唯一发表按钮。"""

    return await page.evaluate(
        r"""() => {
          const normalize = value => String(value || '').replace(/\s+/g, '').trim();
          const rendered = element => {
            if (!element) return false;
            const rect = element.getBoundingClientRect();
            const style = getComputedStyle(element);
            return rect.width > 0 && rect.height > 0
              && rect.bottom > 0 && rect.right > 0
              && style.display !== 'none' && style.visibility !== 'hidden'
              && Number(style.opacity || 1) > 0;
          };
          document.querySelectorAll('[data-oneclick-final-option],'
            + '[data-oneclick-final-publish],[data-oneclick-schedule-input]')
            .forEach(node => {
              node.removeAttribute('data-oneclick-final-option');
              node.removeAttribute('data-oneclick-final-publish');
              node.removeAttribute('data-oneclick-schedule-input');
            });
          const dialogs = Array.from(document.querySelectorAll(
            '.weui-desktop-dialog,[role="dialog"]'
          )).filter(rendered).filter(dialog => {
            const text = normalize(dialog.innerText);
            return text.includes('群发通知') && text.includes('定时发表');
          });
          if (dialogs.length !== 1) {
            return {dialogCount: dialogs.length, options: {}, buttons: []};
          }
          const dialog = dialogs[0];
          const readControl = (key, label) => {
            const labels = Array.from(dialog.querySelectorAll('*')).filter(node =>
              rendered(node) && normalize(node.innerText || node.textContent) === label
            ).sort((a, b) => {
              const ar = a.getBoundingClientRect();
              const br = b.getBoundingClientRect();
              return ar.width * ar.height - br.width * br.height;
            });
            if (!labels.length) return {available: false, labelCount: 0};
            let row = labels[0];
            for (let depth = 0; depth < 7 && row && row !== dialog; depth += 1) {
              const controls = Array.from(row.querySelectorAll(
                'input[type="checkbox"],[role="switch"],[aria-checked],[class*="switch"]'
              )).filter(node => node.matches('input[type="checkbox"]') || rendered(node));
              const stateNode = controls.find(node => node.matches('input[type="checkbox"]'))
                || controls[0];
              if (stateNode) {
                const clickNode = stateNode.matches('input[type="checkbox"]')
                  ? (stateNode.closest('label') || stateNode.parentElement || stateNode)
                  : stateNode;
                clickNode.setAttribute('data-oneclick-final-option', key);
                const aria = stateNode.getAttribute('aria-checked');
                const cls = String(stateNode.className || '').toLowerCase();
                let enabled = null;
                if (stateNode.matches('input[type="checkbox"]')) {
                  enabled = Boolean(stateNode.checked);
                } else if (aria === 'true' || aria === 'false') {
                  enabled = aria === 'true';
                } else if (/(checked|switch_on|active)/.test(cls)) {
                  enabled = true;
                } else if (/(switch_off|disabled)/.test(cls)) {
                  enabled = false;
                } else {
                  const nested = stateNode.querySelector('input[type="checkbox"]');
                  if (nested) enabled = Boolean(nested.checked);
                }
                return {
                  available: enabled !== null,
                  enabled,
                  labelCount: labels.length,
                  controlTag: stateNode.tagName.toLowerCase(),
                  controlClass: String(stateNode.className || '').slice(0, 240),
                };
              }
              row = row.parentElement;
            }
            return {available: false, labelCount: labels.length};
          };
          const options = {
            groupNotification: readControl('groupNotification', '群发通知'),
            groupedNotification: readControl('groupedNotification', '分组通知'),
            scheduledPublish: readControl('scheduledPublish', '定时发表'),
          };
          const scheduleLabels = Array.from(dialog.querySelectorAll('*')).filter(node =>
            rendered(node)
            && normalize(node.innerText || node.textContent) === '定时发表'
          );
          let scheduleDebug = [];
          if (scheduleLabels.length) {
            let row = scheduleLabels[0];
            for (let depth = 0; depth < 6 && row && row !== dialog; depth += 1) {
              scheduleDebug.push(row.outerHTML.slice(0, 6000));
              row = row.parentElement;
            }
          }
          const inputs = Array.from(dialog.querySelectorAll(
            'input:not([type="checkbox"]),textarea'
          )).filter(rendered).map((node, index) => {
            node.setAttribute('data-oneclick-schedule-input', String(index));
            return {
              index,
              type: node.getAttribute('type') || node.tagName.toLowerCase(),
              placeholder: node.getAttribute('placeholder') || '',
              ariaLabel: node.getAttribute('aria-label') || '',
              value: node.value || '',
              readOnly: Boolean(node.readOnly),
            };
          });
          const dateValueNode = dialog.querySelector(
            '.mass-send__timer .weui-desktop-form__dropdown__value'
          );
          const scheduledDateLabel = dateValueNode
            ? normalize(
                dateValueNode.getAttribute('title')
                || dateValueNode.innerText
                || dateValueNode.textContent
              )
            : '';
          const clickables = Array.from(dialog.querySelectorAll(
            'button,a,[role="button"],[tabindex],label'
          )).filter(rendered).map((node, index) => ({
            index,
            tag: node.tagName.toLowerCase(),
            text: normalize(node.innerText || node.textContent).slice(0, 80),
            classes: String(node.className || '').slice(0, 240),
            ariaLabel: node.getAttribute('aria-label') || '',
          })).filter(item => item.text || item.ariaLabel);
          const values = inputs.map(item => item.value).filter(Boolean).join(' ');
          const date = values.match(/20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}/);
          const time = values.match(/\d{1,2}:\d{2}/);
          const scheduledTime = date && time
            ? `${date[0].replace(/[年/.]/g, '-').replace('月', '-').replace('日', '')} ${time[0]}`
            : '';
          if (scheduledTime) options.scheduledPublish.time = scheduledTime;
          const buttons = Array.from(dialog.querySelectorAll(
            'button,a,[role="button"],.weui-desktop-btn'
          )).filter(rendered).map(node => ({
            node,
            text: normalize(node.innerText || node.textContent),
          })).filter(item => item.text);
          const publishButtons = buttons.filter(item => item.text === '发表');
          publishButtons.forEach(item =>
            item.node.setAttribute('data-oneclick-final-publish', '1')
          );
          return {
            dialogCount: 1,
            options,
            inputs,
            clickables,
            dialogText: normalize(dialog.innerText).slice(0, 1800),
            scheduleDebug,
            scheduledDateLabel,
            scheduledTime,
            buttons: Array.from(new Set(buttons.map(item => item.text))),
            publishButtonCount: publishButtons.length,
          };
        }"""
    )


async def _set_toggle(page, key: str, enabled: bool) -> None:
    snapshot = await _final_options_snapshot(page)
    option = dict((snapshot.get("options") or {}).get(key) or {})
    if option.get("available") is not True:
        raise WechatPublishError(f"发布选项无法唯一识别：{key}")
    if option.get("enabled") is enabled:
        return
    node = page.locator(f'[data-oneclick-final-option="{key}"]')
    if await node.count() != 1 or not await node.first.is_visible():
        raise WechatPublishError(f"发布选项不是唯一可见控件：{key}")
    # 公众号最终发表弹窗会先渲染 loading 态开关，再异步解除 disabled。
    # 不可点击时绝不使用 force；有界等待后仍未就绪则带明确证据停止。
    deadline = asyncio.get_running_loop().time() + 30
    while asyncio.get_running_loop().time() < deadline:
        if await node.first.is_enabled():
            break
        await page.wait_for_timeout(250)
    else:
        class_name = str(await node.first.get_attribute("class") or "")
        raise WechatPublishError(
            f"发布选项加载超时且保持不可点击：{key}；class={class_name}"
        )
    await node.first.click(timeout=10_000)
    await page.wait_for_timeout(500)
    updated = await _final_options_snapshot(page)
    actual = dict((updated.get("options") or {}).get(key) or {}).get("enabled")
    if actual is not enabled:
        raise WechatPublishError(f"发布选项设置后回读不一致：{key}")


def _platform_date_label(target: date, *, today: date | None = None) -> str:
    current = today or datetime.now().date()
    if target == current:
        return "今天"
    if target == current + timedelta(days=1):
        return "明天"
    return f"{target.month}月{target.day}日"


async def _fill_schedule_inputs(page, schedule_local: str) -> dict[str, str]:
    expected_date, expected_time = schedule_local.split(" ", 1)
    target_date = datetime.strptime(expected_date, "%Y-%m-%d").date()
    expected_date_label = _platform_date_label(target_date)
    snapshot = await _final_options_snapshot(page)
    inputs = list(snapshot.get("inputs") or [])
    if not inputs:
        raise WechatPublishError("定时发表已开启，但没有出现可回读的时间输入控件")

    date_trigger = page.locator(
        ".mass-send__timer .weui-desktop-form__dropdown__inner-button"
    )
    visible_date_triggers = await _visible_nodes(date_trigger)
    if len(visible_date_triggers) != 1:
        raise WechatPublishError("定时日期下拉控件不是唯一可见控件")
    await visible_date_triggers[0].click(timeout=5_000)
    await page.wait_for_timeout(250)
    date_options = await _visible_nodes(
        page.get_by_text(expected_date_label, exact=True)
    )
    date_options = [
        node
        for node in date_options
        if "weui-desktop-tooltip"
        not in str(await node.get_attribute("class") or "")
    ]
    if len(date_options) != 1:
        raise WechatPublishError(
            f"平台日期选项无法唯一匹配：{expected_date_label}"
        )
    await date_options[0].click(timeout=5_000)
    await page.wait_for_timeout(250)

    time_input = page.locator('input[placeholder="请选择时间"]')
    visible_time_inputs = await _visible_nodes(time_input)
    if len(visible_time_inputs) != 1:
        raise WechatPublishError("定时时间输入控件不是唯一可见控件")
    await visible_time_inputs[0].click(timeout=5_000)
    await page.wait_for_timeout(250)
    hour, minute = expected_time.split(":", 1)
    hour_nodes = await _visible_nodes(
        page.locator(".weui-desktop-picker__time__hour li").filter(
            has_text=re.compile(rf"^\s*{re.escape(hour)}\s*$")
        )
    )
    hour_nodes = [
        node
        for node in hour_nodes
        if "weui-desktop-picker__disabled"
        not in str(await node.get_attribute("class") or "")
    ]
    if len(hour_nodes) != 1:
        raise WechatPublishError(f"定时小时不可唯一选择：{hour}")
    await hour_nodes[0].click(timeout=5_000)
    minute_nodes = await _visible_nodes(
        page.locator(".weui-desktop-picker__time__minute li").filter(
            has_text=re.compile(rf"^\s*{re.escape(minute)}\s*$")
        )
    )
    minute_nodes = [
        node
        for node in minute_nodes
        if "weui-desktop-picker__disabled"
        not in str(await node.get_attribute("class") or "")
    ]
    if len(minute_nodes) != 1:
        raise WechatPublishError(f"定时分钟不可唯一选择：{minute}")
    await minute_nodes[0].click(timeout=5_000)
    await page.wait_for_timeout(300)

    after = await _final_options_snapshot(page)
    actual_date_label = str(after.get("scheduledDateLabel") or "")
    actual_time = str(
        next(
            (
                item.get("value")
                for item in after.get("inputs") or []
                if str(item.get("placeholder") or "") == "请选择时间"
            ),
            "",
        )
        or ""
    )
    if actual_date_label != expected_date_label or actual_time != expected_time:
        raise WechatPublishError(
            "平台定时时间选择后回读不一致："
            + str(
                {
                    "expectedDate": expected_date_label,
                    "actualDate": actual_date_label,
                    "expectedTime": expected_time,
                    "actualTime": actual_time,
                }
            )
        )
    picker_panel = page.locator(".weui-desktop-picker__dd__time")
    visible_picker_panels = await _visible_nodes(picker_panel)
    if len(visible_picker_panels) == 1:
        dialog_headers = await _visible_nodes(
            page.locator(".weui-desktop-dialog__hd")
        )
        if len(dialog_headers) != 1:
            raise WechatPublishError("时间选择器无法安全收起")
        await dialog_headers[0].click(
            position={"x": 12, "y": 12},
            timeout=5_000,
        )
        await page.wait_for_timeout(250)
        if await _visible_nodes(picker_panel):
            raise WechatPublishError("时间选择器点击后仍未收起")
    return {
        "dateLabel": actual_date_label,
        "time": actual_time,
        "scheduleLocal": schedule_local,
    }


async def _prepare_final_options(page, payload: dict[str, Any]) -> dict[str, Any]:
    preferences = normalize_wechat_publish_preferences(payload)
    await _set_toggle(page, "groupNotification", preferences["groupNotification"])
    await _set_toggle(page, "scheduledPublish", preferences["scheduledPublish"])
    schedule_readback: dict[str, str] = {}
    if preferences["scheduledPublish"]:
        schedule_readback = await _fill_schedule_inputs(
            page,
            str(preferences["scheduleLocal"]),
        )
    snapshot = await _final_options_snapshot(page)
    if preferences["scheduledPublish"]:
        snapshot["scheduledTime"] = schedule_readback["scheduleLocal"]
        snapshot["options"]["scheduledPublish"]["time"] = schedule_readback[
            "scheduleLocal"
        ]
    decision = decide_wechat_publish_options(payload, snapshot)
    if not decision.get("allowed"):
        raise WechatPublishError(
            f"{decision.get('reason') or '发布方式回读失败'}；页面快照={snapshot}"
        )
    return {"snapshot": snapshot, "decision": decision}


async def _capture_qr_image(page) -> bytes:
    """只在内存中寻找并截取真正可扫码的二维码像素图。"""

    selectors = (
        "[data-oneclick-wechat-qr] img",
        "[data-oneclick-wechat-qr] canvas",
        "[data-oneclick-wechat-qr] svg",
        "[data-oneclick-wechat-qr] *",
        "[data-oneclick-wechat-qr]",
        "img",
        "canvas",
        "svg",
    )
    seen_boxes: set[tuple[int, int, int, int]] = set()
    candidates: list[tuple[int, bytes]] = []
    for selector in selectors:
        locator = page.locator(selector)
        count = min(await locator.count(), 160)
        for index in range(count):
            node = locator.nth(index)
            try:
                if not await node.is_visible():
                    continue
                box = await node.bounding_box()
                if not box:
                    continue
                width = float(box.get("width") or 0)
                height = float(box.get("height") or 0)
                if min(width, height) < 120 or max(width, height) > 900:
                    continue
                if not 0.70 <= width / max(1.0, height) <= 1.42:
                    continue
                box_key = (
                    round(float(box.get("x") or 0)),
                    round(float(box.get("y") or 0)),
                    round(width),
                    round(height),
                )
                if box_key in seen_boxes:
                    continue
                seen_boxes.add(box_key)
                image = await node.screenshot(type="png")
                validate_qr_image_bytes(image)
                candidates.append((round(width * height), image))
            except Exception:
                continue
    if not candidates:
        raise WechatPublishError(
            "页面出现微信验证，但尚未找到具有可扫码像素特征的二维码"
        )
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


async def _wait_for_qr_image(page, *, timeout_seconds: float = 15) -> bytes:
    deadline = asyncio.get_running_loop().time() + float(timeout_seconds)
    last_error = ""
    while asyncio.get_running_loop().time() < deadline:
        try:
            return await _capture_qr_image(page)
        except WechatPublishError as exc:
            last_error = str(exc)
            await asyncio.sleep(0.4)
    raise WechatPublishError(last_error or "微信验证二维码加载超时")


async def _handle_qr_verification(page, task_id: int) -> None:
    qr_image = await _wait_for_qr_image(page)
    loop = asyncio.get_running_loop()

    def refresh_qr() -> bytes:
        future = asyncio.run_coroutine_threadsafe(
            _wait_for_qr_image(page, timeout_seconds=12),
            loop,
        )
        return future.result(timeout=15)

    def open_verification_page() -> None:
        future = asyncio.run_coroutine_threadsafe(page.bring_to_front(), loop)
        future.result(timeout=5)

    request_id = verification_broker.create(
        task_id=task_id,
        qr_image=qr_image,
        expires_in_seconds=120,
        refresh_callback=refresh_qr,
        open_page_callback=open_verification_page,
    )
    task_service.record_task_event(
        task_id,
        "wechat_verification_required",
        "公众号发表需要微信验证，请在一键发客户端扫码",
        level="warning",
    )
    try:
        deadline = asyncio.get_running_loop().time() + 600
        while asyncio.get_running_loop().time() < deadline:
            broker_state = verification_broker.snapshot(request_id)
            if broker_state["state"] in {"cancelled", "failed"}:
                raise WechatPublishError(str(broker_state["message"]))
            # 扫码成功时页面会主动导航；使用事件循环等待，不能把导航中断
            # 误记为 Playwright 的页面执行上下文错误。
            await asyncio.sleep(0.8)
            state = await _page_state_after_navigation(page, "")
            if state.get("qrCount") or state.get("qrText"):
                if "已扫码" in str(state.get("textTail") or ""):
                    verification_broker.mark_verifying(request_id)
                continue
            verification_broker.succeed(request_id)
            task_service.record_task_event(
                task_id,
                "wechat_verification_succeeded",
                "微信验证成功，继续同一公众号定时发表会话",
            )
            await asyncio.sleep(1)
            return
        verification_broker.fail(request_id, "等待微信验证超时，定时发表已安全停止")
        raise WechatPublishError("等待微信验证超时，定时发表已安全停止")
    finally:
        verification_broker.clear(request_id)


async def run_wechat_publish(payload: dict[str, Any], *, task_id: int) -> dict[str, Any]:
    """在同一 Playwright storage_state 会话中完成公众号正式提交。"""

    if str(payload.get("runtimeMode") or "") != "publish":
        raise WechatPublishError("公众号正式执行器只接受 runtimeMode=publish")
    if payload.get("debugDryRun") is not False:
        raise WechatPublishError("公众号正式执行器要求 debugDryRun=false")
    preferences = normalize_wechat_publish_preferences(payload)
    account = _account_for_payload(payload)
    if int(account.get("type") or 0) != 10:
        raise WechatPublishError("公众号正式执行器只接受公众号账号")
    storage_state = _storage_state(account)
    title = str(payload.get("title") or "").strip()
    if not title:
        raise WechatPublishError("公众号标题为空")

    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=bool(payload.get("backgroundMode", True))
        )
        context = await browser.new_context(
            storage_state=str(storage_state),
            viewport={"width": 1440, "height": 1000},
        )
        page = await context.new_page()
        preflight_message = ""
        execution_record: dict[str, Any] = {}
        ai_accepted = False
        group_scope_accepted = False
        final_clicked = False
        try:
            # 正式模式仅改变最终提交边界；字段写入与四项回读复用已验证链路。
            preflight_payload = dict(payload)
            preflight_payload["runtimeMode"] = "preflight"
            preflight_payload["debugDryRun"] = True
            preflight_message = await _wechat_preflight(
                page,
                preflight_payload,
                account=account,
            )
            editor_account = await account_service._detect_display_name(page, 10)
            expected_account = str(account.get("profileName") or account.get("userName") or "")
            if editor_account != expected_account:
                raise WechatPublishError(
                    f"公众号编辑器账号回读不一致：{editor_account or '空'}"
                )
            execution_record = build_publish_execution_record(
                account_id=int(account["id"]),
                profile_name=expected_account,
                storage_state=storage_state,
                context_type="playwright-storage-state",
                editor_account_readback=editor_account,
            )
            task_service.record_task_event(
                task_id,
                "wechat_editor_verified",
                f"公众号账号、标题、正文、封面与作者规则已回读：{expected_account}",
            )

            buttons = await _visible_nodes(page.get_by_text("发表", exact=True))
            if len(buttons) != 1 or not await buttons[0].is_enabled():
                raise WechatPublishError("公众号初始发表入口不是唯一可用控件")
            await buttons[0].click(timeout=10_000)

            last_state: dict[str, Any] = {}
            for _ in range(180):
                # 发表确认或微信扫码后可能立即跳转；不能在旧页面上下文中
                # 计时，否则正常跳转会被误报为 Page.evaluate 失败。
                await asyncio.sleep(0.5)
                last_state = await _page_state_after_navigation(page, title)

                if last_state.get("qrCount") or last_state.get("qrText"):
                    await _handle_qr_verification(page, task_id)
                    continue

                final_snapshot = await _final_options_snapshot(page)
                if int(final_snapshot.get("dialogCount") or 0) == 1 and not final_clicked:
                    prepared = await _prepare_final_options(page, payload)
                    if payload.get("siliconEvolutionArticleId"):
                        silicon_state = {
                            **dict(prepared["snapshot"]),
                            "articleId": payload.get("siliconEvolutionArticleId"),
                            "packageSha256": payload.get(
                                "siliconEvolutionPackageSha256"
                            ),
                            "accountId": int(account["id"]),
                            "accountDisplayName": editor_account,
                            "preflightVerified": True,
                        }
                        silicon_decision = (
                            validate_silicon_evolution_auto_publish_readback(
                                payload,
                                silicon_state,
                            )
                        )
                        if not silicon_decision.get("allowed"):
                            raise WechatPublishError(
                                str(silicon_decision.get("reason") or "自动直发最终回读失败"),
                                error_code=str(
                                    silicon_decision.get("errorCode")
                                    or "wechat_publish_options_mismatch"
                                ),
                            )
                    final_button = page.locator(
                        '[data-oneclick-final-publish="1"]'
                    )
                    if (
                        await final_button.count() != 1
                        or not await final_button.first.is_visible()
                        or not await final_button.first.is_enabled()
                    ):
                        raise WechatPublishError("最终发表按钮不是唯一可用控件")
                    await final_button.first.click(timeout=10_000)
                    final_clicked = True
                    task_service.record_task_event(
                        task_id,
                        "wechat_final_submit_clicked",
                        (
                            f"已提交公众号定时发表：{preferences['scheduleLocal']}"
                            if preferences["scheduledPublish"]
                            else "已提交公众号立即发表"
                        ),
                    )
                    continue

                if last_state.get("dialogs"):
                    ai_decision = decide_ai_source_declaration(
                        payload.get("aiDisclosure"),
                        last_state,
                    )
                    if ai_decision.get("allowed") and not ai_accepted:
                        continue_buttons = await _visible_nodes(
                            page.get_by_text("继续发表", exact=True)
                        )
                        if len(continue_buttons) != 1 or not await continue_buttons[0].is_enabled():
                            raise WechatPublishError("AI 声明继续发表控件不是唯一可用控件")
                        await continue_buttons[0].click(timeout=10_000)
                        ai_accepted = True
                        task_service.record_task_event(
                            task_id,
                            "wechat_ai_declaration_accepted",
                            "平台仅声明与内容元数据相符的 AI 生成图片，已按规则继续",
                        )
                        continue
                    if ai_accepted and ai_decision.get("allowed"):
                        continue
                    group_scope_decision = decide_group_notification_scope_confirmation(
                        payload,
                        last_state,
                    )
                    if (
                        group_scope_decision.get("allowed")
                        and not group_scope_accepted
                    ):
                        continue_buttons = await _visible_nodes(
                            page.get_by_text("继续发表", exact=True)
                        )
                        if (
                            len(continue_buttons) != 1
                            or not await continue_buttons[0].is_enabled()
                        ):
                            raise WechatPublishError(
                                "群发展示范围提示的继续发表控件不是唯一可用控件"
                            )
                        await continue_buttons[0].click(timeout=10_000)
                        group_scope_accepted = True
                        task_service.record_task_event(
                            task_id,
                            "wechat_group_scope_confirmation_accepted",
                            "已精确匹配并接受群发展示范围与平台推荐场景说明",
                        )
                        continue
                    if (
                        group_scope_accepted
                        and group_scope_decision.get("allowed")
                    ):
                        continue
                    await _pause_for_user(
                        page,
                        task_id=task_id,
                        state=last_state,
                    )

                scheduled_home = _scheduled_home_readback(
                    last_state,
                    preferences,
                )
                immediate_home = _immediate_home_readback(last_state, title)
                if (
                    last_state.get("successMarkers")
                    or scheduled_home.get("ok")
                    or immediate_home.get("ok")
                ):
                    schedule_text = str(preferences.get("scheduleLocal") or "")
                    if preferences["scheduledPublish"] and not scheduled_home.get("ok"):
                        page_text = (
                            str(last_state.get("textHead") or "")
                            + str(last_state.get("textTail") or "")
                        )
                        normalized_schedule = schedule_text.replace("-", "/")
                        if schedule_text not in page_text and normalized_schedule not in page_text:
                            raise WechatPublishError("平台成功页未回读到指定定时时间")
                    if immediate_home.get("ok"):
                        task_service.record_task_event(
                            task_id,
                            "wechat_home_publish_readback",
                            "公众号首页近期发表已回读当前标题与文章链接",
                        )
                    return {
                        "ok": True,
                        "message": (
                            f"公众号定时发表已提交并回读：{schedule_text}"
                            if preferences["scheduledPublish"]
                            else (
                                "公众号已正式发表并由首页近期发表记录回读成功"
                                if immediate_home.get("ok")
                                else "公众号已正式发表并回读成功"
                            )
                        ),
                        "actuallyPublished": not preferences["scheduledPublish"],
                        "scheduled": preferences["scheduledPublish"],
                        "scheduledAt": schedule_text or None,
                        "title": title,
                        "links": (
                            [{"text": title, "href": immediate_home["link"]}]
                            if immediate_home.get("ok")
                            else list(last_state.get("links") or [])
                        ),
                        "publishedAt": immediate_home.get("publishedAt") or None,
                        "aiDeclarationAccepted": ai_accepted,
                        "preflightMessage": preflight_message,
                        "executionRecord": execution_record,
                    }
            if final_clicked:
                try:
                    recovered = await _recover_post_submit_result(
                        page,
                        title=title,
                        preferences=preferences,
                        ai_accepted=ai_accepted,
                        preflight_message=preflight_message,
                        execution_record=execution_record,
                    )
                except Exception:
                    recovered = None
                if recovered:
                    task_service.record_task_event(
                        task_id,
                        "wechat_post_submit_home_readback",
                        "提交后平台页面未给出稳定提示，已由公众号首页精确回读恢复结果",
                    )
                    return recovered
            raise WechatPublishError(
                "提交后未在限定时间内出现可靠成功状态或明确阻断"
            )
        except PreflightError as exc:
            raise WechatPublishError(str(exc)) from exc
        except Exception as exc:
            # 只针对扫码后真实页面导航的暂态错误做首页只读恢复。未知弹窗、
            # 风控、内容合规或其它执行错误不会被自动跨过。
            if final_clicked and _is_transient_navigation_error(exc):
                try:
                    recovered = await _recover_post_submit_result(
                        page,
                        title=title,
                        preferences=preferences,
                        ai_accepted=ai_accepted,
                        preflight_message=preflight_message,
                        execution_record=execution_record,
                    )
                except Exception:
                    recovered = None
                if recovered:
                    task_service.record_task_event(
                        task_id,
                        "wechat_post_submit_navigation_recovered",
                        "扫码后页面跳转中断已由公众号首页精确回读恢复结果",
                    )
                    return recovered
            raise
        finally:
            await _close_publish_session(context, browser)


def run_wechat_publish_sync(
    payload: dict[str, Any],
    *,
    task_id: int,
) -> dict[str, Any]:
    """桌面后台任务线程使用的同步入口。"""

    return asyncio.run(run_wechat_publish(dict(payload), task_id=int(task_id)))
