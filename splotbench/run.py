# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""`bench doctor`, `bench run` and `bench bundle`: preflight, the timed phase of decode-cli-wall/1 and bundles.

A run lives in `W/runs/<run_id>/`: run.json, lock.json, host.json, builds.json,
correctness.jsonl, attempts.jsonl (append + fsync per row) and logs/. The runner
holds no decoder knowledge: it calls `corpus.ensure`, `build.ensure`, `verify.run`,
the adapter functions and the matrix of `plan.py`. docs/METHODOLOGY.md has the rules.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import NamedTuple

from . import build, bundle, corpus, host, plan, provenance, schema, verify
from .util import BenchError, write_atomic

GRACE_S = 2
POLL_S = 0.01
MIN_FREE_BYTES = 1 << 30
FAILED = ("incorrect", "timeout", "error")
# ru_maxrss unit per platform: bytes on macOS, KiB on Linux.
RSS_UNIT = {"darwin": 1, "linux": 1024}


class Interrupted(KeyboardInterrupt):
    """SIGINT, SIGTERM or SIGHUP arrived."""


class Slot(NamedTuple):
    case: str
    threads: int
    phase: str
    round: int
    implementation: str


def _is_run_id(text: str) -> bool:
    return re.fullmatch(schema.registry()[schema.COMMON]["$defs"]["run_id"]["pattern"], text) is not None


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _key(row: dict) -> tuple[str, str, int]:
    return row["implementation"], row["case_id"], row["threads"]


def _rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for number, line in enumerate(path.read_bytes().splitlines(), 1):
        try:
            rows.append(schema.loads(line))
        except ValueError as error:
            raise BenchError(f"{path} line {number} is not valid JSON ({error}). Rows are never repaired; start a new run.") from None
    return rows


