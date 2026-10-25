from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from fixture_model import write_model  # noqa: E402
from softoscope.mcp_server import ScopeServer  # noqa: E402
from softoscope.scope_api import TOOL_SPECS, ScopeAPI  # noqa: E402


class ScopeApiCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = write_model(Path(self.tmp.name) / "model")
        self.api = ScopeAPI(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_purpose_and_system_id(self) -> None:
        r = self.api.call("scope_get_purpose", {"system_id": "demo_system"})
        self.assertEqual(r["spec_version"], "0.1.0")
        self.assertEqual(r["purpose"]["system_id"], "demo_system")
        self.assertEqual(self.api.call("scope_get_purpose", {"system_id": "other"})["error"]["code"], "not_found")

    def test_list_processes_counts_gates_by_rule(self) -> None:
        r = self.api.call("scope_list_processes", {"system_id": "demo_system"})
        by_id = {p["process_id"]: p for p in r["processes"]}
        self.assertEqual(by_id["item_flow"]["human_gates_count"], 1)
        self.assertEqual(by_id["maintenance"]["human_gates_count"], 1)
        self.assertEqual(by_id["item_flow"]["steps_count"], 5)

    def test_process_map_enriched(self) -> None:
        r = self.api.call("scope_get_process_map", {"process_id": "item_flow"})
        pub = next(s for s in r["steps"] if s["step_id"] == "publish")
        self.assertEqual(pub["capability"]["effect"], "write_irreversible")
        self.assertEqual(pub["binding"]["status"], "implemented")
        self.assertEqual(pub["gate_rule"]["threshold_constraint"]["id"], "C001")
        self.assertIn("C001", [c["id"] for c in r["constraints"]])
        risky = self.api.call("scope_get_process_map", {"process_id": "risky_flow"})
        self.assertEqual(risky["steps"][0]["binding"]["status"], "planned")
        self.assertEqual(self.api.call("scope_get_process_map", {"process_id": "nope"})["error"]["code"], "not_found")

    def test_entity_and_capability(self) -> None:
        e = self.api.call("scope_get_entity_schema", {"entity_name": "Item"})
        self.assertEqual(e["initial_state"], "draft")
        self.assertTrue(any(a.get("sensitive") for a in e["attributes"]))
        c = self.api.call("scope_get_capability", {"capability_id": "publish_item"})
        self.assertEqual(c["used_in_steps"], [{"process_id": "item_flow", "step_id": "publish"}])
        self.assertEqual(c["constraints"], ["C001"])

    def test_check_drift_strict(self) -> None:
        self.assertEqual(self.api.call("scope_check_drift", {})["errors"], 0)
        strict = self.api.call("scope_check_drift", {"strict": True})
        self.assertEqual(strict["errors"], 2)
        self.assertEqual(sorted(h["reason"] for h in strict["holes"] if h["level"] == "ERROR"), ["not_implemented", "unconfirmed_constraint"])

    def test_validate_plan(self) -> None:
        r = self.api.call(
            "scope_validate_plan",
            {
                "process_id": "item_flow",
                "steps": [
                    {"step_id": "fetch", "inputs_keys": ["item_id"]},
                    {"step_id": "prepare", "capability_id": "once_action"},
                    {"step_id": "publish", "human_gate": False},
                    {"step_id": "ghost"},
                ],
            },
        )
        codes = {(p["step_id"], p["code"]) for p in r["problems"]}
        self.assertIn(("prepare", "missing_capability"), codes)
        self.assertIn(("publish", "gate_required"), codes)
        self.assertIn(("ghost", "unknown_step"), codes)
        self.assertFalse(r["ok"])
        ok = self.api.call("scope_validate_plan", {"process_id": "item_flow", "steps": [{"step_id": "fetch", "inputs_keys": ["item_id"]}, {"step_id": "publish", "human_gate": True}]})
        self.assertTrue(ok["ok"])
        self.assertEqual([p["code"] for p in ok["problems"]], [])

    def test_invalid_model_answers_invalid_model(self) -> None:
        (self.root / "capabilities.json").write_text("{}", encoding="utf-8")
        api = ScopeAPI(self.root)
        self.assertEqual(api.call("scope_get_purpose", {"system_id": "demo_system"})["error"]["code"], "invalid_model")

    def test_unknown_tool_and_bad_args(self) -> None:
        self.assertEqual(self.api.call("scope_write_anything", {})["error"]["code"], "not_found")
        self.assertEqual(self.api.call("scope_get_purpose", {"nope": 1})["error"]["code"], "invalid_arguments")
        self.assertEqual(len(TOOL_SPECS), 7)


class McpServerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = write_model(Path(self.tmp.name) / "model")
        self.server = ScopeServer(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_stdio_roundtrip(self) -> None:
        lines = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "scope_get_entity_schema", "arguments": {"entity_name": "Item"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "scope_get_entity_schema", "arguments": {"entity_name": "Ghost"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "resources/list"},
        ]
        stdin = io.StringIO("\n".join(json.dumps(l) for l in lines) + "\nnot json\n")
        stdout = io.StringIO()
        self.server.serve(stdin, stdout)
        out = [json.loads(l) for l in stdout.getvalue().splitlines()]
        self.assertEqual(out[0]["result"]["serverInfo"]["name"], "softoscope-scope")
        self.assertEqual(len(out[1]["result"]["tools"]), 7)
        self.assertFalse(out[2]["result"]["isError"])
        self.assertEqual(out[2]["result"]["structuredContent"]["entity_name"], "Item")
        self.assertTrue(out[3]["result"]["isError"])
        self.assertEqual(out[4]["error"]["code"], -32601)
        self.assertEqual(out[5]["error"]["code"], -32700)
        self.assertEqual(len(out), 6)  # уведомление без ответа

    def test_reload_on_change(self) -> None:
        self.assertIsNone(self.server.api.load_error)
        import time

        time.sleep(0.01)
        (self.root / "capabilities.json").write_text("{}", encoding="utf-8")
        import os

        os.utime(self.root / "capabilities.json", None)
        r = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "scope_get_purpose", "arguments": {"system_id": "demo_system"}}})
        self.assertTrue(r["result"]["isError"])
        self.assertEqual(r["result"]["structuredContent"]["error"]["code"], "invalid_model")


if __name__ == "__main__":
    unittest.main()
