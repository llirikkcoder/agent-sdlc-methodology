"""Тесты объектного описания (ADR-021): круг JSON → Python → JSON, наследование, порог, двойник, карта."""

from __future__ import annotations

import json
import runpy
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import fixture_model  # noqa: E402
from softoscope.decompile import decompile  # noqa: E402
from softoscope.model import load_model  # noqa: E402
from softoscope.oop import (  # noqa: E402
    LAYERS,
    Attr,
    Capability,
    DescriptionError,
    Entity,
    Gate,
    Process,
    Role,
    State,
    Step,
    System,
    Trigger,
    compile_namespace,
)
from softoscope.process_map import group_diagram, render  # noqa: E402
from softoscope.twin import Scenario, Twin  # noqa: E402

EXAMPLES = sorted(p for p in (ROOT / "examples").iterdir() if (p / "purpose.json").exists())


def _read(d: Path) -> dict:
    return {n: json.loads((d / f"{n}.json").read_text(encoding="utf-8")) for n in LAYERS}


def _fixture_layers() -> dict:
    return {
        "purpose": fixture_model.PURPOSE,
        "domain": fixture_model.DOMAIN,
        "processes": fixture_model.PROCESSES,
        "capabilities": fixture_model.CAPABILITIES,
        "implementation": fixture_model.IMPLEMENTATION,
    }


def _roundtrip(layers: dict) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "desc.py"
        f.write_text(decompile(layers), encoding="utf-8")
        return compile_namespace(runpy.run_path(str(f)))


class RoundTrip(unittest.TestCase):
    def test_fixture_roundtrip_is_lossless(self) -> None:
        layers = _fixture_layers()
        self.assertEqual(_roundtrip(layers), layers)

    def test_every_example_roundtrip_is_lossless(self) -> None:
        self.assertGreaterEqual(len(EXAMPLES), 4)
        for ex in EXAMPLES:
            with self.subTest(example=ex.name):
                layers = _read(ex)
                self.assertEqual(_roundtrip(layers), layers)

    def test_example_description_matches_committed_layers(self) -> None:
        ex = ROOT / "examples" / "equipment-rental"
        self.assertEqual(compile_namespace(runpy.run_path(str(ex / "model.py"))), _read(ex))


def _mini(**extra) -> dict:
    """Минимальное описание в пространстве имён: система, роль, сущность, возможность, процесс."""

    class Base(Entity, abstract=True):
        """База."""

        id = Attr(str, required=True)
        amount = Attr(float)

    class Item(Base):
        """Элемент."""

        title = Attr(str)
        draft = State(initial=True)
        done = State(terminal=True)
        transitions = [draft.to(done, actor="agent")]

    bot = Role.agent("Агент.")
    boss = Role.human("Утверждает.", may_approve=True)
    finish = Capability("Завершить", kind="function", effect="write_reversible", touches=[Item])
    check = Capability("Проверить", kind="function", effect="read", touches=[Item])

    class Flow(Process):
        """Базовый путь."""

        name = "Путь"
        trigger = Trigger.api_call("Вызов.")
        aggregate = Item
        look = Step.agent("Посмотреть", role=bot, capability=check)
        end = Step.agent("Завершить", role=bot, capability=finish, transition=Item.draft.to(Item.done))

    class StrictFlow(Flow):
        """Строгий путь."""

        name = "Строгий путь"
        end = Step.agent("Завершить с согласием", role=bot, capability=finish, gate=Gate(approver=boss), transition=Item.done)
        report = Step.agent("Отчитаться", role=bot, capability=check)

    ns = dict(system=System("mini", "0.1.0", "Проверка описания.", as_of="2026-10-27"), Base=Base, Item=Item, bot=bot, boss=boss, finish=finish, check=check, Flow=Flow, StrictFlow=StrictFlow)
    ns.update(extra)
    return ns


