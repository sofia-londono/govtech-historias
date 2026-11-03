"""Starts the server and the tunnel, and publishes the tunnel address for the web app."""
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import store

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"
PORT = 8787
URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def publish(url):
    (WEB / "tunnel.json").write_text(json.dumps({"url": url, "ts": time.time()}))
    npx = shutil.which("npx") or "npx"
    r = subprocess.run([npx, "--no-install", "vercel", "deploy", "--prod", "--yes"], cwd=WEB, capture_output=True, text=True, errors="replace")
    print("published" if r.returncode == 0 else f"publish failed: {r.stderr[-400:]}", url, flush=True)


def tunnel():
    while True:
        proc = subprocess.Popen(
            [str(HERE / "bin" / "cloudflared.exe"), "tunnel", "--no-autoupdate", "--protocol", "http2", "--url", f"http://127.0.0.1:{PORT}"],
            stderr=subprocess.PIPE, text=True, errors="replace",
        )
        for line in proc.stderr:
            m = URL.search(line)
            if m:
                threading.Thread(target=publish, args=(m[0],), daemon=True).start()
        print("tunnel closed, restarting", flush=True)
        time.sleep(3)


def main():
    server = subprocess.Popen([sys.executable, str(HERE / "server.py")])
    print("PIN", store.load_secret()["pin"], flush=True)
    threading.Thread(target=tunnel, daemon=True).start()
    while True:
        if server.poll() is not None:
            print("server stopped, restarting", flush=True)
            server = subprocess.Popen([sys.executable, str(HERE / "server.py")])
        time.sleep(2)


if __name__ == "__main__":
    main()
