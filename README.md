# Framework SDK

The framework is a marketplace where developers sell what their code **does**,
not the code itself. A developer publishes a compiled capability as a
**node**; others wire nodes into apps; end users pay per minute of use, and
that money flows back to the app developer and every node vendor, minus a
platform fee. Source never leaves its owner's private repository; only
compiled WebAssembly is published.

This repo is everything a node or app developer needs, and it is
**domain-agnostic**: it knows nothing about games, physics, or any other field.

## The model in four sentences

1. A node is a wasm module that transforms one **state buffer** per tick, has no imports (so no network, files, or clock), and runs on a fuel budget.
2. What the bytes mean is a **schema** (`name@version`) that a domain publishes separately, e.g. [`physics2d@1`](https://github.com/FourAges-Game-Development/physics2d-world).
3. An app is a manifest that pins `node@version`s of one schema, sets their params, and ships an entry module for the browser (`entry`) and/or desktop (`desktop_entry`).
4. The platform certifies nodes at publish time, hands binaries only to live paid sessions, and bills on server-measured time.

Full contract: [ABI.md](ABI.md).

## Writing a node (Rust)

Vendor `rust/fw_node.rs` and your schema's helper next to `lib.rs`:

```rust
#![no_std]
#[macro_use] mod fw_node;
mod physics2d;                       // the schema helper, from its own repo
use physics2d::World;

const DEFAULTS: [f32; 1] = [1500.0]; // params, in node.json order

fn step(world: &mut World, params: &[f32], dt: f32) { /* transform the state */ }

export_node!(World, World::ZERO, step, DEFAULTS);
```

`node.json` declares `abi: 2`, the `schema`, `state_bytes`, params, and price.
Build with `cargo build --release --target wasm32-unknown-unknown`.

A **composite node** needs no code: its `node.json` lists other published
nodes (`"kind": "composite"` plus a `pipeline`), and it is sold and billed as
one node. See [ABI.md](ABI.md#composite-nodes).

## Tools

| Path | What |
|---|---|
| `ABI.md` | The node ABI, `node.json`, and the app manifest |
| `rust/fw_node.rs` | `export_node!` for Rust nodes |
| `python/fwhost.py` | Generic wasmtime host: run nodes headless, in tests, or server-side |
| `tools/fwpublish.py` | Publish a node binary or an app's files (stdlib only; used by CI) |
| `.github/workflows/publish-node.yml`, `publish-app.yml` | Reusable workflows: build and publish with the job's GitHub OIDC token, no secrets |
| `tests/test_sandbox.py` | Host and sandbox tests with hand-written nodes |

```powershell
python tests\test_sandbox.py                                        # needs: pip install wasmtime
$env:FW_GATEWAY_URL="..."; $env:FW_PUBLISH_TOKEN="..."; python tools\fwpublish.py node <node-repo>
```

## Publishing from CI

A node or app repo calls a reusable workflow; `rapidboon init` writes this for you
and registers the repo with the gateway, so no secrets or variables are needed:

```yaml
on: { push: { branches: [main] }, workflow_dispatch: {} }
jobs:
  publish:
    uses: Aeunal/framework-sdk/.github/workflows/publish-node.yml@main   # or publish-app.yml
    permissions: { contents: read, id-token: write }
    with: { gateway: https://admin-billing-gateway.onrender.com }
```

The job asks GitHub for an OIDC token whose audience is the gateway URL, and
`fwpublish.py` sends it when `FW_PUBLISH_TOKEN` is unset. Callers outside this
account can only use these workflows while this repo is public.

## Related repos

- `admin-billing-gateway`: the platform (registry, sessions, billing, browser runtime)
- `admin-cloud-compiler`: desktop runtime and packager
- `physics2d-world`: the first schema, plus the feasibility study built on it
