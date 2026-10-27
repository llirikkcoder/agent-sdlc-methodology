"""Карта модели: сводка, процессы и автоматы сущностей в Markdown + Mermaid.

Строится только из пяти слоёв и дыр drift: ничего не домысливает. На схеме процесса видно
исполнителя каждого шага, возможность и её класс эффекта, гейт (ромб перед шагом, с
утверждающим и условием), переход сущности, события шага (⇒) и шаги, у которых нет implemented-привязки.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from .model import Model

EFFECT_SHORT = {"read": "чтение", "write_reversible": "обратимо", "write_compensable": "компенсируемо", "write_irreversible": "необратимо"}


def _q(text: Any) -> str:
    """Текст для подписи Mermaid в кавычках."""
    return str(text).replace('"', "#quot;").replace("\n", " ")


def _short(text: str, n: int = 60) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def summary(model: Model, holes: list[dict[str, Any]]) -> list[str]:
    caps = model.capabilities["capabilities"]
    binds = model.implementation.get("bindings", [])
    cons = list(model.constraints.values())
    by_effect = Counter(c["effect"] for c in caps)
    by_status = Counter(b["status"] for b in binds)
    states = sum(len(e.get("states", [])) for e in model.entities.values())
    lvl = Counter(h["level"] for h in holes)
    lines = [
        f"# Цифровая модель: {model.system_id} (spec {model.spec_version})",
        "",
        _short(model.purpose["purpose"], 400),
        "",
        "| Слой | Что описано |",
        "|---|---|",
        f"| 1. Назначение | ролей {len(model.roles)} (утверждают: {', '.join(r for r, v in model.roles.items() if v.get('may_approve')) or 'нет'}); ограничений {len(cons)}, из них не подтверждено {sum(1 for c in cons if c.get('status') == 'unconfirmed')} |",
        f"| 2. Домен | сущностей {len(model.entities)}, состояний {states}, доменных событий {len(model.domain_events)} |",
        f"| 3. Процессы | {len(model.procs)}: " + ", ".join(f"{k} {v}" for k, v in Counter(p.get('status', 'active') for p in model.procs.values()).items()) + f"; шагов {sum(len(p['steps']) for p in model.procs.values())} |",
        f"| 4. Возможности | {len(caps)}: " + ", ".join(f"{EFFECT_SHORT.get(k, k)} {v}" for k, v in by_effect.items()) + " |",
        f"| 5. Реализация | привязок {len(binds)}: " + (", ".join(f"{k} {v}" for k, v in by_status.items()) or "нет") + " |",
        f"| Drift | ERROR {lvl.get('ERROR', 0)}, WARN {lvl.get('WARN', 0)}, INFO {lvl.get('INFO', 0)} |",
        "",
        "| Процесс | Запуск | Шагов | Человек | Гейтов | Дыр | Статус |",
        "|---|---|---|---|---|---|---|",
    ]
    for pid, p in model.procs.items():
        gates = sum(1 for s in p["steps"] if model.gate_rule(s).required)
        human = sum(1 for s in p["steps"] if s["actor"] == "human")
        nh = sum(1 for h in holes if h["process_id"] == pid and h["level"] in ("ERROR", "WARN"))
        lines.append(f"| `{pid}` {p['name']} | {p['trigger']['kind']} | {len(p['steps'])} | {human} | {gates} | {nh} | {p.get('status', 'active')} |")
    return lines


def _process_body(model: Model, process_id: str, holes: list[dict[str, Any]], prefix: str = "") -> list[str]:
    """Узлы и рёбра схемы процесса; prefix разводит имена, когда процессов на схеме несколько."""
    p = model.procs[process_id]
    steps = p["steps"]
    hole_steps = {h["step_id"] for h in holes if h["process_id"] == process_id and h["step_id"] and h["level"] in ("ERROR", "WARN")}
    start, done = f"{prefix}start", f"{prefix}done"
    out = [f'    {start}(["{_q(p["trigger"]["kind"])}: {_q(_short(p["trigger"]["description"], 50))}"])']
    node: dict[str, str] = {}
    for s in steps:
        sid = s["step_id"]
        sn = f"{prefix}s_{sid}"
        cap = model.capability(s.get("capability_id"))
        label = f"{_q(s['name'])}<br/><small>{s['actor']}"
        if s.get("role"):
            label += f" · {_q(s['role'])}"
        if cap:
            label += f"<br/>{_q(cap['capability_id'])} · {EFFECT_SHORT.get(cap['effect'], cap['effect'])}"
        tr = s.get("transition")
        if tr:
            label += f"<br/>{_q(tr['entity'])}: {_q(tr.get('from', '*'))} → {_q(tr['to'])}"
        if s.get("emits"):
            label += f"<br/>⇒ {_q(', '.join(s['emits']))}"
        label += "</small>"
        shape = {"human": ('[/"', '"/]'), "system": ('[["', '"]]')}.get(s["actor"], ('["', '"]'))
        out.append(f"    {sn}{shape[0]}{label}{shape[1]}")
        node[sid] = sn
        rule = model.gate_rule(s)
        if rule.required:
            approver = rule.approver_role or next((r for r, v in model.roles.items() if v.get("may_approve")), "?")
            cond = (s.get("human_gate") or {}).get("condition")
            glabel = f"гейт: {_q(approver)}" + (f"<br/>{_q(cond)}" if cond else "") + f"<br/><small>{_q(', '.join(rule.reasons))}</small>"
            out.append(f'    {prefix}g_{sid}{{"{glabel}"}}')
            out.append(f"    {prefix}g_{sid} -->|утверждено| {sn}")
            node[sid] = f"{prefix}g_{sid}"
        if sid in hole_steps:
            out.append(f"    class {sn} hole")
    out.append(f'    {done}(["завершён"])')
    order = [s["step_id"] for s in steps]
    if steps:
        out.append(f"    {start} --> {node[order[0]]}")
    for i, s in enumerate(steps):
        sn = f"{prefix}s_{s['step_id']}"
        if s.get("terminal"):
            targets = []
        else:
            targets = s.get("next") or (order[i + 1 : i + 2] if i + 1 < len(order) else [])
        for t in targets:
            nxt = next((x for x in steps if x["step_id"] == t), None)
            cond = (nxt or {}).get("condition")
            edge = f" -->|{_q(_short(cond, 50))}| " if cond else " --> "
            out.append(f"    {sn}{edge}{node.get(t, prefix + 's_' + t)}")
        if not targets:
            out.append(f"    {sn} --> {done}")
        if s.get("on_failure") in ("escalate", "retry", "skip"):
            out.append(f"    {sn} -.->|отказ: {s['on_failure']}| {sn}")
    return out


HOLE_STYLE = "    classDef hole stroke:#c0392b,stroke-width:2px,stroke-dasharray:5 3"


def process_diagram(model: Model, process_id: str, holes: list[dict[str, Any]]) -> list[str]:
    return ["```mermaid", "flowchart TD", *_process_body(model, process_id, holes), HOLE_STYLE, "```"]


def group_diagram(model: Model, process_ids: list[str], holes: list[dict[str, Any]]) -> list[str]:
    """Несколько процессов на одной схеме: каждый в своей рамке subgraph (входы одной темы)."""
    out = ["```mermaid", "flowchart TD"]
    for n, pid in enumerate(process_ids, 1):
        out.append(f'    subgraph p{n}["{_q(model.procs[pid]["name"])}"]')
        out += ["  " + line for line in _process_body(model, pid, holes, prefix=f"p{n}_")]
        out.append("    end")
    return out + [HOLE_STYLE, "```"]


def entity_diagram(model: Model, name: str) -> list[str]:
    e = model.entities[name]
    if not e.get("states"):
        return []
    out = ["```mermaid", "stateDiagram-v2"]
    if e.get("initial_state"):
        out.append(f"    [*] --> {e['initial_state']}")
    for t in e.get("transitions", []):
        bits = [t.get("actor_kind", "?")]
        if t.get("reversible") is False:
            bits.append("необратимо")
        if t.get("guard"):
            bits.append(_short(t["guard"], 30).replace(":", " —"))
        out.append(f"    {t['from']} --> {t['to']} : {', '.join(bits)}")
    for s in e.get("terminal_states", []):
        out.append(f"    {s} --> [*]")
    out.append("```")
    return out


def render(model: Model, holes: list[dict[str, Any]], process_id: str | None = None) -> str:
    lines = summary(model, holes) if process_id is None else [f"# {model.system_id}: процесс {process_id}"]
    procs = [process_id] if process_id else list(model.procs)
    for pid in procs:
        p = model.procs[pid]
        lines += ["", f"## Процесс `{pid}` — {p['name']}", ""]
        if p.get("description"):
            lines += [_short(p["description"], 500), ""]
        lines.append(f"Агрегат: `{p['aggregate_entity']}`. Статус: {p.get('status', 'active')}. Рамка: штриховая — у шага нет implemented-привязки; ромб — человеческий гейт.")
        lines += [""] + process_diagram(model, pid, holes)
    if process_id is None:
        for name in model.entities:
            d = entity_diagram(model, name)
            if d:
                lines += ["", f"## Сущность `{name}`", "", _short(model.entities[name]["description"], 300), ""] + d
    return "\n".join(lines) + "\n"
