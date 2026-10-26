"""Пункт проката строительного оборудования в объектной форме (softoscope.oop).

Это источник для пяти файлов слоёв рядом. После правки:
    python3 tools/digitize.py build examples/equipment-rental/model.py --out examples/equipment-rental
`make check` сверяет описание и файлы (build --check).

Бизнес описан со слов, кода нет: все привязки слоя 5 — planned. Что показывает пример:
- наследование сущностей: общие атрибуты учёта — в абстрактной `Tracked`;
- наследование процессов: `DamagedReturn` берёт шаги `RentalReturn` и заменяет два из них;
- порог из атрибута: `Deposit.amount > 50000` — условие гейта C001 (ADR-010);
- компенсацию: принятый залог откатывается возвратом залога (write_compensable, ADR-011).
"""

from softoscope.oop import Attr, Binding, Capability, Constraint, Entity, Event, Gate, Goal, Process, Relation, Role, State, Step, System, Trigger

system = System(
    "equipment_rental",
    "0.1.0",
    "Сдавать строительное оборудование в аренду без двойных броней и без споров о залоге: "
    "агент бронирует, ведёт возврат и решает по залогу в пределах правил, человек выдаёт, принимает и утверждает крупные и спорные суммы.",
    goals=[
        Goal("Ни одна единица не забронирована дважды на одни даты.", metric="число пересекающихся броней = 0"),
        Goal("Залог крупнее порога и любое удержание проходят через менеджера.", metric="число возвратов > 50 000 ₽ и удержаний без решения менеджера = 0"),
        Goal("Каждая закрытая аренда имеет решение по залогу.", metric="доля закрытых аренд без возврата или удержания = 0"),
    ],
    non_goals=[
        "Система не ведёт бухгалтерию пункта проката.",
        "Система не ремонтирует оборудование: ремонт — отдельный процесс мастерской.",
    ],
    as_of="2026-10-27",
)

# --- слой 1: роли
manager = Role.human("Менеджер пункта: утверждает возврат крупного залога и любое удержание, разбирает эскалации.", may_approve=True)
clerk = Role.human("Сотрудник выдачи: принимает залог, выдаёт и принимает оборудование по акту.")
rental_agent = Role.agent("ИИ-агент проката: бронирует, ведёт возврат, оценивает повреждения по фото, предлагает решение по залогу.")
scheduler = Role.system("Учётная система пункта: статусы единиц и плановые проверки.")

# --- слой 1: жёсткие ограничения
C001 = Constraint(
    "Залог больше 50 000 ₽ возвращается только с согласия менеджера.",
    severity="critical",
    enforcement="gate",
    capabilities=["refund_deposit"],
    entities=["Deposit"],
    threshold=("Deposit.amount", ">", 50000, "RUB"),
    status="confirmed",
    source="регламент пункта проката, со слов владельца",
)
C002 = Constraint(
    "Оборудование выдаётся только по подписанному акту и после внесения залога.",
    severity="critical",
    enforcement="pre_check",
    capabilities=["issue_equipment"],
    status="confirmed",
)
C003 = Constraint(
    "Одна единица не бронируется на пересекающиеся даты.",
    severity="critical",
    enforcement="pre_check",
    capabilities=["reserve_equipment"],
    entities=["Equipment"],
    status="confirmed",
)
C004 = Constraint(
    "Удержание залога за повреждение — только с согласия менеджера, при любой сумме.",
    severity="critical",
    enforcement="gate",
    capabilities=["withhold_deposit"],
    status="confirmed",
    rationale="оценка повреждения моделью — подсказка, а не решение (ADR-012)",
)
C005 = Constraint(
    "Единица после ремонта возвращается в парк только после проверки мастером.",
    severity="high",
    enforcement="audit_only",
    entities=["Equipment"],
    status="unconfirmed",
    source="со слов сотрудника выдачи; в регламенте не найдено",
)
C006 = Constraint(
    "Закрытая аренда имеет решение по залогу: возврат или удержание.",
    severity="medium",
    enforcement="post_check",
    processes=["rental_return", "damaged_return"],
    status="confirmed",
)


