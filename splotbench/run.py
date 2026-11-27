# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""`bench doctor`, `bench run` and `bench bundle`: preflight, the timed phase of decode-cli-wall/1 and bundles.

A run lives in `W/runs/<run_id>/`: run.json, lock.json, host.json, builds.json,
correctness.jsonl, attempts.jsonl (append + fsync per row) and logs/. The runner
holds no decoder knowledge: it calls `corpus.ensure`, `build.ensure`, `verify.run`,
the adapter functions and the matrix of `plan.py`. docs/METHODOLOGY.md has the rules.
"""

from __future__ import annotations

import argparse
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
    """SIGINT or SIGTERM arrived."""


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
    names = ", ".join(f"{m['implementation']} {m['build_id']} ({m['class']})" for m in ctx.builds)
    return "ok", f"{names}; binary digests unchanged"


def verified_rows(work: Path, builds: list[dict]) -> dict:
    """The latest correctness row per (implementation, case, threads) of the given builds."""
    rows = {}
    for manifest in builds:
        for row in _rows(Path(work) / "verify" / f"{manifest['build_id']}.jsonl"):
            rows[_key(row)] = row
    return rows


def _check_correctness(ctx: _Context) -> tuple[str, str]:
    if ctx.builds is None:
        return "warn", "not checked: the builds are not ready"
    rows = verified_rows(ctx.work, ctx.builds)
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
        raise BenchError(f"{ctx.work / host.LOCK_NAME} is held by process {holder}. Wait until it ends.")
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


class _Child:
    """One timed decoder process. `stop` terminates its group, waits GRACE_S, then kills the group."""

    def __init__(self, pid: int) -> None:
        self.pid, self.reason = pid, None
        self.lock, self.exited = threading.Lock(), threading.Event()

    def stop(self, reason: str) -> None:
        with self.lock:  # signal only while the leader is not reaped, so the group id is ours
            if self.exited.is_set() or self.reason:
                return
            self.reason = reason
            verify._signal_group(self.pid, signal.SIGTERM)
        if not self.exited.wait(GRACE_S):
            with self.lock:
                if not self.exited.is_set():
                    verify._signal_group(self.pid, signal.SIGKILL)

    def reaped(self) -> None:
        with self.lock:
            self.exited.set()


class _Stop:
    """Signal handler for SIGINT and SIGTERM. In the timed phase it stops the child group, else it raises."""

    def __init__(self) -> None:
        self.requested, self.child, self.raise_now = False, None, True

    def __call__(self, _number, _frame) -> None:
        self.requested = True
        if self.child is not None:
            self.stop_child()
        elif self.raise_now:
            raise Interrupted

    def stop_child(self) -> None:
        # A thread: the handler runs in the main thread, which must keep waiting for the child.
        threading.Thread(target=self.child.stop, args=("interrupted",), daemon=True).start()

    def check(self) -> None:
        if self.requested:
            raise Interrupted


def _wait_group_gone(pgid: int) -> None:
    # shortcut: a member counts until it is reaped, so a zombie under a non-reaping PID 1 (some containers)
    # stops the run here; scan /proc for zombies if such hosts must be supported.
    deadline = time.monotonic() + GRACE_S
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            pass
        if time.monotonic() > deadline:
            raise BenchError(f"process group {pgid} still has members after SIGKILL. Stop them, then resume the run.")
        time.sleep(POLL_S)


def _attempt(work: Path, argv: list[str], log: Path, timeout_s: int, expected: int, adapter, stop: _Stop) -> tuple:
    """Run one timed decode. Return (outcome, exit code, reported frames, metrics)."""
    nothing = {"wall_time": None, "cpu_time": None, "peak_rss": None, "first_frame_latency": None}
    with open(log, "wb") as file:
        file.write(f"$ {shlex.join(argv)}\n".encode())
        file.flush()
        try:
            started = time.monotonic_ns()
            process = subprocess.Popen(
                argv, cwd=work, stdin=subprocess.DEVNULL, stdout=file, stderr=subprocess.STDOUT, start_new_session=True
            )
        except OSError as error:
            file.write(f"cannot start the decoder: {error}\n".encode())
            return "error", None, None, nothing
        child = _Child(process.pid)
        stop.child = child
        if stop.requested:
            stop.stop_child()
        timer = threading.Timer(timeout_s, child.stop, ("timeout",))
        timer.daemon = True
        timer.start()
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
    verify._signal_group(process.pid, signal.SIGKILL)
    _wait_group_gone(process.pid)
    exit_code = code if code >= 0 else None
    if child.reason:
        return ("timeout" if child.reason == "timeout" else "interrupted"), exit_code, None, nothing
    if code != 0:
        outcome, reported = "error", None
    else:
        reported = adapter.parse_frames(log.read_text(errors="replace").partition("\n")[2])  # not the command line
        outcome = "incorrect" if reported is not None and reported != expected else "ok"
    rss = RSS_UNIT.get(sys.platform)
    metrics = {
        "wall_time": wall,
        "cpu_time": round((usage.ru_utime + usage.ru_stime) * 1e9),
        "peak_rss": usage.ru_maxrss * rss if rss else None,
        "first_frame_latency": None,  # defined, not measured
    }
    return outcome, exit_code, reported, metrics


def _read_once(path: Path) -> None:
    """Read the input once before its attempts, so every attempt finds it in the page cache."""
    with open(path, "rb") as file:
        while file.read(1 << 20):
            pass


# --- the run ---------------------------------------------------------------------


def _create(ctx: _Context, lock_bytes: bytes, host_document: dict, run_class: str) -> tuple[Path, dict]:
    run_id = f"{ctx.role}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{secrets.token_hex(4)}"
    run_dir = ctx.work / "runs" / run_id
    (run_dir / "logs").mkdir(parents=True)
    write_atomic(run_dir / "lock.json", lock_bytes)
    _write_json(run_dir / "host.json", host_document)
    _write_json(run_dir / "builds.json", ctx.builds)
    for name in ("correctness.jsonl", "attempts.jsonl"):
        write_atomic(run_dir / name, b"")
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
    _write_json(run_dir / "run.json", run_doc)
    return run_dir, run_doc


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
    run_doc.update(state="in-progress", finished_utc=None)
    _write_json(path, run_doc)
    return run_dir, run_doc


def _correctness(ctx: _Context, run_dir: Path, stop: _Stop) -> None:
    """Give every cell one correctness row in the run; check the cells without a row for the current builds."""
    path = run_dir / "correctness.jsonl"
    have = {_key(row) for row in _rows(path)}
    latest = verified_rows(ctx.work, ctx.builds)
    for cell in plan.enumerate_cells(ctx.lock, ctx.role):
        key = (cell.implementation, cell.case, cell.threads)
        if key in have:
            continue
        row = latest.get(key)
        if row is None:
            stop.raise_now = True
            try:
                (row,) = verify.run(ctx.lock, ctx.role, ctx.work, case=cell.case, implementation=cell.implementation, threads=cell.threads)
            finally:
                stop.raise_now = False
        verify._append(path, row)
        print(f"correctness {row['implementation']} {row['case_id']} threads={row['threads']}: {row['outcome']}", flush=True)
        stop.check()


def _timed(ctx: _Context, run_dir: Path, run_id: str, stop: _Stop) -> None:
    """Run every remaining attempt. Rows present are never changed; an interrupted attempt is repeated."""
    body = ctx.lock["lock"]
    correctness = {_key(row): row for row in _rows(run_dir / "correctness.jsonl")}
    rows = _rows(run_dir / "attempts.jsonl")
    sequence = max((row["sequence"] for row in rows), default=0)
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
        sequence += 1
        manifest, case = builds[slot.implementation], cases[slot.case]
        adapter = build.adapter_for(ctx.lock, implementations[slot.implementation])
        # Relative paths from the work directory: no absolute path enters the logs.
        argv = adapter.timed_command(Path(manifest["binary_path"]), corpus.cache_path(Path(), case), slot.threads)
        log = run_dir / "logs" / f"{sequence}-{slot.implementation}-{slot.case}-t{slot.threads}.log"
        started = _now()
        outcome, code, reported, metrics = _attempt(
            ctx.work, argv, log, body["measurement"]["timeout_s"], case["frames"], adapter, stop
        )
        row = {
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
            "started_utc": started,
            "outcome": outcome,
            "exit_code": code,
            "workload": {
                "kind": "decode",
                "frames": {"expected": case["frames"], "reported": reported, "source": "lock" if reported is None else "decoder"},
                "metrics": metrics,
            },
        }
        verify._append(run_dir / "attempts.jsonl", row)
        print(f"attempt {sequence} {slot.implementation} {slot.case} threads={slot.threads} {slot.phase} {slot.round}: {outcome}", flush=True)
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
    with host.HostLock(work):
        previous = {number: signal.signal(number, stop) for number in (signal.SIGINT, signal.SIGTERM)}
        run_dir = None
        try:
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
            _correctness(ctx, run_dir, stop)
            stop.raise_now = False
            for manifest in ctx.builds:
                provenance.check_binary(work, manifest)
            _timed(ctx, run_dir, run_doc["run_id"], stop)
            state = "complete"
        except Interrupted:
            if run_dir is None:
                raise BenchError("interrupted before the run started.", 130) from None
            state = "interrupted"
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)
        run_doc.update(state=state, finished_utc=_now())
        _write_json(run_dir / "run.json", run_doc)
    run_id = run_doc["run_id"]
    rows = len(_rows(run_dir / "attempts.jsonl"))
    print(f"run {run_id}: {state}, {rows} attempt rows")
    print(f"bundle it with: bench bundle --work {shlex.quote(str(work))} --run {run_id} -o {run_id}.tar.gz")
    if state == "interrupted":
        raise BenchError(
            f"interrupted. Run {run_id} keeps its rows. Continue it with `bench run --lock {shlex.quote(str(lock_path))} "
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
        "SIGINT or SIGTERM stops the decoder, keeps the rows, marks the run interrupted and exits 130.",
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
