"""Минимальная синтетическая модель для тестов ядра: без предметной области, пять слоёв."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PURPOSE: dict[str, Any] = {
    "system_id": "demo_system",
    "spec_version": "0.1.0",
    "purpose": "Синтетическая система для проверки ядра: один элемент проходит путь от черновика до публикации.",
    "goals": [{"id": "G001", "statement": "Ни один элемент дороже порога не публикуется без человека."}],
    "roles": [
        {"id": "approver", "kind": "human", "description": "Утверждает публикации.", "may_approve": True},
        {"id": "operator", "kind": "human", "description": "Подтверждает готовность элемента."},
        {"id": "bot", "kind": "agent", "description": "Агент-исполнитель."},
        {"id": "core", "kind": "system", "description": "Системные задачи."},
    ],
    "hard_constraints": [
        {
            "id": "C001",
            "rule": "Элемент дороже 100 единиц публикуется только с согласия утверждающего.",
            "severity": "critical",
            "enforcement": "gate",
            "applies_to": {"capabilities": ["publish_item"], "entities": ["Item"]},
            "status": "confirmed",
            "threshold": {"attribute": "Item.amount", "operator": ">", "value": 100, "unit": "units"},
        },
        {
            "id": "C002",
            "rule": "Подготовка элемента требует непустого заголовка.",
            "severity": "high",
            "enforcement": "pre_check",
            "applies_to": {"capabilities": ["prepare_item"]},
            "status": "confirmed",
        },
        {
            "id": "C003",
            "rule": "Рискованные действия не выполняются, пока не подтверждён контур исполнения.",
            "severity": "critical",
            "enforcement": "audit_only",
            "applies_to": {"processes": ["risky_flow"]},
            "status": "unconfirmed",
        },
        {
            "id": "C004",
            "rule": "После завершения элемент обязан быть опубликован.",
            "severity": "medium",
            "enforcement": "post_check",
            "applies_to": {"processes": ["item_flow"]},
            "status": "confirmed",
        },
    ],
}

DOMAIN: dict[str, Any] = {
    "system_id": "demo_system",
    "entities": {
        "Item": {
            "description": "Элемент, который готовят и публикуют.",
            "identity": "id",
            "attributes": [
                {"name": "id", "type": "string", "required": True},
                {"name": "title", "type": "string"},
                {"name": "amount", "type": "number"},
                {"name": "owner_email", "type": "string", "sensitive": True},
                {"name": "state", "type": "string", "enum": ["draft", "ready", "published"]},
            ],
            "states": ["draft", "ready", "published"],
            "initial_state": "draft",
            "transitions": [
                {"from": "draft", "to": "ready", "actor_kind": "agent", "reversible": True},
                {"from": "ready", "to": "draft", "actor_kind": "human", "reversible": True},
                {"from": "ready", "to": "published", "actor_kind": "agent", "reversible": False, "guard": "C001"},
            ],
        }
    },
    "domain_events": [{"name": "ItemPublished", "entity": "Item", "description": "Элемент опубликован."}],
}

CAPABILITIES: dict[str, Any] = {
    "system_id": "demo_system",
    "capabilities": [
        {"capability_id": "read_item", "name": "Прочитать элемент", "kind": "function", "effect": "read", "human_gate_required": False, "touches_entities": ["Item"], "inputs": {"item_id": "string"}, "outputs": {"item": "object"}, "idempotency": "natural"},
        {"capability_id": "classify_item", "name": "Классифицировать элемент моделью", "kind": "llm_step", "effect": "read", "human_gate_required": False, "touches_entities": ["Item"], "idempotency": "natural"},
        {"capability_id": "prepare_item", "name": "Подготовить элемент", "kind": "function", "effect": "write_reversible", "human_gate_required": False, "touches_entities": ["Item"], "inputs": {"item_id": "string", "title": "string"}, "idempotency": "natural", "constraints": ["C002"]},
        {"capability_id": "confirm_item", "name": "Подтвердить готовность", "kind": "ui_action", "effect": "write_reversible", "human_gate_required": False, "touches_entities": ["Item"], "idempotency": "natural"},
        {"capability_id": "publish_item", "name": "Опубликовать элемент наружу", "kind": "external_call", "effect": "write_irreversible", "human_gate_required": True, "touches_entities": ["Item"], "inputs": {"item_id": "string"}, "external_side_effects": True, "idempotency": "key", "constraints": ["C001"]},
        {"capability_id": "once_action", "name": "Неидемпотентное действие", "kind": "function", "effect": "write_reversible", "human_gate_required": False, "touches_entities": ["Item"], "idempotency": "none"},
        {"capability_id": "risky_action", "name": "Действие без реализации", "kind": "function", "effect": "write_reversible", "human_gate_required": False, "touches_entities": ["Item"], "idempotency": "natural"},
        {"capability_id": "migrate_schema", "name": "Миграция самой системы", "kind": "cli", "effect": "write_reversible", "human_gate_required": True, "system_level": True, "touches_entities": ["Item"], "idempotency": "natural"},
    ],
}

PROCESSES: dict[str, Any] = {
    "system_id": "demo_system",
    "processes": [
        {
            "process_id": "item_flow",
            "name": "Путь элемента до публикации",
            "status": "active",
            "trigger": {"kind": "human_request", "description": "Оператор просит провести элемент."},
            "aggregate_entity": "Item",
            "steps": [
                {"step_id": "fetch", "name": "Прочитать элемент", "actor": "agent", "role": "bot", "capability_id": "read_item"},
                {"step_id": "classify", "name": "Классифицировать", "actor": "agent", "role": "bot", "capability_id": "classify_item"},
                {"step_id": "prepare", "name": "Подготовить", "actor": "agent", "role": "bot", "capability_id": "prepare_item", "on_failure": "retry", "transition": {"entity": "Item", "from": "draft", "to": "ready"}},
                {"step_id": "confirm", "name": "Подтвердить готовность", "actor": "human", "role": "operator", "capability_id": "confirm_item"},
                {
                    "step_id": "publish",
                    "name": "Опубликовать",
                    "actor": "agent",
                    "role": "bot",
                    "capability_id": "publish_item",
                    "human_gate": {"required": True, "reason": "constraint", "approver_role": "approver", "constraint_id": "C001", "condition": "Item.amount > 100"},
                    "transition": {"entity": "Item", "from": "ready", "to": "published"},
                    "emits": ["ItemPublished"],
                },
            ],
        },
        {
            "process_id": "once_flow",
            "name": "Неидемпотентный шаг с retry",
            "status": "active",
            "trigger": {"kind": "api_call", "description": "Вызов API."},
            "aggregate_entity": "Item",
            "steps": [{"step_id": "once", "name": "Сделать один раз", "actor": "agent", "role": "bot", "capability_id": "once_action", "on_failure": "retry"}],
        },
        {
            "process_id": "escalate_flow",
            "name": "Эскалация при ошибке",
            "status": "active",
            "trigger": {"kind": "api_call", "description": "Вызов API."},
            "aggregate_entity": "Item",
            "steps": [{"step_id": "once", "name": "Сделать с эскалацией", "actor": "agent", "role": "bot", "capability_id": "once_action", "on_failure": "escalate"}],
        },
        {
            "process_id": "risky_flow",
            "name": "Процесс с дырой и неподтверждённым ограничением",
            "status": "active",
            "trigger": {"kind": "schedule", "description": "По расписанию."},
            "aggregate_entity": "Item",
            "steps": [{"step_id": "do_risky", "name": "Рискованный шаг", "actor": "agent", "role": "bot", "capability_id": "risky_action"}],
        },
        {
            "process_id": "maintenance",
            "name": "Обслуживание системы",
            "status": "active",
            "trigger": {"kind": "human_request", "description": "Администратор просит мигрировать схему."},
            "aggregate_entity": "Item",
            "steps": [{"step_id": "migrate", "name": "Применить миграцию", "actor": "system", "role": "core", "capability_id": "migrate_schema", "human_gate": {"required": True, "reason": "policy", "approver_role": "approver"}}],
        },
    ],
}

IMPLEMENTATION: dict[str, Any] = {
    "system_id": "demo_system",
    "repository": {"name": "synthetic", "inspected_at": "2026-10-25"},
    "bindings": [
        {"capability_id": c, "status": "implemented", "kind": "function", "location": {"path": "demo/app.py", "symbol": c}, "evidence": "synthetic", "verified_at": "2026-10-25"}
        for c in ("read_item", "classify_item", "prepare_item", "confirm_item", "publish_item", "once_action", "migrate_schema")
    ]
    + [{"capability_id": "risky_action", "status": "planned", "kind": "function", "evidence": "synthetic: реализации нет"}],
}


def write_model(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, data in (("purpose", PURPOSE), ("domain", DOMAIN), ("processes", PROCESSES), ("capabilities", CAPABILITIES), ("implementation", IMPLEMENTATION)):
        (root / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return root
