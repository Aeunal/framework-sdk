"""Python host for Framework World ABI v1 nodes, built on wasmtime.

Runs the same .wasm binaries the browser runtime runs. Use it for headless
simulation, cross-vendor integration tests, and (later) desktop builds.
Every node call gets a fuel budget, so a buggy or hostile node that loops
forever traps instead of hanging the host. The fuel it burns is reported per
node, which is what a trusted (server-side) host can bill on.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import wasmtime

ABI_VERSION = 1
MAGIC = 0x31575746
HEADER_BYTES = 32
BODY_BYTES = 48
MAX_BODIES = 64
TILES_OFFSET = HEADER_BYTES + BODY_BYTES * MAX_BODIES
MAX_TILES = 4096
WORLD_BYTES = TILES_OFFSET + MAX_TILES

REQUIRED_EXPORTS = (
    "memory", "fw_abi_version", "fw_world_ptr", "fw_world_capacity",
    "fw_param_count", "fw_set_param", "fw_step",
)

DEFAULT_FUEL_PER_CALL = 5_000_000
MAX_MEMORY_BYTES = 16 << 20


class Flags:
    DYNAMIC = 1
    GRAVITY = 2
    SOLID = 4
    PUSHABLE = 8
    FRICTION = 16
    PLAYER = 32


class Contacts:
    GROUND = 1
    CEILING = 2
    LEFT = 4
    RIGHT = 8


class NodeRejected(Exception):
    """The module does not satisfy the ABI (imports, exports, version...)."""


class NodeTrap(Exception):
    """The node failed at runtime (trap, fuel exhausted, error code)."""


# Word index of each field within a 12-word body record.
_FLOAT_FIELDS = {"x": 0, "y": 1, "w": 2, "h": 3, "vx": 4, "vy": 5, "mass": 6, "user": 7}
_UINT_FIELDS = {"flags": 8, "contacts": 9, "tag": 10}


class BodyView:
    """Attribute access to one body record inside a World buffer."""

    __slots__ = ("_f", "_u", "_base")

    def __init__(self, world: "World", index: int):
        object.__setattr__(self, "_f", world._f32)
        object.__setattr__(self, "_u", world._u32)
        object.__setattr__(self, "_base", (HEADER_BYTES + index * BODY_BYTES) // 4)

    def __getattr__(self, name):
        if name in _FLOAT_FIELDS:
            return self._f[self._base + _FLOAT_FIELDS[name]]
        if name in _UINT_FIELDS:
            return self._u[self._base + _UINT_FIELDS[name]]
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if name in _FLOAT_FIELDS:
            self._f[self._base + _FLOAT_FIELDS[name]] = float(value)
        elif name in _UINT_FIELDS:
            self._u[self._base + _UINT_FIELDS[name]] = int(value)
        else:
            raise AttributeError(name)

    def __repr__(self):
        fields = {k: round(getattr(self, k), 2) for k in ("x", "y", "vx", "vy")}
        return f"Body({fields}, contacts={self.contacts})"


class World:
    """Host-owned authoritative world, laid out exactly as ABI.md specifies."""

    def __init__(self):
        self.buf = bytearray(WORLD_BYTES)
        self._f32 = memoryview(self.buf).cast("f")
        self._u32 = memoryview(self.buf).cast("I")
        self._u32[0] = MAGIC
        self.tiles_version = 0

    @property
    def body_count(self) -> int:
        return self._u32[1]

    @property
    def frame(self) -> int:
        return self._u32[5]

    def set_tiles(self, width: int, height: int, tile_size: float, tiles: bytes):
        if width * height > MAX_TILES or len(tiles) != width * height:
            raise ValueError("tile grid too large or wrong size")
        self._u32[2], self._u32[3] = width, height
        self._f32[4] = tile_size
        self.buf[TILES_OFFSET:TILES_OFFSET + MAX_TILES] = bytes(MAX_TILES)
        self.buf[TILES_OFFSET:TILES_OFFSET + len(tiles)] = tiles
        self.tiles_version += 1

    def add_body(self, **fields) -> BodyView:
        index = self.body_count
        if index >= MAX_BODIES:
            raise ValueError("world is full")
        self._u32[1] = index + 1
        body = BodyView(self, index)
        for name, value in fields.items():
            setattr(body, name, value)
        return body

    def body(self, index: int) -> BodyView:
        if not 0 <= index < self.body_count:
            raise IndexError(index)
        return BodyView(self, index)

    def live_bytes(self) -> int:
        return HEADER_BYTES + self.body_count * BODY_BYTES

    def tile_bytes(self) -> int:
        return self._u32[2] * self._u32[3]


class Node:
    """One instantiated node with its own sandboxed store and memory."""

    def __init__(self, wasm: bytes, node_id: str = "node", params: list[dict] | None = None,
                 fuel_per_call: int = DEFAULT_FUEL_PER_CALL):
        self.id = node_id
        self.param_names = [p["name"] for p in (params or [])]
        self.fuel_per_call = fuel_per_call
        self.calls = 0
        self.fuel_used = 0
        self.seconds = 0.0

        config = wasmtime.Config()
        config.consume_fuel = True
        engine = wasmtime.Engine(config)
        try:
            module = wasmtime.Module(engine, wasm)
        except wasmtime.WasmtimeError as e:
            raise NodeRejected(f"{node_id}: not a valid wasm module: {e}") from e
        if module.imports:
            names = ", ".join(f"{i.module}.{i.name}" for i in module.imports)
            raise NodeRejected(f"{node_id}: nodes may not import anything (found {names})")
        exported = {e.name for e in module.exports}
        missing = [name for name in REQUIRED_EXPORTS if name not in exported]
        if missing:
            raise NodeRejected(f"{node_id}: missing exports {missing}")

        self.store = wasmtime.Store(engine)
        self.store.set_limits(memory_size=MAX_MEMORY_BYTES)
        self.store.set_fuel(self.fuel_per_call)
        try:
            instance = wasmtime.Instance(self.store, module, [])
        except wasmtime.WasmtimeError as e:
            raise NodeRejected(f"{node_id}: instantiation failed: {e}") from e
        ex = instance.exports(self.store)
        self._memory = ex["memory"]
        self._step = ex["fw_step"]
        self._set_param = ex["fw_set_param"]

        if ex["fw_abi_version"](self.store) != ABI_VERSION:
            raise NodeRejected(f"{node_id}: ABI version mismatch")
        if ex["fw_world_capacity"](self.store) < WORLD_BYTES:
            raise NodeRejected(f"{node_id}: world buffer too small")
        self.param_count = ex["fw_param_count"](self.store)
        if params is not None and self.param_count != len(params):
            raise NodeRejected(
                f"{node_id}: exports {self.param_count} params but node.json declares {len(params)}")
        self.ptr = ex["fw_world_ptr"](self.store)
        if self.ptr + WORLD_BYTES > self._memory.data_len(self.store):
            raise NodeRejected(f"{node_id}: world pointer outside memory")
        self._tiles_version = -1

    def set_param(self, name_or_index, value: float):
        index = (self.param_names.index(name_or_index)
                 if isinstance(name_or_index, str) else int(name_or_index))
        self.store.set_fuel(self.fuel_per_call)
        if self._set_param(self.store, index, float(value)) != 0:
            raise ValueError(f"{self.id}: no param {name_or_index!r}")

    def step(self, world: World, dt: float):
        if self._tiles_version != world.tiles_version:
            tiles = world.buf[TILES_OFFSET:TILES_OFFSET + world.tile_bytes()]
            self._memory.write(self.store, bytes(tiles), self.ptr + TILES_OFFSET)
            self._tiles_version = world.tiles_version
        n = world.live_bytes()
        self._memory.write(self.store, bytes(world.buf[:n]), self.ptr)

        self.store.set_fuel(self.fuel_per_call)
        started = time.perf_counter()
        try:
            rc = self._step(self.store, dt)
        except wasmtime.Trap as e:
            if self.store.get_fuel() == 0:
                raise NodeTrap(f"{self.id}: exceeded its fuel budget of {self.fuel_per_call:,} per tick "
                               "(too slow for real time, or an infinite loop)") from e
            raise NodeTrap(f"{self.id}: trapped: {str(e).splitlines()[0]}") from e
        self.seconds += time.perf_counter() - started
        self.fuel_used += self.fuel_per_call - self.store.get_fuel()
        self.calls += 1
        if rc != 0:
            raise NodeTrap(f"{self.id}: fw_step returned {rc}")

        # Copy bodies back; the header stays host-owned.
        out = self._memory.read(self.store, self.ptr + HEADER_BYTES, self.ptr + n)
        world.buf[HEADER_BYTES:n] = out


class Pipeline:
    """Runs nodes in manifest order on a shared world."""

    def __init__(self, world: World, nodes: list[Node]):
        self.world = world
        self.nodes = nodes
        self.disabled: set[str] = set()

    def step(self, dt: float):
        self.world._u32[5] = self.world.frame + 1
        for node in self.nodes:
            if node.id not in self.disabled:
                node.step(self.world, dt)


def load_node_dir(path: str | Path, **kwargs) -> Node:
    """Load a node from its repo checkout (node.json + release wasm)."""
    path = Path(path)
    meta = json.loads((path / "node.json").read_text())
    wasm_files = sorted((path / "target" / "wasm32-unknown-unknown" / "release").glob("*.wasm"))
    if not wasm_files:
        raise FileNotFoundError(f"{path}: no release wasm; run `cargo build --release` there")
    node = Node(wasm_files[0].read_bytes(), meta["id"], meta.get("params", []), **kwargs)
    for p in meta.get("params", []):
        node.set_param(p["name"], p["default"])
    return node


def tiles_from_ascii(rows: list[str], solid: str = "#") -> tuple[int, int, bytes]:
    width = max(len(r) for r in rows)
    data = bytearray(width * len(rows))
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            if ch in solid:
                data[y * width + x] = 1
    return width, len(rows), bytes(data)