def _write_json(path: Path, document: object) -> None:
    write_atomic(path, (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode())


# --- order: seeded-rotation/1 ---------------------------------------------------


def _rank(seed: int, *parts: object) -> str:
    return hashlib.sha256("/".join(map(str, (seed, *parts))).encode()).hexdigest()


def schedule(lock: dict, role: str) -> list[Slot]:
    """Every timed attempt of `role` in execution order (policy seeded-rotation/1, docs/METHODOLOGY.md).

    Cases sort by sha256("<seed>/case/<case id>"). Per case the thread budgets ascend.
    Per case and budget the rounds are the warm-ups, then the repetitions. The base order of the
    implementations sorts by sha256("<seed>/<case id>/<threads>/<implementation id>"); round k
    (counted from 0 over both phases) starts at position k mod n of the base order.
    """
    measurement = lock["lock"]["measurement"]
    seed = measurement["order"]["seed"]
    groups: dict[tuple[str, int], list[str]] = {}
    for cell in plan.enumerate_cells(lock, role):
        groups.setdefault((cell.case, cell.threads), []).append(cell.implementation)
    rounds = [("warmup", n) for n in range(1, measurement["warmups"] + 1)]
    rounds += [("measure", n) for n in range(1, measurement["repetitions"] + 1)]
    slots = []
    for (case, threads), implementations in sorted(groups.items(), key=lambda g: (_rank(seed, "case", g[0][0]), g[0][1])):
        base = sorted(implementations, key=lambda i: _rank(seed, case, threads, i))
        for index, (phase, number) in enumerate(rounds):
            shift = index % len(base)
            slots += [Slot(case, threads, phase, number, i) for i in base[shift:] + base[:shift]]
    return slots


# --- preflight checks (bench doctor; bench run stops on the first failure) -----


class _Context:
    def __init__(self, lock: dict, role: str, work: Path, machine_id: str | None) -> None:
        self.lock, self.role, self.work, self.machine_id = lock, role, Path(work), machine_id
        self.facts = self.harness = self.inputs = self.builds = None
        self.pass_fds: tuple[int, ...] = ()


def _check_architecture(ctx: _Context) -> tuple[str, str]:
    ctx.facts = host.fingerprint()
    arch = host.check_role(ctx.lock, ctx.role)
    return "ok", f"native {arch}, not emulated; role {ctx.role} needs {arch}"


def _check_machine_id(ctx: _Context) -> tuple[str, str]:
    if ctx.machine_id is None:
        return "warn", "not given; `bench run` needs --machine-id"
    host.check_machine_id(ctx.machine_id)
    return "ok", ctx.machine_id


def _check_harness(ctx: _Context) -> tuple[str, str]:
    body = ctx.lock["lock"]
    locked = body["harness"]["snapshot"]["commit"]
    ctx.harness = provenance.harness_identity(locked)
    if ctx.harness["class"] != "verified":
        return "warn", (
            f"this checkout ({ctx.harness['commit'] or 'no Git commit'}) is not a clean checkout of the lock "
            f"harness commit {locked}; results are class development and no official run is possible"
        )
    if body["origin"] == "synthetic":
        return "ok", f"clean checkout of the lock commit {locked}; the lock is synthetic, so no official run"
    return "ok", f"clean checkout of the lock commit {locked}; an official run is possible with verified builds"


def _check_tools(ctx: _Context) -> tuple[str, str]:
    arch = ctx.lock["lock"]["roles"][ctx.role]["arch"]
    tools = {tool for entry in ctx.lock["lock"]["implementations"] for tool in build.adapter_for(ctx.lock, entry).tools(arch)}
    missing = sorted(tool for tool in tools if shutil.which(tool) is None)
    if missing:
        return "warn", f"not on PATH: {', '.join(missing)}; `bench prepare` needs them to build"
    return "ok", f"on PATH: {', '.join(sorted(tools))}"


def _check_corpus(ctx: _Context) -> tuple[str, str]:
    ctx.inputs = corpus.ensure(ctx.lock, ctx.work, offline=True)
    return "ok", f"{len(ctx.inputs)} streams verified (size and SHA-256)"


def _check_builds(ctx: _Context) -> tuple[str, str]:
    ctx.builds = build.ensure(ctx.lock, ctx.role, ctx.work, offline=True)
    lines = "".join(
        f"\n  build {m['implementation']} {m['source']['commit']} {m['source']['identity']} {m['build_id']} {m['binary_sha256']}"
        for m in ctx.builds
    )
    return "ok", f"{len(ctx.builds)} builds, class {', '.join(sorted({m['class'] for m in ctx.builds}))}; binary digests unchanged{lines}"


def verified_rows(lock: dict, work: Path, builds: list[dict]) -> dict:
    """The latest correctness row per (implementation, case, threads) of the given builds for the lock cases.

    A row counts only when it was checked against the reference of the case in this lock.
    """
    cases = {case["id"]: case for case in lock["lock"]["suite"]["cases"]}
    rows = {}
    for manifest in builds:
        for row in _rows(Path(work) / "verify" / f"{manifest['build_id']}.jsonl"):
            if row["case_id"] in cases and verify.matches_case(row, cases[row["case_id"]]):
                rows[_key(row)] = row
    return rows


def _check_correctness(ctx: _Context) -> tuple[str, str]:
    if ctx.builds is None:
        return "warn", "not checked: the builds are not ready"
    rows = verified_rows(ctx.lock, ctx.work, ctx.builds)
    keys = [(c.implementation, c.case, c.threads) for c in plan.enumerate_cells(ctx.lock, ctx.role)]
    missing = [key for key in keys if key not in rows]
    failed = [key for key in keys if key in rows and rows[key]["outcome"] != "ok"]
    message = f"{len(keys) - len(missing)} of {len(keys)} cells have a row for the current builds"
    if missing:
        message += "; `bench run` checks the others first"
    if failed:
        message += f"; {len(failed)} are not ok and get no timed attempts"
    return ("warn" if missing or failed else "ok"), message


def _check_disk(ctx: _Context) -> tuple[str, str]:
    path = ctx.work.absolute()
    while not path.exists():
        path = path.parent
    free = shutil.disk_usage(path).free
    if free < MIN_FREE_BYTES:
        raise BenchError(f"{free >> 20} MiB free in {path}; rows and logs need at least {MIN_FREE_BYTES >> 20} MiB. Free disk space.")
    return "ok", f"{free >> 20} MiB free"


def _check_cpus(ctx: _Context) -> tuple[str, str]:
    logical, largest = os.cpu_count() or 1, max(ctx.lock["lock"]["roles"][ctx.role]["threads"])
    if logical < largest:
        return "warn", f"{logical} logical CPUs, but the role requests up to {largest} threads; those cells are oversubscribed"
    return "ok", f"{logical} logical CPUs for up to {largest} requested threads"


def _check_host_lock(ctx: _Context) -> tuple[str, str]:
    holder = host.lock_holder(ctx.work)
    if holder:
        raise BenchError(host.held_message(ctx.work / host.LOCK_NAME, holder))
    return "ok", "free"


CHECKS = (
    ("architecture", _check_architecture),
    ("machine id", _check_machine_id),
    ("harness", _check_harness),
    ("tools", _check_tools),
    ("corpus", _check_corpus),
    ("builds", _check_builds),
    ("correctness", _check_correctness),
    ("disk", _check_disk),
    ("cpus", _check_cpus),
    ("host lock", _check_host_lock),
)


def _line(status: str, name: str, message: str) -> None:
    print(f"{status:<4} {name}: {message}", flush=True)


def _doctor(args: argparse.Namespace) -> int:
    try:
        lock = plan.load_lock(args.lock)
        plan.check_role(lock, args.role)
    except BenchError as error:
        _line("fail", "lock", str(error))
        return 1
    _line("ok", "lock", f"campaign {plan.campaign_id(lock)}, role {args.role}")
    ctx, failed = _Context(lock, args.role, args.work, args.machine_id), False
    for name, check in CHECKS:
        try:
            status, message = check(ctx)
        except BenchError as error:
            status, message = "fail", str(error)
        _line(status, name, message)
        failed |= status == "fail"
    if ctx.facts is not None:
        document = host.host_document(args.machine_id or "unset", ctx.facts)
        print(f"fingerprint {document['fingerprint_digest']}")
        print(json.dumps(ctx.facts, indent=2, sort_keys=True))
    return 1 if failed else 0


# --- child processes -------------------------------------------------------------

START = "# attempt "  # first log line: the durable start record of an attempt


class _Child:
    """One timed decoder process. `stop` terminates its group, waits GRACE_S, then kills the group."""

    def __init__(self) -> None:
        self.pid, self.reason = None, None
        self.lock, self.exited = threading.Lock(), threading.Event()

    def stop(self, reason: str) -> None:
        with self.lock:
            if self.exited.is_set() or self.reason:
                return
            self.reason = reason
            if self.pid is None:
                return  # not started yet: `started` stops it
        self._terminate()

    def started(self, pid: int) -> None:
        with self.lock:
            self.pid = pid
            if self.reason is None:
                return
        threading.Thread(target=self._terminate, daemon=True).start()

    def _terminate(self) -> None:
        with self.lock:  # signal only while the leader is not reaped, so the group id is ours
            if self.exited.is_set():
                return
            verify._signal_group(self.pid, signal.SIGTERM)
        if not self.exited.wait(GRACE_S):
            with self.lock:
                if not self.exited.is_set():
                    verify._signal_group(self.pid, signal.SIGKILL)

    def reaped(self) -> None:
        with self.lock:
            self.exited.set()


class _Stop:
    """Handler for SIGINT, SIGTERM and SIGHUP.

    During an attempt it stops the child group. Inside `raising()` (checks and the correctness
    pass) it raises at once. Elsewhere it only records the request; `check()` raises at the next
    safe point, so no directory, row or state file is left half written.
    """

    SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)

    def __init__(self) -> None:
        self.requested, self.child, self.raise_now = False, None, False

    def __call__(self, _number, _frame) -> None:
        self.requested = True
        if self.child is not None:
            # A thread: the handler runs in the main thread, which must keep waiting for the child.
            threading.Thread(target=self.child.stop, args=("interrupted",), daemon=True).start()
        elif self.raise_now:
            raise Interrupted

    @contextlib.contextmanager
    def raising(self):
        self.check()
        self.raise_now = True
        try:
            yield
        finally:
            self.raise_now = False

    def check(self) -> None:
        if self.requested:
            raise Interrupted


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def _kill_group(pgid: int) -> None:
    """Kill the members left in the group of a reaped leader, then wait until none is left."""
    # shortcut: a member counts until it is reaped, so a zombie under a non-reaping PID 1 (some containers)
    # stops the run here; scan /proc for zombies if such hosts must be supported.
    if _group_alive(pgid):
        verify._signal_group(pgid, signal.SIGKILL)
    deadline = time.monotonic() + GRACE_S
    while _group_alive(pgid):
        if time.monotonic() > deadline:
            raise BenchError(f"process group {pgid} still has members after SIGKILL. Stop them, then resume the run.")
        time.sleep(POLL_S)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _attempt(ctx: _Context, argv: list[str], log: Path, record: dict, adapter, stop: _Stop) -> tuple:
    """Run one timed decode. Return (outcome, exit code, reported frames, metrics, detail).

    Before the child starts, the log gets the start record (the row of an interrupted attempt)
    and the command, both fsynced. The log is created exclusively and never truncated.
    """
    nothing = {"wall_time": None, "cpu_time": None, "peak_rss": None, "first_frame_latency": None}
    timeout_s = ctx.lock["lock"]["measurement"]["timeout_s"]
    with open(log, "xb") as file:
        file.write(f"{START}{json.dumps(record, sort_keys=True)}\n$ {shlex.join(argv)}\n".encode())
        file.flush()
        os.fsync(file.fileno())
        _fsync_directory(log.parent)
        child = _Child()
        stop.child = child
        if stop.requested:
            child.stop("interrupted")
        timer = threading.Timer(timeout_s, child.stop, ("timeout",))
        timer.daemon = True
        timer.start()
        try:
            started = time.monotonic_ns()
            process = subprocess.Popen(
                argv, cwd=ctx.work, stdin=subprocess.DEVNULL, stdout=file, stderr=subprocess.STDOUT,
                start_new_session=True, pass_fds=ctx.pass_fds,
            )  # fmt: skip
        except OSError as error:
            timer.cancel()
            stop.child = None
            file.write(f"# harness: cannot start the decoder: {error}\n".encode())
            return "error", None, None, nothing, f"cannot start the decoder: {error}"
        child.started(process.pid)
        try:
            _pid, status, usage = os.wait4(process.pid, 0)
            wall = time.monotonic_ns() - started
        except BaseException:
            verify._signal_group(process.pid, signal.SIGKILL)
            os.waitpid(process.pid, 0)
            raise
        finally:
            child.reaped()
            timer.cancel()
            stop.child = None
        code = process.returncode = os.waitstatus_to_exitcode(status)
        # Descendants share the group: kill them and wait until the group is gone before anything else runs.
        _kill_group(process.pid)
        exit_code, detail = (code if code >= 0 else None), None
        if child.reason:
            outcome, reported, metrics = ("timeout" if child.reason == "timeout" else "interrupted"), None, nothing
        else:
            rss = RSS_UNIT.get(sys.platform)
            metrics = {
                "wall_time": wall,
                "cpu_time": round((usage.ru_utime + usage.ru_stime) * 1e9),
                "peak_rss": usage.ru_maxrss * rss if rss else None,
                "first_frame_latency": None,  # defined, not measured
            }
            reported = None
            if code != 0:
                outcome = "error"
            else:
                file.flush()
                output = log.read_text(errors="replace").split("\n", 2)[-1]  # not the start record and command
                reported = adapter.parse_frames(output)
                if reported is None and adapter.TIMED_REPORTS_FRAMES:
                    outcome, detail = "error", "the decoder printed no frame count, but its timed mode always prints one"
                else:
                    outcome = "incorrect" if reported is not None and reported != record["workload"]["frames"]["expected"] else "ok"
        if detail:
            file.write(f"# harness: {detail}\n".encode())
    verify.cap_log(log)
    return outcome, exit_code, reported, metrics, detail


