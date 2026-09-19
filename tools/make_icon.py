#!/usr/bin/env python
"""生成 Mikasa 的应用图标（packaging/Mikasa.ico）——代码绘制的圆头吉祥物。

**为什么仍然用代码画**：图标是"改一个色值就重出一版"的东西，脚本即源码，
能 diff、能审查。2026-09-11 第一版（紫发动漫少女）按同样的理由画，但被判定
"太丑"。复盘下来问题不在"该不该用代码画"，而在**画法选错了**：

  1. 旧版走的是"半写实比例 + 大色块填充"：五官只占脸的一小块，缩到 32px 后
     只剩几个像素；头发却是一整块纯色多边形。于是整体读作"一坨紫 + 一张脸"。
  2. 新版改走 **chibi 吉祥物**路线：头占满画面、五官占比极大、深色描边勾轮廓。
     描边是关键——扁平色块在浅底上会糊成一片；有了描边，16px 下形状依旧成立。
  3. 主体从"人"换成**动物脸**：猫这类形象对"可爱"的下限更高，不依赖精细五官，
     任何尺寸都不容易崩；同时丢掉"五官必须画对"的包袱。
  4. 配色改用 DESIGN.md / style.css 的 :root（暖米 + 珊瑚 + 青），图标与界面
     同源——旧版的"品牌绿"在新设计系统里已经不存在了。

**四条硬约束**（前两条是第一版踩出来的坑，别再犯）：
  1. **超采样**：所有形状画在 SS 倍画布上再缩回。PIL 的圆弧/多边形没有抗锯齿，
     直接画出来的边缘全是台阶——这是"看着廉价"的头号原因。
  2. **坐标缩放只有一个入口**：构图一律写 0~1024 逻辑坐标，由 `P()` 统一缩放；
     线宽一律走 `sw()`。第一版一半函数过了缩放、一半没做，两组图形差一倍比例。
  3. **描边不用纯黑**：用暖棕色。纯黑描边在暖米底上又脏又硬。
  4. **曲线一律走 `smooth()`**：锚点 → Catmull-Rom 样条。直线段拼出来的脸有
     明显的"几何感"，柔不起来。

**不使用任何外部素材**：曾把用户给的参考图裁成图标，被明确否掉——"不是让你
把照片当图标"。故本脚本不读图片，`packaging/` 下也不放源图。

用法：
    python tools/make_icon.py                  # 默认猫猫版 → packaging/Mikasa.ico
    python tools/make_icon.py --variant bunny  # 换主体（cat / bunny / bear）
    python tools/make_icon.py --sheet          # 三个候选并排出一张对比图，不碰 ico
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT = REPO_ROOT / "packaging" / "Mikasa.ico"
SHOTS = REPO_ROOT / "tools" / "shots"
ICON_SIZES = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]

BASE = 1024  # 逻辑画布边长（最终缩到 256 存进 ico）
SS = 3  # 超采样倍数：3 倍时描边边缘才干净
W = BASE * SS

CORNER_RATIO = 0.22  # 圆角半径 / 边长

Pt = tuple[float, float]

# ---- 配色：与 DESIGN.md / style.css 的 :root 同源 ----
FUR = (255, 253, 250)  # 毛色：暖白（比画布 --bg 再亮一档，才浮得起来）
FUR_SHADE = (238, 219, 204)  # 下颌/耳根的内阴影
LINE = (74, 53, 44)  # 描边：暖棕，**不是纯黑**
EYE = (56, 40, 31)  # 眼：比描边再深一点，否则眼睛"陷"进轮廓里
PINK = (232, 138, 122)  # 鼻/内耳：--accent #cc785c 的粉化版
PINK_SOFT = (247, 186, 170)
BLUSH = (240, 150, 130)  # 腮红
TABBY = (222, 196, 176)  # 虎斑纹（低对比，小尺寸不会糊）
BG_TOP = (255, 245, 236)
BG_BOTTOM = (245, 201, 180)

# 备选主体换底色：同一只"暖白毛 + 珊瑚"的角色，只换站的地面
ALT_BG = {
    "bunny": ((236, 249, 244), (170, 218, 206)),  # 青底
    "bear": ((253, 243, 230), (233, 199, 165)),  # 沙底
}


def s(v: float) -> float:
    """逻辑坐标 → 画布坐标。"""
    return v * SS


def P(points: list[Pt]) -> list[Pt]:
    """把一组逻辑坐标点缩放到画布坐标（**所有形状的唯一入口**，见模块注释）。"""
    return [(s(x), s(y)) for x, y in points]


def sw(v: float) -> int:
    """线宽按同一比例缩放，否则超采样后线条细得看不见。"""
    return max(1, int(round(s(v))))


def smooth(points: list[Pt], steps: int = 16, closed: bool = True) -> list[Pt]:
    """Catmull-Rom 样条：几个锚点 → 一条过点的密集折线。

    这是"不显廉价"的第二根支柱：直接拿锚点连直线，脸型会是一圈折线；
    走样条后同样的锚点画出来是柔的。closed=False 用于画开放笔画（胡须、嘴）。
    """
    n = len(points)
    if n < 3:
        return list(points)

    def at(i: int) -> Pt:
        if closed:
            return points[i % n]
        return points[max(0, min(n - 1, i))]

    out: list[Pt] = []
    for i in range(n if closed else n - 1):
        p0, p1, p2, p3 = at(i - 1), at(i), at(i + 1), at(i + 2)
        for j in range(steps):
            t = j / steps
            t2 = t * t
            t3 = t2 * t
            out.append(
                (
                    0.5
                    * (
                        2 * p1[0]
                        + (-p0[0] + p2[0]) * t
                        + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2
                        + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3
                    ),
                    0.5
                    * (
                        2 * p1[1]
                        + (-p0[1] + p2[1]) * t
                        + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2
                        + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3
                    ),
                )
            )
    if not closed:
        out.append(points[-1])
    return out


def mirror(points: list[Pt]) -> list[Pt]:
    """左右镜像（锚点只写一半，另一半自动生成——不然脸会歪）。"""
    return [(BASE - x, y) for x, y in points]


def fill_shape(img: Image.Image, path: list[Pt], fill: tuple, width: float = 34) -> None:
    """填充 + 居中描边。描边单独走 `line`（而不是 polygon 的 outline），
    线宽才是沿路径两侧均分的，圆角处也不会断。"""
    d = ImageDraw.Draw(img)
    poly = P(path)
    d.polygon(poly, fill=fill)
    d.line(poly + [poly[0]], fill=LINE, width=sw(width), joint="curve")


def gradient_background(top: tuple, bottom: tuple) -> Image.Image:
    """竖向渐变底（上浅下深）。纯色底看着像占位图。

    用 1×H 的窄条再横向拉伸，而不是逐像素写满整个超采样画布——
    同样是渐变，后者在 3072² 上要跑九百多万次赋值。"""
    strip = Image.new("RGB", (1, W))
    px = strip.load()
    for y in range(W):
        t = y / (W - 1)
        px[0, y] = tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3))
    return strip.resize((W, W), Image.BILINEAR).convert("RGBA")


# ---- 头部轮廓：所有主体共用（换的是耳朵，不是脸） ----
HEAD = [
    (512, 236),
    (742, 268),
    (872, 470),
    (866, 664),
    (760, 818),
    (512, 882),
    (264, 818),
    (158, 664),
    (152, 470),
    (282, 268),
]
HEAD_PATH = smooth(HEAD, 20)


def draw_ears(img: Image.Image, variant: str) -> None:
    """耳朵画在头**之前**：耳根被脸盖住才像长在头上，而不是贴了两片纸。"""
    d = ImageDraw.Draw(img)
    if variant == "bear":
        for sign in (-1, 1):
            cx = 512 + sign * 262
            d.ellipse(
                [s(cx - 128), s(184), s(cx + 128), s(440)], fill=FUR, outline=LINE, width=sw(34)
            )
            d.ellipse([s(cx - 62), s(250), s(cx + 62), s(374)], fill=PINK_SOFT)
        return

    if variant == "bunny":
        outer = [(330, 520), (250, 300), (262, 128), (330, 84), (404, 122), (424, 300), (426, 520)]
        inner = [(332, 470), (296, 306), (306, 178), (338, 146), (382, 176), (396, 306), (398, 470)]
    else:  # cat
        outer = [(248, 486), (238, 232), (322, 88), (438, 268)]
        inner = [(298, 430), (300, 250), (330, 176), (382, 268)]

    for sign in (-1, 1):
        pts = outer if sign == 1 else mirror(outer)
        fill_shape(img, smooth(pts, 18), FUR)
        ip = inner if sign == 1 else mirror(inner)
        ImageDraw.Draw(img).polygon(P(smooth(ip, 18)), fill=PINK_SOFT)


def draw_head(img: Image.Image) -> None:
    """脸底色 + 下颌柔光。柔光单独成层再模糊 + 按脸型裁剪：
    不裁剪的话模糊会溢到背景上，读作"一圈脏雾"。"""
    fill_shape(img, HEAD_PATH, FUR)

    # 阴影给得很克制：第一版给到 alpha 210 + 46px 模糊，脸的下半部糊成一片灰，
    # 32px 下整只猫读作"脏"。现在只留贴近下颌的一层暖调暗部，用来托住体积。
    layer = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    ImageDraw.Draw(layer).ellipse([s(170), s(800), s(854), s(1090)], fill=(*FUR_SHADE, 78))
    layer = layer.filter(ImageFilter.GaussianBlur(s(70)))

    mask = Image.new("L", (W, W), 0)
    ImageDraw.Draw(mask).polygon(P(HEAD_PATH), fill=255)
    layer.putalpha(Image.composite(layer.getchannel("A"), Image.new("L", (W, W), 0), mask))
    img.alpha_composite(layer)


def draw_tabby(d: ImageDraw.ImageDraw) -> None:
    """额头虎斑：三短竖。低对比、不描边——大尺寸下给出"这是猫"的第三重线索
    （前两重是耳朵和胡须），小尺寸下自己糊掉，不会变成噪点。"""
    for dx in (-96, 0, 96):
        d.line(
            P(smooth([(512 + dx, 318), (512 + dx - 4, 366), (512 + dx, 412)], 8, closed=False)),
            fill=TABBY,
            width=sw(26),
            joint="curve",
        )


def draw_eyes(img: Image.Image) -> None:
    """一双大眼：深色底 + 两点高光。

    眼睛是"可爱"的第一权重——占比给足（高度接近脸的 1/3），高光不能省：
    没有高光的纯黑眼睛立刻变"死鱼眼"。"""
    d = ImageDraw.Draw(img)
    w, h = 178, 212
    cy = 566
    for sign in (-1, 1):
        cx = 512 + sign * 168
        d.ellipse([s(cx - w / 2), s(cy - h / 2), s(cx + w / 2), s(cy + h / 2)], fill=EYE)
        d.ellipse(
            [s(cx - w * 0.34), s(cy - h * 0.34), s(cx - w * 0.02), s(cy - h * 0.02)],
            fill=(255, 255, 255),
        )
        d.ellipse(
            [s(cx + w * 0.10), s(cy + h * 0.12), s(cx + w * 0.30), s(cy + h * 0.32)],
            fill=(255, 255, 255, 205),
        )


def draw_muzzle(img: Image.Image, variant: str) -> None:
    """鼻 + 嘴。两段弧拼成一个 "ω"：这是猫脸的通用写法，
    比"一条横线"多花两分钟，可爱度差一个量级。"""
    d = ImageDraw.Draw(img)
    if variant == "bear":
        # 吻部：比脸再白一档的椭圆，熊的五官才有"凸出来"的层次
        d.ellipse([s(512 - 168), s(596), s(512 + 168), s(800)], fill=(255, 255, 255))
        d.ellipse([s(512 - 52), s(634), s(512 + 52), s(716)], fill=LINE)
        arc_y = 716
    else:
        d.polygon(P(smooth([(462, 628), (562, 628), (512, 696)], 14)), fill=PINK)
        arc_y = 676

    r = 42
    for sign in (-1, 1):
        cx = 512 + sign * (r - 4)
        d.arc(
            [s(cx - r), s(arc_y - r + 8), s(cx + r), s(arc_y + r * 1.6)],
            start=0,
            end=180,
            fill=LINE,
            width=sw(13),
        )


def draw_whiskers(img: Image.Image) -> None:
    """胡须：只画脸外侧的部分，从脸边一直伸到背景上。

    刻意留细——它是"猫"的第三重线索，只在 64px 以上起作用，
    小尺寸下融进背景即可，不需要为它单独优化。"""
    layer = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    strokes = [
        [(336, 620), (250, 594), (196, 580)],
        [(330, 682), (238, 678), (188, 676)],
        [(342, 744), (254, 768), (204, 784)],
    ]
    for sign in (-1, 1):
        for stroke in strokes:
            pts = stroke if sign == 1 else mirror(stroke)
            d.line(
                P(smooth(pts, 12, closed=False)),
                fill=(*LINE, 132),
                width=sw(15),
                joint="curve",
            )
    img.alpha_composite(layer)


def draw_blush() -> Image.Image:
    """腮红：模糊过的粉色块。清晰边缘会像"贴了两片红纸"。"""
    layer = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for sign in (-1, 1):
        cx = 512 + sign * 268
        d.ellipse([s(cx - 66), s(668), s(cx + 66), s(730)], fill=(*BLUSH, 130))
    return layer.filter(ImageFilter.GaussianBlur(s(26)))


def draw_icon(variant: str = "cat") -> Image.Image:
    """画完整图标。分层顺序即遮挡关系，不能随便调：
    底 → 耳 → 脸 → 虎斑 → 五官 → 腮红 → 胡须。"""
    top, bottom = ALT_BG.get(variant, (BG_TOP, BG_BOTTOM))
    img = gradient_background(top, bottom)
    img.putalpha(255)

    draw_ears(img, variant)
    draw_head(img)
    if variant == "cat":
        draw_tabby(ImageDraw.Draw(img))
    draw_eyes(img)
    draw_muzzle(img, variant)
    img.alpha_composite(draw_blush())
    if variant == "cat":
        draw_whiskers(img)

    return img.resize((BASE, BASE), Image.LANCZOS)


def rounded(img: Image.Image) -> Image.Image:
    """圆角遮罩。在超采样尺寸上做再缩回，否则 16/32px 的圆角全是锯齿。"""
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, img.width - 1, img.height - 1), radius=int(img.width * CORNER_RATIO), fill=255
    )
    out = img.convert("RGBA")
    out.putalpha(mask)
    return out


def render(variant: str) -> Image.Image:
    return rounded(draw_icon(variant))


def write_sheet() -> Path:
    """三个候选并排：上排 256px 实际观感，下排 32px 放大 8 倍（**最近邻**，
    保留真实像素块）。下排才是决定生死的那个——图标大多数时候就这么小。"""
    variants = ["cat", "bunny", "bear"]
    cell = 256
    pad = 24
    sheet = Image.new(
        "RGB",
        (cell * len(variants) + pad * (len(variants) + 1), cell * 2 + pad * 3),
        (255, 255, 255),
    )
    for i, name in enumerate(variants):
        img = render(name)
        x = pad + i * (cell + pad)
        sheet.paste(img.resize((cell, cell), Image.LANCZOS).convert("RGB"), (x, pad))
        small = (
            img.resize((32, 32), Image.LANCZOS).resize((cell, cell), Image.NEAREST).convert("RGB")
        )
        sheet.paste(small, (x, pad * 2 + cell))
    path = SHOTS / "icon-variants.png"
    SHOTS.mkdir(parents=True, exist_ok=True)
    sheet.save(path)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 Mikasa 应用图标")
    parser.add_argument("--variant", choices=["cat", "bunny", "bear"], default="cat")
    parser.add_argument("--sheet", action="store_true", help="只出候选对比图，不覆盖 ico")
    args = parser.parse_args()

    if args.sheet:
        print(f"候选对比图：{write_sheet().relative_to(REPO_ROOT)}")
        return

    img = render(args.variant)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT, format="ICO", sizes=ICON_SIZES)

    SHOTS.mkdir(parents=True, exist_ok=True)
    img.resize((256, 256), Image.LANCZOS).save(SHOTS / "icon-preview.png")
    # 32px 实际观感（放大 8 倍）：图标被看到时大多这么小，必须单独检查
    img.resize((32, 32), Image.LANCZOS).resize((256, 256), Image.NEAREST).save(
        SHOTS / "icon-32px.png"
    )
    rel = OUT.relative_to(REPO_ROOT)
    print(
        f"图标已生成：{rel}（{args.variant}，{OUT.stat().st_size} 字节，{len(ICON_SIZES)} 种尺寸）"
    )


if __name__ == "__main__":
    main()