# --- слой 2: сущности
class Tracked(Entity, abstract=True):
    """Общие атрибуты учёта: наследуются всеми сущностями пункта."""

    id = Attr(str, required=True)
    created_at = Attr("datetime")


class Equipment(Tracked):
    """Единица оборудования в парке пункта."""

    model_name = Attr(str, required=True)
    daily_rate = Attr(float, description="Цена суток аренды, ₽")
    available = State(initial=True)
    reserved = State()
    rented = State()
    inspection = State()
    repair = State()
    retired = State(terminal=True)
    transitions = [
        available.to(reserved, actor="agent", reversible=True),
        reserved.to(available, actor="agent", reversible=True),
        reserved.to(rented, actor="system"),
        rented.to(inspection, actor="system"),
        inspection.to(available, actor="agent"),
        inspection.to(repair, actor="agent"),
        repair.to(available, actor="human", guard=C005),
        available.to(retired, actor="human", reversible=False),
    ]


class Booking(Tracked):
    """Аренда: от запроса до закрытия с решением по залогу."""

    equipment_id = Attr(Equipment, required=True)
    renter_phone = Attr(str, sensitive=True)
    start_date = Attr("date", required=True)
    end_date = Attr("date", required=True)
    requested = State(initial=True)
    confirmed = State()
    issued = State()
    returned = State()
    closed = State(terminal=True)
    cancelled = State(terminal=True)
    transitions = [
        requested.to(confirmed, actor="agent"),
        requested.to(cancelled, actor="agent", reversible=False, guard=C003),
        confirmed.to(cancelled, actor="human", reversible=False),
        confirmed.to(issued, actor="human", guard=C002),
        issued.to(returned, actor="human"),
        returned.to(closed, actor="agent", reversible=False, guard=C006),
    ]
    relations = [Relation("equipment", "Equipment", "one"), Relation("deposit", "Deposit", "one"), Relation("inspection", "Inspection", "one")]
    invariants = ["даты брони одной единицы не пересекаются (C003)", "закрытая аренда имеет решение по залогу (C006)"]


class Deposit(Tracked):
    """Залог по аренде."""

    booking_id = Attr(Booking, required=True)
    amount = Attr(float, required=True, description="Сумма залога, ₽; порог C001 = 50 000")
    held = State(initial=True)
    refunded = State(terminal=True)
    withheld = State(terminal=True)
    transitions = [
        held.to(refunded, actor="agent", reversible=False, guard=C001),
        held.to(withheld, actor="agent", reversible=False, guard=C004),
    ]


class Inspection(Tracked):
    """Осмотр при возврате: фото, оценка повреждения моделью, вердикт."""

    booking_id = Attr(Booking, required=True)
    damage_score = Attr(float, description="0–1, оценка модели по фото")
    damage_cost = Attr(float, description="Оценка стоимости ремонта, ₽")
    verdict = Attr(str, enum=["ok", "damaged"])


# --- слой 2: доменные события
BookingConfirmed = Event(Booking, "Бронь подтверждена, единица зарезервирована.")
EquipmentIssued = Event(Booking, "Оборудование выдано по акту.")
DepositRefunded = Event(Deposit, "Залог возвращён.")
DepositWithheld = Event(Deposit, "Залог удержан за повреждение.")
EquipmentSentToRepair = Event(Equipment, "Единица отправлена в ремонт.")
OverdueFlagged = Event(Booking, "Аренда просрочена, менеджеру поставлена задача.")

