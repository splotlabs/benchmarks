# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Campaign plan, lock and matrix: `bench lock` and `bench matrix`."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import NamedTuple

from . import __version__, corpus, provenance, schema
from .util import BenchError, canonical_json, sha256_bytes

MAX_TIMEOUT_S = 86400  # one day; the lock schema has the same maximum
PLACEHOLDER_COMMIT = "0" * 40
PLAN_KEYS = {
    "required": ("name", "origin", "suite", "warmups", "repetitions", "timeout_s", "order_seed",
                 "noise_max_range_permille", "implementation", "roles"),
    "optional": ("corpus", "suites"),
}  # fmt: skip
IMPLEMENTATION_KEYS = {
    "required": ("id", "adapter", "repository", "select"),
    "optional": ("name", "release_tag_pattern", "release_url_template"),
}
ROLE_KEYS = {"required": ("arch", "threads"), "optional": ()}


# --- plan ------------------------------------------------------------------------


def _table(value: object, where: str, keys: dict) -> dict:
    if not isinstance(value, dict):
        raise BenchError(f"plan {where}: expected a table. Write it as a TOML table.")
    for key in value:
        if key not in (*keys["required"], *keys["optional"]):
            raise BenchError(f"plan {where}: unknown key {key!r}. Remove it or check the spelling.")
    for key in keys["required"]:
        if key not in value:
            raise BenchError(f"plan {where}: missing key {key!r}. Add it.")
    return value


def _typed(table: dict, key: str, where: str, kind: type, minimum: int | None = None, maximum: int | None = None) -> None:
    value = table.get(key)
    if value is None:
        return
    if not isinstance(value, kind) or isinstance(value, bool):
        raise BenchError(f"plan {where}{key}: expected {'an integer' if kind is int else 'a string'}, found {value!r}.")
    if minimum is not None and value < minimum:
        raise BenchError(f"plan {where}{key}: must be at least {minimum}, found {value}.")
    if maximum is not None and value > maximum:
        raise BenchError(f"plan {where}{key}: must be at most {maximum}, found {value}.")


def parse_plan(path: Path) -> dict:
    """Read a plan file and check its structure. Values are checked later against the lock schema."""
    path = Path(path)
    try:
        plan = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise BenchError(f"cannot read the plan {path}: {error.strerror}. Check the path.") from None
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise BenchError(f"the plan {path} is not valid TOML: {error}. Fix the file.") from None
    _table(plan, "(top level)", PLAN_KEYS)
    for key in ("name", "origin", "suite", "corpus", "suites"):
        _typed(plan, key, "", str)
    for key, minimum in (("warmups", 0), ("repetitions", 1), ("timeout_s", 1), ("order_seed", 0),
                         ("noise_max_range_permille", 0)):  # fmt: skip
        _typed(plan, key, "", int, minimum, MAX_TIMEOUT_S if key == "timeout_s" else None)
    if not isinstance(plan["implementation"], list) or not plan["implementation"]:
        raise BenchError("plan implementation: expected a non-empty list of [[implementation]] tables.")
    ids = set()
    for index, entry in enumerate(plan["implementation"]):
        where = f"[[implementation]] #{index + 1}"
        _table(entry, where, IMPLEMENTATION_KEYS)
        for key in (*IMPLEMENTATION_KEYS["required"], *IMPLEMENTATION_KEYS["optional"]):
            _typed(entry, key, f"{where} ", str)
        if entry["id"] in ids:
            raise BenchError(f"plan {where} id: {entry['id']!r} is used twice. Make ids unique.")
        ids.add(entry["id"])
        if entry.get("release_url_template") and not entry.get("release_tag_pattern"):
            raise BenchError(f"plan {where} release_url_template: needs release_tag_pattern. Add the pattern.")
        if (entry["adapter"] == "fixture") != (plan["origin"] == "synthetic"):
            raise BenchError(
                f"plan {where} adapter: {entry['adapter']!r} does not fit origin {plan['origin']!r}. "
                "Only a synthetic plan may use the 'fixture' adapter, and it may use only that adapter."
            )
        corpus.check_url(entry["repository"], plan["origin"] == "synthetic", f"plan {where} repository")
    if not isinstance(plan["roles"], dict) or not plan["roles"]:
        raise BenchError("plan roles: expected at least one [roles.NAME] table.")
    for name, role in plan["roles"].items():
        where = f"[roles.{name}]"
        _table(role, where, ROLE_KEYS)
        _typed(role, "arch", f"{where} ", str)
        threads = role["threads"]
        if not isinstance(threads, list) or not all(isinstance(t, int) and not isinstance(t, bool) for t in threads):
            raise BenchError(f"plan {where} threads: expected a list of integers, found {threads!r}.")
        if len(set(threads)) != len(threads):
            raise BenchError(f"plan {where} threads: {threads} has a value twice. List each thread count once.")
    return plan