class Description(unittest.TestCase):
    def test_names_come_from_variables_classes_and_attributes(self) -> None:
        L = compile_namespace(_mini())
        self.assertEqual([r["id"] for r in L["purpose"]["roles"]], ["bot", "boss"])
        self.assertEqual([c["capability_id"] for c in L["capabilities"]["capabilities"]], ["finish", "check"])
        self.assertEqual([p["process_id"] for p in L["processes"]["processes"]], ["flow", "strict_flow"])
        self.assertEqual(L["implementation"]["repository"], {"name": "mini", "inspected_at": "2026-10-27"})

    def test_abstract_entity_is_not_exported_but_its_attributes_are_inherited(self) -> None:
        ents = compile_namespace(_mini())["domain"]["entities"]
        self.assertEqual(list(ents), ["Item"])
        self.assertEqual([a["name"] for a in ents["Item"]["attributes"]], ["id", "amount", "title"])
        self.assertEqual(ents["Item"]["initial_state"], "draft")
        self.assertEqual(ents["Item"]["terminal_states"], ["done"])

    def test_subprocess_inherits_steps_and_replaces_by_name_in_place(self) -> None:
        procs = {p["process_id"]: p for p in compile_namespace(_mini())["processes"]["processes"]}
        steps = procs["strict_flow"]["steps"]
        self.assertEqual([s["step_id"] for s in steps], ["look", "end", "report"])
        self.assertEqual(steps[1]["name"], "Завершить с согласием")
        self.assertEqual(steps[1]["human_gate"], {"required": True, "approver_role": "boss"})
        self.assertEqual(steps[1]["transition"], {"entity": "Item", "to": "done"})
        self.assertEqual(procs["flow"]["steps"][1]["transition"], {"entity": "Item", "from": "draft", "to": "done"})

    def test_attribute_comparison_gives_threshold_bound_to_subclass(self) -> None:
        ns = _mini()
        self.assertEqual(str(ns["Item"].amount > 100), "Item.amount > 100")
        self.assertEqual((ns["Item"].amount >= 5).per("RUB").as_dict(), {"attribute": "Item.amount", "operator": ">=", "value": 5, "unit": "RUB"})

    def test_irreversible_capability_requires_gate_by_default(self) -> None:
        cap = Capability("Отправить", kind="external_call", effect="write_irreversible", id="send")
        self.assertTrue(cap.compile()["human_gate_required"])

    def test_description_errors_are_named(self) -> None:
        with self.assertRaisesRegex(DescriptionError, "ровно один System"):
            compile_namespace({})
        with self.assertRaisesRegex(DescriptionError, "без имени"):
            compile_namespace(_mini(roles=[Role.human("Без имени.")]))
        ns = _mini()
        ns["system"] = System("mini", "0.1.0", "Без даты.")
        with self.assertRaisesRegex(DescriptionError, "as_of"):
            compile_namespace(ns)


