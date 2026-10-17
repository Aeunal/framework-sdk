# Framework World ABI — v1

Every physics/logic node is a WebAssembly module that operates on one shared,
fixed-layout **world** (the "common database" all vendors agree on). The host
(browser runtime, desktop runtime, or the registry's certifier) owns the
authoritative copy and runs the nodes in the order the app's manifest lists
them, once per fixed tick:

```
for node in pipeline:
    copy host world  -> node memory   (header + live bodies; tiles only when changed)
    node.fw_step(dt)
    copy node memory -> host world    (header + live bodies)
```

Nodes never see each other's memory, and they **must not import anything**:
no host functions, no WASI, so no network, no files, no clock. A node is pure
computation over the world; the registry rejects any module with imports.
This lets the platform run code it cannot read, and it lets end users run that
code without having to trust it.

## Required exports

| Export              | Signature           | Meaning                                                    |
|---------------------|---------------------|------------------------------------------------------------|
| `memory`            | memory              | Linear memory holding the node's copy of the world         |
| `fw_abi_version`    | `() -> i32`         | Must return `1`                                            |
| `fw_world_ptr`      | `() -> i32`         | Byte offset of the `World` struct in `memory`              |
| `fw_world_capacity` | `() -> i32`         | `sizeof(World)`; must be `>= 7200`                         |
| `fw_param_count`    | `() -> i32`         | Number of tunable `f32` params (see `node.json`)           |
| `fw_set_param`      | `(i32, f32) -> i32` | Set param *i*; returns 0 on success, 1 if out of range     |
| `fw_step`           | `(f32) -> i32`      | Advance one tick of `dt` seconds; 0 = ok, non-zero = error |

## World layout (little-endian, 4-byte aligned, 7200 bytes)

```
offset  size   field
0       32     Header
32      3072   Body[64]          (48 bytes each)
3104    4096   u8 tiles[4096]    (row-major, tiles_w * tiles_h used; 0 = empty, non-zero = solid)
```

**Header** (8 × 4 bytes)

| # | type | field        | notes                                 |
|---|------|--------------|---------------------------------------|
| 0 | u32  | `magic`      | `0x31575746` ("FWW1")                 |
| 1 | u32  | `body_count` | live bodies, ≤ 64                     |
| 2 | u32  | `tiles_w`    | grid width in tiles                   |
| 3 | u32  | `tiles_h`    | grid height in tiles                  |
| 4 | f32  | `tile_size`  | world units (px) per tile             |
| 5 | u32  | `frame`      | tick counter (host increments)        |
| 6 | u32  | reserved     |                                       |
| 7 | u32  | reserved     |                                       |

**Body** (12 × 4 bytes)

| # | type | field      | notes                                                          |
|---|------|------------|----------------------------------------------------------------|
| 0 | f32  | `x`        | top-left, world units                                          |
| 1 | f32  | `y`        | top-left, +y is down                                           |
| 2 | f32  | `w`        |                                                                |
| 3 | f32  | `h`        |                                                                |
| 4 | f32  | `vx`       | units/s                                                        |
| 5 | f32  | `vy`       | units/s                                                        |
| 6 | f32  | `mass`     |                                                                |
| 7 | f32  | `user`     | free for the app; nodes must not touch it                      |
| 8 | u32  | `flags`    | see below                                                      |
| 9 | u32  | `contacts` | written by the collision stage; read by anyone                 |
|10 | u32  | `tag`      | app-defined kind; nodes must not touch it                      |
|11 | u32  | reserved   |                                                                |

**flags**: `DYNAMIC=1` (moves), `GRAVITY=2` (affected by gravity), `SOLID=4`
(blocks other bodies), `PUSHABLE=8` (can be shoved by other bodies),
`FRICTION=16` (ground friction applies), `PLAYER=32` (informational).

**contacts**: `GROUND=1`, `CEILING=2`, `LEFT=4`, `RIGHT=8`.

Out-of-bounds tiles count as solid, so bodies cannot leave the map.

## node.json

Each node repo carries a `node.json` that is published alongside the binary:

```json
{
  "id": "gravity",
  "name": "Gravity",
  "version": "1.0.0",
  "abi": 1,
  "description": "...",
  "price_per_minute_usd": 0.0005,
  "params": [{ "name": "gravity", "default": 1500, "unit": "px/s^2" }]
}
```

`params[i]` maps the name an app uses in its manifest to `fw_set_param(i, …)`.

## Versioning

A published `(node id, version)` is immutable. Re-publishing identical bytes
is a no-op; different bytes under the same version are rejected. Bump the
version instead. A future ABI v2 gets a new `fw_abi_version` value, and hosts
refuse modules whose version they do not speak.
