# Framework Node ABI — v2

A node is a WebAssembly module that transforms one **state buffer**. The
framework never interprets that buffer: its layout is a **schema** that the
nodes and apps of one domain agree on (for example `physics2d@1`, published
in its own repo). The framework only guarantees that every node in an app
speaks the same schema, and runs them in the order the app's manifest lists:

```
every tick:
  for node in pipeline:
      copy host state -> node memory
      node.fw_step(dt)
      copy node memory -> host state
```

Nodes never see each other's memory, and they **must not import anything**:
no host functions, no WASI, so no network, no files, no clock. A node is pure
computation over the state; the registry rejects any module with imports
and runs each call on a fuel budget.

## Required exports

| Export              | Signature           | Meaning                                                  |
|---------------------|---------------------|----------------------------------------------------------|
| `memory`            | memory              | Linear memory holding the node's copy of the state       |
| `fw_abi_version`    | `() -> i32`         | Must return `2`                                          |
| `fw_state_ptr`      | `() -> i32`         | Byte offset of the state buffer in `memory`              |
| `fw_state_capacity` | `() -> i32`         | Size of that buffer; must be `>= state_bytes`            |
| `fw_param_count`    | `() -> i32`         | Number of tunable `f32` params (see `node.json`)         |
| `fw_set_param`      | `(i32, f32) -> i32` | Set param *i*; 0 = ok, 1 = out of range                  |
| `fw_step`           | `(f32) -> i32`      | Advance one tick of `dt` seconds; 0 = ok, else error     |

A node must accept an all-zero state (that is how the registry certifies it).

## node.json

Published alongside the binary:

```json
{
  "id": "my-node",
  "name": "My Node",
  "version": "2.0.0",
  "abi": 2,
  "schema": "my-domain@1",
  "state_bytes": 7200,
  "description": "...",
  "price_per_minute_usd": 0.0005,
  "params": [{ "name": "strength", "default": 1 }]
}
```

`params[i]` maps the name an app uses in its manifest to `fw_set_param(i, …)`.

### Composite nodes

A node can also be a combination of other published nodes, sold as one. Its
`node.json` has `"kind": "composite"` and a `pipeline` (the same shape as an
app's) instead of a binary, `abi`, `state_bytes` or `params`:

```json
{
  "id": "my-engine",
  "name": "My Engine",
  "version": "1.0.0",
  "kind": "composite",
  "schema": "my-domain@1",
  "description": "...",
  "price_per_minute_usd": 0.0015,
  "pipeline": [
    { "node": "my-node", "version": "2.0.0", "params": { "strength": 2 } },
    { "node": "other-vendors-node", "version": "1.0.0" }
  ]
}
```

- The gateway checks it like an app: members exist and speak its schema, agree on the state size, number at most 16, and are not composites themselves. A member may appear more than once (e.g. two layers of one kind), at one version.
- Member versions are pinned: a composite keeps running and paying the versions it lists.
- Apps list a composite like any node. A session runs its members in its place, in order, so runtimes see only plain nodes (a second run of the same node gets the id `node#2`); the app's 16-node limit counts members.
- Billing: the app pays the composite's price to its owner, who pays each member's price to its vendor, each transfer minus the platform fee. A composite priced below its members' cost after the fee is refused.

## Remote services (rb-call/1)

Code that can't be an import-free wasm node (Python, JavaScript/TypeScript, C#, Java, Shell, or C/C++ built
natively) is a **remote service**: called per request through the gateway, billed per call. The contract is
one process per call:

```
stdin   {"text": "a b c"}                    the inputs, one JSON object
stdout  {"words": 3, "label": "3 words"}     the outputs, one JSON object
exit 0  (anything else is a failed call; stderr says why)
env     RAPIDBOON_GATEWAY, RAPIDBOON_CALL_TOKEN   for calling other services, see below
```

A runner (the platform's, or `rapidboon serve` / `rapidboon dev` locally) answers `POST <endpoint>` with
`{"inputs"}` by running the program once and replying `{"outputs"}`. Published metadata is `service.json`:

```json
{
  "id": "text-report", "name": "Text Report", "version": "0.1.0", "language": "python",
  "inputs": [{ "name": "text", "type": "string" }],
  "outputs": [{ "name": "report", "type": "string" }],
  "price_per_call_usd": 0.0003,
  "calls": [{ "service": "text-stats", "version": "0.1.0" }]
}
```

- Port types: `number`, `integer`, `bool`, `string`, `number[]`, `json`. The gateway checks every call's inputs
  and the service's outputs against them, so neither side gets a shape it didn't declare.
- `calls` pins the services it may call. Each call gets a short-lived call token; with it the service calls
  `POST /api/services/<id>/call` for exactly those, and its owner pays them, like a composite's owner pays its
  members. A price that doesn't cover `calls` after the platform fee is refused. Calls nest at most 4 deep.
- Billing is per successful call: the caller pays the price, minus the platform fee for the owner. An app lists the
  services it calls in its manifest with its own `price_per_call_usd` (it resells them; the same price rule
  applies), and `fw.call(service, inputs)` in the browser runtime calls them through the session.
- The gateway forwards only to endpoints under `SERVICE_URL_PREFIXES` (the platform's runners; empty = remote
  calls off), never follows redirects, and caps answers at 1 MB and 30 s. A published service with no runner
  is listed as not deployed.
- `fwpublish.py service <dir>` publishes `service.json` (with the repo's OIDC token in CI). Hosting the built
  service is the platform's job; generated repos carry a `Dockerfile` for it.

## App manifest

```json
{
  "app_id": "my-app",
  "name": "My App",
  "schema": "my-domain@1",
  "entry": "main.js",
  "files": ["main.js", "helpers.js"],
  "desktop_entry": "desktop.py",
  "price_per_minute_usd": 0.002,
  "pipeline": [{ "node": "my-node", "version": "2.0.0", "params": { "strength": 2 } }]
}
```

- `entry` is an ES module for the browser runtime; its default export receives `fw`.
- `desktop_entry` (optional) is a Python module for the desktop runtime; its `run(fw)` owns the loop.
- `files` are served next to each other, so the entry can `import "./helpers.js"`.

## Versioning

A published `(node id, version)` is immutable. Re-publishing identical bytes
is a no-op; different bytes under the same version are rejected.
