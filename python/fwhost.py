"""Python host for Framework Node ABI v2, built on wasmtime.

Domain-agnostic: it moves an opaque state buffer through a pipeline of nodes.
What the bytes mean is up to the schema the nodes implement. Every call runs
on a fuel budget, so a buggy or hostile node that loops forever traps instead
of hanging the host; the fuel each node burns is reported, which is what a
trusted (server-side) host can bill on.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import wasmtime

ABI_VERSION = 2
REQUIRED_EXPORTS = (
    "memory", "fw_abi_version", "fw_state_ptr", "fw_state_capacity",
    "fw_param_count", "fw_set_param", "fw_step",
)
DEFAULT_FUEL_PER_CALL = 5_000_000
MAX_MEMORY_BYTES = 16 << 20


class NodeRejected(Exception):
    """The module does not satisfy the ABI (imports, exports, version...)."""


class NodeTrap(Exception):
    """The node failed at runtime (trap, fuel exhausted, error code)."""


class Node:
    """One instantiated node with its own sandboxed store and memory."""

    def __init__(self, wasm: bytes, node_id: str, state_bytes: int, params: list[dict] | None = None,
                 fuel_per_call: int = DEFAULT_FUEL_PER_CALL):
        self.id = node_id
        self.state_bytes = state_bytes
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
            raise NodeRejected(f"{node_id}: ABI version mismatch (host speaks {ABI_VERSION})")
        if ex["fw_state_capacity"](self.store) < state_bytes:
            raise NodeRejected(f"{node_id}: state buffer smaller than {state_bytes} bytes")
        self.param_count = ex["fw_param_count"](self.store)
        if params is not None and self.param_count != len(params):
            raise NodeRejected(
                f"{node_id}: exports {self.param_count} params but node.json declares {len(params)}")
        self.ptr = ex["fw_state_ptr"](self.store)
        if self.ptr + state_bytes > self._memory.data_len(self.store):
            raise NodeRejected(f"{node_id}: state pointer outside memory")

    def set_param(self, name_or_index, value: float):
        index = (self.param_names.index(name_or_index)
                 if isinstance(name_or_index, str) else int(name_or_index))
        self.store.set_fuel(self.fuel_per_call)
        if self._set_param(self.store, index, float(value)) != 0:
            raise ValueError(f"{self.id}: no param {name_or_index!r}")

    def step(self, state: bytearray, dt: float):
        if len(state) != self.state_bytes:
            raise ValueError(f"{self.id}: expected {self.state_bytes} state bytes, got {len(state)}")
        self._memory.write(self.store, bytes(state), self.ptr)
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
        state[:] = self._memory.read(self.store, self.ptr, self.ptr + self.state_bytes)


class Pipeline:
    """Runs nodes in manifest order on one shared state buffer."""

    def __init__(self, nodes: list[Node]):
        self.nodes = nodes
        self.disabled: set[str] = set()

    def step(self, state: bytearray, dt: float):
        for node in self.nodes:
            if node.id not in self.disabled:
                node.step(state, dt)


def load_node_dir(path: str | Path, **kwargs) -> Node:
    """Load a node from its repo checkout (node.json + release wasm), params at defaults."""
    path = Path(path)
    meta = json.loads((path / "node.json").read_text())
    wasm_files = sorted((path / "target" / "wasm32-unknown-unknown" / "release").glob("*.wasm"))
    if not wasm_files:
        raise FileNotFoundError(f"{path}: no release wasm; run `cargo build --release` there")
    node = Node(wasm_files[0].read_bytes(), meta["id"], meta["state_bytes"], meta.get("params", []), **kwargs)
    for p in meta.get("params", []):
        node.set_param(p["name"], p["default"])
    return node
