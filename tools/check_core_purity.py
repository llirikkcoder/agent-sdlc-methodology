#!/usr/bin/env python3
"""Проверка универсальности ядра: в схемах, инструментах и модульных документах
не должно быть слов из конкретных предметных областей.

Правило из исходной постановки: «если в ядре понадобилось слово из одного домена —
это дефект ядра». Список стоп-слов собран из двух контрастных примеров (ответы
регулятору и продажи) и расширяется при добавлении новых примеров.

Использование: python3 tools/check_core_purity.py [файлы...]
Без аргументов проверяет ядро по умолчанию (см. DEFAULT_CORE).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_CORE = [
    "schemas/purpose.schema.json",
    "schemas/domain.schema.json",
    "schemas/processes.schema.json",
    "schemas/capabilities.schema.json",
    "schemas/implementation.schema.json",
    "schemas/event.schema.json",
    "tools/validate.py",
    "tools/drift.py",
    "tools/_schema_lite.py",
    "tools/code_inventory.py",
    "tools/draft_bindings.py",
    "tools/holes_to_tasks.py",
    "docs/01-softoscope.md",
    "docs/02-event-sourcing.md",
    "docs/03-sdlc-pipeline.md",
    "docs/04-mcp-scope-interface.md",
    "docs/06-glossary.md",
    "docs/07-reference-implementation.md",
    "softoscope/model.py",
    "softoscope/scope_api.py",
    "softoscope/mcp_server.py",
    "softoscope/event_store.py",
    "softoscope/projections.py",
    "softoscope/runtime.py",
]

# Стемы, по которым узнаётся домен. Регистр не важен.
STOP_STEMS = [
    # предметная область «ответы регулятору»
    r"pharm", r"фарм", r"\bctd\b", r"ectd", r"gxp", r"dossier", r"досье", r"regulat", r"регулятор",
    r"\bdrug", r"препарат", r"minzdrav", r"минздрав", r"clinical", r"клиническ",
    # предметная область «продажи»
    r"\bdeal", r"сделк", r"invoice", r"\bсч[её]т", r"\bcrm\b", r"\blead\b", r"\bлид", r"sales", r"продаж",
    r"\boffer", r"оффер", r"коммерческ", r"billing", r"contact", r"контакт", r"customer", r"клиент", r"payment", r"плат[её]ж",
    # технологии конкретной реализации
    r"\brag\b", r"retriev", r"embedding", r"эмбеддинг", r"chunk", r"чанк", r"pgvector", r"langgraph", r"fastapi",
    r"reranker", r"vector store", r"векторн",
    # роли конкретного домена
    r"expert", r"эксперт", r"inspector", r"инспектор", r"reviewer",
]
_STOP = re.compile("|".join(STOP_STEMS), re.IGNORECASE)
# Ссылки на каталоги примеров допустимы: они указывают наружу, а не вносят домен в ядро.
_ALLOW = re.compile(r"examples/[a-z0-9\-]+|docs/05-[a-z\-]+\.md")


def scan(path: Path) -> list[tuple[int, str, str]]:
    hits = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        cleaned = _ALLOW.sub("", line)
        for m in _STOP.finditer(cleaned):
            hits.append((n, m.group(0), line.strip()))
    return hits


def main(argv: list[str]) -> int:
    files = [Path(a) for a in argv] if argv else [ROOT / f for f in DEFAULT_CORE]
    total = 0
    for f in files:
        if not f.exists():
            print(f"WARN  {f.relative_to(ROOT) if f.is_relative_to(ROOT) else f}: файла нет")
            continue
        for n, word, line in scan(f):
            total += 1
            rel = f.relative_to(ROOT) if f.is_relative_to(ROOT) else f
            print(f"ERROR {rel}:{n}: доменное слово {word!r} в ядре: {line[:100]}")
    print(f"-- чистота ядра: найдено {total} доменных слов в {len(files)} файлах")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
