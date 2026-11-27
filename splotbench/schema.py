# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Load splotbench documents and validate them against `schemas/`.

The validator implements only the JSON Schema keywords that the schemas use.
Loading a schema with any other keyword is an error, so a schema can never
rely on a check that does not run. `discriminator` is a splotbench keyword:
`{"property": "kind", "mapping": {"decode": {...}}}` selects the subschema by
the value of `property` and reports an unknown value as unsupported.
"""

from __future__ import annotations

import json
import math
import re
from functools import cache
from pathlib import Path

from .util import BenchError, canonical_json, sha256_bytes

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"
COMMON = "common.schema.json"
SCHEMA_ID = re.compile(r"^splotbench\.([a-z]+)/([0-9]+)$")

ANNOTATIONS = {"$schema", "$id", "$defs", "$comment", "title", "description"}
KEYWORDS = {
    "type",
    "const",
    "enum",
    "pattern",
    "minimum",
    "maximum",
    "items",
    "minItems",
    "properties",
    "required",
    "additionalProperties",
    "propertyNames",
    "anyOf",
    "$ref",
    "discriminator",
}

JSON_TYPES = {
    "null": lambda v: v is None,
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "string": lambda v: isinstance(v, str),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


# --- strict JSON loading ------------------------------------------------------


def _reject_constant(name: str) -> float:
    raise ValueError(f"non-finite number {name} is not allowed")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"number {text} is out of range")
    return value


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def loads(text: str | bytes) -> object:
    """Parse JSON. NaN, Infinity, out-of-range numbers and duplicate keys raise ValueError."""
    if isinstance(text, bytes):
        text = text.decode("utf-8")
    try:
        return json.loads(
            text, parse_constant=_reject_constant, parse_float=_finite_float, object_pairs_hook=_unique_keys
        )
    except RecursionError:
        raise ValueError("the document is nested too deeply") from None


# --- schema registry ----------------------------------------------------------


def _check_keywords(schema: object, where: str, refs: list[tuple[str, str]] | None = None) -> None:
    """Refuse keywords that the validator does not implement; collect every `$ref`."""
    if not isinstance(schema, dict):
        raise BenchError(f"{where}: a schema must be an object")
    unknown = sorted(set(schema) - KEYWORDS - ANNOTATIONS)
    if unknown:
        raise BenchError(f"{where}: the validator does not implement keyword(s) {', '.join(unknown)}")
    if refs is not None and "$ref" in schema:
        refs.append((where, schema["$ref"]))
    for name, sub in schema.get("$defs", {}).items():
        _check_keywords(sub, f"{where}/$defs/{name}", refs)
    for name, sub in schema.get("properties", {}).items():
        _check_keywords(sub, f"{where}/properties/{name}", refs)
    for keyword in ("items", "propertyNames"):
        if keyword in schema:
            _check_keywords(schema[keyword], f"{where}/{keyword}", refs)
    if isinstance(schema.get("additionalProperties"), dict):
        _check_keywords(schema["additionalProperties"], f"{where}/additionalProperties", refs)
    for index, sub in enumerate(schema.get("anyOf", [])):
        _check_keywords(sub, f"{where}/anyOf/{index}", refs)
    if "discriminator" in schema:
        for name, sub in schema["discriminator"]["mapping"].items():
            _check_keywords(sub, f"{where}/discriminator/mapping/{name}", refs)


def load_schema_file(path: Path, refs: list[tuple[str, str]] | None = None) -> dict:
    schema = loads(path.read_bytes())
    _check_keywords(schema, path.name, refs)
    return schema


def _pointer(schemas: dict[str, dict], document: dict, ref: str) -> dict:
    file_part, _, pointer = ref.partition("#")
    node = schemas[file_part] if file_part else document
    for part in filter(None, pointer.split("/")):
        node = node[part]
    return node


@cache
def registry() -> dict[str, dict]:
    """Return every schema file by its `$id`: the common file and one file per data kind."""
    schemas, refs = {}, {}
    for path in sorted(SCHEMA_DIR.glob("*.schema.json")):
        found: list[tuple[str, str]] = []
        schema = load_schema_file(path, found)
        schemas[schema["$id"]] = schema
        refs[schema["$id"]] = found
    for schema_id, found in refs.items():
        for where, ref in found:
            try:
                _pointer(schemas, schemas[schema_id], ref)
            except (KeyError, TypeError):
                raise BenchError(f"{where}: $ref {ref!r} does not resolve") from None
    return schemas


def known_ids() -> list[str]:
    return sorted(schema_id for schema_id in registry() if SCHEMA_ID.match(schema_id))


# --- validation ---------------------------------------------------------------


class _Validator:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def resolve(self, ref: str, base: dict) -> tuple[dict, dict]:
        file_part = ref.partition("#")[0]
        return _pointer(registry(), base, ref), registry()[file_part] if file_part else base

    def check(self, value: object, schema: dict, path: str, base: dict) -> None:
        types = schema.get("type")
        if types is not None:
            types = [types] if isinstance(types, str) else types
            if not any(JSON_TYPES[name](value) for name in types):
                self.errors.append(f"{path}: expected {' or '.join(types)}, found {_type_name(value)}")
                return
        if "$ref" in schema:
            target, document = self.resolve(schema["$ref"], base)
            self.check(value, target, path, document)
        if "const" in schema and not _same(value, schema["const"]):
            self.errors.append(f"{path}: expected {json.dumps(schema['const'])}, found {json.dumps(value)}")
        if "enum" in schema and not any(_same(value, option) for option in schema["enum"]):
            options = ", ".join(json.dumps(option) for option in schema["enum"])
            self.errors.append(f"{path}: {json.dumps(value)} is not one of {options}")
        if isinstance(value, str) and "pattern" in schema and not re.search(schema["pattern"], value):
            self.errors.append(f"{path}: {json.dumps(value)} does not match {schema['pattern']}")
        if JSON_TYPES["number"](value):
            if "minimum" in schema and value < schema["minimum"]:
                self.errors.append(f"{path}: {value} is less than {schema['minimum']}")
            if "maximum" in schema and value > schema["maximum"]:
                self.errors.append(f"{path}: {value} is more than {schema['maximum']}")
        if isinstance(value, list):
            if len(value) < schema.get("minItems", 0):
                self.errors.append(f"{path}: needs at least {schema['minItems']} item(s)")
            if "items" in schema:
                for index, item in enumerate(value):
                    self.check(item, schema["items"], f"{path}[{index}]", base)
        if isinstance(value, dict):
            self.check_object(value, schema, path, base)
        if "anyOf" in schema:
            self.check_any(value, schema["anyOf"], path, base)

    def check_object(self, value: dict, schema: dict, path: str, base: dict) -> None:
        for name in schema.get("required", []):
            if name not in value:
                self.errors.append(f"{path}: missing required field {name!r}")
        properties = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for name, item in value.items():
            if "propertyNames" in schema:
                self.check(name, schema["propertyNames"], f"{path} key {name!r}", base)
            if name in properties:
                self.check(item, properties[name], f"{path}.{name}", base)
            elif extra is False:
                self.errors.append(f"{path}: unknown field {name!r}")
            elif isinstance(extra, dict):
                self.check(item, extra, f"{path}.{name}", base)
        if "discriminator" in schema:
            field = schema["discriminator"]["property"]
            mapping = schema["discriminator"]["mapping"]
            if field not in value:
                self.errors.append(f"{path}: missing required field {field!r}")
            elif not isinstance(value[field], str) or value[field] not in mapping:
                self.errors.append(
                    f"{path}.{field}: unsupported {field} {json.dumps(value[field])}; "
                    f"this harness reads {', '.join(mapping)}"
                )
            else:
                self.check(value, mapping[value[field]], path, base)

    def check_any(self, value: object, options: list[dict], path: str, base: dict) -> None:
        attempts = []
        for option in options:
            trial = _Validator()
            trial.check(value, option, path, base)
            if not trial.errors:
                return
            attempts.append(trial.errors)
        self.errors.extend(min(attempts, key=len))


def _same(value: object, expected: object) -> bool:
    return value == expected and type(value) is type(expected)


def _type_name(value: object) -> str:
    return next(name for name, matches in JSON_TYPES.items() if matches(value))


def schema_id_problem(document: object) -> str | None:
    """Return why the document's `schema` field cannot be read, or None when it can."""
    if not isinstance(document, dict):
        return f"$: expected an object with a 'schema' field, found {_type_name(document)}"
    schema_id = document.get("schema")
    if not isinstance(schema_id, str):
        return "$: missing the 'schema' field (for example \"splotbench.lock/1\")"
    match = SCHEMA_ID.match(schema_id)
    known = known_ids()
    kinds = {SCHEMA_ID.match(known_id).group(1): known_id for known_id in known}
    if match is None or match.group(1) not in kinds:
        return f"$.schema: unknown schema kind {schema_id!r}; this harness reads {', '.join(known)}"
    if schema_id not in registry():
        return (
            f"$.schema: unsupported schema version {schema_id!r}; "
            f"this harness reads {kinds[match.group(1)]}. Use a harness that supports it."
        )
    return None


