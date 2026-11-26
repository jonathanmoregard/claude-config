"""The advisor verdict's one checker, shared by the artl-verdict MCP server
(which refuses a bad submission) and hooks/artl-evidence.py (which records
only a verdict that passes it).

Driven by skills/advice-refine-test-loop/verdict-schema.json, loaded, not
copied. Stdlib only (hooks run as `python3 -S -E`), so it interprets the small
JSON Schema subset that file uses and raises on any keyword it does not know:
a schema edit the checker cannot enforce fails loudly, never passes silently.
On top of the schema it enforces the verdict/findings agreement the schema
can only state in prose (its `verdict` description).
"""
from __future__ import annotations

import json
from pathlib import Path

SCHEMA_PATH = (Path(__file__).resolve().parents[1]
               / "skills" / "advice-refine-test-loop" / "verdict-schema.json")
ANNOTATIONS = {"$schema", "$comment", "title", "description"}
KEYWORDS = {"type", "enum", "required", "properties", "additionalProperties",
            "items", "minimum", "maxLength"}
TYPES = {"object": dict, "array": list, "string": str, "boolean": bool,
         "null": type(None)}
BLOCKING = ("BLOCKER", "HIGH")


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _is(value: object, name: str) -> bool:
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name not in TYPES:
        raise ValueError(f"unsupported schema type {name!r}")
    return isinstance(value, TYPES[name])


def _check(value: object, schema: dict, where: str, out: list[str]) -> None:
    unknown = set(schema) - KEYWORDS - ANNOTATIONS
    if unknown:
        raise ValueError(f"unsupported schema keyword(s) {sorted(unknown)} at {where}")
    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_is(value, n) for n in names):
            out.append(f"{where}: must be {' or '.join(names)}, got {json.dumps(value)}")
            return
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{where}: must be one of {schema['enum']}, got {json.dumps(value)}")
    if "minimum" in schema and _is(value, "integer") and value < schema["minimum"]:
        out.append(f"{where}: must be >= {schema['minimum']}, got {value}")
    if "maxLength" in schema and isinstance(value, str) and len(value) > schema["maxLength"]:
        out.append(f"{where}: at most {schema['maxLength']} characters, got {len(value)}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                out.append(f"{where}: missing required key {key!r}")
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    out.append(f"{where}: unexpected key {key!r}")
        elif "additionalProperties" in schema:
            raise ValueError(f"only additionalProperties: false is supported, at {where}")
        for key, sub in props.items():
            if key in value:
                _check(value[key], sub, f"{where}.{key}", out)
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            _check(item, schema["items"], f"{where}[{i}]", out)


def schema_problems(value: object, schema: dict) -> list[str]:
    out: list[str] = []
    _check(value, schema, "verdict", out)
    return out


def problems(verdict: object) -> list[str]:
    """Everything wrong with `verdict`; empty means it is a valid verdict."""
    out = schema_problems(verdict, load_schema())
    if out:
        return out
    findings = verdict["findings"]
    expected = ("PASS" if not findings else
                "BLOCKS" if any(f["severity"] in BLOCKING for f in findings) else
                "PASS_WITH_FIXES")
    if verdict["verdict"] != expected:
        out.append(f"verdict: {verdict['verdict']} disagrees with the findings, which make "
                   f"it {expected} (PASS = no findings; BLOCKS = at least one BLOCKER or "
                   "HIGH; PASS_WITH_FIXES = only MEDIUM or LOW)")
    return out
