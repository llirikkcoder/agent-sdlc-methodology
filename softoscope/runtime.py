"""Runtime цикла исполнения (docs/03): Static Guard → Plan → Execute → Verify → Close.

Состояние экземпляра нигде не хранится отдельно: перед каждым решением проекция
ProcessInstances пересобирается из журнала. Возможности исполняют «исполнители» —
вызываемые объекты, которые передаёт реализация (вызов MCP, HTTP, функция, мок);
runtime их не подбирает и не домысливает (MissingCapabilityError).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .event_store import EventStore
from .model import Model, drift_holes, snake
from .projections import EntityState, PendingGates, ProcessInstances

SCHEMA_VERSION = "1.0"
SUGGESTED = {
    "not_declared": "описать возможность в слое 4",
    "not_bound": "добавить привязку в слой 5",
    "not_implemented": "реализовать и перевести привязку в implemented",
    "stale": "обновить слой 5 по коду",
    "role_mismatch": "выдать роль",
    "unconfirmed_constraint": "подтвердить или снять ограничение (человек)",
}


class CapabilityError(Exception):
    """Исполнитель не смог выполнить возможность (StepFailed)."""


class RuntimeError_(Exception):
    """Нарушение протокола runtime (неверный вызов), не событие процесса."""


@dataclass
class Context:
    model: Model
    instance: dict[str, Any]
    step: dict[str, Any]
    capability: dict[str, Any] | None
    correlation_id: str
    read_entity: Callable[[str, str], dict[str, Any] | None] | None


Executor = Callable[[dict[str, Any], dict[str, Any], Context], dict[str, Any]]
Checker = Callable[[dict[str, Any], Context], str | None]


class Runtime:
    def __init__(
        self,
        model: Model,
        store: EventStore,
        executors: dict[str, Executor],
        *,
        read_entity: Callable[[str, str], dict[str, Any] | None] | None = None,
        constraint_checkers: dict[str, Checker] | None = None,
        repo: Path | None = None,
        clock: Callable[[], str] | None = None,
        id_factory: Callable[[], str] | None = None,
        agent_id: str = "agent",
        max_retries: int = 1,
    ):
        self.model = model
        self.store = store
        self.executors = executors
        self.read_entity = read_entity
        self.checkers = constraint_checkers or {}
        self.repo = repo
        self.clock = clock or (lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.ids = id_factory or (lambda: str(uuid.uuid4()))
        self.agent_id = agent_id
        self.max_retries = max_retries
        self._last_event: dict[str, str] = {}

    # ------------------------------------------------------------- проекции
    def instances(self) -> ProcessInstances:
        return ProcessInstances().rebuild(self.store.all())

    def instance(self, instance_id: str) -> dict[str, Any]:
        inst = self.instances().get(instance_id)
        if inst is None:
            raise RuntimeError_(f"экземпляр {instance_id!r} не найден в журнале")
        return inst

    def pending_gates(self, role: str | None = None) -> list[dict[str, Any]]:
        return PendingGates().rebuild(self.store.all()).list(role=role, now=self.clock())

    def entity_state(self) -> EntityState:
        return EntityState().rebuild(self.store.all())

    # --------------------------------------------------------------- события
    def _emit(
        self,
        event_type: str,
        *,
        inst: dict[str, Any] | None = None,
        aggregate: dict[str, str] | None = None,
        actor: dict[str, Any],
        scope: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        iid = inst["instance_id"] if inst else None
        if aggregate is None:
            assert inst is not None
            aggregate = {"kind": "process_instance", "type": inst["process_id"], "id": iid}
        sc = {"system_id": self.model.system_id}
        if inst:
            sc["process_id"] = inst["process_id"]
        sc.update({k: v for k, v in (scope or {}).items() if v is not None})
        meta: dict[str, Any] = {
            "correlation_id": correlation_id or (inst or {}).get("correlation_id") or iid or "-",
            "schema_version": SCHEMA_VERSION,
        }
        if iid and iid in self._last_event:
            meta["causation_id"] = self._last_event[iid]
        meta.update(metadata or {})
        event = {
            "event_id": self.ids(),
            "event_type": event_type,
            "occurred_at": self.clock(),
            "aggregate": aggregate,
            "actor": {k: v for k, v in actor.items() if v is not None},
            "scope": sc,
            "payload": self.model.mask(payload or {}),
            "metadata": meta,
        }
        stored = self.store.append(event)
        if stored is None:
            raise RuntimeError_("журнал отверг событие как дубликат")
        if iid:
            self._last_event[iid] = event["event_id"]
        return event

    def _agent_actor(self, step: dict[str, Any]) -> dict[str, Any]:
        return {"kind": step.get("actor", "agent"), "id": self.agent_id if step.get("actor") == "agent" else "runtime", "role": step.get("role")}

    # ---------------------------------------------------------- Static Guard
    def static_guard(self, process_id: str) -> list[dict[str, Any]]:
        """Дыры, при которых процесс не исполняется: MissingCapabilityError (docs/03) и
        неподтверждённые критические ограничения (ADR-017)."""
        proc = self.model.process(process_id)
        if proc is None:
            raise RuntimeError_(f"процесс {process_id!r} не описан в слое 3")
        problems: list[dict[str, Any]] = []
        for h in drift_holes(self.model, self.repo, strict=True):
            if h["level"] != "ERROR" or h["process_id"] != process_id:
                continue
            cid = h["capability_id"]
            problems.append(
                {
                    "process_id": process_id,
                    "step_id": h["step_id"],
                    "capability_id": cid,
                    "reason": h["reason"],
                    "binding_status": self.model.binding_summary(cid)["status"] if cid else None,
                    "detail": h["detail"],
                    "suggested_action": SUGGESTED.get(h["reason"], "решение человека"),
                }
            )
        for s in proc["steps"]:
            cid = s.get("capability_id")
            if s["actor"] in ("agent", "system") and cid and cid in self.model.caps and cid not in self.executors:
                if self.model.binding_summary(cid)["status"] == "implemented":
                    problems.append(
                        {
                            "process_id": process_id,
                            "step_id": s["step_id"],
                            "capability_id": cid,
                            "reason": "not_bound",
                            "binding_status": "implemented",
                            "detail": "в runtime не зарегистрирован исполнитель возможности",
                            "suggested_action": "подключить исполнитель (вызов MCP, функция)",
                        }
                    )
        return problems

    # ------------------------------------------------------------ жизненный цикл
    def start(
        self,
        process_id: str,
        aggregate_id: str,
        *,
        inputs: dict[str, Any] | None = None,
        actor: dict[str, Any] | None = None,
        correlation_id: str | None = None,
        instance_id: str | None = None,
        run: bool = True,
    ) -> str:
        proc = self.model.process(process_id)
        if proc is None:
            raise RuntimeError_(f"процесс {process_id!r} не описан в слое 3")
        iid = instance_id or self.ids()
        plan = [
            {
                "step_id": s["step_id"],
                "actor": s["actor"],
                "capability_id": s.get("capability_id"),
                "effect": (self.model.capability(s.get("capability_id")) or {}).get("effect"),
                "gate": self.model.gate_rule(s).as_dict(),
                "transition": s.get("transition"),
            }
            for s in proc["steps"]
        ]
        inst = {"instance_id": iid, "process_id": process_id, "correlation_id": correlation_id or iid}
        self._emit(
            "ProcessInitiated",
            inst=inst,
            actor=actor or {"kind": "system", "id": "runtime"},
            payload={"aggregate_id": aggregate_id, "inputs": inputs or {}, "plan": plan},
            correlation_id=correlation_id or iid,
        )
        problems = self.static_guard(process_id)
        for p in problems:
            self._emit(
                "CapabilityMissing",
                inst=inst,
                actor={"kind": "system", "id": "static_guard"},
                scope={"step_id": p["step_id"], "capability_id": p["capability_id"]},
                payload=p,
            )
        if problems:
            return iid
        if run:
            self.run(iid)
        return iid

    def run(self, instance_id: str) -> str:
        """Исполнять шаги, пока процесс не завершится или не остановится на человеке/дыре."""
        guard = 0
        while True:
            guard += 1
            if guard > 1000:
                raise RuntimeError_("слишком много итераций: зацикливание on_failure")
            inst = self.instance(instance_id)
            if inst["status"] in ("completed", "aborted", "blocked", "waiting_gate", "waiting_human"):
                return inst["status"]
            step = self._next_step(inst)
            if step is None:
                self._close(inst)
                return self.instance(instance_id)["status"]
            if not self._execute_step(inst, step):
                return self.instance(instance_id)["status"]

    def _next_step(self, inst: dict[str, Any]) -> dict[str, Any] | None:
        done = set(inst["completed_steps"]) | set(inst["skipped_steps"])
        proc = self.model.process(inst["process_id"])
        assert proc is not None
        steps = proc["steps"]
        if not inst["completed_steps"] and not inst["skipped_steps"]:
            return steps[0]
        last = (inst["completed_steps"] + inst["skipped_steps"])[-1]
        last_step = next(s for s in steps if s["step_id"] == last)
        order = [s["step_id"] for s in steps]
        # next[] задаёт ветвление; условия словами runtime не вычисляет — берётся первый не сделанный.
        candidates = [n for n in last_step.get("next", []) if n not in done] or order[order.index(last) + 1 :]
        for sid in candidates:
            if sid not in done:
                return next(s for s in steps if s["step_id"] == sid)
        return None

    # --------------------------------------------------------------- шаг
    def _inputs_for(self, inst: dict[str, Any]) -> dict[str, Any]:
        data = dict(inst["inputs"])
        for out in inst["outputs"].values():
            data.update(out)
        return data

    def _entity_id(self, entity: str, inst: dict[str, Any], data: dict[str, Any]) -> str | None:
        proc = self.model.process(inst["process_id"]) or {}
        for key in (f"{snake(entity)}_id", self.model.entity_identity(entity)):
            if key in data and key != "id":
                return str(data[key])
        if proc.get("aggregate_entity") == entity:
            return inst.get("aggregate_id")
        if "id" in data:
            return str(data["id"])
        return None

    def _threshold_value(self, c: dict[str, Any], inst: dict[str, Any], data: dict[str, Any]) -> tuple[Any, str | None]:
        entity, attr = c["threshold"]["attribute"].split(".", 1)
        eid = self._entity_id(entity, inst, data)
        if self.read_entity and eid:
            row = self.read_entity(entity, eid) or {}
            if attr in row:
                return row[attr], eid
        for key in (f"{entity}.{attr}", f"{snake(entity)}_{attr}", attr):
            if key in data:
                return data[key], eid
        return None, eid

    def _execute_step(self, inst: dict[str, Any], step: dict[str, Any]) -> bool:
        """True — продолжать цикл; False — процесс ждёт (гейт, человек) или завершён."""
        sid = step["step_id"]
        cap = self.model.capability(step.get("capability_id"))
        scope = {"step_id": sid, "capability_id": step.get("capability_id")}
        data = self._inputs_for(inst)

        if step["actor"] == "human":
            self._emit("StepRequested", inst=inst, actor={"kind": "system", "id": "runtime"}, scope=scope, payload={"awaiting": "human", "role": step.get("role")})
            return False

        gate = inst["gates"].get(sid)
        if gate and gate.get("status") == "rejected":
            return self._on_failure(inst, step, "gate_rejected", "человек отклонил шаг")

        rule = self.model.gate_rule(step)
        if rule.required and not (gate and gate.get("status") == "approved") and sid not in inst["auto_approvals"]:
            c = rule.threshold_constraint
            if c:
                value, eid = self._threshold_value(c, inst, data)
                if value is not None and not self.model.threshold_exceeded(c, value):
                    th = c["threshold"]
                    self._emit(
                        "AutoApprovedByRule",
                        inst=inst,
                        actor={"kind": "system", "id": "runtime"},
                        scope=scope | {"constraint_id": c["id"]},
                        payload={"attribute": th["attribute"], "operator": th["operator"], "threshold": th["value"], "unit": th.get("unit"), "value": value, "entity_id": eid},
                    )
                    inst = self.instance(inst["instance_id"])
                    return self._call(inst, step, cap, scope, data)
                detail = {"threshold_value": value} if value is not None else {"note": "порог не вычислен — гейт безусловный"}
                self._request_gate(inst, step, rule, scope, detail)
                return False
            self._request_gate(inst, step, rule, scope, {})
            return False
        return self._call(inst, step, cap, scope, data)

    def _request_gate(self, inst: dict[str, Any], step: dict[str, Any], rule: Any, scope: dict[str, Any], extra: dict[str, Any]) -> None:
        approver = rule.approver_role or next((r for r, v in self.model.roles.items() if v.get("may_approve")), None)
        self._emit(
            "HumanGateRequested",
            inst=inst,
            actor={"kind": "system", "id": "runtime"},
            scope=scope | {"constraint_id": rule.constraint_id},
            payload={"approver_role": approver, "reason": rule.declared_reason or (rule.reasons[0] if rule.reasons else "policy"), "reasons": rule.reasons, "condition": (step.get("human_gate") or {}).get("condition")} | extra,
        )

    def _call(self, inst: dict[str, Any], step: dict[str, Any], cap: dict[str, Any] | None, scope: dict[str, Any], data: dict[str, Any]) -> bool:
        sid = step["step_id"]
        actor = self._agent_actor(step)
        if cap is None:
            return self._on_failure(inst, step, "no_capability", "у шага нет возможности")
        ctx = Context(self.model, inst, step, cap, inst["correlation_id"], self.read_entity)
        is_read = cap["effect"] == "read" and cap["kind"] != "llm_step"
        if not is_read:
            self._emit("StepRequested", inst=inst, actor=actor, scope=scope, payload={"inputs": data})
        # Переход: проверяем до вызова, чтобы не менять мир, если автомат слоя 2 этого не допускает.
        tr = step.get("transition")
        if tr:
            eid = self._entity_id(tr["entity"], inst, data)
            current = self.entity_state().get(tr["entity"], eid) if eid else None
            frm = current if current is not None else tr.get("from")
            if not self.model.transition_allowed(tr["entity"], frm, tr["to"]):
                self._emit("StepFailed", inst=inst, actor=actor, scope=scope, payload={"error": "transition_not_allowed", "entity": tr["entity"], "from": frm, "to": tr["to"]})
                return self._on_failure(inst, step, "transition_not_allowed", f"{tr['entity']}: {frm} → {tr['to']}")
        for c in self.model.constraints_for_capability(cap["capability_id"], "pre_check"):
            checker = self.checkers.get(c["id"])
            if checker is None:
                continue
            problem = checker(data, ctx)
            if problem:
                self._emit("ConstraintViolated", inst=inst, actor=actor, scope=scope | {"constraint_id": c["id"]}, payload={"rule": c["rule"], "detail": problem, "enforcement": "pre_check"})
                return self._on_failure(inst, step, "constraint", problem)
        executor = self.executors.get(cap["capability_id"])
        if executor is None:
            self._emit("StepFailed", inst=inst, actor=actor, scope=scope, payload={"error": "no_executor"})
            return self._on_failure(inst, step, "no_executor", "исполнитель не зарегистрирован")
        try:
            outputs = dict(executor(cap, data, ctx) or {})
        except CapabilityError as exc:
            self._emit("StepFailed", inst=inst, actor=actor, scope=scope, payload={"error": str(exc)})
            return self._on_failure(inst, step, "capability_error", str(exc))
        model_meta = outputs.pop("_model", None)
        if cap["kind"] == "llm_step" and not (isinstance(model_meta, dict) and model_meta.get("name")):
            self._emit("StepFailed", inst=inst, actor=actor, scope=scope, payload={"error": "llm_step без metadata.model (ADR-015)"})
            return self._on_failure(inst, step, "no_model_metadata", "llm_step обязан сообщить модель и версию промпта")
        idem = outputs.pop("_idempotency_key", None)
        meta: dict[str, Any] = {}
        if model_meta:
            meta["model"] = model_meta
        if idem:
            meta["idempotency_key"] = str(idem)
        self._emit("StepCompleted", inst=inst, actor=actor, scope=scope, payload={"outputs": outputs}, metadata=meta)
        self._after_success(inst, step, actor, scope, data | outputs)
        return True

    def _after_success(self, inst: dict[str, Any], step: dict[str, Any], actor: dict[str, Any], scope: dict[str, Any], data: dict[str, Any]) -> None:
        tr = step.get("transition")
        if tr:
            eid = self._entity_id(tr["entity"], inst, data) or inst.get("aggregate_id") or "-"
            current = self.entity_state().get(tr["entity"], eid)
            self._emit(
                "EntityTransitioned",
                inst=inst,
                aggregate={"kind": "entity", "type": tr["entity"], "id": eid},
                actor=actor,
                scope=scope | {"entity": tr["entity"]},
                payload={"from": current if current is not None else tr.get("from"), "to": tr["to"]},
            )
        for name in step.get("emits", []):
            ent = self.model.domain_events.get(name, {}).get("entity")
            eid = (self._entity_id(ent, inst, data) if ent else None) or inst.get("aggregate_id") or "-"
            self._emit(
                name,
                inst=inst,
                aggregate={"kind": "entity", "type": ent or inst["process_id"], "id": eid},
                actor=actor,
                scope=scope | {"entity": ent},
                payload={"step_id": step["step_id"]},
            )

    def _on_failure(self, inst: dict[str, Any], step: dict[str, Any], kind: str, detail: str) -> bool:
        inst = self.instance(inst["instance_id"])
        sid = step["step_id"]
        policy = step.get("on_failure", "abort")
        cap = self.model.capability(step.get("capability_id")) or {}
        scope = {"step_id": sid, "capability_id": step.get("capability_id")}
        if kind == "gate_rejected":
            policy = "skip" if policy == "skip" else "abort"
        if policy == "retry":
            if cap.get("idempotency", "none") != "none" and inst["failed_counts"].get(sid, 0) <= self.max_retries:
                return True
            detail = f"{detail}; повтор невозможен (idempotency={cap.get('idempotency', 'none')} или лимит {self.max_retries})"
            policy = "abort"
        if policy == "escalate":
            if (inst["gates"].get(sid) or {}).get("status") == "approved":
                policy = "abort"  # человек уже разрешил повтор, а шаг снова не удался
            else:
                approver = (step.get("human_gate") or {}).get("approver_role") or next((r for r, v in self.model.roles.items() if v.get("may_approve")), None)
                self._emit(
                    "HumanGateRequested",
                    inst=inst,
                    actor={"kind": "system", "id": "runtime"},
                    scope=scope,
                    payload={"approver_role": approver, "reason": "low_confidence", "escalation": kind, "detail": detail},
                )
                return False
        if policy == "skip":
            self._emit("StepCompleted", inst=inst, actor={"kind": "system", "id": "runtime"}, scope=scope, payload={"skipped": True, "reason": kind, "detail": detail})
            return True
        self._emit("ProcessAborted", inst=inst, actor={"kind": "system", "id": "runtime"}, scope=scope, payload={"reason": kind, "detail": detail, "step_id": sid})
        return False

    def _close(self, inst: dict[str, Any]) -> None:
        proc = self.model.process(inst["process_id"])
        assert proc is not None
        ctx = Context(self.model, inst, {}, None, inst["correlation_id"], self.read_entity)
        data = self._inputs_for(inst)
        for c in self.model.constraints_for_process(proc, "post_check"):
            checker = self.checkers.get(c["id"])
            if checker is None:
                continue
            problem = checker(data, ctx)
            if problem:
                self._emit("ConstraintViolated", inst=inst, actor={"kind": "system", "id": "runtime"}, scope={"constraint_id": c["id"]}, payload={"rule": c["rule"], "detail": problem, "enforcement": "post_check"})
                self._emit("ProcessAborted", inst=inst, actor={"kind": "system", "id": "runtime"}, payload={"reason": "post_check", "detail": problem})
                return
        self._emit("ProcessCompleted", inst=inst, actor={"kind": "system", "id": "runtime"}, payload={"outcome": "ok", "completed_steps": inst["completed_steps"], "skipped_steps": inst["skipped_steps"]})

    # ------------------------------------------------------------ человек
    def _require_human(self, actor: dict[str, Any]) -> None:
        if actor.get("kind") != "human" or not actor.get("id"):
            raise RuntimeError_("решение гейта фиксируется только от человека с actor.id (не 'system')")

    def _check_approver(self, gate: dict[str, Any], actor: dict[str, Any]) -> None:
        role = actor.get("role")
        need = gate.get("approver_role")
        if need and role != need:
            raise RuntimeError_(f"role_mismatch: гейт ждёт роль {need!r}, актор — {role!r}")
        if not need and not self.model.roles.get(role or "", {}).get("may_approve"):
            raise RuntimeError_(f"role_mismatch: роль {role!r} не может утверждать")

    def claim_gate(self, instance_id: str, step_id: str, actor: dict[str, Any]) -> dict[str, Any]:
        inst = self.instance(instance_id)
        gate = inst["gates"].get(step_id)
        if not gate or gate.get("status") not in ("pending", "claimed"):
            raise RuntimeError_(f"по шагу {step_id!r} нет ожидающего гейта")
        self._require_human(actor)
        self._check_approver(gate, actor)
        step = self.model.step(inst["process_id"], step_id) or {}
        return self._emit("HumanGateClaimed", inst=inst, actor=actor, scope={"step_id": step_id, "capability_id": step.get("capability_id"), "constraint_id": gate.get("constraint_id")}, payload={})

    def resolve_gate(self, instance_id: str, step_id: str, decision: str, actor: dict[str, Any], note: str | None = None, run: bool = True) -> str:
        if decision not in ("approved", "rejected"):
            raise RuntimeError_("decision: approved | rejected")
        inst = self.instance(instance_id)
        gate = inst["gates"].get(step_id)
        if not gate or gate.get("status") not in ("pending", "claimed"):
            raise RuntimeError_(f"по шагу {step_id!r} нет ожидающего гейта")
        self._require_human(actor)
        self._check_approver(gate, actor)
        step = self.model.step(inst["process_id"], step_id) or {}
        self._emit(
            "HumanGateResolved",
            inst=inst,
            actor=actor,
            scope={"step_id": step_id, "capability_id": step.get("capability_id"), "constraint_id": gate.get("constraint_id")},
            payload={"decision": decision, "note": note},
        )
        return self.run(instance_id) if run else self.instance(instance_id)["status"]

    def complete_human_step(self, instance_id: str, step_id: str, actor: dict[str, Any], outputs: dict[str, Any] | None = None, run: bool = True) -> str:
        inst = self.instance(instance_id)
        waiting = inst.get("waiting") or {}
        if inst["status"] != "waiting_human" or waiting.get("step_id") != step_id:
            raise RuntimeError_(f"экземпляр не ждёт человеческий шаг {step_id!r}")
        self._require_human(actor)
        step = self.model.step(inst["process_id"], step_id)
        assert step is not None
        if step.get("role") and actor.get("role") != step["role"]:
            raise RuntimeError_(f"role_mismatch: шаг выполняет роль {step['role']!r}")
        scope = {"step_id": step_id, "capability_id": step.get("capability_id")}
        self._emit("StepCompleted", inst=inst, actor=actor, scope=scope, payload={"outputs": outputs or {}})
        self._after_success(inst, step, actor, scope, self._inputs_for(inst) | (outputs or {}))
        return self.run(instance_id) if run else self.instance(instance_id)["status"]
