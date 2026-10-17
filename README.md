# Framework: sell what your code *does*, not your code

A platform where developers publish **compiled capabilities as nodes**, other
developers wire those nodes into bigger nodes and apps, and end users pay for
what they use. The money flows back through every layer automatically.
Nobody, including the platform, ever sees anyone else's source.

This repository is the **SDK**: the contract every node speaks, the tools to
test and publish nodes, and the record of the feasibility study below.

## The feasibility question

Before committing to the full product, we picked the hardest case we could
think of and tried to break it:

> Three *different vendors* each write one piece of a real-time physics engine
> (gravity, momentum, collision). None can see the others' code. An app
> developer combines them into a platformer that must run at **60 FPS**.
> Users pay per minute; the app developer and every vendor get their share.

If this works, batch jobs, APIs and slower workflows are the easy cases.

**It works.** Measured on this prototype:

| | Result |
|---|---|
| Real-time | Physics for 64 bodies across 3 vendors' modules: **0.13 ms/tick** (wasmtime); the demo level runs at **~10 µs/tick** in the browser. A 60 FPS frame has 16,667 µs. |
| Closed source | Vendor CI compiles in the vendor's own private repo and uploads **only the `.wasm` binary**. The platform never has repo access. |
| Safe to run unseen code | Modules may import **nothing** (no network, files, clock): rejected at publish. Each call has a **fuel budget**, so infinite loops are stopped. Each module has private memory, so vendors can't read each other. |
| Commercial | Per-minute billing measured on the server. User → app developer → each vendor, **10% platform fee** on every transfer, ledger balances exactly. |
| Decoupled | Vendors can be switched off live in the running game (press 1–3) and the rest keeps working. Params are set per app in its manifest. |

## How it fits together

```
 VENDORS (private repos)        PLATFORM  (admin-billing-gateway, Render)       PLAYER (browser)
 ┌─────────────────────┐       ┌──────────────────────────────────┐       ┌──────────────────────┐
 │ gravity    lib.rs   │─ CI ─▶│ Registry  certify: no imports,   │       │ runtime.js           │
 │ momentum   lib.rs   │─ CI ─▶│           fuel budget, test run  │       │  each 1/60 s:        │
 │ collision  lib.rs   │─ CI ─▶│           stores .wasm + hash    │─wasm─▶│   world → gravity    │
 └─────────────────────┘ .wasm │                                  │ (paid │         → momentum   │
 ┌─────────────────────┐ only  │ Apps      manifest + game.js     │ session│        → collision  │
 │ platformer (app)    │─ CI ─▶│ Sessions  hand out binaries only │  only)│   game.js: input,    │
 │ app_manifest.json   │       │           to live, paid sessions │       │   rules, drawing     │
 │ game.js             │       │ Billing   server-timed minutes,  │◀─beat─│                      │
 └─────────────────────┘       │           splits + ledger        │ 15 s  └──────────────────────┘
                               └──────────────────────────────────┘
```

**The world is the shared "database".** All nodes agree on one fixed
7,200-byte layout (bodies, tile grid, contact flags), specified in
[ABI.md](ABI.md). The host owns it and, every tick, copies it through each
node in manifest order. Nodes never call each other or the host. They only
transform the world. That is what lets strangers' code compose.

**Writing a node** is this (the whole gravity vendor):

```rust
const DEFAULTS: [f32; 2] = [1500.0, 900.0]; // gravity, max_fall (named in node.json)

fn step(world: &mut World, params: &[f32], dt: f32) {
    for body in world.bodies_mut() {
        if body.has(flags::DYNAMIC | flags::GRAVITY) {
            body.vy = (body.vy + params[0] * dt).min(params[1]);
        }
    }
}
export_node!(step, DEFAULTS);
```

**Using nodes in an app** is a manifest; the app developer never sees the nodes' code:

```json
"pipeline": [
  { "node": "gravity",   "version": "1.0.0", "params": { "gravity": 1500 } },
  { "node": "momentum",  "version": "1.0.0", "params": { "ground_friction": 10 } },
  { "node": "collision", "version": "1.0.0" }
]
```

### The five actors, mapped to repos

| Actor | In this study | Repo |
|---|---|---|
| Admin | Runs the platform, takes the fee | `admin-billing-gateway`, `framework-sdk` |
| Low-level developer | Writes base nodes from scratch | `physics-gravity-node`, `physics-momentum-node`, `physics-collision-node` |
| High-level developer | Composes nodes into bigger nodes | *not built yet; same ABI, a node that runs a sub-pipeline* |
| App developer | Composes nodes into a product | `platformer-game-client` |
| User | Plays and pays | the browser |

## Try it

### Locally (one command)

Needs Rust with the wasm target and Python 3.11+. One-time setup, from the
folder that contains `admin-billing-gateway` and `game-test`:

