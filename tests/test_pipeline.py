"""Cross-vendor integration tests: three independently built nodes, one world.

Run from anywhere after building the nodes (`cargo build --release` in each):

    python framework-sdk/tests/test_pipeline.py

Node repos are looked up next to framework-sdk unless FW_NODES_DIR is set.
"""

import os
import sys
import time
import unittest
from pathlib import Path

SDK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SDK / "python"))

from fwhost import (  # noqa: E402
    Contacts, Flags, Node, NodeRejected, NodeTrap, Pipeline, World, load_node_dir, tiles_from_ascii,
)

NODES_DIR = Path(os.environ.get("FW_NODES_DIR", SDK.parent))
PIPELINE = ("gravity", "momentum", "collision")
DT = 1 / 60
TS = 20.0

PLAYER = Flags.DYNAMIC | Flags.GRAVITY | Flags.SOLID | Flags.PLAYER
CRATE = Flags.DYNAMIC | Flags.GRAVITY | Flags.SOLID | Flags.PUSHABLE | Flags.FRICTION

ROOM = [
    "################",
    "#..............#",
    "#..............#",
    "#..............#",
    "#..............#",
    "#..............#",
    "#..............#",
    "################",
]
FLOOR_Y = 7 * TS


def build(rows=ROOM):
    world = World()
    w, h, data = tiles_from_ascii(rows)
    world.set_tiles(w, h, TS, data)
    nodes = [load_node_dir(NODES_DIR / f"physics-{name}-node") for name in PIPELINE]
    return world, Pipeline(world, nodes)


def run(pipe, seconds, each_tick=None):
    for _ in range(round(seconds / DT)):
        if each_tick:
            each_tick()
        pipe.step(DT)


class PipelineTests(unittest.TestCase):
    def test_body_falls_and_lands_on_floor(self):
        world, pipe = build()
        player = world.add_body(x=100, y=30, w=14, h=18, mass=1, flags=PLAYER)
        run(pipe, 1.5)
        self.assertAlmostEqual(player.y, FLOOR_Y - 18, places=3)
        self.assertEqual(player.vy, 0)
        self.assertTrue(player.contacts & Contacts.GROUND)

    def test_walls_stop_movement(self):
        world, pipe = build()
        player = world.add_body(x=100, y=FLOOR_Y - 18, w=14, h=18, mass=1, flags=PLAYER)

        def push_left():
            player.vx = -400

        run(pipe, 2, push_left)
        self.assertAlmostEqual(player.x, TS, places=3)  # flush against the left wall
        self.assertTrue(player.contacts & Contacts.LEFT)

    def test_player_pushes_crate_with_momentum_and_crate_stops_at_wall(self):
        world, pipe = build()
        player = world.add_body(x=60, y=FLOOR_Y - 18, w=14, h=18, mass=1, flags=PLAYER)
        crate = world.add_body(x=100, y=FLOOR_Y - 20, w=20, h=20, mass=2, flags=CRATE)
        speed = 180.0

        def walk_right():
            player.vx = speed

        run(pipe, 0.6, walk_right)
        self.assertGreater(crate.x, 100, "crate should have been pushed")
        self.assertLess(crate.vx, speed, "a heavier crate must slow the pusher (momentum exchange)")
        self.assertLessEqual(player.x + player.w, crate.x + 0.01, "player must never overlap the crate")

        run(pipe, 4, walk_right)
        right_wall = 15 * TS
        self.assertAlmostEqual(crate.x + crate.w, right_wall, places=2)
        self.assertAlmostEqual(player.x + player.w, crate.x, places=2)

    def test_crate_slides_to_rest_by_friction(self):
        world, pipe = build()
        crate = world.add_body(x=100, y=FLOOR_Y - 20, w=20, h=20, mass=2, vx=200, flags=CRATE)
        run(pipe, 1.5)
        self.assertEqual(crate.vx, 0)
        self.assertGreater(crate.x, 100)

    def test_player_can_stand_on_crate(self):
        world, pipe = build()
        crate = world.add_body(x=100, y=FLOOR_Y - 20, w=20, h=20, mass=2, flags=CRATE)
        player = world.add_body(x=103, y=20, w=14, h=18, mass=1, flags=PLAYER)
        run(pipe, 1.5)
        self.assertAlmostEqual(player.y + player.h, crate.y, places=3)
        self.assertTrue(player.contacts & Contacts.GROUND)
        self.assertAlmostEqual(crate.x, 100, places=3, msg="standing on a crate must not push it")

    def test_nodes_are_decoupled_disabling_gravity_stops_falling(self):
        world, pipe = build()
        player = world.add_body(x=100, y=30, w=14, h=18, mass=1, flags=PLAYER)
        pipe.disabled.add("gravity")
        run(pipe, 1)
        self.assertAlmostEqual(player.y, 30, places=3)

    def test_params_are_configurable_per_app(self):
        world, pipe = build()
        pipe.nodes[0].set_param("gravity", 0)
        player = world.add_body(x=100, y=30, w=14, h=18, mass=1, flags=PLAYER)
        run(pipe, 1)
        self.assertAlmostEqual(player.y, 30, places=3)

    def test_fast_bodies_do_not_tunnel_through_thin_floors(self):
        rows = ["#" * 16] + ["#" + "." * 14 + "#"] * 20 + ["#" * 16]
        world, pipe = build(rows)
        pipe.nodes[0].set_param("max_fall", 5000)
        pipe.nodes[0].set_param("gravity", 20000)
        player = world.add_body(x=100, y=30, w=14, h=18, mass=1, flags=PLAYER)
        run(pipe, 1)
        self.assertAlmostEqual(player.y + player.h, 21 * TS, places=2)

    def test_real_time_budget_with_full_world(self):
        world, pipe = build()
        for i in range(64):
            world.add_body(x=24 + (i % 13) * 21, y=22 + (i // 13) * 22, w=18, h=18, mass=1,
                           flags=CRATE if i % 2 else PLAYER)
        ticks = 300
        started = time.perf_counter()
        run(pipe, ticks * DT)
        per_tick_ms = (time.perf_counter() - started) / ticks * 1000
        fuel = {n.id: n.fuel_used // n.calls for n in pipe.nodes}
        print(f"\n  64 bodies, 3 nodes: {per_tick_ms:.3f} ms/tick in wasmtime "
              f"(budget at 60 Hz: 16.7 ms); fuel/tick per node: {fuel}")
        self.assertLess(per_tick_ms, 8.0)


class SandboxTests(unittest.TestCase):
    def test_module_with_imports_is_rejected(self):
        import wasmtime
        wasm = wasmtime.wat2wasm('(module (import "env" "fetch" (func)) (memory (export "memory") 1))')
        with self.assertRaises(NodeRejected):
            Node(wasm, "evil")

    def test_infinite_loop_is_stopped_by_fuel(self):
        import wasmtime
        wasm = wasmtime.wat2wasm("""
        (module
          (memory (export "memory") 1)
          (func (export "fw_abi_version") (result i32) i32.const 1)
          (func (export "fw_world_ptr") (result i32) i32.const 0)
          (func (export "fw_world_capacity") (result i32) i32.const 7200)
          (func (export "fw_param_count") (result i32) i32.const 0)
          (func (export "fw_set_param") (param i32 f32) (result i32) i32.const 1)
          (func (export "fw_step") (param f32) (result i32) (loop $l (br $l)) i32.const 0))
        """)
        node = Node(wasm, "spinner", [], fuel_per_call=100_000)
        world = World()
        with self.assertRaises(NodeTrap):
            node.step(world, DT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
