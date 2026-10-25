"""Проекции ядра (docs/02): ProcessInstance, PendingGates, EntityState, AuditTrail.

Проекция — функция от последовательности событий; каждая хранит последний применённый
sequence по агрегату и игнорирует повторы. Снимков нет: пересборка всегда с нуля по журналу.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from .event_store import EventStore, aggregate_key

TERMINAL = {"completed", "aborted"}


class Projection:
    def __init__(self) -> None:
        self._applied: dict[tuple[str, str, str], int] = {}
        self.gaps: list[dict[str, Any]] = []

    def apply(self, event: dict[str, Any]) -> bool:
        key = aggregate_key(event)
        last = self._applied.get(key, 0)
        seq = event["sequence"]
        if seq <= last:
            return False  # уже применено
        if seq != last + 1 and key[0] == "process_instance":
            self.gaps.append({"aggregate": key, "expected": last + 1, "got": seq})
        self._applied[key] = seq
        self._on(event)
        return True

    def _on(self, event: dict[str, Any]) -> None:  # noqa: D401
        raise NotImplementedError

    def rebuild(self, events: Iterable[dict[str, Any]]) -> "Projection":
        for e in events:
            self.apply(e)
        return self


class ProcessInstances(Projection):
    """На каком шаге экземпляр, что ждёт, чем кончился."""

    def __init__(self) -> None:
        super().__init__()
        self.instances: dict[str, dict[str, Any]] = {}

    def get(self, instance_id: str) -> dict[str, Any] | None:
        return self.instances.get(instance_id)

    def _on(self, e: dict[str, Any]) -> None:
        agg = e["aggregate"]
        if agg["kind"] != "process_instance":
            return
        iid = agg["id"]
        t = e["event_type"]
        sc = e.get("scope", {})
        step = sc.get("step_id")
        p = e.get("payload", {})
        if t == "ProcessInitiated":
            self.instances[iid] = {
                "instance_id": iid,
                "process_id": agg.get("type") or sc.get("process_id"),
                "aggregate_id": p.get("aggregate_id"),
                "status": "running",
                "started_at": e["occurred_at"],
                "plan": p.get("plan", []),
                "inputs": p.get("inputs", {}),
                "correlation_id": e.get("metadata", {}).get("correlation_id"),
                "current_step": None,
                "waiting": None,
                "completed_steps": [],
                "skipped_steps": [],
                "outputs": {},
                "failed_counts": {},
                "gates": {},
                "auto_approvals": {},
                "violations": [],
                "missing": [],
                "last_event_id": e["event_id"],
                "updated_at": e["occurred_at"],
                "outcome": None,
            }
            return
        inst = self.instances.get(iid)
        if inst is None:
            return
        inst["last_event_id"] = e["event_id"]
        inst["updated_at"] = e["occurred_at"]
        if t == "StepRequested":
            inst["current_step"] = step
            if p.get("awaiting") == "human":
                inst["status"] = "waiting_human"
                inst["waiting"] = {"kind": "human_step", "step_id": step, "role": p.get("role")}
            else:
                inst["status"] = "running"
                inst["waiting"] = None
        elif t == "StepCompleted":
            inst["current_step"] = step
            if p.get("skipped"):
                inst["skipped_steps"].append(step)
            else:
                inst["completed_steps"].append(step)
                inst["outputs"][step] = p.get("outputs", {})
            inst["status"] = "running"
            inst["waiting"] = None
        elif t == "StepFailed":
            inst["failed_counts"][step] = inst["failed_counts"].get(step, 0) + 1
            inst["status"] = "running"
        elif t == "HumanGateRequested":
            inst["gates"][step] = {
                "status": "pending",
                "requested_event_id": e["event_id"],
                "requested_at": e["occurred_at"],
                "approver_role": p.get("approver_role"),
                "reason": p.get("reason"),
                "constraint_id": sc.get("constraint_id"),
                "claimed_by": None,
                "resolved_by": None,
            }
            inst["status"] = "waiting_gate"
            inst["waiting"] = {"kind": "gate", "step_id": step, "approver_role": p.get("approver_role")}
        elif t == "HumanGateClaimed":
            g = inst["gates"].setdefault(step, {"status": "pending"})
            g["status"] = "claimed"
            g["claimed_by"] = e.get("actor", {}).get("id")
        elif t == "HumanGateResolved":
            g = inst["gates"].setdefault(step, {})
            g["status"] = p.get("decision")
            g["resolved_by"] = e.get("actor", {}).get("id")
            g["resolved_at"] = e["occurred_at"]
            inst["status"] = "running"
            inst["waiting"] = None
        elif t == "AutoApprovedByRule":
            inst["auto_approvals"][step] = p
        elif t == "ConstraintViolated":
            inst["violations"].append({"step_id": step, "constraint_id": sc.get("constraint_id"), "detail": p})
            if step:
                inst["failed_counts"][step] = inst["failed_counts"].get(step, 0) + 1
        elif t == "CapabilityMissing":
            inst["missing"].append(p)
            inst["status"] = "blocked"
            inst["waiting"] = {"kind": "delivery", "step_id": step}
        elif t == "ProcessCompleted":
            inst["status"] = "completed"
            inst["outcome"] = p
            inst["waiting"] = None
        elif t == "ProcessAborted":
            inst["status"] = "aborted"
            inst["outcome"] = p
            inst["waiting"] = None


class PendingGates(Projection):
    """Очередь решений человека: HumanGateRequested без парного HumanGateResolved."""

    def __init__(self) -> None:
        super().__init__()
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

    def _on(self, e: dict[str, Any]) -> None:
        if e["aggregate"]["kind"] != "process_instance":
            return
        key = (e["aggregate"]["id"], e.get("scope", {}).get("step_id", ""))
        t = e["event_type"]
        p = e.get("payload", {})
        if t == "HumanGateRequested":
            self.items[key] = {
                "instance_id": key[0],
                "process_id": e["scope"].get("process_id"),
                "step_id": key[1],
                "capability_id": e["scope"].get("capability_id"),
                "constraint_id": e["scope"].get("constraint_id"),
                "approver_role": p.get("approver_role"),
                "reason": p.get("reason"),
                "requested_at": e["occurred_at"],
                "claimed_by": None,
                "detail": p,
            }
        elif t == "HumanGateClaimed" and key in self.items:
            self.items[key]["claimed_by"] = e.get("actor", {}).get("id")
        elif t == "HumanGateResolved":
            self.items.pop(key, None)
        elif t in ("ProcessAborted", "ProcessCompleted"):
            for k in [k for k in self.items if k[0] == key[0]]:
                self.items.pop(k)

    def list(self, role: str | None = None, now: str | None = None) -> list[dict[str, Any]]:
        out = []
        for item in self.items.values():
            if role and item["approver_role"] != role:
                continue
            row = dict(item)
            if now:
                row["age_seconds"] = _age(item["requested_at"], now)
            out.append(row)
        out.sort(key=lambda r: r["requested_at"])
        return out


class EntityState(Projection):
    """Текущее состояние каждой сущности по EntityTransitioned; сверяется с хранилищем (post_check)."""

    def __init__(self) -> None:
        super().__init__()
        self.states: dict[str, dict[str, dict[str, Any]]] = {}

    def _on(self, e: dict[str, Any]) -> None:
        if e["event_type"] != "EntityTransitioned" or e["aggregate"]["kind"] != "entity":
            return
        ent = e["aggregate"].get("type") or e.get("scope", {}).get("entity")
        p = e.get("payload", {})
        self.states.setdefault(ent, {})[e["aggregate"]["id"]] = {
            "state": p.get("to"),
            "previous": p.get("from"),
            "updated_at": e["occurred_at"],
            "last_event_id": e["event_id"],
        }

    def get(self, entity: str, id_: str) -> str | None:
        return self.states.get(entity, {}).get(id_, {}).get("state")


def audit_trail(
    store: EventStore,
    *,
    process_id: str | None = None,
    instance_id: str | None = None,
    step_id: str | None = None,
    entity: str | None = None,
    actor_id: str | None = None,
    event_type: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[dict[str, Any]]:
    """Ответ аудитору «кто, что, когда, на каком основании»: фильтр по scope, actor, времени."""
    out = []
    for e in store:
        sc = e.get("scope", {})
        if process_id and sc.get("process_id") != process_id:
            continue
        if instance_id and not (e["aggregate"]["kind"] == "process_instance" and e["aggregate"]["id"] == instance_id):
            continue
        if step_id and sc.get("step_id") != step_id:
            continue
        if entity and (sc.get("entity") or e["aggregate"].get("type")) != entity:
            continue
        if actor_id and e.get("actor", {}).get("id") != actor_id:
            continue
        if event_type and e["event_type"] != event_type:
            continue
        if since and e["occurred_at"] < since:
            continue
        if until and e["occurred_at"] > until:
            continue
        out.append(e)
    return out


def rebuild_all(store: EventStore) -> dict[str, Projection]:
    events = store.all()
    return {
        "ProcessInstances": ProcessInstances().rebuild(events),
        "PendingGates": PendingGates().rebuild(events),
        "EntityState": EntityState().rebuild(events),
    }


def _age(requested_at: str, now: str) -> float:
    try:
        a = datetime.fromisoformat(requested_at.replace("Z", "+00:00"))
        b = datetime.fromisoformat(now.replace("Z", "+00:00"))
        return (b - a).total_seconds()
    except ValueError:
        return 0.0
