"""Starts the server and the tunnel, and publishes the tunnel address for the web app."""
import json
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import store

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"
PORT = 8787
URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def publish(url):
    (WEB / "tunnel.json").write_text(json.dumps({"url": url, "ts": time.time()}))
    npx = shutil.which("npx") or "npx"
    for attempt in range(4):
        r = subprocess.run([npx, "--no-install", "vercel", "deploy", "--prod", "--yes"], cwd=WEB, capture_output=True, text=True, errors="replace")
        if r.returncode == 0:
            print("published", url, flush=True)
            return
        print(f"publish failed (attempt {attempt + 1}): {r.stderr[-300:]}", flush=True)
        time.sleep(10)


current = {"url": None, "proc": None}


def watchdog():
    """The free tunnel drops now and then: check it from the internet every 45 s and, after
    two failed checks in a row, restart it (a new address is published automatically)."""
    fails = 0
    while True:
        time.sleep(45)
        url, proc = current["url"], current["proc"]
        if not url or not proc or proc.poll() is not None:
            continue
        try:
            ok = urllib.request.urlopen(url + "/api/ping", timeout=15).status == 200
        except Exception:
            ok = False
        fails = 0 if ok else fails + 1
        if fails >= 2:
            print("tunnel not answering, restarting", flush=True)
            fails = 0
            current["url"] = None
            proc.kill()


def tunnel():
    while True:
        proc = subprocess.Popen(
            [str(HERE / "bin" / "cloudflared.exe"), "tunnel", "--no-autoupdate", "--protocol", "http2", "--url", f"http://127.0.0.1:{PORT}"],
            stderr=subprocess.PIPE, text=True, errors="replace",
        )
        current["proc"] = proc
        for line in proc.stderr:
            m = URL.search(line)
            if m:
                current["url"] = m[0]
                threading.Thread(target=publish, args=(m[0],), daemon=True).start()
        print("tunnel closed, restarting", flush=True)
        time.sleep(3)


def main():
    server = subprocess.Popen([sys.executable, str(HERE / "server.py")])
    print("PIN", store.load_secret()["pin"], flush=True)
    threading.Thread(target=tunnel, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()
    while True:
        if server.poll() is not None:
            print("server stopped, restarting", flush=True)
            server = subprocess.Popen([sys.executable, str(HERE / "server.py")])
        time.sleep(2)


if __name__ == "__main__":
    main()
