"""Цифровой двойник: прогон процессов модели на заглушках, без единого внешнего вызова.

Двойник не заменяет runtime, а запускает его (docs/07) с исполнителями-заглушками и
автоматическим человеком, который действует по сценарию. Так по одной модели видно, как
процесс пройдёт: где остановится Static Guard, где встанет гейт, где сработает порог,
какие переходы сущностей и события получатся, чем кончится отказ шага.

Допущения двойника всегда названы в результате и никогда не попадают в модель:
- `assume_implemented` (по умолчанию да) — возможность без implemented-привязки считается
  реализованной; без этого допущения процесс с дырой честно блокируется, как в жизни;
- `assume_confirmed` (по умолчанию нет) — неподтверждённые ограничения считаются
  подтверждёнными; без него critical-ограничение блокирует, как Static Guard (ADR-017).
Модель для прогона — копия пяти файлов во временном каталоге; исходные файлы не меняются.
"""

from __future__ import annotations

import copy
import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .event_store import EventStore
from .model import Model, load_model, snake
from .runtime import CapabilityError, Runtime

LAYER_NAMES = ("purpose", "domain", "processes", "capabilities", "implementation")
TERMINAL = ("completed", "aborted", "blocked")
ASSUMED_EVIDENCE = "цифровой двойник: допущение, реализация не проверялась"


@dataclass
class Scenario:
    """Что происходит вокруг процесса: входы, данные сущностей, ответы возможностей, решения людей."""

    inputs: dict[str, Any] = field(default_factory=dict)
    entities: dict[str, dict[str, Any]] = field(default_factory=dict)  # "Сущность:id" → атрибуты (для порогов)
    outputs: dict[str, dict[str, Any]] = field(default_factory=dict)  # capability_id → что вернёт заглушка
    decisions: dict[str, str] = field(default_factory=dict)  # step_id → approved | rejected (по умолчанию approved)
    human_outputs: dict[str, dict[str, Any]] = field(default_factory=dict)  # step_id → результат человеческого шага
    fail: list[str] = field(default_factory=list)  # capability_id, которые откажут (CapabilityError)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Scenario:
        unknown = set(d) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"сценарий: неизвестные поля {sorted(unknown)}")
        return cls(**d)


