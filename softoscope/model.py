"""Модель системы: загрузка пяти слоёв и правила ядра, общие для scope_* и runtime.

Правила здесь — те же, что проверяет tools/validate.py; модель загружается только после того,
как валидатор не нашёл ошибок (невалидную модель нельзя ни отдавать агенту, ни исполнять).
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import drift as _drift  # noqa: E402
import validate as _validate  # noqa: E402

GATE_OPERATORS = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


class InvalidModel(Exception):
    """Модель не прошла валидатор; findings — список {level, file, path, message}."""

    def __init__(self, findings: list[dict[str, Any]]):
        super().__init__(f"модель невалидна: ошибок {sum(1 for f in findings if f['level'] == 'ERROR')}")
        self.findings = findings


def snake(entity_name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", entity_name).lower()


@dataclass
class GateRule:
    """Ответ на вопрос «нужен ли человек перед шагом» и почему (docs/01, правило гейта)."""

    required: bool
    reasons: list[str] = field(default_factory=list)
    approver_role: str | None = None
    declared_reason: str | None = None
    constraint_id: str | None = None
    threshold_constraint: dict[str, Any] | None = None  # условный гейт по порогу (ADR-010)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.threshold_constraint:
            d["threshold_constraint"] = {"id": self.threshold_constraint["id"], "threshold": self.threshold_constraint["threshold"]}
        return d


@dataclass
class Model:
    root: Path
    purpose: dict[str, Any]
    domain: dict[str, Any]
    processes: dict[str, Any]
    capabilities: dict[str, Any]
    implementation: dict[str, Any]
    warnings: list[dict[str, Any]] = field(default_factory=list)

    # ----------------------------------------------------------------- индексы
    def __post_init__(self) -> None:
        self.caps: dict[str, dict[str, Any]] = {c["capability_id"]: c for c in self.capabilities["capabilities"]}
        self.procs: dict[str, dict[str, Any]] = {p["process_id"]: p for p in self.processes["processes"]}
        self.constraints: dict[str, dict[str, Any]] = {c["id"]: c for c in self.purpose.get("hard_constraints", [])}
        self.roles: dict[str, dict[str, Any]] = {r["id"]: r for r in self.purpose.get("roles", [])}
        self.entities: dict[str, dict[str, Any]] = dict(self.domain.get("entities", {}))
        self.domain_events: dict[str, dict[str, Any]] = {e["name"]: e for e in self.domain.get("domain_events", [])}
        self.bindings_by_cap: dict[str, list[dict[str, Any]]] = {}
        for b in self.implementation.get("bindings", []):
            self.bindings_by_cap.setdefault(b["capability_id"], []).append(b)
        self.sensitive_attributes: set[str] = {
            a["name"] for e in self.entities.values() for a in e.get("attributes", []) if a.get("sensitive")
        }

    @property
    def system_id(self) -> str:
        return self.purpose["system_id"]

    @property
    def spec_version(self) -> str | None:
        return self.purpose.get("spec_version")

    def process(self, process_id: str) -> dict[str, Any] | None:
        return self.procs.get(process_id)

    def step(self, process_id: str, step_id: str) -> dict[str, Any] | None:
        proc = self.procs.get(process_id)
        if not proc:
            return None
        return next((s for s in proc["steps"] if s["step_id"] == step_id), None)

    def capability(self, capability_id: str | None) -> dict[str, Any] | None:
        return self.caps.get(capability_id or "")

    def binding_summary(self, capability_id: str) -> dict[str, Any]:
        """Лучшая привязка слоя 5 (implemented — приоритет), кратко."""
        bs = self.bindings_by_cap.get(capability_id, [])
        if not bs:
            return {"status": "not_bound"}
        order = {"implemented": 0, "stub": 1, "planned": 2, "unconfirmed": 3, "deprecated": 4}
        b = sorted(bs, key=lambda x: order.get(x["status"], 9))[0]
        out = {"status": b["status"], "kind": b["kind"]}
        if b.get("location"):
            out["location"] = b["location"]
        if b.get("feature_flag"):
            out["feature_flag"] = b["feature_flag"]
        return out

    def used_in_steps(self, capability_id: str) -> list[dict[str, str]]:
        return [
            {"process_id": p["process_id"], "step_id": s["step_id"]}
            for p in self.processes["processes"]
            for s in p["steps"]
            if s.get("capability_id") == capability_id
        ]

    # ------------------------------------------------------------- ограничения
    def constraints_for_capability(self, capability_id: str, enforcement: str | None = None) -> list[dict[str, Any]]:
        out = []
        for c in self.constraints.values():
            if enforcement and c.get("enforcement") != enforcement:
                continue
            if capability_id in c.get("applies_to", {}).get("capabilities", []):
                out.append(c)
        return out

    def constraints_for_process(self, process: dict[str, Any], enforcement: str | None = None) -> list[dict[str, Any]]:
        """Те же правила применимости, что в tools/drift.py: по процессу, по возможностям шагов,
        по сущности-агрегату; ограничение без applies_to применимо ко всем."""
        step_caps = {s.get("capability_id") for s in process["steps"]}
        touched = {process["aggregate_entity"]} | {s["transition"]["entity"] for s in process["steps"] if s.get("transition")}
        out = []
        for c in self.constraints.values():
            if enforcement and c.get("enforcement") != enforcement:
                continue
            at = c.get("applies_to", {})
            hit = (
                process["process_id"] in at.get("processes", [])
                or bool(step_caps & set(at.get("capabilities", [])))
                or bool(touched & set(at.get("entities", [])))
            )
            if hit or not any(at.values()):
                out.append(c)
        return out

    # ------------------------------------------------------------ правило гейта
    def gate_rule(self, step: dict[str, Any]) -> GateRule:
        cap = self.capability(step.get("capability_id"))
        declared = step.get("human_gate", {}) or {}
        rule = GateRule(
            required=bool(declared.get("required")),
            approver_role=declared.get("approver_role"),
            declared_reason=declared.get("reason"),
            constraint_id=declared.get("constraint_id"),
        )
        if step.get("actor") == "human":
            rule.required = False
            return rule
        if cap:
            if cap["effect"] == "write_irreversible":
                rule.reasons.append("irreversible")
            if cap.get("human_gate_required"):
                rule.reasons.append("policy")
            if cap.get("system_level"):
                rule.reasons.append("system_level")
            for c in self.constraints_for_capability(cap["capability_id"], "gate"):
                rule.reasons.append(f"constraint:{c['id']}")
        if rule.reasons:
            rule.required = True
        # Условный гейт: только через ограничение с машиночитаемым порогом (ADR-010).
        if rule.required and declared.get("condition"):
            c = self.constraints.get(declared.get("constraint_id") or "")
            if c and c.get("enforcement") == "gate" and c.get("threshold"):
                rule.threshold_constraint = c
        return rule

    @staticmethod
    def threshold_exceeded(constraint: dict[str, Any], value: Any) -> bool:
        """True — порог превышен, нужен человек; False — ниже порога, AutoApprovedByRule."""
        th = constraint["threshold"]
        return bool(GATE_OPERATORS[th["operator"]](value, th["value"]))

    # ------------------------------------------------------------------ домен
    def transition_allowed(self, entity: str, frm: str | None, to: str) -> bool:
        e = self.entities.get(entity)
        if not e:
            return False
        if not e.get("transitions"):
            return to in e.get("states", [to])
        if frm is None:
            return any(t["to"] == to for t in e["transitions"]) or e.get("initial_state") == to
        return any(t["from"] == frm and t["to"] == to for t in e["transitions"])

    def entity_identity(self, entity: str) -> str:
        return self.entities.get(entity, {}).get("identity", "id")

    def mask(self, data: Any) -> Any:
        """Убрать чувствительные атрибуты слоя 2 из полезной нагрузки события."""
        if isinstance(data, dict):
            return {k: ("<redacted>" if k in self.sensitive_attributes else self.mask(v)) for k, v in data.items()}
        if isinstance(data, list):
            return [self.mask(v) for v in data]
        return data


def load_model(root: Path | str) -> Model:
    root = Path(root)
    report = _validate.run(root)
    findings = [f.__dict__ for f in report.findings]
    if report.errors:
        raise InvalidModel(findings)

    def _load(name: str) -> dict[str, Any]:
        with (root / name).open(encoding="utf-8") as fh:
            return json.load(fh)

    return Model(
        root=root,
        purpose=_load("purpose.json"),
        domain=_load("domain.json"),
        processes=_load("processes.json"),
        capabilities=_load("capabilities.json"),
        implementation=_load("implementation.json"),
        warnings=[f for f in findings if f["level"] != "ERROR"],
    )


def drift_holes(model: Model, repo: Path | None = None, strict: bool = False) -> list[dict[str, Any]]:
    """Дыры drift в формате `drift.py --json`."""
    return [asdict(h) for h in _drift.run(model.root, repo, strict)]
