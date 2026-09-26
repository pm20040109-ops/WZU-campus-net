"""生成校园网登录工具的 .ico 图标。多分辨率 PNG → 单文件 .ico。"""
import os
import sys

from PIL import Image, ImageDraw


def make_image(size: int) -> Image.Image:
    # RGBA 画布，蓝色径向渐变背景
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 圆角矩形背景（蓝色渐变简化为两种蓝）
    radius = max(2, size // 6)
    pad = max(1, size // 32)
    bg_top = (54, 130, 230)   # 顶 #3682E6
    bg_bot = (32, 86, 174)    # 底 #2056AE

    # 简单实现：从上到下画一条条横线近似渐变
    for y in range(size):
        t = y / max(1, size - 1)
        r = int(bg_top[0] * (1 - t) + bg_bot[0] * t)
        g = int(bg_top[1] * (1 - t) + bg_bot[1] * t)
        b = int(bg_top[2] * (1 - t) + bg_bot[2] * t)
        draw.line([(0, y), (size, y)], fill=(r, g, b, 255))

    # 把四个角裁成圆角
    mask = Image.new("L", (size, size), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle((pad, pad, size - pad - 1, size - pad - 1),
                         radius=radius, fill=255)
    bg = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    bg.paste(img, (0, 0), mask)
    img = bg

    # 在中央画一个 WiFi 信号 + 校园建筑的小符号
    # 三层 WiFi 弧 + 一个圆点
    cx = size / 2
    cy = size * 0.62  # 圆点位置偏下，让弧线在上
    arc_color = (255, 255, 255, 235)
    dot_color = (255, 255, 255, 255)

    s = max(1, size / 64.0)

    # 最外弧
    draw_arc(img, cx, cy, 0.45, arc_color, max(2, int(2.0 * s)))
    # 中弧
    draw_arc(img, cx, cy, 0.30, arc_color, max(2, int(2.0 * s)))
    # 内弧
    draw_arc(img, cx, cy, 0.16, arc_color, max(2, int(2.0 * s)))

    # 中心圆点
    r = max(2, int(2.2 * s))
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=dot_color)

    return img


def draw_arc(img: Image.Image, cx: float, cy: float,
             ratio: float, color, width: int):
    """画一段从左上到右上的 WiFi 弧（180° 的上半部分）。"""
    size = img.size[0]
    r = size * ratio
    d = ImageDraw.Draw(img)
    # 弧线从 (cx-r, cy) 到 (cx+r, cy) 经过 (cx, cy-r) 上半圆
    bbox = (cx - r, cy - r, cx + r, cy + r)
    d.arc(bbox, start=180, end=360, fill=color, width=width)


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "campus.ico"
    sizes = [16, 24, 32, 48, 64, 128, 256]
    imgs = [make_image(s) for s in sizes]
    # ICO 格式：Pillow 把最大尺寸作为源，自动生成其它尺寸的 PNG 子帧嵌入
    imgs[-1].save(out, format="ICO", sizes=[(s, s) for s in sizes])
    print("wrote", out, os.path.getsize(out), "bytes")


if __name__ == "__main__":
    main()
