#!/usr/bin/env python3
"""MCP-сервер scope_* по stdio (JSON-RPC 2.0, по сообщению на строку), без зависимостей.

Запуск:  python3 -m softoscope.mcp_server <каталог модели> [--repo <корень кода>]
Регистрация в Claude Code:
  claude mcp add softoscope -- python3 -m softoscope.mcp_server /abs/path/examples/<dir>

Сервер только читает модель: инструментов записи нет (docs/04). Модель перечитывается
по изменению mtime файлов слоёв, чтобы не кэшировать её дольше, чем живёт ревизия.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, TextIO

from .scope_api import TOOL_SPECS, ScopeAPI

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "softoscope-scope", "version": "0.1.0"}
LAYER_FILES = ("purpose.json", "domain.json", "processes.json", "capabilities.json", "implementation.json")


class ScopeServer:
    def __init__(self, root: Path, repo: Path | None = None):
        self.api = ScopeAPI(root, repo)
        self._stamp = self._mtime()

    def _mtime(self) -> tuple[float, ...]:
        return tuple((self.api.root / f).stat().st_mtime if (self.api.root / f).exists() else 0.0 for f in LAYER_FILES)

    def _refresh(self) -> None:
        stamp = self._mtime()
        if stamp != self._stamp:
            self._stamp = stamp
            self.api.reload()

    # --------------------------------------------------------------- JSON-RPC
    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Обработать одно сообщение; None — для уведомлений (ответа нет)."""
        method = message.get("method")
        msg_id = message.get("id")
        params = message.get("params") or {}
        if method is None:
            return None
        if method.startswith("notifications/"):
            return None
        try:
            result = self._dispatch(method, params)
        except KeyError as exc:
            return self._err(msg_id, -32601, f"метод не поддерживается: {exc}")
        except ValueError as exc:
            return self._err(msg_id, -32602, str(exc))
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _err(msg_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return {
                "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": "Сначала scope_get_purpose, затем scope_list_processes/scope_get_process_map; перед исполнением — scope_check_drift {strict: true} и scope_validate_plan.",
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOL_SPECS}
        if method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str):
                raise ValueError("tools/call: нет name")
            self._refresh()
            result = self.api.call(name, params.get("arguments") or {})
            is_error = "error" in result
            return {
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}],
                "structuredContent": result,
                "isError": is_error,
            }
        raise KeyError(method)

    # ------------------------------------------------------------------ stdio
    def serve(self, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> None:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                stdout.write(json.dumps(self._err(None, -32700, "невалидный JSON")) + "\n")
                stdout.flush()
                continue
            messages = message if isinstance(message, list) else [message]
            for m in messages:
                response = self.handle(m) if isinstance(m, dict) else self._err(None, -32600, "ожидался объект")
                if response is not None:
                    stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                    stdout.flush()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("model_dir", type=Path, help="каталог с purpose.json, domain.json, processes.json, capabilities.json, implementation.json")
    ap.add_argument("--repo", type=Path, help="корень репозитория для scope_check_drift против кода (только чтение)")
    args = ap.parse_args(argv)
    server = ScopeServer(args.model_dir, args.repo)
    if server.api.load_error:
        print(f"[softoscope] модель невалидна, инструменты отвечают invalid_model: {args.model_dir}", file=sys.stderr)
    else:
        print(f"[softoscope] модель {server.api.model.system_id} загружена из {args.model_dir}", file=sys.stderr)  # type: ignore[union-attr]
    server.serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