# --- lock ------------------------------------------------------------------------


def lock_document(lock: dict) -> dict:
    """Wrap a lock object in the lock file form. The digest is outside the hashed object."""
    return {"schema": "splotbench.lock/1", "lock_digest": sha256_bytes(canonical_json(lock)), "lock": lock}


def campaign_id(document: dict) -> str:
    """`<plan name>-<first 12 hex of the lock digest>`."""
    return f"{document['lock']['name']}-{document['lock_digest'].removeprefix('sha256:')[:12]}"


def _assemble(plan: dict, base: Path, harness: dict, resolved: list[dict] | None) -> dict:
    manifest = corpus.load_manifest(
        base / plan["corpus"] if "corpus" in plan else corpus.DEFAULT_MANIFEST, synthetic=plan["origin"] == "synthetic"
    )
    suites = corpus.load_suites(base / plan["suites"] if "suites" in plan else corpus.DEFAULT_SUITES)
    cases = corpus.suite_cases(manifest, suites, plan["suite"])
    implementations = []
    for index, entry in enumerate(plan["implementation"]):
        release = {"commit": PLACEHOLDER_COMMIT, "identity": "development", "release": None}
        implementations.append(
            {
                "id": entry["id"],
                "name": entry.get("name", entry["id"]),
                "adapter": entry["adapter"],
                "repository": entry["repository"],
                "select": entry["select"],
                "release_tag_pattern": entry.get("release_tag_pattern"),
                "release_url_template": entry.get("release_url_template"),
                **(resolved[index] if resolved else release),
            }
        )
    return {
        "name": plan["name"],
        "origin": plan["origin"],
        "methodology": "decode-cli-wall/1",
        "harness": {"repository": provenance.HARNESS_REPOSITORY, "version": __version__, "snapshot": harness},
        "build_policy": "optimized-source/1",
        "implementations": implementations,
        "corpus": corpus.lock_corpus(manifest),
        "suite": {"id": plan["suite"], "cases": [corpus.lock_case(manifest, case) for case in cases]},
        "roles": {name: {"arch": role["arch"], "threads": role["threads"]} for name, role in plan["roles"].items()},
        "measurement": {
            "warmups": plan["warmups"],
            "repetitions": plan["repetitions"],
            "timeout_s": plan["timeout_s"],
            "order": {"policy": "seeded-rotation/1", "seed": plan["order_seed"]},
            "noise_max_range_permille": plan["noise_max_range_permille"],
            "film_grain": "apply",
            "output_policy": "raw-discard",
            "frame_delay": "decoder-default",
            "timing_boundary": "process-wall",
        },
    }


def _checked(lock: dict) -> dict:
    document = lock_document(lock)
    problems = schema.validate(document)
    if problems:
        raise BenchError("the plan gives an invalid lock. Fix the plan:\n  " + "\n  ".join(problems))
    return document


def build_lock(plan_path: Path, *, harness: dict | None = None) -> dict:
    """Resolve every implementation once and return the lock document.

    This is the only function that reads moving references (`git ls-remote`).
    `harness` is the harness snapshot; the default is the snapshot of this checkout.
    """
    plan = parse_plan(plan_path)
    base = Path(plan_path).resolve().parent
    harness = harness or provenance.snapshot(provenance.HARNESS_ROOT)
    _checked(_assemble(plan, base, harness, None))  # reject a bad plan before any network use
    resolved = [
        provenance.resolve(e["repository"], e["select"], e.get("release_tag_pattern"), e.get("release_url_template"))
        for e in plan["implementation"]
    ]
    return _checked(_assemble(plan, base, harness, resolved))


def load_lock(path: Path) -> dict:
    """Read a lock file and check schema and digest. Returns the lock document."""
    try:
        data = Path(path).read_bytes()
    except OSError as error:
        raise BenchError(f"cannot read the lock {path}: {error.strerror}. Check the path.") from None
    try:
        document = schema.loads(data)
    except ValueError as error:
        raise BenchError(f"the lock {path} is not valid JSON: {error}. Make the lock again with `bench lock`.") from None
    problems = schema.validate(document)
    if not problems and document["schema"] != "splotbench.lock/1":
        problems = [f"$.schema: {document['schema']!r} is not a lock"]
    if problems:
        raise BenchError(f"the lock {path} is invalid. Do not edit a lock; make it again with `bench lock`:\n  " + "\n  ".join(problems))
    return document


