from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tools"))
sys.path.insert(0, str(HERE))

import code_inventory  # noqa: E402
import draft_bindings  # noqa: E402
import drift  # noqa: E402
from fixture_model import write_model  # noqa: E402

APP = '''\
import os
from fastapi import APIRouter

router = APIRouter(prefix="/items")
LIMIT = int(os.getenv("ITEM_LIMIT", "10"))


@router.get("/{item_id}")
async def read_item(item_id: str):
    return {"id": item_id, "debug": os.environ.get("ITEM_DEBUG")}


@router.post("/{item_id}/publish")
def publish_item(item_id: str):
    return {}


@router.delete("/{item_id}")
def drop_item(item_id: str):
    return {}


class Service:
    def classify_item(self, item):
        return item

    def prepare_item(self, item):
        return item


def confirm_item(item):
    return item


def once_action():
    return None


def migrate_schema():
    return None


class ItemRow:
    __tablename__ = "items"


class AuditRow:
    __tablename__ = "audit_log"


def build(graph):
    graph.add_node("route_item", lambda s: s)
'''

SCRIPT = '''\
#!/usr/bin/env python3
def main():
    return 0
'''


class SyntheticRepoCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.model = write_model(base / "model")
        self.repo = base / "repo"
        (self.repo / "demo").mkdir(parents=True)
        (self.repo / "demo" / "app.py").write_text(APP, encoding="utf-8")
        (self.repo / "bin").mkdir()
        (self.repo / "bin" / "tool").write_text(SCRIPT, encoding="utf-8")
        (self.repo / "demo" / "broken.py").write_text("def (:\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def set_impl(self, mutate) -> None:
        p = self.model / "implementation.json"
        impl = json.loads(p.read_text(encoding="utf-8"))
        mutate(impl)
        p.write_text(json.dumps(impl, ensure_ascii=False), encoding="utf-8")

    def test_inventory_reads_declarations(self) -> None:
        inv = code_inventory.build(self.repo)
        self.assertIn({"file": "demo/app.py", "method": "GET", "path": "/items/{item_id}", "handler": "read_item", "line": 8}, inv.routes)
        self.assertEqual({t["name"] for t in inv.tables}, {"items", "audit_log"})
        self.assertEqual([n["name"] for n in inv.graph_nodes], ["route_item"])
        self.assertEqual({e["name"] for e in inv.env}, {"ITEM_LIMIT", "ITEM_DEBUG"})
        self.assertTrue(inv.has_symbol("demo/app.py", "classify_item"))
        self.assertTrue(inv.has_symbol("bin/tool", "main"), "скрипт без расширения с python в shebang")
        self.assertIn("demo/broken.py", inv.parse_errors)
        self.assertIsNone(inv.snapshot["commit"])
        self.assertEqual(len(inv.snapshot["digest"]), 64)

    def test_symbol_in_string_is_not_a_declaration(self) -> None:
        # поиск по тексту принял бы строку внутри docstring за объявление; реестр — нет
        app = self.repo / "demo" / "app.py"
        app.write_text(app.read_text(encoding="utf-8").replace("def once_action():", 'NOTE = """\ndef once_action():\n"""\n\n\ndef once_action_v2():'), encoding="utf-8")
        self.assertTrue(drift._check_by_text({"symbol": "once_action"}, "demo/app.py", self.repo, "") == [])
        stale = [h for h in drift.run(self.model, self.repo, strict=False) if h.reason == "stale"]
        self.assertEqual({h.capability_id for h in stale}, {"once_action"})
        self.assertIn("не объявлен", stale[0].detail)

    def test_route_checked_with_prefix(self) -> None:
        def bind_route(impl):
            for b in impl["bindings"]:
                if b["capability_id"] == "read_item":
                    b.update(kind="http_route", location={"path": "demo/app.py", "route": {"method": "GET", "path": "/items/{item_id}"}})
                if b["capability_id"] == "publish_item":
                    b.update(kind="http_route", location={"path": "demo/app.py", "route": {"method": "POST", "path": "/items/publish"}})
        self.set_impl(bind_route)
        stale = [h for h in drift.run(self.model, self.repo, strict=False) if h.reason == "stale"]
        self.assertEqual({h.capability_id for h in stale}, {"publish_item"})

    def test_unmodeled_is_warning_even_in_strict(self) -> None:
        self.set_impl(lambda impl: impl.setdefault("storage", []).append({"name": "items", "kind": "table"}))
        holes = drift.run(self.model, self.repo, strict=True)
        unmodeled = sorted(h.detail.split(" (")[0] for h in holes if h.reason == "unmodeled")
        self.assertEqual(unmodeled, [
            "маршрут DELETE /items/{item_id}",
            "маршрут GET /items/{item_id}",
            "маршрут POST /items/{item_id}/publish",
            "таблица 'audit_log'",
            "узел графа 'route_item'",
        ])
        self.assertTrue(all(h.level == "WARN" for h in holes if h.reason == "unmodeled"))
        self.assertFalse(any(h.reason == "unmodeled" for h in drift.run(self.model, self.repo, strict=True, reverse=False)))

    def test_snapshot_records_commit(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git не установлен")
        def git(*args: str) -> None:
            subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)
        git("init", "-q")
        git("add", "-A")
        git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "init")
        _, snap = drift.run_with_snapshot(self.model, self.repo, strict=False)
        self.assertEqual(len(snap["commit"]), 40)
        self.assertFalse(snap["dirty"])
        (self.repo / "demo" / "new.py").write_text("x = 1\n", encoding="utf-8")
        _, snap2 = drift.run_with_snapshot(self.model, self.repo, strict=False)
        self.assertTrue(snap2["dirty"])
        self.assertNotEqual(snap["digest"], snap2["digest"])

    def test_draft_bindings_are_planned(self) -> None:
        self.set_impl(lambda impl: impl.update(bindings=[]))
        d = draft_bindings.draft(self.model, self.repo)
        by_cap = {b["capability_id"]: b for b in d["draft_bindings"]}
        self.assertTrue(all(b["status"] == "planned" and b["evidence"].startswith("draft ") for b in by_cap.values()))
        self.assertEqual(by_cap["read_item"]["kind"], "http_route")
        self.assertEqual(by_cap["read_item"]["location"]["route"], {"method": "GET", "path": "/items/{item_id}"})
        self.assertEqual(by_cap["classify_item"]["location"], {"path": "demo/app.py", "symbol": "Service.classify_item"})
        self.assertIn("risky_action", d["unmatched"])


if __name__ == "__main__":
    unittest.main()
