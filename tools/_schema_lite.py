"""Минимальный валидатор подмножества JSON Schema draft 2020-12 (без зависимостей).

Поддерживает ровно то, что используют схемы в schemas/: type, properties, required,
additionalProperties, patternProperties, propertyNames, enum, const, items, minItems,
maxItems, uniqueItems, pattern, minLength, maxLength, minimum, maximum, minProperties,
anyOf, oneOf, allOf, $ref (только локальные "#/..."). Если установлен пакет
``jsonschema``, tools/validate.py использует его, а этот модуль — запасной путь.
"""

from __future__ import annotations

import re
from typing import Any

_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


class Validator:
    def __init__(self, schema: dict[str, Any]):
        self.root = schema

    def _resolve(self, ref: str) -> dict[str, Any]:
        if not ref.startswith("#/"):
            raise ValueError(f"поддерживаются только локальные $ref, получено {ref!r}")
        node: Any = self.root
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            node = node[part]
        return node

    def iter_errors(self, value: Any, schema: dict[str, Any] | bool | None = None, path: str = "$"):
        if schema is None:
            schema = self.root
        if schema is True:
            return
        if schema is False:
            yield (path, "значение запрещено схемой (false)")
            return
        if "$ref" in schema:
            yield from self.iter_errors(value, self._resolve(schema["$ref"]), path)
            # остальные ключи рядом с $ref тоже применяются (2020-12)
            rest = {k: v for k, v in schema.items() if k != "$ref"}
            if rest:
                yield from self.iter_errors(value, rest, path)
            return

        if "type" in schema:
            types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
            if not any(_TYPE_CHECKS[t](value) for t in types):
                yield (path, f"ожидался тип {'/'.join(types)}, получено {type(value).__name__}")
                return
        if "enum" in schema and value not in schema["enum"]:
            yield (path, f"значение {value!r} не входит в {schema['enum']}")
        if "const" in schema and value != schema["const"]:
            yield (path, f"значение {value!r} != const {schema['const']!r}")

        for key in ("allOf",):
            for sub in schema.get(key, []):
                yield from self.iter_errors(value, sub, path)
        if "anyOf" in schema:
            if not any(not list(self.iter_errors(value, s, path)) for s in schema["anyOf"]):
                yield (path, "ни одна из anyOf-схем не подошла")
        if "oneOf" in schema:
            matched = sum(1 for s in schema["oneOf"] if not list(self.iter_errors(value, s, path)))
            if matched != 1:
                yield (path, f"oneOf: подошло {matched} схем, нужно ровно 1")

        if isinstance(value, str):
            if "minLength" in schema and len(value) < schema["minLength"]:
                yield (path, f"строка короче {schema['minLength']}")
            if "maxLength" in schema and len(value) > schema["maxLength"]:
                yield (path, f"строка длиннее {schema['maxLength']}")
            if "pattern" in schema and not re.search(schema["pattern"], value):
                yield (path, f"строка {value!r} не соответствует шаблону {schema['pattern']}")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in schema and value < schema["minimum"]:
                yield (path, f"{value} < minimum {schema['minimum']}")
            if "maximum" in schema and value > schema["maximum"]:
                yield (path, f"{value} > maximum {schema['maximum']}")
        if isinstance(value, list):
            if "minItems" in schema and len(value) < schema["minItems"]:
                yield (path, f"меньше {schema['minItems']} элементов")
            if "maxItems" in schema and len(value) > schema["maxItems"]:
                yield (path, f"больше {schema['maxItems']} элементов")
            if schema.get("uniqueItems"):
                seen = []
                for item in value:
                    if item in seen:
                        yield (path, f"повторяющийся элемент {item!r}")
                    seen.append(item)
            if "items" in schema:
                for i, item in enumerate(value):
                    yield from self.iter_errors(item, schema["items"], f"{path}[{i}]")
        if isinstance(value, dict):
            if "minProperties" in schema and len(value) < schema["minProperties"]:
                yield (path, f"меньше {schema['minProperties']} свойств")
            for req in schema.get("required", []):
                if req not in value:
                    yield (path, f"нет обязательного поля {req!r}")
            props = schema.get("properties", {})
            pattern_props = schema.get("patternProperties", {})
            for k, v in value.items():
                if "propertyNames" in schema:
                    yield from self.iter_errors(k, schema["propertyNames"], f"{path}.{k}<name>")
                matched = False
                if k in props:
                    matched = True
                    yield from self.iter_errors(v, props[k], f"{path}.{k}")
                for pat, sub in pattern_props.items():
                    if re.search(pat, k):
                        matched = True
                        yield from self.iter_errors(v, sub, f"{path}.{k}")
                if not matched:
                    ap = schema.get("additionalProperties", True)
                    if ap is False:
                        yield (f"{path}.{k}", "неизвестное поле (additionalProperties=false)")
                    elif isinstance(ap, dict):
                        yield from self.iter_errors(v, ap, f"{path}.{k}")


def validate(value: Any, schema: dict[str, Any]) -> list[tuple[str, str]]:
    return list(Validator(schema).iter_errors(value))