class TwinRuns(unittest.TestCase):
    def setUp(self) -> None:
        self.twin = Twin(_fixture_layers())

    def tearDown(self) -> None:
        self.twin.close()

    def _types(self, run) -> list[str]:
        return [e["event_type"] for e in run.events]

    def test_below_threshold_passes_by_rule(self) -> None:
        run = self.twin.run("item_flow", "it-1", Scenario(entities={"Item:it-1": {"amount": 50}}))
        self.assertEqual(run.status, "completed")
        self.assertIn("AutoApprovedByRule", self._types(run))
        self.assertNotIn("HumanGateRequested", self._types(run))
        self.assertEqual(run.entity_states, {"Item:it-1": "published"})

    def test_above_threshold_waits_for_human_and_rejection_aborts(self) -> None:
        run = self.twin.run("item_flow", "it-2", Scenario(entities={"Item:it-2": {"amount": 500}}, decisions={"publish": "rejected"}))
        self.assertEqual(run.status, "aborted")
        self.assertIn("HumanGateRequested", self._types(run))
        self.assertNotIn("publish_item", run.calls)

    def test_failing_capability_follows_on_failure(self) -> None:
        run = self.twin.run("escalate_flow", "it-3", Scenario(fail=["once_action"]))
        self.assertIn("HumanGateRequested", self._types(run))  # escalate → человек, затем повтор и abort

    def test_as_is_blocks_on_unimplemented_binding(self) -> None:
        with Twin(_fixture_layers(), assume_implemented=False) as twin:
            run = twin.run("risky_flow")
        self.assertEqual(run.status, "blocked")
        self.assertEqual(run.calls, [])

    def test_unconfirmed_critical_blocks_unless_assumed_confirmed(self) -> None:
        self.assertEqual(self.twin.run("risky_flow").status, "blocked")
        with Twin(_fixture_layers(), assume_confirmed=True) as twin:
            run = twin.run("risky_flow")
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.assumed_confirmed, ["C003"])

    def test_chain_carries_entity_state_between_processes(self) -> None:
        with Twin(_read(ROOT / "examples" / "equipment-rental")) as twin:
            runs = twin.run_chain(
                ["booking_request", "equipment_issue", "rental_return"],
                "b-1",
                {"rental_return": Scenario(inputs={"equipment_id": "eq-1", "deposit_id": "d-1"}, entities={"Deposit:d-1": {"amount": 1000}}), "booking_request": Scenario(inputs={"equipment_id": "eq-1"}), "equipment_issue": Scenario(inputs={"equipment_id": "eq-1"})},
            )
        self.assertEqual([r.status for r in runs], ["completed"] * 3)
        self.assertEqual(runs[-1].entity_states["Booking:b-1"], "closed")
        self.assertEqual(runs[-1].entity_states["Equipment:eq-1"], "available")

    def test_terminal_step_ends_process_in_runtime_and_map(self) -> None:
        import copy

        layers = copy.deepcopy(_fixture_layers())
        flow = next(p for p in layers["processes"]["processes"] if p["process_id"] == "item_flow")
        flow["steps"][1]["terminal"] = True  # classify: дальше процесс не идёт
        with Twin(layers) as twin:
            run = twin.run("item_flow", "it-9")
            text = render(twin.model, [], "item_flow")
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.calls, ["read_item", "classify_item"])
        self.assertIn("s_classify --> done", text)
        self.assertNotIn("s_classify --> s_prepare", text)

    def test_twin_never_touches_source_layers(self) -> None:
        layers = _fixture_layers()
        before = json.dumps(layers, sort_keys=True)
        with Twin(layers) as twin:
            twin.run_all()
        self.assertEqual(json.dumps(layers, sort_keys=True), before)


class ProcessMap(unittest.TestCase):
    def test_map_has_every_process_and_stateful_entity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            model = load_model(fixture_model.write_model(Path(tmp) / "m"))
            text = render(model, [{"level": "ERROR", "reason": "not_implemented", "process_id": "risky_flow", "step_id": "do_risky", "capability_id": "risky_action", "detail": ""}])
        self.assertEqual(text.count("flowchart TD"), len(fixture_model.PROCESSES["processes"]))
        self.assertIn("stateDiagram-v2", text)
        self.assertIn("g_publish{", text)  # гейт перед необратимым шагом
        self.assertIn("class s_do_risky hole", text)
        self.assertIn("⇒ ItemPublished", text)  # событие шага на схеме

    def test_group_diagram_keeps_processes_apart_and_shows_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            model = load_model(fixture_model.write_model(Path(tmp) / "m"))
            ids = [p["process_id"] for p in fixture_model.PROCESSES["processes"]][:2]
            text = "\n".join(group_diagram(model, ids, []))
        self.assertEqual(text.count("flowchart TD"), 1)
        self.assertEqual(text.count("subgraph "), 2)
        self.assertIn("p1_start", text)
        self.assertIn("p2_start", text)
        self.assertNotIn("    start", text)


if __name__ == "__main__":
    unittest.main()
