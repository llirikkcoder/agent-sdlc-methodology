# Решения (ADR)

Короткие записи по развилкам, разрешённым при сборке методологии. ADR-001…009 — формат и структура (решены при сборке). ADR-010…017 — развилки по сути методологии (`../open-questions.md`, Q1–Q8): решены 25.10.2026 по делегированию Кирилла с учётом его предложений.

| № | Решение |
|---|---|
| [ADR-001](ADR-001-json-not-yaml.md) | JSON, а не YAML, для слоёв и событий |
| [ADR-002](ADR-002-layer4-vs-layer5-boundary.md) | Граница между слоем 4 (контракт) и слоем 5 (привязка) |
| [ADR-003](ADR-003-reversibility-encoding.md) | Как кодировать обратимость и откуда берётся гейт |
| [ADR-004](ADR-004-capability-kind-not-only-mcp.md) | Возможность — не обязательно MCP-инструмент |
| [ADR-005](ADR-005-event-scope-instead-of-layer.md) | В событии — `scope` со ссылками вместо номера слоя |
| [ADR-006](ADR-006-drift-levels.md) | Уровни и причины дыр в drift-проверке |
| [ADR-007](ADR-007-two-loops.md) | Конвейер — два цикла: исполнение и поставка |
| [ADR-008](ADR-008-naming.md) | Именование: snake_case, PascalCase, латиница |
| [ADR-009](ADR-009-tools-stdlib-only.md) | Инструменты на стандартной библиотеке, `jsonschema` — опционально |
| [ADR-010](ADR-010-threshold-gate-on-irreversible.md) | Q1. Условный гейт на необратимом — только по порогу ограничения, `AutoApprovedByRule` |
| [ADR-011](ADR-011-four-effect-classes-and-system-level.md) | Q2. Четыре класса эффекта, компенсация-сага, `system_level` |
| [ADR-012](ADR-012-judge-is-not-a-gate.md) | Q3. Судья даёт оценки; решение детерминировано |
| [ADR-013](ADR-013-developer-agent-and-pr-bridge.md) | Q4. Два цикла; агент-разработчик и ревью на слияние |
| [ADR-014](ADR-014-queue-as-entity-with-mandatory-events.md) | Q5. Очередь — сущность, каждый переход — событие, `HumanGateClaimed` |
| [ADR-015](ADR-015-read-events-and-full-llm-log.md) | Q6. Чтения — итог; `llm_step` — полный лог с `model`/`prompt_version` |
| [ADR-016](ADR-016-imports-between-systems.md) | Q7. `imports` между системами |
| [ADR-017](ADR-017-unconfirmed-by-severity.md) | Q8. `unconfirmed` — градация по severity, блок в Static Guard |
| [ADR-018](ADR-018-reference-implementation-boundaries.md) | Границы эталонной реализации `softoscope/`: stdlib, исполнители снаружи, журнал — истина |
| [ADR-020](ADR-020-code-inventory-and-reverse-drift.md) | Реестр кода через `ast`, обратный дрейф `unmodeled` (только WARN), снимок исходника, черновик слоя 5 |
| [ADR-019](ADR-019-agent-merges-by-gates.md) | Агент-разработчик сливает сам по гейтам; человек — в постановке и в исключениях (заменяет ADR-013 п. 3) |
