#!/usr/bin/env python3
"""Реестр кода: что объявлено в репозитории — разбором синтаксиса, а не поиском строки.

Для Python (файлы *.py и исполняемые скрипты с python в shebang) реестр строится модулем ast:
  symbols — функции, классы и методы с полным именем (Class.method) и строкой;
  routes  — маршруты вида @router.get("/path") с учётом prefix у APIRouter(...)/Blueprint(...);
  graph_nodes — узлы графа, объявленные как .add_node("имя", ...);
  tables  — классы с __tablename__ = "имя";
  env     — переменные окружения: os.getenv("X"), os.environ.get("X"), os.environ["X"].
Остальные файлы попадают в реестр только хэшем: их привязки drift сверяет по тексту (ADR-020).

Снимок (snapshot): коммит HEAD и признак незакоммиченных правок, если это git-репозиторий,
и общий хэш всех файлов реестра. По нему находку drift можно воспроизвести.

Использование:
  python3 tools/code_inventory.py <репозиторий> [--root <подкаталог>] [--json]
Только чтение; репозиторий не меняется, зависимостей проекта не требуется.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SKIP_DIRS = {".git", ".hg", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache", "dist", "build", ".tox"}
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
ROUTER_FACTORIES = {"APIRouter", "Blueprint", "Router"}
MAX_FILE_BYTES = 2_000_000


@dataclass
class Inventory:
    root: Path
    files: dict[str, str] = field(default_factory=dict)          # путь → sha256
    python_files: set[str] = field(default_factory=set)
    parse_errors: dict[str, str] = field(default_factory=dict)
    symbols: list[dict[str, Any]] = field(default_factory=list)
    routes: list[dict[str, Any]] = field(default_factory=list)
    graph_nodes: list[dict[str, Any]] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=list)
    env: list[dict[str, Any]] = field(default_factory=list)
    snapshot: dict[str, Any] = field(default_factory=dict)

    # -------- поиск
    def has_symbol(self, path: str, name: str) -> bool:
        for s in self.symbols:
            if s["path"] == path and (s["name"] == name or s["name"].endswith("." + name)):
                return True
        return False

    def has_route(self, path: str | None, method: str, route_path: str) -> bool:
        return any(r["method"] == method.upper() and r["path"] == route_path and (path is None or r["file"] == path) for r in self.routes)

    def has_graph_node(self, path: str | None, name: str) -> bool:
        return any(n["name"] == name and (path is None or n["path"] == path) for n in self.graph_nodes)

    def has_table(self, path: str | None, name: str) -> bool:
        return any(t["name"] == name and (path is None or t["path"] == path) for t in self.tables)

    def to_dict(self) -> dict[str, Any]:
        return {"snapshot": self.snapshot, "symbols": self.symbols, "routes": self.routes, "graph_nodes": self.graph_nodes,
                "tables": self.tables, "env": self.env, "parse_errors": self.parse_errors}


def _is_python(p: Path, head: bytes) -> bool:
    return p.suffix == ".py" or (p.suffix == "" and head.startswith(b"#!") and b"python" in head.split(b"\n", 1)[0])


def _const(node: ast.AST | None) -> Any:
    return node.value if isinstance(node, ast.Constant) else None


def _call_name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _env_key(node: ast.AST) -> str | None:
    """os.getenv("X"), os.environ.get("X"), os.environ["X"] → "X"."""
    if isinstance(node, ast.Call) and node.args:
        f = node.func
        text = ast.unparse(f) if hasattr(ast, "unparse") else ""
        if text in ("os.getenv", "getenv", "os.environ.get", "environ.get"):
            k = _const(node.args[0])
            return k if isinstance(k, str) else None
    if isinstance(node, ast.Subscript) and ast.unparse(node.value) in ("os.environ", "environ"):
        k = _const(node.slice)
        return k if isinstance(k, str) else None
    return None


def _scan_python(inv: Inventory, rel: str, source: str) -> None:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        inv.parse_errors[rel] = f"{exc.msg} (строка {exc.lineno})"
        return
    inv.python_files.add(rel)

    routers: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and _call_name(node.value.func) in ROUTER_FACTORIES:
            prefix = ""
            for kw in node.value.keywords:
                if kw.arg in ("prefix", "url_prefix") and isinstance(_const(kw.value), str):
                    prefix = _const(kw.value)
            for t in node.targets:
                if isinstance(t, ast.Name):
                    routers[t.id] = prefix

    def visit(body: list[ast.stmt], owner: list[str]) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qual = ".".join(owner + [node.name])
                kind = "class" if isinstance(node, ast.ClassDef) else ("method" if owner else "function")
                inv.symbols.append({"path": rel, "name": qual, "kind": kind, "line": node.lineno})
                if isinstance(node, ast.ClassDef):
                    for item in node.body:
                        if isinstance(item, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__tablename__" for t in item.targets):
                            name = _const(item.value)
                            if isinstance(name, str):
                                inv.tables.append({"path": rel, "name": name, "class": qual, "line": item.lineno})
                else:
                    for deco in node.decorator_list:
                        if isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute) and deco.func.attr in HTTP_METHODS:
                            target = ast.unparse(deco.func.value)
                            tail = _const(deco.args[0]) if deco.args else next((_const(k.value) for k in deco.keywords if k.arg == "path"), None)
                            if isinstance(tail, str):
                                inv.routes.append({"file": rel, "method": deco.func.attr.upper(), "path": routers.get(target, "") + tail,
                                                   "handler": qual, "line": deco.lineno})
                visit(node.body, owner + [node.name])
            elif not owner and isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name):
                        inv.symbols.append({"path": rel, "name": t.id, "kind": "variable", "line": node.lineno, "assigned": True})

    visit(tree.body, [])
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_node" and node.args:
            name = _const(node.args[0])
            if isinstance(name, str):
                inv.graph_nodes.append({"path": rel, "name": name, "line": node.lineno})
        key = _env_key(node)
        if key:
            inv.env.append({"path": rel, "name": key, "line": node.lineno})


def _git(repo: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def build(repo: Path, root: str = "") -> Inventory:
    base = (repo / root) if root else repo
    inv = Inventory(root=base)
    digest = hashlib.sha256()
    for p in sorted(base.rglob("*")):
        if not p.is_file() or any(part in SKIP_DIRS for part in p.relative_to(base).parts):
            continue
        try:
            if p.stat().st_size > MAX_FILE_BYTES:
                continue
            data = p.read_bytes()
        except OSError:
            continue
        rel = p.relative_to(base).as_posix()
        sha = hashlib.sha256(data).hexdigest()
        inv.files[rel] = sha
        digest.update(rel.encode() + b"\0" + sha.encode() + b"\n")
        if _is_python(p, data[:200]):
            _scan_python(inv, rel, data.decode("utf-8", errors="replace"))
    commit = _git(repo, "rev-parse", "HEAD")
    status = _git(repo, "status", "--porcelain") if commit else None
    inv.snapshot = {"commit": commit, "dirty": bool(status) if status is not None else None,
                    "files": len(inv.files), "python_files": len(inv.python_files), "digest": digest.hexdigest()}
    return inv


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo", type=Path)
    ap.add_argument("--root", default="", help="подкаталог с кодом (как repository.root в слое 5)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if not args.repo.is_dir():
        print(f"репозиторий не найден: {args.repo}", file=sys.stderr)
        return 2
    inv = build(args.repo, args.root)
    if args.json:
        print(json.dumps(inv.to_dict(), ensure_ascii=False, indent=2))
    else:
        s = inv.snapshot
        print(f"снимок: commit={s['commit'] or '-'} dirty={s['dirty']} файлов={s['files']} python={s['python_files']} digest={s['digest'][:12]}")
        for k in ("symbols", "routes", "graph_nodes", "tables", "env"):
            print(f"{k}: {len(getattr(inv, k))}")
        for path, err in inv.parse_errors.items():
            print(f"не разобран: {path}: {err}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
