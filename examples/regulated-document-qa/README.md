# Пример: regulated-document-qa — вопросно-ответная система по регулируемой документации

Домен: экспертная команда готовит ответы на запросы надзорного органа по регулируемой документации. Система — агентный RAG с моделью-оценщиком, очередью проверки экспертом и журналом аудита; подлежит квалификации. Детали предметной области в файлах слоёв оставлены ровно настолько, насколько нужны, чтобы пример был рабочим.

Пример заполнен **по реальному коду** закрытой пилотной системы (только чтением, 25.10.2026) и затем **обезличен**: имена репозитория, модулей, таблиц, классов и пунктов спецификации заменены (`SPEC-*`), структура привязок, маршрутов и переходов сохранена. Поэтому `drift.py --repo` против кода из этого репозитория не воспроизводится; на момент снятия привязок сверка давала 0 расхождений.

## Что откуда взято

| Слой | Файл | Источник в коде |
|---|---|---|
| 1 Назначение | `purpose.json` | роли — `db/models.py` `User.role` (reviewer/gxp_expert/admin/inspector), `auth/dependencies.py` (`require_expert`, `require_inspector`); ограничения — `agent/gxp_gate.py` (SPEC-G1), `api/routers/prepare.py` (четыре глаза, SPEC-R1/2205), `db/models.py ChatAuditEvent` (append-only), `ingest/pii_exclusion.py` (SPEC-P1) |
| 2 Домен | `domain.json` | таблицы `request_trees`, `request_questions`, `request_nodes`, `fallback_queue`, `chats`, `chat_messages`, `ingest_events`, `answer_records`, `users` (`db/models.py`); состояния узла `idle→draft→review→approved` и переходы — `api/routers/prepare.py`; очередь `pending→claimed→resolved/rejected`, `reopen` — `db/fallback_queue.py` |
| 3 Процессы | `processes.json` | флоу «Подготовка ответа регулятору» — роутер `/prepare`; граф чата — `agent/graph.py` (узлы `query_analysis`, `read_file`, `resolve_search_query`, `query_expansion`, `hyde`, `sgr_search`, `answer_question`, `reflect`); очередь — `/fallback/*`; корпус — `/upload`, `/collection/sources/*`; индексация — `/admin/ingest-*` |
| 4 Возможности | `capabilities.json` | контракты поверх маршрутов, узлов графа и функций; эффект и гейт выведены из кода (удаление документа и экспорт — необратимы) |
| 5 Реализация | `implementation.json` | пути, символы, маршруты, узлы графа, таблицы, флаги (`PREPARE_FLOW_ENABLED`, `GXP_VALIDATION_GATE_ENABLED`, `HUMAN_FALLBACK_ENABLED`, `NOTIFY_ENABLED`) |
| События | `events.sample.json` | типы аудит-событий кода (`gxp_validation_run`, `fallback_*`, `request_node_*`, `document_*`) показаны в `payload.audit_event_type` |

Проверка привязок против кода:

```bash
python3 tools/validate.py examples/regulated-document-qa
python3 tools/drift.py examples/regulated-document-qa --repo /path/to/source-repo
```

На 25.10.2026 drift против кода: 0 расхождений (`stale`), все пути, символы, маршруты, узлы графа и таблицы найдены.

## Чем этот домен проверяет универсальность ядра

- **Возможности не только MCP.** В regulated-document-qa нет MCP-сервера: возможности — HTTP-маршруты, узлы LangGraph, функции и CLI. Ядро этого не знает: `kind` — перечисление, транспорт — данные.
- **Условный гейт.** GxP-гейт срабатывает не всегда, а по вердикту судьи. В ядре это `human_gate.condition` + ограничение `C001` с `enforcement=gate`.
- **Очередь человека как проекция.** `fallback_queue` — это проекция «ожидающих гейтов» (HumanGateRequested без HumanGateResolved); в ядре очередь не называется «экспертной».
- **Производное состояние.** `request_trees.status` вычисляется из узлов (`_derive_request_status`) — в ядре флаг `derived_state`.
- **Четыре глаза.** Правило «автор не согласует своё» — ограничение `pre_check` на возможность, а не особенность ядра.

## Соответствие событий кода контракту события

В коде одно хранилище аудита — `chat_audit_events` с полями `event_type, chat_id, message_id, user_id, changes, meta`. Отображение в контракт `schemas/event.schema.json`:

| Контракт | chat_audit_events |
|---|---|
| `event_type` (PascalCase) | `event_type` (snake_case кода, напр. `request_node_approved`) — в примере хранится в `payload.audit_event_type` |
| `aggregate.id` | `chat_id` / `message_id` / id узла из `changes` |
| `actor.id` | `user_id` |
| `payload.from/to` | `changes[{field:"status", before, after}]` |
| `metadata.trace` | `meta` (ip, user_agent, session_id, request_id) |
| `metadata.correlation_id` | нет прямого поля; ближайшее — `meta.request_id`/`audit_session_id` |

Вывод для продукта (не для ядра): журналу исходной системы не хватает `correlation_id` и `causation_id` сквозь всю цепочку «вопрос → судья → очередь → ответ эксперта»; сейчас связь восстанавливается по `message_id`.

## Не подтверждено (помечено в данных)

- `run_ingest_pass` — скрипт полного прохода (`scripts/index_dossier.py`, `ingest_one_pass.py`) лежит вне `src` и в рамках задачи не открывался; в `src` есть `tasks/ingest_task.py`. Статус привязки `unconfirmed`.
- `notify_review_status` — код webhook-уведомления есть (`db/fallback_notify.py`), по умолчанию выключен; подключённый приёмник не подтверждён. Статус `stub`.
- Таблица `fallback_events` из `docs/architecture/flows/audit-flow.mmd` в моделях отсутствует — действия эксперта пишутся в `chat_audit_events`. Статус `unconfirmed`.
- Действие `escalate` (эскалация на тимлида) из `fallback-flow.mmd` в маршрутах `/fallback/*` отсутствует (есть claim/save/answer/reject/reopen) — в пример не включено.
- `C007` «досье не покидают контур» — открытое решение D-LLM-HOSTING в `docs/architecture/system-spec.md`; записано как `unconfirmed`, `enforcement=audit_only`.
- Имя флага HyDE (`HYDE_ENABLED`) — по функции `hyde_enabled()`, точное имя переменной окружения не сверялось.
- Документ `backend-architecture.md` называет префикс `/api/admin`; в коде `prefix="/admin"` (прокси срезает `/api`). В примере — как в коде.
