# -*- coding: utf-8 -*-
"""用真实预检文稿生成两套公众号移动端阅读模板候选。"""

from __future__ import annotations

from html import escape
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app_core.oneclick_preflight import (  # noqa: E402
    _WECHAT_MOBILE_TEMPLATES,
    _wechat_markdown_to_html,
)


FIXTURE = ROOT / "preflight-fixtures" / "2026-07-30_WX-20260730-001-公众号图文预检测试"
OUTPUT = ROOT / "design-reviews" / "公众号移动阅读模板"


def _preview_page(title: str, theme_id: str, article_html: str) -> str:
    theme = _WECHAT_MOBILE_TEMPLATES[theme_id]
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{escape(theme["name"])}｜公众号移动阅读候选</title>
</head>
<body style="margin:0;background:#ecebe7;">
  <main style="box-sizing:border-box;width:min(100%,420px);margin:0 auto;padding:22px 14px 48px;background:#fff;">
    <header style="padding:10px 16px 26px;background:#fff;">
      <p style="margin:0 0 10px;color:#8a867f;font:13px/1.5 -apple-system,BlinkMacSystemFont,Segoe UI,PingFang SC,sans-serif;">
        公众号移动端候选 · {escape(theme["name"])}
      </p>
      <h1 style="margin:0;color:#2f2d2a;font:700 24px/1.42 -apple-system,BlinkMacSystemFont,Segoe UI,PingFang SC,sans-serif;">
        {escape(title)}
      </h1>
    </header>
    {article_html}
  </main>
</body>
</html>
"""


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    markdown = (FIXTURE / "正文.md").read_text(encoding="utf-8")
    title = "AI 越会接话，越别急着把它当成‘我已经想清楚了’"
    for theme_id, theme in _WECHAT_MOBILE_TEMPLATES.items():
        html = _preview_page(
            title,
            theme_id,
            _wechat_markdown_to_html(markdown, theme_id),
        )
        path = OUTPUT / f"{theme_id}_{theme['name']}.html"
        path.write_text(html, encoding="utf-8")
        print(path)


if __name__ == "__main__":
    main()