@dataclass
class TwinRun:
    process_id: str
    aggregate_id: str
    status: str
    events: list[dict[str, Any]]
    assumed_bindings: list[str]
    assumed_confirmed: list[str]
    calls: list[str]
    entity_states: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class Twin:
    def __init__(self, layers: dict[str, dict[str, Any]], *, assume_implemented: bool = True, assume_confirmed: bool = False, max_rounds: int = 50):
        self.source = layers
        self.max_rounds = max_rounds
        sim = copy.deepcopy(layers)
        self.assumed_bindings: list[str] = []
        self.assumed_confirmed: list[str] = []
        if assume_implemented:
            have = {b["capability_id"] for b in sim["implementation"].get("bindings", []) if b["status"] == "implemented"}
            for c in sim["capabilities"]["capabilities"]:
                if c["capability_id"] not in have:
                    sim["implementation"].setdefault("bindings", []).append(
                        {"capability_id": c["capability_id"], "status": "implemented", "kind": c["kind"], "evidence": ASSUMED_EVIDENCE}
                    )
                    self.assumed_bindings.append(c["capability_id"])
        if assume_confirmed:
            for c in sim["purpose"].get("hard_constraints", []):
                if c.get("status") == "unconfirmed":
                    c["status"] = "confirmed"
                    self.assumed_confirmed.append(c["id"])
        self._tmp = tempfile.TemporaryDirectory(prefix="softoscope-twin-")
        root = Path(self._tmp.name)
        for name in LAYER_NAMES:
            (root / f"{name}.json").write_text(json.dumps(sim[name], ensure_ascii=False, indent=2), encoding="utf-8")
        self.model: Model = load_model(root)

    @classmethod
    def from_dir(cls, path: Path | str, **kw: Any) -> Twin:
        path = Path(path)
        return cls({n: json.loads((path / f"{n}.json").read_text(encoding="utf-8")) for n in LAYER_NAMES}, **kw)

    def close(self) -> None:
        self._tmp.cleanup()

    def __enter__(self) -> Twin:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ прогон
    def run(self, process_id: str, aggregate_id: str = "twin-1", scenario: Scenario | None = None, store: EventStore | None = None) -> TwinRun:
        """Один процесс. Общий `store` — продолжение жизни тех же сущностей (см. run_chain)."""
        sc = scenario or Scenario()
        store = store if store is not None else EventStore()
        first = len(store.all())
        calls: list[str] = []
        rt = Runtime(
            self.model,
            store,
            {cid: self._stub(cid, cap, sc, calls) for cid, cap in self.model.caps.items()},
            read_entity=lambda entity, eid: sc.entities.get(f"{entity}:{eid}"),
            agent_id="twin_agent",
        )
        iid = rt.start(process_id, aggregate_id, inputs=sc.inputs, actor={"kind": "system", "id": "twin"}, instance_id=f"twin-{process_id}-{aggregate_id}")
        status = rt.instance(iid)["status"]
        for _ in range(self.max_rounds):
            if status in TERMINAL:
                break
            inst = rt.instance(iid)
            waiting = inst.get("waiting") or {}
            sid = waiting.get("step_id")
            if status == "waiting_gate":
                actor = self._human(waiting.get("approver_role"))
                rt.claim_gate(iid, sid, actor)
                status = rt.resolve_gate(iid, sid, sc.decisions.get(sid, "approved"), actor, note="решение по сценарию двойника")
            elif status == "waiting_human":
                step = self.model.step(process_id, sid) or {}
                status = rt.complete_human_step(iid, sid, self._human(step.get("role")), outputs=sc.human_outputs.get(sid, {}))
            else:
                status = rt.run(iid)
        states = {f"{ent}:{eid}": v["state"] for ent, rows in rt.entity_state().states.items() for eid, v in rows.items()}
        return TwinRun(process_id, aggregate_id, status, store.all()[first:], self.assumed_bindings, self.assumed_confirmed, calls, states)

    def run_chain(self, process_ids: list[str], aggregate_id: str, scenarios: dict[str, Scenario] | None = None, store: EventStore | None = None) -> list[TwinRun]:
        """Процессы друг за другом над одним агрегатом в общем журнале: состояния сущностей переходят
        из процесса в процесс. Цепочка останавливается на первом незавершённом процессе.
        Сценарий ищется по ключу `процесс@агрегат`, затем по `процесс`."""
        store = store if store is not None else EventStore()
        scenarios = scenarios or {}
        runs = []
        for pid in process_ids:
            sc = scenarios.get(f"{pid}@{aggregate_id}") or scenarios.get(pid)
            runs.append(self.run(pid, aggregate_id, sc, store=store))
            if runs[-1].status != "completed":
                break
        return runs

    def run_all(self, scenarios: dict[str, Scenario] | None = None, include_planned: bool = False) -> list[TwinRun]:
        out = []
        for p in self.model.processes["processes"]:
            if p.get("status", "active") != "active" and not include_planned:
                continue
            out.append(self.run(p["process_id"], scenario=(scenarios or {}).get(p["process_id"])))
        return out

    # ------------------------------------------------------------- заглушки
    def _human(self, role: str | None) -> dict[str, Any]:
        role = role or next((r for r, v in self.model.roles.items() if v.get("may_approve")), None)
        return {"kind": "human", "id": f"twin_{role or 'human'}", "role": role}

    @staticmethod
    def _stub(cid: str, cap: dict[str, Any], sc: Scenario, calls: list[str]):
        def run(capability: dict[str, Any], inputs: dict[str, Any], ctx: Any) -> dict[str, Any]:
            calls.append(cid)
            if cid in sc.fail:
                raise CapabilityError(f"сценарий двойника: {cid} отказывает")
            out = dict(sc.outputs.get(cid, {}))
            # заглушка «создаёт» затронутые сущности: без id runtime не узнает их текущее состояние
            agg = ctx.instance.get("aggregate_id")
            proc = ctx.model.process(ctx.instance["process_id"]) or {}
            for ent in cap.get("touches_entities", []):
                key = f"{snake(ent)}_id"
                if ent != proc.get("aggregate_entity") and key not in inputs and key not in out:
                    out[key] = f"{snake(ent)}-{agg}"
            if cap["kind"] == "llm_step":
                out.setdefault("_model", {"name": "twin-stub", "prompt_version": "twin"})
            return out

        return run


def format_trail(run: TwinRun) -> list[str]:
    """Журнал прогона строками: номер, событие, шаг, кто, суть."""
    lines = []
    for n, e in enumerate(run.events, 1):
        sc, p = e["scope"], e["payload"]
        extra = ""
        et = e["event_type"]
        if et == "CapabilityMissing":
            extra = f"{p.get('reason')}: {p.get('detail')}"
        elif et == "HumanGateRequested":
            extra = f"ждёт {p.get('approver_role')}; причины {', '.join(p.get('reasons') or [p.get('reason')])}"
        elif et == "HumanGateResolved":
            extra = f"{p.get('decision')} ({e['actor'].get('role')})"
        elif et == "AutoApprovedByRule":
            extra = f"{p.get('attribute')}={p.get('value')} не {p.get('operator')} {p.get('threshold')}"
        elif et == "EntityTransitioned":
            extra = f"{e['aggregate']['type']}:{e['aggregate']['id']} {p.get('from')} → {p.get('to')}"
        elif et in ("ProcessAborted", "StepFailed"):
            extra = str(p.get("detail") or p.get("error") or p.get("reason"))
        elif et == "ConstraintViolated":
            extra = f"{sc.get('constraint_id')}: {p.get('detail')}"
        elif et == "StepCompleted" and p.get("skipped"):
            extra = "пропущен"
        lines.append(f"{n:>3} {et:<22} {sc.get('step_id') or '-':<22} {e['actor']['kind']:<6} {extra}".rstrip())
    return lines
