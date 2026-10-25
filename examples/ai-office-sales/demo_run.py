#!/usr/bin/env python3
"""Демонстрация runtime на примере ai-office-sales с моками вместо MCP-инструментов.

Запуск: python3 examples/ai-office-sales/demo_run.py [--journal путь.jsonl]

Три сценария:
  1. deal_onboarding — Static Guard блокирует: send_offer и send_invoice в слое 5 planned
     (MissingCapabilityError, события CapabilityMissing), ни один инструмент не вызван.
  2. contact_cleanup — необратимое удаление под гейтом C002: HumanGateRequested → владелец
     утверждает → delete_contact вызван → ProcessCompleted.
  3. stalled_deal_followup — агентские шаги, затем человеческий шаг log_touch (runtime ждёт),
     затем эскалация flag_for_human с доменным событием EscalationRaised.
Исполнители — функции-моки; в реальной системе на их месте MCP-клиент ai-office.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from softoscope.event_store import EventStore  # noqa: E402
from softoscope.model import load_model  # noqa: E402
from softoscope.runtime import Runtime  # noqa: E402

OWNER = {"kind": "human", "id": "owner_kirill", "role": "owner"}
MANAGER = {"kind": "human", "id": "manager_anna", "role": "sales_manager"}

CALLS: list[str] = []


def mock(name: str, **outputs):
    def run(cap, inputs, ctx):
        CALLS.append(f"{name}({', '.join(f'{k}={v!r}' for k, v in inputs.items() if k.endswith('_id'))})")
        return dict(outputs)

    return run


EXECUTORS = {
    "get_deal": mock("get_deal", deal={"id": "deal-41", "status": "processing"}),
    "list_deals": mock("list_deals", deals=[{"id": "deal-41", "days_silent": 6}]),
    "create_task": mock("create_task", task_id="task-9"),
    "flag_for_human": mock("flag_for_human", escalation_task_id="esc-3"),
    "delete_contact": mock("delete_contact", deleted=True),
    "move_deal_stage": mock("move_deal_stage"),
    "record_qualification": mock("record_qualification"),
    "create_offer": mock("create_offer", offer_id="offer-10"),
    "record_payment": mock("record_payment"),
    "log_deal_touch": mock("log_deal_touch"),
}


def trail(store: EventStore, instance_id: str) -> None:
    for e in store:
        if e["aggregate"]["id"] == instance_id or e["aggregate"]["kind"] == "entity":
            sc = e["scope"]
            where = ".".join(x for x in (sc.get("step_id"),) if x) or "-"
            extra = ""
            if e["event_type"] == "CapabilityMissing":
                extra = f" reason={e['payload']['reason']} binding={e['payload']['binding_status']}"
            elif e["event_type"] == "HumanGateRequested":
                extra = f" approver={e['payload']['approver_role']} reasons={e['payload'].get('reasons')}"
            elif e["event_type"] == "HumanGateResolved":
                extra = f" decision={e['payload']['decision']} by={e['actor']['id']}"
            elif e["event_type"] == "EntityTransitioned":
                extra = f" {e['aggregate']['type']}:{e['aggregate']['id']} {e['payload']['from']}→{e['payload']['to']}"
            print(f"  {e['sequence']:>2} {e['event_type']:<22} {where:<22} actor={e['actor']['kind']}{extra}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--journal", type=Path, help="JSONL-файл журнала (по умолчанию — в памяти)")
    args = ap.parse_args(argv)
    model = load_model(ROOT / "examples" / "ai-office-sales")
    store = EventStore(args.journal)
    rt = Runtime(model, store, EXECUTORS, agent_id="sales_agent")

    print("== 1. deal_onboarding: Static Guard")
    iid = rt.start("deal_onboarding", "deal-41", inputs={"deal_id": "deal-41"}, actor=MANAGER, correlation_id="demo-1")
    inst = rt.instance(iid)
    print(f"  статус: {inst['status']}; дыры: {[(m['step_id'], m['reason']) for m in inst['missing']]}")
    trail(store, iid)
    print(f"  вызовов инструментов: {len(CALLS)}")

    print("\n== 2. contact_cleanup: гейт C002")
    iid = rt.start("contact_cleanup", "contact-7", inputs={"contact_id": "contact-7"}, actor=OWNER, correlation_id="demo-2")
    print(f"  статус после старта: {rt.instance(iid)['status']}; очередь владельца: {[(g['step_id'], g['reason']) for g in rt.pending_gates('owner')]}")
    rt.claim_gate(iid, "delete", OWNER)
    status = rt.resolve_gate(iid, "delete", "approved", OWNER, note="дубликат, сделок нет")
    print(f"  статус после решения: {status}")
    trail(store, iid)

    print("\n== 3. stalled_deal_followup: человеческий шаг и эскалация")
    iid = rt.start("stalled_deal_followup", "deal-41", inputs={"deal_id": "deal-41", "company_id": 1}, correlation_id="demo-3")
    print(f"  статус: {rt.instance(iid)['status']} (ждёт {rt.instance(iid)['waiting']})")
    status = rt.complete_human_step(iid, "log_touch", MANAGER, outputs={"channel": "call"})
    print(f"  статус после шага человека: {status}")
    trail(store, iid)

    print(f"\nвызовы инструментов: {CALLS}")
    print(f"событий в журнале: {len(store)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
