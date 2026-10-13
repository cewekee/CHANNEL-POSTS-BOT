"""Отрисовка картинок: мем с графиком и карточка «ФАКТ» (тёмный звёздный фон, оранжевые акценты)."""

import base64
import math
import random
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).parent
W = H = 1080
BG = (8, 8, 11)
ORANGE = (245, 166, 35)
CREAM = (246, 232, 200)

FONT_DIRS = [ROOT / "assets", Path("/usr/share/fonts/truetype/dejavu")]


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    for d in FONT_DIRS:
        p = d / name
        if p.exists():
            return ImageFont.truetype(str(p), size)
    raise FileNotFoundError(f"Шрифт {name} не найден (установите fonts-dejavu-core)")


def sans(size):
    return font("DejaVuSans-Bold.ttf", size)


def serif(size):
    return font("DejaVuSerif-Bold.ttf", size)


# ---------- фон ----------

def starry_background() -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    for _ in range(420):
        x, y = random.randint(0, W - 1), random.randint(0, H - 1)
        b = random.randint(70, 200)
        r = random.choice([0, 0, 0, 0, 1])
        d.ellipse((x - r, y - r, x + r, y + r), fill=(b, b, b))
    for _ in range(18):  # несколько ярких звёзд
        x, y = random.randint(0, W - 1), random.randint(0, H - 1)
        d.ellipse((x - 1.5, y - 1.5, x + 1.5, y + 1.5), fill=(235, 235, 235))
    return img


def wrap(draw, text, fnt, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=fnt) <= max_w:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def fit_text(draw, text, maker, max_w, max_h, start, minimum=30, spacing=1.12):
    size = start
    while size >= minimum:
        f = maker(size)
        lines = wrap(draw, text, f, max_w)
        lh = int(size * spacing)
        if lh * len(lines) <= max_h:
            return f, lines, lh
        size -= 2
    f = maker(minimum)
    return f, wrap(draw, text, f, max_w), int(minimum * spacing)


def draw_block(draw, text, maker, box, start, fill, align_v="center"):
    x0, y0, x1, y1 = box
    f, lines, lh = fit_text(draw, text, maker, x1 - x0, y1 - y0, start)
    total = lh * len(lines)
    y = y0 if align_v == "top" else (y1 - total if align_v == "bottom" else y0 + (y1 - y0 - total) // 2)
    for ln in lines:
        w = draw.textlength(ln, font=f)
        draw.text(((x0 + x1 - w) / 2, y), ln, font=f, fill=fill)
        y += lh


# ---------- формы графиков ----------

def _noise(v, amp):
    return v + random.uniform(-amp, amp)


def shape_points(shape: str, n: int = 90):
    """Возвращает (список y 0..1, индекс точки для метки)."""
    ys, label = [], None
    if shape == "staircase_crash":  # лестница вверх → обвал
        peak = int(n * 0.68)
        v = 0.15
        for i in range(n):
            if i < peak:
                if random.random() < 0.12:
                    v += random.uniform(0.06, 0.13)
                ys.append(_noise(v, 0.012))
            elif i < peak + 7:
                ys.append(None)
            else:
                ys.append(_noise(0.06, 0.012))
        top = max(y for y in ys if y is not None)
        for k in range(peak, peak + 7):
            t = (k - peak + 1) / 8
            ys[k] = top * (1 - t) + 0.06 * t
        ys = [y if y is not None else 0.06 for y in ys]
        label = max(range(peak), key=lambda i: ys[i])
    elif shape in ("peak", "fake_breakout"):  # рост → пик → падение
        peak = int(n * (0.45 if shape == "peak" else 0.6))
        for i in range(n):
            if i <= peak:
                v = 0.1 + 0.8 * (i / peak) ** 1.6
            else:
                v = 0.9 - 0.82 * ((i - peak) / (n - peak)) ** 0.9
            ys.append(_noise(v, 0.018))
        ys[peak] = 0.92
        label = peak
    elif shape == "slow_bleed":  # медленное сливание
        for i in range(n):
            v = 0.85 - 0.75 * (i / n) + 0.05 * random.random()
            ys.append(v)
        ys[3] = 0.88
        label = 3
    elif shape == "pump_after_sell":  # продал — и полетело
        sell = int(n * 0.45)
        for i in range(n):
            if i <= sell:
                v = 0.2 + 0.04 * random.random()
            else:
                v = 0.22 + 0.72 * ((i - sell) / (n - sell)) ** 2.2
            ys.append(_noise(v, 0.012))
        label = sell
    elif shape == "v_shape":  # продал на дне
        bottom = int(n * 0.5)
        for i in range(n):
            v = 0.85 - 0.78 * (i / bottom) if i <= bottom else 0.07 + 0.83 * ((i - bottom) / (n - bottom))
            ys.append(_noise(v, 0.018))
        ys[bottom] = 0.05
        label = bottom
    elif shape == "rollercoaster":  # качели
        for i in range(n):
            ys.append(0.5 + 0.35 * math.sin(i / n * 4 * math.pi) + random.uniform(-0.03, 0.03))
        label = int(n * 0.125)
    else:  # flat — боковик
        for i in range(n):
            ys.append(0.45 + random.uniform(-0.04, 0.04))
        label = int(n * 0.5)
    ys = [min(max(y, 0.02), 0.98) for y in ys]
    return ys, label


def draw_chart(img, box, ys, label_idx=None, label_text=None, color=ORANGE, fill=True, width=5):
    x0, y0, x1, y1 = box
    n = len(ys)
    pts = [(x0 + (x1 - x0) * i / (n - 1), y1 - (y1 - y0) * y) for i, y in enumerate(ys)]

    if fill:
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        od.polygon(pts + [(x1, y1), (x0, y1)], fill=color + (105,))
        # затухание к низу
        mask = Image.new("L", img.size, 0)
        md = ImageDraw.Draw(mask)
        for yy in range(int(y0), int(y1) + 1):
            a = int(255 * (0.35 + 0.65 * (y1 - yy) / (y1 - y0)))
            md.line((0, yy, W, yy), fill=a)
        overlay.putalpha(Image.composite(overlay.getchannel("A"), Image.new("L", img.size, 0), mask))
        img.paste(overlay, (0, 0), overlay)

    d = ImageDraw.Draw(img)
    for gy in range(4):  # еле заметная сетка
        yy = y0 + (y1 - y0) * gy / 3
        d.line((x0, yy, x1, yy), fill=(28, 28, 32), width=1)
    d.line(pts, fill=color, width=width, joint="curve")

    if label_idx is not None and label_text:
        px, py = pts[label_idx]
        d.ellipse((px - 9, py - 9, px + 9, py + 9), fill=(255, 255, 255), outline=(20, 20, 20), width=3)
        f = sans(30)
        tw = d.textlength(label_text, font=f)
        bw, bh = tw + 30, 50
        bx = min(max(px - bw / 2, x0), x1 - bw)
        by = py - 22 - bh
        d.rounded_rectangle((bx, by, bx + bw, by + bh), radius=10, fill=(245, 245, 245))
        d.text((bx + 15, by + 8), label_text, font=f, fill=(15, 15, 15))


def to_bytes(img) -> bytes:
    buf = BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=92)
    return buf.getvalue()


