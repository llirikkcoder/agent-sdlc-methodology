#!/usr/bin/env python3
"""Drift-проверка: описанные процессы (слой 3) и возможности (слой 4) против реализации (слой 5)
и, при --repo, против реального кода.

Дыра (MissingCapability) — это шаг процесса, для которого нет работающей возможности:
  not_declared   — шаг ссылается на capability_id, которого нет в слое 4;
  not_bound      — возможность описана, но в слое 5 нет ни одной привязки;
  not_implemented— привязка есть, но status in {stub, planned, unconfirmed, deprecated};
  stale          — привязка указывает на файл/символ/маршрут/узел, которых нет в репозитории (--repo);
  unconfirmed_constraint — к процессу применимо ограничение со status=unconfirmed: critical → WARN, в --strict ERROR (блок); high → INFO, в --strict WARN; medium → INFO (ADR-017).
Отдельно: orphan — привязка к неописанной возможности; unused — возможность без шагов;
  unmodeled — обратный дрейф (--repo): маршрут, узел графа или таблица есть в коде, но не описаны в слое 5.
  Всегда WARN, в том числе в --strict: не всё в коде обязано быть в модели (ADR-020).

С --repo привязки к Python-файлам сверяются по реестру кода (tools/code_inventory.py, разбор ast):
символ — это объявленная функция, класс или метод, а не совпавшая строка. Остальные файлы — по тексту.
В отчёт пишется снимок исходника: коммит, признак незакоммиченных правок, общий хэш файлов.

Использование:
  python3 tools/drift.py examples/<dir> [--repo /path/to/repo] [--strict] [--json] [--no-reverse]
Код возврата 1, если есть дыры уровня ERROR (--strict переводит WARN в ERROR, кроме unmodeled и unconfirmed_constraint).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import code_inventory  # noqa: E402

NOT_IMPLEMENTED = {"stub", "planned", "unconfirmed", "deprecated"}
# уровень этих дыр задан правилом и в --strict не повышается
STRICT_EXEMPT = {"unconfirmed_constraint", "unmodeled"}


@dataclass
class Hole:
    level: str  # ERROR | WARN | INFO
    reason: str  # not_declared | not_bound | not_implemented | stale | unconfirmed_constraint | orphan | unused | unverifiable | unmodeled
    process_id: str | None
    step_id: str | None
    capability_id: str | None
    detail: str

    def __str__(self) -> str:
        loc = ".".join(x for x in (self.process_id, self.step_id) if x) or "-"
        return f"{self.level:5} {self.reason:16} {loc:40} {self.capability_id or '-':36} {self.detail}"


def _load(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


# ------------------------------------------------------------ проверка кода


def _read(repo_root: Path, root_hint: str, rel: str) -> str | None:
    p = (repo_root / root_hint / rel) if root_hint else (repo_root / rel)
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def check_binding_in_repo(b: dict[str, Any], repo_root: Path, root_hint: str, inv: code_inventory.Inventory | None = None) -> list[str]:
    """Вернуть список расхождений с кодом (пусто — всё подтверждено)."""
    loc = b.get("location", {})
    path = loc.get("path")
    if not path:
        return ["нет location.path — проверить по коду нельзя"]
    if inv is not None and path in inv.python_files:
        return _check_by_inventory(loc, path, inv)
    if inv is not None and path in inv.parse_errors:
        return [f"файл не разобран: {path}: {inv.parse_errors[path]}"]
    return _check_by_text(loc, path, repo_root, root_hint)


def _check_by_inventory(loc: dict[str, Any], path: str, inv: code_inventory.Inventory) -> list[str]:
    problems: list[str] = []
    sym = loc.get("symbol")
    if sym and not inv.has_symbol(path, sym):
        problems.append(f"символ {sym!r} не объявлен в {path}")
    route = loc.get("route")
    if route and not inv.has_route(path, route["method"], route["path"]):
        problems.append(f"маршрут {route['method']} {route['path']} не объявлен в {path}")
    node = loc.get("graph_node")
    if node and not inv.has_graph_node(path, node):
        problems.append(f"узел графа {node!r} не объявлен в {path}")
    return problems


def _check_by_text(loc: dict[str, Any], path: str, repo_root: Path, root_hint: str) -> list[str]:
    problems: list[str] = []
    text = _read(repo_root, root_hint, path)
    if text is None:
        return [f"файл не найден: {path}"]
    sym = loc.get("symbol")
    if sym:
        pat = rf"^\s*(async\s+def|def|class)\s+{re.escape(sym)}\b|^\s*{re.escape(sym)}\s*[:=]"
        if not re.search(pat, text, re.M):
            problems.append(f"символ {sym!r} не найден в {path}")
    route = loc.get("route")
    if route:
        method = route["method"].lower()
        rpath = route["path"]
        # маршрут собирается из prefix роутера + path декоратора; ищем хвост пути в декораторе
        prefix_m = re.search(r"APIRouter\([^)]*prefix\s*=\s*[\"']([^\"']+)[\"']", text)
        prefix = prefix_m.group(1) if prefix_m else ""
        tail = rpath[len(prefix):] if prefix and rpath.startswith(prefix) else rpath
        dec = rf"@\w+\.{method}\(\s*[\"']{re.escape(tail)}[\"']"
        if not re.search(dec, text):
            problems.append(f"маршрут {route['method']} {rpath} не найден в {path} (prefix={prefix!r}, tail={tail!r})")
    node = loc.get("graph_node")
    if node and not re.search(rf"add_node\(\s*[\"']{re.escape(node)}[\"']", text):
        problems.append(f"узел графа {node!r} не найден в {path}")
    return problems


def check_storage_in_repo(st: dict[str, Any], repo_root: Path, root_hint: str, inv: code_inventory.Inventory | None = None) -> list[str]:
    path, sym = st.get("path"), st.get("symbol")
    if not path:
        return []
    if inv is not None and path in inv.python_files:
        problems = []
        if sym and not inv.has_symbol(path, sym):
            problems.append(f"класс {sym!r} не объявлен в {path}")
        if st.get("kind") == "table" and not inv.has_table(path, st["name"]):
            problems.append(f"таблица {st['name']!r} не объявлена в {path} (__tablename__)")
        return problems
    text = _read(repo_root, root_hint, path)
    if text is None:
        return [f"файл не найден: {path}"]
    problems = []
    if sym and not re.search(rf"^\s*class\s+{re.escape(sym)}\b", text, re.M):
        problems.append(f"класс {sym!r} не найден в {path}")
    if st.get("kind") == "table" and not re.search(rf"__tablename__\s*=\s*[\"']{re.escape(st['name'])}[\"']|CREATE TABLE[^;]*\b{re.escape(st['name'])}\b|\{{table\}}", text):
        problems.append(f"таблица {st['name']!r} не объявлена в {path}")
    return problems


def reverse_holes(impl: dict[str, Any], inv: code_inventory.Inventory) -> list[Hole]:
    """Обратный дрейф: маршруты, узлы графа и таблицы кода, которых нет в слое 5 (ADR-020)."""
    routes, nodes, tables = set(), set(), set()
    for b in impl.get("bindings", []):
        loc = b.get("location", {})
        if loc.get("route"):
            routes.add((loc["route"]["method"].upper(), loc["route"]["path"]))
        if loc.get("graph_node"):
            nodes.add(loc["graph_node"])
    for st in impl.get("storage", []):
        if st.get("kind") == "table":
            tables.add(st["name"])
    holes = []
    for r in inv.routes:
        if (r["method"], r["path"]) not in routes:
            holes.append(Hole("WARN", "unmodeled", None, None, None, f"маршрут {r['method']} {r['path']} ({r['file']}:{r['line']}, {r['handler']}) не описан в слое 5"))
    for n in inv.graph_nodes:
        if n["name"] not in nodes:
            holes.append(Hole("WARN", "unmodeled", None, None, None, f"узел графа {n['name']!r} ({n['path']}:{n['line']}) не описан в слое 5"))
    for tb in inv.tables:
        if tb["name"] not in tables:
            holes.append(Hole("WARN", "unmodeled", None, None, None, f"таблица {tb['name']!r} ({tb['path']}:{tb['line']}, {tb['class']}) не описана в storage слоя 5"))
    return holes


# ------------------------------------------------------------------- drift


def run(example: Path, repo: Path | None, strict: bool, reverse: bool = True) -> list[Hole]:
    return run_with_snapshot(example, repo, strict, reverse)[0]


def run_with_snapshot(example: Path, repo: Path | None, strict: bool, reverse: bool = True) -> tuple[list[Hole], dict[str, Any] | None]:
    processes = _load(example / "processes.json")["processes"]
    caps = {c["capability_id"]: c for c in _load(example / "capabilities.json")["capabilities"]}
    impl = _load(example / "implementation.json")
    bindings = impl.get("bindings", [])
    root_hint = impl.get("repository", {}).get("root", "")
    inv = code_inventory.build(repo, root_hint) if repo else None
    by_cap: dict[str, list[dict[str, Any]]] = {}
    for b in bindings:
        by_cap.setdefault(b["capability_id"], []).append(b)

    holes: list[Hole] = []
    used_caps: set[str] = set()
    for proc in processes:
        planned = proc.get("status") == "planned"
        for step in proc["steps"]:
            cid = step.get("capability_id")
            if not cid:
                continue
            used_caps.add(cid)
            if cid not in caps:
                holes.append(Hole("ERROR", "not_declared", proc["process_id"], step["step_id"], cid, "capability_id нет в слое 4"))
                continue
            bs = by_cap.get(cid, [])
            if not bs:
                holes.append(Hole("WARN" if planned else "ERROR", "not_bound", proc["process_id"], step["step_id"], cid, "в слое 5 нет привязки"))
                continue
            impl_ok = [b for b in bs if b["status"] == "implemented"]
            if not impl_ok:
                statuses = ",".join(sorted({b["status"] for b in bs}))
                level = "INFO" if planned else ("ERROR" if strict else "WARN")
                holes.append(Hole(level, "not_implemented", proc["process_id"], step["step_id"], cid, f"status={statuses}"))
            if repo:
                for b in bs:
                    if b["status"] != "implemented":
                        continue
                    if b["kind"] == "mcp_tool" and not b.get("location", {}).get("path"):
                        holes.append(Hole("INFO", "unverifiable", proc["process_id"], step["step_id"], cid, "mcp_tool без пути в репозитории — сверяется списком инструментов сервера"))
                        continue
                    for p in check_binding_in_repo(b, repo, root_hint, inv):
                        holes.append(Hole("ERROR", "stale", proc["process_id"], step["step_id"], cid, p))

    # ADR-017: неподтверждённое критическое ограничение, применимое к процессу, — дыра в знании агента
    purpose_path = example / "purpose.json"
    if purpose_path.exists():
        for c in _load(purpose_path).get("hard_constraints", []):
            if c.get("status") != "unconfirmed":
                continue
            sev = c.get("severity")
            # Цикл поставки (по умолчанию) предупреждает; runtime Static Guard (--strict) блокирует critical.
            level = {"critical": "ERROR" if strict else "WARN", "high": "WARN" if strict else "INFO"}.get(sev, "INFO")
            verdict = {"critical": "Static Guard (--strict) блокирует исполнение", "high": "в strict-режиме предупреждение"}.get(sev, "только пометка")
            at = c.get("applies_to", {})
            for proc in processes:
                step_caps = {s.get("capability_id") for s in proc["steps"]}
                hit = proc["process_id"] in at.get("processes", []) or step_caps & set(at.get("capabilities", []))
                if hit or not any(at.values()):
                    holes.append(Hole(level, "unconfirmed_constraint", proc["process_id"], None, None, f"{c['id']} ({sev}) не подтверждено: {verdict}"))
    for cid in caps:
        if cid not in used_caps:
            if cid not in by_cap:
                holes.append(Hole("WARN", "unused", None, None, cid, "возможность не используется шагами и не привязана"))
            else:
                holes.append(Hole("INFO", "unused", None, None, cid, "возможность не используется ни одним шагом"))
    for b in bindings:
        if b["capability_id"] not in caps:
            holes.append(Hole("ERROR", "orphan", None, None, b["capability_id"], "привязка к возможности, которой нет в слое 4"))
    if repo:
        for st in impl.get("storage", []):
            if st.get("status", "implemented") != "implemented":
                continue
            for p in check_storage_in_repo(st, repo, root_hint, inv):
                holes.append(Hole("ERROR", "stale", None, None, None, f"storage {st['name']}: {p}"))
    if inv is not None and reverse:
        holes.extend(reverse_holes(impl, inv))
    if strict:
        for h in holes:
            # уровень unconfirmed_constraint задан по severity (ADR-017), unmodeled — всегда WARN (ADR-020)
            if h.level == "WARN" and h.reason not in STRICT_EXEMPT:
                h.level = "ERROR"
    order = {"ERROR": 0, "WARN": 1, "INFO": 2}
    holes.sort(key=lambda h: (order[h.level], h.reason, h.process_id or "", h.step_id or "", h.detail))
    return holes, (inv.snapshot if inv is not None else None)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("example", type=Path)
    ap.add_argument("--repo", type=Path, help="корень репозитория для сверки привязок с кодом (только чтение)")
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-reverse", action="store_true", help="не искать в коде то, чего нет в модели (unmodeled)")
    args = ap.parse_args(argv)
    if args.repo and not args.repo.exists():
        print(f"репозиторий не найден: {args.repo}", file=sys.stderr)
        return 2
    holes, snapshot = run_with_snapshot(args.example, args.repo, args.strict, not args.no_reverse)
    errors = sum(1 for h in holes if h.level == "ERROR")
    if args.json:
        report: dict[str, Any] = {"holes": [asdict(h) for h in holes], "errors": errors}
        if snapshot is not None:
            report["snapshot"] = snapshot
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        if snapshot is not None:
            print(f"-- снимок: commit={snapshot['commit'] or '-'} dirty={snapshot['dirty']} файлов={snapshot['files']} digest={snapshot['digest'][:12]}")
        for h in holes:
            print(h)
        counts: dict[str, int] = {}
        for h in holes:
            counts[f"{h.level}/{h.reason}"] = counts.get(f"{h.level}/{h.reason}", 0) + 1
        summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "дыр нет"
        print(f"-- {args.example}{' против ' + str(args.repo) if args.repo else ''}: {summary}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
