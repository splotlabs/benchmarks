# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""`bench verify`: the untimed correctness pass of methodology decode-cli-wall/1.

For each build, case and thread budget of a role, run the adapter command with
the raw output on a pipe, hash and count the stream without storing it, and
append one `splotbench.correctness/1` row to `W/verify/<build_id>.jsonl`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import select
import shlex
import signal
import subprocess
import time
from pathlib import Path

from . import adapters, build, bundle, provenance, schema
from .util import EXIT_FAILURE, EXIT_PREREQUISITE, BenchError, canonical_json, sha256_file

CHUNK = 1 << 20
GRACE_S = 2
POLL_S = 0.01
CHROMA = {"4:2:0": (1, 1), "4:2:2": (1, 0), "4:4:4": (0, 0)}


def frame_bytes(case: dict) -> int:
    """Bytes of one raw frame: visible planes Y, U, V; 1 byte per sample at 8 bit, else 2."""
    width, height = case["width"], case["height"]
    samples = width * height
    if case["chroma"] in CHROMA:
        x, y = CHROMA[case["chroma"]]
        samples += 2 * ((width + x) >> x) * ((height + y) >> y)
    return samples * (1 if case["bit_depth"] == 8 else 2)


def input_path(work: Path, case: dict) -> Path:
    suffix = Path(case["name"]).suffix or ".ivf"
    return Path(work) / "corpus" / (case["input_sha256"].removeprefix("sha256:") + suffix)


def _signal_group(pgid: int, number: int) -> None:
    try:
        os.killpg(pgid, number)
    except (ProcessLookupError, PermissionError):
        pass  # the group has no member left


def _wait(process: subprocess.Popen, deadline: float) -> int | None:
    while (code := process.poll()) is None:
        if time.monotonic() >= deadline:
            return None
        time.sleep(POLL_S)
    return code