# ---------- мем ----------

SHAPES = ["staircase_crash", "peak", "fake_breakout", "slow_bleed", "pump_after_sell", "v_shape",
          "rollercoaster", "flat", "multi_dump"]


def render_meme(top: str, bottom: str, shape: str, label: str | None) -> bytes:
    img = starry_background().convert("RGBA")
    d = ImageDraw.Draw(img)
    top, bottom = (top or "").upper(), (bottom or "").upper()

    has_top = bool(top.strip())
    chart_box = (95, 300 if has_top else 140, W - 95, 770 if has_top else 700)
    if has_top:
        draw_block(d, top, sans, (60, 40, W - 60, 260), 84, (255, 255, 255))
    draw_block(d, bottom, sans, (60, 800 if has_top else 740, W - 60, H - 40), 76, (255, 255, 255))

    if shape == "multi_dump":  # «десять монет, которые падают одновременно»
        palette = [ORANGE, (240, 90, 90), (90, 200, 250), (160, 120, 255), (90, 230, 160),
                   (250, 220, 90), (255, 140, 200), (200, 200, 200), (255, 120, 60), (120, 160, 255)]
        for c in palette:
            start = random.uniform(0.6, 0.97)
            ys = [_noise(start - (start - random.uniform(0.03, 0.35)) * (i / 79) ** 1.3, 0.015) for i in range(80)]
            draw_chart(img, chart_box, [min(max(y, 0.02), 0.98) for y in ys], color=c, fill=False, width=4)
    else:
        ys, idx = shape_points(shape if shape in SHAPES else "peak")
        draw_chart(img, chart_box, ys, idx, label)
    return to_bytes(img)


# ---------- карточка «ФАКТ» ----------

def render_fact(text: str, date_str: str) -> bytes:
    img = starry_background()
    d = ImageDraw.Draw(img)

    # лев справа снизу, растворяется в фон
    lion_p, lion_b64 = ROOT / "assets" / "lion.png", ROOT / "assets" / "lion.b64"
    src = lion_p if lion_p.exists() else (BytesIO(base64.b64decode(lion_b64.read_text())) if lion_b64.exists() else None)
    if src:
        lion = Image.open(src).convert("RGB").resize((400, 400))
        mask = Image.new("L", lion.size, 0)
        ImageDraw.Draw(mask).ellipse((20, 0, 380, 450), fill=255)
        mask = mask.filter(ImageFilter.GaussianBlur(45))
        dark = Image.new("RGB", lion.size, BG)
        lion = Image.blend(dark, lion, 0.92)
        img.paste(lion, (W - 400 - 60, H - 400 - 46), mask)

    # рамка
    d.rounded_rectangle((40, 40, W - 40, H - 40), radius=28, outline=(214, 150, 50), width=5)
    # бейдж ФАКТ
    bf = sans(40)
    d.rounded_rectangle((70, 62, 70 + d.textlength("ФАКТ", font=bf) + 44, 128), radius=10, fill=(238, 150, 40))
    d.text((92, 70), "ФАКТ", font=bf, fill=(30, 18, 5))
    # дата
    df = serif(38)
    dw = d.textlength(date_str, font=df)
    d.text(((W - dw) / 2, 150), date_str, font=df, fill=(235, 235, 235))
    # текст факта
    draw_block(d, text, serif, (100, 215, W - 100, 620), 66, CREAM, align_v="top")
    return to_bytes(img)