# --- rules that a schema cannot state ------------------------------------------


def _provenance_problems(path: str, entry: dict) -> list[str]:
    problems = []
    if entry["build_origin"] == "upstream-release-asset":
        problems.append(f"{path}.build_origin: 'upstream-release-asset' is not supported. Build from source.")
    if (entry["identity"] == "release") != (entry["release"] is not None):
        problems.append(f"{path}.identity: 'release' needs a release object, and a release object needs 'release'")
    return problems


def _lock_rules(document: dict) -> list[str]:
    digest = sha256_bytes(canonical_json(document["lock"]))
    if document["lock_digest"] != digest:
        return [f"$.lock_digest: {document['lock_digest']} does not match the lock content ({digest})"]
    return []


def _build_rules(document: dict) -> list[str]:
    from .provenance import build_id, build_origin, clean_at

    source = document["source"]
    problems = _provenance_problems("$.source", source)
    expected = build_id(document)
    if document["build_id"] != expected:
        problems.append(f"$.build_id: {document['build_id']} does not match the build content ({expected})")
    if document["class"] == "verified" and not clean_at(source["snapshot"], source["commit"]):
        problems.append("$.class: 'verified' needs a clean Git snapshot at source.commit")
    origin = build_origin(source["release"], source["snapshot"], source["commit"])
    if source["build_origin"] != "upstream-release-asset" and source["build_origin"] != origin:
        problems.append(f"$.source.build_origin: expected {origin!r} for this release and snapshot")
    return problems


