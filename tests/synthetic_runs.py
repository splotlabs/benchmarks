# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Write synthetic run directories and bundles from a compact description. All data is synthetic."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from splotbench import bundle
from splotbench.provenance import build_id
from splotbench.util import canonical_json, sha256_bytes

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FAIL_OUTCOMES = ("incorrect", "unsupported", "timeout", "error")


def make_lock(change=None) -> dict:
    """Return a lock document built from the fixture lock; `change(lock)` edits the lock object first.

    The digest is recomputed, so every edit gives a valid lock with another digest.
    """
    lock = json.loads((FIXTURES / "lock.json").read_text())["lock"]
    if change:
        change(lock)
    return {"schema": "splotbench.lock/1", "lock_digest": sha256_bytes(canonical_json(lock)), "lock": lock}


def campaign_id(lock_doc: dict) -> str:
    return f"{lock_doc['lock']['name']}-{lock_doc['lock_digest'].removeprefix('sha256:')[:12]}"


def full_cells(lock_doc: dict, role: str, overrides: dict | None = None, default=(1_000_000_000,) * 3) -> dict:
    """Return a cell description for every implementation, case and thread budget of `role`."""
    lock = lock_doc["lock"]
    cells = {
        (impl["id"], case["id"], threads): list(default)
        for impl in lock["implementations"]
        for case in lock["suite"]["cases"]
        for threads in lock["roles"][role]["threads"]
    }
    cells.update(overrides or {})
    return cells


def _build_from_lock(entry: dict, template: list) -> dict:
    """A build manifest whose source is exactly the lock entry (commit, identity, release)."""
    build = copy.deepcopy(next((b for b in template if b["source"]["identity"] == entry["identity"]), template[0]))
    build["source"].update(
        repository=entry["repository"], commit=entry["commit"], identity=entry["identity"], release=entry["release"],
        build_origin="source-release" if entry["release"] else "source-development",
    )  # fmt: skip
    build["source"]["snapshot"].update(
        commit=entry["commit"], tree=entry["commit"],
        snapshot_sha256=sha256_bytes(f"snapshot {entry['id']} {entry['commit']}".encode()),
    )  # fmt: skip
    build.update(
        implementation=entry["id"], binary_path=f"build/{entry['id']}/{entry['commit']}/{entry['id']}",
        version_output=f"{entry['id']} synthetic",
    )  # fmt: skip
    return build


