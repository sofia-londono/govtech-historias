"""Renders Instagram stories (1080x1920) from a JSON spec.

Spec:
  layout: "cuts" (one or more clips in sequence) | "stack" (rows) | "note"
  items:  [{src, start?, dur?, focus?: [x, y], audio?: bool, stabilize?: bool, text?: TEXT}]
  text:   TEXT for the whole story (stack and note use this one)
  duration: seconds for stack and note

TEXT: {lines: [..], style: "plain"|"box", size?: int, pos?: "low"|"mid", font?: "raleway"|"inter"|"tall"|"bask", scrim?: bool}
Wrap a word in [[ ]] to paint it in the event blue.
"""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps
from pillow_heif import register_heif_opener

register_heif_opener()

HERE = Path(__file__).resolve().parent
FONTS = HERE / "fonts"
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
# 9:20 fills tall phones like the Galaxy S20+; on 9:16 screens Instagram trims about 240 px
# at the top and bottom, so text stays inside the central 1080x1920.
W, H = 1080, 2400
FPS = 30
BOTTOM = H - 620  # above Instagram's reply bar, and inside the 9:16 area
WHITE = (255, 255, 255, 255)
BLUE = (59, 169, 240, 255)
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")
LOOK = "eq=contrast=1.04:saturation=1.08,unsharp=5:5:0.45:5:5:0"
TONEMAP = (
    "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,"
    "zscale=t=bt709:m=bt709:r=tv,format=yuv420p,"
)


def font(family, size, weight=700):
    name = {"raleway": "Raleway.ttf", "inter": "Inter.ttf", "tall": "Inter.ttf", "bask": "LibreBaskerville-Italic.ttf"}[family]
    f = ImageFont.truetype(str(FONTS / name), size)
    f.set_variation_by_axes(
        [{"weight": weight, "optical": 32}.get(a["name"].decode().lower().split()[0], a["default"]) for a in f.get_variation_axes()]
    )
    return f


def runs(text):
    """Splits '[[word]] rest' into [(word, True), (rest, False)]."""
    return [(p, i % 2 == 1) for i, p in enumerate(re.split(r"\[\[|\]\]", text)) if p]


def plain(text):
    return re.sub(r"\[\[|\]\]", "", text)


def wrap(line, f, max_w):
    """Wraps to the fewest lines, then narrows the width so the lines come out even."""
    lines = greedy(line, f, max_w)
    lo, hi = max_w * 0.4, max_w
    while hi - lo > 4:
        mid = (lo + hi) / 2
        if len(greedy(line, f, mid)) > len(lines):
            lo = mid
        else:
            hi = mid
    return greedy(line, f, hi)


def greedy(line, f, max_w):
    out, cur = [], ""
    for word in line.split():
        probe = f"{cur} {word}".strip()
        if f.getlength(plain(probe)) > max_w and cur:
            out.append(cur)
            cur = word
        else:
            cur = probe
    return out + [cur] if cur else out


def balance_markup(lines):
    """Keeps a [[...]] span valid when wrapping cuts it across two lines."""
    out, inside = [], False
    for ln in lines:
        s = ("[[" if inside else "") + ln
        inside = s.count("[[") > s.count("]]")
        out.append(s + ("]]" if inside else ""))
    return out


def draw_line(text, f, fill=WHITE, stretch=1.0):
    w = max(1, round(f.getlength(plain(text))))
    asc, desc = f.getmetrics()
    img = Image.new("RGBA", (w + 8, asc + desc + 8), (0, 0, 0, 0))
    d, x = ImageDraw.Draw(img), 4
    for part, blue in runs(text):
        d.text((x, 4), part, font=f, fill=BLUE if blue else fill)
        x += f.getlength(part)
    if stretch != 1.0:
        img = img.resize((img.width, round(img.height * stretch)), Image.LANCZOS)
    return img


