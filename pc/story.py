"""The commands Claude uses to work a job: pending, show, start, render, fail."""
import json
import subprocess
import sys
import time
from pathlib import Path

import imageio_ffmpeg

import render as renderer
import store


def pending():
    for d in sorted(store.JOBS.iterdir(), key=lambda p: p.stat().st_mtime):
        job = store.read(d.name)
        if job and job["status"] in ("en_cola", "corrigiendo", "editando"):
            print(job["status"], job["id"])


def show(job_id):
    """Prints the job and extracts frames to look at."""
    job = store.read(job_id)
    d = store.job_dir(job_id)
    frames = d / "frames"
    frames.mkdir(exist_ok=True)
    for f in job["files"]:
        src = d / "in" / f["stored"]
        f["path"] = str(src)
        stem = Path(f["stored"]).stem
        if src.suffix.lower() in renderer.IMAGE_EXT:
            out = frames / f"{stem}.jpg"
            img = renderer.ImageOps.exif_transpose(renderer.Image.open(src)).convert("RGB")
            img.thumbnail((900, 900))
            img.save(out, quality=82)
            f["frames"] = [str(out)]
        else:
            info = renderer.probe(src)
            f["dur"] = info["dur"]
            f["hdr"] = info["hdr"]
            f["audio"] = info["audio"]
            times = [min(info["dur"] * p, max(info["dur"] - 0.2, 0)) for p in (0.1, 0.5, 0.9)]
            f["frames"] = []
            for i, t in enumerate(times):
                out = frames / f"{stem}_{i}.jpg"
                subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1", "-vf", "scale='min(720,iw)':-2", "-q:v", "4", str(out)], check=True)
                f["frames"].append(str(out))
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(job, ensure_ascii=False, indent=1))


def start(job_id):
    store.update(job_id, status="editando")


def do_render(job_id, spec_path, note=""):
    job = store.read(job_id)
    d = store.job_dir(job_id)
    n = len(job["versions"]) + 1
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    for item in spec["items"]:
        if "file" in item:
            item["src"] = str(d / "in" / job["files"][item.pop("file")]["stored"])
    out = d / "out" / f"v{n}.mp4"
    t0 = time.time()
    try:
        renderer.render(spec, out)
    except Exception as e:
        store.update(job_id, status="error", error=str(e)[:400])
        raise
    (d / f"spec_v{n}.json").write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
    job = store.read(job_id)
    for fb in job["feedback"]:
        fb["done"] = True
    job["versions"].append({"n": n, "note": note, "ts": time.time(), "mb": round(out.stat().st_size / 1e6, 1)})
    job["status"] = "lista"
    job.pop("error", None)
    store.write(job)
    print(f"ok v{n} {time.time() - t0:.0f}s {out}")


def texts(job_id, path):
    """Saves text options (a JSON list of strings) for the phone to copy."""
    options = json.loads(Path(path).read_text(encoding="utf-8"))
    bad = [t for t in options if renderer.EMOJI.search(t)]
    if bad:
        raise ValueError(f"emoji in: {bad}")
    job = store.read(job_id)
    for fb in job["feedback"]:
        fb["done"] = True
    job["options"] = options
    job["status"] = "lista" if job["versions"] else "textos"
    store.write(job)
    print(f"ok {len(options)} options")


def still(job_id, image_path, note=""):
    """Publishes a finished still image (no video) as the next version."""
    job = store.read(job_id)
    d = store.job_dir(job_id)
    n = len(job["versions"]) + 1
    img = renderer.Image.open(image_path).convert("RGB")
    img.save(d / "out" / f"v{n}_full.jpg", quality=95, subsampling=0)
    poster = img.copy()
    poster.thumbnail((720, 1600))
    poster.save(d / "out" / f"v{n}.jpg", quality=88)
    job = store.read(job_id)
    for fb in job["feedback"]:
        fb["done"] = True
    job["versions"].append({"n": n, "note": note, "ts": time.time(), "image": True, "mb": round((d / "out" / f"v{n}_full.jpg").stat().st_size / 1e6, 1)})
    job["status"] = "lista"
    store.write(job)
    print(f"ok v{n} image")


def fail(job_id, message):
    store.update(job_id, status="error", error=message[:400])


if __name__ == "__main__":
    cmd, *args = sys.argv[1:]
    {"pending": pending, "show": show, "start": start, "render": do_render, "texts": texts, "still": still, "fail": fail}[cmd](*args)