def _read_once(path: Path) -> None:
    """Read the input once before its attempts, so every attempt finds it in the page cache."""
    with open(path, "rb") as file:
        while file.read(1 << 20):
            pass


# --- the run ---------------------------------------------------------------------


def _create(ctx: _Context, lock_bytes: bytes, host_document: dict, run_class: str) -> tuple[Path, dict]:
    """Write the run directory under a temporary name, then rename it: it exists complete or not at all."""
    run_id = f"{ctx.role}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{secrets.token_hex(4)}"
    runs = ctx.work / "runs"
    run_dir, partial = runs / run_id, runs / f".{run_id}.partial"
    (partial / "logs").mkdir(parents=True)
    write_atomic(partial / "lock.json", lock_bytes)
    _write_json(partial / "host.json", host_document)
    _write_json(partial / "builds.json", ctx.builds)
    for name in ("correctness.jsonl", "attempts.jsonl"):
        write_atomic(partial / name, b"")
    run_doc = {
        "schema": "splotbench.run/1",
        "run_id": run_id,
        "campaign_id": plan.campaign_id(ctx.lock),
        "lock_digest": ctx.lock["lock_digest"],
        "role": ctx.role,
        "machine_id": ctx.machine_id,
        "state": "in-progress",
        "class": run_class,
        "harness": ctx.harness,
        "fingerprint_digest": host_document["fingerprint_digest"],
        "build_ids": [manifest["build_id"] for manifest in ctx.builds],
        "started_utc": _now(),
        "finished_utc": None,
    }
    _write_json(partial / "run.json", run_doc)
    os.rename(partial, run_dir)
    _fsync_directory(runs)
    return run_dir, run_doc


