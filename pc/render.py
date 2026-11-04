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
    if src.suffix.lower() in IMAGE_EXT and item.get("still"):
        # sharpest path for fast cuts: one high-quality downscale, no zoom resampling
        chain = (
            f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos+accurate_rnd,"
            f"crop={w}:{h}:(iw-{w})*{fx}:(ih-{h})*{fy},{LOOK},unsharp=5:5:0.7:5:5:0,"
            f"scale=in_range=pc:out_range=tv,setsar=1,fps={FPS},format=yuv420p,trim=duration={dur},setpts=PTS-STARTPTS"
        )
        return ["-loop", "1", "-framerate", str(FPS), "-t", str(dur), "-i", str(prepare_image(src, tmp, idx))], chain, False
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
        if item.get("text") or (spec.get("text") and (len(items) == 1 or i == 0)):
            dur = max(dur, spec.get("min_text_dur", 2.8))  # long enough to read on a phone
        item["dur"] = dur
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
    fixed = text_layer(spec.get("fixed_text"))
    parts.append(f"{joined}concat=n={len(items)}:v=1:a=1[{'vc' if fixed else 'v'}][a0]")
    if fixed:
        # one text pinned over the whole video, instead of a text per cut
        full = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        if spec["fixed_text"].get("scrim"):
            full.alpha_composite(shade)
        full.alpha_composite(fixed)
        total = sum(it.get("dur", default_dur) for it in items)
        inputs += overlay_input(full, tmp, "fixed")
        parts.append(f"[{n}:v]format=rgba,fade=t=in:st=0:d=0.3:alpha=1,trim=duration={total},setpts=PTS-STARTPTS[fx]")
        parts.append("[vc][fx]overlay=shortest=1,format=yuv420p[v]")
    # loudnorm on pure silence yields NaN, so only normalize when some cut keeps its sound
    any_audio = any(f"[{k}:a]" in p for p in parts for k in range(n + 1))
    parts.append("[a0]loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000[a]" if any_audio else "[a0]anull[a]")
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