# --- слой 4: возможности
check_availability = Capability(
    "Проверить, свободна ли единица на даты",
    kind="query",
    effect="read",
    touches=[Equipment, Booking],
    inputs={"equipment_id": "string", "start_date": "date", "end_date": "date"},
    outputs={"available": "boolean"},
    idempotency="natural",
)
reserve_equipment = Capability(
    "Зарезервировать единицу",
    kind="function",
    effect="write_reversible",
    touches=[Equipment],
    constraints=[C003],
    idempotency="key",
)
confirm_booking = Capability("Подтвердить бронь", kind="function", effect="write_reversible", touches=[Booking], idempotency="natural")
refund_deposit = Capability(
    "Вернуть залог",
    kind="external_call",
    effect="write_irreversible",
    touches=[Deposit],
    inputs={"deposit_id": "string", "amount": "number"},
    external_side_effects=True,
    idempotency="key",
    constraints=[C001],
)
take_deposit = Capability(
    "Принять залог",
    kind="ui_action",
    effect="write_compensable",
    compensation=refund_deposit,
    allowed_roles=[clerk],
    touches=[Deposit],
    idempotency="key",
)
issue_equipment = Capability("Выдать оборудование по акту", kind="ui_action", effect="write_reversible", allowed_roles=[clerk], touches=[Booking], constraints=[C002], idempotency="natural")
mark_rented = Capability("Отметить единицу как сданную", kind="function", effect="write_reversible", touches=[Equipment], idempotency="natural")
accept_return = Capability("Принять оборудование по акту", kind="ui_action", effect="write_reversible", allowed_roles=[clerk], touches=[Booking], idempotency="natural")
mark_inspection = Capability("Поставить единицу на осмотр", kind="function", effect="write_reversible", touches=[Equipment], idempotency="natural")
assess_damage = Capability(
    "Оценить повреждения по фото",
    kind="llm_step",
    effect="read",
    touches=[Inspection],
    outputs={"damage_score": "number", "damage_cost": "number", "verdict": "string"},
    idempotency="natural",
    description="Модель даёт оценку; решение по залогу принимает правило и человек (ADR-012).",
)
withhold_deposit = Capability("Удержать залог", kind="function", effect="write_irreversible", touches=[Deposit], constraints=[C004], idempotency="key")
return_to_fleet = Capability("Вернуть единицу в парк", kind="function", effect="write_reversible", touches=[Equipment], idempotency="natural")
send_to_repair = Capability("Отправить единицу в ремонт", kind="function", effect="write_reversible", touches=[Equipment], idempotency="natural")
close_booking = Capability("Закрыть аренду", kind="function", effect="write_reversible", touches=[Booking], idempotency="natural")
list_overdue = Capability("Найти просроченные аренды", kind="query", effect="read", touches=[Booking], idempotency="natural")
flag_overdue = Capability("Поставить менеджеру задачу по просрочке", kind="function", effect="write_reversible", touches=[Booking], idempotency="key")
finish_repair = Capability("Принять единицу из ремонта", kind="ui_action", effect="write_reversible", touches=[Equipment], idempotency="natural")


# --- слой 3: процессы
class BookingRequest(Process):
    """Запрос брони: проверить даты, зарезервировать единицу, подтвердить бронь."""

    name = "Бронирование"
    trigger = Trigger.human_request("Арендатор просит забронировать единицу на даты.")
    aggregate = Booking
    check = Step.agent("Проверить свободные даты", role=rental_agent, capability=check_availability)
    reserve = Step.agent(
        "Зарезервировать единицу",
        role=rental_agent,
        capability=reserve_equipment,
        transition=Equipment.available.to(Equipment.reserved),
        on_failure="escalate",
    )
    confirm = Step.agent(
        "Подтвердить бронь",
        role=rental_agent,
        capability=confirm_booking,
        transition=Booking.requested.to(Booking.confirmed),
        emits=[BookingConfirmed],
    )


