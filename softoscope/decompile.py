"""Пять слоёв JSON → объектное описание на Python (softoscope.oop), без потерь.

Обратный ход к `oop.compile_namespace`: сгенерированный модуль компилируется ровно в те же
слои (проверяется тестом на всех примерах). Поля, которых описание не знает, уходят в
`extra`/`**{...}`, поэтому круг замыкается и на моделях с полями сверх схемы.
"""

from __future__ import annotations

import keyword
from typing import Any

from .oop import _ENTITY_RESERVED, _PROCESS_RESERVED, camel, snake

_TYPE_NAMES = {"string": "str", "integer": "int", "number": "float", "boolean": "bool", "json": "dict", "list": "list"}


def lit(v: Any, indent: int = 0) -> str:
    """Литерал Python: в строку, если помещается, иначе по элементу на строке."""
    one = repr(v)
    if len(one) + indent <= 110 or not isinstance(v, (list, dict)) or not v:
        return one
    pad = " " * (indent + 4)
    if isinstance(v, list):
        items = [lit(x, indent + 4) for x in v]
        return "[\n" + "".join(f"{pad}{x},\n" for x in items) + " " * indent + "]"
    items = [f"{k!r}: {lit(x, indent + 4)}" for k, x in v.items()]
    return "{\n" + "".join(f"{pad}{x},\n" for x in items) + " " * indent + "}"


class Names:
    """Уникальные имена Python в одном пространстве (модуль или тело класса)."""

    def __init__(self, reserved: set[str] = frozenset()):
        self.used = set(reserved)

    def take(self, want: str, fallback_suffix: str = "_") -> str:
        py = want if want.isidentifier() else "".join(ch if ch.isalnum() else "_" for ch in want)
        if not py or py[0].isdigit():
            py = "x_" + py
        while keyword.iskeyword(py) or py in self.used or py.startswith("__"):
            py += fallback_suffix
        self.used.add(py)
        return py


def _kw(pairs: list[tuple[str, Any]], extra: dict[str, Any] | None = None, indent: int = 4) -> str:
    parts = [f"{k}={v}" for k, v in pairs]
    if extra:
        parts.append(f"**{lit(extra, indent)}")
    return ", ".join(parts)


def _extra(d: dict[str, Any], known: set[str]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if k not in known}


