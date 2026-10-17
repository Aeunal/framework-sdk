"""Host and sandbox tests for ABI v2, using tiny hand-written nodes.

    python tests/test_sandbox.py      (needs: pip install wasmtime)
"""

import sys
import unittest
from pathlib import Path

import wasmtime

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from fwhost import Node, NodeRejected, NodeTrap, Pipeline  # noqa: E402

DT = 1 / 60


def node_wat(step_body: str, capacity: int = 16) -> bytes:
    return wasmtime.wat2wasm(f"""
    (module
      (memory (export "memory") 1)
      (global $p (mut f32) (f32.const 1))
      (func (export "fw_abi_version") (result i32) i32.const 2)
      (func (export "fw_state_ptr") (result i32) i32.const 64)
      (func (export "fw_state_capacity") (result i32) i32.const {capacity})
      (func (export "fw_param_count") (result i32) i32.const 1)
      (func (export "fw_set_param") (param i32 f32) (result i32)
        (if (result i32) (i32.eqz (local.get 0))
          (then (global.set $p (local.get 1)) (i32.const 0)) (else (i32.const 1))))
      (func (export "fw_step") (param f32) (result i32) {step_body}))
    """)


# state[0] += param  (u8 add at offset 64)
ADD_PARAM = "(i32.store8 (i32.const 64) (i32.add (i32.load8_u (i32.const 64)) " \
            "(i32.trunc_f32_u (global.get $p)))) i32.const 0"
# state[1] = state[0] * 2
DOUBLE = "(i32.store8 (i32.const 65) (i32.mul (i32.load8_u (i32.const 64)) (i32.const 2))) i32.const 0"


class HostTests(unittest.TestCase):
    def test_pipeline_runs_nodes_in_order_on_shared_state(self):
        adder = Node(node_wat(ADD_PARAM), "adder", 16, [{"name": "amount", "default": 1}])
        adder.set_param("amount", 3)
        doubler = Node(node_wat(DOUBLE), "doubler", 16, [{"name": "unused", "default": 0}])
        state = bytearray(16)
        Pipeline([adder, doubler]).step(state, DT)
        self.assertEqual((state[0], state[1]), (3, 6))

    def test_disabled_node_is_skipped(self):
        adder = Node(node_wat(ADD_PARAM), "adder", 16)
        pipe = Pipeline([adder])
        pipe.disabled.add("adder")
        state = bytearray(16)
        pipe.step(state, DT)
        self.assertEqual(state[0], 0)

    def test_state_larger_than_capacity_is_rejected(self):
        with self.assertRaises(NodeRejected):
            Node(node_wat(ADD_PARAM, capacity=8), "small", 16)

    def test_param_count_must_match_node_json(self):
        with self.assertRaises(NodeRejected):
            Node(node_wat(ADD_PARAM), "adder", 16, [{"name": "a", "default": 0}, {"name": "b", "default": 0}])

    def test_error_code_is_reported(self):
        with self.assertRaises(NodeTrap):
            Node(node_wat("i32.const 7"), "failing", 16).step(bytearray(16), DT)


class SandboxTests(unittest.TestCase):
    def test_module_with_imports_is_rejected(self):
        wasm = wasmtime.wat2wasm('(module (import "env" "fetch" (func)) (memory (export "memory") 1))')
        with self.assertRaises(NodeRejected):
            Node(wasm, "evil", 16)

    def test_infinite_loop_is_stopped_by_fuel(self):
        node = Node(node_wat("(loop $l (br $l)) i32.const 0"), "spinner", 16, fuel_per_call=100_000)
        with self.assertRaisesRegex(NodeTrap, "fuel budget"):
            node.step(bytearray(16), DT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
