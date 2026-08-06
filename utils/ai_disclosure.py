# -*- coding: utf-8 -*-
"""国内平台 AI 生成内容声明辅助。"""

AI_DISCLOSURE_TEXT = "本内容包含AI生成画面与配音。"

NATIVE_AI_DISCLOSURE_LABELS = {
    1: "笔记含AI合成内容",
    3: "内容由AI生成",
    4: "内容为AI生成",
    5: "含AI生成内容",
}

FALLBACK_AI_DISCLOSURE_PLATFORMS = {2}


def ensure_ai_disclosure_text(text: str | None) -> str:
    """为没有网页原生声明控件的平台补充可见披露。"""

    normalized = str(text or "").strip()
    if AI_DISCLOSURE_TEXT in normalized:
        return normalized
    return "\n".join(part for part in (normalized, AI_DISCLOSURE_TEXT) if part)


async def open_control_until_option_visible(page, trigger_text: str, option_text: str) -> bool:
    """逐层点击可见触发节点，直到目标选项真正显示。"""

    return bool(
        await page.evaluate(
            """async ({ triggerText, optionText }) => {
                const normalize = value => String(value || '').replace(/\\s+/g, ' ').trim();
                const visible = node => {
                    if (!(node instanceof HTMLElement)) return false;
                    const rect = node.getBoundingClientRect();
                    const style = getComputedStyle(node);
                    return rect.width > 0 && rect.height > 0
                        && style.display !== 'none'
                        && style.visibility !== 'hidden';
                };
                const optionVisible = () => Array.from(document.querySelectorAll('body *'))
                    .some(node => visible(node) && normalize(node.innerText || node.textContent) === optionText);
                if (optionVisible()) return true;

                const triggers = Array.from(document.querySelectorAll('body *'))
                    .filter(node => visible(node) && normalize(node.innerText || node.textContent) === triggerText)
                    .sort((left, right) => left.childElementCount - right.childElementCount);
                for (const trigger of triggers) {
                    let candidate = trigger;
                    for (let depth = 0; candidate && depth < 6; depth += 1, candidate = candidate.parentElement) {
                        candidate.scrollIntoView({ block: 'center', inline: 'nearest' });
                        candidate.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
                        candidate.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true }));
                        candidate.click();
                        await new Promise(resolve => setTimeout(resolve, 350));
                        if (optionVisible()) return true;
                    }
                }
                return false;
            }""",
            {"triggerText": trigger_text, "optionText": option_text},
        )
    )


async def click_visible_text_option(page, option_text: str) -> bool:
    """点击目标文案对应的可见选项容器。"""

    return bool(
        await page.evaluate(
            """optionText => {
                const normalize = value => String(value || '').replace(/\\s+/g, ' ').trim();
                const visible = node => {
                    if (!(node instanceof HTMLElement)) return false;
                    const rect = node.getBoundingClientRect();
                    const style = getComputedStyle(node);
                    return rect.width > 0 && rect.height > 0
                        && style.display !== 'none'
                        && style.visibility !== 'hidden';
                };
                const candidates = Array.from(document.querySelectorAll('body *'))
                    .filter(node => visible(node) && normalize(node.innerText || node.textContent) === optionText)
                    .sort((left, right) => left.childElementCount - right.childElementCount);
                const textNode = candidates[0];
                if (!textNode) return false;
                const clickable = textNode.closest(
                    '[role="option"], li, .ant-select-item-option, .d-option, .item, [class*="option"]'
                ) || textNode;
                clickable.scrollIntoView({ block: 'center', inline: 'nearest' });
                clickable.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
                clickable.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true }));
                clickable.click();
                return true;
            }""",
            option_text,
        )
    )