def text_layer(spec):
    """Full-frame RGBA with the story text, or None."""
    if not spec or not spec.get("lines"):
        return None
    for ln in spec["lines"]:
        if EMOJI.search(ln):
            raise ValueError(f"emoji in text: {ln}")
    family = spec.get("font", "raleway")
    box = spec.get("style", "plain") == "box"
    size = spec.get("size", 46)
    max_w = W - (240 if box else 160)
    stretch = 1.25 if family == "tall" else 1.0
    while True:
        f = font(family, size)
        lines = balance_markup([w for ln in spec["lines"] for w in wrap(ln, f, max_w)])
        if len(lines) <= 5 or size <= 36:
            break
        size -= 4
    imgs = [draw_line(ln, f, stretch=stretch) for ln in lines]
    if spec.get("sub"):
        # smaller credit lines under the main text, e.g. a quote's speaker and role
        fs = font(family, round(size * 0.66), 600)
        imgs.append(Image.new("RGBA", (1, round(size * 0.35)), (0, 0, 0, 0)))
        imgs += [draw_line(ln, fs) for s in spec["sub"] for ln in balance_markup(wrap(s, fs, max_w))]
    gap = round(size * 0.12)
    tw = max(i.width for i in imgs)
    th = sum(i.height for i in imgs) + gap * (len(imgs) - 1)
    pad = 44 if box else 0
    pos = spec.get("pos", "low")
    bottom = pos if isinstance(pos, int) else BOTTOM if pos == "low" else (H + th) // 2
    y = bottom - th - pad
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    if box:
        ImageDraw.Draw(layer).rounded_rectangle(
            [(W - tw) // 2 - pad, y - pad, (W + tw) // 2 + pad, bottom + pad // 2],
            radius=30,
            fill=(0, 0, 0, 190),
        )
    words = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    for img in imgs:
        words.alpha_composite(img, ((W - img.width) // 2, y))
        y += img.height + gap
    if not box:
        shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        shadow.putalpha(words.getchannel("A").point(lambda a: a * 0.85))
        layer.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(10)))
        layer.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(3)))
    layer.alpha_composite(words)
    return layer


def scrim():
    a = np.clip((np.arange(H) - (H - 870)) / 500, 0, 1) ** 1.4 * 165
    img = np.zeros((H, W, 4), np.uint8)
    img[..., 3] = a[:, None].astype(np.uint8)
    return Image.fromarray(img, "RGBA")


def probe(src):
    err = subprocess.run([FFMPEG, "-hide_banner", "-i", str(src)], capture_output=True, text=True, errors="replace").stderr
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    dur = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0
    return {"dur": dur, "audio": "Audio:" in err, "hdr": bool(re.search(r"smpte2084|arib-std-b67|bt2020", err))}


def prepare_image(src, tmp, idx):
    img = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    out = Path(tmp) / f"img{idx}.jpg"
    img.save(out, quality=97, subsampling=0)
    return out


def source(item, idx, tmp, w, h, dur):
    """Returns (ffmpeg input args, filter string turning the input into a w x h clip, has_audio)."""
    src = Path(item["src"])
    fx, fy = item.get("focus", [0.5, 0.5])
    if src.suffix.lower() in IMAGE_EXT:
        n = round(dur * FPS)
        chain = (
            f"scale={w * 2}:{h * 2}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={w * 2}:{h * 2}:(iw-{w * 2})*{fx}:(ih-{h * 2})*{fy},"
            f"zoompan=z='1+0.05*on/{n}':x='(iw-iw/zoom)/2':y='(ih-ih/zoom)/2':d={n}:s={w}x{h}:fps={FPS},"
            f"{LOOK},{'unsharp=5:5:0.6:5:5:0,' if item.get('sharp') else ''}scale=in_range=pc:out_range=tv,setsar=1,format=yuv420p,trim=duration={dur},setpts=PTS-STARTPTS"
        )
        return ["-i", str(prepare_image(src, tmp, idx))], chain, False
    info = probe(src)
    start = item.get("start", 0)
    args = ["-ss", str(start)]
    if info["dur"] and info["dur"] - start < dur:
        args = ["-stream_loop", "-1", "-ss", str(start)]
    chain = TONEMAP if info["hdr"] else ""
    if item.get("stabilize"):
        chain += "deshake=rx=32:ry=32,"
    chain += (
        f"fps={FPS},scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={w}:{h}:(iw-{w})*{fx}:(ih-{h})*{fy},{LOOK},setsar=1,format=yuv420p,"
        f"trim=duration={dur},setpts=PTS-STARTPTS"
    )
    return args + ["-i", str(src)], chain, info["audio"] and item.get("audio", True)


