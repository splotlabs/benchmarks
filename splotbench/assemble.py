# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""`bench assemble`: check run bundles against a lock and build a dataset directory.

The dataset is a pure function of the lock, the selected bundles, the origin and the
aggregation version. No wall-clock time enters it. See docs/FORMATS.md for the rules.
"""

from __future__ import annotations

import argparse
import csv
import io
import itertools
import json
import math
import os
import shutil
import statistics
from dataclasses import dataclass
from pathlib import Path

from . import bundle, plan, provenance, schema
from .util import BenchError, canonical_json, sha256_bytes

AGGREGATION = "summary/1"
CELL_FIELDS = (
    "machine_id", "case_id", "threads", "implementation", "build_id", "status", "n_measured", "n_valid",
    "noisy", "frames", "wall_ns_median", "fps_median", "fps_min", "fps_max", "fps_mad", "cpu_ns_median",
    "peak_rss_bytes_median", "first_frame_latency_ns", "scaling_vs_1t",
)  # fmt: skip
# Precedence among failed attempts (warm-ups included). A failed correctness row is its own status.
ATTEMPT_FAILURES = ("incorrect", "timeout", "error", "unsupported")
METRICS = {
    "throughput": {"unit": "frames/s", "direction": "higher"},
    "wall_time": {"unit": "ns", "direction": "lower"},
    "cpu_time": {"unit": "ns", "direction": "lower"},
    "peak_rss": {"unit": "bytes", "direction": "lower"},
    "first_frame_latency": {"unit": "ns", "direction": "lower"},
}
DOWNLOADS = {"manifest": "manifest.json", "csv": "summary.csv", "lock": "lock.json", "checksums": bundle.CHECKSUMS}


@dataclass
class Imported:
    path: Path
    data: bytes
    digest: str
    run: dict
    host: dict
    builds: dict  # implementation id -> build manifest
    correctness: dict  # (implementation, case, threads) -> row
    attempts: dict  # (implementation, case, threads) -> attempt rows of both phases


def _campaign_id(lock_doc: dict) -> str:
    return f"{lock_doc['lock']['name']}-{lock_doc['lock_digest'].removeprefix('sha256:')[:12]}"


def _load_lock(path: Path) -> tuple[dict, bytes]:
    try:
        data = path.read_bytes()
        document = schema.loads(data)
    except (OSError, ValueError) as error:
        raise BenchError(f"cannot read the lock {path}: {error}. Give the LOCK.json that `bench lock` wrote.") from None
    problems = schema.validate(document)
    if problems:
        raise BenchError(f"invalid lock {path}:\n  " + "\n  ".join(problems))
    return document, data


def _rows(data: bytes) -> list:
    return [schema.loads(line) for line in data.decode().splitlines()]


def _import(path: Path, lock_doc: dict) -> Imported:
    """Read a bundle and check that it belongs to the lock. Raise BenchError with every problem."""
    data = path.read_bytes()
    members = bundle.read_bundle(data, path)
    lock = lock_doc["lock"]
    embedded = schema.loads(members["lock.json"])
    if embedded["lock_digest"] != lock_doc["lock_digest"]:
        differing = sorted(k for k in lock.keys() | embedded["lock"].keys() if lock.get(k) != embedded["lock"].get(k))
        raise BenchError(
            f"{path}: the bundle was made for another lock ({embedded['lock_digest']}, the given lock is "
            f"{lock_doc['lock_digest']}). Differing parts: {', '.join(differing)}. "
            "Assemble only bundles of one campaign, or give the lock of this bundle."
        )
    run, host = schema.loads(members["run.json"]), schema.loads(members["host.json"])
    builds_list = schema.loads(members["builds.json"])
    problems = []
    campaign = _campaign_id(lock_doc)
    if run["lock_digest"] != lock_doc["lock_digest"]:
        problems.append(f"run.json lock_digest {run['lock_digest']} differs from the lock")
    if run["campaign_id"] != campaign:
        problems.append(f"run.json campaign_id {run['campaign_id']} differs from {campaign}")
    role = lock["roles"].get(run["role"])
    if role is None:
        problems.append(f"role {run['role']!r} is not in the lock (roles: {', '.join(sorted(lock['roles']))})")
    elif host["fingerprint"]["arch"] != role["arch"]:
        problems.append(
            f"host architecture {host['fingerprint']['arch']} does not match role {run['role']} ({role['arch']}). "
            "Run the role on a machine with the right architecture."
        )
    synthetic = lock["origin"] == "synthetic"
    for what, value in [(f"run {run['run_id']}", run["class"])] + [(f"build {b['build_id']}", b["class"]) for b in builds_list]:
        if synthetic != (value == "synthetic"):
            problems.append(
                f"{what} has class {value}, but the lock origin is {lock['origin']}: "
                f"{'a synthetic lock needs synthetic' if synthetic else 'a standard lock allows no synthetic'} runs and builds"
            )
    if host["machine_id"] != run["machine_id"] or host["fingerprint_digest"] != run["fingerprint_digest"]:
        problems.append("host.json machine_id or fingerprint_digest differs from run.json")

    implementations = {entry["id"]: entry for entry in lock["implementations"]}
    cases = {case["id"]: case for case in lock["suite"]["cases"]}
    builds, by_id = {}, {}
    for build in builds_list:
        entry = implementations.get(build["implementation"])
        if entry is None:
            problems.append(f"build {build['build_id']}: implementation {build['implementation']!r} is not in the lock")
        elif build["source"]["commit"] != entry["commit"]:
            problems.append(
                f"build {build['build_id']}: source commit {build['source']['commit']} differs from the lock "
                f"commit {entry['commit']} of {build['implementation']}"
            )
        if build["implementation"] in builds:
            problems.append(f"two builds for implementation {build['implementation']!r}; a run holds one")
        builds[build["implementation"]] = by_id[build["build_id"]] = build
    if sorted(by_id) != sorted(run["build_ids"]):
        problems.append("builds.json and run.json build_ids list different builds")

    def known(row, where):
        if row.get("run_id", run["run_id"]) != run["run_id"]:
            problems.append(f"{where}: run_id {row['run_id']} is not {run['run_id']}")
        if row["build_id"] not in by_id:
            problems.append(f"{where}: unknown build_id {row['build_id']}")
        elif by_id[row["build_id"]]["implementation"] != row["implementation"]:
            problems.append(f"{where}: build {row['build_id']} is not a build of {row['implementation']}")
        if row["case_id"] not in cases:
            problems.append(f"{where}: unknown case {row['case_id']!r}")
        if role is not None and row["threads"] not in role["threads"]:
            problems.append(f"{where}: thread budget {row['threads']} is not in role {run['role']}")

    correctness = {}
    for number, row in enumerate(_rows(members["correctness.jsonl"]), 1):
        where = f"correctness.jsonl line {number}"
        known(row, where)
        key = (row["implementation"], row["case_id"], row["threads"])
        if key in correctness:
            problems.append(f"{where}: second correctness row for {key}")
        correctness[key] = row
    attempts, sequences, ok_counts = {}, set(), {}
    for number, row in enumerate(_rows(members["attempts.jsonl"]), 1):
        where = f"attempts.jsonl line {number}"
        known(row, where)
        if row["attempt_id"] != f"{row['run_id']}#{row['sequence']}":
            problems.append(f"{where}: attempt_id {row['attempt_id']} does not match sequence {row['sequence']}")
        if row["sequence"] in sequences:
            problems.append(f"{where}: sequence {row['sequence']} appears twice")
        sequences.add(row["sequence"])
        key = (row["implementation"], row["case_id"], row["threads"])
        if key not in correctness or correctness[key]["outcome"] != "ok":
            problems.append(f"{where}: attempt of {key} without a correctness row with outcome ok")
        wall = row["workload"]["metrics"]["wall_time"]
        frames = row["workload"]["frames"]
        if row["outcome"] == "ok" and (wall is None or wall < 1):
            problems.append(f"{where}: outcome ok needs a wall_time of at least 1 ns")
        if frames["source"] == "decoder" and frames["reported"] is None:
            problems.append(f"{where}: frames.source is decoder but no frame count was reported")
        if row["outcome"] == "ok" and row["case_id"] in cases:
            locked = cases[row["case_id"]]["frames"]
            if frames["expected"] != locked or frames["reported"] not in (None, locked):
                problems.append(
                    f"{where}: outcome ok with frames expected {frames['expected']}, reported {frames['reported']}, "
                    f"but the lock has {locked} frames. A short decode must have outcome incorrect; "
                    "this bundle is inconsistent."
                )
            ok_counts[key, row["phase"]] = ok_counts.get((key, row["phase"]), 0) + 1
        attempts.setdefault(key, []).append(row)
    required = {"warmup": lock["measurement"]["warmups"], "measure": lock["measurement"]["repetitions"]}
    for (key, phase), count in sorted(ok_counts.items()):
        if count > required[phase]:
            problems.append(f"{key}: {count} ok {phase} attempts, the lock requires {required[phase]}")
    if problems:
        raise BenchError(f"{path}: incompatible bundle:\n  " + "\n  ".join(problems))
    return Imported(path, data, sha256_bytes(data), run, host, builds, correctness, attempts)


def _select_runs(runs: dict, select: dict) -> tuple[list[Imported], list[Imported]]:
    by_role: dict[str, list[Imported]] = {}
    for imported in sorted(runs.values(), key=lambda r: r.run["run_id"]):
        by_role.setdefault(imported.run["role"], []).append(imported)
    for role, run_id in select.items():
        if role not in by_role:
            raise BenchError(f"--select {role}={run_id}: no imported bundle has role {role}.")
        if run_id not in {r.run["run_id"] for r in by_role[role]}:
            have = ", ".join(r.run["run_id"] for r in by_role[role])
            raise BenchError(f"--select {role}={run_id}: no imported run with that id for role {role} (have: {have}).")
    selected, excluded = [], []
    for role in sorted(by_role):
        candidates = by_role[role]
        if len(candidates) > 1 and role not in select:
            ids = ", ".join(r.run["run_id"] for r in candidates)
            raise BenchError(
                f"role {role} has {len(candidates)} different runs ({ids}). "
                f"Choose one with --select {role}=RUN_ID. The others stay in the dataset as excluded evidence."
            )
        chosen = select.get(role, candidates[0].run["run_id"])
        selected += [r for r in candidates if r.run["run_id"] == chosen]
        excluded += [r for r in candidates if r.run["run_id"] != chosen]
    machines = [r.run["machine_id"] for r in selected]
    if len(set(machines)) != len(machines):
        raise BenchError(f"two selected runs use one machine_id ({', '.join(sorted(machines))}). Select one run per host.")
    return selected, excluded


def _cell(lock: dict, run: Imported, implementation: str, case: dict, threads: int) -> dict:
    measurement = lock["measurement"]
    key = (implementation, case["id"], threads)
    build = run.builds.get(implementation)
    correctness, attempts = run.correctness.get(key), run.attempts.get(key, [])
    complete = run.run["state"] == "complete"
    measured = [a for a in attempts if a["phase"] == "measure"]
    valid = [a for a in measured if a["outcome"] == "ok"]
    warmed = [a for a in attempts if a["phase"] == "warmup" and a["outcome"] == "ok"]
    cell = dict.fromkeys(CELL_FIELDS)
    cell.update(
        machine_id=run.run["machine_id"], case_id=case["id"], threads=threads, implementation=implementation,
        build_id=build["build_id"] if build else None, n_measured=len(measured), n_valid=len(valid), noisy=False,
    )  # fmt: skip
    if correctness is None:
        cell["status"] = "not-run" if complete else "interrupted"
    elif correctness["outcome"] != "ok":
        cell["status"] = correctness["outcome"]
    elif failed := next((o for o in ATTEMPT_FAILURES if any(a["outcome"] == o for a in attempts)), None):
        cell["status"] = failed
    elif len(valid) == measurement["repetitions"] and len(warmed) == measurement["warmups"]:
        cell["status"] = "ok"  # interrupted attempts are ignored: a resumed run repeats them
    else:
        cell["status"] = "error" if complete else "interrupted"
    if cell["status"] != "ok":
        return cell
    walls = [a["workload"]["metrics"]["wall_time"] for a in valid]
    fps = [case["frames"] / (w / 1e9) for w in walls]
    wall_median, fps_median = float(statistics.median(walls)), float(statistics.median(fps))

    def median_or_none(metric):
        values = [a["workload"]["metrics"][metric] for a in valid]
        return None if None in values else float(statistics.median(values))

    cell.update(
        frames=case["frames"], wall_ns_median=wall_median, fps_median=fps_median, fps_min=min(fps), fps_max=max(fps),
        fps_mad=float(statistics.median(abs(value - fps_median) for value in fps)),
        noisy=(max(walls) - min(walls)) * 1000 > measurement["noise_max_range_permille"] * wall_median,
        cpu_ns_median=median_or_none("cpu_time"), peak_rss_bytes_median=median_or_none("peak_rss"),
    )  # fmt: skip
    return cell


def _status_counts(cells: list[dict], missing: int) -> dict:
    counts = {}
    for cell in cells:
        counts[cell["status"]] = counts.get(cell["status"], 0) + 1
    if missing:
        counts["not-run"] = counts.get("not-run", 0) + missing
    return dict(sorted(counts.items()))


def _aggregates(lock: dict, machines: list[str], cells: dict) -> list[dict]:
    result = []
    ids = [entry["id"] for entry in lock["implementations"]]
    cases = lock["suite"]["cases"]
    for machine in machines:
        threads_list = sorted({t for (m, _, t, _) in cells if m == machine})
        for threads, depth in itertools.product(threads_list, sorted({c["bit_depth"] for c in cases})):
            wanted = [c["id"] for c in cases if c["bit_depth"] == depth]
            for implementation, baseline in itertools.permutations(ids, 2):
                ratios, excluded = [], []
                for case_id in wanted:
                    a, b = (cells[(machine, case_id, threads, i)] for i in (implementation, baseline))
                    if a["status"] == b["status"] == "ok":
                        ratios.append(a["fps_median"] / b["fps_median"])
                    else:
                        reasons = [f"{i}: {c['status']}" for i, c in ((implementation, a), (baseline, b)) if c["status"] != "ok"]
                        excluded.append({"case_id": case_id, "reason": ", ".join(reasons)})
                result.append({
                    "machine_id": machine, "threads": threads, "bit_depth": depth, "implementation": implementation,
                    "baseline": baseline,
                    "geomean_ratio": math.exp(math.fsum(map(math.log, ratios)) / len(ratios)) if ratios else None,
                    "n_matched": len(ratios), "n_expected": len(wanted), "excluded": excluded,
                })  # fmt: skip
    return result


def _csv(cells: list[dict]) -> bytes:
    def text(value):
        return "" if value is None else str(value).lower() if isinstance(value, bool) else str(value)

    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(CELL_FIELDS)
    writer.writerows([text(cell[field]) for field in CELL_FIELDS] for cell in cells)
    return out.getvalue().encode()


def _json(document: dict) -> bytes:
    return (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode()


def _series_id(lock: dict, run: dict, host: dict) -> str:
    basis = {
        "corpus": lock["corpus"]["digest"], "methodology": lock["methodology"], "build_policy": lock["build_policy"],
        "suite": lock["suite"], "measurement": lock["measurement"], "machine_id": run["machine_id"],
        "fingerprint_digest": host["fingerprint_digest"],
    }  # fmt: skip
    return "ser-" + sha256_bytes(canonical_json(basis)).removeprefix("sha256:")[:16]


def running_harness() -> dict:
    """Identity of the harness checkout that runs this code: `verified` only for a clean Git checkout at a commit."""
    source = provenance.snapshot(provenance.HARNESS_ROOT)
    return {
        "repository": provenance.HARNESS_REPOSITORY, "commit": source["commit"],
        "snapshot_sha256": source["snapshot_sha256"], "class": provenance.identity_class(source, source["commit"]),
    }  # fmt: skip


def _official_problems(lock: dict, selected: list[Imported], cells: list[dict], missing: list[str], harness: dict) -> list[str]:
    problems = ["the lock is synthetic; synthetic data is never official"] if lock["origin"] == "synthetic" else []
    if harness["class"] != "verified":
        problems.append(
            f"the assembling harness has class {harness['class']}, not verified. "
            "Run assemble from a clean Git checkout at a commit."
        )
    problems += [f"role {role} has no run" for role in missing]
    for imported in selected:
        run = imported.run
        if run["state"] != "complete":
            problems.append(f"run {run['run_id']} is {run['state']}, not complete")
        if run["class"] != "verified":
            problems.append(f"run {run['run_id']} has class {run['class']}, not verified")
        problems += [
            f"build {b['build_id']} of run {run['run_id']} has class {b['class']}, not verified"
            for b in imported.builds.values()
            if b["class"] != "verified"
        ]
        problems += [
            f"run {run['run_id']} has no build of {entry['id']}"
            for entry in lock["implementations"]
            if entry["id"] not in imported.builds
        ]
    unfinished = sum(cell["status"] in ("not-run", "interrupted") for cell in cells)
    if unfinished:
        problems.append(f"{unfinished} cells are not-run or interrupted")
    return problems


def assemble(
    lock_path: Path, bundles: list[Path], out: Path, select: dict | None = None, official: bool = False,
    harness: dict | None = None,
) -> Path:  # fmt: skip
    """Build the dataset directory under `out`/datasets and return its path.

    `harness` is the identity of the assembling harness (see `running_harness`); tests inject it.
    """
    lock_doc, lock_bytes = _load_lock(lock_path)
    lock = lock_doc["lock"]
    runs: dict[str, Imported] = {}
    for path in bundles:
        imported = _import(path, lock_doc)
        previous = runs.setdefault(imported.run["run_id"], imported)
        if previous.digest != imported.digest:
            raise BenchError(
                f"conflict: {previous.path} and {path} both have run id {imported.run['run_id']} but different content. "
                "Run ids are unique; one of the bundles was changed or is a different run."
            )
    selected, excluded = _select_runs(runs, select or {})
    snapshot = lock["harness"]["snapshot"]
    if snapshot["commit"] is None:
        raise BenchError("the lock has no harness commit. Make the lock with a harness that has a Git commit.")
    harness = harness or running_harness()
    if lock["origin"] == "synthetic":
        harness = {**harness, "class": "synthetic"}

    cases = lock["suite"]["cases"]
    ids = [entry["id"] for entry in lock["implementations"]]
    by_id = {case["id"]: case for case in cases}
    cells = [
        _cell(lock, imported, cell.implementation, by_id[cell.case], cell.threads)
        for imported in selected
        for cell in plan.enumerate_cells(lock_doc, imported.run["role"])
    ]
    index = {(c["machine_id"], c["case_id"], c["threads"], c["implementation"]): c for c in cells}
    present = {imported.run["role"] for imported in selected}
    missing = sorted(set(lock["roles"]) - present)
    missing_cells = sum(len(plan.enumerate_cells(lock_doc, role)) for role in missing)
    counts = _status_counts(cells, missing_cells)
    complete = not missing and all(i.run["state"] == "complete" for i in selected)
    complete = complete and not counts.get("not-run") and not counts.get("interrupted")
    if official:
        problems = _official_problems(lock, selected, cells, missing, harness)
        if problems:
            raise BenchError("cannot assemble an official dataset:\n  " + "\n  ".join(problems))
    origin = "synthetic" if lock["origin"] == "synthetic" else "official" if official else "local"

    for cell in cells:
        base = index.get((cell["machine_id"], cell["case_id"], 1, cell["implementation"]))
        if cell["status"] == "ok" and base and base["status"] == "ok":
            cell["scaling_vs_1t"] = cell["fps_median"] / base["fps_median"]
    speedups = []
    for a in cells:
        for baseline in ids:
            b = index[(a["machine_id"], a["case_id"], a["threads"], baseline)]
            if a["status"] == b["status"] == "ok" and baseline != a["implementation"]:
                speedups.append({
                    "machine_id": a["machine_id"], "case_id": a["case_id"], "threads": a["threads"],
                    "implementation": a["implementation"], "baseline": baseline, "ratio": a["fps_median"] / b["fps_median"],
                })  # fmt: skip

    dataset_id = "ds-" + sha256_bytes(canonical_json({
        "lock_digest": lock_doc["lock_digest"],
        "runs": [{"run_id": i.run["run_id"], "bundle": i.digest} for i in sorted(selected, key=lambda i: i.run["run_id"])],
        "excluded": [{"run_id": i.run["run_id"], "bundle": i.digest} for i in sorted(excluded, key=lambda i: i.run["run_id"])],
        "aggregation": AGGREGATION, "origin": origin, "harness": harness["snapshot_sha256"],
    })).removeprefix("sha256:")[:16]  # fmt: skip
    campaign_id = _campaign_id(lock_doc)

    def sized(path: str, data: bytes) -> dict:
        return {"path": path, "sha256": sha256_bytes(data), "bytes": len(data)}

    def run_entry(imported: Imported) -> dict:
        run = imported.run
        return {
            "run_id": run["run_id"], "role": run["role"], "machine_id": run["machine_id"], "state": run["state"],
            "class": run["class"], "bundle": sized(f"bundles/{run['run_id']}.tar.gz", imported.data),
        }  # fmt: skip

    machines = []
    for imported in selected:
        run, host = imported.run, imported.host
        starts = sorted(a["started_utc"] for rows in imported.attempts.values() for a in rows if a["phase"] == "measure")
        machines.append({
            "machine_id": run["machine_id"], "role": run["role"], "series_id": _series_id(lock, run, host),
            "run_id": run["run_id"], "run_state": run["state"], "class": run["class"],
            "threads": sorted(lock["roles"][run["role"]]["threads"]),
            "measured_utc": {"first": starts[0] if starts else None, "last": starts[-1] if starts else None},
            "fingerprint_digest": host["fingerprint_digest"], "fingerprint": host["fingerprint"],
            "bundle": run_entry(imported)["bundle"],
        })  # fmt: skip
    implementations = []
    for entry in lock["implementations"]:
        found = [(i.run["role"], i.builds[entry["id"]]) for i in selected if entry["id"] in i.builds]
        if not found:
            raise BenchError(f"no selected run has a build of {entry['id']}. Run `bench prepare` for every role.")
        build = found[0][1]
        for role, other in found[1:]:
            first, second = build["source"], other["source"]
            for field, one, two in (
                ("commit", first["commit"], second["commit"]),
                ("snapshot_sha256", first["snapshot"]["snapshot_sha256"], second["snapshot"]["snapshot_sha256"]),
            ):
                if one != two:
                    raise BenchError(
                        f"the source of {entry['id']} differs between runs: {field} {one} (role {found[0][0]}) "
                        f"and {two} (role {role}). A clean checkout of one commit has one snapshot on every host; "
                        "one host built modified source."
                    )
        implementations.append({
            "id": entry["id"], "name": entry["name"], "repository": entry["repository"], "commit": entry["commit"],
            "identity": entry["identity"], "release": entry["release"], "build_origin": build["source"]["build_origin"],
            "snapshot_sha256": build["source"]["snapshot"]["snapshot_sha256"],
        })  # fmt: skip
    builds = [
        {"build_id": b["build_id"], "implementation": b["implementation"], "machine_id": i.run["machine_id"],
         "class": b["class"], "binary_sha256": b["binary_sha256"], "libraries": b["libraries"],
         "version_output": b["version_output"], "target": b["target"], "toolchain": b["toolchain"],
         "configuration": b["configuration"]}
        for i in selected for b in (i.builds[x] for x in ids if x in i.builds)
    ]  # fmt: skip
    measurement = lock["measurement"]
    summary = {
        "schema": "splotbench.summary/1", "dataset_id": dataset_id, "campaign_id": campaign_id, "name": lock["name"],
        "lock_digest": lock_doc["lock_digest"], "origin": origin, "complete": complete, "workload": {"kind": "decode"},
        "methodology": {
            "id": lock["methodology"], "timing_boundary": measurement["timing_boundary"],
            "film_grain": measurement["film_grain"], "output_policy": measurement["output_policy"],
            "frame_delay": measurement["frame_delay"], "warmups": measurement["warmups"],
            "repetitions": measurement["repetitions"], "timeout_s": measurement["timeout_s"],
            "noise_max_range_permille": measurement["noise_max_range_permille"], "build_policy": lock["build_policy"],
        },
        "aggregation": {"version": AGGREGATION, "harness": harness},
        "corpus": {k: lock["corpus"][k] for k in ("id", "digest", "source", "oracle")},
        "suite": {"id": lock["suite"]["id"], "cases_expected": len(cases)},
        "metrics": METRICS, "machines": machines, "implementations": implementations, "builds": builds,
        "cases": [
            {k: case[k] for k in ("id", "name", "width", "height", "bit_depth", "chroma", "frames", "bytes",
                                  "input_sha256", "url", "film_grain")}
            for case in cases
        ],
        "cells": cells, "speedups": speedups,
        "aggregates": _aggregates(lock, [m["machine_id"] for m in machines], index),
        "downloads": DOWNLOADS,
        "reproduce": {
            "repository": harness["repository"], "harness_commit": snapshot["commit"],
            "instructions": f"{harness['repository']}/blob/{snapshot['commit']}/docs/METHODOLOGY.md",
        },
    }  # fmt: skip

    files = {
        "summary.json": _json(summary),
        "summary.csv": _csv(cells),
        "lock.json": lock_bytes,
        **{f"bundles/{i.run['run_id']}.tar.gz": i.data for i in selected + excluded},
    }
    manifest = {
        "schema": "splotbench.dataset/1", "dataset_id": dataset_id, "campaign_id": campaign_id, "name": lock["name"],
        "lock_digest": lock_doc["lock_digest"], "origin": origin, "complete": complete,
        "aggregation": {"version": AGGREGATION, "harness": harness},
        "runs": [run_entry(i) for i in selected],
        "coverage": {
            "roles_expected": sorted(lock["roles"]), "roles_present": sorted(present),
            "cells_expected": len(plan.enumerate_cells(lock_doc)),
            "cells_ok": counts.get("ok", 0), "status_counts": counts,
        },
        "files": [sized(path, data) for path, data in sorted(files.items())],
    }  # fmt: skip
    if excluded:
        manifest["excluded_runs"] = [{**run_entry(i), "reason": "not selected with --select"} for i in excluded]
    files["manifest.json"] = _json(manifest)
    files[bundle.CHECKSUMS] = bundle.checksums({p: d for p, d in files.items() if p != bundle.CHECKSUMS})
    for name, document in (("summary.json", summary), ("manifest.json", manifest)):
        problems = schema.validate(document)
        if problems:  # a defect of this module, not of the input
            raise BenchError(f"internal error: the generated {name} is invalid:\n  " + "\n  ".join(problems))
    return _write(out / "datasets" / dataset_id, files)


def _write(target: Path, files: dict[str, bytes]) -> Path:
    if target.exists():
        existing = {
            p.relative_to(target).as_posix(): p.read_bytes() for p in sorted(target.rglob("*")) if p.is_file()
        }
        if existing == files:
            return target
        differing = sorted(p for p in existing.keys() | files.keys() if existing.get(p) != files.get(p))
        raise BenchError(
            f"{target} exists with different content ({', '.join(differing)}). "
            "Remove it or use another output directory; datasets are never overwritten."
        )
    temporary = target.with_name(f".{target.name}.tmp")
    shutil.rmtree(temporary, ignore_errors=True)
    try:
        for path, data in files.items():
            (temporary / path).parent.mkdir(parents=True, exist_ok=True)
            (temporary / path).write_bytes(data)
        os.rename(temporary, target)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return target


def index_entry(summary: dict) -> dict:
    """Return the `index.datasets[]` entry of a dataset, without `published_utc` (the publisher adds it)."""
    starts = [m["measured_utc"]["first"] for m in summary["machines"] if m["measured_utc"]["first"]]
    ends = [m["measured_utc"]["last"] for m in summary["machines"] if m["measured_utc"]["last"]]
    return {
        "dataset_id": summary["dataset_id"], "campaign_id": summary["campaign_id"], "name": summary["name"],
        "origin": summary["origin"], "complete": summary["complete"], "workload": summary["workload"]["kind"],
        "methodology": summary["methodology"]["id"], "suite": summary["suite"]["id"],
        "measured_utc": {"first": min(starts, default=None), "last": max(ends, default=None)},
        "path": f"datasets/{summary['dataset_id']}/",
        "series": [{"machine_id": m["machine_id"], "role": m["role"], "series_id": m["series_id"]} for m in summary["machines"]],
    }  # fmt: skip


def _parse_select(entries: list[str]) -> dict:
    select = {}
    for entry in entries:
        role, separator, run_id = entry.partition("=")
        if not separator or not role or not run_id or role in select:
            raise BenchError(f"--select {entry!r}: use ROLE=RUN_ID once per role.", 2)
        select[role] = run_id
    return select


def _assemble(args: argparse.Namespace) -> int:
    target = assemble(args.lock, args.bundles, args.output, _parse_select(args.select), args.official)
    document = schema.loads((target / "manifest.json").read_bytes())
    state = "complete" if document["complete"] else "incomplete"
    print(f"{target}: {document['origin']} dataset, {state}, {document['coverage']['cells_ok']} ok cells")
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "assemble",
        help="check run bundles and assemble a dataset directory",
        description="Check each BUNDLE against the lock and write OUT/datasets/<dataset_id>/ with the manifest, "
        "summary.json, summary.csv, the lock, the bundles and SHA256SUMS. Exit 1 when a bundle is incompatible "
        "or the official checks fail; nothing is written then.",
    )
    parser.add_argument("--lock", required=True, type=Path, metavar="LOCK", help="the campaign lock (LOCK.json)")
    parser.add_argument("bundles", nargs="+", type=Path, metavar="BUNDLE")
    parser.add_argument("-o", "--output", required=True, type=Path, metavar="OUT", help="output root directory")
    parser.add_argument("--select", action="append", default=[], metavar="ROLE=RUN_ID", help="choose one run of a role")
    parser.add_argument("--official", action="store_true", help="require every role, complete verified runs")
    parser.set_defaults(handler=_assemble)
