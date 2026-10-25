# Модуль 4. MCP-интерфейс `scope_*`

Агент получает модель системы не из промпта, а через отдельный MCP-сервер, который читает пять файлов слоёв и отдаёт срезы. Сервер **только читает** модель; менять слои через него нельзя — это цикл поставки с ревью.

Все инструменты принимают и возвращают JSON; ответы строятся из файлов слоёв без домысливания. Ошибки — объект `{error: {code, message}}` с кодами `not_found`, `invalid_model`, `drift`.

## `scope_get_purpose`
- Слой 1 целиком.
- Вход: `{ "system_id": string }`.
- Выход: `purpose.json` (назначение, цели, non_goals, роли, ограничения). Агент обязан вызвать его первым в любой сессии: без ограничений план не строится.

## `scope_list_processes`
- Вход: `{ "system_id": string, "status"?: "active" | "planned" | "deprecated" }`.
- Выход: `[{ process_id, name, trigger, aggregate_entity, status, steps_count, human_gates_count }]`.
- Нужен на этапе Intake: сопоставить задачу с процессом.

## `scope_get_process_map`
- Слои 3–4 в развёрнутом виде для одного процесса.
- Вход: `{ "process_id": string }`.
- Выход:
```json
{
  "process_id": "…", "aggregate_entity": "…", "trigger": {…},
  "constraints": [ {id, rule, enforcement} ],          // применимые правила слоя 1
  "steps": [
    {
      "step_id": "…", "name": "…", "actor": "agent", "role": "…",
      "capability": { "capability_id": "…", "kind": "…", "effect": "…", "idempotency": "…",
                      "inputs": {…}, "outputs": {…}, "preconditions": [...], "constraints": [...] },
      "human_gate": { "required": true, "reason": "…", "approver_role": "…", "condition": "…" },
      "transition": { "entity": "…", "from": "…", "to": "…" },
      "next": [...], "on_failure": "…",
      "binding": { "status": "implemented", "kind": "…", "location": {…} }   // слой 5, кратко
    }
  ]
}
```
- Сервер подставляет возможность и привязку в каждый шаг, чтобы агент не собирал это из трёх файлов. Если привязки нет или она не `implemented` — поле `binding.status` это показывает, а Static Guard поднимет `MissingCapabilityError`.

## `scope_get_entity_schema`
- Слой 2 для одной сущности.
- Вход: `{ "entity_name": string }`.
- Выход: описание, атрибуты (с пометкой `sensitive`), состояния, `initial_state`, переходы с `actor_kind`/`guard`, связи, инварианты.

## `scope_get_capability`
- Слой 4 + привязки слоя 5 для одной возможности.
- Вход: `{ "capability_id": string }`.
- Выход: `{ capability: {…}, bindings: [...], used_in_steps: [{process_id, step_id}] }`.

## `scope_check_drift`
- Запускает `tools/drift.py` над моделью (и, если сервер сконфигурирован путём к репозиторию, против кода).
- Вход: `{ "strict"?: boolean }`.
- Выход: `{ "holes": [ {level, reason, process_id, step_id, capability_id, detail} ], "errors": n }` — ровно формат `drift.py --json`.
- Агент вызывает его на Static Guard; ненулевой `errors` останавливает исполнение.

## `scope_validate_plan`
- Проверяет план исполнения до старта (часть Static Guard).
- Вход: `{ "process_id": string, "steps": [ { step_id, capability_id, inputs_keys: [...] } ] }`.
- Выход: `{ "ok": boolean, "problems": [ {step_id, code, message} ] }`, коды: `missing_capability`, `gate_required`, `constraint_pre_check`, `unknown_step`, `transition_not_allowed`.

## Реализация сервера

Минимальный сервер — тонкая обёртка над `tools/validate.py` и `tools/drift.py` плюс чтение JSON. Требования:
- при старте валидировать модель; невалидная модель — все инструменты отвечают `invalid_model`;
- не кэшировать модель дольше, чем живёт ревизия; в ответах отдавать `spec_version` слоя 1;
- не иметь инструментов записи; изменение слоёв — только через репозиторий и ревью.

Соответствие исходной спецификации: четыре инструмента из неё (`scope_get_purpose`, `scope_get_process_map`, `scope_check_drift`, `scope_get_entity_schema`) сохранены с теми же параметрами; добавлены `scope_list_processes`, `scope_get_capability`, `scope_validate_plan` как необходимые для этапов Intake и Static Guard.

Эталонная реализация — `softoscope/scope_api.py` и `softoscope/mcp_server.py`; отличия по форме (обёртка `spec_version`, `gate_rule` в карте процесса, поля `human_gate`/`entity_state` и код `missing_inputs` в `scope_validate_plan`) описаны в `07-reference-implementation.md`.
