#!/usr/bin/env python3
"""Оцифровка системы: объектное описание ↔ пять слоёв, карта процессов, цифровой двойник (ADR-021).

Источник модели (SRC) — каталог с пятью JSON-файлами слоёв или модуль Python с описанием
на классах softoscope.oop. Подкоманды:

  build SRC.py --out DIR [--check]
      скомпилировать описание в пять файлов слоёв и прогнать валидатор;
      --check ничего не пишет и падает, если файлы в DIR расходятся с описанием
  decompile DIR [--out SRC.py]
      готовая модель → описание на классах (без потерь: build вернёт те же файлы)
  map SRC [--process ID[,ID...]] [--out FILE.md]
      сводка по пяти слоям, схемы процессов и автоматы сущностей (Markdown + Mermaid);
      несколько ID через запятую — процессы одной темы на одной схеме
  twin SRC [--process ID] [--aggregate ID] [--scenario FILE.json] [--as-is] [--assume-confirmed] [--json]
      прогнать процессы на заглушках: Static Guard, гейты, пороги, переходы, события;
      без --process — все активные процессы, каждый в своём журнале; --chain p1,p2@id — процессы
      подряд над одним агрегатом в общем журнале (состояния переходят); --events — сохранить журнал.
      --as-is: без допущения «всё реализовано».

Примеры:
  python3 tools/digitize.py decompile examples/agent-delivery --out /tmp/delivery.py
  python3 tools/digitize.py build examples/equipment-rental/model.py --out examples/equipment-rental --check
  python3 tools/digitize.py map examples/equipment-rental --out /tmp/rental-map.md
  python3 tools/digitize.py twin examples/equipment-rental --process rental_return --scenario s.json
"""

from __future__ import annotations

import argparse
import json
import runpy
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import validate  # noqa: E402
from softoscope.decompile import decompile  # noqa: E402
from softoscope.event_store import EventStore  # noqa: E402
from softoscope.model import InvalidModel, load_model  # noqa: E402
from softoscope.oop import LAYERS, DescriptionError, compile_namespace  # noqa: E402
from softoscope.process_map import group_diagram, render  # noqa: E402
from softoscope.twin import Scenario, Twin, format_trail  # noqa: E402

import drift  # noqa: E402


def compile_file(path: Path) -> dict[str, dict[str, Any]]:
    sys.path.insert(0, str(path.resolve().parent))
    try:
        return compile_namespace(runpy.run_path(str(path), run_name="softoscope_description"))
    finally:
        sys.path.pop(0)


def read_dir(path: Path) -> dict[str, dict[str, Any]]:
    return {n: json.loads((path / f"{n}.json").read_text(encoding="utf-8")) for n in LAYERS}


def load_layers(src: Path) -> dict[str, dict[str, Any]]:
    return compile_file(src) if src.suffix == ".py" else read_dir(src)


