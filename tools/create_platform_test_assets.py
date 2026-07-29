# -*- coding: utf-8 -*-
"""生成仅用于一键发预检的本地测试素材，不包含真实业务内容。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "demo-runtime" / "test-assets" / "20260729-platform-preflight"
FONT_PATH = Path("/System/Library/Fonts/STHeiti Medium.ttc")


def font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_PATH), size=size)


def draw_centered(draw: ImageDraw.ImageDraw, text: str, y: int, size: int, color: str) -> None:
    value = font(size)
    box = draw.textbbox((0, 0), text, font=value)
    draw.text(((1080 - (box[2] - box[0])) / 2, y), text, font=value, fill=color)


def poster(path: Path, title: str, subtitle: str, accent: str) -> None:
    image = Image.new("RGB", (1080, 1440), "#F6F8FB")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((80, 120, 1000, 1320), radius=54, fill="#FFFFFF", outline="#D6DFEC", width=4)
    draw.rounded_rectangle((128, 185, 250, 307), radius=30, fill=accent)
    draw.ellipse((165, 222, 213, 270), fill="#FFFFFF")
    draw_centered(draw, "一键发", 410, 82, "#12233E")
    draw_centered(draw, title, 548, 64, "#12233E")
    draw_centered(draw, subtitle, 660, 38, "#60708B")
    draw.rounded_rectangle((156, 1015, 924, 1125), radius=30, fill="#FFF4E8")
    draw_centered(draw, "仅作功能预检，不公开发布", 1041, 36, "#B45309")
    draw_centered(draw, "2026-07-29", 1210, 32, "#8A99AE")
    image.save(path, quality=95)


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    posters = {
        "xhs-image-01.png": ("小红书图文测试", "图片序列 · 标题 · 正文", "#E94362"),
        "xhs-image-02.png": ("素材上传验证", "封面与平台字段", "#7856E8"),
        "wechat-cover.png": ("公众号图文测试", "封面 · 标题 · 正文", "#07A17B"),
    }
    for name, (title, subtitle, accent) in posters.items():
        poster(OUTPUT / name, title, subtitle, accent)

    video = OUTPUT / "xhs-video-preflight.mp4"
    command = [
        "ffmpeg", "-y", "-loop", "1", "-i", str(OUTPUT / "xhs-image-01.png"),
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-t", "3", "-r", "30", "-vf", "scale=1080:1440,format=yuv420p",
        "-c:v", "libx264", "-c:a", "aac", "-shortest", str(video),
    ]
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(OUTPUT)


if __name__ == "__main__":
    main()
