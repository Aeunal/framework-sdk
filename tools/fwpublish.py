#!/usr/bin/env python3
"""Publish a node binary or an app bundle to a Framework gateway.

Only build output leaves the repo: for a node, node.json + the compiled .wasm;
for a composite node ("kind": "composite"), node.json alone (a pipeline of
published nodes); for an app, app_manifest.json + its entry JS. Source code is
never uploaded.

    FW_GATEWAY_URL=https://... FW_PUBLISH_TOKEN=fw_... python fwpublish.py node path/to/node-repo
    FW_GATEWAY_URL=https://... FW_PUBLISH_TOKEN=fw_... python fwpublish.py app  path/to/app-repo

In a GitHub Actions job with `permissions: id-token: write`, leave
FW_PUBLISH_TOKEN unset: the job's OIDC token (audience = the gateway URL) is
used instead, and the gateway maps the repository to the node or app that
`rapidboon init` registered for it. No secrets needed.

Standard library only, so CI needs nothing beyond Python.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def post(url: str, token: str | None, payload: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        # Generous timeout: a sleeping Render free instance takes a while to wake.
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, {"detail": body.decode(errors="replace")[:500]}


def node_payload(root: Path) -> tuple[str, dict]:
    meta = json.loads((root / "node.json").read_text())
    path = f"/api/registry/nodes/{meta['id']}/versions/{meta['version']}"
    if meta.get("kind") == "composite":  # nothing to build: the gateway runs its members' published binaries
        return path, {"meta": meta}
    wasm_files = sorted((root / "target" / "wasm32-unknown-unknown" / "release").glob("*.wasm"))
    if len(wasm_files) != 1:
        sys.exit(f"{root}: expected exactly one release .wasm (run `cargo build --release`), found {len(wasm_files)}")
    wasm = wasm_files[0].read_bytes()
    return path, {"meta": meta, "wasm_b64": base64.b64encode(wasm).decode()}


def app_payload(root: Path) -> tuple[str, dict]:
    manifest = json.loads((root / "app_manifest.json").read_text())
    names = manifest.get("files") or [manifest["entry"]]
    files = {name: (root / name).read_text(encoding="utf-8") for name in names}
    return f"/api/apps/{manifest['app_id']}", {"manifest": manifest, "files": files}


def publish(kind: str, root: str | Path, gateway: str, token: str) -> dict:
    root = Path(root)
    path, payload = node_payload(root) if kind == "node" else app_payload(root)
    status, body = post(gateway.rstrip("/") + path, token, payload)
    if status >= 400:
        raise SystemExit(f"publish {kind} {root.name} failed ({status}): {body.get('detail', body)}")
    return body


def oidc_token(audience: str) -> str | None:
    """This GitHub Actions job's OIDC token for `audience`, if the job may request one."""
    url, bearer = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL"), os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not url or not bearer:
        return None
    request = urllib.request.Request(f"{url}&audience={urllib.parse.quote(audience, safe='')}",
                                     headers={"Authorization": f"Bearer {bearer}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())["value"]


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ("node", "app"):
        sys.exit(__doc__)
    gateway = os.environ.get("FW_GATEWAY_URL", "").rstrip("/")
    token = os.environ.get("FW_PUBLISH_TOKEN") or (gateway and oidc_token(gateway))
    if not gateway or not token:
        sys.exit("set FW_GATEWAY_URL, plus FW_PUBLISH_TOKEN unless this runs in GitHub Actions "
                 "with `permissions: id-token: write`")
    result = publish(sys.argv[1], sys.argv[2], gateway, token)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