# --- matrix ----------------------------------------------------------------------


class Cell(NamedTuple):
    role: str
    case: str
    threads: int
    implementation: str


def check_role(lock: dict, role: str) -> None:
    if role not in lock["lock"]["roles"]:
        raise BenchError(f"role {role!r} is not in the lock (it has {', '.join(lock['lock']['roles'])}).", 2)


def enumerate_cells(lock: dict, role: str | None = None) -> list[Cell]:
    """Return every expected cell of a lock document, from the lock only.

    Order: roles in lock order, then cases in suite order, then thread counts in
    ascending order, then implementations in lock order. With `role` only that role.
    Each cell gets `attempt_counts(lock)` attempts.
    """
    body = lock["lock"]
    if role is not None:
        check_role(lock, role)
    return [
        Cell(name, case["id"], threads, implementation["id"])
        for name, spec in body["roles"].items()
        if role in (None, name)
        for case in body["suite"]["cases"]
        for threads in sorted(spec["threads"])
        for implementation in body["implementations"]
    ]


def attempt_counts(lock: dict) -> dict[str, int]:
    """Return the untimed warm-up attempts and the timed repetitions that each cell gets."""
    measurement = lock["lock"]["measurement"]
    return {"warmups": measurement["warmups"], "repetitions": measurement["repetitions"]}


# --- commands --------------------------------------------------------------------


def write_new(path: Path, data: bytes) -> None:
    """Create `path` with `data`. Fail when the file exists, also when it appeared a moment ago."""
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        try:
            os.link(temporary, path)  # never replaces an existing file
        except FileExistsError:
            raise BenchError(f"{path} exists. Choose another -o path; a lock is never overwritten.") from None
    finally:
        os.unlink(temporary)


def _lock(args: argparse.Namespace) -> int:
    if args.output.exists():
        raise BenchError(f"{args.output} exists. Choose another -o path; a lock is never overwritten.")
    document = build_lock(args.plan)
    body = document["lock"]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise BenchError(f"{args.output} exists. Choose another -o path; a lock is never overwritten.")
    text = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    write_new(args.output, text.encode("utf-8"))
    snapshot = body["harness"]["snapshot"]
    print(f"campaign {campaign_id(document)}")
    print(f"digest {document['lock_digest']}")
    print(f"suite {body['suite']['id']}, {len(body['suite']['cases'])} cases")
    print(f"harness {snapshot['commit']}" + (" (modified)" if snapshot["dirty"] else ""))
    for entry in body["implementations"]:
        release = entry["release"]
        label = f"release {release['version']} ({release['tag']})" if release else "development"
        print(f"{entry['id']} {entry['commit']} {label}")
    counts = attempt_counts(document)
    for name in body["roles"]:
        cells = len(enumerate_cells(document, name))
        attempts = cells * (counts["warmups"] + counts["repetitions"])
        print(f"role {name}: {cells} cells, {attempts} attempts")
    return 0


def _matrix(args: argparse.Namespace) -> int:
    document = load_lock(args.lock)
    cells = enumerate_cells(document, args.role)
    counts = attempt_counts(document)
    for cell in cells:
        print(f"{cell.role}\t{cell.case}\t{cell.threads}\t{cell.implementation}")
    print(
        f"{len(cells)} cells; each gets {counts['warmups']} warm-up and {counts['repetitions']} timed attempts",
        file=sys.stderr,
    )
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "lock",
        help="resolve a campaign plan into a lock file",
        description="Read PLAN, resolve every implementation selector once with `git ls-remote`, copy the "
        "selected cases from the corpus manifest and write the lock to OUTPUT. This is the only command "
        "that reads moving references. It refuses to overwrite an existing file.",
    )
    parser.add_argument("plan", type=Path, metavar="PLAN.toml")
    parser.add_argument("-o", "--output", required=True, type=Path, metavar="LOCK.json")
    parser.set_defaults(handler=_lock)

    parser = subparsers.add_parser(
        "matrix",
        help="list every expected cell of a lock",
        description="Print one line per expected cell: role, case, threads, implementation. "
        "The summary of attempt counts goes to stderr. Reads the lock only.",
    )
    parser.add_argument("--lock", required=True, type=Path, metavar="LOCK")
    parser.add_argument("--role", metavar="ROLE", help="list only this role")
    parser.set_defaults(handler=_matrix)