def _recover_partial_line(path: Path) -> None:
    """A crash can leave a last line without its newline. It is not a row: move it to logs/ and cut it off."""
    data = path.read_bytes()
    if not data or data.endswith(b"\n"):
        return
    cut = data.rfind(b"\n") + 1
    logs = path.parent / "logs"
    number = 1
    while (side := logs / f"{path.stem}-partial-{number}.txt").exists():
        number += 1
    write_atomic(side, data[cut:])
    with open(path, "r+b") as file:
        file.truncate(cut)
        file.flush()
        os.fsync(file.fileno())
    print(f"{path.name}: the last line was incomplete ({len(data) - cut} bytes, not a row); moved it to logs/{side.name}", flush=True)


def _resume(ctx: _Context, run_id: str, host_document: dict) -> tuple[Path, dict]:
    run_dir = ctx.work / "runs" / run_id
    path = run_dir / "run.json"
    if not path.is_file():
        raise BenchError(f"no run {run_id} in {run_dir.parent}. Check --work and the run id.")
    data = path.read_bytes()
    problems = bundle.document_problems(path.name, data, ("object", "splotbench.run/1"))
    if problems:
        raise BenchError(f"{path} is invalid ({problems[0]}). Start a new run without --resume.")
    run_doc = schema.loads(data)
    if run_doc["state"] == "complete":
        raise BenchError(f"run {run_id} is complete. Bundle it with `bench bundle`, or start a new run without --resume.")
    recorded_builds = schema.loads((run_dir / "builds.json").read_bytes())
    changed = [
        name
        for name, recorded, current in (
            ("lock digest", run_doc["lock_digest"], ctx.lock["lock_digest"]),
            ("role", run_doc["role"], ctx.role),
            ("machine id", run_doc["machine_id"], ctx.machine_id),
            ("host fingerprint", run_doc["fingerprint_digest"], host_document["fingerprint_digest"]),
            ("harness identity", run_doc["harness"], ctx.harness),
            ("build ids", run_doc["build_ids"], [manifest["build_id"] for manifest in ctx.builds]),
            ("build manifests and binary digests", recorded_builds, ctx.builds),
        )
        if recorded != current
    ]
    if changed:
        raise BenchError(
            f"run {run_id} cannot resume: the {', '.join(changed)} changed since it started. "
            "Start a new run without --resume."
        )
    for name in ("correctness.jsonl", "attempts.jsonl"):
        _recover_partial_line(run_dir / name)
    run_doc.update(state="in-progress", finished_utc=None)
    _write_json(path, run_doc)
    return run_dir, run_doc