def write_run(
    work: Path,
    lock_doc: dict,
    role: str,
    cells: dict,
    *,
    machine_id: str,
    run_id: str,
    state: str = "complete",
    run_class: str = "synthetic",
    binary_seed: str = "",
    arch: str | None = None,
    started: str = "2026-01-01T00:00:00Z",
    edit=None,
    without=(),
    snapshot_sha256=None,
    warmup=True,
    builds_from_lock=False,
) -> tuple[Path, str]:
    """Write a run directory under `work`, pack it as `<run_id>.tar.gz` and return (archive, digest).

    `without` lists implementations that have no build in this run (their cells need no rows).
    `warmup=False` writes no warm-up attempt (the lock must say warmups 0).
    `builds_from_lock` makes every build from the lock entry; implementations that are not in the
    fixture lock always are.
    `snapshot_sha256` replaces the source snapshot digest of every build.
    `edit`, when given, changes the documents before they are written.
    `cells` maps (implementation, case, threads) to one of:
      None            no rows at all (the cell was not run)
      "<outcome>"     a correctness row with that outcome and no attempts
      [item, ...]     correctness ok, one warm-up attempt of 999 s, and one measured attempt per item.
                      An int is a wall time in ns (cpu time = half of it). A (wall, cpu) pair sets the
                      cpu time (None = not given). An outcome string is a failed measured attempt.
    """
    lock = lock_doc["lock"]
    run_dir = work / f"run-{run_id}"
    (run_dir / "logs").mkdir(parents=True)
    (run_dir / "logs" / "synthetic.log").write_text("synthetic log\n")
    cases = {case["id"]: case for case in lock["suite"]["cases"]}
    template = json.loads((FIXTURES / "run" / "builds.json").read_text())
    builds = {}
    for implementation in lock["implementations"]:
        if implementation["id"] in without:
            continue
        known = next((b for b in template if b["implementation"] == implementation["id"]), None)
        build = copy.deepcopy(known) if known and not builds_from_lock else _build_from_lock(implementation, template)
        build["class"] = run_class
        if snapshot_sha256:
            build["source"]["snapshot"]["snapshot_sha256"] = snapshot_sha256
        build["binary_sha256"] = sha256_bytes(f"{implementation['id']}{binary_seed}".encode())
        build["target"] = "synthetic-" + (arch or lock["roles"][role]["arch"])
        build["build_id"] = build_id(build)
        builds[implementation["id"]] = build
    host = json.loads((FIXTURES / "run" / "host.json").read_text())
    host["machine_id"] = machine_id
    host["fingerprint"]["arch"] = arch or lock["roles"][role]["arch"]
    host["fingerprint_digest"] = sha256_bytes(canonical_json(host["fingerprint"]))

    attempts, correctness, sequence = [], [], 0
    started_minute = int(started[11:13]) * 60 + int(started[14:16])

    def attempt(implementation, case, threads, phase, round_number, wall, cpu, outcome):
        nonlocal sequence, started_minute
        sequence += 1
        started_minute += 1
        attempts.append({
            "schema": "splotbench.attempt/1",
            "attempt_id": f"{run_id}#{sequence}",
            "run_id": run_id,
            "sequence": sequence,
            "implementation": implementation,
            "build_id": builds[implementation]["build_id"],
            "case_id": case,
            "threads": threads,
            "phase": phase,
            "round": round_number,
            "started_utc": f"{started[:10]}T{started_minute // 60:02d}:{started_minute % 60:02d}:00Z",
            "outcome": outcome,
            "exit_code": 0 if outcome == "ok" else None,
            "workload": {
                "kind": "decode",
                "frames": {"expected": cases[case]["frames"], "reported": None, "source": "lock"},
                "metrics": {"wall_time": wall, "cpu_time": cpu, "peak_rss": 1 << 20, "first_frame_latency": None},
            },
        })

    for (implementation, case, threads), value in cells.items():
        if value is None or implementation in without:
            continue
        reference = cases[case]["reference"]
        outcome = value if isinstance(value, str) else "ok"
        correctness.append({
            "schema": "splotbench.correctness/1",
            "build_id": builds[implementation]["build_id"],
            "implementation": implementation,
            "case_id": case,
            "threads": threads,
            "outcome": outcome,
            "exit_code": 0 if outcome == "ok" else None,
            "detail": None if outcome == "ok" else f"synthetic {outcome}",
            "workload": {
                "kind": "decode",
                "output_bytes": reference["raw_bytes"] if outcome == "ok" else None,
                "output_sha256": reference["sha256"] if outcome == "ok" else None,
                "reference_bytes": reference["raw_bytes"],
                "reference_sha256": reference["sha256"],
                "frames": cases[case]["frames"] if outcome == "ok" else None,
            },
        })
        if isinstance(value, str):
            continue
        if warmup:
            attempt(implementation, case, threads, "warmup", 1, 999_000_000_000, 1, "ok")
        for round_number, item in enumerate(value, 1):
            if isinstance(item, str):
                attempt(implementation, case, threads, "measure", round_number, None, None, item)
            else:
                wall, cpu = item if isinstance(item, tuple) else (item, item // 2)
                attempt(implementation, case, threads, "measure", round_number, wall, cpu, "ok")

    run = {
        "schema": "splotbench.run/1",
        "run_id": run_id,
        "campaign_id": campaign_id(lock_doc),
        "lock_digest": lock_doc["lock_digest"],
        "role": role,
        "machine_id": machine_id,
        "state": state,
        "class": run_class,
        "harness": {
            "repository": "https://github.com/splotlabs/benchmarks",
            "commit": lock["harness"]["snapshot"]["commit"],
            "snapshot_sha256": lock["harness"]["snapshot"]["snapshot_sha256"],
            "class": run_class,
        },
        "fingerprint_digest": host["fingerprint_digest"],
        "build_ids": [build["build_id"] for build in builds.values()],
        "started_utc": started,
        "finished_utc": None if state == "in-progress" else "2026-01-02T00:00:00Z",
    }
    if edit:  # tests use this to plant a defect; edit() changes the documents in place
        edit({"run": run, "host": host, "builds": list(builds.values()), "attempts": attempts, "correctness": correctness})
    files = {
        "run.json": run,
        "lock.json": lock_doc,
        "host.json": host,
        "builds.json": list(builds.values()),
    }
    for name, document in files.items():
        (run_dir / name).write_text(json.dumps(document, indent=2) + "\n")
    for name, rows in (("attempts.jsonl", attempts), ("correctness.jsonl", correctness)):
        (run_dir / name).write_text("".join(json.dumps(row) + "\n" for row in rows))
    archive = work / f"{run_id}.tar.gz"
    return archive, bundle.write_bundle(run_dir, archive)


def digest_of(archive: Path) -> str:
    return "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()