class EquipmentIssue(Process):
    """Выдача: залог, акт, единица в аренде."""

    name = "Выдача оборудования"
    trigger = Trigger.human_request("Арендатор пришёл за оборудованием по брони.")
    aggregate = Booking
    deposit = Step.human("Принять залог", role=clerk, capability=take_deposit)
    issue = Step.human(
        "Выдать по акту",
        role=clerk,
        capability=issue_equipment,
        transition=Booking.confirmed.to(Booking.issued),
        emits=[EquipmentIssued],
    )
    rented = Step.system("Отметить единицу в аренде", role=scheduler, capability=mark_rented, transition=Equipment.reserved.to(Equipment.rented))


class RentalReturn(Process):
    """Возврат без повреждений: приём, осмотр, оценка, возврат залога, единица в парк, закрытие."""

    name = "Возврат без повреждений"
    trigger = Trigger.human_request("Арендатор вернул оборудование.")
    aggregate = Booking
    accept = Step.human("Принять оборудование", role=clerk, capability=accept_return, transition=Booking.issued.to(Booking.returned))
    to_inspection = Step.system("Поставить единицу на осмотр", role=scheduler, capability=mark_inspection, transition=Equipment.rented.to(Equipment.inspection))
    assess = Step.agent("Оценить повреждения по фото", role=rental_agent, capability=assess_damage)
    settle = Step.agent(
        "Вернуть залог",
        role=rental_agent,
        capability=refund_deposit,
        gate=Gate(approver=manager, constraint=C001, condition=Deposit.amount > 50000),
        transition=Deposit.held.to(Deposit.refunded),
        emits=[DepositRefunded],
    )
    release = Step.agent("Вернуть единицу в парк", role=rental_agent, capability=return_to_fleet, transition=Equipment.inspection.to(Equipment.available))
    close = Step.agent("Закрыть аренду", role=rental_agent, capability=close_booking, transition=Booking.returned.to(Booking.closed))


class DamagedReturn(RentalReturn):
    """Возврат с повреждением: те же шаги, но залог удерживается (всегда через менеджера), единица уходит в ремонт."""

    name = "Возврат с повреждением"
    trigger = Trigger.agent_decision("Оценка при возврате показала повреждение.")
    settle = Step.agent(
        "Удержать залог",
        role=rental_agent,
        capability=withhold_deposit,
        gate=Gate(approver=manager, constraint=C004),
        transition=Deposit.held.to(Deposit.withheld),
        emits=[DepositWithheld],
    )
    release = Step.agent(
        "Отправить в ремонт",
        role=rental_agent,
        capability=send_to_repair,
        transition=Equipment.inspection.to(Equipment.repair),
        emits=[EquipmentSentToRepair],
    )


class OverdueCheck(Process):
    """Ежедневная проверка просрочек."""

    name = "Проверка просрочек"
    trigger = Trigger.schedule("Каждый день в 10:00.")
    aggregate = Booking
    find = Step.agent("Найти просроченные аренды", role=rental_agent, capability=list_overdue)
    flag = Step.agent("Поставить задачу менеджеру", role=rental_agent, capability=flag_overdue, emits=[OverdueFlagged])


class RepairCycle(Process):
    """Возврат единицы из ремонта в парк (ещё не согласован с мастерской)."""

    name = "Возврат из ремонта"
    status = "planned"
    trigger = Trigger.human_request("Мастер сообщил, что ремонт закончен.")
    aggregate = Equipment
    finish = Step.human("Принять единицу из ремонта", role=clerk, capability=finish_repair, transition=Equipment.repair.to(Equipment.available))


# --- слой 5: реализации нет — бизнес описан со слов
bindings = [
    Binding(c, status="planned", evidence="процесс ведётся вручную, автоматизации нет (2026-10-27)")
    for c in (
        check_availability, reserve_equipment, confirm_booking, refund_deposit, take_deposit, issue_equipment, mark_rented, accept_return,
        mark_inspection, assess_damage, withhold_deposit, return_to_fleet, send_to_repair, close_booking, list_overdue, flag_overdue, finish_repair,
    )
]