def _host_rules(document: dict) -> list[str]:
    digest = sha256_bytes(canonical_json(document["fingerprint"]))
    if document["fingerprint_digest"] != digest:
        return [f"$.fingerprint_digest: does not match the fingerprint ({digest})"]
    return []


def _summary_rules(document: dict) -> list[str]:
    problems = []
    for index, entry in enumerate(document["implementations"]):
        problems += _provenance_problems(f"$.implementations[{index}]", entry)
    known = {
        "machine_id": {machine["machine_id"] for machine in document["machines"]},
        "implementation": {entry["id"] for entry in document["implementations"]},
        "baseline": {entry["id"] for entry in document["implementations"]},
        "case_id": {case["id"] for case in document["cases"]},
        "build_id": {build["build_id"] for build in document["builds"]} | {None},
    }
    for section in ("builds", "cells", "speedups", "aggregates"):
        for index, row in enumerate(document[section]):
            for field, values in known.items():
                if field in row and row[field] not in values:
                    problems.append(f"$.{section}[{index}].{field}: {row[field]!r} is not defined in this summary")
    return problems


def _index_rules(document: dict) -> list[str]:
    latest = document["latest_complete"]
    complete = {entry["dataset_id"] for entry in document["datasets"] if entry["complete"] and entry["origin"] == "official"}
    if latest is not None and latest not in complete:
        return [f"$.latest_complete: {latest!r} is not a complete official dataset of this index"]
    return []


RULES = {
    "splotbench.lock/1": _lock_rules,
    "splotbench.build/1": _build_rules,
    "splotbench.host/1": _host_rules,
    "splotbench.summary/1": _summary_rules,
    "splotbench.index/1": _index_rules,
}


def validate(document: object) -> list[str]:
    """Return one message per problem; an empty list means the document is valid."""
    problem = schema_id_problem(document)
    if problem:
        return [problem]
    schema = registry()[document["schema"]]
    validator = _Validator()
    validator.check(document, schema, "$", schema)
    if validator.errors or document["schema"] not in RULES:
        return validator.errors
    return RULES[document["schema"]](document)
