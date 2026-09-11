#!/usr/bin/env python
"""生成 Mikasa 的应用图标（packaging/Mikasa.ico）。

为什么用代码画而不是塞一张图片（与 sample-corpus 的二进制的同一套理由）：
图片没法 diff/审查，而图标是"改一个色值就重出一版"的东西——脚本即源码。

设计（与 Web UI 一套配色，见 static/css/style.css 的 :root）：
  - 深色圆角底 #0f1117 + 品牌绿描边 #3fb950（暗色仪表盘气质）
  - 三条浅色横线 = 文档正文
  - 右下角绿色实心圆 = 引用标记（Mikasa 的核心特性：每个结论可回溯到出处）

用法：python tools/make_icon.py   （输出 packaging/Mikasa.ico，幂等覆盖）
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT = REPO_ROOT / "packaging" / "Mikasa.ico"

BG = (15, 17, 23, 255)  # --bg
GREEN = (63, 185, 80, 255)  # --green
TEXT = (230, 237, 243, 255)  # --text
MUTED = (139, 148, 158, 255)  # --muted

# 大尺寸画一次再缩小：小图标（16px）直接用大图降采样，比按比例重画更清晰
BASE = 512


def draw_icon() -> Image.Image:
    img = Image.new("RGBA", (BASE, BASE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 圆角底 + 品牌绿描边
    pad = BASE * 0.06
    d.rounded_rectangle(
        [pad, pad, BASE - pad, BASE - pad],
        radius=BASE * 0.22,
        fill=BG,
        outline=GREEN,
        width=int(BASE * 0.028),
    )

    # 文档正文：三条横线，宽度递减（像一段段被引用的话）
    x0, x1 = BASE * 0.24, BASE * 0.76
    line_h = BASE * 0.052
    ys = [BASE * 0.34, BASE * 0.47, BASE * 0.60]
    widths = [1.0, 0.82, 0.92]
    for y, w in zip(ys, widths, strict=True):
        d.rounded_rectangle(
            [x0, y, x0 + (x1 - x0) * w, y + line_h],
            radius=line_h / 2,
            fill=TEXT if w == 1.0 else MUTED,
        )

    # 引用标记：右下角实心圆 + 中心小方孔（像 [n] 角标）
    cx, cy, r = BASE * 0.72, BASE * 0.74, BASE * 0.11
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=GREEN)
    inner = r * 0.42
    d.rounded_rectangle(
        [cx - inner, cy - inner, cx + inner, cy + inner],
        radius=inner * 0.35,
        fill=BG,
    )
    return img


def main() -> None:
    img = draw_icon()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    # 多尺寸 ICO：Windows 会按显示场景挑合适的一档（桌面 48/任务栏 32/资源管理器 16）
    sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
    img.save(OUT, format="ICO", sizes=sizes)
    print(f"图标已生成：{OUT.relative_to(REPO_ROOT)}（{OUT.stat().st_size} 字节，{len(sizes)} 种尺寸）")


if __name__ == "__main__":
    main()