def write_layers(layers: dict[str, dict[str, Any]], out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for n in LAYERS:
        (out / f"{n}.json").write_text(json.dumps(layers[n], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class Materialized:
    """Модель на диске для валидатора и drift: каталог как есть или временная копия описания."""

    def __init__(self, src: Path):
        self.layers = load_layers(src)
        if src.suffix == ".py":
            self._tmp = tempfile.TemporaryDirectory(prefix="softoscope-digitize-")
            self.root = Path(self._tmp.name)
            write_layers(self.layers, self.root)
        else:
            self._tmp = None
            self.root = src

    def __enter__(self) -> Materialized:
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._tmp:
            self._tmp.cleanup()


def _print_findings(report: Any) -> None:
    for f in report.findings:
        print(f"  {f.level:<5} {f.file} {f.path}: {f.message}")


def cmd_build(args: argparse.Namespace) -> int:
    layers = compile_file(args.src)
    if args.check:
        current = read_dir(args.out)
        bad = [n for n in LAYERS if current.get(n) != layers[n]]
        if bad:
            print(f"описание {args.src} расходится с {args.out}: {', '.join(f'{n}.json' for n in bad)}")
            print(f"  пересоберите: python3 tools/digitize.py build {args.src} --out {args.out}")
            return 1
        print(f"описание {args.src} совпадает с {args.out} (пять слоёв)")
        return 0
    with tempfile.TemporaryDirectory(prefix="softoscope-build-") as tmp:
        write_layers(layers, Path(tmp))
        report = validate.run(Path(tmp))
    if report.errors:
        print(f"описание скомпилировано, но модель невалидна (ошибок {report.errors}); файлы не записаны:")
        _print_findings(report)
        return 1
    write_layers(layers, args.out)
    print(f"записано пять слоёв в {args.out}; валидатор: ошибок 0, предупреждений {len(report.findings)}")
    return 0


def cmd_decompile(args: argparse.Namespace) -> int:
    src = decompile(read_dir(args.src))
    if args.out:
        args.out.write_text(src, encoding="utf-8")
        print(f"описание записано в {args.out}")
    else:
        sys.stdout.write(src)
    return 0


def _model_and_holes(m: Materialized) -> tuple[Any, list[dict[str, Any]]]:
    model = load_model(m.root)
    holes = [h.__dict__ for h in drift.run(m.root, None, strict=False)]
    return model, holes


def cmd_map(args: argparse.Namespace) -> int:
    with Materialized(args.src) as m:
        model, holes = _model_and_holes(m)
        ids = args.process.split(",") if args.process else []
        missing = [x for x in ids if x not in model.procs]
        if missing:
            print(f"процессов {missing} нет в слое 3")
            return 2
        if len(ids) > 1:
            text = "\n".join([f"# {model.system_id}: процессы {', '.join(ids)}", ""] + group_diagram(model, ids, holes)) + "\n"
        else:
            text = render(model, holes, args.process)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"карта записана в {args.out}")
    else:
        sys.stdout.write(text)
    return 0


def cmd_twin(args: argparse.Namespace) -> int:
    layers = load_layers(args.src)
    raw = json.loads(args.scenario.read_text(encoding="utf-8")) if args.scenario else {}
    with Twin(layers, assume_implemented=not args.as_is, assume_confirmed=args.assume_confirmed) as twin:
        if args.process:
            if args.process not in twin.model.procs:
                print(f"процесса {args.process!r} нет в слое 3")
                return 2
            runs = [twin.run(args.process, args.aggregate, Scenario.from_dict(raw))]
        else:
            scen = {key: Scenario.from_dict(v) for key, v in raw.items()}
            if args.chain:
                store = EventStore()
                runs = []
                for spec in args.chain:
                    procs, _, agg = spec.partition("@")
                    ids = [x for x in procs.split(",") if x]
                    missing = [x for x in ids if x not in twin.model.procs]
                    if missing:
                        print(f"процессов {missing} нет в слое 3")
                        return 2
                    runs += twin.run_chain(ids, agg or args.aggregate, scen, store=store)
            else:
                if args.events:
                    print("--events пишет один журнал: укажите --process или --chain")
                    return 2
                runs = twin.run_all(scen)
    if args.events:
        events = [e for r in runs for e in r.events]
        args.events.write_text(json.dumps({"events": events}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps([r.as_dict() for r in runs], ensure_ascii=False, indent=2))
        return 0
    if runs and runs[0].assumed_bindings:
        print(f"допущение: реализованными считаются {len(runs[0].assumed_bindings)} возможностей без implemented-привязки: {', '.join(runs[0].assumed_bindings)}")
    if runs and runs[0].assumed_confirmed:
        print(f"допущение: подтверждёнными считаются ограничения {', '.join(runs[0].assumed_confirmed)}")
    for r in runs:
        print(f"\n== {r.process_id} [{r.aggregate_id}]: {r.status}")
        if args.process or r.status != "completed":
            print("\n".join(format_trail(r)))
        if r.entity_states:
            print("  состояния: " + ", ".join(f"{k} = {v}" for k, v in r.entity_states.items()))
    return 0 if all(r.status == "completed" for r in runs) else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="описание → пять слоёв")
    b.add_argument("src", type=Path)
    b.add_argument("--out", type=Path, required=True)
    b.add_argument("--check", action="store_true", help="только сравнить с файлами в --out")
    d = sub.add_parser("decompile", help="пять слоёв → описание")
    d.add_argument("src", type=Path)
    d.add_argument("--out", type=Path)
    m = sub.add_parser("map", help="карта модели в Markdown + Mermaid")
    m.add_argument("src", type=Path)
    m.add_argument("--process")
    m.add_argument("--out", type=Path)
    t = sub.add_parser("twin", help="прогон процессов на заглушках")
    t.add_argument("src", type=Path)
    t.add_argument("--process")
    t.add_argument("--chain", action="append", default=[], metavar="P1,P2,...@AGG", help="процессы подряд над одним агрегатом в общем журнале; можно повторять")
    t.add_argument("--aggregate", default="twin-1")
    t.add_argument("--scenario", type=Path, help="JSON сценария; без --process — {process_id: сценарий}")
    t.add_argument("--as-is", action="store_true", help="без допущения о реализации: дыры слоя 5 блокируют")
    t.add_argument("--assume-confirmed", action="store_true", help="считать неподтверждённые ограничения подтверждёнными")
    t.add_argument("--json", action="store_true")
    t.add_argument("--events", type=Path, help="записать журнал прогона как образец событий {\"events\": [...]}")
    args = ap.parse_args(argv)
    try:
        return {"build": cmd_build, "decompile": cmd_decompile, "map": cmd_map, "twin": cmd_twin}[args.cmd](args)
    except (DescriptionError, InvalidModel, ValueError) as exc:
        print(f"ошибка: {exc}")
        for f in getattr(exc, "findings", [])[:20]:
            print(f"  {f['level']:<5} {f['file']} {f['path']}: {f['message']}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
