"""Объектное описание системы: классы и объекты Python → пять слоёв JSON (ADR-021).

Источник истины по-прежнему пять файлов слоёв (ADR-001): описание на Python — способ их
писать, а не замена. `tools/digitize.py build` компилирует модуль в JSON и прогоняет
валидатор, `--check` ловит расхождение описания и закоммиченных файлов, `decompile`
превращает готовую модель обратно в описание.

Модуль описания — это и есть система. Всё, что видно в его пространстве имён, входит в модель:

    system = System("demo_system", "0.1.0", "Назначение в два предложения.", as_of="2026-10-27")

    approver = Role.human("Утверждает выпуск.", may_approve=True)
    bot = Role.agent("Исполнитель.")

    C001 = Constraint("Дороже 100 — только с согласия.", severity="critical", enforcement="gate",
                      capabilities=["publish_item"], entities=["Item"], threshold=("Item.amount", ">", 100))

    class Item(Entity):
        '''Элемент, который готовят и выпускают.'''
        id = Attr(str, required=True)
        amount = Attr(float)
        draft = State(initial=True)
        ready = State()
        published = State(terminal=True)
        transitions = [draft.to(ready, actor="agent"), ready.to(published, actor="agent", reversible=False, guard=C001)]

    ItemPublished = Event(Item, "Элемент выпущен.")

    publish_item = Capability("Выпустить", kind="external_call", effect="write_irreversible",
                              touches=[Item], constraints=[C001])

    class ItemFlow(Process):          # process_id = item_flow
        '''Путь элемента до выпуска.'''
        name = "Путь элемента"
        trigger = Trigger.human_request("Оператор просит провести элемент.")
        aggregate = Item
        publish = Step.agent("Выпустить", role=bot, capability=publish_item,
                             gate=Gate(approver=approver, constraint=C001, condition=Item.amount > 100),
                             transition=Item.ready.to(Item.published), emits=[ItemPublished])

Что даёт объектная форма по сравнению с ручным JSON:
- ссылки — объекты, а не строки: опечатка в имени роли или возможности падает при импорте;
- наследование: абстрактная сущность или процесс (`abstract=True`) отдаёт атрибуты и шаги
  наследникам, наследник переопределяет шаг по имени;
- `Item.amount > 100` — порог и условие гейта из атрибута сущности, без строк руками;
- имена берутся из имён переменных, атрибутов и классов: `step_id`, `capability_id`, состояния.

Модуль ядра: ни одного слова предметной области, только правила описания.
"""

from __future__ import annotations

import itertools
import keyword
import re
from typing import Any, Iterable

LAYERS = ("purpose", "domain", "processes", "capabilities", "implementation")

_counter = itertools.count()

builtins_type = type  # в Attr имя type занято параметром

_PY_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean", dict: "json", list: "list"}


class DescriptionError(Exception):
    """Описание нельзя скомпилировать: нет системы, неизвестная ссылка, неверное имя."""


def snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def camel(name: str) -> str:
    return "".join(p[:1].upper() + p[1:] for p in name.split("_") if p)


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    """Убрать незаданные поля: в слой попадает ровно то, что описано."""
    return {k: v for k, v in d.items() if v is not None}


def _ident(x: Any) -> Any:
    """Объект описания → его идентификатор в слоях; строки и числа — как есть."""
    if isinstance(x, type) and issubclass(x, (Entity, Process)):
        return x.ident()
    if isinstance(x, Decl):
        return x.ident()
    if isinstance(x, State):
        return x.name
    return x


