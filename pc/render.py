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
        for when, what in design["body"]:
            tl = draw_line(when, ft, BLUE)
            page.alpha_composite(tl, (m + 60, y))
            lines = wrap(what, fb, pw - 120 - 190)
            yy = y + 4
            for ln in lines:
                li = draw_line(ln, fb, (235, 235, 235, 255))
                page.alpha_composite(li, (m + 60 + 190, yy))
                yy += li.height
            y = max(y + tl.height, yy) + 26
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
        {"cuts": render_cuts, "stack": render_stack, "grid": render_grid, "split": render_split, "agenda": render_agenda, "postcard": render_postcard, "note": render_note}[spec["layout"]](spec, out, tmp)
    return previews(out)


if __name__ == "__main__":
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    print(render(spec, sys.argv[2]))
