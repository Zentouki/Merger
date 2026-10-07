"""Builds the Merger logo set (SVG, PNG, ICO) and the brand sheet from one description.
Run:  python brand/make_brand.py      (needs Pillow)
Every logo is made of a few round-capped strokes, so the SVGs and PNGs always match."""
import math, sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent
INDIGO, INK, WHITE = "#4F46E5", "#0F172A", "#FFFFFF"
TINT, SOFT = "#C7D2FE", "#818CF8"        # lighter indigos for the second stream of the "m"

# ---------------------------------------------------------------- geometry
# A stroke is a list of segments: ("L", x0, y0, x1, y1) or ("A", cx, cy, r, deg0, deg1).
# Angles run clockwise on screen (y points down), so 270 degrees is the top of a circle.
def _pt(cx, cy, r, d):
    return cx + r * math.cos(math.radians(d)), cy + r * math.sin(math.radians(d))

def start(seg):
    return seg[1:3] if seg[0] == "L" else _pt(*seg[1:4], seg[4])

def end(seg):
    return seg[3:5] if seg[0] == "L" else _pt(*seg[1:4], seg[5])

def path_d(stroke, k=1.0, ox=0.0, oy=0.0):
    f = lambda p: f"{p[0] * k + ox:.2f} {p[1] * k + oy:.2f}"
    d = "M" + f(start(stroke[0]))
    for s in stroke:
        if s[0] == "L":
            d += " L" + f(end(s))
        else:
            sweep = 1 if s[5] > s[4] else 0
            large = 1 if abs(s[5] - s[4]) > 180 else 0
            d += f" A{s[3] * k:.2f} {s[3] * k:.2f} 0 {large} {sweep} " + f(end(s))
    return d

def points(stroke, step=0.35):
    for s in stroke:
        if s[0] == "L":
            n = max(2, int(math.hypot(s[3] - s[1], s[4] - s[2]) / step))
            for i in range(n + 1):
                yield s[1] + (s[3] - s[1]) * i / n, s[2] + (s[4] - s[2]) * i / n
        else:
            n = max(2, int(abs(s[5] - s[4]) * math.pi / 180 * s[3] / step))
            for i in range(n + 1):
                yield _pt(s[1], s[2], s[3], s[4] + (s[5] - s[4]) * i / n)

def m_glyph(x0, ytop, T, a):
    yb, r = ytop + T, a / 2
    return [[("L", x0, ytop, x0, yb)],
            [("A", x0 + r, ytop + r, r, 180, 360), ("L", x0 + a, ytop + r, x0 + a, yb)],
            [("A", x0 + 1.5 * a, ytop + r, r, 180, 360), ("L", x0 + 2 * a, ytop + r, x0 + 2 * a, yb)]]

def mark_strokes(R=100.0):
    """The monogram, for a circle of radius R centred at 0,0: left stream, right stream."""
    k, a, T = R / 100, 48, 76
    g = m_glyph(-a, -T / 2, T, a)
    sc = lambda ss: [[tuple(v * k if i else v for i, v in enumerate(seg)) if seg[0] == "L" else
                      ("A", seg[1] * k, seg[2] * k, seg[3] * k, seg[4], seg[5]) for seg in st] for st in ss]
    return sc(g[:2]), sc(g[2:]), 26 * k

def wordmark(x, ytop, T):
    """merger, drawn as monoline letters. Returns ([6 letters' strokes], stroke width, x-extent)."""
    a, re_, rr, ext, rh, desc, gap = .71 * T, T / 2, .30 * T, .17 * T, .25 * T, .49 * T, .41 * T
    yb, cy, cur, out = ytop + T, ytop + T / 2, x, []
    for ch in "merger":
        if ch == "m":
            out.append(m_glyph(cur, ytop, T, a)); cur += 2 * a
        elif ch == "e":
            cx = cur + re_
            out.append([[("L", cx - re_, cy, cx + re_, cy)], [("A", cx, cy, re_, 0, -315)]]); cur += T
        elif ch == "r":
            out.append([[("L", cur, ytop, cur, yb)], [("A", cur + rr, ytop + rr, rr, 180, 270), ("L", cur + rr, ytop, cur + rr + ext, ytop)]]); cur += rr + ext
        else:
            cx = cur + re_; xs, hd = cx + re_, yb + desc - rh
            out.append([[("A", cx, cy, re_, 0, 180), ("A", cx, cy, re_, 180, 360)], [("L", xs, ytop, xs, hd), ("A", xs - rh, hd, rh, 0, 135)]]); cur += T
        cur += gap
    return out, .22 * T, (x, cur - gap)

# ---------------------------------------------------------------- scenes
# A scene is a list of layers: ("circle", cx, cy, r, color) | ("rrect", x, y, w, h, rx, color)
#                              | ("stroke", [strokes], width, color)
def mark_layers(cx, cy, R, kind="color"):
    left, right, w = mark_strokes(R)
    shift = lambda ss: [[tuple((v + (cx if i in (1, 3) else cy if i in (2, 4) else 0)) if (seg[0] == "L" and i) else v for i, v in enumerate(seg)) if seg[0] == "L"
                         else ("A", seg[1] + cx, seg[2] + cy, seg[3], seg[4], seg[5]) for seg in st] for st in ss]
    disc, a, b = {"color": (INDIGO, WHITE, TINT), "white": (WHITE, INDIGO, SOFT), "mono": (INK, WHITE, WHITE), "mono-white": (WHITE, INK, INK)}[kind]
    return [("circle", cx, cy, R, disc), ("stroke", shift(right), w, b), ("stroke", shift(left), w, a)]

