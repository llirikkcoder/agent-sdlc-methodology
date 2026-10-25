"""Инструменты scope_* (docs/04) как чистые функции над моделью.

Сервер только читает модель. Ошибки — {"error": {"code", "message"}} с кодами
not_found, invalid_model, drift, invalid_arguments.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .model import InvalidModel, Model, drift_holes, load_model

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "scope_get_purpose",
        "description": "Слой 1 целиком: назначение, цели, non_goals, роли, ограничения. Вызывается первым в сессии.",
        "inputSchema": {"type": "object", "required": ["system_id"], "properties": {"system_id": {"type": "string"}}},
    },
    {
        "name": "scope_list_processes",
        "description": "Список процессов слоя 3 с числом шагов и гейтов (этап Intake).",
        "inputSchema": {
            "type": "object",
            "required": ["system_id"],
            "properties": {"system_id": {"type": "string"}, "status": {"enum": ["active", "planned", "deprecated"]}},
        },
    },
    {
        "name": "scope_get_process_map",
        "description": "Карта процесса: шаги с возможностью (слой 4), привязкой (слой 5), гейтами и переходами.",
        "inputSchema": {"type": "object", "required": ["process_id"], "properties": {"process_id": {"type": "string"}}},
    },
    {
        "name": "scope_get_entity_schema",
        "description": "Сущность слоя 2: атрибуты (с sensitive), состояния, переходы, связи, инварианты.",
        "inputSchema": {"type": "object", "required": ["entity_name"], "properties": {"entity_name": {"type": "string"}}},
    },
    {
        "name": "scope_get_capability",
        "description": "Возможность слоя 4 с привязками слоя 5 и списком шагов, где она используется.",
        "inputSchema": {"type": "object", "required": ["capability_id"], "properties": {"capability_id": {"type": "string"}}},
    },
    {
        "name": "scope_check_drift",
        "description": "Дыры между слоями 3/4 и 5 (и кодом, если сервер сконфигурирован репозиторием). strict — runtime Static Guard.",
        "inputSchema": {"type": "object", "properties": {"strict": {"type": "boolean", "default": False}}},
    },
    {
        "name": "scope_validate_plan",
        "description": "Проверка плана исполнения до старта: неизвестные шаги, отсутствующие возможности, гейты, переходы, входы.",
        "inputSchema": {
            "type": "object",
            "required": ["process_id", "steps"],
            "properties": {
                "process_id": {"type": "string"},
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["step_id"],
                        "properties": {
                            "step_id": {"type": "string"},
                            "capability_id": {"type": "string"},
                            "inputs_keys": {"type": "array", "items": {"type": "string"}},
                            "human_gate": {"type": "boolean", "description": "План предусматривает HumanGateRequested перед шагом."},
                            "entity_state": {"type": "string", "description": "Текущее состояние сущности перехода, если известно."},
                        },
                    },
                },
            },
        },
    },
]

BLOCKING_CODES = {"unknown_step", "missing_capability", "transition_not_allowed", "missing_inputs"}


def _error(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


class ScopeAPI:
    def __init__(self, root: Path | str, repo: Path | str | None = None):
        self.root = Path(root)
        self.repo = Path(repo) if repo else None
        self.model: Model | None = None
        self.load_error: dict[str, Any] | None = None
        self.reload()

    def reload(self) -> None:
        try:
            self.model = load_model(self.root)
            self.load_error = None
        except InvalidModel as exc:
            self.model = None
            self.load_error = _error("invalid_model", "модель не прошла валидатор") | {"findings": exc.findings}
        except (OSError, ValueError) as exc:
            self.model = None
            self.load_error = _error("invalid_model", f"модель не читается: {exc}")

    # ------------------------------------------------------------------ вызов
    def call(self, name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
        arguments = arguments or {}
        handler = getattr(self, name, None) if name.startswith("scope_") else None
        if handler is None:
            return _error("not_found", f"инструмент {name!r} не существует")
        if self.load_error:
            return self.load_error
        try:
            return handler(**arguments)
        except TypeError as exc:
            return _error("invalid_arguments", str(exc))

    def _ok(self, **body: Any) -> dict[str, Any]:
        assert self.model is not None
        return {"spec_version": self.model.spec_version, **body}

    def _check_system(self, system_id: str) -> dict[str, Any] | None:
        assert self.model is not None
        if system_id != self.model.system_id:
            return _error("not_found", f"system_id {system_id!r}: сервер обслуживает {self.model.system_id!r}")
        return None

    # ------------------------------------------------------------ инструменты
    def scope_get_purpose(self, system_id: str) -> dict[str, Any]:
        assert self.model is not None
        if err := self._check_system(system_id):
            return err
        return self._ok(purpose=self.model.purpose)

    def scope_list_processes(self, system_id: str, status: str | None = None) -> dict[str, Any]:
        assert self.model is not None
        if err := self._check_system(system_id):
            return err
        out = []
        for p in self.model.processes["processes"]:
            st = p.get("status", "active")
            if status and st != status:
                continue
            out.append(
                {
                    "process_id": p["process_id"],
                    "name": p["name"],
                    "trigger": p["trigger"],
                    "aggregate_entity": p["aggregate_entity"],
                    "status": st,
                    "steps_count": len(p["steps"]),
                    "human_gates_count": sum(1 for s in p["steps"] if self.model.gate_rule(s).required),
                }
            )
        return self._ok(processes=out)

    def scope_get_process_map(self, process_id: str) -> dict[str, Any]:
        m = self.model
        assert m is not None
        proc = m.process(process_id)
        if not proc:
            return _error("not_found", f"процесс {process_id!r} не описан в слое 3")
        steps = []
        for s in proc["steps"]:
            cap = m.capability(s.get("capability_id"))
            item: dict[str, Any] = {
                "step_id": s["step_id"],
                "name": s["name"],
                "actor": s["actor"],
                "role": s.get("role"),
                "capability": cap,
                "human_gate": s.get("human_gate", {"required": False}),
                "gate_rule": m.gate_rule(s).as_dict(),
                "transition": s.get("transition"),
                "next": s.get("next", []),
                "on_failure": s.get("on_failure", "abort"),
                "emits": s.get("emits", []),
                "condition": s.get("condition"),
                "binding": m.binding_summary(cap["capability_id"]) if cap else None,
            }
            steps.append(item)
        constraints = [
            {"id": c["id"], "rule": c["rule"], "enforcement": c["enforcement"], "severity": c["severity"], "status": c.get("status", "confirmed"), "threshold": c.get("threshold")}
            for c in m.constraints_for_process(proc)
        ]
        return self._ok(
            process_id=process_id,
            name=proc["name"],
            status=proc.get("status", "active"),
            aggregate_entity=proc["aggregate_entity"],
            trigger=proc["trigger"],
            constraints=constraints,
            steps=steps,
            outcomes=proc.get("outcomes", []),
        )

    def scope_get_entity_schema(self, entity_name: str) -> dict[str, Any]:
        assert self.model is not None
        e = self.model.entities.get(entity_name)
        if not e:
            imported = {i for imp in self.model.domain.get("imports", []) for i in imp.get("entities", [])}
            if entity_name in imported:
                return _error("not_found", f"сущность {entity_name!r} импортирована из другой системы; её схема там")
            return _error("not_found", f"сущность {entity_name!r} не описана в слое 2")
        return self._ok(entity_name=entity_name, **e)

    def scope_get_capability(self, capability_id: str) -> dict[str, Any]:
        assert self.model is not None
        cap = self.model.capability(capability_id)
        if not cap:
            return _error("not_found", f"возможность {capability_id!r} не описана в слое 4")
        return self._ok(
            capability=cap,
            bindings=self.model.bindings_by_cap.get(capability_id, []),
            used_in_steps=self.model.used_in_steps(capability_id),
            constraints=[c["id"] for c in self.model.constraints_for_capability(capability_id)],
        )

    def scope_check_drift(self, strict: bool = False) -> dict[str, Any]:
        assert self.model is not None
        holes = drift_holes(self.model, self.repo, bool(strict))
        errors = sum(1 for h in holes if h["level"] == "ERROR")
        return self._ok(holes=holes, errors=errors, strict=bool(strict), repo=str(self.repo) if self.repo else None)

    def scope_validate_plan(self, process_id: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
        m = self.model
        assert m is not None
        proc = m.process(process_id)
        if not proc:
            return _error("not_found", f"процесс {process_id!r} не описан в слое 3")
        problems: list[dict[str, Any]] = []

        def add(step_id: str, code: str, message: str) -> None:
            problems.append({"step_id": step_id, "code": code, "severity": "block" if code in BLOCKING_CODES else "warn", "message": message})

        for ps in steps:
            sid = ps.get("step_id", "")
            step = m.step(process_id, sid)
            if not step:
                add(sid, "unknown_step", "шага нет в процессе")
                continue
            declared_cap = step.get("capability_id")
            cap_id = ps.get("capability_id", declared_cap)
            if step["actor"] in ("agent", "system") or cap_id:
                if cap_id != declared_cap:
                    add(sid, "missing_capability", f"план использует {cap_id!r}, слой 3 требует {declared_cap!r}: подбор возможности по смыслу запрещён")
                    continue
                cap = m.capability(cap_id)
                if cap_id and not cap:
                    add(sid, "missing_capability", f"возможность {cap_id!r} не описана в слое 4")
                    continue
                if cap_id:
                    b = m.binding_summary(cap_id)
                    if b["status"] != "implemented":
                        add(sid, "missing_capability", f"привязка слоя 5: {b['status']} — MissingCapabilityError")
            rule = m.gate_rule(step)
            if rule.required:
                planned_gate = ps.get("human_gate")
                if planned_gate is False:
                    add(sid, "gate_required", "план отключает гейт, а правило требует: " + ", ".join(rule.reasons or [rule.declared_reason or "declared"]))
                    problems[-1]["severity"] = "block"
                elif planned_gate is None:
                    add(sid, "gate_required", "перед шагом нужен HumanGateRequested (" + ", ".join(rule.reasons or [rule.declared_reason or "declared"]) + ")" + ("; ниже порога — AutoApprovedByRule" if rule.threshold_constraint else ""))
            if cap_id and (cap := m.capability(cap_id)):
                pre = m.constraints_for_capability(cap_id, "pre_check")
                if pre:
                    add(sid, "constraint_pre_check", "до вызова проверить: " + "; ".join(f"{c['id']} {c['rule']}" for c in pre))
                keys = ps.get("inputs_keys")
                if keys is not None and cap.get("inputs"):
                    required = [k for k, t in cap["inputs"].items() if not str(t).endswith("?") and "|" not in k]
                    missing = [k for k in required if k not in keys]
                    if missing:
                        add(sid, "missing_inputs", "нет обязательных входов: " + ", ".join(missing))
            tr = step.get("transition")
            if tr:
                frm = ps.get("entity_state", tr.get("from"))
                if not m.transition_allowed(tr["entity"], frm, tr["to"]):
                    add(sid, "transition_not_allowed", f"{tr['entity']}: {frm} → {tr['to']} не описан в слое 2")
        ok = not any(p["severity"] == "block" for p in problems)
        return self._ok(ok=ok, problems=problems)