def run_decode(argv: list[str], log: Path, timeout_s: int) -> dict:
    """Run one decode in its own process group; hash and count stdout. stderr goes to `log`.

    On timeout: terminate the group, wait `GRACE_S`, kill the group, reap.
    The group is killed in every case, so no descendant survives the check.
    """
    with open(log, "wb") as file:
        file.write(f"$ {shlex.join(argv)}\n".encode())
        file.flush()
        try:
            process = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=file, start_new_session=True
            )
        except OSError as error:
            return {"code": None, "timed_out": False, "bytes": None, "sha256": None, "error": str(error)}
    digest, count, code = hashlib.sha256(), 0, None
    deadline = time.monotonic() + timeout_s
    descriptor = process.stdout.fileno()
    try:
        while (remaining := deadline - time.monotonic()) > 0:
            if not select.select([descriptor], [], [], remaining)[0]:
                continue
            chunk = os.read(descriptor, CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            count += len(chunk)
        code = _wait(process, deadline)
        if code is None:
            _signal_group(process.pid, signal.SIGTERM)
            _wait(process, time.monotonic() + GRACE_S)
    finally:
        # A reaped leader leaves the group id reserved while a descendant lives, so this reaches only our group.
        _signal_group(process.pid, signal.SIGKILL)
        process.stdout.close()
        process.wait()
    return {"code": code, "timed_out": code is None, "bytes": count, "sha256": "sha256:" + digest.hexdigest(), "error": None}


def _last_line(log: Path) -> str:
    lines = [line.strip() for line in log.read_text(errors="replace").splitlines()[1:] if line.strip()]
    return lines[-1][:200] if lines else ""


def _judge(adapter, case: dict, result: dict, log: Path, timeout_s: int) -> tuple[str, int | None, str | None]:
    """Return (outcome, exit code, detail). A zero exit code alone is never `ok`."""
    if result["error"]:
        return "error", None, f"cannot start the decoder: {result['error']}"
    if result["timed_out"]:
        return "timeout", None, f"no exit within {timeout_s} s; the process group was killed"
    code = result["code"]
    if code < 0:
        return "error", None, f"killed by signal {-code}"
    if code != 0:
        outcome = "unsupported" if adapter.unsupported(log.read_text(errors="replace")) else "error"
        return outcome, code, f"exit code {code}: {_last_line(log)}"
    expected = case["reference"]["raw_bytes"]
    if result["bytes"] < expected:
        return "incorrect", code, f"short output: {result['bytes']} of {expected} bytes"
    if result["bytes"] > expected:
        return "incorrect", code, f"wrong byte count: {result['bytes']} bytes, the reference has {expected}"
    if result["sha256"] != case["reference"]["sha256"]:
        return "incorrect", code, "output sha256 differs from the reference"
    return "ok", code, None


def _next_log(directory: Path, case_id: str, threads: int) -> Path:
    number = 1
    while (path := directory / f"{case_id}-t{threads}-{number}.log").exists():
        number += 1
    return path


def _append(path: Path, row: dict) -> None:
    problems = schema.validate(row)
    if problems:
        raise BenchError(f"internal error: correctness row is invalid: {problems[0]}")
    with open(path, "ab") as file:
        file.write(canonical_json(row) + b"\n")
        file.flush()
        os.fsync(file.fileno())


def _select(items: list, key, wanted, what: str) -> list:
    if wanted is None:
        return items
    chosen = [item for item in items if key(item) == wanted]
    if not chosen:
        raise BenchError(f"{what} {wanted!r} is not in the lock for this role.", 2)
    return chosen


def run(
    lock: dict,
    role: str,
    work: Path,
    *,
    case: str | None = None,
    implementation: str | None = None,
    threads: int | None = None,
) -> list[dict]:
    """Check every (build, case, thread budget) of `role` and append one row each. Return the rows."""
    work = Path(work)
    build.role_arch(lock, role)
    body = lock["lock"]
    cases = _select(body["suite"]["cases"], lambda item: item["id"], case, "case")
    budgets = _select(body["roles"][role]["threads"], lambda item: item, threads, "thread budget")
    implementations = _select(body["implementations"], lambda item: item["id"], implementation, "implementation")
    timeout_s = body["measurement"]["timeout_s"]

    inputs = {}
    for item in cases:
        path = input_path(work, item)
        if not path.is_file():
            raise BenchError(f"input of case {item['id']} is missing at {path}. Run `bench prepare` first.", EXIT_PREREQUISITE)
        if sha256_file(path) != item["input_sha256"]:
            raise BenchError(f"{path} does not have the locked digest {item['input_sha256']}. Remove it and prepare again.")
        inputs[item["id"]] = path

    rows = []
    for entry in implementations:
        adapter = build.adapter_for(lock, entry)
        manifest = build.load_manifest(work, entry)
        provenance.check_binary(work, manifest)
        binary = work / manifest["binary_path"]
        try:
            adapter.capabilities(binary)
            missing = None
        except BenchError as error:
            missing = str(error)
        logs = work / "verify" / manifest["build_id"]
        logs.mkdir(parents=True, exist_ok=True)
        for item in cases:
            for budget in budgets:
                if missing:
                    outcome, code, detail, result = "unsupported", None, missing, {"bytes": None, "sha256": None}
                else:
                    log = _next_log(logs, item["id"], budget)
                    result = run_decode(adapter.command(binary, inputs[item["id"]], budget, adapters.STDOUT), log, timeout_s)
                    outcome, code, detail = _judge(adapter, item, result, log, timeout_s)
                row = {
                    "schema": "splotbench.correctness/1",
                    "build_id": manifest["build_id"],
                    "implementation": entry["id"],
                    "case_id": item["id"],
                    "threads": budget,
                    "outcome": outcome,
                    "exit_code": code,
                    "detail": detail,
                    "workload": {
                        "kind": "decode",
                        "output_bytes": result["bytes"],
                        "output_sha256": result["sha256"],
                        "reference_bytes": item["reference"]["raw_bytes"],
                        "reference_sha256": item["reference"]["sha256"],
                        "frames": None if result["bytes"] is None else result["bytes"] // frame_bytes(item),
                    },
                }
                _append(work / "verify" / f"{manifest['build_id']}.jsonl", row)
                rows.append(row)
    return rows


def load_lock(path: Path) -> dict:
    try:
        data = Path(path).read_bytes()
    except OSError as error:
        raise BenchError(f"cannot read the lock {path}: {error.strerror}.", EXIT_PREREQUISITE) from None
    problems = bundle.document_problems(Path(path).name, data, ("object", "splotbench.lock/1"))
    if problems:
        raise BenchError(f"{path} is not a valid lock: {problems[0]}")
    return json.loads(data)


def _verify(args: argparse.Namespace) -> int:
    rows = run(
        load_lock(args.lock), args.role, args.work, case=args.case, implementation=args.implementation, threads=args.threads
    )
    for row in rows:
        detail = f" ({row['detail']})" if row["detail"] else ""
        print(f"{row['outcome']}: {row['implementation']} {row['case_id']} threads={row['threads']}{detail}")
    return EXIT_FAILURE if any(row["outcome"] != "ok" for row in rows) else 0


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "verify",
        help="run the untimed correctness pass",
        description="Decode each case of the role with each build and thread budget, with the raw output on a pipe. "
        "Hash and count the output, compare it with the reference and append one row per check to "
        "W/verify/<build_id>.jsonl. Exit 1 when a check is not ok. Nothing is timed.",
    )
    parser.add_argument("--lock", required=True, type=Path, help="the campaign lock (LOCK.json)")
    parser.add_argument("--role", required=True, help="the host role of the lock")
    parser.add_argument("--work", required=True, type=Path, help="the work directory with corpus and builds")
    parser.add_argument("--case", help="check only this case id")
    parser.add_argument("--implementation", help="check only this implementation id")
    parser.add_argument("--threads", type=int, help="check only this thread budget")
    parser.set_defaults(handler=_verify)
