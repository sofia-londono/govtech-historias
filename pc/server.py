"""Receives material from the phone over the tunnel and serves finished stories back."""
import ctypes
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import store

PORT = 8787
ORIGINS = {"https://govtech-historias.vercel.app", "http://127.0.0.1:8788"}
ALLOWED_EXT = {"jpg", "jpeg", "png", "webp", "heic", "heif", "mp4", "mov", "m4v", "3gp"}
MAX_CHUNK = 16 * 1024 * 1024
MAX_FILE = 4 * 1024 * 1024 * 1024
MEDIA_NAME = re.compile(r"^v\d+(_preview)?\.(mp4|jpg)$")
ROUTE = re.compile(r"^/api/jobs/(?P<id>[a-z0-9]{6,16})(/(?P<rest>.*))?$")

secret = store.load_secret()
pin = secret["pin"]
token = hmac.new(secret["key"].encode(), pin.encode(), hashlib.sha256).hexdigest()
fails = []
lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def cors(self):
        origin = self.headers.get("Origin")
        if origin in ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "content-type,x-token")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,OPTIONS")
            self.send_header("Access-Control-Expose-Headers", "Content-Range,Accept-Ranges")

    def reply(self, code, data=None):
        body = json.dumps(data if data is not None else {}, ensure_ascii=False).encode()
        self.send_response(code)
        self.cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def body(self, limit=64 * 1024):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            self.close_connection = True
            raise ValueError("too large")
        return self.rfile.read(n)

    def json_body(self):
        return json.loads(self.body() or b"{}")

    def authed(self, query=None):
        given = self.headers.get("x-token") or (query or {}).get("k", [""])[0]
        return hmac.compare_digest(given, token)

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/api/ping":
            return self.reply(200, {"ok": True})
        if not self.authed(q):
            return self.reply(401, {"error": "pin"})
        if u.path == "/api/state":
            return self.reply(200, {"jobs": store.summaries(), "now": time.time()})
        m = re.match(r"^/media/([a-z0-9]{6,16})/([^/]+)$", u.path)
        if m and MEDIA_NAME.match(m[2]):
            return self.media(store.job_dir(m[1]) / "out" / m[2], "dl" in q)
        m = ROUTE.match(u.path)
        if m and m["rest"] and m["rest"].startswith("files/"):
            job = store.read(m["id"])
            idx = int(m["rest"].split("/")[1])
            if not job or idx >= len(job["files"]):
                return self.reply(404)
            path = store.job_dir(m["id"]) / "in" / job["files"][idx]["stored"]
            return self.reply(200, {"offset": path.stat().st_size if path.exists() else 0})
        self.reply(404)

    def media(self, path, download):
        if not path.is_file():
            return self.reply(404)
        size = path.stat().st_size
        start, end, code = 0, size - 1, 200
        rng = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        if rng and (rng[1] or rng[2]):
            if rng[1]:
                start = int(rng[1])
                end = int(rng[2]) if rng[2] else end
            else:
                start = max(0, size - int(rng[2]))
            end = min(end, size - 1)
            code = 206
        self.send_response(code)
        self.cors()
        self.send_header("Content-Type", "video/mp4" if path.suffix == ".mp4" else "image/jpeg")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "private, max-age=3600")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="govtech-{path.parent.parent.name}-{path.name}"')
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1024 * 1024, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/login":
            return self.login()
        if not self.authed():
            return self.reply(401, {"error": "pin"})
        try:
            data = self.json_body()
        except ValueError:
            return self.reply(400, {"error": "json"})
        if u.path == "/api/jobs":
            files = data.get("files") or []
            if not files or len(files) > 12:
                return self.reply(400, {"error": "files"})
            for f in files:
                ext = str(f.get("name", "")).rsplit(".", 1)[-1].lower()
                if ext not in ALLOWED_EXT or int(f.get("size", 0)) > MAX_FILE:
                    return self.reply(400, {"error": f"archivo no admitido: {str(f.get('name'))[:60]}"})
            return self.reply(200, {"id": store.create(data)})
        m = ROUTE.match(u.path)
        if not m or not store.read(m["id"]):
            return self.reply(404)
        if m["rest"] == "submit":
            job = store.read(m["id"])
            for f in job["files"]:
                p = store.job_dir(m["id"]) / "in" / f["stored"]
                if not p.exists() or p.stat().st_size != f["size"]:
                    return self.reply(409, {"error": "faltan archivos"})
            store.enqueue(m["id"], "nueva")
            return self.reply(200)
        if m["rest"] == "feedback":
            text = str(data.get("text", "")).strip()[:2000]
            if not text:
                return self.reply(400)
            store.add_feedback(m["id"], text)
            return self.reply(200)
        if m["rest"] == "choose":
            text = str(data.get("text", "")).strip()[:500]
            if not text:
                return self.reply(400)
            store.add_feedback(m["id"], f"Arma la historia con este texto, tal cual: {text}")
            return self.reply(200)
        if m["rest"] == "status" and data.get("status") in ("publicada", "lista", "descartada"):
            store.update(m["id"], status=data["status"])
            return self.reply(200)
        self.reply(404)

    def login(self):
        now = time.time()
        with lock:
            fails[:] = [t for t in fails if now - t < 300]
            if len(fails) >= 10:
                return self.reply(429, {"error": "espera unos minutos"})
        try:
            given = str(self.json_body().get("pin", ""))
        except ValueError:
            given = ""
        if hmac.compare_digest(given, pin):
            return self.reply(200, {"token": token})
        with lock:
            fails.append(now)
        self.reply(401, {"error": "PIN incorrecto"})

    def do_PUT(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not self.authed():
            return self.reply(401, {"error": "pin"})
        m = ROUTE.match(u.path)
        job = store.read(m["id"]) if m else None
        if not job or not (m["rest"] or "").startswith("files/"):
            return self.reply(404)
        idx = int(m["rest"].split("/")[1])
        if idx >= len(job["files"]):
            return self.reply(404)
        info = job["files"][idx]
        path = store.job_dir(m["id"]) / "in" / info["stored"]
        offset = int(q.get("offset", ["0"])[0])
        have = path.stat().st_size if path.exists() else 0
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_CHUNK or have + n > info["size"]:
            self.close_connection = True
            return self.reply(413)
        data = self.rfile.read(n)
        if offset != have:
            return self.reply(409, {"offset": have})
        if len(data) != n:
            return self.reply(400, {"offset": have})
        with open(path, "ab") as f:
            f.write(data)
        self.reply(200, {"offset": have + n})


def main():
    ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)  # keep the PC awake while serving
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
