"""Журнал событий: append-only, идемпотентность по event_id и metadata.idempotency_key,
sequence внутри агрегата (docs/02). Хранение — память или JSONL-файл (по строке на событие).
Каждое событие проверяется по schemas/event.schema.json до записи.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from .model import ROOT, _validate

EVENT_SCHEMA_PATH = ROOT / "schemas" / "event.schema.json"


class InvalidEvent(Exception):
    def __init__(self, errors: list[tuple[str, str]]):
        super().__init__("; ".join(f"{p}: {m}" for p, m in errors))
        self.errors = errors


def aggregate_key(event: dict[str, Any]) -> tuple[str, str, str]:
    agg = event["aggregate"]
    return (agg["kind"], agg.get("type", ""), agg["id"])


class EventStore:
    def __init__(self, path: Path | str | None = None, validate_schema: bool = True):
        self.path = Path(path) if path else None
        self.validate_schema = validate_schema
        self._schema = json.loads(EVENT_SCHEMA_PATH.read_text(encoding="utf-8")) if validate_schema else None
        self._events: list[dict[str, Any]] = []
        self._ids: set[str] = set()
        self._idem: set[tuple[tuple[str, str, str], str]] = set()
        self._seq: dict[tuple[str, str, str], int] = {}
        if self.path and self.path.exists():
            with self.path.open(encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        self._accept(json.loads(line), persist=False)

    # ------------------------------------------------------------------ запись
    def append(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """Записать событие. None — дубликат (тот же event_id или idempotency_key в агрегате)."""
        return self._accept(dict(event), persist=True)

    def _accept(self, event: dict[str, Any], persist: bool) -> dict[str, Any] | None:
        if event.get("event_id") in self._ids:
            return None
        key = aggregate_key(event)
        idem = event.get("metadata", {}).get("idempotency_key")
        if idem and (key, idem) in self._idem:
            return None
        if "sequence" not in event:
            event["sequence"] = self._seq.get(key, 0) + 1
        if self._schema is not None:
            errors = _validate._schema_errors(event, self._schema)
            if errors:
                raise InvalidEvent(errors)
        self._events.append(event)
        self._ids.add(event["event_id"])
        if idem:
            self._idem.add((key, idem))
        self._seq[key] = max(self._seq.get(key, 0), event["sequence"])
        if persist and self.path:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    # ----------------------------------------------------------------- чтение
    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(list(self._events))

    def all(self) -> list[dict[str, Any]]:
        return list(self._events)

    def for_aggregate(self, kind: str, type_: str, id_: str) -> list[dict[str, Any]]:
        return [e for e in self._events if aggregate_key(e) == (kind, type_, id_)]

    def last_sequence(self, kind: str, type_: str, id_: str) -> int:
        return self._seq.get((kind, type_, id_), 0)

    def until(self, occurred_at: str) -> list[dict[str, Any]]:
        """Состояние на момент T (time-travel): события с occurred_at <= T (ISO-строки сравнимы при одной зоне)."""
        return [e for e in self._events if e["occurred_at"] <= occurred_at]
