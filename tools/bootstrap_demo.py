#!/usr/bin/env python3
"""Stand up the feasibility demo on a gateway (local or Render) in one go.

Plays every actor in turn: the admin creates developer accounts, three
vendors each build and publish their node, the app developer publishes the
game. Tokens are written to framework-sdk/.fw-dev-tokens.json (gitignored).

    ADMIN_TOKEN=... python bootstrap_demo.py [--gateway http://127.0.0.1:8000] [--skip-build]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

from fwpublish import post, publish

SDK = Path(__file__).resolve().parents[1]
WORKSPACE = SDK.parent
NODE_VENDORS = {
    "gravity": ("gravity-labs", "Gravity Labs"),
    "momentum": ("momentum-works", "Momentum Works"),
    "collision": ("collide-co", "Collide & Co"),
}
APP_DEV = ("kayseri-games", "Kayseri Games")
APP_DIR = WORKSPACE / "platformer-game-client"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gateway", default=os.environ.get("FW_GATEWAY_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()
    admin = os.environ.get("ADMIN_TOKEN") or sys.exit("set ADMIN_TOKEN")
    gateway = args.gateway.rstrip("/")

    with urllib.request.urlopen(f"{gateway}/health", timeout=120) as r:
        print(f"gateway {gateway}: {json.loads(r.read())}")

    def account(account_id: str, name: str, role: str) -> str:
        status, body = post(f"{gateway}/api/admin/accounts", admin, {"id": account_id, "name": name, "role": role})
        if status >= 400:
            sys.exit(f"creating {account_id} failed ({status}): {body}")
        return body["token"]

    tokens = {}
    for node_id, (vendor_id, vendor_name) in NODE_VENDORS.items():
        node_dir = WORKSPACE / f"physics-{node_id}-node"
        if not args.skip_build:
            subprocess.run(["cargo", "build", "--release", "--quiet"], cwd=node_dir, check=True)
        tokens[vendor_id] = account(vendor_id, vendor_name, "vendor")
        result = publish("node", node_dir, gateway, tokens[vendor_id])
        print(f"node {node_id}: {result['status']} (sha256 {result['sha256'][:12]}..."
              + (f", {result['fuel_per_tick']:,} fuel/tick)" if "fuel_per_tick" in result else ")"))

    tokens[APP_DEV[0]] = account(*APP_DEV, "appdev")
    result = publish("app", APP_DIR, gateway, tokens[APP_DEV[0]])
    for warning in result.get("warnings", []):
        print(f"  warning: {warning}")

    (SDK / ".fw-dev-tokens.json").write_text(json.dumps({"gateway": gateway, "tokens": tokens}, indent=2))
    print(f"app published: {gateway}{result['play_url']}")
    print(f"admin ledger:  {gateway}/admin")
    print(f"tokens saved to {SDK / '.fw-dev-tokens.json'}")


if __name__ == "__main__":
    main()
