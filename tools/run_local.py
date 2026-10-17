#!/usr/bin/env python3
"""One command to try the whole framework locally.

Starts the gateway (from the sibling admin-billing-gateway checkout), builds
and publishes the three nodes and the platformer, then keeps serving until
Ctrl+C.

    python run_local.py [--port 8000] [--gateway-dir ../../../admin-billing-gateway]

First time only, set up the gateway's venv:

    cd admin-billing-gateway
    python -m venv .venv
    .venv/Scripts/python -m pip install -r requirements.txt     (.venv/bin/python on macOS/Linux)
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from bootstrap_demo import WORKSPACE, bootstrap

LOCAL_ADMIN_TOKEN = "dev-admin"  # local only; Render generates a real one


def gateway_python(gateway_dir: Path) -> Path:
    for candidate in (gateway_dir / ".venv" / "Scripts" / "python.exe", gateway_dir / ".venv" / "bin" / "python"):
        if candidate.exists():
            return candidate
    sys.exit(f"no venv in {gateway_dir}; see the setup steps at the top of {Path(__file__).name}")


def wait_healthy(url: str, proc: subprocess.Popen, seconds: int = 30):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if proc.poll() is not None:
            sys.exit("gateway exited during startup (is the port already in use?)")
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=2):
                return
        except OSError:
            time.sleep(0.5)
    proc.terminate()
    sys.exit("gateway did not become healthy in time")


def main():
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--gateway-dir", type=Path, default=WORKSPACE.parent / "admin-billing-gateway")
    args = parser.parse_args()

    gateway_dir = args.gateway_dir.resolve()
    url = f"http://127.0.0.1:{args.port}"
    env = {**os.environ, "ADMIN_TOKEN": os.environ.get("ADMIN_TOKEN", LOCAL_ADMIN_TOKEN)}
    proc = subprocess.Popen(
        [str(gateway_python(gateway_dir)), "-m", "uvicorn", "main:app", "--port", str(args.port), "--log-level", "warning"],
        cwd=gateway_dir, env=env)
    try:
        wait_healthy(url, proc)
        bootstrap(url, env["ADMIN_TOKEN"])
        print(f"\nPlay:  {url}/play/simple-platformer")
        print(f"Admin: {url}/admin   (token: {env['ADMIN_TOKEN']})")
        print("Ctrl+C to stop.")
        proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
