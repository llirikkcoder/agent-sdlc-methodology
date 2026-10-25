#!/usr/bin/env python3
"""Дыры → задачи: превратить находки drift и события журнала в задачи трекера.

Методология говорит: находка drift — это событие и задача (docs/03). Инструмент делает
вторую половину: читает отчёт `drift.py --json` или журнал событий (JSONL) и печатает задачи.
В трекер он ничего не пишет: вывод — данные или команды, которые запускает человек или агент.

Источники (тип определяется по содержимому):
  отчёт drift   — {"holes": [...]}; берутся дыры уровня не ниже --min-level;
  журнал (JSONL) — события CapabilityMissing и DriftDetected.

У каждой задачи есть ключ `drift:<причина>:<процесс>:<шаг>:<возможность>`. По нему трекер
отличает новую дыру от уже заведённой: ключи, перечисленные в --skip-keys, пропускаются.

Использование:
  python3 tools/drift.py <модель> --json | python3 tools/holes_to_tasks.py -
  python3 tools/holes_to_tasks.py <файл> [--min-level WARN] [--skip-keys keys.txt]
                                  [--emit json|bd] [--parent <id>]
  --emit json (по умолчанию) — JSON-строка на задачу: key, title, description, priority, labels;
  --emit bd                  — команды `bd create …` для трекера beads (только печать).
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any, Iterable

LEVELS = {"INFO": 0, "WARN": 1, "ERROR": 2}
PRIORITY = {"ERROR": 1, "WARN": 2, "INFO": 3}
ACTION = {
    "not_declared": "описать возможность в слое 4",
    "not_bound": "добавить привязку в слой 5",
    "not_implemented": "реализовать и перевести привязку в implemented",
    "stale": "обновить слой 5 по коду или вернуть код",
    "orphan": "убрать привязку или описать возможность в слое 4",
    "unused": "привязать к шагу или убрать возможность",
    "role_mismatch": "выдать роль",
    "unconfirmed_constraint": "подтвердить или снять ограничение (решение человека)",
}


def key_of(t: dict[str, Any]) -> str:
    parts = [t.get("reason"), t.get("process_id"), t.get("step_id"), t.get("capability_id")]
    return "drift:" + ":".join(p or "-" for p in parts)


def from_report(report: dict[str, Any], min_level: str) -> Iterable[dict[str, Any]]:
    for h in report.get("holes", []):
        if LEVELS[h["level"]] >= LEVELS[min_level]:
            yield {**h, "source": "drift"}


def from_events(lines: Iterable[str]) -> Iterable[dict[str, Any]]:
    for line in lines:
        line = line.strip()
        if not line:
            continue
        ev = json.loads(line)
        et = ev.get("event_type")
        if et not in ("CapabilityMissing", "DriftDetected"):
            continue
        sc, pl = ev.get("scope", {}), ev.get("payload", {})
        yield {
            "level": "ERROR" if et == "CapabilityMissing" else pl.get("level", "WARN"),
            "reason": pl.get("reason", "drift"),
            "process_id": sc.get("process_id"),
            "step_id": sc.get("step_id"),
            "capability_id": sc.get("capability_id"),
            "detail": pl.get("detail") or pl.get("suggested_action") or "",
            "source": f"{et} {ev.get('event_id', '')}".strip(),
        }


def to_task(h: dict[str, Any]) -> dict[str, Any]:
    where = ".".join(p for p in (h.get("process_id"), h.get("step_id")) if p) or "модель"
    cap = h.get("capability_id")
    title = f"[drift] {h['reason']}: {where}" + (f" → {cap}" if cap else "")
    action = ACTION.get(h["reason"], "решение человека")
    description = "\n".join(filter(None, [
        f"Что: {h.get('detail') or h['reason']}",
        f"Где: {where}" + (f", возможность {cap}" if cap else ""),
        f"Что сделать: {action}",
        f"Источник: {h['source']}, уровень {h['level']}",
        "Изменение начинается со слоёв, потом код (docs/03, цикл поставки).",
    ]))
    return {"key": key_of(h), "title": title, "description": description,
            "priority": PRIORITY.get(h["level"], 2), "labels": ["drift", h["reason"]]}


def read_source(path: str) -> tuple[dict[str, Any] | None, list[str]]:
    text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, dict) and "holes" in data:
                return data, []
        except json.JSONDecodeError:
            pass
    return None, text.splitlines()


def main() -> int:
    ap = argparse.ArgumentParser(description="Дыры drift и журнала → задачи трекера (только вывод)")
    ap.add_argument("source", help="отчёт drift.py --json или журнал JSONL; - — stdin")
    ap.add_argument("--min-level", choices=list(LEVELS), default="WARN")
    ap.add_argument("--skip-keys", type=Path, help="файл с уже заведёнными ключами, по одному в строке")
    ap.add_argument("--emit", choices=["json", "bd"], default="json")
    ap.add_argument("--parent", help="id родительской задачи (для --emit bd)")
    args = ap.parse_args()

    report, lines = read_source(args.source)
    holes = from_report(report, args.min_level) if report is not None else from_events(lines)
    skip = set(args.skip_keys.read_text(encoding="utf-8").split()) if args.skip_keys else set()

    seen: set[str] = set()
    for h in holes:
        task = to_task(h)
        if task["key"] in skip or task["key"] in seen:
            continue
        seen.add(task["key"])
        if args.emit == "json":
            print(json.dumps(task, ensure_ascii=False))
        else:
            cmd = ["bd", "create", task["title"], "-d", task["description"], "-p", str(task["priority"]),
                   "-l", ",".join(task["labels"]), "--external-ref", task["key"]]
            if args.parent:
                cmd += ["--parent", args.parent]
            print(" ".join(shlex.quote(c) for c in cmd))
    return 0


if __name__ == "__main__":
    sys.exit(main())
