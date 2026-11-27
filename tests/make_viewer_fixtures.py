# SPDX-License-Identifier: MIT
"""Write synthetic published-style data for tests of data viewers (the website).

    python3 tests/make_viewer_fixtures.py OUT_DIR

The tree is made by the real harness code (`tests/synthetic_runs.py`, `splotbench.assemble`,
`assemble.index_entry`), so a viewer that reads it proves compatibility with the contract.
OUT_DIR gets `MANIFEST.txt` and two scenarios, each with its own `index.json`:

    main/    four datasets (see SCENARIOS), `latest_complete` null
    empty/   an index with no datasets

All data is synthetic: origin `synthetic`, decoders `fixture-a`, `fixture-b`, `fixture-c`, machines
`fixture-arm-01` and `fixture-x86-01`. Values are artificial multiples of 5 fps. The output is
deterministic: fixed times, a fixed assembling-harness identity, no current time.

This tree is not a publishable directory. `bench publish` and `validate.published_problems` refuse
synthetic datasets by design, and the index rules allow `latest_complete` only for a complete official
dataset, so it is null here. Validate each `index.json` and each dataset directory with the document
and dataset validators instead (tests/test_viewer_fixtures.py does). The script never writes to
`published/` and refuses an OUT_DIR inside it or an OUT_DIR that is not empty.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(Path(__file__).resolve().parent)]

from splotbench import assemble, provenance  # noqa: E402
from splotbench.util import BenchError, canonical_json, sha256_bytes  # noqa: E402
from synthetic_runs import make_lock, write_run  # noqa: E402

# Fixed identity of the assembling harness, so the dataset ids do not change with the checkout.
HARNESS = {
    "repository": "https://github.com/splotlabs/benchmarks",
    "commit": "0123456789abcdef0123456789abcdef01234567",
    "snapshot_sha256": "sha256:" + "ab" * 32,
    "class": "verified",
}
CASES = (  # id, width, height, bit depth, fps factor (numerator, denominator)
    ("example-1080p-8bit", 1920, 1080, 8, (1, 1)),
    ("example-720p-8bit", 1280, 720, 8, (2, 1)),
    ("example-1080p-10bit", 1920, 1080, 10, (1, 2)),
    ("example-720p-10bit", 1280, 720, 10, (1, 1)),
)
FPS = {  # role -> threads -> implementation -> frames per second of the first case
    "arm64": {1: {"fixture-a": 20, "fixture-b": 10, "fixture-c": 50}, 4: {"fixture-a": 40, "fixture-b": 20, "fixture-c": 100}},
    "x86-64": {1: {"fixture-a": 10, "fixture-b": 20, "fixture-c": 20}, 4: {"fixture-a": 20, "fixture-b": 40, "fixture-c": 50}},
}  # fmt: skip
MACHINES = {"arm64": "fixture-arm-01", "x86-64": "fixture-x86-01"}
CPUS = {"arm64": ("Fixture ARM CPU", ["neon"]), "x86-64": ("Fixture x86 CPU", ["avx2"])}
FRAMES = 10
SECOND = 10**9

# name, measurement date, decoder revision, x86 CPU name, runs to write: role -> state
SCENARIOS = (
    ("complete", "2026-12-01", 1, "Fixture x86 CPU", {"arm64": "complete", "x86-64": "complete"}),
    ("complete-later", "2026-12-08", 2, "Fixture x86 CPU", {"arm64": "complete", "x86-64": "complete"}),
    ("changed-fingerprint", "2026-12-15", 1, "Fixture x86 CPU B", {"arm64": "complete", "x86-64": "complete"}),
    ("partial", "2026-12-22", 1, "Fixture x86 CPU", {"x86-64": "interrupted"}),
)  # fmt: skip


def _hex(*parts: object, length: int) -> str:
    return hashlib.sha1(" ".join(map(str, parts)).encode()).hexdigest()[:length]


def make_fixture_lock(revision: int) -> dict:
    def change(lock):
        base = {entry["identity"]: entry for entry in lock["implementations"]}
        entries = []
        for name, identity, version in (
            ("a", "development", None), ("b", "release", f"1.{1 + revision}.0"), ("c", "release", "2.0.0"),
        ):  # fmt: skip
            entry = copy.deepcopy(base[identity])
            entry.update(id=f"fixture-{name}", name=f"Fixture {name.upper()}", commit=_hex("fixture", name, revision, length=40))
            entry["repository"] = f"https://example.invalid/synthetic/fixture-{name}.git"
            if version:
                tag = f"v{version}"
                entry["release"] = {"tag": tag, "version": version, "url": f"https://example.invalid/synthetic/fixture-{name}/releases/{tag}"}
                entry["select"] = tag
            entries.append(entry)
        lock["implementations"] = entries
        template = lock["suite"]["cases"][0]
        lock["suite"] = {"id": "full", "cases": []}
        for case_id, width, height, depth, _ in CASES:
            case = copy.deepcopy(template)
            raw = width * height * 3 // 2 * FRAMES * (1 if depth == 8 else 2)
            case.update(
                id=case_id, name=f"{case_id}.obu", url=f"https://example.invalid/synthetic/corpus/{case_id}.obu",
                input_sha256="sha256:" + _hex(case_id, "input", length=40).ljust(64, "0"), bytes=1000, width=width, height=height,
                bit_depth=depth, frames=FRAMES, film_grain=None,
            )  # fmt: skip
            case["reference"].update(sha256="sha256:" + _hex(case_id, "reference", length=40).ljust(64, "0"), raw_bytes=raw)
            lock["suite"]["cases"].append(case)
        lock["name"] = "fixture-viewer"
        lock["corpus"]["imported_from"]["repository"] = "https://example.invalid/synthetic/corpus-source.git"
        lock["roles"] = {"arm64": {"arch": "aarch64", "threads": [1, 4]}, "x86-64": {"arch": "x86_64", "threads": [1, 4]}}
        lock["measurement"].update(warmups=0, repetitions=3)

    return make_lock(change)


def cell_values(lock: dict, role: str, revision: int) -> dict:
    cells = {}
    for entry in lock["lock"]["implementations"]:
        for case_id, _, _, _, (numerator, denominator) in CASES:
            for threads in (1, 4):
                fps = FPS[role][threads][entry["id"]] * numerator // denominator
                if revision == 2 and role == "arm64" and entry["id"] == "fixture-a":
                    fps *= 2
                cells[(entry["id"], case_id, threads)] = [SECOND * FRAMES // fps] * 3
    if role == "arm64":
        cells[("fixture-b", "example-1080p-10bit", 4)] = "incorrect"
        cells[("fixture-c", "example-720p-10bit", 1)] = "unsupported"
        wall = cells[("fixture-a", "example-720p-8bit", 4)][0]
        cells[("fixture-a", "example-720p-8bit", 4)] = [wall, "timeout", wall]
    else:
        wall = cells[("fixture-b", "example-1080p-8bit", 1)][0]  # range 300 permille: noisy
        cells[("fixture-b", "example-1080p-8bit", 1)] = [wall, wall * 12 // 10, wall * 9 // 10]
    return cells


def run_edit(role: str, cpu_model: str):
    def edit(documents):
        fingerprint = documents["host"]["fingerprint"]
        fingerprint["os"] = {"name": "FixtureOS", "version": "1.0", "kernel": "fixture-kernel 1.0"}
        fingerprint["cpu"].update(
            model=(cpu_model if role == "x86-64" else CPUS[role][0]), physical_cores=8, logical_cores=8, isa=CPUS[role][1]
        )  # fmt: skip
        fingerprint["memory_bytes"] = 17179869184
        digest = sha256_bytes(canonical_json(fingerprint))
        documents["host"]["fingerprint_digest"] = documents["run"]["fingerprint_digest"] = digest
        if role == "x86-64":  # this machine gives no CPU time and no peak RSS
            for row in documents["attempts"]:
                row["workload"]["metrics"].update(cpu_time=None, peak_rss=None)

    return edit


def generate(out: Path) -> None:
    published = (ROOT / "published").resolve()
    out = Path(out).resolve()
    if out == published or published in out.parents:
        raise BenchError(f"{out} is inside published/. This script never writes there; give another directory.")
    if out.exists() and any(out.iterdir()):
        raise BenchError(f"{out} is not empty. Give an empty or new directory.")
    main, empty = out / "main", out / "empty"
    main.mkdir(parents=True)
    empty.mkdir()
    entries, lines = [], []
    with tempfile.TemporaryDirectory() as temporary:
        for number, (name, date, revision, x86_cpu, runs) in enumerate(SCENARIOS, 1):
            work = Path(temporary, name)
            work.mkdir()
            lock = make_fixture_lock(revision)
            lock_path = work / "LOCK.json"
            lock_path.write_text(json.dumps(lock, indent=2) + "\n")
            bundles = []
            for role, state in runs.items():
                cells = cell_values(lock, role, revision)
                if state == "interrupted":
                    cells.update({key: None for key in list(cells) if key[2] == 4 or key[0] == "fixture-c"})
                    cells[("fixture-b", "example-720p-8bit", 1)] = [SECOND // 10, SECOND // 10]
                run_id = f"{role}-{date.replace('-', '')}T100000Z-{_hex(role, date, length=8)}"
                bundles.append(write_run(
                    work, lock, role, cells, machine_id=MACHINES[role], run_id=run_id, state=state,
                    binary_seed=role, started=f"{date}T10:00:00Z", warmup=False, builds_from_lock=True,
                    edit=run_edit(role, x86_cpu),
                )[0])  # fmt: skip
            target = assemble.assemble(lock_path, bundles, main, harness=HARNESS)
            summary = json.loads((target / "summary.json").read_text())
            published_utc = f"{date[:8]}{int(date[8:]) + 1:02d}T09:00:00Z"
            entries.append({**assemble.index_entry(summary), "published_utc": published_utc})
            lines.append(f"main/datasets/{summary['dataset_id']}  {number}-{name}")
    for directory, datasets in ((main, entries), (empty, [])):
        index = {"schema": "splotbench.index/1", "latest_complete": None, "datasets": datasets}
        (directory / "index.json").write_text(json.dumps(index, indent=2) + "\n")
    commit = provenance.harness_commit() or "unknown"
    (out / "MANIFEST.txt").write_text(
        "Synthetic fixtures for data viewers. Not measurements.\n"
        f"harness commit: {commit}\n"
        "command: python3 tests/make_viewer_fixtures.py OUT_DIR\n"
        + "\n".join(lines)
        + "\nempty/index.json  empty index\n"
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python3 tests/make_viewer_fixtures.py OUT_DIR", file=sys.stderr)
        return 2
    try:
        generate(Path(argv[1]))
    except BenchError as error:
        print(f"make_viewer_fixtures: {error}", file=sys.stderr)
        return error.exit_code
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
