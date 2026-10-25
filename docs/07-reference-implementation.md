# Модуль 7. Эталонная реализация: `softoscope/`

Пакет `softoscope/` — исполняемое воплощение модулей 2–4 на стандартной библиотеке Python (ADR-009, ADR-018). Он не знает ни одного домена: всё, что относится к предметной области, приходит из пяти файлов слоёв и из исполнителей, которые подключает реализация.

```
softoscope/
  model.py        — загрузка пяти слоёв (только после зелёного validate.py), правило гейта, порог, маскирование sensitive
  scope_api.py    — семь инструментов scope_* как функции над моделью
  mcp_server.py   — MCP-сервер по stdio (JSON-RPC 2.0), только чтение
  event_store.py  — журнал: append-only, схема события, event_id/idempotency_key, sequence по агрегату, JSONL
  projections.py  — ProcessInstances, PendingGates, EntityState, audit_trail; пересборка с нуля
  runtime.py      — цикл исполнения: Static Guard → Plan → Execute → Verify → Close
tests/            — 31 тест на синтетической модели (tests/fixture_model.py), без домена
```

## MCP-сервер

```
python3 -m softoscope.mcp_server examples/ai-office-sales            # stdio
python3 -m softoscope.mcp_server <модель> --repo <корень кода>        # scope_check_drift против кода
claude mcp add softoscope -- python3 -m softoscope.mcp_server /abs/path/examples/ai-office-sales
```

Поведение:
- при старте модель валидируется; невалидная — все инструменты отвечают `{"error": {"code": "invalid_model", "findings": [...]}}`, сервер не падает;
- слои перечитываются при изменении mtime любого из пяти файлов — модель не кэшируется дольше ревизии;
- инструментов записи нет; метод `resources/*` и прочие — `-32601`;
- каждый успешный ответ несёт `spec_version` слоя 1; `tools/call` возвращает и текст, и `structuredContent`.

Отличия от docs/04 по форме: ответ `scope_list_processes` обёрнут в `{spec_version, processes: [...]}`; в карте процесса у каждого шага есть `gate_rule` — ответ правила гейта с причинами (`irreversible`, `policy`, `system_level`, `constraint:Cnnn`) и порогом; `scope_validate_plan` принимает у шага плана `human_gate` (план предусматривает гейт) и `entity_state` (текущее состояние сущности перехода), а в кодах проблем добавлен `missing_inputs`; у каждой проблемы — `severity: block | warn`, `ok` = нет блокирующих.

## Runtime

```python
from softoscope.model import load_model
from softoscope.event_store import EventStore
from softoscope.runtime import Runtime, CapabilityError

model = load_model("examples/ai-office-sales")          # InvalidModel, если валидатор красный
store = EventStore("journal.jsonl")                      # или EventStore() в памяти
rt = Runtime(model, store, executors={...}, read_entity=..., constraint_checkers={...})

iid = rt.start("<process_id>", "<aggregate_id>", inputs={...}, actor=human)
rt.pending_gates(role="<approver_role>")                 # очередь человека
rt.claim_gate(iid, "<step_id>", human); rt.resolve_gate(iid, "<step_id>", "approved", human)
rt.complete_human_step(iid, "<human_step_id>", human, outputs={...})
rt.instance(iid)                                         # проекция ProcessInstances
```

Что подключает реализация:
- **исполнители** `executors[capability_id](capability, inputs, ctx) -> outputs` — вызов MCP, HTTP, функция, мок. Ошибка — `CapabilityError`. Для `kind: llm_step` исполнитель обязан вернуть `_model: {name, version?, prompt_version?}` (иначе `StepFailed`, ADR-015); `_idempotency_key` попадает в `metadata`;
- **`read_entity(entity, id) -> dict`** — чтение атрибутов сущности для порога (ADR-010). Если порог вычислить нельзя — гейт безусловный, в `HumanGateRequested.payload.note` написано почему;
- **`constraint_checkers[Cnnn](data, ctx) -> None | причина`** — детерминированные проверки `pre_check` (до вызова) и `post_check` (на Close). Ограничения без проверяющего не проверяются runtime (это pre_check для агента, см. `scope_validate_plan`).

Что делает runtime сам:
- **Static Guard** на старте: `drift.py --strict` для процесса (planned/stub-привязки, `unconfirmed` critical-ограничения) плюс «нет исполнителя» → `ProcessInitiated` с планом, затем `CapabilityMissing` на каждую дыру, статус `blocked`; ни один инструмент не вызван;
- **гейт** по правилу модуля 1 (`Model.gate_rule`), не по полю `human_gate` в одиночку: необратимость, политика, `system_level`, ограничение `gate`. Условный гейт — только по порогу ограничения: ниже порога `AutoApprovedByRule`, выше — `HumanGateRequested`. Решение принимает человек с ролью `approver_role` (иначе `role_mismatch`), `actor.kind` — только `human`;
- **чтения** без `StepRequested` (только `StepCompleted`), `llm_step` — полный лог;
- **переход** проверяется по `EntityState` и автомату слоя 2 **до** вызова; после успеха — `EntityTransitioned` и события из `emits`;
- **on_failure**: `retry` только при `idempotency != none` и не более `max_retries`; `escalate` → `HumanGateRequested(reason: low_confidence)`, после `approved` шаг повторяется; `skip` → `StepCompleted{skipped}`; `abort` → `ProcessAborted`. Отклонённый гейт — `abort` (или `skip`);
- **Close**: `post_check` ограничения, затем `ProcessCompleted`;
- чувствительные атрибуты слоя 2 заменяются на `<redacted>` в любой полезной нагрузке.

Состояние экземпляра нигде не хранится, кроме журнала: перед каждым решением проекция пересобирается (`Runtime.instance`). Это медленно для больших журналов и намеренно: эталон показывает, что журнал — единственный источник истины. Продуктивная реализация добавит снимки проекций (docs/02: допустимы как ускорение).

Ветвление `next[]` по условиям словами runtime не вычисляет: берётся первый ещё не исполненный шаг из `next`, иначе следующий по порядку. Условия — для агента и валидатора плана; детерминированное ветвление — через ограничения с порогом или отдельные процессы.

## Демонстрация

`python3 examples/ai-office-sales/demo_run.py` — три сценария с моками вместо MCP-сервера примера: блокировка процесса Static Guard'ом (две `planned`-привязки), необратимый шаг через гейт, человеческий шаг и эскалация с доменным событием. Печатает трассу событий.

## Чего в эталоне нет

- Подключения к реальным серверам возможностей: исполнители подключаются реализацией.
- Снимков проекций и параллельного исполнения экземпляров.
- Аутентификации человека: `actor.id` принимается на веру; проверяется только роль.
- Планировщика триггеров (`schedule`, `event`): `Runtime.start` вызывает внешний код.