def _correctness(ctx: _Context, run_dir: Path, stop: _Stop) -> None:
    """Give every cell one correctness row in the run; check the cells without a matching row for the current builds."""
    path = run_dir / "correctness.jsonl"
    have = {_key(row) for row in _rows(path)}
    latest = verified_rows(ctx.lock, ctx.work, ctx.builds)
    for cell in plan.enumerate_cells(ctx.lock, ctx.role):
        key = (cell.implementation, cell.case, cell.threads)
        if key in have:
            continue
        row = latest.get(key)
        if row is None:
            with stop.raising():
                (row,) = verify.run(
                    ctx.lock, ctx.role, ctx.work, case=cell.case, implementation=cell.implementation,
                    threads=cell.threads, pass_fds=ctx.pass_fds,
                )  # fmt: skip
        verify._append(path, row)
        print(f"correctness {row['implementation']} {row['case_id']} threads={row['threads']}: {row['outcome']}", flush=True)
        stop.check()


def _log_sequence(log: Path) -> int:
    prefix = log.name.partition("-")[0]
    return int(prefix) if prefix.isdigit() else 0


def _lost_attempts(run_dir: Path, rows: list[dict]) -> list[dict]:
    """Rows for attempts that have a start record in their log but no row: the harness ended during them."""
    present = {row["sequence"] for row in rows}
    lost = []
    for log in sorted((run_dir / "logs").glob("*.log"), key=_log_sequence):
        with open(log, "rb") as file:
            first = file.readline().decode("utf-8", "replace")
        if not first.startswith(START) or not first.endswith("\n"):
            continue  # no durable start record: the child never started
        try:
            record = schema.loads(first.removeprefix(START))
        except ValueError:
            continue
        if schema.validate(record) == [] and record["sequence"] not in present:
            lost.append(record)
    return lost