def notepad_design(design, cells, rh, labels):
    """The campaign's notepad look: warm white page, a black spiral notepad holding the photos,
    a blue and a white sheet peeking out behind it, title in the event blue."""
    s = 2
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    shade = 246 - 14 * ((xx / W) * 0.5 + (yy / H) * 0.5)
    page = Image.fromarray(np.stack([shade, shade - 2, shade - 6], -1).clip(0, 255).astype(np.uint8), "RGB").convert("RGBA")
    m = design.get("pad_margin", 52)
    top_pad, bot_pad = design["top"] - 250, design.get("pad_bottom", design["bottom"] + 70)
    pw, ph = W - 2 * m, bot_pad - top_pad

    def sheet(color, angle, dx, dy):
        im = Image.new("RGBA", (pw + 200, ph + 200), (0, 0, 0, 0))
        ImageDraw.Draw(im).rectangle([100, 100, 100 + pw, 100 + ph], fill=color)
        sh = Image.new("RGBA", im.size, (0, 0, 0, 0))
        ImageDraw.Draw(sh).rectangle([100, 112, 100 + pw, 112 + ph], fill=(0, 0, 0, 90))
        sh = sh.filter(ImageFilter.GaussianBlur(18))
        sh.alpha_composite(im)
        sh = sh.rotate(angle, Image.BICUBIC)
        page.alpha_composite(sh, (m - 100 + dx, top_pad - 100 + dy))

    sheet((250, 250, 248, 255), 3.0, 34, 40)
    sheet(BLUE, -3.2, -30, -24)
    sheet((22, 22, 24, 255), 0, 0, 0)
    # spiral rings along the top edge
    rings = Image.new("RGBA", (W * s, H * s), (0, 0, 0, 0))
    rd = ImageDraw.Draw(rings)
    n = 12
    for i in range(n):
        cx = (m + 40 + i * (pw - 80) / (n - 1)) * s
        cy = (top_pad + 8) * s
        rd.rounded_rectangle([cx - 5 * s, cy, cx + 5 * s, cy + 46 * s], radius=5 * s, fill=(70, 70, 74, 255))
        rd.ellipse([cx - 17 * s, cy - 17 * s, cx + 17 * s, cy + 17 * s], fill=(228, 228, 230, 255))
        rd.ellipse([cx - 9 * s, cy - 13 * s, cx + 3 * s, cy - 3 * s], fill=(255, 255, 255, 255))
    rings = rings.resize((W, H), Image.LANCZOS)
    rsh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    rsh.putalpha(rings.getchannel("A").point(lambda a: a * 0.5))
    page.alpha_composite(rsh.filter(ImageFilter.GaussianBlur(4)), (0, 5))
    page.alpha_composite(rings)
    # title in the event blue on the notepad
    title = design.get("title") or []
    imgs = [draw_line(t, font("raleway", 58 if i == 0 else 36, 700 if i == 0 else 600), BLUE if i == 0 else (235, 235, 235, 255)) for i, t in enumerate(title)]
    y = top_pad + 95
    for im in imgs:
        page.alpha_composite(im, ((W - im.width) // 2, y))
        y += im.height + 4
    # optional agenda under the photos: [time, title] rows
    if design.get("body"):
        y = design["bottom"] + 70
        if design.get("body_title"):
            bt = draw_line(design["body_title"], font("raleway", 40, 700), WHITE)
            page.alpha_composite(bt, (m + 60, y))
            y += bt.height + 26
        ft, fb = font("raleway", 38, 700), font("raleway", 33, 600)
        tl_x = m + 60 + (40 if design.get("timeline") else 0)
        col = max(round(ft.getlength(r[0])) for r in design["body"]) + 30
        first = y
        dots = []
        for when, what in design["body"]:
            tl = draw_line(when, ft, BLUE)
            page.alpha_composite(tl, (tl_x, y))
            dots.append(y + tl.height // 2)
            lines = what if isinstance(what, list) else wrap(what, fb, pw - 120 - col - (tl_x - m - 60))
            yy = y + 4
            for ln in lines:
                li = draw_line(ln, fb, (235, 235, 235, 255))
                page.alpha_composite(li, (tl_x + col, yy))
                yy += li.height
            y = max(y + tl.height, yy) + (46 if design.get("timeline") else 26)
        if design.get("body_footer"):
            bf = draw_line(design["body_footer"], font("raleway", 40, 700), BLUE)
            page.alpha_composite(bf, ((W - bf.width) // 2, y + 10))
        if design.get("timeline"):
            # a vertical timeline with one dot per block
            td = ImageDraw.Draw(page)
            lx = m + 70
            td.line([(lx, dots[0]), (lx, dots[-1])], fill=(59, 169, 240, 140), width=4)
            for dy in dots:
                td.ellipse([lx - 13, dy - 13, lx + 13, dy + 13], fill=BLUE, outline=(22, 22, 24, 255), width=4)
    # photo windows with a thin blue frame
    r = design.get("radius", 22)
    bw = design.get("border", 4)
    edge = Image.new("L", (W * s, H * s), 0)
    hole = Image.new("L", (W * s, H * s), 255)
    ed, hd = ImageDraw.Draw(edge), ImageDraw.Draw(hole)
    for x, y0, cw in cells:
        ed.rounded_rectangle([(x - bw) * s, (y0 - bw) * s, (x + cw + bw) * s - 1, (y0 + rh + bw) * s - 1], radius=(r + bw) * s, fill=255)
        ed.rounded_rectangle([x * s, y0 * s, (x + cw) * s - 1, (y0 + rh) * s - 1], radius=r * s, fill=0)
        hd.rounded_rectangle([x * s, y0 * s, (x + cw) * s - 1, (y0 + rh) * s - 1], radius=r * s, fill=0)
    blue = Image.new("RGBA", (W, H), BLUE)
    blue.putalpha(edge.resize((W, H), Image.LANCZOS))
    page.alpha_composite(blue)
    page.putalpha(Image.fromarray(np.minimum(np.asarray(page.getchannel("A")), np.asarray(hole.resize((W, H), Image.LANCZOS)))))
    for (x, cy, cw), lab in zip(cells, labels):
        if isinstance(lab, dict):
            logo_badge(page, lab["logo"], x, cy)
    return page


def logo_badge(frame, path, x, y):
    logo = Image.open(path).convert("RGBA")
    sc = min(240 / logo.width, 52 / logo.height)
    logo = logo.resize((round(logo.width * sc), round(logo.height * sc)), Image.LANCZOS)
    pad, bx, by = 14, x + 14, y + 14
    badge = [bx, by, bx + logo.width + 2 * pad, by + logo.height + 2 * pad]
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle([badge[0], badge[1] + 4, badge[2], badge[3] + 4], radius=16, fill=(0, 0, 0, 110))
    frame.alpha_composite(sh.filter(ImageFilter.GaussianBlur(8)))
    ImageDraw.Draw(frame).rounded_rectangle(badge, radius=16, fill=(255, 255, 255, 255))
    frame.alpha_composite(logo, (bx + pad, by + pad))


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
        builder = notepad_design if design.get("style") == "notepad" else grid_design
        frame = builder(design, cells, rh, [it.get("label") for it in items])
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


def pill(text, f, fg, bg, pad=(22, 8)):
    t = draw_line(text, f, fg)
    im = Image.new("RGBA", (t.width + 2 * pad[0], t.height + 2 * pad[1]), (0, 0, 0, 0))
    ImageDraw.Draw(im).rounded_rectangle([0, 0, im.width - 1, im.height - 1], radius=im.height // 2, fill=bg)
    im.alpha_composite(t, (pad[0], pad[1]))
    return im


def render_split(spec, out, tmp):
    """"Mientras tanto": two places at once, top and bottom, with a black band in between."""
    items = spec["items"][:2]
    dur = spec.get("duration", 7)
    band = spec.get("band_height", 200)
    rh = (H - band) // 2
    rh -= rh % 2
    inputs, parts, audio = [], [], None
    for i, item in enumerate(items):
        args, chain, has_audio = source({**item, "audio": item.get("audio", False), "sharp": True}, i, tmp, W, rh, dur)
        inputs += args
        parts.append(f"[{i}:v]{chain}[r{i}]")
        if has_audio and audio is None:
            audio = i
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    by = rh
    ImageDraw.Draw(layer).rectangle([0, by, W, by + band], fill=(0, 0, 0, 255))
    ImageDraw.Draw(layer).rectangle([0, by, W, by + 6], fill=BLUE)
    ImageDraw.Draw(layer).rectangle([0, by + band - 6, W, by + band], fill=BLUE)
    big = draw_line(spec.get("band", "Mientras tanto…"), font("bask", 64, 400), WHITE)
    layer.alpha_composite(big, ((W - big.width) // 2, by + (band - big.height) // 2))
    fl = font("raleway", 34, 700)
    for i, item in enumerate(items):
        if item.get("label"):
            p = pill(item["label"], fl, (4, 32, 47, 255), BLUE)
            y = max(300, i * (rh + band) + 40) if i == 0 else min(i * (rh + band) + rh - p.height - 40, BOTTOM - p.height)
            layer.alpha_composite(p, (40, y))
    text = text_layer(spec.get("text"))
    if text:
        layer.alpha_composite(text)
    inputs += overlay_input(layer, tmp, "t")
    n = len(items)
    parts.append(f"color=c=black:s={W}x{H}:r={FPS}:d={dur},format=yuv420p[bg]")
    parts.append(f"[bg][r0]overlay=0:0[s0]")
    parts.append(f"[s0][r1]overlay=0:{rh + band}[s1]")
    parts.append(f"[{n}:v]format=rgba,trim=duration={dur},setpts=PTS-STARTPTS[o]")
    parts.append(f"[s1][o]overlay=shortest=1,format=yuv420p[v]")
    if audio is not None:
        parts.append(f"[{audio}:a]atrim=duration={dur},asetpts=PTS-STARTPTS,loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000[a]")
    else:
        parts.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={dur}[a]")
    run([FFMPEG, "-y", "-v", "error", *inputs, "-filter_complex", ";".join(parts), "-map", "[v]", "-map", "[a]", "-t", str(dur), *ENCODE, str(out)])


def render_agenda(spec, out, tmp):
    """Calendar page over a darkened black-and-white background; rows appear one by one."""
    dur = spec.get("duration", 8)
    cal = spec["calendar"]
    args, chain, _ = source({**spec["items"][0], "audio": False}, 0, tmp, W, H, dur)
    chain += ",hue=s=0,eq=brightness=-0.18:contrast=1.05"
    cw, x0 = W - 120, 60
    y0 = cal.get("top", 330)
    rows = cal["rows"]
    ft, fb, fs, fp = font("raleway", 38, 700), font("raleway", 33, 700), font("raleway", 26, 500), font("raleway", 24, 700)
    ink, grey = (25, 25, 28, 255), (110, 114, 120, 255)
    tx_col = 92 + max(round(ft.getlength(r[0])) for r in rows) + 30  # text column, after the timeline and the longest time
    row_imgs = []
    for row in rows:
        when, what, where = row[:3]
        who = row[3] if len(row) > 3 else ""
        tl = draw_line(when, ft, BLUE)
        lines = [draw_line(ln, fb, ink) for ln in wrap(what, fb, cw - tx_col - 50)]
        if who:
            lines += [draw_line(ln, fs, grey) for ln in wrap(who, fs, cw - tx_col - 50)]
        body_h = sum(l.height for l in lines) + (48 if where else 0)
        h = max(tl.height, body_h) + 40
        im = Image.new("RGBA", (cw, h), (0, 0, 0, 0))
        di = ImageDraw.Draw(im)
        # timeline: a blue line through every row and a dot at each session
        di.line([(62, 0), (62, h)], fill=(59, 169, 240, 90), width=4)
        di.ellipse([50, 22, 74, 46], fill=BLUE, outline=(250, 250, 248, 255), width=4)
        im.alpha_composite(tl, (92, 16))
        y = 18
        for k, l in enumerate(lines):
            im.alpha_composite(l, (tx_col, y))
            y += l.height + (4 if k == len(lines) - 1 else 0)
        if where:
            im.alpha_composite(pill(where, fp, (4, 32, 47, 255), BLUE, (14, 5)), (tx_col, y + 8))
        row_imgs.append(im)
    head_h = 340
    card_h = head_h + sum(r.height for r in row_imgs) + 40 + (70 if cal.get("footer") else 0)
    if "top" not in cal:
        y0 = max(400, (H - card_h) // 2 - 40)  # centered, inside the area every phone shows
    card = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(card)
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle([x0, y0 + 30, x0 + cw, y0 + card_h + 40], radius=34, fill=(0, 0, 0, 170))
    card.alpha_composite(sh.filter(ImageFilter.GaussianBlur(26)))
    # pages underneath, so it reads as a desk calendar pad
    for k, tone in ((2, 214), (1, 232)):
        d.rounded_rectangle([x0 + 10 * k, y0 + 20, x0 + cw - 10 * k, y0 + card_h + 12 * k], radius=34, fill=(tone, tone, tone - 2, 255))
    d.rounded_rectangle([x0, y0, x0 + cw, y0 + card_h], radius=34, fill=(250, 250, 248, 255))
    head = Image.new("RGBA", (cw, head_h), BLUE)
    hd = ImageDraw.Draw(head)
    for k in range(-head_h, cw, 26):  # fine diagonal texture in a lighter tone of the same blue
        hd.line([(k, head_h), (k + head_h, 0)], fill=(88, 184, 244, 255), width=2)
    hm = Image.new("L", (cw, head_h), 0)
    ImageDraw.Draw(hm).rounded_rectangle([0, 0, cw - 1, head_h + 40], radius=34, fill=255)
    head.putalpha(hm)
    card.alpha_composite(head, (x0, y0))
    # binding: two punched holes with metal rings
    for i in range(2):
        hx = round(x0 + cw * (0.25 + 0.5 * i))
        d.ellipse([hx - 20, y0 + 22, hx + 20, y0 + 62], fill=(14, 14, 16, 255))
        d.rounded_rectangle([hx - 11, y0 - 40, hx + 11, y0 + 44], radius=11, fill=(150, 152, 156, 255))
        d.rounded_rectangle([hx - 5, y0 - 34, hx + 1, y0 + 36], radius=3, fill=(225, 226, 228, 255))
    dn = draw_line(cal["day_number"], font("inter", 200, 800), WHITE, 1.15)
    card.alpha_composite(dn, (x0 + 60, y0 + (head_h - dn.height) // 2 + 20))
    wd = draw_line(cal["weekday"], font("raleway", 56, 700), WHITE)
    mo = draw_line(cal["month"], font("raleway", 38, 600), (4, 32, 47, 255))
    tx = x0 + 60 + dn.width + 40
    card.alpha_composite(wd, (tx, y0 + head_h // 2 - wd.height + 20))
    card.alpha_composite(mo, (tx, y0 + head_h // 2 + 28))
    if cal.get("tag"):
        tg = pill(cal["tag"], font("raleway", 28, 700), BLUE, (255, 255, 255, 255), (18, 6))
        card.alpha_composite(tg, (x0 + cw - tg.width - 40, y0 + 86))
    if cal.get("footer"):
        ff = draw_line(cal["footer"], font("raleway", 26, 600), grey)
        fy = y0 + card_h - 62
        d.line([(x0 + 50, fy - 14), (x0 + cw - 50, fy - 14)], fill=(225, 225, 227, 255), width=2)
        card.alpha_composite(ff, ((W - ff.width) // 2, fy))
    if cal.get("title"):
        tt = draw_line(cal["title"], font("raleway", 64, 700), WHITE)
        tsh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        tsh.alpha_composite(tt, ((W - tt.width) // 2, y0 - 70 - tt.height))
        blur = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        blur.putalpha(tsh.getchannel("A"))
        card.alpha_composite(blur.filter(ImageFilter.GaussianBlur(12)))
        card.alpha_composite(tt, ((W - tt.width) // 2, y0 - 70 - tt.height))
    inputs = args + overlay_input(card, tmp, "card")
    parts = [f"[0:v]{chain}[b]", f"[1:v]format=rgba,fade=t=in:st=0:d=0.35:alpha=1,trim=duration={dur},setpts=PTS-STARTPTS[c]", "[b][c]overlay=shortest=1[s0]"]
    y = y0 + head_h + 10
    last = "s0"
    for i, im in enumerate(row_imgs):
        full = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        full.alpha_composite(im, (x0, y))
        y += im.height
        inputs += overlay_input(full, tmp, f"row{i}")
        st = 0.5 + 0.35 * i
        parts.append(f"[{i + 2}:v]format=rgba,fade=t=in:st={st}:d=0.35:alpha=1,trim=duration={dur},setpts=PTS-STARTPTS[q{i}]")
        parts.append(f"[{last}][q{i}]overlay=shortest=1[s{i + 1}]")
        last = f"s{i + 1}"
    parts.append(f"[{last}]format=yuv420p[v]")
    parts.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={dur}[a]")
    run([FFMPEG, "-y", "-v", "error", *inputs, "-filter_complex", ";".join(parts), "-map", "[v]", "-map", "[a]", "-t", str(dur), *ENCODE, str(out)])


def render_postcard(spec, out, tmp):
    """Thank-you postcard: the clip as the picture side over a blurred black-and-white copy,
    the message, a stamp and a Bogotá postmark."""
    dur = spec.get("duration", 9)
    pc = spec["postcard"]
    item = {**spec["items"][0], "audio": False}
    x0, y0, cw = 70, 400, W - 140
    wx, wy, ww = x0 + 36, y0 + 36, cw - 72
    wh = round(ww * 9 / 16) // 2 * 2
    a1, c1, _ = source(item, 0, tmp, W, H, dur)
    a2, c2, _ = source(item, 1, tmp, ww, wh, dur)
    c1 += ",hue=s=0,boxblur=18:2,eq=brightness=-0.22"
    ink, grey = (25, 25, 28, 255), (120, 124, 130, 255)
    ft, fm, fa = font("raleway", 56, 700), font("raleway", 34, 600), font("raleway", 28, 600)
    card = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(card)
    # message block, measured first so the card fits it: it wraps narrower beside the stamp
    ty = wy + wh + 46
    sw, shh = 190, 220
    title = draw_line(pc["title"], ft, BLUE) if pc.get("title") else None
    msg_y = ty + (title.height + 24 if title else 0)
    lines, cur, yy = [], "", msg_y
    lh = fm.getbbox("Ág")[3] + 10
    for word in pc["message"].split():
        width = ww - sw - 40 if yy < ty + shh + 10 else ww - 10
        probe = f"{cur} {word}".strip()
        if fm.getlength(probe) > width and cur:
            lines.append((cur, yy))
            cur, yy = word, yy + lh
        else:
            cur = probe
    if cur:
        lines.append((cur, yy))
        yy += lh
    fc = font("raleway", 46, 800)
    closing = [draw_line(ln, fc, BLUE) for ln in wrap(pc.get("closing", ""), fc, ww - 10)] if pc.get("closing") else []
    close_y = max(yy, ty + shh + 10) + 26
    pm_y = close_y + sum(c.height for c in closing) + 30
    end_y = pm_y + (210 if pc.get("postmark") else 0)
    ch = end_y - y0 + 30
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle([x0, y0 + 24, x0 + cw, y0 + ch + 24], radius=26, fill=(0, 0, 0, 170))
    card.alpha_composite(sh.filter(ImageFilter.GaussianBlur(24)))
    d.rounded_rectangle([x0, y0, x0 + cw, y0 + ch], radius=26, fill=(249, 248, 244, 255))
    # window for the clip, cut out of the card
    hole = Image.new("L", (W * 2, H * 2), 255)
    ImageDraw.Draw(hole).rounded_rectangle([wx * 2, wy * 2, (wx + ww) * 2 - 1, (wy + wh) * 2 - 1], radius=36, fill=0)
    card.putalpha(Image.fromarray(np.minimum(np.asarray(card.getchannel("A")), np.asarray(hole.resize((W, H), Image.LANCZOS)))))
    if title:
        card.alpha_composite(title, (wx, ty))
    # stamp: perforated edge, blue face, event name; the postmark goes at the bottom of the card
    sx, sy = wx + ww - sw, ty - 6
    st = Image.new("RGBA", (sw, shh), (0, 0, 0, 0))
    sd = ImageDraw.Draw(st)
    sd.rectangle([0, 0, sw - 1, shh - 1], fill=(255, 255, 255, 255))
    for k in range(0, sw, 16):
        sd.ellipse([k - 5, -5, k + 5, 5], fill=(0, 0, 0, 0))
        sd.ellipse([k - 5, shh - 5, k + 5, shh + 5], fill=(0, 0, 0, 0))
    for k in range(0, shh, 16):
        sd.ellipse([-5, k - 5, 5, k + 5], fill=(0, 0, 0, 0))
        sd.ellipse([sw - 5, k - 5, sw + 5, k + 5], fill=(0, 0, 0, 0))
    sd.rectangle([14, 14, sw - 15, shh - 15], fill=BLUE)
    for i, (t, sz) in enumerate(pc.get("stamp", [])):
        li = draw_line(t, font("raleway", sz, 800 if i == 0 else 700), WHITE)
        st.alpha_composite(li, ((sw - li.width) // 2, 40 + i * 56))
    card.alpha_composite(st.rotate(4, Image.BICUBIC, expand=True), (sx - 6, sy))
    if pc.get("postmark"):
        pm = Image.new("RGBA", (420, 220), (0, 0, 0, 0))
        pd = ImageDraw.Draw(pm)
        pd.ellipse([20, 20, 200, 200], outline=(40, 40, 46, 190), width=5)
        pd.ellipse([38, 38, 182, 182], outline=(40, 40, 46, 150), width=3)
        for k, line_t in enumerate(pc["postmark"]):
            li = draw_line(line_t, font("raleway", 24 if k else 26, 800), (40, 40, 46, 200))
            pm.alpha_composite(li, (110 - li.width // 2, 76 + k * 36))
        for k in range(3):
            pts = [(205 + j * 6, 70 + k * 30 + 8 * np.sin(j / 2.2)) for j in range(26)]
            pd.line(pts, fill=(40, 40, 46, 160), width=4)
        card.alpha_composite(pm.rotate(-6, Image.BICUBIC, expand=True), (wx - 20, pm_y - 20))
    for text, ly in lines:
        card.alpha_composite(draw_line(text, fm, ink), (wx, ly))
    y = close_y
    for c in closing:
        card.alpha_composite(c, (wx, y))
        y += c.height
    inputs = a1 + a2 + overlay_input(card, tmp, "card")
    parts = [
        f"[0:v]{c1}[bg]",
        f"[1:v]{c2}[win]",
        f"[bg][win]overlay={wx}:{wy}[s0]",
        f"[2:v]format=rgba,fade=t=in:st=0:d=0.5:alpha=1,trim=duration={dur},setpts=PTS-STARTPTS[c]",
        "[s0][c]overlay=shortest=1,format=yuv420p[v]",
        f"anullsrc=r=48000:cl=stereo,atrim=duration={dur}[a]",
    ]
    run([FFMPEG, "-y", "-v", "error", *inputs, "-filter_complex", ";".join(parts), "-map", "[v]", "-map", "[a]", "-t", str(dur), *ENCODE, str(out)])


def render_board(spec, out, tmp):
    """Airport departures board: the clip in a window on top, rows below that flip into place."""
    dur = spec.get("duration", 9)
    bd = spec["board"]
    item = {**spec["items"][0], "audio": False}
    x0, cw = 50, W - 100
    wy = bd.get("window_top", 430)
    ww = cw
    wh = round(ww * 9 / 16) // 2 * 2
    a1, c1, _ = source(item, 0, tmp, W, H, dur)
    a2, c2, _ = source(item, 1, tmp, ww, wh, dur)
    c1 += ",hue=s=0,boxblur=22:2,eq=brightness=-0.32"
    fh, fr, fs = font("inter", 26, 700), font("inter", 36, 800), font("inter", 30, 700)
    cols = bd.get("cols", [("HORA", 170), ("SALÓN", 120), ("MESA", 470), ("ESTADO", 190)])
    rows = bd["rows"]
    top_y = wy + wh + 40
    title = draw_line(bd["title"], font("raleway", 64, 700), WHITE)
    sub = draw_line(bd.get("subtitle", ""), font("raleway", 36, 600), BLUE) if bd.get("subtitle") else None
    rh, gap = 74, 12
    head_y = top_y
    first_row = head_y + 56
    end_y = first_row + len(rows) * (rh + gap)
    rng = np.random.default_rng(7)
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

    def base():
        im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.rounded_rectangle([x0 - 20, top_y - 30, x0 + cw + 20, end_y + 110], radius=28, fill=(14, 14, 16, 235))
        ty = wy - 40 - (title.height + (sub.height + 6 if sub else 0))
        im.alpha_composite(title, ((W - title.width) // 2, ty))
        if sub:
            im.alpha_composite(sub, ((W - sub.width) // 2, ty + title.height + 6))
        x = x0
        for name, w in cols:
            hd = draw_line(name, fh, (150, 154, 160, 255))
            im.alpha_composite(hd, (x + 10, head_y))
            x += w
        if bd.get("footer"):
            ft = draw_line(bd["footer"], font("raleway", 40, 700), BLUE)
            im.alpha_composite(ft, ((W - ft.width) // 2, end_y + 30))
        return im

    def cell(d, im, x, y, w, text, color, f):
        d.rounded_rectangle([x, y, x + w - 10, y + rh], radius=8, fill=(34, 35, 38, 255))
        d.line([(x, y + rh // 2), (x + w - 10, y + rh // 2)], fill=(10, 10, 12, 255), width=3)
        t = draw_line(text, f, color)
        if t.width > w - 30:
            t = t.resize((w - 30, t.height), Image.LANCZOS)
        im.alpha_composite(t, (x + 12, y + (rh - t.height) // 2))

    frames_dir = Path(tmp) / "board"
    frames_dir.mkdir()
    anim = 2.6
    nfr = round(anim * FPS)
    static = base()
    for n in range(nfr + 1):
        t = n / FPS
        im = static.copy()
        d = ImageDraw.Draw(im)
        for r, row in enumerate(rows):
            y = first_row + r * (rh + gap)
            x = x0
            settle = 0.35 + r * 0.28
            for c, ((_, w), val) in enumerate(zip(cols, row)):
                done = t >= settle + c * 0.12
                if t < 0.15 + r * 0.28:
                    shown = ""
                elif done:
                    shown = val
                else:
                    shown = "".join(ch if ch == " " else letters[rng.integers(len(letters))] for ch in val)
                color = BLUE if (c == 0 or c == len(cols) - 1) else WHITE
                cell(d, im, x, y, w, shown, color, fr if c == 2 else fs)
                x += w
        im.save(frames_dir / f"f{n:03d}.png")
    inputs = a1 + a2 + ["-framerate", str(FPS), "-i", str(frames_dir / "f%03d.png")]
    parts = [
        f"[0:v]{c1}[bg]",
        f"[1:v]{c2}[win]",
        f"[bg][win]overlay={x0}:{wy}[s0]",
        f"[2:v]format=rgba[bo]",
        "[s0][bo]overlay=eof_action=repeat,format=yuv420p[v]",
        f"anullsrc=r=48000:cl=stereo,atrim=duration={dur}[a]",
    ]
    run([FFMPEG, "-y", "-v", "error", *inputs, "-filter_complex", ";".join(parts), "-map", "[v]", "-map", "[a]", "-t", str(dur), *ENCODE, str(out)])


def render_plan(spec, out, tmp):
    """Floor-plan look: rooms drawn in event-blue lines with a table, chairs, door swing and the
    sessions of each room; the clip plays in an inset. The drawing wipes in from the top."""
    dur = spec.get("duration", 9)
    pl = spec["plan"]
    item = {**spec["items"][0], "audio": False}
    s = 2
    ink = (59, 169, 240, 255)
    soft = (59, 169, 240, 90)
    a1, c1, _ = source(item, 0, tmp, W, H, dur)
    c1 += ",hue=s=0,boxblur=24:2,eq=brightness=-0.4"
    iw_, ih_ = 640, 360
    ix, iy = (W - iw_) // 2, pl.get("inset_top", 1760)
    a2, c2, _ = source(item, 1, tmp, iw_, ih_, dur)
    big = Image.new("RGBA", (W * s, H * s), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    S = lambda v: round(v * s)
    title = draw_line(pl["title"], font("raleway", 64, 700), WHITE)
    sub = draw_line(pl.get("subtitle", ""), font("raleway", 36, 600), BLUE) if pl.get("subtitle") else None
    rooms = pl["rooms"]
    x0, x1 = 70, W - 70
    top = pl.get("top", 520)
    rh = pl.get("room_height", 560)
    corridor = 90
    # panel behind the plan
    d.rounded_rectangle([S(x0 - 30), S(top - 40), S(x1 + 30), S(top + len(rooms) * rh + (len(rooms) - 1) * corridor + 40)], radius=S(28), fill=(10, 12, 16, 225))
    fl, ft, fb = font("inter", 30, 800), font("raleway", 34, 700), font("raleway", 32, 600)
    texts = []
    for k, room in enumerate(rooms):
        y0 = top + k * (rh + corridor)
        y1 = y0 + rh
        wall = S(6)
        door_x = x1 - 240
        # walls with a door gap at the bottom-left, and the door swing
        d.line([(S(x0), S(y0)), (S(x1), S(y0))], fill=ink, width=wall)
        d.line([(S(x0), S(y0)), (S(x0), S(y1))], fill=ink, width=wall)
        d.line([(S(x1), S(y0)), (S(x1), S(y1))], fill=ink, width=wall)
        d.line([(S(x0), S(y1)), (S(door_x), S(y1))], fill=ink, width=wall)
        d.line([(S(door_x + 110), S(y1)), (S(x1), S(y1))], fill=ink, width=wall)
        d.line([(S(door_x), S(y1)), (S(door_x), S(y1 - 110))], fill=ink, width=S(3))
        d.arc([S(door_x - 110), S(y1 - 110), S(door_x + 110), S(y1 + 110)], 270, 360, fill=soft, width=S(3))
        # table and chairs on the left half
        tx0, ty0, tx1, ty1 = x0 + 70, y0 + 140, x0 + 340, y0 + rh - 170
        d.rounded_rectangle([S(tx0), S(ty0), S(tx1), S(ty1)], radius=S(14), outline=ink, width=S(4))
        n = room.get("chairs", 8)
        per = n // 2
        for i in range(per):
            cx = tx0 + (i + 0.5) * (tx1 - tx0) / per
            for cy in (ty0 - 32, ty1 + 32):
                d.ellipse([S(cx - 18), S(cy - 18), S(cx + 18), S(cy + 18)], outline=ink, width=S(3))
        texts.append((draw_line(room["name"], fl, BLUE), (x0 + 24, y0 + 26)))
        # sessions of the room on the right half
        sx = x0 + 420
        sy = y0 + 110
        tw = max(round(ft.getlength(t)) for t, _ in room["sessions"]) + 24
        for when, what in room["sessions"]:
            tl = draw_line(when, ft, BLUE)
            texts.append((tl, (sx, sy)))
            yy = sy + 3
            for ln in wrap(what, fb, x1 - 30 - sx - tw):
                li = draw_line(ln, fb, WHITE)
                texts.append((li, (sx + tw, yy)))
                yy += li.height
            d.line([(S(sx), S(max(sy + tl.height, yy) + 14)), (S(x1 - 40), S(max(sy + tl.height, yy) + 14))], fill=(59, 169, 240, 60), width=S(2))
            sy = max(sy + tl.height, yy) + 36
        if k < len(rooms) - 1:
            cy = y1 + corridor // 2
            lab = draw_line(pl.get("corridor", "Pasillo"), font("raleway", 26, 600), (160, 170, 180, 255))
            texts.append((lab, ((W - lab.width) // 2, cy - lab.height // 2)))
            for xx in range(x0 + 20, x1 - 20, 40):
                if abs(xx - W // 2) > lab.width // 2 + 30:
                    d.line([(S(xx), S(cy)), (S(xx + 18), S(cy))], fill=soft, width=S(3))
    # inset frame for the clip
    d.rounded_rectangle([S(ix - 6), S(iy - 6), S(ix + iw_ + 6), S(iy + ih_ + 6)], radius=S(22), outline=ink, width=S(5))
    plan = big.resize((W, H), Image.LANCZOS)
    for im, pos in texts:
        plan.alpha_composite(im, pos)
    ty = top - 70 - title.height - (sub.height + 6 if sub else 0)
    plan.alpha_composite(title, ((W - title.width) // 2, ty))
    if sub:
        plan.alpha_composite(sub, ((W - sub.width) // 2, ty + title.height + 6))
    if pl.get("inset_label"):
        il = pill(pl["inset_label"], font("raleway", 28, 700), (4, 32, 47, 255), BLUE, (16, 6))
        plan.alpha_composite(il, ((W - il.width) // 2, iy - il.height - 22))
    hole = Image.new("L", (W, H), 255)
    ImageDraw.Draw(hole).rounded_rectangle([ix, iy, ix + iw_, iy + ih_], radius=18, fill=0)
    plan.putalpha(Image.fromarray(np.minimum(np.asarray(plan.getchannel("A")), np.asarray(hole))))
    # wipe-in frames: the plan draws itself from top to bottom
    frames_dir = Path(tmp) / "plan"
    frames_dir.mkdir()
    nfr = round(1.8 * FPS)
    alpha = np.asarray(plan.getchannel("A")).astype(np.float32)
    rows = np.arange(H, dtype=np.float32)[:, None]
    for n in range(nfr + 1):
        edge = (n / nfr) * (H + 200)
        m = np.clip((edge - rows) / 200, 0, 1)
        fr = plan.copy()
        fr.putalpha(Image.fromarray((alpha * m).astype(np.uint8)))
        fr.save(frames_dir / f"f{n:03d}.png")
    inputs = a1 + a2 + ["-framerate", str(FPS), "-i", str(frames_dir / "f%03d.png")]
    parts = [
        f"[0:v]{c1}[bg]",
        f"[1:v]{c2}[win]",
        f"[bg][win]overlay={ix}:{iy}[s0]",
        "[2:v]format=rgba[pl]",
        "[s0][pl]overlay=eof_action=repeat,format=yuv420p[v]",
        f"anullsrc=r=48000:cl=stereo,atrim=duration={dur}[a]",
    ]
    run([FFMPEG, "-y", "-v", "error", *inputs, "-filter_complex", ";".join(parts), "-map", "[v]", "-map", "[a]", "-t", str(dur), *ENCODE, str(out)])


def badge_card(b, bw, bh):
    """One event badge: blue header with the room, its sessions, and a slot for the lanyard clip."""
    s = 2
    im = Image.new("RGBA", (bw * s, bh * s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, bw * s - 1, bh * s - 1], radius=28 * s, fill=(250, 250, 248, 255))
    hh = 190
    col = tuple(b.get("color", BLUE))
    d.rounded_rectangle([0, 0, bw * s - 1, hh * s], radius=28 * s, fill=col)
    d.rectangle([0, (hh - 30) * s, bw * s - 1, hh * s], fill=col)
    d.rounded_rectangle([(bw / 2 - 50) * s, 22 * s, (bw / 2 + 50) * s, 40 * s], radius=9 * s, fill=(20, 20, 22, 255))
    im = im.resize((bw, bh), Image.LANCZOS)
    on = tuple(b.get("on_color", (4, 32, 47, 255)))
    kick = draw_line(b.get("kicker", "POLICY LABS"), font("inter", 24, 700), on)
    im.alpha_composite(kick, ((bw - kick.width) // 2, 64))
    name = draw_line(b["name"], font("inter", 56, 800), tuple(b.get("name_color", WHITE)), 1.15)
    im.alpha_composite(name, ((bw - name.width) // 2, 98))
    ft, fb = font("raleway", b.get("time_size", 30), 700), font("raleway", b.get("text_size", 33), 700)
    acc = tuple(b.get("accent", BLUE))
    y = hh + 34
    ink = (25, 25, 28, 255)
    for when, what in b["sessions"]:
        t = draw_line(when, ft, acc)
        im.alpha_composite(t, (34, y))
        y += t.height + 2
        for ln in wrap(what, fb, bw - 68):
            li = draw_line(ln, fb, ink)
            im.alpha_composite(li, (34, y))
            y += li.height
        y += b.get("row_gap", 22)
        ImageDraw.Draw(im).line([(34, y - 12), (bw - 34, y - 12)], fill=(222, 222, 225, 255), width=2)
    if b.get("footer"):
        f = draw_line(b["footer"], font("raleway", 24, 600), (120, 124, 130, 255))
        im.alpha_composite(f, ((bw - f.width) // 2, bh - f.height - 26))
    return im


def render_badges(spec, out, tmp):
    """Room-by-room schedule as event badges hanging from lanyards; they drop in and swing to rest.
    Layers: blurred clip, lanyards (behind), framed clip window, badges and texts (in front)."""
    dur = spec.get("duration", 9)
    bg = spec["badges"]
    item = {**spec["items"][0], "audio": False}
    ww = bg.get("window_width", W - 180)
    wh = round(ww * 9 / 16) // 2 * 2
    wx, wy = (W - ww) // 2, bg.get("window_top", 440)
    col = tuple(bg.get("color", BLUE))
    col_dark = tuple(bg.get("color_dark", (30, 128, 196, 255)))
    a1, c1, _ = source(item, 0, tmp, W, H, dur)
    a2, c2, _ = source(item, 1, tmp, ww, wh, dur)
    c1 += ",hue=s=0,boxblur=24:2,eq=brightness=-0.42"
    cards = bg["cards"]
    n = len(cards)
    side = bg.get("side", 90)  # margin to the screen edges
    bw = (W - 2 * side - 40 * (n - 1)) // n
    bh = bg.get("badge_height", 760)
    top_y = wy + wh + bg.get("drop", 170)
    imgs = [badge_card(c, bw, bh) for c in cards]
    title = draw_line(bg["title"], font("raleway", 70, 800), col)
    fsub = font("raleway", 36, 600)
    subs = [draw_line(ln, fsub, WHITE) for ln in wrap(bg.get("subtitle", ""), fsub, W - 160)] if bg.get("subtitle") else []
    static = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ty = wy - 46 - title.height - sum(sl.height + 4 for sl in subs) - (8 if subs else 0)
    static.alpha_composite(title, ((W - title.width) // 2, ty))
    yy = ty + title.height + 8
    for sl in subs:
        static.alpha_composite(sl, ((W - sl.width) // 2, yy))
        yy += sl.height + 4
    # frame around the clip window: rounded blue border with a soft shadow
    fr_ = Image.new("RGBA", (W * 2, H * 2), (0, 0, 0, 0))
    ImageDraw.Draw(fr_).rounded_rectangle([(wx - 8) * 2, (wy - 8) * 2, (wx + ww + 8) * 2, (wy + wh + 8) * 2], radius=34 * 2, outline=col, width=7 * 2)
    static.alpha_composite(fr_.resize((W, H), Image.LANCZOS))
    if bg.get("cta"):
        cy = top_y + bh + 50
        for k, ln in enumerate(bg["cta"]):
            li = draw_line(ln, font("raleway", 46 if k == 0 else 34, 700 if k == 0 else 600), col if k == 0 else WHITE)
            static.alpha_composite(li, ((W - li.width) // 2, cy))
            cy += li.height + 8
    mask = Image.new("L", (ww * 2, wh * 2), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, ww * 2 - 1, wh * 2 - 1], radius=28 * 2, fill=255)
    mask_path = Path(tmp) / "winmask.png"
    mask.resize((ww, wh), Image.LANCZOS).save(mask_path)
    back_dir, front_dir = Path(tmp) / "back", Path(tmp) / "front"
    back_dir.mkdir()
    front_dir.mkdir()
    nfr = round(3.2 * FPS)
    for f in range(nfr + 1):
        t = f / FPS
        back = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        front = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        bd = ImageDraw.Draw(back)
        for i, im in enumerate(imgs):
            cx = side + i * (bw + 40) + bw // 2
            st = 0.15 + 0.35 * i
            p = min(1, max(0, (t - st) / 0.65))
            # the badge slides straight down out of the clip window's bottom edge, then swings a little
            rise = top_y - (wy + wh - bh - 40)
            drop = -(1 - (1 - (1 - p) ** 3)) * rise if p < 1 else 0
            hit = t - st - 0.65
            side = 1 if i % 2 == 0 else -1
            ang = 3.5 * side * np.exp(-hit * 2.6) * np.sin(hit * 7) if hit > 0 else 0
            clip_y = round(top_y + drop)
            # lanyards start behind the clip window, so they never cross the titles
            if clip_y - 30 > wy + 60:
                for off in (-34, 34):
                    bd.line([(cx + off * 1.6, wy + 40), (cx + off * 0.5, clip_y - 30)], fill=col_dark, width=26)
                    bd.line([(cx + off * 1.6 - 6, wy + 40), (cx + off * 0.5 - 6, clip_y - 30)], fill=col, width=10)
            rot = im.rotate(ang, Image.BICUBIC, expand=True, center=(bw // 2, 0))
            sh = Image.new("RGBA", rot.size, (0, 0, 0, 0))
            sh.putalpha(rot.getchannel("A").point(lambda a: a * 0.55))
            card = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            fd = ImageDraw.Draw(card)
            fd.rounded_rectangle([cx - 22, clip_y - 46, cx + 22, clip_y + 30], radius=8, fill=(185, 188, 192, 255))
            fd.rounded_rectangle([cx - 10, clip_y - 38, cx + 10, clip_y + 22], radius=5, fill=(120, 124, 128, 255))
            paste(card, sh.filter(ImageFilter.GaussianBlur(14)), round(cx - rot.width / 2 + 8), clip_y + 16)
            paste(card, rot, round(cx - rot.width / 2), clip_y)
            # nothing of the badge shows above the window's bottom edge: it comes out of the image
            a = np.asarray(card.getchannel("A")).astype(np.float32) * min(1.0, p * 2.2)
            a[: wy + wh] = 0
            a = a.astype(np.uint8)
            card.putalpha(Image.fromarray(a))
            back.alpha_composite(card)
        merged = static.copy()
        merged.alpha_composite(front)
        back.save(back_dir / f"f{f:03d}.png")
        merged.save(front_dir / f"f{f:03d}.png")
    inputs = a1 + a2 + ["-framerate", str(FPS), "-i", str(back_dir / "f%03d.png"),
                        "-framerate", str(FPS), "-i", str(front_dir / "f%03d.png"),
                        "-loop", "1", "-framerate", str(FPS), "-i", str(mask_path)]
    parts = [
        f"[0:v]{c1}[bg]",
        "[2:v]format=rgba[bk]",
        "[bg][bk]overlay=eof_action=repeat[s0]",
        f"[1:v]{c2},format=rgba[w0]",
        f"[4:v]format=gray,trim=duration={dur},setpts=PTS-STARTPTS[m]",
        "[w0][m]alphamerge[win]",
        f"[s0][win]overlay={wx}:{wy}:shortest=1[s1]",
        "[3:v]format=rgba[fr]",
        "[s1][fr]overlay=eof_action=repeat,format=yuv420p[v]",
        f"anullsrc=r=48000:cl=stereo,atrim=duration={dur}[a]",
    ]
    run([FFMPEG, "-y", "-v", "error", *inputs, "-filter_complex", ";".join(parts), "-map", "[v]", "-map", "[a]", "-t", str(dur), *ENCODE, str(out)])


def render_opener(spec, out, tmp):
    """Day opener: fast flashes of the previous day, then a giant counter that flips to the new
    day number over a black-and-white photo, the word DÍA and a closing line."""
    op = spec["opener"]
    flashes = spec["items"][:-1]
    photo = spec["items"][-1]
    part_a = Path(tmp) / "a.mp4"
    render_cuts({"layout": "cuts", "items": [{**it, "audio": False} for it in flashes]}, part_a, tmp)
    dur_b = op.get("duration", 4.2)
    base = ImageOps.exif_transpose(Image.open(photo["src"])).convert("L")
    base = ImageOps.fit(base, (round(W * 1.1), round(H * 1.1)), Image.LANCZOS, centering=photo.get("focus", [0.5, 0.5]))
    base = Image.eval(ImageOps.autocontrast(base, cutoff=1), lambda v: int(v * 0.55)).convert("RGB")
    fnum = font("inter", 520, 800)
    old_n = draw_line(op.get("from", "01"), fnum, WHITE, 1.2)
    new_n = draw_line(op.get("to", "02"), fnum, BLUE, 1.2)
    word = draw_line(op.get("word", "DÍA"), font("inter", 120, 800), WHITE, 1.2)
    sub = draw_line(op.get("sub", "GovTech Summit 2026"), font("raleway", 44, 600), WHITE)
    tag = text_layer({"lines": [op["tagline"]], "size": 54, "pos": H // 2 + 640}) if op.get("tagline") else None
    nh = max(old_n.height, new_n.height)
    cy = H // 2 - 40
    frames = round(dur_b * FPS)
    enc = subprocess.Popen(
        [FFMPEG, "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
         "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-shortest", *ENCODE, str(Path(tmp) / "b.mp4")],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    for n in range(frames):
        t = n / FPS
        # photo fades in behind the counter and keeps a slow push-in
        pa = min(1, max(0, (t - 0.9) / 0.6))
        z = 1 + 0.06 * (t / dur_b)
        cw_, ch_ = round(W * 1.1 / z), round(H * 1.1 / z)
        ox, oy = (base.width - cw_) // 2, (base.height - ch_) // 2
        bgf = base.crop((ox, oy, ox + cw_, oy + ch_)).resize((W, H), Image.BILINEAR)
        frame = Image.blend(Image.new("RGB", (W, H), (0, 0, 0)), bgf, pa).convert("RGBA")
        # counter: 01 slides up and out, 02 rises in from below inside a fixed window
        win = Image.new("RGBA", (W, nh + 40), (0, 0, 0, 0))
        p = min(1, max(0, (t - 0.55) / 0.45))
        e = 1 - (1 - p) ** 3
        win.alpha_composite(old_n, ((W - old_n.width) // 2, round(20 - e * (nh + 40))))
        win.alpha_composite(new_n, ((W - new_n.width) // 2, round(20 + (1 - e) * (nh + 40))))
        frame.alpha_composite(win, (0, cy - nh // 2 - 20))
        wa = min(1, max(0, (t - 1.2) / 0.4))
        if wa > 0:
            w2 = word.copy()
            w2.putalpha(w2.getchannel("A").point(lambda v: int(v * wa)))
            frame.alpha_composite(w2, ((W - word.width) // 2, cy - nh // 2 - word.height - 10 + round((1 - wa) * 30)))
            s2 = sub.copy()
            s2.putalpha(s2.getchannel("A").point(lambda v: int(v * wa)))
            frame.alpha_composite(s2, ((W - sub.width) // 2, cy + nh // 2 + 20))
            lw = round(320 * wa)
            ImageDraw.Draw(frame).rectangle([W // 2 - lw // 2, cy + nh // 2 + 20 + sub.height + 14, W // 2 + lw // 2, cy + nh // 2 + 20 + sub.height + 20], fill=BLUE)
        ta = min(1, max(0, (t - 2.0) / 0.5))
        if tag and ta > 0:
            t2 = tag.copy()
            t2.putalpha(t2.getchannel("A").point(lambda v: int(v * ta)))
            frame.alpha_composite(t2)
        enc.stdin.write(frame.convert("RGB").tobytes())
    enc.stdin.close()
    err = enc.stderr.read().decode(errors="replace")
    if enc.wait():
        raise RuntimeError(err[-1500:])
    lst = Path(tmp) / "list.txt"
    lst.write_text(f"file '{part_a.as_posix()}'\nfile '{(Path(tmp) / 'b.mp4').as_posix()}'\n")
    run([FFMPEG, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", "-movflags", "+faststart", str(out)])


def calendar_page(pw, ph, month, weekday, number, lines, seed):
    """One tear-off calendar page on paper texture: blue month band, big day number, lines below."""
    paper = paper_sheet(max(pw, ph), seed).crop((0, 0, pw, ph))
    d = ImageDraw.Draw(paper)
    band = 150
    d.rectangle([0, 0, pw, band], fill=BLUE)
    mo = draw_line(month, font("raleway", 50, 800), WHITE)
    paper.alpha_composite(mo, ((pw - mo.width) // 2, 34))
    wd = draw_line(weekday, font("raleway", 40, 700), (90, 94, 100, 255))
    paper.alpha_composite(wd, ((pw - wd.width) // 2, band + 26))
    num = draw_line(number, font("inter", 330, 800), (22, 22, 26, 255), 1.1)
    ny = band + 70
    paper.alpha_composite(num, ((pw - num.width) // 2, ny))
    y = ny + num.height + 4
    for text, size, color, weight in lines:
        li = draw_line(text, font("raleway", size, weight), color)
        paper.alpha_composite(li, ((pw - li.width) // 2, y))
        y += li.height + 2
    return paper


def render_tearoff(spec, out, tmp):
    """Day opener: a desk calendar over the clip; yesterday's page tears off and falls,
    revealing today's page; then a line and the logo."""
    dur = spec.get("duration", 7)
    tc = spec["tearoff"]
    item = {**spec["items"][0], "audio": False}
    a1, c1, _ = source(item, 0, tmp, W, H, dur)
    c1 += f",hue=s={tc.get('bg_saturation', 0)},eq=brightness={tc.get('bg_brightness', -0.25)}:contrast=1.08"
    pw, ph = 740, 880
    old = calendar_page(pw, ph, tc["month"], tc["old_weekday"], tc["old_number"], [], 3)
    new = calendar_page(pw, ph, tc["month"], tc["weekday"], tc["number"], [
        (tc.get("top", "Bienvenidos al"), 46, (25, 25, 28, 255), 600),
        (tc.get("big", "DÍA 2"), 76, BLUE, 800),
        (tc.get("bottom", "del GovTech Summit"), 40, (25, 25, 28, 255), 600),
    ], 5)
    under = paper_sheet(ph, 9).crop((0, 0, pw, ph))
    cx, top = W // 2, tc.get("top_y", 560)
    caption = text_layer({"lines": [tc["caption"]], "size": 50, "pos": top + ph + 250}) if tc.get("caption") else None
    logo = None
    if tc.get("logo"):
        src = Image.open(tc["logo"]).convert("RGBA")
        white = Image.new("RGBA", src.size, (255, 255, 255, 255))
        white.putalpha(src.getchannel("A"))
        lw = 500
        logo = white.resize((lw, round(src.height * lw / src.width)), Image.LANCZOS)
    rings = Image.new("RGBA", (W * 2, 140), (0, 0, 0, 0))
    rd = ImageDraw.Draw(rings)
    for k in range(2):
        rx = (cx - pw // 2 + pw * (0.25 + 0.5 * k)) * 2
        rd.rounded_rectangle([rx - 22, 0, rx + 22, 150], radius=22, fill=(150, 152, 156, 255))
        rd.rounded_rectangle([rx - 10, 12, rx + 2, 138], radius=6, fill=(228, 229, 231, 255))
    rings = rings.resize((W, 70), Image.LANCZOS)
    shadow = Image.new("L", (pw + 160, ph + 160), 0)
    ImageDraw.Draw(shadow).rectangle([80, 96, pw + 80, ph + 96], fill=180)
    shadow = shadow.filter(ImageFilter.GaussianBlur(26))
    sh_rgba = Image.new("RGBA", shadow.size, (0, 0, 0, 255))
    sh_rgba.putalpha(shadow)
    frames_dir = Path(tmp) / "tear"
    frames_dir.mkdir()
    nfr = round(3.6 * FPS)
    for n in range(nfr + 1):
        t = n / FPS
        fr = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        p = min(1, max(0, t / 0.6))
        dy = round((1 - ease_out_back(p, 1.1)) * -900) if p < 1 else 0
        y0 = top + dy
        paste(fr, sh_rgba, cx - pw // 2 - 80, y0 - 60)
        # pad thickness: two sheet edges under the pages
        paste(fr, under, cx - pw // 2 + 8, y0 + 14)
        paste(fr, under, cx - pw // 2 + 4, y0 + 7)
        paste(fr, new, cx - pw // 2, y0)
        tt = t - 1.0  # yesterday's page lifts, tears and falls away
        if tt < 0:
            paste(fr, old, cx - pw // 2, y0)
        elif tt < 1.4:
            ang = -min(40, 10 + tt * 45)
            fall = round(0.5 * 2600 * tt ** 2)
            shift = round(tt * 260)
            pg = old.copy()
            if tt > 0.6:
                pg.putalpha(pg.getchannel("A").point(lambda v: int(v * max(0, 1 - (tt - 0.6) / 0.8))))
            rot = pg.rotate(ang, Image.BICUBIC, expand=True)
            paste(fr, rot, cx - rot.width // 2 + shift, y0 - 40 + fall)
        fr.alpha_composite(rings, (0, y0 - 34))
        ca = min(1, max(0, (t - 2.3) / 0.5))
        if caption and ca > 0:
            c2 = caption.copy()
            c2.putalpha(c2.getchannel("A").point(lambda v: int(v * ca)))
            fr.alpha_composite(c2)
        la = min(1, max(0, (t - 2.8) / 0.5))
        if logo and la > 0:
            l2 = logo.copy()
            l2.putalpha(l2.getchannel("A").point(lambda v: int(v * la)))
            paste(fr, l2, (W - logo.width) // 2, H - 330 - logo.height)
        fr.save(frames_dir / f"f{n:03d}.png")
    inputs = a1 + ["-framerate", str(FPS), "-i", str(frames_dir / "f%03d.png")]
    parts = [
        f"[0:v]{c1}[bg]",
        "[1:v]format=rgba[cal]",
        "[bg][cal]overlay=eof_action=repeat,format=yuv420p[v]",
        f"anullsrc=r=48000:cl=stereo,atrim=duration={dur}[a]",
    ]
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
    caption = None
    if note.get("caption"):
        # a short line under the note, white with a soft shadow
        caption = text_layer({"lines": [note["caption"]], "size": 50, "pos": H // 2 + 560})
    for t in (small1, big, small2):
        if EMOJI.search(t):
            raise ValueError(f"emoji in text: {t}")
    base = ImageOps.exif_transpose(Image.open(spec["items"][0]["src"])).convert("L")
    base = ImageOps.autocontrast(base, cutoff=1)
    bw = round(W * 1.08)
    bh = round(H * 1.08)
    bg_big = ImageOps.fit(base, (bw, bh), Image.LANCZOS, centering=spec["items"][0].get("focus", [0.5, 0.5]))
    bg_big = Image.eval(bg_big, lambda v: int(v * 0.72)).convert("RGB")

    size = note.get("size", 700)  # leaves a margin to the screen edges
    paper = paper_sheet(size, 1)
    under = paper_sheet(size, 2)
    rows = []
    # Raleway Bold like the rest of the stories; "inter" keeps the campaign's stretched title
    family = note.get("font", "raleway")
    small_w, big_w, big_stretch = (700, 800, 1.0) if family == "raleway" else (500, 800, 1.28)
    fs1 = 58
    while max(font(family, fs1, small_w).getlength(t) for t in (small1, small2)) > size - 110:
        fs1 -= 2
    f1 = font(family, fs1, small_w)
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
                paste(frame, lg, (W - lg.width) // 2, note["logo_y"] if "logo_y" in note else ((H - 330 - lg.height) if note.get("caption") else (BOTTOM - lg.height - 10)))
        if caption:
            a = min(1, max(0, (t - 2.0) / 0.5))
            if a > 0:
                cl = caption.copy()
                cl.putalpha(cl.getchannel("A").point(lambda v: int(v * a)))
                frame.alpha_composite(cl)
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
        {"cuts": render_cuts, "stack": render_stack, "grid": render_grid, "split": render_split, "agenda": render_agenda, "postcard": render_postcard, "board": render_board, "plan": render_plan, "badges": render_badges, "opener": render_opener, "tearoff": render_tearoff, "note": render_note}[spec["layout"]](spec, out, tmp)
    return previews(out)


if __name__ == "__main__":
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    print(render(spec, sys.argv[2]))
