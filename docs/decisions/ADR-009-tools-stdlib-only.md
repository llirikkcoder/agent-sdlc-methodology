# ADR-009. Инструменты на стандартной библиотеке

**Контекст.** Требование: скрипты без тяжёлых зависимостей, должны реально запускаться.

**Решение.** `tools/validate.py`, `tools/drift.py`, `tools/check_core_purity.py` используют только стандартную библиотеку Python 3.11+. Для проверки по JSON Schema используется пакет `jsonschema`, если он установлен; иначе — встроенный `tools/_schema_lite.py`, покрывающий подмножество draft 2020-12, которым пользуются схемы репозитория (`type`, `properties`, `required`, `additionalProperties`, `patternProperties`, `propertyNames`, `enum`, `const`, `items`, `min/maxItems`, `uniqueItems`, `pattern`, `min/maxLength`, `minimum/maximum`, `minProperties`, `anyOf/oneOf/allOf`, локальные `$ref`).

**Проверка эквивалентности.** `make check` гоняет валидацию обоими путями (`SOFTOSCOPE_NO_JSONSCHEMA=1` включает встроенный). Расхождение результатов — ошибка сборки.

**Ограничение.** Новая конструкция JSON Schema в `schemas/` требует поддержки во встроенном валидаторе; иначе он промолчит там, где `jsonschema` найдёт ошибку. Это цена отсутствия зависимостей; список поддерживаемого — в докстринге `_schema_lite.py`.
