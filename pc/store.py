"""Job files shared by the server and the editing command."""
import json
import os
import re
import secrets
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
JOBS = DATA / "jobs"
QUEUE = DATA / "queue.log"
JOBS.mkdir(parents=True, exist_ok=True)


def load_secret():
    path = DATA / "secret.json"
    if not path.exists():
        path.write_text(json.dumps({"pin": f"{secrets.randbelow(10**6):06d}", "key": secrets.token_hex(32)}))
    return json.loads(path.read_text())


def job_dir(job_id):
    return JOBS / job_id


def read(job_id):
    try:
        return json.loads((job_dir(job_id) / "job.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write(job):
    path = job_dir(job["id"]) / "job.json"
    tmp = path.with_suffix(f".{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))
    raise OSError("job.json is locked")


def update(job_id, **fields):
    job = read(job_id)
    job.update(fields)
    write(job)
    return job


def create(data):
    job_id = secrets.token_hex(4)
    d = job_dir(job_id)
    (d / "in").mkdir(parents=True)
    (d / "out").mkdir()
    files = []
    for i, f in enumerate(data["files"]):
        ext = str(f["name"]).rsplit(".", 1)[-1].lower()
        files.append({"name": str(f["name"])[:120], "size": int(f["size"]), "stored": f"{i}.{ext}"})
    write({
        "id": job_id,
        "created": time.time(),
        "status": "subiendo",
        "zona": str(data.get("zona", ""))[:40],
        "desc": str(data.get("desc", ""))[:3000],
        "formato": re.sub(r"[^a-z0-9_]", "", str(data.get("formato", "auto")))[:20],
        "idea": str(data.get("idea", ""))[:2000],
        "files": files,
        "versions": [],
        "feedback": [],
    })
    return job_id


def enqueue(job_id, kind):
    update(job_id, status="en_cola" if kind == "nueva" else "corrigiendo")
    with open(QUEUE, "a", encoding="utf-8") as f:
        f.write(f"{kind} {job_id}\n")


def add_feedback(job_id, text):
    job = read(job_id)
    job["feedback"].append({"text": text, "ts": time.time(), "done": False})
    write(job)
    enqueue(job_id, "correccion")


def summaries():
    out = []
    for d in JOBS.iterdir():
        job = read(d.name)
        if job and job["status"] != "descartada":
            out.append({k: job[k] for k in ("id", "created", "status", "zona", "desc", "formato", "versions", "feedback")} | {"count": len(job["files"]), "error": job.get("error")})
    return sorted(out, key=lambda j: -j["created"])