def square_layers(x, y, S, kind):
    inner = mark_layers(x + S / 2, y + S / 2, S * .36, "color" if kind == "white" else "white")
    return [("rrect", x, y, S, S, S * .24, WHITE if kind == "white" else INDIGO)] + inner

def lockup_layers(x, cy, D, mer, ger, kind="color"):
    """Mark of diameter D with the wordmark to its right, vertically centred on cy."""
    R = D / 2
    Tw = .344 * D
    letters, w, (x0, x1) = wordmark(x + D + .26 * D, cy - Tw / 2, Tw)
    layers = mark_layers(x + R, cy, R, kind)
    for i, st in enumerate(letters):
        layers.append(("stroke", st, w, mer if i < 3 else ger))
    return layers, x1 + w / 2 - x

# ---------------------------------------------------------------- writers
def to_svg(layers, W, H, bg=None):
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W:.1f} {H:.1f}" width="{W:.0f}" height="{H:.0f}" role="img" aria-label="Merger">']
    if bg:
        o.append(f'<rect width="100%" height="100%" fill="{bg}"/>')
    for l in layers:
        if l[0] == "circle":
            o.append(f'<circle cx="{l[1]:.2f}" cy="{l[2]:.2f}" r="{l[3]:.2f}" fill="{l[4]}"/>')
        elif l[0] == "rrect":
            o.append(f'<rect x="{l[1]:.2f}" y="{l[2]:.2f}" width="{l[3]:.2f}" height="{l[4]:.2f}" rx="{l[5]:.2f}" fill="{l[6]}"/>')
        else:
            d = " ".join(path_d(s) for s in l[1])
            o.append(f'<path d="{d}" fill="none" stroke="{l[3]}" stroke-width="{l[2]:.2f}" stroke-linecap="round" stroke-linejoin="round"/>')
    o.append("</svg>")
    return "\n".join(o)

def draw_layers(img, layers, ss):
    d = ImageDraw.Draw(img)
    for l in layers:
        if l[0] == "circle":
            d.ellipse([(l[1] - l[3]) * ss, (l[2] - l[3]) * ss, (l[1] + l[3]) * ss, (l[2] + l[3]) * ss], fill=l[4])
        elif l[0] == "rrect":
            d.rounded_rectangle([l[1] * ss, l[2] * ss, (l[1] + l[3]) * ss, (l[2] + l[4]) * ss], radius=l[5] * ss, fill=l[6])
        else:
            r = l[2] / 2 * ss
            for st in l[1]:
                for x, y in points(st, .3 / ss * 3):
                    d.ellipse([x * ss - r, y * ss - r, x * ss + r, y * ss + r], fill=l[3])

def to_png(layers, W, H, bg=None, ss=4):
    img = Image.new("RGBA", (int(W * ss), int(H * ss)), bg or (0, 0, 0, 0))
    draw_layers(img, layers, ss)
    return img.resize((int(W), int(H)), Image.LANCZOS)

# ---------------------------------------------------------------- export
def build():
    (OUT / "svg").mkdir(exist_ok=True)
    save = lambda n, s: (OUT / "svg" / n).write_text(s, encoding="utf-8")
    # marks
    for name, kind in [("merger-mark", "color"), ("merger-mark-white", "white"), ("merger-mark-mono", "mono")]:
        save(name + ".svg", to_svg(mark_layers(100, 100, 100, kind), 200, 200))
    # lockups, tightly cropped
    D, pad = 200, 10
    for name, kind, mer, ger, bg in [("merger-logo", "color", INK, INDIGO, None), ("merger-logo-reversed", "white", WHITE, TINT, None),
                                     ("merger-logo-mono", "mono", INK, INK, None)]:
        lay, wd = lockup_layers(pad, 100 + pad, D, mer, ger, kind)
        save(name + ".svg", to_svg(lay, wd + 2 * pad, D + 2 * pad + 40, bg))
    # app files in the project root
    (ROOT / "icon.svg").write_text(to_svg(mark_layers(100, 100, 100, "color"), 200, 200), encoding="utf-8")
    to_png(mark_layers(256, 256, 256, "color"), 512, 512).save(ROOT / "icon-512.png")
    icon = to_png(mark_layers(128, 128, 128, "color"), 256, 256)
    icon.save(ROOT / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    to_png(square_layers(0, 0, 512, "color"), 512, 512).save(OUT / "merger-app-icon-512.png")
    for fn, kind, mer, ger in [("logo.png", "color", INK, INDIGO), ("logo-dark.png", "white", WHITE, TINT)]:
        lay, wd = lockup_layers(8, 8 + 112, 224, mer, ger, kind)
        to_png(lay, wd + 16, 224 + 16 + 56).crop((0, 0, int(wd + 16), 256 + 8)).save(ROOT / fn)
    return icon

if __name__ == "__main__":
    build()
    print("logo set written")
