# Пример: ai-office — продажи и сделки (MCP-агент)

Домен из исходной спецификации (`source/00-ishodnaya-specifikaciya-2026-10-25.md`, `system_id: ai_office`): ИИ-агент ведёт сделки по воронке, фиксирует квалификацию и оплаты, готовит коммерческие предложения, эскалирует решения владельцу.

Слой 5 опирается на **реальный MCP-сервер `ai-office`**, доступный в сессии 25.10.2026: инструменты `get_deal`, `list_deals`, `create_deal`, `update_deal_status`, `record_qualification`, `create_offer`, `record_payment`, `log_deal_touch`, `create_task`, `flag_for_human`, `delete_contact`. Их схемы и описания прочитаны; код сервера и его база не смотрелись — хранилища помечены `unconfirmed`.

## Отличия от исходной спецификации и почему

| Исходник | Здесь | Причина |
|---|---|---|
| Состояния `Deal`: lead → qualification → proposal → closed_won / closed_lost | Реальные этапы MCP: new_request → processing → qualification → brief → proposal → negotiation → contract → deal → in_work → done; закрытие — rejected / cancelled | Слой 2 обязан описывать то, что принимает `update_deal_status`; иначе drift между моделью и инструментом. Соответствие: lead ≈ new_request/processing, closed_won ≈ done, closed_lost ≈ rejected/cancelled |
| Шаг `step_2` «Отправка счёта» → `mcp_billing_send_invoice` (`finance_mcp.send_invoice`) | Шаг `send_invoice` оставлен, возможность `send_invoice` описана, привязка `status=planned` | Такого инструмента в сессии нет. Это **намеренная дыра**: `tools/drift.py` показывает `not_implemented`, а в `events.sample.json` есть событие `CapabilityMissing` — так выглядит `MissingCapabilityError` на практике |
| `is_reversible: false` на шаге + `type: mutating_irreversible` + `requires_human_gate` на инструменте | Обратимость — свойство возможности (`effect`), гейт — свойство шага (`human_gate`), с правилом согласованности | См. `docs/decisions/ADR-003-reversibility-encoding.md` |
| C001 «КП > 1000 USD без согласования запрещено» | Ограничение с порогом `Offer.amount_usd > 1000 USD`; гейт на `send_offer` **условный** по этому ограничению; ниже порога — `AutoApprovedByRule` в журнале (см. `events.sample.json`, offer-10) | Решение Q1 (ADR-010): условный гейт на необратимом действии допустим только по детерминированному порогу ограничения |

## Что показывает этот пример в ядре

- **Чистый MCP-случай**: все реализованные возможности — `kind: mcp_tool`, `location.mcp = {server, tool}`; имя возможности (`move_deal_stage`) намеренно не совпадает с именем инструмента (`update_deal_status`) — контракт и привязка разделены.
- **Ограничения, которые проверяет сам инструмент** (422 без `loss_reason`, без `next_step`, без продукта) — это `enforcement: pre_check` в слое 1; агент обязан знать их до вызова, а не узнавать из ошибки.
- **«Не выдумывать»** (C005) — ограничение на данные, а не на действие; в событиях видно, что квалификация записана с `source_note`.
- **Эскалация** (`flag_for_human`) — возможность с `external_side_effects`, компенсируемая (отбой сообщением), но не необратимая.

## Находки цифрового двойника (27.10.2026)

`python3 tools/digitize.py twin examples/ai-office-sales` прогнал все активные процессы на заглушках (ADR-021). `deal_onboarding` не доходит до конца, хотя валидатор и drift чистые: каждый шаг по отдельности допустим, но последовательность ломается.
- `draft_offer` создаёт Offer в `draft`, а `send_offer` объявляет переход `active → sent`. Шага `draft → active` в процессе нет, поэтому runtime останавливает процесс на отправке (`transition_not_allowed`).
- `close_won` переводит Deal из `proposal` в `done`, а автомат слоя 2 разрешает `done` только из `in_work`. Этапы `negotiation → contract → deal → in_work` двигает человек вне процесса, и в описании процесса их нет.

Не исправлено: как на самом деле живёт статус предложения в ai-office (`active` перед отправкой обязателен или нет) и где в процессе стоят человеческие этапы, нужно уточнить у владельца.

## Проверка

```bash
python3 tools/validate.py examples/ai-office-sales
python3 tools/drift.py examples/ai-office-sales            # 2 дыры not_implemented — намеренно
python3 tools/drift.py examples/ai-office-sales --strict   # то же, но с кодом возврата 1
```

Сверка `mcp_tool`-привязок с кодом невозможна (`--repo` неприменим): drift помечает их `unverifiable`, а подтверждение — список инструментов сервера на дату `inspected_at`.