ENCODE = [
    "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-profile:v", "high", "-pix_fmt", "yuv420p",
    "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv", "-r", str(FPS),
    "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart",
]


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if r.returncode:
        raise RuntimeError(r.stderr[-1800:])


def overlay_input(layer, tmp, name):
    p = Path(tmp) / f"{name}.png"
    layer.save(p)
    return ["-loop", "1", "-framerate", str(FPS), "-i", str(p)]


def render_cuts(spec, out, tmp):
    items, inputs, parts, n = spec["items"], [], [], 0
    default_dur = 6 if len(items) == 1 else 3
    shade = scrim()
    for i, item in enumerate(items):
        dur = item.get("dur", default_dur)
        args, chain, has_audio = source(item, i, tmp, W, H, dur)
        inputs += args
        v = n
        n += 1
        parts.append(f"[{v}:v]{chain}[b{i}]")
        text = item.get("text") or (spec.get("text") if len(items) == 1 or i == 0 else None)
        layer = text_layer(text)
        if layer:
            full = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            if text.get("scrim"):
                full.alpha_composite(shade)
            full.alpha_composite(layer)
            inputs += overlay_input(full, tmp, f"t{i}")
            parts.append(f"[{n}:v]format=rgba,fade=t=in:st=0:d=0.25:alpha=1,trim=duration={dur},setpts=PTS-STARTPTS[o{i}]")
            parts.append(f"[b{i}][o{i}]overlay=shortest=1,format=yuv420p[v{i}]")
            n += 1
        else:
            parts.append(f"[b{i}]null[v{i}]")
        if has_audio:
            parts.append(f"[{v}:a]atrim=duration={dur},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,afade=t=in:d=0.03,afade=t=out:st={max(0, dur - 0.05)}:d=0.05[a{i}]")
        else:
            parts.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={dur}[a{i}]")
    joined = "".join(f"[v{i}][a{i}]" for i in range(len(items)))
    parts.append(f"{joined}concat=n={len(items)}:v=1:a=1[v][a0]")
    parts.append("[a0]loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000[a]")
    run([FFMPEG, "-y", "-v", "error", *inputs, "-filter_complex", ";".join(parts), "-map", "[v]", "-map", "[a]", *ENCODE, str(out)])


def render_stack(spec, out, tmp):
    items = spec["items"]
    dur = spec.get("duration", 7)
    rows = len(items)
    line = 6
    rh = (H - line * (rows - 1)) // rows
    rh -= rh % 2
    inputs, parts, audio = [], [], None
    for i, item in enumerate(items):
        args, chain, has_audio = source({**item, "audio": item.get("audio", False)}, i, tmp, W, rh, dur)
        inputs += args
        parts.append(f"[{i}:v]{chain}[r{i}]")
        if has_audio and audio is None:
            audio = i
    parts.append(f"color=c=white:s={W}x{H}:r={FPS}:d={dur},format=yuv420p[bg]")
    last = "bg"
    for i in range(rows):
        parts.append(f"[{last}][r{i}]overlay=0:{i * (rh + line)}[s{i}]")
        last = f"s{i}"
    n = rows
    layer = text_layer(spec.get("text"))
    labels = [it.get("label") for it in items]
    if any(labels):
        # name and role per row, kept inside the area every phone shows
        layer = layer or Image.new("RGBA", (W, H), (0, 0, 0, 0))
        for i, lab in enumerate(labels):
            if not lab:
                continue
            fn, fr = font("raleway", 44, 700), font("raleway", 32, 600)
            rows_img = [draw_line(lab[0], fn)] + [draw_line(r, fr) for r in lab[1:]]
            h = sum(p.height for p in rows_img)
            top = i * (rh + line)
            floor = H - 260 if items[i].get("label_low") else BOTTOM  # label_low: as low as the reply bar allows
            y = max(top + 40, 300, min(top + rh - 40 - h, floor - h))
            words = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            for p in rows_img:
                words.alpha_composite(p, (56, y))
                y += p.height
            shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            shadow.putalpha(words.getchannel("A").point(lambda a: a * 0.85))
            layer.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(18)))
            layer.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(18)))
            layer.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(4)))
            layer.alpha_composite(words)
    if layer:
        inputs += overlay_input(layer, tmp, "t")
        parts.append(f"[{n}:v]format=rgba,fade=t=in:st=0.2:d=0.3:alpha=1,trim=duration={dur},setpts=PTS-STARTPTS[o]")
        parts.append(f"[{last}][o]overlay=shortest=1,format=yuv420p[v]")
    else:
        parts.append(f"[{last}]format=yuv420p[v]")
    if audio is not None:
        parts.append(f"[{audio}:a]atrim=duration={dur},asetpts=PTS-STARTPTS,loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000[a]")
    else:
        parts.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={dur}[a]")
    run([FFMPEG, "-y", "-v", "error", *inputs, "-filter_complex", ";".join(parts), "-map", "[v]", "-map", "[a]", "-t", str(dur), *ENCODE, str(out)])


