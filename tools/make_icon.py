#!/usr/bin/env python
"""生成 Mikasa 的应用图标（packaging/Mikasa.ico）——代码绘制的紫发动漫少女。

**为什么坚持用代码画**：图标是"改一个色值就重出一版"的东西，脚本即源码，
能 diff、能审查；塞一张位图则谁也说不清它从哪来、改了什么。2026-09-11 用户明确
要求"照那个风格你自己生成一个"之后，这条理由更加成立。

**画风的四个硬约束**（都是踩坑换来的）：
  1. **超采样**：所有形状画在 SS 倍画布上再缩回。PIL 的圆弧/多边形没有抗锯齿，
     直接画出来的边缘全是台阶——这是"看着廉价"的头号原因。
  2. **坐标缩放只有一个入口**：构图一律写 0~1024 的逻辑坐标，由 `P()` 统一缩放。
     第一版一半函数过了 `s()`、一半直接写逻辑坐标，两组图形差一倍比例，画出来
     头发缩在左上角、五官飘在中间。（同理线宽一律走 `sw()`。）
  3. **描边不用纯黑**：用偏紫深色。纯黑描边在浅色底上又脏又硬。
  4. **女性特征要明确**（初版被认成孙悟空、整改版又被说"像个男的"）：大眼 +
     上睫毛外挑 + 圆脸收下颌 + 侧发垂落 + 腮红 + 发饰，一个都不能省。

**不使用任何外部素材**：曾把用户给的参考图裁成图标，用户明确否掉——"不是让你
把照片当图标"。故本脚本不读图片，`packaging/` 下也不放源图。

用法：python tools/make_icon.py   （输出 packaging/Mikasa.ico，幂等覆盖）
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT = REPO_ROOT / "packaging" / "Mikasa.ico"
ICON_SIZES = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]

BASE = 1024  # 逻辑画布边长（最终缩到 256 存进 ico）
SS = 2  # 超采样倍数
W = BASE * SS

# ---- 配色：紫发（用户指定）+ 品牌绿 ----
BG_TOP = (238, 252, 248)
BG_BOTTOM = (203, 233, 232)
HAIR = (146, 108, 205)
HAIR_DARK = (92, 62, 145)
HAIR_LIGHT = (206, 184, 250)
SKIN = (255, 233, 223)
SKIN_SHADE = (238, 198, 196)
LINE = (78, 56, 108)
IRIS_TOP = (82, 62, 150)
IRIS_BOTTOM = (156, 200, 250)
PUPIL = (48, 34, 84)
BLUSH = (255, 143, 168)
CLOTH = (76, 60, 118)  # 衣领/肩：与头发区分开，别用同色
GREEN = (63, 185, 80)

CORNER_RATIO = 0.22  # 圆角半径 / 边长

Pt = tuple[float, float]


def s(v: float) -> float:
    """逻辑坐标 → 画布坐标。"""
    return v * SS


def P(points: list[Pt]) -> list[Pt]:
    """把一组逻辑坐标点缩放到画布坐标（**所有形状的唯一入口**，见模块注释）。"""
    return [(s(x), s(y)) for x, y in points]


def sw(v: float) -> int:
    """线宽按同一比例缩放，否则超采样后线条细得看不见。"""
    return max(1, int(round(s(v))))


def bezier(points: list[Pt], steps: int = 24) -> list[Pt]:
    """德卡斯特里奥求值：控制点 → 平滑折线。

    头发与脸型用直线段会显得生硬（前一版"几何感"的来源），要弧才柔和。
    """
    result: list[Pt] = []
    n = len(points) - 1
    for i in range(steps + 1):
        t = i / steps
        pts = list(points)
        for _ in range(n):
            pts = [
                (pts[j][0] * (1 - t) + pts[j + 1][0] * t, pts[j][1] * (1 - t) + pts[j + 1][1] * t)
                for j in range(len(pts) - 1)
            ]
        result.append(pts[0])
    return result


def gradient_background() -> Image.Image:
    """竖向渐变底（上浅下深）。纯色底看着像占位图。"""
    img = Image.new("RGB", (W, W))
    px = img.load()
    for y in range(W):
        t = y / (W - 1)
        row = tuple(int(BG_TOP[i] * (1 - t) + BG_BOTTOM[i] * t) for i in range(3))
        for x in range(W):
            px[x, y] = row
    return img.convert("RGBA")


def face_outline() -> list[Pt]:
    """脸型：上半圆润、下颌收尖。

    直接用椭圆会得到"饼脸"——那是第一版显呆的原因之一。
    """
    cx, top, bottom = 512, 286, 838
    half = 252  # 脸宽约占画面一半：脸小发大就会变成"一坨紫配张脸"
    left = bezier(
        [
            (cx - half * 0.18, top),
            (cx - half, top + 150),
            (cx - half, bottom - 250),
            (cx - half * 0.46, bottom - 58),
        ]
    )
    chin = bezier(
        [
            (cx - half * 0.46, bottom - 58),
            (cx - half * 0.21, bottom),
            (cx + half * 0.21, bottom),
            (cx + half * 0.46, bottom - 58),
        ]
    )
    right = bezier(
        [
            (cx + half * 0.46, bottom - 58),
            (cx + half, bottom - 250),
            (cx + half, top + 150),
            (cx + half * 0.18, top),
        ]
    )
    return left + chin + right


def draw_back_hair(img: Image.Image) -> None:
    """后发 + 衣领。

    头发只比脸宽一圈（"包边"），不是一大坨——体量给多了脸就被吞掉。
    底部加衣领肩线，否则脑袋悬空、整体读作"一坨紫"。
    """
    d = ImageDraw.Draw(img)
    cx = 512
    # 肩/衣领：先画（要被头发压住上缘）
    d.polygon(
        P(
            bezier(
                [
                    (cx - 470, 1024),
                    (cx - 430, 950),
                    (cx - 210, 892),
                    (cx, 884),
                    (cx + 210, 892),
                    (cx + 430, 950),
                    (cx + 470, 1024),
                ],
                40,
            )
        ),
        fill=CLOTH,
    )
    # 后发轮廓：贴着头的圆拱，向下垂到画布外（读作长发）
    d.polygon(
        P(
            bezier(
                [
                    (cx - 372, 1024),
                    (cx - 396, 640),
                    (cx - 344, 300),
                    (cx, 224),
                    (cx + 344, 300),
                    (cx + 396, 640),
                    (cx + 372, 1024),
                ],
                60,
            )
        ),
        fill=HAIR_DARK,
    )
    # 两侧垂发：贴在脸旁、搭到肩上——**女性轮廓的关键**（短发会变"男的"）
    for sign in (-1, 1):
        d.polygon(
            P(
                bezier(
                    [
                        (cx + sign * 250, 470),
                        (cx + sign * 352, 660),
                        (cx + sign * 336, 880),
                        (cx + sign * 300, 1024),
                        (cx + sign * 214, 1024),
                        (cx + sign * 232, 800),
                        (cx + sign * 224, 560),
                    ],
                    40,
                )
            ),
            fill=HAIR,
        )


def draw_face(img: Image.Image) -> None:
    """脸底色 + 下颌柔光。

    柔光单独画一层再做高斯模糊：PIL 没有"柔边填充"，直接画半透明实色会留硬边。
    """
    ImageDraw.Draw(img).polygon(P(face_outline()), fill=SKIN)
    shade = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    ImageDraw.Draw(shade).ellipse([s(318), s(738), s(706), s(858)], fill=(*SKIN_SHADE, 120))
    img.alpha_composite(shade.filter(ImageFilter.GaussianBlur(s(32))))


def hair_shadow_on_face() -> Image.Image:
    """刘海在额头压出的那点阴影。

    刻意画得**很淡很浅**：第一版给到 alpha 95 + 26px 模糊，结果是一块灰紫色
    糊在眉眼上方，看着像脏（2026-09-11 实测）。这里只要一丝暗度提示体积。
    """
    layer = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    ImageDraw.Draw(layer).polygon(
        P(bezier([(300, 330), (512, 452), (724, 330), (724, 260), (300, 260)], 40)),
        fill=(*HAIR_DARK, 42),
    )
    return layer.filter(ImageFilter.GaussianBlur(s(20)))


def draw_eye(img: Image.Image, cx: float, cy: float, w: float, h: float, sign: int) -> None:
    """一只动漫眼：眼白 → 虹膜渐变 → 瞳孔 → 两点高光 → 上眼睑 → 睫毛 → 下睑。

    顺序不能换（后画的压先画的）；虹膜渐变必须单独成层——PIL 没法在已画好的
    形状上按行改色。
    """
    box = [s(cx - w / 2), s(cy - h / 2), s(cx + w / 2), s(cy + h / 2)]
    ImageDraw.Draw(img).ellipse(
        [box[0], box[1] + s(h * 0.10), box[2], box[3] - s(h * 0.06)], fill=(253, 251, 255)
    )
    iris = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    ImageDraw.Draw(iris).ellipse(
        [s(cx - w * 0.40), s(cy - h * 0.40), s(cx + w * 0.40), s(cy + h * 0.36)],
        fill=IRIS_BOTTOM,
    )
    idraw = iris.load()
    y0, y1 = int(s(cy - h * 0.40)), int(s(cy + h * 0.36))
    for y in range(y0, max(y0 + 1, y1)):
        t = (y - y0) / max(1, y1 - y0)
        row = tuple(int(IRIS_TOP[i] * (1 - t) + IRIS_BOTTOM[i] * t) for i in range(3))
        for x in range(int(s(cx - w * 0.42)), int(s(cx + w * 0.42))):
            if idraw[x, y][3] > 0:
                idraw[x, y] = (*row, 255)
    img.alpha_composite(iris)

    d = ImageDraw.Draw(img)  # 合成过像素，重新取
    d.ellipse([s(cx - w * 0.17), s(cy - h * 0.19), s(cx + w * 0.17), s(cy + h * 0.22)], fill=PUPIL)
    # 两点高光是动漫眼的标志，缺了立刻变"死鱼眼"
    d.ellipse(
        [s(cx - w * 0.28), s(cy - h * 0.32), s(cx - w * 0.03), s(cy - h * 0.07)],
        fill=(255, 255, 255, 248),
    )
    d.ellipse(
        [s(cx + w * 0.06), s(cy + h * 0.08), s(cx + w * 0.21), s(cy + h * 0.22)],
        fill=(255, 255, 255, 190),
    )
    d.arc(box, start=188, end=352, fill=LINE, width=sw(18))  # 上眼睑：粗
    # 睫毛：外眼角上挑的一根（女性特征的强信号）
    ox = cx + sign * w * 0.48
    d.polygon(
        P(
            bezier(
                [
                    (ox, cy - h * 0.18),
                    (ox + sign * w * 0.34, cy - h * 0.54),
                    (ox + sign * w * 0.22, cy - h * 0.14),
                ],
                16,
            )
        ),
        fill=LINE,
    )
    # 下睑：细（画粗了下眼睑会显凶）
    d.arc(
        [box[0] + s(w * 0.10), box[1] + s(h * 0.16), box[2] - s(w * 0.10), box[3]],
        start=20,
        end=160,
        fill=LINE,
        width=sw(5),
    )


def draw_face_features(img: Image.Image) -> None:
    # 眼位约在脸高的 52% 处：太低显幼、太高显凶
    draw_eye(img, 512 - 146, 576, 152, 182, -1)
    draw_eye(img, 512 + 146, 576, 152, 182, 1)
    d = ImageDraw.Draw(img)
    # 鼻：一个极小的暗点（动漫脸几乎不画鼻子，画大了立刻变写实）
    d.ellipse([s(504), s(664), s(520), s(678)], fill=SKIN_SHADE)
    # 嘴：一小段上弯弧
    d.arc([s(486), s(696), s(538), s(740)], start=15, end=165, fill=LINE, width=sw(10))


def draw_blush() -> Image.Image:
    """腮红：模糊过的粉色块。清晰边缘会像"贴了两片红纸"。"""
    layer = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    for sign in (-1, 1):
        cx = 512 + sign * 208
        ld.ellipse([s(cx - 72), s(668), s(cx + 72), s(716)], fill=(*BLUSH, 130))
    return layer.filter(ImageFilter.GaussianBlur(s(24)))


def draw_bangs(img: Image.Image) -> None:
    """前发：刘海 + 两侧垂发，**一个整块形状** + 头顶一道高光带。

    试过把刘海拆成一缕缕"毛发"（每缕独立多边形、深浅交错），结果是每缕都成了
    方块或触角，比整块更糟。**纯几何画法里"加细节"常常等于"加错误"**——可控的
    路子是简洁：一个轮廓干净的整体形状 + 一处高光，形状本身就够读作头发了
    （2026-09-11 两版实测）。
    """
    ImageDraw.Draw(img).polygon(
        P(
            bezier(
                [
                    (176, 660),  # 左下：垂发末端
                    (156, 300),  # 左侧：贴脸垂下
                    (332, 172),  # 左上头顶
                    (512, 152),  # 头顶
                    (692, 172),
                    (868, 300),
                    (848, 660),  # 右下
                    (806, 566),  # 下缘右端
                    (736, 468),  # 刘海右缘收进去，露出脸颊
                    (688, 534),  # 刘海尖 1
                    (614, 424),
                    (560, 516),  # 刘海尖 2（中间略短，避免正中一个尖＝头盔）
                    (512, 414),
                    (462, 516),  # 刘海尖 3
                    (408, 424),
                    (334, 534),  # 刘海尖 4
                    (286, 468),
                    (216, 566),  # 下缘左端
                ],
                90,
            )
        ),
        fill=HAIR,
    )
    # 头顶高光带：动漫头发的标志性处理，没有它头发是一坨死色
    shine = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    ImageDraw.Draw(shine).polygon(
        P(bezier([(288, 296), (512, 236), (736, 296), (708, 362), (512, 296), (316, 362)], 40)),
        fill=(*HAIR_LIGHT, 200),
    )
    img.alpha_composite(shine.filter(ImageFilter.GaussianBlur(s(9))))


def draw_ornament(d: ImageDraw.ImageDraw) -> None:
    """发饰：一枚品牌绿的小星星。

    两重考虑：① 纯色头发在 32px 下是一块紫，需要一处亮点提神；
    ② 绿色呼应产品品牌色（引用标记同色）。
    """
    cx, cy, r = 292, 372, 58
    pts: list[Pt] = []
    for i in range(10):
        ang = -math.pi / 2 + i * math.pi / 5
        rad = r if i % 2 == 0 else r * 0.44
        pts.append((cx + rad * math.cos(ang), cy + rad * math.sin(ang)))
    d.polygon(P(pts), fill=GREEN)


def draw_icon() -> Image.Image:
    """画完整图标。分层顺序即遮挡关系，不能随便调：
    底 → 后发 → 脸 → 额头阴影 → 五官 → 腮红 → 刘海 → 发饰 → 脸部轮廓。
    """
    img = gradient_background()
    img.putalpha(255)

    draw_back_hair(img)
    draw_face(img)
    img.alpha_composite(hair_shadow_on_face())
    draw_face_features(img)
    img.alpha_composite(draw_blush())
    draw_bangs(img)

    orn = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    draw_ornament(ImageDraw.Draw(orn))
    img.alpha_composite(orn)  # 发饰要压在刘海之上

    # 脸部极淡的一圈轮廓：小尺寸下没有它，脸和头发会糊成一片
    edge = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    ImageDraw.Draw(edge).polygon(P(face_outline()), outline=(*LINE, 72), width=sw(4))
    img.alpha_composite(edge)

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


def main() -> None:
    argparse.ArgumentParser(description="生成 Mikasa 应用图标").parse_args()

    img = rounded(draw_icon())
    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT, format="ICO", sizes=ICON_SIZES)

    shots = REPO_ROOT / "tools" / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    img.resize((256, 256), Image.LANCZOS).save(shots / "icon-preview.png")
    # 32px 实际观感（放大 8 倍）：图标被看到时大多这么小，必须单独检查
    img.resize((32, 32), Image.LANCZOS).resize((256, 256), Image.NEAREST).save(
        shots / "icon-32px.png"
    )
    rel = OUT.relative_to(REPO_ROOT)
    print(f"图标已生成：{rel}（{OUT.stat().st_size} 字节，{len(ICON_SIZES)} 种尺寸）")


if __name__ == "__main__":
    main()