def _plain(x: Any) -> Any:
    """Рекурсивно заменить объекты описания идентификаторами (для полей extra и словарей)."""
    if isinstance(x, Threshold):
        return x.as_dict()
    if isinstance(x, dict):
        return {k: _plain(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_plain(v) for v in x]
    return _ident(x)


def _ids(xs: Iterable[Any] | None) -> list[Any] | None:
    return None if xs is None else [_ident(x) for x in xs]


# ----------------------------------------------------------------------------- база


class Decl:
    """Объявление с идентификатором. Имя по умолчанию — имя переменной в модуле описания."""

    _kind = "объявление"

    def __init__(self, id: str | None = None, extra: dict[str, Any] | None = None):
        self.id = id
        self.extra = dict(extra or {})
        self._order = next(_counter)

    def ident(self) -> str:
        if not self.id:
            raise DescriptionError(f"{self._kind} без имени: присвойте его переменной модуля или передайте id=")
        return self.id

    def _with_extra(self, d: dict[str, Any]) -> dict[str, Any]:
        return _clean(d) | _plain(self.extra)


# ------------------------------------------------------------------------ слой 1


class System:
    """Слой 1 (шапка) и общие поля всех слоёв. В модуле описания — ровно один объект."""

    def __init__(
        self,
        system_id: str,
        spec_version: str,
        purpose: str,
        *,
        goals: Iterable[Goal | str] = (),
        non_goals: Iterable[str] | None = None,
        imports: list[dict[str, Any]] | None = None,
        repository: dict[str, Any] | None = None,
        storage: list[dict[str, Any]] | None = None,
        event_sinks: list[dict[str, Any]] | None = None,
        checks: list[dict[str, Any]] | None = None,
        as_of: str | None = None,
    ):
        self.system_id = system_id
        self.spec_version = spec_version
        self.purpose = purpose
        self.goals = [g if isinstance(g, Goal) else Goal(g) for g in goals]
        self.non_goals = list(non_goals) if non_goals is not None else None
        self.imports = imports
        self.repository = repository
        self.storage = storage
        self.event_sinks = event_sinks
        self.checks = checks
        self.as_of = as_of


class Goal:
    def __init__(self, statement: str, *, metric: str | None = None, id: str | None = None):
        self.statement, self.metric, self.id = statement, metric, id


class Role(Decl):
    _kind = "роль"

    def __init__(self, kind: str, description: str, *, may_approve: bool | None = None, id: str | None = None, **extra: Any):
        super().__init__(id, extra)
        self.kind, self.description, self.may_approve = kind, description, may_approve

    @classmethod
    def human(cls, description: str, **kw: Any) -> Role:
        return cls("human", description, **kw)

    @classmethod
    def agent(cls, description: str, **kw: Any) -> Role:
        return cls("agent", description, **kw)

    @classmethod
    def system(cls, description: str, **kw: Any) -> Role:
        return cls("system", description, **kw)

    def compile(self) -> dict[str, Any]:
        return self._with_extra({"id": self.ident(), "kind": self.kind, "description": self.description, "may_approve": self.may_approve})


class Threshold:
    """Машиночитаемый порог ограничения (ADR-010). Обычно получается из `Сущность.атрибут > значение`."""

    def __init__(self, attribute: str, operator: str, value: Any, unit: str | None = None):
        self.attribute, self.operator, self.value, self.unit = attribute, operator, value, unit

    def __str__(self) -> str:
        return f"{self.attribute} {self.operator} {self.value}"

    def per(self, unit: str) -> Threshold:
        return Threshold(self.attribute, self.operator, self.value, unit)

    def as_dict(self) -> dict[str, Any]:
        return _clean({"attribute": self.attribute, "operator": self.operator, "value": self.value, "unit": self.unit})


class Constraint(Decl):
    """Жёсткое ограничение слоя 1. Применимость — объектами или именами: entities, capabilities, processes."""

    _kind = "ограничение"

    def __init__(
        self,
        rule: str,
        *,
        severity: str,
        enforcement: str,
        applies_to: Iterable[Any] | dict[str, Any] = (),
        entities: Iterable[Any] | None = None,
        capabilities: Iterable[Any] | None = None,
        processes: Iterable[Any] | None = None,
        status: str | None = None,
        threshold: Threshold | tuple | dict | None = None,
        rationale: str | None = None,
        source: str | None = None,
        id: str | None = None,
        **extra: Any,
    ):
        super().__init__(id, extra)
        self.rule, self.severity, self.enforcement = rule, severity, enforcement
        self.status, self.rationale, self.source = status, rationale, source
        if isinstance(threshold, tuple):
            threshold = Threshold(*threshold)
        elif isinstance(threshold, dict):
            threshold = Threshold(**threshold)
        self.threshold = threshold
        self.targets: dict[str, list[Any]] | None = None
        buckets: dict[str, list[Any]] = {}
        explicit = isinstance(applies_to, dict)  # {} — ограничение применимо ко всему
        if explicit:
            buckets = {k: list(v) for k, v in applies_to.items()}
        else:
            for x in applies_to:
                buckets.setdefault(_bucket(x), []).append(x)
        for name, xs in (("entities", entities), ("capabilities", capabilities), ("processes", processes)):
            if xs is not None:
                buckets.setdefault(name, []).extend(xs)
        if explicit or buckets or entities is not None or capabilities is not None or processes is not None:
            self.targets = buckets

    def compile(self) -> dict[str, Any]:
        applies = None
        if self.targets is not None:
            applies = {k: [_ident(x) for x in self.targets[k]] for k in ("entities", "capabilities", "processes") if k in self.targets}
        return self._with_extra(
            {
                "id": self.ident(),
                "rule": self.rule,
                "severity": self.severity,
                "enforcement": self.enforcement,
                "applies_to": applies,
                "rationale": self.rationale,
                "source": self.source,
                "status": self.status,
                "threshold": self.threshold.as_dict() if self.threshold else None,
            }
        )


def _bucket(x: Any) -> str:
    if isinstance(x, type) and issubclass(x, Entity):
        return "entities"
    if isinstance(x, type) and issubclass(x, Process):
        return "processes"
    if isinstance(x, Capability):
        return "capabilities"
    raise DescriptionError(f"applies_to: {x!r} — передайте строку через entities=/capabilities=/processes=")


# ------------------------------------------------------------------------ слой 2


class _Member:
    """Член класса сущности или процесса: имя — из имени атрибута (`__set_name__`)."""

    def __init__(self, name: str | None):
        self.name = name
        self.owner: type | None = None
        self._order = next(_counter)

    def __set_name__(self, owner: type, attr: str) -> None:
        if self.name is None:
            self.name = attr
        if self.owner is None:
            self.owner = owner

    def __get__(self, obj: Any, owner: type) -> Any:
        if obj is not None:
            return self
        # доступ через класс-наследник: порог и переход ссылаются на наследника, а не на базу
        if owner is self.owner:
            return self
        bound = object.__new__(type(self))
        bound.__dict__.update(self.__dict__)
        bound.owner = owner
        return bound


class Attr(_Member):
    def __init__(
        self,
        type: Any = "string",
        *,
        required: bool | None = None,
        description: str | None = None,
        enum: list[Any] | None = None,
        ref: Any = None,
        sensitive: bool | None = None,
        identity: bool = False,
        name: str | None = None,
        **extra: Any,
    ):
        super().__init__(name)
        if isinstance(type, builtins_type) and issubclass(type, Entity):
            ref, type = ref or type, "ref"
        self.type = _PY_TYPES.get(type, type)
        self.required, self.description, self.enum = required, description, enum
        self.ref, self.sensitive, self.identity, self.extra = ref, sensitive, identity, extra

    def _ref(self) -> str:
        return f"{self.owner.ident() if self.owner else '?'}.{self.name}"

    def __gt__(self, v: Any) -> Threshold:
        return Threshold(self._ref(), ">", v)

    def __ge__(self, v: Any) -> Threshold:
        return Threshold(self._ref(), ">=", v)

    def __lt__(self, v: Any) -> Threshold:
        return Threshold(self._ref(), "<", v)

    def __le__(self, v: Any) -> Threshold:
        return Threshold(self._ref(), "<=", v)

    def eq(self, v: Any) -> Threshold:
        return Threshold(self._ref(), "==", v)

    def ne(self, v: Any) -> Threshold:
        return Threshold(self._ref(), "!=", v)

    def compile(self) -> dict[str, Any]:
        return _clean(
            {
                "name": self.name,
                "type": self.type,
                "required": self.required,
                "description": self.description,
                "enum": self.enum,
                "ref": _ident(self.ref),
                "sensitive": self.sensitive,
            }
        ) | _plain(self.extra)



class State(_Member):
    def __init__(self, *, initial: bool = False, terminal: bool = False, name: str | None = None):
        super().__init__(name)
        self.initial, self.terminal = initial, terminal

    def to(self, target: State | str, *, actor: str | None = None, reversible: bool | None = None, guard: Any = None, trigger: str | None = None, **extra: Any) -> Transition:
        return Transition(self, target, actor=actor, reversible=reversible, guard=guard, trigger=trigger, entity=self.owner, **extra)


class Transition:
    """Переход автомата сущности (в теле класса) или переход шага процесса (`Item.a.to(Item.b)`)."""

    def __init__(self, frm: Any, to: Any, *, actor: str | None = None, reversible: bool | None = None, guard: Any = None, trigger: str | None = None, entity: Any = None, **extra: Any):
        self.frm, self.to_, self.actor, self.reversible = frm, to, actor, reversible
        self.guard, self.trigger, self.entity, self.extra = guard, trigger, entity, extra

    def compile(self) -> dict[str, Any]:
        return _clean(
            {
                "from": _ident(self.frm),
                "to": _ident(self.to_),
                "trigger": self.trigger,
                "actor_kind": self.actor,
                "reversible": self.reversible,
                "guard": _ident(self.guard),
            }
        ) | _plain(self.extra)

    def step_ref(self) -> dict[str, Any]:
        entity = self.entity or getattr(self.to_, "owner", None)
        if entity is None:
            raise DescriptionError("переход шага без сущности: используйте Сущность.состояние.to(Сущность.состояние)")
        return _clean({"entity": _ident(entity), "from": _ident(self.frm), "to": _ident(self.to_)})


class Relation:
    def __init__(self, name: str, target: Any, cardinality: str, description: str | None = None):
        self.name, self.target, self.cardinality, self.description = name, target, cardinality, description

    def compile(self) -> dict[str, Any]:
        return _clean({"name": self.name, "target": _ident(self.target), "cardinality": self.cardinality, "description": self.description})


_ENTITY_RESERVED = {"description", "identity", "transitions", "relations", "invariants", "derived_state", "terminal_states", "states", "initial_state", "entity_name", "extra"}


class Entity:
    """Сущность слоя 2. Подкласс = сущность; `abstract=True` — только база для наследования.

    Описание — докстринг или `description`; атрибуты — `Attr`, состояния — `State`,
    переходы — список `transitions`; атрибуты и состояния наследуются по MRO.
    """

    entity_name: str | None = None
    description: str | None = None
    identity: str | None = None
    transitions: list[Transition] = []
    relations: list[Relation] = []
    invariants: list[str] | None = None
    derived_state: Any = None
    terminal_states: list[str] | None = None
    states: list[str] | None = None
    initial_state: str | None = None
    extra: dict[str, Any] = {}
    _abstract = True

    def __init_subclass__(cls, abstract: bool = False, **kw: Any) -> None:
        super().__init_subclass__(**kw)
        cls._abstract = abstract
        cls._order = next(_counter)

    @classmethod
    def ident(cls) -> str:
        return cls.entity_name or cls.__name__

    @classmethod
    def _members(cls, kind: type) -> list[Any]:
        seen: dict[str, Any] = {}
        for klass in reversed(cls.__mro__):
            for attr, v in vars(klass).items():
                if isinstance(v, kind):
                    seen.pop(v.name or attr, None)
                    seen[v.name or attr] = v
        return list(seen.values())

    @classmethod
    def compile(cls) -> dict[str, Any]:
        attrs = cls._members(Attr)
        states = cls._members(State)
        identity = cls.identity or next((a.name for a in attrs if a.identity), None) or "id"
        out: dict[str, Any] = {
            "description": cls.description or _doc(cls),
            "identity": identity,
            "attributes": [a.compile() for a in attrs],
        }
        if not out["description"]:
            raise DescriptionError(f"сущность {cls.ident()}: нет описания (докстринг или description)")
        if states:
            out["states"] = [s.name for s in states]
            initial = [s.name for s in states if s.initial]
            if initial:
                out["initial_state"] = initial[0]
            terminal = [s.name for s in states if s.terminal]
            if terminal:
                out["terminal_states"] = terminal
        if cls.states is not None:
            out["states"] = list(cls.states)
        if cls.initial_state is not None:
            out["initial_state"] = cls.initial_state
        if cls.terminal_states is not None:
            out["terminal_states"] = list(cls.terminal_states)
        if cls.derived_state is not None:
            out["derived_state"] = _plain(cls.derived_state)
        if cls.transitions:
            out["transitions"] = [t.compile() for t in cls.transitions]
        if cls.relations:
            out["relations"] = [r.compile() for r in cls.relations]
        if cls.invariants is not None:
            out["invariants"] = list(cls.invariants)
        return out | _plain(cls.extra)


def _doc(obj: Any) -> str | None:
    d = obj.__dict__.get("__doc__")
    return " ".join(d.split()) if d else None


class Event(Decl):
    """Доменное событие слоя 2: `ItemPublished = Event(Item, "описание")`."""

    _kind = "событие"

    def __init__(self, entity: Any, description: str, *, id: str | None = None, **extra: Any):
        super().__init__(id, extra)
        self.entity, self.description = entity, description

    def compile(self) -> dict[str, Any]:
        return self._with_extra({"name": self.ident(), "entity": _ident(self.entity), "description": self.description})


# ------------------------------------------------------------------------ слой 4


class Capability(Decl):
    """Возможность слоя 4. `human_gate_required` по умолчанию — необратимое или system_level."""

    _kind = "возможность"

    def __init__(
        self,
        name: str,
        *,
        kind: str,
        effect: str,
        human_gate_required: bool | None = None,
        description: str | None = None,
        compensation: Any = None,
        allowed_roles: Iterable[Any] | None = None,
        touches: Iterable[Any] | None = None,
        inputs: dict[str, Any] | None = None,
        outputs: dict[str, Any] | None = None,
        preconditions: list[str] | None = None,
        idempotency: str | None = None,
        external_side_effects: bool | None = None,
        constraints: Iterable[Any] | None = None,
        system_level: bool | None = None,
        binding: Binding | None = None,
        id: str | None = None,
        **extra: Any,
    ):
        super().__init__(id, extra)
        self.name, self.kind, self.effect, self.description = name, kind, effect, description
        if human_gate_required is None:
            human_gate_required = effect == "write_irreversible" or bool(system_level)
        self.human_gate_required = human_gate_required
        self.compensation = compensation
        self.allowed_roles = list(allowed_roles) if allowed_roles is not None else None
        self.touches = list(touches) if touches is not None else None
        self.inputs, self.outputs, self.preconditions = inputs, outputs, preconditions
        self.idempotency, self.external_side_effects = idempotency, external_side_effects
        self.constraints = list(constraints) if constraints is not None else None
        self.system_level = system_level
        if binding is not None:
            binding.capability = self
        self.binding = binding

    def compile(self) -> dict[str, Any]:
        comp = self.compensation
        if isinstance(comp, Capability):
            comp = {"capability_id": comp.ident()}
        return self._with_extra(
            {
                "capability_id": self.ident(),
                "name": self.name,
                "description": self.description,
                "kind": self.kind,
                "effect": self.effect,
                "compensation": _plain(comp),
                "human_gate_required": self.human_gate_required,
                "allowed_roles": _ids(self.allowed_roles),
                "touches_entities": _ids(self.touches),
                "inputs": self.inputs,
                "outputs": self.outputs,
                "preconditions": self.preconditions,
                "idempotency": self.idempotency,
                "external_side_effects": self.external_side_effects,
                "constraints": _ids(self.constraints),
                "system_level": self.system_level,
            }
        )


# ------------------------------------------------------------------------ слой 3


class Trigger:
    def __init__(self, kind: str, description: str, *, event_type: Any = None):
        self.kind, self.description, self.event_type = kind, description, event_type

    @classmethod
    def human_request(cls, description: str) -> Trigger:
        return cls("human_request", description)

    @classmethod
    def event(cls, description: str, event_type: Any = None) -> Trigger:
        return cls("event", description, event_type=event_type)

    @classmethod
    def schedule(cls, description: str) -> Trigger:
        return cls("schedule", description)

    @classmethod
    def api_call(cls, description: str) -> Trigger:
        return cls("api_call", description)

    @classmethod
    def agent_decision(cls, description: str) -> Trigger:
        return cls("agent_decision", description)

    def compile(self) -> dict[str, Any]:
        return _clean({"kind": self.kind, "description": self.description, "event_type": _ident(self.event_type)})


class Gate:
    """Человеческий гейт шага. `condition` — строка или порог (`Item.amount > 100`)."""

    def __init__(self, *, required: bool = True, reason: str | None = None, approver: Any = None, constraint: Any = None, condition: Any = None, note: str | None = None, **extra: Any):
        if reason is None and constraint is not None:
            reason = "constraint"
        self.required, self.reason, self.approver, self.constraint = required, reason, approver, constraint
        self.condition, self.note, self.extra = condition, note, extra

    def compile(self) -> dict[str, Any]:
        return _clean(
            {
                "required": self.required,
                "reason": self.reason,
                "approver_role": _ident(self.approver),
                "constraint_id": _ident(self.constraint),
                "note": self.note,
                "condition": str(self.condition) if self.condition is not None else None,
            }
        ) | _plain(self.extra)


class Step(_Member):
    """Шаг процесса. `step_id` — имя атрибута класса процесса (или `id=`, если имя занято)."""

    def __init__(
        self,
        name: str,
        *,
        actor: str,
        role: Any = None,
        capability: Any = None,
        gate: Gate | None = None,
        transition: Transition | State | None = None,
        condition: str | None = None,
        next: Iterable[Any] | None = None,
        on_failure: str | None = None,
        emits: Iterable[Any] | None = None,
        description: str | None = None,
        id: str | None = None,
        **extra: Any,
    ):
        super().__init__(id)
        self.title, self.actor, self.role, self.capability = name, actor, role, capability
        self.gate, self.transition, self.condition = gate, transition, condition
        self.next = list(next) if next is not None else None
        self.on_failure, self.description, self.extra = on_failure, description, extra
        self.emits = list(emits) if emits is not None else None

    @classmethod
    def agent(cls, name: str, **kw: Any) -> Step:
        return cls(name, actor="agent", **kw)

    @classmethod
    def human(cls, name: str, **kw: Any) -> Step:
        return cls(name, actor="human", **kw)

    @classmethod
    def system(cls, name: str, **kw: Any) -> Step:
        return cls(name, actor="system", **kw)

    def compile(self) -> dict[str, Any]:
        tr = self.transition
        if isinstance(tr, State):
            tr = Transition(None, tr, entity=tr.owner)
        return _clean(
            {
                "step_id": self.name,
                "name": self.title,
                "description": self.description,
                "actor": self.actor,
                "role": _ident(self.role),
                "capability_id": _ident(self.capability),
                "human_gate": self.gate.compile() if self.gate else None,
                "transition": tr.step_ref() if tr else None,
                "condition": self.condition,
                "next": [x.name if isinstance(x, Step) else x for x in self.next] if self.next is not None else None,
                "on_failure": self.on_failure,
                "emits": _ids(self.emits),
            }
        ) | _plain(self.extra)


_PROCESS_RESERVED = {"process_id", "name", "description", "status", "trigger", "aggregate", "outcomes", "extra"}


class Process:
    """Процесс слоя 3. Подкласс = процесс; шаги — атрибуты `Step` в порядке объявления,
    шаги базового класса идут первыми, наследник заменяет шаг с тем же именем на месте."""

    process_id: str | None = None
    name: str | None = None
    description: str | None = None
    status: str | None = None
    trigger: Trigger | None = None
    aggregate: Any = None
    outcomes: list[Any] | None = None
    extra: dict[str, Any] = {}
    _abstract = True

    def __init_subclass__(cls, abstract: bool = False, **kw: Any) -> None:
        super().__init_subclass__(**kw)
        cls._abstract = abstract
        cls._order = next(_counter)

    @classmethod
    def ident(cls) -> str:
        return cls.process_id or snake(cls.__name__)

    @classmethod
    def steps(cls) -> list[Step]:
        seen: dict[str, Step] = {}
        for klass in reversed(cls.__mro__):
            for attr, v in vars(klass).items():
                if isinstance(v, Step):
                    seen[v.name or attr] = v  # наследник заменяет шаг базы на его месте
        return list(seen.values())

    @classmethod
    def compile(cls) -> dict[str, Any]:
        for field in ("name", "trigger", "aggregate"):
            if getattr(cls, field) is None:
                raise DescriptionError(f"процесс {cls.ident()}: не задано {field}")
        return _clean(
            {
                "process_id": cls.ident(),
                "name": cls.name,
                "description": cls.description or _doc(cls),
                "status": cls.status,
                "trigger": cls.trigger.compile(),
                "aggregate_entity": _ident(cls.aggregate),
                "steps": [s.compile() for s in cls.steps()],
                "outcomes": _plain(cls.outcomes),
            }
        ) | _plain(cls.extra)


# ------------------------------------------------------------------------ слой 5


class Binding(Decl):
    """Привязка слоя 5. Объявляется в модуле (порядок сохраняется) или через `Capability(binding=...)`."""

    _kind = "привязка"

    def __init__(
        self,
        capability: Any = None,
        *,
        status: str,
        kind: str | None = None,
        evidence: str,
        path: str | None = None,
        symbol: str | None = None,
        route: tuple[str, str] | dict[str, str] | None = None,
        graph_node: str | None = None,
        mcp: tuple[str, str] | dict[str, str] | None = None,
        command: str | None = None,
        location: dict[str, Any] | None = None,
        feature_flag: str | None = None,
        verified_at: str | None = None,
        notes: str | None = None,
        **extra: Any,
    ):
        super().__init__(None, extra)
        self.capability, self.status, self.kind, self.evidence = capability, status, kind, evidence
        if isinstance(route, tuple):
            route = {"method": route[0], "path": route[1]}
        if isinstance(mcp, tuple):
            mcp = {"server": mcp[0], "tool": mcp[1]}
        loc = _clean({"path": path, "symbol": symbol, "route": route, "graph_node": graph_node, "mcp": mcp, "command": command})
        self.location = location if location is not None else (loc or None)
        self.feature_flag, self.verified_at, self.notes = feature_flag, verified_at, notes

    def ident(self) -> str:
        return _ident(self.capability)

    def compile(self) -> dict[str, Any]:
        if self.capability is None:
            raise DescriptionError("привязка без возможности")
        kind = self.kind or (self.capability.kind if isinstance(self.capability, Capability) else None)
        if kind is None:
            raise DescriptionError(f"привязка {self.ident()}: не задан kind")
        return self._with_extra(
            {
                "capability_id": self.ident(),
                "status": self.status,
                "kind": kind,
                "location": self.location,
                "feature_flag": self.feature_flag,
                "evidence": self.evidence,
                "verified_at": self.verified_at,
                "notes": self.notes,
            }
        )


# -------------------------------------------------------------------- компиляция


class Description:
    """Собранное описание: всё, что найдено в пространстве имён модуля, по видам и в порядке объявления."""

    def __init__(self, namespace: dict[str, Any]):
        systems = [v for v in namespace.values() if isinstance(v, System)]
        if len(systems) != 1:
            raise DescriptionError(f"в описании должен быть ровно один System(...), найдено {len(systems)}")
        self.system: System = systems[0]
        for var, v in namespace.items():
            if isinstance(v, Decl) and not isinstance(v, Binding) and v.id is None:
                v.id = var
        values = list(namespace.values())
        for v in list(values):
            if isinstance(v, (list, tuple)):
                values.extend(x for x in v if isinstance(x, Decl))  # bindings = [Binding(...), ...]
        uniq = {id(v): v for v in values}.values()

        def of(kind: type) -> list[Any]:
            return sorted((v for v in uniq if isinstance(v, kind)), key=lambda v: v._order)

        def classes(base: type) -> list[Any]:
            found = [v for v in uniq if isinstance(v, type) and issubclass(v, base) and v is not base and not v._abstract]
            return sorted(found, key=lambda v: v._order)

        self.roles: list[Role] = of(Role)
        self.constraints: list[Constraint] = of(Constraint)
        self.entities: list[type[Entity]] = classes(Entity)
        self.events: list[Event] = of(Event)
        self.capabilities: list[Capability] = of(Capability)
        self.processes: list[type[Process]] = classes(Process)
        bindings = of(Binding)
        for c in self.capabilities:
            if c.binding is not None and c.binding not in bindings:
                bindings.append(c.binding)
        self.bindings: list[Binding] = bindings

    def compile(self) -> dict[str, dict[str, Any]]:
        s = self.system
        sid = {"system_id": s.system_id}
        goals = []
        for n, g in enumerate(s.goals, 1):
            goals.append(_clean({"id": g.id or f"G{n:03d}", "statement": g.statement, "metric": g.metric}))
        purpose = sid | _clean(
            {
                "spec_version": s.spec_version,
                "purpose": s.purpose,
                "goals": goals or None,
                "non_goals": s.non_goals,
                "roles": [r.compile() for r in self.roles],
                "hard_constraints": [c.compile() for c in self.constraints],
            }
        )
        entities: dict[str, Any] = {}
        for e in self.entities:
            if e.ident() in entities:
                raise DescriptionError(f"сущность {e.ident()} объявлена дважды")
            entities[e.ident()] = e.compile()
        domain = sid | _clean({"entities": entities, "domain_events": [e.compile() for e in self.events] or None, "imports": s.imports})
        processes = sid | {"processes": [p.compile() for p in self.processes]}
        capabilities = sid | {"capabilities": [c.compile() for c in self.capabilities]}
        repository = s.repository
        if repository is None:
            if not s.as_of:
                raise DescriptionError("нужен System(repository={...}) или System(as_of='ГГГГ-ММ-ДД') для слоя 5")
            repository = {"name": s.system_id, "inspected_at": s.as_of}
        implementation = sid | _clean(
            {
                "repository": repository,
                "bindings": [b.compile() for b in self.bindings],
                "storage": _plain(s.storage),
                "event_sinks": _plain(s.event_sinks),
                "checks": s.checks,
            }
        )
        return {"purpose": purpose, "domain": domain, "processes": processes, "capabilities": capabilities, "implementation": implementation}


def compile_namespace(namespace: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return Description(namespace).compile()


def safe_name(name: str, reserved: set[str] = frozenset()) -> str:
    """Имя Python для идентификатора слоя: ключевые и занятые слова получают подчёркивание."""
    py = name if name.isidentifier() else re.sub(r"\W", "_", name)
    if keyword.iskeyword(py) or py in reserved or py.startswith("_"):
        py += "_"
    return py
