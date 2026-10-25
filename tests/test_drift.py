from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tools"))
sys.path.insert(0, str(HERE))

import drift  # noqa: E402
from fixture_model import write_model  # noqa: E402


class UnconfirmedBySeverityCase(unittest.TestCase):
    """ADR-017: в --strict critical блокирует, high остаётся предупреждением."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = write_model(Path(self.tmp.name) / "model")
        purpose_path = self.root / "purpose.json"
        purpose = json.loads(purpose_path.read_text(encoding="utf-8"))
        purpose["hard_constraints"].append({
            "id": "C099",
            "rule": "Неподтверждённое правило уровня high.",
            "severity": "high",
            "enforcement": "post_check",
            "applies_to": {"processes": ["item_flow"]},
            "status": "unconfirmed",
        })
        purpose_path.write_text(json.dumps(purpose, ensure_ascii=False), encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def levels(self, strict: bool) -> dict[str, str]:
        holes = drift.run(self.root, None, strict)
        return {h.detail.split()[0]: h.level for h in holes if h.reason == "unconfirmed_constraint"}

    def test_strict_keeps_high_as_warning(self) -> None:
        self.assertEqual(self.levels(strict=True), {"C003": "ERROR", "C099": "WARN"})

    def test_delivery_mode(self) -> None:
        self.assertEqual(self.levels(strict=False), {"C003": "WARN", "C099": "INFO"})


if __name__ == "__main__":
    unittest.main()


class HolesToTasksCase(unittest.TestCase):
    def test_report_filters_level_and_dedupes(self) -> None:
        import holes_to_tasks as h2t
        report = {"holes": [
            {"level": "WARN", "reason": "not_implemented", "process_id": "p", "step_id": "s", "capability_id": "c", "detail": "status=planned"},
            {"level": "WARN", "reason": "not_implemented", "process_id": "p", "step_id": "s", "capability_id": "c", "detail": "повтор"},
            {"level": "INFO", "reason": "unused", "process_id": None, "step_id": None, "capability_id": "u", "detail": ""},
        ]}
        tasks = [h2t.to_task(h) for h in h2t.from_report(report, "WARN")]
        self.assertEqual({t["key"] for t in tasks}, {"drift:not_implemented:p:s:c"})
        self.assertEqual(tasks[0]["priority"], 2)

    def test_events_take_capability_missing(self) -> None:
        import holes_to_tasks as h2t
        lines = [
            json.dumps({"event_id": "e1", "event_type": "StepCompleted", "scope": {}, "payload": {}}),
            json.dumps({"event_id": "e2", "event_type": "CapabilityMissing",
                        "scope": {"process_id": "p", "step_id": "s", "capability_id": "c"},
                        "payload": {"reason": "not_bound", "suggested_action": "добавить привязку"}}),
        ]
        tasks = [h2t.to_task(h) for h in h2t.from_events(lines)]
        self.assertEqual([t["key"] for t in tasks], ["drift:not_bound:p:s:c"])
        self.assertEqual(tasks[0]["priority"], 1)
        self.assertIn("CapabilityMissing e2", tasks[0]["description"])
