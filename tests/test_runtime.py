"""Тесты runtime на синтетической модели: гейты, порог, Static Guard, on_failure, проекции."""

from __future__ import annotations

import itertools
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from fixture_model import write_model  # noqa: E402
from softoscope.event_store import EventStore  # noqa: E402
from softoscope.model import load_model  # noqa: E402
from softoscope.projections import rebuild_all  # noqa: E402
from softoscope.runtime import CapabilityError, Runtime, RuntimeError_  # noqa: E402

APPROVER = {"kind": "human", "id": "alice", "role": "approver"}
OPERATOR = {"kind": "human", "id": "bob", "role": "operator"}


def _clock():
    tick = itertools.count()
    return lambda: f"2026-10-25T10:00:{next(tick) % 60:02d}Z"


def _ids():
    n = itertools.count(1)
    return lambda: f"00000000-0000-4000-8000-{next(n):012d}"


class RuntimeCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.model = load_model(write_model(Path(self.tmp.name) / "model"))
        self.store = EventStore()
        self.items = {"item-1": {"id": "item-1", "title": "T", "amount": 50, "owner_email": "x@y"}}
        self.calls: list[str] = []
        self.fail_once = {"prepare_item": 0, "once_action": 0}

        def read_item(cap, inputs, ctx):
            self.calls.append("read_item")
            return {"item": self.items[inputs["item_id"]]}

        def classify_item(cap, inputs, ctx):
            self.calls.append("classify_item")
            return {"category": "regular", "_model": {"name": "test-model", "prompt_version": "v1"}}

        def prepare_item(cap, inputs, ctx):
            self.calls.append("prepare_item")
            if self.fail_once["prepare_item"] > 0:
                self.fail_once["prepare_item"] -= 1
                raise CapabilityError("временная ошибка")
            return {"prepared": True}

        def publish_item(cap, inputs, ctx):
            self.calls.append("publish_item")
            return {"published_url": "https://example.test/item-1", "_idempotency_key": "pub-item-1"}

        def once_action(cap, inputs, ctx):
            self.calls.append("once_action")
            if self.fail_once["once_action"] > 0:
                self.fail_once["once_action"] -= 1
                raise CapabilityError("ошибка")
            return {"done": True}

        self.executors = {
            "read_item": read_item,
            "classify_item": classify_item,
            "prepare_item": prepare_item,
            "publish_item": publish_item,
            "once_action": once_action,
            "migrate_schema": lambda cap, i, c: {"migrated": True},
        }
        self.checkers = {
            "C002": lambda data, ctx: None if data.get("title") else "пустой заголовок",
            "C004": lambda data, ctx: None if data.get("published_url") else "элемент не опубликован",
        }
        self.rt = Runtime(
            self.model,
            self.store,
            self.executors,
            read_entity=lambda entity, id_: self.items.get(id_),
            constraint_checkers=self.checkers,
            clock=_clock(),
            id_factory=_ids(),
            agent_id="bot",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def types(self, instance_id: str | None = None) -> list[str]:
        return [e["event_type"] for e in self.store if instance_id is None or e["aggregate"]["id"] == instance_id or e["aggregate"]["kind"] == "entity"]

    def start_item_flow(self, amount: int) -> str:
        self.items["item-1"]["amount"] = amount
        return self.rt.start("item_flow", "item-1", inputs={"item_id": "item-1", "title": "T"}, actor=OPERATOR, correlation_id="corr-1")

    # ------------------------------------------------------------ сценарии
    def test_below_threshold_auto_approved(self) -> None:
        iid = self.start_item_flow(amount=50)
        self.assertEqual(self.rt.instance(iid)["status"], "waiting_human")
        status = self.rt.complete_human_step(iid, "confirm", OPERATOR)
        self.assertEqual(status, "completed")
        types = self.types(iid)
        self.assertIn("AutoApprovedByRule", types)
        self.assertNotIn("HumanGateRequested", types)
        auto = next(e for e in self.store if e["event_type"] == "AutoApprovedByRule")
        self.assertEqual(auto["scope"]["constraint_id"], "C001")
        self.assertEqual(auto["payload"]["value"], 50)
        self.assertEqual(auto["payload"]["attribute"], "Item.amount")
        self.assertEqual(self.calls.count("publish_item"), 1)
        # чтение без StepRequested, llm_step — с полным логом и моделью
        reqs = [e["scope"]["step_id"] for e in self.store if e["event_type"] == "StepRequested"]
        self.assertNotIn("fetch", reqs)
        self.assertIn("classify", reqs)
        classify_done = next(e for e in self.store if e["event_type"] == "StepCompleted" and e["scope"]["step_id"] == "classify")
        self.assertEqual(classify_done["metadata"]["model"]["name"], "test-model")
        # переходы и доменное событие
        self.assertEqual(self.rt.entity_state().get("Item", "item-1"), "published")
        self.assertIn("ItemPublished", types)
        self.assertEqual(self.types(iid)[-1], "ProcessCompleted")

    def test_above_threshold_requires_human_and_approve(self) -> None:
        iid = self.start_item_flow(amount=500)
        self.rt.complete_human_step(iid, "confirm", OPERATOR)
        inst = self.rt.instance(iid)
        self.assertEqual(inst["status"], "waiting_gate")
        self.assertEqual(inst["waiting"]["step_id"], "publish")
        self.assertEqual(self.calls.count("publish_item"), 0)
        pending = self.rt.pending_gates(role="approver")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["constraint_id"], "C001")
        self.assertIn("age_seconds", pending[0])
        self.rt.claim_gate(iid, "publish", APPROVER)
        self.assertEqual(self.rt.pending_gates()[0]["claimed_by"], "alice")
        status = self.rt.resolve_gate(iid, "publish", "approved", APPROVER)
        self.assertEqual(status, "completed")
        self.assertEqual(self.calls.count("publish_item"), 1)
        self.assertEqual(self.rt.pending_gates(), [])
        types = self.types(iid)
        self.assertEqual(types.index("HumanGateRequested") < types.index("HumanGateClaimed") < types.index("HumanGateResolved") < types.index("ProcessCompleted"), True)
        self.assertNotIn("AutoApprovedByRule", types)

    def test_reject_aborts(self) -> None:
        iid = self.start_item_flow(amount=500)
        self.rt.complete_human_step(iid, "confirm", OPERATOR)
        status = self.rt.resolve_gate(iid, "publish", "rejected", APPROVER)
        self.assertEqual(status, "aborted")
        self.assertEqual(self.calls.count("publish_item"), 0)
        self.assertEqual(self.rt.instance(iid)["outcome"]["reason"], "gate_rejected")

    def test_gate_role_and_human_checks(self) -> None:
        iid = self.start_item_flow(amount=500)
        self.rt.complete_human_step(iid, "confirm", OPERATOR)
        with self.assertRaises(RuntimeError_):
            self.rt.resolve_gate(iid, "publish", "approved", OPERATOR)  # не та роль
        with self.assertRaises(RuntimeError_):
            self.rt.resolve_gate(iid, "publish", "approved", {"kind": "system", "id": "runtime"})
        with self.assertRaises(RuntimeError_):
            self.rt.complete_human_step(iid, "publish", OPERATOR)  # шаг не человеческий
        self.assertEqual(self.rt.instance(iid)["status"], "waiting_gate")

    def test_threshold_unevaluable_falls_back_to_gate(self) -> None:
        self.rt.read_entity = lambda entity, id_: {}
        iid = self.rt.start("item_flow", "item-1", inputs={"item_id": "item-1", "title": "T"}, actor=OPERATOR)
        self.rt.complete_human_step(iid, "confirm", OPERATOR)
        self.assertEqual(self.rt.instance(iid)["status"], "waiting_gate")
        req = next(e for e in self.store if e["event_type"] == "HumanGateRequested")
        self.assertIn("порог не вычислен", req["payload"]["note"])

    def test_static_guard_blocks_planned_and_unconfirmed(self) -> None:
        iid = self.rt.start("risky_flow", "item-1", inputs={})
        inst = self.rt.instance(iid)
        self.assertEqual(inst["status"], "blocked")
        reasons = sorted(m["reason"] for m in inst["missing"])
        self.assertEqual(reasons, ["not_implemented", "unconfirmed_constraint"])
        self.assertEqual(self.types(iid), ["ProcessInitiated", "CapabilityMissing", "CapabilityMissing"])
        self.assertEqual(self.rt.run(iid), "blocked")

    def test_static_guard_no_executor(self) -> None:
        del self.executors["read_item"]
        problems = self.rt.static_guard("item_flow")
        self.assertEqual([p["capability_id"] for p in problems], ["read_item"])
        self.assertEqual(problems[0]["reason"], "not_bound")

    def test_retry_when_idempotent(self) -> None:
        self.fail_once["prepare_item"] = 1
        iid = self.start_item_flow(amount=50)
        self.assertEqual(self.rt.instance(iid)["status"], "waiting_human")
        self.assertEqual(self.calls.count("prepare_item"), 2)
        self.assertEqual(self.types(iid).count("StepFailed"), 1)

    def test_no_retry_when_not_idempotent(self) -> None:
        self.fail_once["once_action"] = 1
        iid = self.rt.start("once_flow", "item-1", inputs={})
        self.assertEqual(self.rt.instance(iid)["status"], "aborted")
        self.assertEqual(self.calls.count("once_action"), 1)
        self.assertIn("idempotency=none", self.rt.instance(iid)["outcome"]["detail"])

    def test_escalate_then_approve_retries(self) -> None:
        self.fail_once["once_action"] = 1
        iid = self.rt.start("escalate_flow", "item-1", inputs={})
        self.assertEqual(self.rt.instance(iid)["status"], "waiting_gate")
        req = next(e for e in self.store if e["event_type"] == "HumanGateRequested")
        self.assertEqual(req["payload"]["reason"], "low_confidence")
        status = self.rt.resolve_gate(iid, "once", "approved", APPROVER)
        self.assertEqual(status, "completed")
        self.assertEqual(self.calls.count("once_action"), 2)

    def test_pre_check_violation(self) -> None:
        iid = self.rt.start("item_flow", "item-1", inputs={"item_id": "item-1", "title": ""})
        self.assertEqual(self.rt.instance(iid)["status"], "aborted")  # prepare: retry → ConstraintViolated снова → лимит
        types = self.types(iid)
        self.assertIn("ConstraintViolated", types)
        self.assertEqual(self.calls.count("prepare_item"), 0)
        viol = next(e for e in self.store if e["event_type"] == "ConstraintViolated")
        self.assertEqual(viol["scope"]["constraint_id"], "C002")

    def test_llm_step_without_model_fails(self) -> None:
        self.executors["classify_item"] = lambda cap, i, c: {"category": "x"}
        iid = self.start_item_flow(amount=50)
        self.assertEqual(self.rt.instance(iid)["status"], "aborted")
        failed = next(e for e in self.store if e["event_type"] == "StepFailed")
        self.assertIn("ADR-015", failed["payload"]["error"])

    def test_system_level_requires_gate(self) -> None:
        iid = self.rt.start("maintenance", "item-1", inputs={})
        self.assertEqual(self.rt.instance(iid)["status"], "waiting_gate")
        req = next(e for e in self.store if e["event_type"] == "HumanGateRequested")
        self.assertIn("system_level", req["payload"]["reasons"])
        self.assertEqual(self.rt.resolve_gate(iid, "migrate", "approved", APPROVER), "completed")

    def test_sensitive_masked_and_idempotency_key(self) -> None:
        iid = self.start_item_flow(amount=50)
        self.rt.complete_human_step(iid, "confirm", OPERATOR)
        fetch_done = next(e for e in self.store if e["event_type"] == "StepCompleted" and e["scope"]["step_id"] == "fetch")
        self.assertEqual(fetch_done["payload"]["outputs"]["item"]["owner_email"], "<redacted>")
        pub = next(e for e in self.store if e["event_type"] == "StepCompleted" and e["scope"]["step_id"] == "publish")
        self.assertEqual(pub["metadata"]["idempotency_key"], "pub-item-1")

    def test_transition_not_allowed(self) -> None:
        # Переход ready→published невозможен, если элемент ещё draft по проекции (симулируем прямым событием).
        iid = self.start_item_flow(amount=50)
        self.rt.complete_human_step(iid, "confirm", OPERATOR)
        self.assertEqual(self.rt.entity_state().get("Item", "item-1"), "published")
        # второй прогон того же элемента: fetch/classify/prepare → переход draft→ready не допустим из published
        iid2 = self.rt.start("item_flow", "item-1", inputs={"item_id": "item-1", "title": "T"})
        self.assertEqual(self.rt.instance(iid2)["status"], "aborted")
        failed = [e for e in self.store if e["event_type"] == "StepFailed" and e["aggregate"]["id"] == iid2]
        self.assertEqual(failed[0]["payload"]["error"], "transition_not_allowed")

    def test_projections_rebuild_from_journal_file(self) -> None:
        path = Path(self.tmp.name) / "journal.jsonl"
        self.rt.store = EventStore(path)
        iid = self.start_item_flow(amount=500)
        self.rt.complete_human_step(iid, "confirm", OPERATOR)
        reloaded = EventStore(path)
        self.assertEqual(len(reloaded), len(self.rt.store))
        proj = rebuild_all(reloaded)
        inst = proj["ProcessInstances"].get(iid)
        self.assertEqual(inst["status"], "waiting_gate")
        self.assertEqual(len(proj["PendingGates"].list()), 1)
        self.assertEqual(proj["EntityState"].get("Item", "item-1"), "ready")
        # time-travel: до подтверждения человеком
        before = [e for e in reloaded.until("2026-10-25T10:00:06Z")]
        self.assertLess(len(before), len(reloaded))
        # продолжение из перезагруженного журнала
        rt2 = Runtime(self.model, reloaded, self.executors, read_entity=lambda e, i: self.items.get(i), clock=_clock(), id_factory=lambda: f"11111111-0000-4000-8000-{len(reloaded):012d}", agent_id="bot")
        self.assertEqual(rt2.resolve_gate(iid, "publish", "approved", APPROVER), "completed")

    def test_sequence_per_aggregate_and_causation(self) -> None:
        iid = self.start_item_flow(amount=50)
        proc_events = [e for e in self.store if e["aggregate"]["kind"] == "process_instance"]
        self.assertEqual([e["sequence"] for e in proc_events], list(range(1, len(proc_events) + 1)))
        self.assertEqual(proc_events[1]["metadata"]["causation_id"], proc_events[0]["event_id"])
        self.assertTrue(all(e["metadata"]["correlation_id"] == "corr-1" for e in self.store))


if __name__ == "__main__":
    unittest.main()
