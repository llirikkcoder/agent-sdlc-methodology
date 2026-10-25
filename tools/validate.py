#!/usr/bin/env python3
"""Валидация описания системы: пять слоёв + образец событий.

Два уровня:
  1. Схемная проверка каждого файла по schemas/*.schema.json (JSON Schema 2020-12).
     Используется пакет ``jsonschema``, если установлен; иначе встроенный
     tools/_schema_lite.py. Переменная SOFTOSCOPE_NO_JSONSCHEMA=1 принудительно
     включает встроенный путь.
  2. Семантическая проверка межслойных ссылок и правил гейтов (см. docs/01-softoscope.md,
     раздел «Правила заполнения»).

Использование:
  python3 tools/validate.py examples/<dir> [--no-semantic] [--json]
Код возврата 1 при ошибках, 0 при успехе (предупреждения не ломают).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SCHEMAS = HERE.parent / "schemas"
sys.path.insert(0, str(HERE))

LAYER_FILES = {
    "purpose": "purpose.json",
    "domain": "domain.json",
    "processes": "processes.json",
    "capabilities": "capabilities.json",
    "implementation": "implementation.json",
}
EVENTS_FILE = "events.sample.json"

CORE_EVENT_TYPES = {
    "ProcessInitiated",
    "StepRequested",
    "HumanGateRequested",
    "HumanGateClaimed",
    "HumanGateResolved",
    "AutoApprovedByRule",
    "StepCompleted",
    "StepFailed",
    "ConstraintViolated",
    "EntityTransitioned",
    "ProcessCompleted",
    "ProcessAborted",
    "CapabilityMissing",
    "DriftDetected",
}


@dataclass
class Finding:
    level: str  # ERROR | WARN
    file: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.level:5} {self.file}:{self.path}: {self.message}"


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    def error(self, file: str, path: str, msg: str) -> None:
        self.findings.append(Finding("ERROR", file, path, msg))

    def warn(self, file: str, path: str, msg: str) -> None:
        self.findings.append(Finding("WARN", file, path, msg))

    @property
    def errors(self) -> int:
        return sum(1 for f in self.findings if f.level == "ERROR")


# ---------------------------------------------------------------- схемный уровень


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _schema_errors(instance: Any, schema: dict[str, Any]) -> list[tuple[str, str]]:
    use_lib = os.environ.get("SOFTOSCOPE_NO_JSONSCHEMA") != "1"
    if use_lib:
        try:
            import jsonschema  # type: ignore

            validator = jsonschema.Draft202012Validator(schema)
            out = []
            for err in sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path)):
                path = "$" + "".join(
                    f"[{p}]" if isinstance(p, int) else f".{p}" for p in err.absolute_path
                )
                out.append((path, err.message))
            return out
        except ImportError:
            pass
    from _schema_lite import validate as lite_validate

    return lite_validate(instance, schema)


def validate_schemas(example: Path, report: Report) -> dict[str, Any]:
    loaded: dict[str, Any] = {}
    for layer, fname in LAYER_FILES.items():
        fpath = example / fname
        if not fpath.exists():
            report.error(fname, "$", "файл слоя отсутствует")
            continue
        try:
            data = _load_json(fpath)
        except json.JSONDecodeError as exc:
            report.error(fname, "$", f"невалидный JSON: {exc}")
            continue
        schema = _load_json(SCHEMAS / f"{layer}.schema.json")
        for path, msg in _schema_errors(data, schema):
            report.error(fname, path, msg)
        loaded[layer] = data

    events_path = example / EVENTS_FILE
    if events_path.exists():
        data = _load_json(events_path)
        schema = _load_json(SCHEMAS / "event.schema.json")
        events = data.get("events") if isinstance(data, dict) else None
        if not isinstance(events, list):
            report.error(EVENTS_FILE, "$", "ожидался объект {\"events\": [...]}")
        else:
            for i, ev in enumerate(events):
                for path, msg in _schema_errors(ev, schema):
                    report.error(EVENTS_FILE, f"$.events[{i}]{path[1:]}", msg)
            loaded["events"] = events
    return loaded


# ------------------------------------------------------------ семантический уровень


def _ids(items: list[dict[str, Any]], key: str) -> list[str]:
    return [it.get(key) for it in items if isinstance(it, dict) and key in it]


def _dupes(values: list[str]) -> set[str]:
    seen, dup = set(), set()
    for v in values:
        if v in seen:
            dup.add(v)
        seen.add(v)
    return dup


def validate_semantics(L: dict[str, Any], report: Report) -> None:  # noqa: N803
    purpose = L.get("purpose")
    domain = L.get("domain")
    processes = L.get("processes")
    caps = L.get("capabilities")
    impl = L.get("implementation")
    if not all([purpose, domain, processes, caps, impl]):
        report.warn("*", "$", "семантика пропущена: не все слои загрузились")
        return

    # system_id совпадает
    sids = {name: L[name].get("system_id") for name in LAYER_FILES}
    if len(set(sids.values())) != 1:
        report.error("*", "$.system_id", f"system_id расходится между слоями: {sids}")

    roles = {r["id"]: r for r in purpose.get("roles", [])}
    human_roles = {rid for rid, r in roles.items() if r.get("kind") == "human"}
    approver_roles = {rid for rid, r in roles.items() if r.get("may_approve")}
    constraints = {c["id"]: c for c in purpose.get("hard_constraints", [])}
    entities = domain.get("entities", {})
    imported: set[str] = set()
    for i, imp in enumerate(domain.get("imports", [])):
        if imp["system_id"] == sids["domain"]:
            report.error("domain.json", f"$.imports[{i}]", "система импортирует саму себя")
        for e in imp.get("entities", []):
            if e in entities:
                report.error("domain.json", f"$.imports[{i}]", f"сущность {e!r} и импортирована, и описана локально")
            imported.add(e)
    known_entities = set(entities) | imported
    cap_map = {c["capability_id"]: c for c in caps.get("capabilities", [])}
    proc_map = {p["process_id"]: p for p in processes.get("processes", [])}
    domain_events = {e["name"] for e in domain.get("domain_events", [])}

    for name, values in (
        ("purpose.json roles", list(roles)),
        ("purpose.json hard_constraints", list(constraints)),
        ("capabilities.json", list(cap_map)),
        ("processes.json", list(proc_map)),
    ):
        for d in _dupes(_ids(purpose.get("roles", []), "id") if "roles" in name else values):
            report.error(name.split()[0], "$", f"повтор идентификатора {d!r}")
    for d in _dupes(_ids(caps.get("capabilities", []), "capability_id")):
        report.error("capabilities.json", "$", f"повтор capability_id {d!r}")
    for d in _dupes(_ids(processes.get("processes", []), "process_id")):
        report.error("processes.json", "$", f"повтор process_id {d!r}")

    # ---- слой 1: ссылки ограничений
    for cid, c in constraints.items():
        if c.get("threshold") and c.get("enforcement") != "gate":
            report.warn("purpose.json", f"$.hard_constraints[{cid}]", "threshold имеет смысл только при enforcement=gate")
        at = c.get("applies_to", {})
        for e in at.get("entities", []):
            if e not in known_entities:
                report.error("purpose.json", f"$.hard_constraints[{cid}]", f"неизвестная сущность {e!r}")
        for k in at.get("capabilities", []):
            if k not in cap_map:
                report.error("purpose.json", f"$.hard_constraints[{cid}]", f"неизвестная возможность {k!r}")
        for p in at.get("processes", []):
            if p not in proc_map:
                report.error("purpose.json", f"$.hard_constraints[{cid}]", f"неизвестный процесс {p!r}")

    # ---- слой 2: состояния и ссылки
    for ename, ent in entities.items():
        attrs = {a["name"]: a for a in ent.get("attributes", [])}
        if ent.get("identity") not in attrs:
            report.error("domain.json", f"$.entities.{ename}.identity", "identity не среди attributes")
        for aname, a in attrs.items():
            if a.get("type") == "ref" and a.get("ref") not in known_entities:
                report.error("domain.json", f"$.entities.{ename}.attributes.{aname}", f"ref на неизвестную сущность {a.get('ref')!r}")
        states = set(ent.get("states", []))
        if states:
            if "initial_state" not in ent:
                report.error("domain.json", f"$.entities.{ename}", "есть states, но нет initial_state")
            elif ent["initial_state"] not in states:
                report.error("domain.json", f"$.entities.{ename}.initial_state", "не входит в states")
            for t in ent.get("terminal_states", []):
                if t not in states:
                    report.error("domain.json", f"$.entities.{ename}.terminal_states", f"{t!r} не входит в states")
            pairs = set()
            for i, tr in enumerate(ent.get("transitions", [])):
                for side in ("from", "to"):
                    if tr[side] not in states:
                        report.error("domain.json", f"$.entities.{ename}.transitions[{i}].{side}", f"{tr[side]!r} не входит в states")
                pairs.add((tr["from"], tr["to"]))
            # обратимость: reversible=true требует обратного перехода
            for i, tr in enumerate(ent.get("transitions", [])):
                if tr.get("reversible") is True and (tr["to"], tr["from"]) not in pairs:
                    report.warn("domain.json", f"$.entities.{ename}.transitions[{i}]", "reversible=true, но обратного перехода нет (ожидается компенсация)")
            reachable = {ent.get("initial_state")}
            changed = True
            while changed:
                changed = False
                for a, b in pairs:
                    if a in reachable and b not in reachable:
                        reachable.add(b)
                        changed = True
            for s in states - reachable:
                report.warn("domain.json", f"$.entities.{ename}.states", f"состояние {s!r} недостижимо из initial_state")
        elif ent.get("transitions"):
            report.error("domain.json", f"$.entities.{ename}", "есть transitions, но нет states")
        for i, rel in enumerate(ent.get("relations", [])):
            if rel["target"] not in known_entities:
                report.error("domain.json", f"$.entities.{ename}.relations[{i}]", f"неизвестная сущность {rel['target']!r}")
    for ev in domain.get("domain_events", []):
        if ev["entity"] not in entities:
            report.error("domain.json", "$.domain_events", f"событие {ev['name']} ссылается на неизвестную сущность {ev['entity']!r}")
        if ev["name"] in CORE_EVENT_TYPES:
            report.error("domain.json", "$.domain_events", f"{ev['name']} совпадает с базовым типом события")

    # ---- слой 4: правила возможностей
    for cid, cap in cap_map.items():
        where = f"$.capabilities[{cid}]"
        if cap["effect"] == "write_irreversible" and not cap["human_gate_required"]:
            report.error("capabilities.json", where, "effect=write_irreversible требует human_gate_required=true")
        if cap["effect"] == "write_compensable" and not cap.get("compensation"):
            report.error("capabilities.json", where, "effect=write_compensable требует compensation")
        elif cap["effect"] == "write_compensable" and not cap.get("compensation", {}).get("capability_id"):
            report.warn("capabilities.json", where, "compensation без capability_id: компенсацию придётся делать человеку, а не системой (ADR-011)")
        if cap.get("system_level") and not cap["human_gate_required"]:
            report.error("capabilities.json", where, "system_level=true требует human_gate_required=true (ADR-011)")
        comp = cap.get("compensation", {}).get("capability_id")
        if comp and comp not in cap_map:
            report.error("capabilities.json", where, f"compensation ссылается на неизвестную возможность {comp!r}")
        for r in cap.get("allowed_roles", []):
            if r not in roles:
                report.error("capabilities.json", where, f"неизвестная роль {r!r}")
        for e in cap.get("touches_entities", []):
            if e not in known_entities:
                report.error("capabilities.json", where, f"неизвестная сущность {e!r}")
        for k in cap.get("constraints", []):
            if k not in constraints:
                report.error("capabilities.json", where, f"неизвестное ограничение {k!r}")
        if cap["effect"] == "read" and cap.get("external_side_effects"):
            report.warn("capabilities.json", where, "effect=read, но external_side_effects=true — проверьте")

    gate_constraints_by_cap: dict[str, list[str]] = {}
    for cid, c in constraints.items():
        if c.get("enforcement") == "gate":
            for k in c.get("applies_to", {}).get("capabilities", []):
                gate_constraints_by_cap.setdefault(k, []).append(cid)

    # ---- слой 3: шаги
    for pid, proc in proc_map.items():
        pwhere = f"$.processes[{pid}]"
        if proc["aggregate_entity"] not in entities:
            hint = " (импортированная сущность агрегатом быть не может)" if proc["aggregate_entity"] in imported else ""
            report.error("processes.json", pwhere, f"aggregate_entity {proc['aggregate_entity']!r} не описана в слое 2{hint}")
        steps = proc.get("steps", [])
        step_ids = [s["step_id"] for s in steps]
        for d in _dupes(step_ids):
            report.error("processes.json", pwhere, f"повтор step_id {d!r}")
        for s in steps:
            swhere = f"{pwhere}.steps[{s['step_id']}]"
            actor = s["actor"]
            cap_id = s.get("capability_id")
            cap = cap_map.get(cap_id) if cap_id else None
            if actor == "human":
                if not s.get("role"):
                    report.error("processes.json", swhere, "actor=human требует role")
                elif s["role"] not in human_roles:
                    report.error("processes.json", swhere, f"роль {s['role']!r} не является человеческой ролью слоя 1")
            else:
                if not cap_id:
                    report.error("processes.json", swhere, f"actor={actor} требует capability_id")
                if s.get("role") and s["role"] not in roles:
                    report.error("processes.json", swhere, f"неизвестная роль {s['role']!r}")
            if cap_id and cap is None:
                report.error("processes.json", swhere, f"capability_id {cap_id!r} не описана в слое 4 (MissingCapability: not_declared)")
            gate = s.get("human_gate", {})
            gate_required = bool(gate.get("required"))
            if cap and actor != "human":
                needs = []
                if cap["effect"] == "write_irreversible":
                    needs.append("effect=write_irreversible")
                if cap.get("human_gate_required"):
                    needs.append("capability.human_gate_required")
                if cap.get("system_level"):
                    needs.append("capability.system_level")
                for k in gate_constraints_by_cap.get(cap_id, []):
                    needs.append(f"ограничение {k} (enforcement=gate)")
                if needs and not gate_required:
                    report.error("processes.json", swhere, "шаг агента/системы требует human_gate.required=true: " + "; ".join(needs))
            if cap and actor == "human" and cap.get("allowed_roles") and s.get("role") not in cap["allowed_roles"]:
                report.error("processes.json", swhere, f"роль {s.get('role')!r} не входит в allowed_roles возможности {cap_id!r}")
            if gate_required:
                if gate.get("condition") and cap and cap["effect"] == "write_irreversible":
                    # ADR-010: условие на необратимом действии допустимо только через ограничение с машиночитаемым порогом
                    c = constraints.get(gate.get("constraint_id") or "")
                    if not c:
                        report.error("processes.json", swhere, "условный гейт на effect=write_irreversible требует constraint_id ограничения с threshold (ADR-010)")
                    elif c.get("enforcement") != "gate" or not c.get("threshold"):
                        report.error("processes.json", swhere, f"условный гейт на необратимом действии: ограничение {c['id']} должно иметь enforcement=gate и threshold (ADR-010)")
                    else:
                        attr = c["threshold"]["attribute"]
                        ename, _, aname = attr.partition(".")
                        if ename not in entities or aname not in {a["name"] for a in entities[ename].get("attributes", [])}:
                            report.error("purpose.json", f"$.hard_constraints[{c['id']}].threshold", f"attribute {attr!r} не найден в слое 2")
                ar = gate.get("approver_role")
                if not ar:
                    report.error("processes.json", swhere, "human_gate.required=true требует approver_role")
                elif ar not in approver_roles:
                    report.error("processes.json", swhere, f"approver_role {ar!r} не имеет may_approve=true в слое 1")
                if gate.get("constraint_id") and gate["constraint_id"] not in constraints:
                    report.error("processes.json", swhere, f"неизвестное ограничение {gate['constraint_id']!r}")
                if gate.get("reason") == "constraint" and not gate.get("constraint_id"):
                    report.warn("processes.json", swhere, "reason=constraint без constraint_id")
            tr = s.get("transition")
            if tr:
                ent = entities.get(tr["entity"])
                if ent is None:
                    hint = " (переходы импортированной сущности делает её система-владелец)" if tr["entity"] in imported else ""
                    report.error("processes.json", swhere, f"transition на неизвестную сущность {tr['entity']!r}{hint}")
                else:
                    states = set(ent.get("states", []))
                    if tr["to"] not in states:
                        report.error("processes.json", swhere, f"transition.to {tr['to']!r} нет среди состояний {tr['entity']}")
                    if "from" in tr:
                        if tr["from"] not in states:
                            report.error("processes.json", swhere, f"transition.from {tr['from']!r} нет среди состояний {tr['entity']}")
                        pairs = {(t["from"], t["to"]) for t in ent.get("transitions", [])}
                        if (tr["from"], tr["to"]) not in pairs:
                            report.error("processes.json", swhere, f"перехода {tr['from']}→{tr['to']} нет в слое 2 для {tr['entity']}")
            for nxt in s.get("next", []):
                if nxt not in step_ids:
                    report.error("processes.json", swhere, f"next ссылается на неизвестный шаг {nxt!r}")
            for ev in s.get("emits", []):
                if ev not in CORE_EVENT_TYPES and ev not in domain_events:
                    report.error("processes.json", swhere, f"emits {ev!r}: нет ни в базовом каталоге, ни в domain_events")

    # ---- слой 5: ссылки на модель
    storage_names = set()
    for i, st in enumerate(impl.get("storage", [])):
        storage_names.add(st["name"])
        for e in st.get("entities", []):
            if e not in known_entities:
                report.error("implementation.json", f"$.storage[{i}]", f"неизвестная сущность {e!r}")
    for i, sink in enumerate(impl.get("event_sinks", [])):
        if sink["storage"] not in storage_names:
            report.error("implementation.json", f"$.event_sinks[{i}]", f"storage {sink['storage']!r} не описан в storage[]")
    for i, b in enumerate(impl.get("bindings", [])):
        if b["capability_id"] not in cap_map:
            report.error("implementation.json", f"$.bindings[{i}]", f"привязка к неизвестной возможности {b['capability_id']!r}")
        loc = b.get("location", {})
        if b["kind"] == "http_route" and "route" not in loc and b["status"] == "implemented":
            report.warn("implementation.json", f"$.bindings[{i}]", "kind=http_route без location.route")
        if b["kind"] == "mcp_tool" and "mcp" not in loc and b["status"] == "implemented":
            report.warn("implementation.json", f"$.bindings[{i}]", "kind=mcp_tool без location.mcp")
        if b["status"] == "implemented" and b.get("evidence", "").lower().startswith(("docs", "manual")):
            report.warn("implementation.json", f"$.bindings[{i}]", "status=implemented, но evidence не 'code'")

    # ---- события
    events = L.get("events")
    if events is None:
        return
    seen_ids: set[str] = set()
    last_seq: dict[str, int] = {}
    for i, ev in enumerate(events):
        where = f"$.events[{i}]"
        if ev["event_id"] in seen_ids:
            report.error(EVENTS_FILE, where, "повтор event_id")
        seen_ids.add(ev["event_id"])
        et = ev["event_type"]
        if et not in CORE_EVENT_TYPES and et not in domain_events:
            report.error(EVENTS_FILE, where, f"event_type {et!r}: нет ни в базовом каталоге, ни в domain_events")
        sc = ev["scope"]
        if sc["system_id"] != sids["purpose"]:
            report.error(EVENTS_FILE, where, "scope.system_id не совпадает с системой")
        proc = proc_map.get(sc.get("process_id")) if sc.get("process_id") else None
        if sc.get("process_id") and proc is None:
            report.error(EVENTS_FILE, where, f"неизвестный process_id {sc['process_id']!r}")
        if sc.get("step_id"):
            if proc is None:
                report.error(EVENTS_FILE, where, "step_id без process_id")
            elif sc["step_id"] not in {s["step_id"] for s in proc["steps"]}:
                report.error(EVENTS_FILE, where, f"шаг {sc['step_id']!r} не принадлежит процессу {sc['process_id']}")
        if sc.get("capability_id") and sc["capability_id"] not in cap_map:
            report.error(EVENTS_FILE, where, f"неизвестная возможность {sc['capability_id']!r}")
        if sc.get("entity") and sc["entity"] not in known_entities:
            report.error(EVENTS_FILE, where, f"неизвестная сущность {sc['entity']!r}")
        if sc.get("constraint_id") and sc["constraint_id"] not in constraints:
            report.error(EVENTS_FILE, where, f"неизвестное ограничение {sc['constraint_id']!r}")
        if ev["actor"].get("role") and ev["actor"]["role"] not in roles:
            report.error(EVENTS_FILE, where, f"неизвестная роль {ev['actor']['role']!r}")
        if ev["actor"]["kind"] == "human" and not ev["actor"].get("id"):
            report.warn(EVENTS_FILE, where, "actor.kind=human без actor.id — аудит не атрибутирован")
        agg = ev["aggregate"]
        if agg["kind"] == "process_instance" and agg.get("type") and agg["type"] not in proc_map:
            report.error(EVENTS_FILE, where, f"aggregate.type {agg['type']!r} не является process_id")
        if agg["kind"] == "entity" and agg.get("type") and agg["type"] not in entities:
            report.error(EVENTS_FILE, where, f"aggregate.type {agg['type']!r} не является сущностью")
        key = f"{agg['kind']}:{agg.get('type')}:{agg['id']}"
        if "sequence" in ev:
            if key in last_seq and ev["sequence"] <= last_seq[key]:
                report.error(EVENTS_FILE, where, f"sequence не возрастает внутри агрегата {key}")
            last_seq[key] = ev["sequence"]
        caus = ev["metadata"].get("causation_id")
        if caus and caus not in seen_ids:
            report.warn(EVENTS_FILE, where, "causation_id не указывает на более раннее событие образца")
        cap_of_ev = cap_map.get(sc.get("capability_id") or "")
        if cap_of_ev and cap_of_ev.get("kind") == "llm_step" and (et == "StepCompleted" or et in domain_events):
            model = ev["metadata"].get("model")
            if not model:
                report.error(EVENTS_FILE, where, f"{et} на llm_step {cap_of_ev['capability_id']} без metadata.model — полный лог обязателен (ADR-015)")
            elif not model.get("prompt_version"):
                report.warn(EVENTS_FILE, where, f"{et} на llm_step без metadata.model.prompt_version (ADR-015)")
        if et == "AutoApprovedByRule":
            if not sc.get("constraint_id") or not sc.get("step_id"):
                report.error(EVENTS_FILE, where, "AutoApprovedByRule требует scope.constraint_id и scope.step_id")
            else:
                c = constraints.get(sc["constraint_id"], {})
                if not c.get("threshold"):
                    report.error(EVENTS_FILE, where, f"AutoApprovedByRule ссылается на {sc['constraint_id']} без threshold")
                for k in ("attribute", "value"):
                    if k not in ev["payload"]:
                        report.error(EVENTS_FILE, where, f"AutoApprovedByRule.payload без {k!r} (параметры автопрохождения обязательны, ADR-010)")
        if et == "HumanGateClaimed" and ev["actor"]["kind"] != "human":
            report.error(EVENTS_FILE, where, "HumanGateClaimed: actor.kind должен быть human")
        if et == "ConstraintViolated" and not sc.get("constraint_id"):
            report.error(EVENTS_FILE, where, "ConstraintViolated без scope.constraint_id")
        if et in ("StepRequested", "StepCompleted", "StepFailed", "HumanGateRequested") and not sc.get("step_id"):
            report.error(EVENTS_FILE, where, f"{et} без scope.step_id")
        if et == "EntityTransitioned":
            if not sc.get("entity"):
                report.error(EVENTS_FILE, where, "EntityTransitioned без scope.entity")
            else:
                ent = entities.get(sc["entity"], {})
                pairs = {(t["from"], t["to"]) for t in ent.get("transitions", [])}
                pl = ev["payload"]
                if ("from" in pl and "to" in pl) and (pl["from"], pl["to"]) not in pairs:
                    report.error(EVENTS_FILE, where, f"переход {pl['from']}→{pl['to']} не описан для {sc['entity']}")
        if et == "HumanGateResolved" and ev["payload"].get("decision") not in ("approved", "rejected"):
            report.error(EVENTS_FILE, where, "HumanGateResolved.payload.decision должен быть approved|rejected")
        # чувствительные атрибуты не попадают в payload открытым текстом
        ent_name = sc.get("entity") or (agg.get("type") if agg["kind"] == "entity" else None)
        if ent_name in entities:
            sensitive = {a["name"] for a in entities[ent_name].get("attributes", []) if a.get("sensitive")}
            for k in ev["payload"]:
                if k in sensitive:
                    report.error(EVENTS_FILE, where, f"payload содержит чувствительный атрибут {k!r} сущности {ent_name}")


# ----------------------------------------------------------------------- main


def run(example: Path, semantic: bool = True) -> Report:
    report = Report()
    loaded = validate_schemas(example, report)
    if semantic and report.errors == 0:
        validate_semantics(loaded, report)
    elif semantic:
        report.warn("*", "$", "семантическая проверка пропущена из-за схемных ошибок")
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("example", type=Path, help="каталог с purpose.json, domain.json, ...")
    ap.add_argument("--no-semantic", action="store_true")
    ap.add_argument("--json", action="store_true", help="вывод в JSON")
    args = ap.parse_args(argv)
    report = run(args.example, semantic=not args.no_semantic)
    if args.json:
        print(json.dumps([f.__dict__ for f in report.findings], ensure_ascii=False, indent=2))
    else:
        for f in report.findings:
            print(f)
        warns = len(report.findings) - report.errors
        print(f"-- {args.example}: ошибок {report.errors}, предупреждений {warns}")
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