def _timed(ctx: _Context, run_dir: Path, run_id: str, stop: _Stop) -> None:
    """Run every remaining attempt. Rows present are never changed; an interrupted attempt is repeated."""
    body = ctx.lock["lock"]
    attempts = run_dir / "attempts.jsonl"
    rows = _rows(attempts)
    for record in _lost_attempts(run_dir, rows):
        verify._append(attempts, record)
        rows.append(record)
        print(f"attempt {record['sequence']} has a start record but no row; recorded it as interrupted", flush=True)
        stop.check()
    correctness = {_key(row): row for row in _rows(run_dir / "correctness.jsonl")}
    # No sequence number is used twice: a log without a row still holds its number.
    sequence = max([row["sequence"] for row in rows] + [_log_sequence(log) for log in (run_dir / "logs").glob("*.log")] + [0])
    done = {(*_key(row), row["phase"], row["round"]) for row in rows if row["outcome"] == "ok"}
    failed = {_key(row) for row in rows if row["outcome"] in FAILED}
    builds = {manifest["implementation"]: manifest for manifest in ctx.builds}
    implementations = {entry["id"]: entry for entry in body["implementations"]}
    cases = {case["id"]: case for case in body["suite"]["cases"]}
    warm = None
    for slot in schedule(ctx.lock, ctx.role):
        key = (slot.implementation, slot.case, slot.threads)
        # Only cells with an ok correctness row are measured; a failed attempt settles its cell.
        if correctness[key]["outcome"] != "ok" or key in failed or (*key, slot.phase, slot.round) in done:
            continue
        stop.check()
        if warm != slot.case:
            _read_once(ctx.inputs[slot.case])
            warm = slot.case
            stop.check()
        sequence += 1
        manifest, case = builds[slot.implementation], cases[slot.case]
        adapter = build.adapter_for(ctx.lock, implementations[slot.implementation])
        # Relative paths from the work directory: no absolute path enters the logs.
        argv = adapter.timed_command(Path(manifest["binary_path"]), corpus.cache_path(Path(), case), slot.threads)
        log = run_dir / "logs" / f"{sequence}-{slot.implementation}-{slot.case}-t{slot.threads}.log"
        record = {
            "schema": "splotbench.attempt/1",
            "attempt_id": f"{run_id}#{sequence}",
            "run_id": run_id,
            "sequence": sequence,
            "implementation": slot.implementation,
            "build_id": manifest["build_id"],
            "case_id": slot.case,
            "threads": slot.threads,
            "phase": slot.phase,
            "round": slot.round,
            "started_utc": _now(),
            "outcome": "interrupted",
            "exit_code": None,
            "workload": {
                "kind": "decode",
                "frames": {"expected": case["frames"], "reported": None, "source": "lock"},
                "metrics": {"wall_time": None, "cpu_time": None, "peak_rss": None, "first_frame_latency": None},
            },
        }
        outcome, code, reported, metrics, detail = _attempt(ctx, argv, log, record, adapter, stop)
        row = {**record, "outcome": outcome, "exit_code": code}
        row["workload"] = {
            "kind": "decode",
            "frames": {"expected": case["frames"], "reported": reported, "source": "lock" if reported is None else "decoder"},
            "metrics": metrics,
        }
        verify._append(attempts, row)
        note = f" ({detail})" if detail else ""
        print(f"attempt {sequence} {slot.implementation} {slot.case} threads={slot.threads} {slot.phase} {slot.round}: {outcome}{note}", flush=True)
        if outcome in FAILED:
            failed.add(key)
        if outcome == "interrupted":
            raise Interrupted


