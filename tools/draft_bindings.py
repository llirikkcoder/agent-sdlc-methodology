#!/usr/bin/env python3
"""Черновик слоя 5: предложить привязки возможностей слоя 4 к коду по реестру (tools/code_inventory.py).

Слой 5 — утверждение человека, подтверждённое evidence; по коду он автоматически не правится
(docs/03). Поэтому инструмент ничего не пишет: он печатает черновик, где каждая привязка имеет
status=planned и evidence «draft: …». Человек проверяет кандидата, дописывает evidence и переводит
привязку в implemented.

Кандидаты ищутся по имени возможности (без учёта регистра и подчёркиваний):
  маршрут, чей обработчик называется как возможность → kind=http_route, location.route;
  узел графа с таким именем                            → kind=graph_node, location.graph_node;
  функция, метод или класс с таким именем              → kind=function, location.symbol.
Несколько кандидатов — все в списке candidates, первым идёт наиболее точный.

Использование:
  python3 tools/draft_bindings.py <модель> --repo <репозиторий> [--root <подкаталог>] [--all]
  --all — предлагать и для возможностей, у которых уже есть привязка implemented.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import code_inventory  # noqa: E402


def norm(name: str) -> str:
    return name.rsplit(".", 1)[-1].replace("_", "").replace("-", "").lower()


def candidates(cap_id: str, inv: code_inventory.Inventory) -> list[dict[str, Any]]:
    want = norm(cap_id)
    found: list[tuple[int, dict[str, Any]]] = []
    for r in inv.routes:
        if norm(r["handler"]) == want:
            found.append((0, {"kind": "http_route", "location": {"path": r["file"], "symbol": r["handler"], "route": {"method": r["method"], "path": r["path"]}},
                              "why": f"обработчик маршрута {r['method']} {r['path']} ({r['file']}:{r['line']})"}))
    for n in inv.graph_nodes:
        if norm(n["name"]) == want:
            found.append((1, {"kind": "graph_node", "location": {"path": n["path"], "graph_node": n["name"]},
                              "why": f"узел графа {n['name']!r} ({n['path']}:{n['line']})"}))
    routed = {(r["file"], r["handler"]) for r in inv.routes}
    for s in inv.symbols:
        if s["kind"] == "variable" or (s["path"], s["name"]) in routed or norm(s["name"]) != want:
            continue
        exact = s["name"].rsplit(".", 1)[-1] == cap_id
        found.append((2 if exact else 3, {"kind": "function", "location": {"path": s["path"], "symbol": s["name"]},
                                          "why": f"{s['kind']} {s['name']} ({s['path']}:{s['line']})" + ("" if exact else ", имя совпало без учёта регистра")}))
    found.sort(key=lambda x: (x[0], x[1]["location"]["path"]))
    return [c for _, c in found]


def draft(model: Path, repo: Path, root: str = "", include_bound: bool = False) -> dict[str, Any]:
    caps = json.loads((model / "capabilities.json").read_text(encoding="utf-8"))
    impl_path = model / "implementation.json"
    impl = json.loads(impl_path.read_text(encoding="utf-8")) if impl_path.exists() else {}
    root = root or impl.get("repository", {}).get("root", "")
    bound = {b["capability_id"] for b in impl.get("bindings", []) if b.get("status") == "implemented"}
    inv = code_inventory.build(repo, root)
    out: dict[str, Any] = {"system_id": caps["system_id"], "snapshot": inv.snapshot, "draft_bindings": [], "unmatched": []}
    today = date.today().isoformat()
    for c in caps["capabilities"]:
        cid = c["capability_id"]
        if cid in bound and not include_bound:
            continue
        found = candidates(cid, inv)
        if not found:
            out["unmatched"].append(cid)
            continue
        best = found[0]
        out["draft_bindings"].append({
            "capability_id": cid,
            "status": "planned",
            "kind": best["kind"],
            "location": best["location"],
            "evidence": f"draft {today}: {best['why']}; подтвердить человеком и заменить на code: …",
            "candidates": [{"kind": f["kind"], "location": f["location"], "why": f["why"]} for f in found],
        })
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("model", type=Path)
    ap.add_argument("--repo", type=Path, required=True)
    ap.add_argument("--root", default="")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args(argv)
    if not args.repo.is_dir():
        print(f"репозиторий не найден: {args.repo}", file=sys.stderr)
        return 2
    print(json.dumps(draft(args.model, args.repo, args.root, args.all), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
