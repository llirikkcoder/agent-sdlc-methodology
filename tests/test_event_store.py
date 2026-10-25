from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from softoscope.event_store import EventStore, InvalidEvent  # noqa: E402


def ev(eid: str, seq: int | None = None, idem: str | None = None, agg_id: str = "p-1") -> dict:
    e = {
        "event_id": f"00000000-0000-4000-8000-{eid:0>12}",
        "event_type": "StepCompleted",
        "occurred_at": "2026-10-25T10:00:00Z",
        "aggregate": {"kind": "process_instance", "type": "flow", "id": agg_id},
        "actor": {"kind": "agent", "id": "bot"},
        "scope": {"system_id": "demo_system", "process_id": "flow", "step_id": "s1"},
        "payload": {},
        "metadata": {"correlation_id": "c", "schema_version": "1.0"},
    }
    if seq is not None:
        e["sequence"] = seq
    if idem:
        e["metadata"]["idempotency_key"] = idem
    return e


class EventStoreCase(unittest.TestCase):
    def test_sequence_assigned_per_aggregate(self) -> None:
        s = EventStore()
        a = s.append(ev("1"))
        b = s.append(ev("2"))
        c = s.append(ev("3", agg_id="p-2"))
        self.assertEqual((a["sequence"], b["sequence"], c["sequence"]), (1, 2, 1))

    def test_duplicate_event_id_and_idempotency_key(self) -> None:
        s = EventStore()
        self.assertIsNotNone(s.append(ev("1", idem="k")))
        self.assertIsNone(s.append(ev("1")))
        self.assertIsNone(s.append(ev("2", idem="k")))
        self.assertIsNotNone(s.append(ev("3", idem="k", agg_id="p-2")))  # ключ действует внутри агрегата
        self.assertEqual(len(s), 2)

    def test_schema_validation(self) -> None:
        s = EventStore()
        bad = ev("1")
        bad["actor"] = {"kind": "robot"}
        with self.assertRaises(InvalidEvent):
            s.append(bad)
        bad2 = ev("2")
        del bad2["metadata"]["correlation_id"]
        with self.assertRaises(InvalidEvent):
            s.append(bad2)
        self.assertEqual(len(s), 0)

    def test_jsonl_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "j.jsonl"
            s = EventStore(p)
            s.append(ev("1"))
            s.append(ev("2"))
            r = EventStore(p)
            self.assertEqual(len(r), 2)
            self.assertIsNone(r.append(ev("1")))
            self.assertEqual(r.last_sequence("process_instance", "flow", "p-1"), 2)
            self.assertEqual(r.append(ev("3"))["sequence"], 3)


if __name__ == "__main__":
    unittest.main()