def execute(lock_path: Path, role: str, work: Path, machine_id: str, resume: str | None = None) -> str:
    """Run the role of the lock on this host (`bench run`). Return the run id."""
    lock_path, work = Path(lock_path), Path(work)
    lock = plan.load_lock(lock_path)
    plan.check_role(lock, role)
    host.check_machine_id(machine_id)
    if resume is not None and not _is_run_id(resume):
        raise BenchError(f"--resume {resume!r} is not a run id like arm64-20260101T000000Z-0123abcd.", 2)
    ctx, stop = _Context(lock, role, work, machine_id), _Stop()
    with host.HostLock(work) as held:
        ctx.pass_fds = (held.descriptor,)  # each decoder keeps the host lock while it lives
        previous = {number: signal.signal(number, stop) for number in stop.SIGNALS}
        run_dir = run_doc = None
        try:
            try:
                with stop.raising():
                    for name, check in CHECKS[:-1]:  # this process holds the host lock
                        status, message = check(ctx)
                        _line(status, name, message)
                host_document = host.host_document(machine_id, ctx.facts)
                synthetic = lock["lock"]["origin"] == "synthetic"
                verified = ctx.harness["class"] == "verified" and all(m["class"] == "verified" for m in ctx.builds)
                run_class = "synthetic" if synthetic else "verified" if verified else "development"
                if resume:
                    run_dir, run_doc = _resume(ctx, resume, host_document)
                else:
                    run_dir, run_doc = _create(ctx, lock_path.read_bytes(), host_document, run_class)
                print(f"run {run_doc['run_id']} ({run_class}) in {run_dir}", flush=True)
                stop.check()
                _correctness(ctx, run_dir, stop)
                for manifest in ctx.builds:
                    provenance.check_binary(work, manifest)
                _timed(ctx, run_dir, run_doc["run_id"], stop)
                state = "complete"
            except Interrupted:
                if run_dir is None:
                    raise BenchError(
                        "interrupted before the run started: no run directory was created and no decoder was started.", 130
                    ) from None
                state = "interrupted"
            run_doc.update(state=state, finished_utc=_now())
            _write_json(run_dir / "run.json", run_doc)
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)
    run_id = run_doc["run_id"]
    rows = len(_rows(run_dir / "attempts.jsonl"))
    checked = len(_rows(run_dir / "correctness.jsonl"))
    print(f"run {run_id}: {state}, {checked} correctness rows, {rows} attempt rows")
    print(f"bundle it with: bench bundle --work {shlex.quote(str(work))} --run {run_id} -o {run_id}.tar.gz")
    if state == "interrupted":
        raise BenchError(
            f"interrupted. Run {run_id} is recorded with {checked} correctness rows and {rows} attempt rows; "
            f"no decoder is running. Continue it with `bench run --lock {shlex.quote(str(lock_path))} "
            f"--role {role} --work {shlex.quote(str(work))} --machine-id {machine_id} --resume {run_id}`.",
            130,
        )
    return run_id