```powershell
rustup target add wasm32-unknown-unknown
cd admin-billing-gateway
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
cd ..
```

Then, every time:

```powershell
python game-test\framework-sdk\tools\run_local.py
```

It starts the gateway, builds the three nodes, signs up the demo vendors and
app developer, publishes everything, and prints two links:

- **Play**: <http://127.0.0.1:8000/play/simple-platformer>. Click *Start session*.
  Arrows/A-D move, Space jumps, R resets, **1–3 switch a vendor's node off/on**.
  The walls are too high to jump, so push the crates to climb.
- **Admin**: <http://127.0.0.1:8000/admin> (token `dev-admin`): balances, sessions, and every ledger transfer.

What to look for: the right-hand panel shows each vendor's cost per tick
next to the 16,667 µs frame budget. The wallet ticks down while you play.
After *End session*, the admin ledger shows the user paying the app developer
and the app developer paying all three vendors, each minus the platform fee.

### Tests

```powershell
admin-billing-gateway\.venv\Scripts\python game-test\framework-sdk\tests\test_pipeline.py
admin-billing-gateway\.venv\Scripts\python admin-billing-gateway\tests\test_gateway.py
```

The first runs the three real vendor binaries together (falling, landing,
pushing, walls, no tunnelling, toggling, the 64-body real-time budget,
rejecting a module with imports, and stopping an infinite loop). The second
runs the platform flow end to end, including billing amounts and ledger
conservation. Set `TEST_DATABASE_URL` to run it against Postgres.

### On Render

The gateway deploys from `main` of `admin-billing-gateway` (Dockerfile).

1. In the Render dashboard, open the service → **Environment** and set
   `ADMIN_TOKEN` to a long random string. Without it, the service makes up a
   new one on every start (printed in its logs).
2. Recommended: **New → Postgres** (free), then set the service's `DATABASE_URL`
   to its *Internal Database URL*. Free web services wipe their disk when they
   sleep after 15 idle minutes. Without Postgres, you re-run step 3 after each nap.
   (Alternatively, recreate the service from `render.yaml` via **New → Blueprint**,
   which wires both up.)
3. Seed it from your machine:

   ```powershell
   python game-test\framework-sdk\tools\bootstrap_demo.py --gateway https://admin-billing-gateway.onrender.com --admin-token <ADMIN_TOKEN>
   ```

4. Open `https://admin-billing-gateway.onrender.com/play/simple-platformer`.
   The first request after a nap takes 30–60 s.

**Continuous publishing (optional).** Each node/app repo has a workflow that
rebuilds and republishes on every push to `main`. It needs, per repo or at org
level: variables `FW_GATEWAY_URL` and `FW_SDK_REPO` (this repo, which CI must be
able to read), and secret `FW_PUBLISH_TOKEN` (the token `bootstrap_demo.py`
saved to `.fw-dev-tokens.json` for that vendor or app developer). Published
versions are immutable: bump `version` in `node.json` when the code changes.

## Honest limits (what "closed source" buys you)

- **Source is never shared.** That guarantee is solid.
- **The binary has to reach whoever runs it.** A wasm module sent to a browser can
  be saved and decompiled with effort, like any shipped `.dll`. Here it is only
  served to live, paying sessions, stripped of symbols, and pinned to its certified
  hash. Next steps would be per-session watermarking and licence terms. Logic that
  must never leave the vendor has to run **server-side**: same ABI, same binary,
  run by `python/fwhost.py` on the platform. That's fine for batch work and too slow for 60 Hz physics.
- **The client can't be trusted to report usage.** So billing uses server-measured
  session time (heartbeats, capped, sessions expire after 2 minutes of silence).
  Instruction-level ("fuel") billing is only sound where the platform runs the code
  itself. Certification records fuel per tick as a cost signal.

## What's next

- High-level nodes: a node whose implementation is a pipeline of other nodes, billed through.
- Desktop/executable target: same binaries via `fwhost.py` (the old `admin-cloud-compiler` path is not ported yet).
- The node-graph editor that writes manifests, GitHub sign-in instead of tokens, real payments.

## Repository layout

| Path | What |
|---|---|
| `ABI.md` | The world layout and the exports every node must provide |
| `rust/fw_abi.rs` | Copy into a Rust node as `src/fw_abi.rs`; gives `World`, flags, and `export_node!` |
| `python/fwhost.py` | Host for headless runs, tests, and server-side execution (wasmtime, fuel budgets) |
| `tests/test_pipeline.py` | Cross-vendor integration tests |
| `tools/fwpublish.py` | Publish a node or app (used by CI; standard library only) |
| `tools/bootstrap_demo.py` | Create demo accounts and publish everything to a gateway |
| `tools/run_local.py` | Start a local gateway and bootstrap it in one go |