def grid_design(design, cells, rh, labels):
    """Black frame with rounded windows, a title on top and a blue name pill per photo."""
    s = 2
    mask = Image.new("L", (W * s, H * s), 255)
    d = ImageDraw.Draw(mask)
    r = design.get("radius", 28)
    for x, y, cw in cells:
        d.rounded_rectangle([x * s, y * s, (x + cw) * s - 1, (y + rh) * s - 1], radius=r * s, fill=0)
    # only the event's colors: black background and a solid #3BA9F0 frame around each photo
    frame = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    frame.putalpha(mask.resize((W, H), Image.LANCZOS))
    bw = design.get("border", 6)
    edge = Image.new("L", (W * s, H * s), 0)
    ed = ImageDraw.Draw(edge)
    for x, y, cw in cells:
        ed.rounded_rectangle([(x - bw) * s, (y - bw) * s, (x + cw + bw) * s - 1, (y + rh + bw) * s - 1], radius=(r + bw) * s, fill=255)
        ed.rounded_rectangle([x * s, y * s, (x + cw) * s - 1, (y + rh) * s - 1], radius=r * s, fill=0)
    blue = Image.new("RGBA", (W, H), BLUE)
    blue.putalpha(edge.resize((W, H), Image.LANCZOS))
    frame.alpha_composite(blue)
    title = design.get("title") or []
    y = design["top"] - 70
    imgs = []
    for i, t in enumerate(title):
        imgs.append(draw_line(t, font("raleway", 64 if i == 0 else 38, 700 if i == 0 else 600), WHITE if i == 0 else (190, 220, 240, 255)))
    y -= sum(im.height for im in imgs)
    for i, im in enumerate(imgs):
        frame.alpha_composite(im, ((W - im.width) // 2, y))
        y += im.height
        if i == 0 and len(imgs) > 1:
            ImageDraw.Draw(frame).rounded_rectangle([W // 2 - 40, y + 4, W // 2 + 40, y + 10], radius=3, fill=BLUE)
            y += 18
    fp = font("raleway", 34, 700)
    for (x, cy, cw), lab in zip(cells, labels):
        if not lab:
            continue
        if isinstance(lab, dict):
            # the brand's own logo on a white badge in the corner
            logo = Image.open(lab["logo"]).convert("RGBA")
            sc = min(260 / logo.width, 58 / logo.height)
            logo = logo.resize((round(logo.width * sc), round(logo.height * sc)), Image.LANCZOS)
            pad = 16
            bx, by = x + 16, cy + 16
            badge = [bx, by, bx + logo.width + 2 * pad, by + logo.height + 2 * pad]
            sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            ImageDraw.Draw(sh).rounded_rectangle([badge[0], badge[1] + 4, badge[2], badge[3] + 4], radius=18, fill=(0, 0, 0, 110))
            frame.alpha_composite(sh.filter(ImageFilter.GaussianBlur(8)))
            ImageDraw.Draw(frame).rounded_rectangle(badge, radius=18, fill=(255, 255, 255, 255))
            frame.alpha_composite(logo, (bx + pad, by + pad))
            continue
        txt = draw_line(lab, fp, (4, 32, 47, 255))
        pw, ph = txt.width + 36, txt.height + 10
        px, py = x + 18, cy + rh - ph - 18
        ImageDraw.Draw(frame).rounded_rectangle([px, py, px + pw, py + ph], radius=ph // 2, fill=BLUE)
        frame.alpha_composite(txt, (px + 18, py + 5))
    return frame


def render_grid(spec, out, tmp):
    """Collage: one column up to 3 items, two columns from 4; an odd last item spans the row.
    With "design": {title, top, bottom, margin, gap, radius} it sits on black with rounded photos."""
    items = spec["items"]
    dur = spec.get("duration", 6)
    n = len(items)
    cols = 1 if n <= 3 else 2
    rows = -(-n // cols)
    design = spec.get("design")
    line = design.get("gap", 16) if design else 8
    margin = design.get("margin", 40) if design else 0
    top, bottom = (design["top"], design["bottom"]) if design else (0, H)
    rh = (bottom - top - line * (rows - 1)) // rows
    rh -= rh % 2
    cells = []
    for i in range(n):
        r, c = divmod(i, cols)
        span = cols if (i == n - 1 and n % cols) else 1
        cw = W - 2 * margin if span == cols else (W - 2 * margin - line * (cols - 1)) // cols
        cw -= cw % 2
        cells.append((margin + c * (cw + line), top + r * (rh + line), cw))
    inputs, parts = [], []
    for i, (item, (x, y, cw)) in enumerate(zip(items, cells)):
        args, chain, _ = source({**item, "audio": False, "sharp": True}, i, tmp, cw, rh, dur)
        inputs += args
        parts.append(f"[{i}:v]{chain}[r{i}]")
    parts.append(f"color=c={'black' if design else 'white'}:s={W}x{H}:r={FPS}:d={dur},format=yuv420p[bg]")
    last = "bg"
    for i, (x, y, _) in enumerate(cells):
        parts.append(f"[{last}][r{i}]overlay={x}:{y}[s{i}]")
        last = f"s{i}"
    layer = text_layer(spec.get("text"))
    if design:
        frame = grid_design(design, cells, rh, [it.get("label") for it in items])
        if layer:
            frame.alpha_composite(layer)
        layer = frame
    if layer:
        inputs += overlay_input(layer, tmp, "t")
        parts.append(f"[{n}:v]format=rgba,fade=t=in:st=0.2:d=0.3:alpha=1,trim=duration={dur},setpts=PTS-STARTPTS[o]")
        parts.append(f"[{last}][o]overlay=shortest=1,format=yuv420p[v]")
    else:
        parts.append(f"[{last}]format=yuv420p[v]")
    parts.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={dur}[a]")
    run([FFMPEG, "-y", "-v", "error", *inputs, "-filter_complex", ";".join(parts), "-map", "[v]", "-map", "[a]", "-t", str(dur), *ENCODE, str(out)])


def paper_sheet(size, seed):
    rng = np.random.default_rng(seed)
    noise = rng.normal(0, 3.2, (size, size)).astype(np.float32)
    grain = np.asarray(Image.fromarray(noise + 128).convert("L").filter(ImageFilter.GaussianBlur(0.8)), np.float32) - 128
    v = np.clip(246 + grain * 1.6, 0, 255).astype(np.uint8)
    return Image.merge("RGBA", (Image.fromarray(v), Image.fromarray(v), Image.fromarray(np.clip(v.astype(int) - 2, 0, 255).astype(np.uint8)), Image.new("L", (size, size), 255)))


def pin_sprite():
    s = 4
    img = Image.new("RGBA", (130 * s, 170 * s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.polygon([(62 * s, 104 * s), (68 * s, 104 * s), (65 * s, 166 * s)], fill=(150, 165, 178, 255))
    d.polygon([(44 * s, 40 * s), (86 * s, 40 * s), (92 * s, 86 * s), (38 * s, 86 * s)], fill=(40, 132, 196, 255))
    d.ellipse([22 * s, 76 * s, 108 * s, 112 * s], fill=(39, 125, 188, 255))
    d.ellipse([22 * s, 70 * s, 108 * s, 106 * s], fill=BLUE)
    d.ellipse([34 * s, 8 * s, 96 * s, 52 * s], fill=BLUE)
    d.ellipse([46 * s, 14 * s, 68 * s, 28 * s], fill=(176, 222, 250, 255))
    d.ellipse([38 * s, 76 * s, 62 * s, 86 * s], fill=(150, 210, 248, 255))
    return img.resize((130, 170), Image.LANCZOS)


def paste(frame, im, x, y):
    """alpha_composite that tolerates sprites partly above or left of the frame."""
    sx, sy = max(0, -x), max(0, -y)
    if sx >= im.width or sy >= im.height or x >= frame.width or y >= frame.height:
        return
    frame.alpha_composite(im, (max(0, x), max(0, y)), (sx, sy))


def ease_out_back(x, k=1.25):
    x -= 1
    return 1 + (k + 1) * x**3 + k * x**2


def render_note(spec, out, tmp):
    """The pinned note from the GovTech campaign, animated over a B&W background photo."""
    dur = spec.get("duration", 6)
    note = spec.get("note", {})
    small1 = note.get("top", "bienvenidos al")
    big = note.get("big", "DÍA 1")
    small2 = note.get("bottom", "del GovTech Summit")
    for t in (small1, big, small2):
        if EMOJI.search(t):
            raise ValueError(f"emoji in text: {t}")
    base = ImageOps.exif_transpose(Image.open(spec["items"][0]["src"])).convert("L")
    base = ImageOps.autocontrast(base, cutoff=1)
    bw = round(W * 1.08)
    bh = round(H * 1.08)
    bg_big = ImageOps.fit(base, (bw, bh), Image.LANCZOS, centering=spec["items"][0].get("focus", [0.5, 0.5]))
    bg_big = Image.eval(bg_big, lambda v: int(v * 0.72)).convert("RGB")

    size = 800
    paper = paper_sheet(size, 1)
    under = paper_sheet(size, 2)
    rows = []
    # Raleway Bold like the rest of the stories; "inter" keeps the campaign's stretched title
    family = note.get("font", "raleway")
    small_w, big_w, big_stretch = (700, 800, 1.0) if family == "raleway" else (500, 800, 1.28)
    f1 = font(family, 58, small_w)
    fit = 250
    while font(family, fit, big_w).getlength(big) > size - 120:
        fit -= 6
    for text, f, fill, stretch in ((small1, f1, (25, 25, 28, 255), 1.0), (big, font(family, fit, big_w), BLUE, big_stretch), (small2, f1, (25, 25, 28, 255), 1.0)):
        rows.append(draw_line(text, f, fill, stretch))
    total = sum(r.height for r in rows) + 40
    y = (size - total) // 2 + 20
    layers = []
    for r in rows:
        la = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        la.alpha_composite(r, ((size - r.width) // 2, y))
        layers.append(la)
        y += r.height + 20

    logo = None
    if note.get("logo"):
        # the logo comes in black; the note uses it in white over the dark photo
        src = Image.open(note["logo"]).convert("RGBA")
        white = Image.new("RGBA", src.size, (255, 255, 255, 255))
        white.putalpha(src.getchannel("A"))
        lw = 540
        white = white.resize((lw, round(src.height * lw / src.width)), Image.LANCZOS)
        logo = Image.new("RGBA", (lw + 80, white.height + 80), (0, 0, 0, 0))
        shade = Image.new("RGBA", logo.size, (0, 0, 0, 0))
        shade.paste((0, 0, 0, 255), (40, 40), white.getchannel("A").point(lambda v: v * 0.7))
        logo.alpha_composite(shade.filter(ImageFilter.GaussianBlur(14)))
        logo.alpha_composite(white, (40, 40))
    pin = pin_sprite()
    shadow = Image.new("L", (size + 160, size + 160), 0)
    ImageDraw.Draw(shadow).rectangle([80, 90, size + 80, size + 100], fill=170)
    shadow = shadow.filter(ImageFilter.GaussianBlur(26))
    cx, cy = W // 2, H // 2 - 80
    frames = round(dur * FPS)
    enc = subprocess.Popen(
        [FFMPEG, "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
         "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-shortest", *ENCODE, str(out)],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    under_rot = under.rotate(3.5, Image.BICUBIC, expand=True)
    grid_x, grid_y = (120, W - 120), (cy - 460, cy + 620)
    for n in range(frames):
        t = n / FPS
        zoom = 1 + 0.08 * (1 - t / dur)
        cw, ch = round(W * zoom), round(H * zoom)
        ox, oy = (bw - cw) // 2, (bh - ch) // 2
        frame = bg_big.crop((ox, oy, ox + cw, oy + ch)).resize((W, H), Image.BILINEAR).convert("RGBA")
        if note.get("grid", True):
            d = ImageDraw.Draw(frame)
            for gx in grid_x:
                d.line([(gx, 0), (gx, H)], fill=(255, 255, 255, 235), width=3)
            for gy in grid_y:
                d.line([(0, gy), (W, gy)], fill=(255, 255, 255, 235), width=3)
        if logo:
            a = min(1, max(0, (t - 2.2) / 0.5))
            if a > 0:
                lg = logo.copy()
                lg.putalpha(lg.getchannel("A").point(lambda v: int(v * a)))
                paste(frame, lg, (W - lg.width) // 2, BOTTOM - lg.height - 10)
        p = min(1, max(0, (t - 0.1) / 0.7))
        py = cy - (1 - ease_out_back(p)) * 1500 if p < 1 else cy
        hit = t - 1.05  # the pin lands: small jolt
        jolt = 1.6 * np.exp(-hit * 7) * np.cos(hit * 26) if hit > 0 else 0
        angle = -5 - (1 - p) * 9 + jolt
        sheet = paper.copy()
        for i, la in enumerate(layers):
            alpha = min(1, max(0, (t - (1.25 + i * 0.28)) / 0.3))
            if alpha > 0:
                sheet = Image.alpha_composite(sheet, Image.blend(Image.new("RGBA", la.size, (0, 0, 0, 0)), la, alpha))
        rot = sheet.rotate(-angle, Image.BICUBIC, expand=True)
        sh = shadow.rotate(-angle, Image.BILINEAR, expand=True)
        black = Image.new("RGBA", sh.size, (0, 0, 0, 255))
        black.putalpha(sh)
        y = round(py)
        paste(frame, black, cx - sh.width // 2, y - sh.height // 2 + 10)
        paste(frame, under_rot, cx - under_rot.width // 2, y + 14 - under_rot.height // 2)
        paste(frame, rot, cx - rot.width // 2, y - rot.height // 2)
        pt = (t - 0.8) / 0.25
        if pt > 0:
            pp = min(1, pt)
            sc = 1.8 - 0.8 * pp
            sp = pin.resize((round(pin.width * sc), round(pin.height * sc)), Image.LANCZOS)
            sp.putalpha(sp.getchannel("A").point(lambda a: int(a * pp)))
            paste(frame, sp, cx - sp.width // 2, cy - size // 2 - 40 - sp.height // 2)
        enc.stdin.write(frame.convert("RGB").tobytes())
    enc.stdin.close()
    err = enc.stderr.read().decode(errors="replace")
    if enc.wait():
        raise RuntimeError(err[-1500:])


def previews(out):
    out = Path(out)
    prev = out.with_name(out.stem + "_preview.mp4")
    poster = out.with_name(out.stem + ".jpg")
    run([FFMPEG, "-y", "-v", "error", "-i", str(out), "-vf", f"scale=720:{H * 2 // 3}:flags=lanczos", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(prev)])
    run([FFMPEG, "-y", "-v", "error", "-ss", "1.9", "-i", str(out), "-frames:v", "1", "-vf", f"scale=540:{H // 2}", "-q:v", "4", str(poster)])
    return prev, poster


def render(spec, out):
    with tempfile.TemporaryDirectory() as tmp:
        {"cuts": render_cuts, "stack": render_stack, "grid": render_grid, "note": render_note}[spec["layout"]](spec, out, tmp)
    return previews(out)


if __name__ == "__main__":
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    print(render(spec, sys.argv[2]))