def decompile(layers: dict[str, dict[str, Any]]) -> str:
    P, D, PR, C, I = (layers[k] for k in ("purpose", "domain", "processes", "capabilities", "implementation"))
    sid = P["system_id"]
    for name, layer in layers.items():
        if layer.get("system_id") != sid:
            raise ValueError(f"{name}: system_id {layer.get('system_id')!r} ≠ {sid!r}")
    names = Names({"system", "bindings"} | {n for n in _IMPORTS})
    out: list[str] = []
    w = out.append

    w(f'"""Система {sid} в объектной форме (softoscope.oop).\n\nСгенерировано `tools/digitize.py decompile`; компилируется обратно в те же пять слоёв:\n    python3 tools/digitize.py build <этот файл> --out <каталог>\n"""\n')
    w("from softoscope.oop import " + ", ".join(_IMPORTS) + "\n")

    # ------------------------------------------------------------------ System
    known_p = {"system_id", "spec_version", "purpose", "goals", "non_goals", "roles", "hard_constraints"}
    if _extra(P, known_p):
        raise ValueError(f"purpose.json: неизвестные поля {sorted(_extra(P, known_p))}")
    sys_kw: list[tuple[str, Any]] = []
    if P.get("goals"):
        goals = []
        for n, g in enumerate(P["goals"], 1):
            args = [repr(g["statement"])]
            if g.get("metric") is not None:
                args.append(f"metric={g['metric']!r}")
            if g["id"] != f"G{n:03d}":
                args.append(f"id={g['id']!r}")
            goals.append(f"Goal({', '.join(args)})")
        sys_kw.append(("goals", "[\n        " + ",\n        ".join(goals) + ",\n    ]"))
    elif "goals" in P:
        raise ValueError("purpose.json: пустой goals не выражается описанием")
    if "non_goals" in P:
        sys_kw.append(("non_goals", lit(P["non_goals"], 4)))
    if "imports" in D:
        sys_kw.append(("imports", lit(D["imports"], 4)))
    known_i = {"system_id", "repository", "bindings", "storage", "event_sinks", "checks"}
    if _extra(I, known_i):
        raise ValueError(f"implementation.json: неизвестные поля {sorted(_extra(I, known_i))}")
    sys_kw.append(("repository", lit(I["repository"], 4)))
    for k in ("storage", "event_sinks", "checks"):
        if k in I:
            sys_kw.append((k, lit(I[k], 4)))
    w("system = System(\n    " + ",\n    ".join([repr(sid), repr(P["spec_version"]), repr(P["purpose"])] + [f"{k}={v}" for k, v in sys_kw]) + ",\n)\n")

    # ------------------------------------------------------------------- роли
    role_var: dict[str, str] = {}
    w("# --- слой 1: роли")
    for r in P["roles"]:
        v = names.take(r["id"])
        role_var[r["id"]] = v
        kws: list[tuple[str, Any]] = []
        if "may_approve" in r:
            kws.append(("may_approve", repr(r["may_approve"])))
        if v != r["id"]:
            kws.append(("id", repr(r["id"])))
        extra = _extra(r, {"id", "kind", "description", "may_approve"})
        args = _kw(kws, extra)
        head = f"Role.{r['kind']}(" if r["kind"] in ("human", "agent", "system") else f"Role({r['kind']!r}, "
        w(f"{v} = {head}{r['description']!r}{', ' + args if args else ''})")
    w("")

    # ------------------------------------------------------------- ограничения
    cons_var: dict[str, str] = {}
    w("# --- слой 1: жёсткие ограничения")
    for c in P["hard_constraints"]:
        v = names.take(c["id"])
        cons_var[c["id"]] = v
        kws = [("severity", repr(c["severity"])), ("enforcement", repr(c["enforcement"]))]
        at = c.get("applies_to")
        extra = _extra(c, {"id", "rule", "severity", "enforcement", "applies_to", "status", "threshold", "rationale", "source"})
        if at is not None:
            unknown = _extra(at, {"entities", "capabilities", "processes"})
            if not at:
                kws.append(("applies_to", "{}"))
            elif unknown:
                extra["applies_to"] = at
            else:
                for k in ("entities", "capabilities", "processes"):
                    if k in at:
                        kws.append((k, repr(at[k])))
        for k in ("status", "rationale", "source"):
            if k in c:
                kws.append((k, repr(c[k])))
        if "threshold" in c:
            th = c["threshold"]
            if set(th) <= {"attribute", "operator", "value", "unit"}:
                kws.append(("threshold", repr(tuple(th[k] for k in ("attribute", "operator", "value", "unit") if k in th))))
            else:
                extra["threshold"] = th
        if v != c["id"]:
            kws.append(("id", repr(c["id"])))
        w(f"{v} = Constraint(\n    {c['rule']!r},\n    {_kw(kws, extra)},\n)")
    w("")

    def ref(table: dict[str, str], key: Any) -> str:
        return table[key] if isinstance(key, str) and key in table else repr(key)

    def refs(table: dict[str, str], keys: list[Any]) -> str:
        return "[" + ", ".join(ref(table, k) for k in keys) + "]"

    # --------------------------------------------------------------- сущности
    ent_var: dict[str, str] = {}
    ent_states: dict[str, dict[str, str]] = {}
    w("# --- слой 2: сущности")
    for ename, e in D["entities"].items():
        v = names.take(ename)
        ent_var[ename] = v
        body: list[str] = []
        members = Names(_ENTITY_RESERVED | {"to", "compile", "ident"})
        if v != ename:
            body.append(f"entity_name = {ename!r}")
        desc = e["description"]
        body.append(f"description = {desc!r}" if ('"""' in desc or "\\" in desc or desc != " ".join(desc.split())) else f'"""{desc}"""')
        attrs = e["attributes"]
        identity = e["identity"]
        if identity != "id":
            body.append(f"identity = {identity!r}")
        for a in attrs:
            an = members.take(a["name"])
            t = a["type"]
            args = [_TYPE_NAMES.get(t, repr(t))]
            for k in ("required", "description", "enum", "ref", "sensitive"):
                if k in a:
                    args.append(f"{k}={a[k]!r}")
            if an != a["name"]:
                args.append(f"name={a['name']!r}")
            ex = _extra(a, {"name", "type", "required", "description", "enum", "ref", "sensitive"})
            if ex:
                args.append(f"**{ex!r}")
            body.append(f"{an} = Attr({', '.join(args)})")
        states = e.get("states")
        initial = e.get("initial_state")
        terminal = e.get("terminal_states")
        use_states = (
            states is not None
            and len(set(states)) == len(states)
            and (initial is None or (initial in states and states))
            and (terminal is None or (terminal and set(terminal) <= set(states) and [s for s in states if s in terminal] == terminal))
        )
        svars: dict[str, str] = {}
        if use_states:
            for s in states:
                sv = members.take(s, "_state")
                svars[s] = sv
                args = []
                if s == initial:
                    args.append("initial=True")
                if terminal and s in terminal:
                    args.append("terminal=True")
                if sv != s:
                    args.append(f"name={s!r}")
                body.append(f"{sv} = State({', '.join(args)})")
        else:
            for k in ("states", "initial_state", "terminal_states"):
                if k in e:
                    body.append(f"{k} = {e[k]!r}")
        ent_states[ename] = svars
        if "derived_state" in e:
            body.append(f"derived_state = {lit(e['derived_state'], 4)}")
        if e.get("transitions"):
            trs = []
            for t in e["transitions"]:
                kws = []
                for jk, pk in (("actor_kind", "actor"), ("reversible", "reversible"), ("trigger", "trigger")):
                    if jk in t:
                        kws.append(f"{pk}={t[jk]!r}")
                if "guard" in t:
                    kws.append(f"guard={ref(cons_var, t['guard'])}")
                ex = _extra(t, {"from", "to", "actor_kind", "reversible", "trigger", "guard"})
                if ex:
                    kws.append(f"**{ex!r}")
                frm, to = t.get("from"), t["to"]
                if "from" in t and frm in svars and to in svars:
                    trs.append(f"{svars[frm]}.to({svars[to]}{', ' if kws else ''}{', '.join(kws)})")
                else:
                    frm_s = svars.get(frm, repr(frm)) if "from" in t else "None"
                    trs.append(f"Transition({frm_s}, {svars.get(to, repr(to))}{', ' if kws else ''}{', '.join(kws)})")
            body.append("transitions = [\n        " + ",\n        ".join(trs) + ",\n    ]")
        elif "transitions" in e:
            raise ValueError(f"{ename}: пустой transitions не выражается описанием")
        if e.get("relations"):
            rels = []
            for r in e["relations"]:
                args = [repr(r["name"]), repr(r["target"]), repr(r["cardinality"])]
                if "description" in r:
                    args.append(repr(r["description"]))
                if _extra(r, {"name", "target", "cardinality", "description"}):
                    raise ValueError(f"{ename}.relations: неизвестные поля")
                rels.append(f"Relation({', '.join(args)})")
            body.append("relations = [\n        " + ",\n        ".join(rels) + ",\n    ]")
        elif "relations" in e:
            raise ValueError(f"{ename}: пустой relations не выражается описанием")
        if "invariants" in e:
            body.append(f"invariants = {lit(e['invariants'], 4)}")
        ex = _extra(e, {"description", "identity", "attributes", "states", "initial_state", "terminal_states", "derived_state", "transitions", "relations", "invariants"})
        if ex:
            body.append(f"extra = {lit(ex, 4)}")
        w(f"\n\nclass {v}(Entity):\n    " + "\n    ".join(body))
    w("")

    # ---------------------------------------------------------------- события
    ev_var: dict[str, str] = {}
    if D.get("domain_events"):
        w("\n# --- слой 2: доменные события")
        for ev in D["domain_events"]:
            v = names.take(ev["name"])
            ev_var[ev["name"]] = v
            kws = []
            if v != ev["name"]:
                kws.append(("id", repr(ev["name"])))
            extra = _extra(ev, {"name", "entity", "description"})
            args = _kw(kws, extra)
            w(f"{v} = Event({ref(ent_var, ev['entity'])}, {ev['description']!r}{', ' + args if args else ''})")
        w("")
    elif "domain_events" in D:
        raise ValueError("domain.json: пустой domain_events не выражается описанием")
    known_d = {"system_id", "entities", "domain_events", "imports"}
    if _extra(D, known_d):
        raise ValueError(f"domain.json: неизвестные поля {sorted(_extra(D, known_d))}")

    # ------------------------------------------------------------- возможности
    cap_var: dict[str, str] = {}
    cap_kind: dict[str, str] = {}
    w("\n# --- слой 4: возможности")
    for c in C["capabilities"]:
        v = names.take(c["capability_id"])
        cap_var[c["capability_id"]] = v
        cap_kind[c["capability_id"]] = c["kind"]
        kws = [("kind", repr(c["kind"])), ("effect", repr(c["effect"]))]
        default_gate = c["effect"] == "write_irreversible" or bool(c.get("system_level"))
        if c["human_gate_required"] != default_gate:
            kws.append(("human_gate_required", repr(c["human_gate_required"])))
        for k in ("description",):
            if k in c:
                kws.append((k, repr(c[k])))
        if "compensation" in c:
            kws.append(("compensation", repr(c["compensation"])))
        if "allowed_roles" in c:
            kws.append(("allowed_roles", refs(role_var, c["allowed_roles"])))
        if "touches_entities" in c:
            kws.append(("touches", refs(ent_var, c["touches_entities"])))
        for k in ("inputs", "outputs", "preconditions", "idempotency", "external_side_effects"):
            if k in c:
                kws.append((k, lit(c[k], 4)))
        if "constraints" in c:
            kws.append(("constraints", refs(cons_var, c["constraints"])))
        if "system_level" in c:
            kws.append(("system_level", repr(c["system_level"])))
        if v != c["capability_id"]:
            kws.append(("id", repr(c["capability_id"])))
        extra = _extra(c, {"capability_id", "name", "description", "kind", "effect", "compensation", "human_gate_required", "allowed_roles", "touches_entities", "inputs", "outputs", "preconditions", "idempotency", "external_side_effects", "constraints", "system_level"})
        w(f"{v} = Capability(\n    {c['name']!r},\n    " + _kw(kws, extra) + ",\n)")
    known_c = {"system_id", "capabilities"}
    if _extra(C, known_c):
        raise ValueError(f"capabilities.json: неизвестные поля {sorted(_extra(C, known_c))}")
    w("")

    # ---------------------------------------------------------------- процессы
    w("\n# --- слой 3: процессы")
    for p in PR["processes"]:
        want = camel(p["process_id"])
        v = names.take(want if want not in names.used else want + "Process")
        body = []
        if snake(v) != p["process_id"]:
            body.append(f"process_id = {p['process_id']!r}")
        if "description" in p:
            d = p["description"]
            body.append(f"description = {d!r}")
        body.append(f"name = {p['name']!r}")
        if "status" in p:
            body.append(f"status = {p['status']!r}")
        tr = p["trigger"]
        if _extra(tr, {"kind", "description", "event_type"}):
            raise ValueError(f"{p['process_id']}.trigger: неизвестные поля")
        targs = [repr(tr["description"])]
        if "event_type" in tr:
            targs.append(f"event_type={ref(ev_var, tr['event_type'])}")
        if tr["kind"] in ("human_request", "event", "schedule", "api_call", "agent_decision") and (tr["kind"] == "event" or "event_type" not in tr):
            body.append(f"trigger = Trigger.{tr['kind']}({', '.join(targs)})")
        else:
            body.append(f"trigger = Trigger({tr['kind']!r}, {', '.join(targs)})")
        body.append(f"aggregate = {ref(ent_var, p['aggregate_entity'])}")
        if "outcomes" in p:
            body.append(f"outcomes = {lit(p['outcomes'], 4)}")
        ex = _extra(p, {"process_id", "name", "description", "status", "trigger", "aggregate_entity", "steps", "outcomes"})
        if ex:
            body.append(f"extra = {lit(ex, 4)}")
        members = Names(_PROCESS_RESERVED | {"steps", "compile", "ident"})
        for s in p["steps"]:
            sv = members.take(s["step_id"])
            kws = []
            if "description" in s:
                kws.append(f"description={s['description']!r}")
            if "role" in s:
                kws.append(f"role={ref(role_var, s['role'])}")
            if "capability_id" in s:
                kws.append(f"capability={ref(cap_var, s['capability_id'])}")
            if "human_gate" in s:
                g = s["human_gate"]
                gk = []
                if g.get("required") is not True:
                    gk.append(f"required={g.get('required')!r}")
                reason_default = "constraint" if "constraint_id" in g else None
                if g.get("reason") != reason_default:
                    gk.append(f"reason={g.get('reason')!r}")
                if "approver_role" in g:
                    gk.append(f"approver={ref(role_var, g['approver_role'])}")
                if "constraint_id" in g:
                    gk.append(f"constraint={ref(cons_var, g['constraint_id'])}")
                for k in ("condition", "note"):
                    if k in g:
                        gk.append(f"{k}={g[k]!r}")
                gx = _extra(g, {"required", "reason", "approver_role", "constraint_id", "condition", "note"})
                if gx:
                    gk.append(f"**{gx!r}")
                if "required" not in g:
                    raise ValueError(f"{p['process_id']}.{s['step_id']}: human_gate без required")
                kws.append(f"gate=Gate({', '.join(gk)})")
            if "transition" in s:
                t = s["transition"]
                if _extra(t, {"entity", "from", "to"}):
                    raise ValueError(f"{p['process_id']}.{s['step_id']}: transition с лишними полями")
                ent = t["entity"]
                sv_map = ent_states.get(ent, {})
                ev = ent_var.get(ent)
                if ev and t["to"] in sv_map and ("from" not in t or t["from"] in sv_map):
                    if "from" in t:
                        kws.append(f"transition={ev}.{sv_map[t['from']]}.to({ev}.{sv_map[t['to']]})")
                    else:
                        kws.append(f"transition={ev}.{sv_map[t['to']]}")
                else:
                    frm = repr(t["from"]) if "from" in t else "None"
                    kws.append(f"transition=Transition({frm}, {t['to']!r}, entity={ent!r})")
            if "condition" in s:
                kws.append(f"condition={s['condition']!r}")
            if "next" in s:
                kws.append(f"next={s['next']!r}")
            if "on_failure" in s:
                kws.append(f"on_failure={s['on_failure']!r}")
            if "emits" in s:
                kws.append(f"emits={refs(ev_var, s['emits'])}")
            if sv != s["step_id"]:
                kws.append(f"id={s['step_id']!r}")
            sx = _extra(s, {"step_id", "name", "description", "actor", "role", "capability_id", "human_gate", "transition", "condition", "next", "on_failure", "emits"})
            if sx:
                kws.append(f"**{sx!r}")
            ctor = f"Step.{s['actor']}" if s["actor"] in ("agent", "human", "system") else "Step"
            first = repr(s["name"]) + ("" if ctor != "Step" else f", actor={s['actor']!r}")
            body.append(f"{sv} = {ctor}(\n        {first},\n        " + ",\n        ".join(kws) + ("," if kws else "") + "\n    )")
        w(f"\n\nclass {v}(Process):\n    " + "\n    ".join(body))
    known_pr = {"system_id", "processes"}
    if _extra(PR, known_pr):
        raise ValueError(f"processes.json: неизвестные поля {sorted(_extra(PR, known_pr))}")
    w("")

    # ---------------------------------------------------------------- привязки
    if I.get("bindings"):
        w("\n# --- слой 5: привязки к реализации")
        items = []
        for b in I["bindings"]:
            kws = [f"status={b['status']!r}"]
            if b["kind"] != cap_kind.get(b["capability_id"]):
                kws.append(f"kind={b['kind']!r}")
            kws.append(f"evidence={b['evidence']!r}")
            loc = b.get("location")
            if loc is not None:
                if loc and not _extra(loc, {"path", "symbol", "route", "graph_node", "mcp", "command"}) and _route_ok(loc):
                    for k in ("path", "symbol", "graph_node", "command"):
                        if k in loc:
                            kws.append(f"{k}={loc[k]!r}")
                    if "route" in loc:
                        kws.append(f"route=({loc['route']['method']!r}, {loc['route']['path']!r})")
                    if "mcp" in loc:
                        kws.append(f"mcp=({loc['mcp']['server']!r}, {loc['mcp']['tool']!r})")
                else:
                    kws.append(f"location={loc!r}")
            for k in ("feature_flag", "verified_at", "notes"):
                if k in b:
                    kws.append(f"{k}={b[k]!r}")
            bx = _extra(b, {"capability_id", "status", "kind", "evidence", "location", "feature_flag", "verified_at", "notes"})
            if bx:
                kws.append(f"**{bx!r}")
            items.append(f"Binding({ref(cap_var, b['capability_id'])}, " + ", ".join(kws) + ")")
        w("bindings = [\n    " + ",\n    ".join(items) + ",\n]")
    return "\n".join(out) + "\n"


def _route_ok(loc: dict[str, Any]) -> bool:
    r, m = loc.get("route"), loc.get("mcp")
    return (r is None or set(r) == {"method", "path"}) and (m is None or set(m) == {"server", "tool"})


_IMPORTS = ["Attr", "Binding", "Capability", "Constraint", "Entity", "Event", "Gate", "Goal", "Process", "Relation", "Role", "State", "Step", "System", "Transition", "Trigger"]