def _run(args: argparse.Namespace) -> int:
    execute(args.lock, args.role, args.work, args.machine_id, args.resume)
    return 0


def _bundle(args: argparse.Namespace) -> int:
    if not _is_run_id(args.run):
        raise BenchError(f"--run {args.run!r} is not a run id like arm64-20260101T000000Z-0123abcd.", 2)
    run_dir = Path(args.work) / "runs" / args.run
    if not (run_dir / "run.json").is_file():
        raise BenchError(f"no run {args.run} in {run_dir.parent}. Check --work and --run.")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    digest = bundle.write_bundle(run_dir, args.output)
    state = schema.loads((run_dir / "run.json").read_bytes())["state"]
    print(f"bundle {args.output}: {digest}, run {args.run}, state {state}")
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--lock", required=True, type=Path, metavar="LOCK", help="the campaign lock")
    common.add_argument("--role", required=True, metavar="ROLE", help="the host role of the lock")
    common.add_argument("--work", required=True, type=Path, metavar="W", help="the prepared work directory")

    parser = subparsers.add_parser(
        "doctor",
        parents=[common],
        help="check that this host can run a role; changes nothing",
        description="Check the lock, the role, the native architecture, the harness identity, the build tools, "
        "the corpus cache, the builds and their binary digests, the correctness rows, free disk space, the "
        "logical CPUs and the host lock. Print one line per check (ok, warn or fail) and the host "
        "fingerprint. Exit 1 when a check fails. Nothing is written.",
    )
    parser.add_argument("--machine-id", metavar="ID", help="the machine id that `bench run` will use")
    parser.set_defaults(handler=_doctor)

    parser = subparsers.add_parser(
        "run",
        parents=[common],
        help="run the correctness pass and the timed phase of a role",
        description="Take the host lock, run the doctor checks, check the cells without a correctness row, "
        "then run every timed attempt of the role (docs/METHODOLOGY.md) into W/runs/<run_id>/. "
        "Nothing is downloaded or built: missing inputs or builds exit 3; run `bench prepare` first. "
        "SIGINT, SIGTERM or SIGHUP stops the decoder, keeps the rows, marks the run interrupted and exits 130.",
    )
    parser.add_argument("--machine-id", required=True, metavar="ID", help="owner-chosen machine name, for example m2max-01")
    parser.add_argument("--resume", metavar="RUN_ID", help="continue this interrupted run")
    parser.set_defaults(handler=_run)

    parser = subparsers.add_parser(
        "bundle",
        help="pack a run directory into a portable bundle",
        description="Write W/runs/RUN_ID/ as a deterministic tar.gz bundle with SHA256SUMS. Complete and "
        "interrupted runs can be bundled; run.json holds the state. Nothing is uploaded.",
    )
    parser.add_argument("--work", required=True, type=Path, metavar="W")
    parser.add_argument("--run", required=True, metavar="RUN_ID")
    parser.add_argument("-o", "--output", required=True, type=Path, metavar="FILE.tar.gz")
    parser.set_defaults(handler=_bundle)
