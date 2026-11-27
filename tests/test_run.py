# SPDX-License-Identifier: MIT
"""Tests for `bench doctor`, `bench run` and `bench bundle` with the synthetic fixture decoder."""

import contextlib
import copy
import io
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import fixture_builds as fb
from helpers import ROOT, bench, isolate_git, restore_git
from splotbench import build, bundle, host, run, verify
from splotbench.util import BenchError, canonical_json, sha256_bytes

A, B = "fixture-a", "fixture-b"
MACHINE = "synthetic-host"


def setUpModule():
    isolate_git()


def tearDownModule():
    restore_git()


def gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def wait_gone(pid: int, wait_s: float = 10) -> bool:
    """For processes that init reaps (orphans): poll until the pid is gone."""
    deadline = time.monotonic() + wait_s
    while not gone(pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    return gone(pid)


def interrupt_on_call(target, number: int = 1):
    """Wrap `target`: the `number`-th call first sends SIGINT to this process."""
    calls = []

    def wrapper(*args, **kwargs):
        calls.append(1)
        if len(calls) == number:
            os.kill(os.getpid(), signal.SIGINT)
        return target(*args, **kwargs)

    return wrapper


class RunTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.work = self.root / "work"
        self.role = fb.native_role()
        self.impls = [fb.implementation(name, *fb.decoder_repo(self.root / f"{name}-source")) for name in (A, B)]

    def prepare(self, cases, implementations=None, *, repetitions=2, threads=(1, 2), timeout_s=60) -> Path:
        """Build the fixture decoders and write a synthetic lock. Return the lock path."""
        document = fb.lock(implementations or self.impls, cases, timeout_s=timeout_s)
        body = document["lock"]
        body["measurement"]["repetitions"] = repetitions
        for role in body["roles"].values():
            role["threads"] = list(threads)
        document["lock_digest"] = sha256_bytes(canonical_json(body))
        build.ensure(document, self.role, self.work, jobs=1)
        path = self.root / "lock.json"
        path.write_text(json.dumps(document))
        return path

    def execute(self, lock: Path, **kwargs) -> str:
        with contextlib.redirect_stdout(io.StringIO()):
            return run.execute(lock, self.role, self.work, kwargs.pop("machine_id", MACHINE), **kwargs)

    def rows(self, run_id: str, name: str = "attempts.jsonl") -> list[dict]:
        return [json.loads(line) for line in (self.work / "runs" / run_id / name).read_text().splitlines()]

    def common(self, lock: Path) -> list[str]:
        return ["--lock", str(lock), "--role", self.role, "--work", str(self.work)]

    def test_complete_run_gives_the_rows_in_seeded_order(self):
        cases = [fb.case(self.work, "fixture-ok-1", "ok", label=1), fb.case(self.work, "fixture-ok-2", "ok", label=2)]
        run_id = self.execute(self.prepare(cases))
        rows = self.rows(run_id)
        # Hand-computed from seeded-rotation/1 with seed 1 (sha256 ranks, see docs/METHODOLOGY.md):
        # fixture-ok-2 before fixture-ok-1; base orders a,b | a,b | b,a | a,b; each round rotates by one.
        expected = []
        for case, threads, base in (("fixture-ok-2", 1, (A, B)), ("fixture-ok-2", 2, (A, B)),
                                    ("fixture-ok-1", 1, (B, A)), ("fixture-ok-1", 2, (A, B))):  # fmt: skip
            rotated = (base, base[::-1], base)
            expected += [(i, case, threads, phase, n) for (phase, n), order in zip((("warmup", 1), ("measure", 1), ("measure", 2)), rotated) for i in order]
        self.assertEqual([(r["implementation"], r["case_id"], r["threads"], r["phase"], r["round"]) for r in rows], expected)
        self.assertEqual([r["sequence"] for r in rows], list(range(1, 25)))
        self.assertEqual([r["attempt_id"] for r in rows], [f"{run_id}#{n}" for n in range(1, 25)])
        for row in rows:
            self.assertEqual((row["outcome"], row["exit_code"]), ("ok", 0))
            self.assertEqual(row["workload"]["frames"], {"expected": 2, "reported": 2, "source": "decoder"})
            metrics = row["workload"]["metrics"]
            self.assertGreater(metrics["wall_time"], 0)
            self.assertIsInstance(metrics["cpu_time"], int)
            self.assertGreater(metrics["peak_rss"], 1 << 20)  # bytes, not KiB
            self.assertIsNone(metrics["first_frame_latency"])
        self.assertEqual(len(self.rows(run_id, "correctness.jsonl")), 8)
        run_doc = json.loads((self.work / "runs" / run_id / "run.json").read_text())
        self.assertEqual((run_doc["state"], run_doc["class"], run_doc["machine_id"]), ("complete", "synthetic", MACHINE))
        log = next((self.work / "runs" / run_id / "logs").glob("1-*.log")).read_text()
        self.assertEqual(json.loads(log.splitlines()[0].removeprefix("# attempt "))["sequence"], 1)
        self.assertRegex(log.splitlines()[1], r"^\$ build/fixture-a/[0-9a-f]{40}/decoder --threads 1 --discard corpus/[0-9a-f]{64}\.obu$")

        archive = self.root / "bundle.tar.gz"
        done = bench(ROOT, "bundle", "--work", str(self.work), "--run", run_id, "-o", str(archive))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(bench(ROOT, "validate", str(archive)).returncode, 0)
        members = bundle.read_bundle(archive)
        self.assertIn("logs/24-" + expected[-1][0] + "-fixture-ok-1-t2.log", members)
        text = b"".join(members.values()).decode()
        for private in {str(self.work), str(self.work.resolve()), socket.gethostname()} | (
            set() if str(self.root).startswith(str(Path.home())) else {str(Path.home())}  # the repository URLs hold root
        ):
            self.assertNotIn(private, text)

    def test_failed_correctness_or_attempt_settles_the_cell(self):
        pid_file = self.root / "pids.txt"
        cases = [
            fb.case(self.work, "fixture-wrong", "wrong"),
            fb.case(self.work, "fixture-hang", "ok", timed={"mode": "spawn-hang"}, pid_file=str(pid_file)),
            fb.case(self.work, "fixture-short", "ok", timed={"frames": 1}),
            fb.case(self.work, "fixture-silent", "ok", timed={"frames": None}),  # no frame counter in the output
            fb.case(self.work, "fixture-noisy", "noisy"),
            fb.case(self.work, "fixture-ok", "ok"),
        ]
        # The hang always reaches the timeout; the other children need far less than 5 s, also on a loaded host.
        lock = self.prepare(cases, self.impls[:1], repetitions=1, threads=(1,), timeout_s=5)
        real_popen = subprocess.Popen

        def popen(*args, **kwargs):  # every later process starts only after the timed-out group is gone
            if pid_file.exists():
                self.assertTrue(all(map(gone, map(int, pid_file.read_text().split()))), "a timed-out process is alive")
            return real_popen(*args, **kwargs)

        with mock.patch.object(run.subprocess, "Popen", popen):
            run_id = self.execute(lock)
        leader, descendant = map(int, pid_file.read_text().split())
        self.assertTrue(gone(leader) and gone(descendant))
        rows = {r["case_id"]: r for r in self.rows(run_id, "correctness.jsonl")}
        self.assertEqual({c: r["outcome"] for c, r in rows.items()}, dict.fromkeys([c["id"] for c in cases], "ok") | {"fixture-wrong": "incorrect"})
        attempts = {}
        for row in self.rows(run_id):
            attempts.setdefault(row["case_id"], []).append(row)
        self.assertNotIn("fixture-wrong", attempts)
        (timeout,) = attempts["fixture-hang"]
        self.assertEqual((timeout["phase"], timeout["outcome"], timeout["exit_code"]), ("warmup", "timeout", None))
        self.assertEqual(set(timeout["workload"]["metrics"].values()), {None})
        (short,) = attempts["fixture-short"]
        self.assertEqual((short["outcome"], short["workload"]["frames"]), ("incorrect", {"expected": 2, "reported": 1, "source": "decoder"}))
        self.assertEqual([r["outcome"] for r in attempts["fixture-ok"]], ["ok", "ok"])
        (silent,) = attempts["fixture-silent"]
        self.assertEqual((silent["outcome"], silent["workload"]["frames"]["reported"]), ("error", None))
        self.assertEqual([r["outcome"] for r in attempts["fixture-noisy"]], ["ok", "ok"])
        logs = self.work / "runs" / run_id / "logs"
        self.assertIn("# harness: the decoder printed no frame count", (logs / f"{silent['sequence']}-{A}-fixture-silent-t1.log").read_text())
        verify_logs = list((self.work / "verify").glob("*/fixture-noisy-*.log"))
        for log in [logs / f"{r['sequence']}-{A}-fixture-noisy-t1.log" for r in attempts["fixture-noisy"]] + verify_logs:
            data = log.read_bytes()
            self.assertLess(len(data), (1 << 20) + 200, log.name)
            self.assertRegex(data.decode(), r"\n# harness: [0-9]+ bytes removed here; the log is capped at 1048576 bytes\n")
            self.assertTrue(data.endswith(b"fixture: decoded 2 frames\n"), log.name)
        self.assertEqual(len(verify_logs), 1)

    def test_child_that_ignores_sigterm_is_killed(self):
        cases = [fb.case(self.work, "fixture-stubborn", "ok", timed={"mode": "stubborn-hang"})]
        lock = self.prepare(cases, self.impls[:1], repetitions=1, threads=(1,), timeout_s=3)
        with mock.patch.object(run, "GRACE_S", 0.5):
            run_id = self.execute(lock)
        (row,) = self.rows(run_id)
        self.assertEqual((row["outcome"], row["exit_code"]), ("timeout", None))  # killed by SIGKILL after the grace

    def test_old_correctness_row_of_another_reference_is_not_reused(self):
        case = fb.case(self.work, "fixture-ok", "ok")
        first = self.prepare([case], self.impls[:1], threads=(1,))
        (old,) = verify.run(json.loads(first.read_text()), self.role, self.work)
        self.assertEqual(old["outcome"], "ok")
        other = copy.deepcopy(case)
        other["reference"]["sha256"] = "sha256:" + "0" * 64  # same case id, another expected output
        run_id = self.execute(self.prepare([other], self.impls[:1], threads=(1,)))
        (row,) = self.rows(run_id, "correctness.jsonl")
        self.assertEqual((row["outcome"], row["workload"]["reference_sha256"]), ("incorrect", "sha256:" + "0" * 64))
        self.assertEqual(self.rows(run_id), [])

    def hang_once_lock(self, pid_file: Path) -> Path:
        # fixture-ok-2 runs first (see the seeded order above); fixture-ok-1 hangs once in its timed run.
        cases = [
            fb.case(self.work, "fixture-ok-1", "ok", timed={"mode": "spawn-hang"}, pid_file=str(pid_file), marker=str(pid_file) + ".hung"),
            fb.case(self.work, "fixture-ok-2", "ok"),
        ]
        return self.prepare(cases, self.impls[:1], repetitions=1, threads=(1,))

    def start_until_hang(self, lock: Path, pid_file: Path) -> tuple[subprocess.Popen, int, int]:
        args = [sys.executable, str(ROOT / "bench"), "run", *self.common(lock), "--machine-id", MACHINE]
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        deadline = time.monotonic() + 30
        while not (pid_file.exists() and pid_file.read_text().endswith("\n")) and time.monotonic() < deadline:
            time.sleep(0.02)
        leader, descendant = map(int, pid_file.read_text().split())
        return process, leader, descendant

    def bundle_and_assemble(self, lock: Path, run_id: str, name: str) -> dict:
        archive = self.root / f"{name}.tar.gz"
        self.assertEqual(bench(ROOT, "bundle", "--work", str(self.work), "--run", run_id, "-o", str(archive)).returncode, 0)
        self.assertEqual(bench(ROOT, "validate", str(archive)).returncode, 0)
        done = bench(ROOT, "assemble", "--lock", str(lock), str(archive), "-o", str(self.root / f"out-{name}"))
        self.assertEqual(done.returncode, 0, done.stderr)
        (dataset,) = (self.root / f"out-{name}" / "datasets").iterdir()
        return json.loads((dataset / "summary.json").read_text())

    def test_sigint_keeps_the_rows_and_resume_completes_the_run(self):
        pid_file = self.root / "pids.txt"
        lock = self.hang_once_lock(pid_file)
        process, leader, descendant = self.start_until_hang(lock, pid_file)
        process.send_signal(signal.SIGINT)
        _out, err = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 130, err)
        self.assertIn("with 2 correctness rows and 3 attempt rows; no decoder is running", err)
        self.assertIn("--resume", err)
        self.assertTrue(gone(leader) and gone(descendant))
        (run_id,) = os.listdir(self.work / "runs")
        run_dir = self.work / "runs" / run_id
        self.assertEqual(json.loads((run_dir / "run.json").read_text())["state"], "interrupted")
        before = (run_dir / "attempts.jsonl").read_bytes()
        self.assertEqual([r["outcome"] for r in self.rows(run_id)], ["ok", "ok", "interrupted"])
        self.bundle_and_assemble(lock, run_id, "interrupted")

        self.execute(lock, resume=run_id)
        after = (run_dir / "attempts.jsonl").read_bytes()
        self.assertTrue(after.startswith(before), "earlier rows changed")
        rows = self.rows(run_id)
        self.assertEqual([r["sequence"] for r in rows], [1, 2, 3, 4, 5])
        interrupted = rows[2]
        repeated = [r for r in rows[3:] if (r["case_id"], r["phase"], r["round"]) == (interrupted["case_id"], interrupted["phase"], interrupted["round"])]
        self.assertEqual([r["outcome"] for r in repeated], ["ok"])
        ok = [(r["case_id"], r["phase"], r["round"]) for r in rows if r["outcome"] == "ok"]
        self.assertEqual(len(ok), len(set(ok)), "an attempt is counted twice")
        self.assertEqual(len(ok), 4)
        self.assertEqual(json.loads((run_dir / "run.json").read_text())["state"], "complete")
        summary = self.bundle_and_assemble(lock, run_id, "resumed")
        self.assertEqual([(c["status"], c["n_valid"]) for c in summary["cells"] if c["machine_id"] == MACHINE], [("ok", 1), ("ok", 1)])
        with self.assertRaisesRegex(BenchError, "is complete"):
            self.execute(lock, resume=run_id)

    def test_sigterm_and_sighup_interrupt_like_sigint(self):
        for number in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=number.name):
                self.work = self.root / f"work-{number.name}"
                pid_file = self.root / f"pids-{number.name}.txt"
                process, leader, descendant = self.start_until_hang(self.hang_once_lock(pid_file), pid_file)
                process.send_signal(number)
                _out, err = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 130, err)
                self.assertTrue(gone(leader) and gone(descendant))
                (run_id,) = os.listdir(self.work / "runs")
                self.assertEqual(json.loads((self.work / "runs" / run_id / "run.json").read_text())["state"], "interrupted")
                self.assertEqual([r["outcome"] for r in self.rows(run_id)], ["ok", "ok", "interrupted"])

    def test_sigint_during_the_correctness_pass(self):
        pid_file = self.root / "pids.txt"
        lock = self.prepare([fb.case(self.work, "fixture-hang", "spawn-hang", pid_file=str(pid_file))], self.impls[:1], threads=(1,))
        process, leader, descendant = self.start_until_hang(lock, pid_file)
        process.send_signal(signal.SIGINT)
        _out, err = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 130, err)
        self.assertIn("with 0 correctness rows and 0 attempt rows", err)
        self.assertTrue(gone(leader) and gone(descendant))
        (run_id,) = os.listdir(self.work / "runs")
        self.assertEqual(json.loads((self.work / "runs" / run_id / "run.json").read_text())["state"], "interrupted")

    def test_sigint_before_between_and_inside_safe_sections(self):
        cases = [fb.case(self.work, "fixture-ok-1", "ok"), fb.case(self.work, "fixture-ok-2", "ok")]
        lock = self.prepare(cases, self.impls[:1], repetitions=1, threads=(1,))
        with mock.patch.object(host, "fingerprint", interrupt_on_call(host.fingerprint)), self.assertRaises(BenchError) as raised:
            self.execute(lock)
        self.assertEqual(raised.exception.exit_code, 130)
        self.assertIn("no run directory was created and no decoder was started", str(raised.exception))
        self.assertFalse((self.work / "runs").exists())

        # Inside the creation of the run directory: it is written completely, then the run stops.
        with mock.patch.object(run, "_write_json", interrupt_on_call(run._write_json)), self.assertRaises(BenchError) as raised:
            self.execute(lock)
        self.assertIn("with 0 correctness rows and 0 attempt rows", str(raised.exception))
        (run_id,) = os.listdir(self.work / "runs")
        self.assertEqual(sorted(os.listdir(self.work / "runs" / run_id)), ["attempts.jsonl", "builds.json", "correctness.jsonl", "host.json", "lock.json", "logs", "run.json"])
        self.assertEqual(json.loads((self.work / "runs" / run_id / "run.json").read_text())["state"], "interrupted")

        # Between attempts: the second input read is followed by the stop, before the next child starts.
        with mock.patch.object(run, "_read_once", interrupt_on_call(run._read_once, 2)), self.assertRaises(BenchError) as raised:
            self.execute(lock, resume=run_id)
        self.assertEqual(raised.exception.exit_code, 130)
        self.assertEqual([(r["case_id"], r["outcome"]) for r in self.rows(run_id)], [("fixture-ok-2", "ok")] * 2)
        self.execute(lock, resume=run_id)
        self.assertEqual([r["outcome"] for r in self.rows(run_id)], ["ok"] * 4)

    def test_harness_crash_keeps_the_lock_and_resume_records_the_lost_attempt(self):
        pid_file = self.root / "pids.txt"
        lock = self.hang_once_lock(pid_file)
        process, leader, descendant = self.start_until_hang(lock, pid_file)
        process.kill()
        process.communicate()
        (run_id,) = os.listdir(self.work / "runs")
        run_dir = self.work / "runs" / run_id
        # The orphaned decoder holds the host lock: no second harness starts while it runs.
        self.assertEqual(host.lock_holder(), str(process.pid))
        done = bench(ROOT, "run", *self.common(lock), "--machine-id", MACHINE, "--resume", run_id)
        self.assertEqual(done.returncode, 1)
        self.assertIn(f"harness process {process.pid} has ended: a decoder process that it started still runs", done.stderr)
        os.killpg(leader, signal.SIGKILL)
        self.assertTrue(wait_gone(leader) and wait_gone(descendant))
        self.assertIsNone(host.lock_holder())

        (lost_log,) = run_dir.glob("logs/3-*.log")
        lost_bytes = lost_log.read_bytes()
        with open(run_dir / "attempts.jsonl", "ab") as file:
            file.write(b'{"schema":"splotbench.attempt/1","attem')  # a torn last line
        self.execute(lock, resume=run_id)
        rows = self.rows(run_id)
        self.assertEqual([(r["sequence"], r["outcome"]) for r in rows], [(1, "ok"), (2, "ok"), (3, "interrupted"), (4, "ok"), (5, "ok")])
        self.assertEqual(rows[2], json.loads(lost_bytes.decode().splitlines()[0].removeprefix("# attempt ")))
        self.assertEqual(lost_log.read_bytes(), lost_bytes)
        self.assertEqual((run_dir / "logs" / "attempts-partial-1.txt").read_bytes(), b'{"schema":"splotbench.attempt/1","attem')
        self.bundle_and_assemble(lock, run_id, "crashed")

    def crashed_run(self) -> tuple[Path, str]:
        """A finished run whose run.json says in-progress, as a crash leaves it."""
        lock = self.prepare([fb.case(self.work, "fixture-ok", "ok")], self.impls[:1], repetitions=1, threads=(1,))
        run_id = self.execute(lock)
        path = self.work / "runs" / run_id / "run.json"
        path.write_text(path.read_text().replace('"complete"', '"in-progress"'))
        return lock, run_id

    def test_replaced_executable_refuses_resume_and_a_new_run(self):
        lock, run_id = self.crashed_run()
        attempts = (self.work / "runs" / run_id / "attempts.jsonl").read_bytes()
        binary = self.work / "build" / A / self.impls[0]["commit"] / "decoder"
        binary.write_bytes(binary.read_bytes() + b"# synthetic change\n")
        for resume in (run_id, None):
            with self.assertRaisesRegex(BenchError, "changed since its manifest"):
                self.execute(lock, resume=resume)
        self.assertEqual(os.listdir(self.work / "runs"), [run_id])
        self.assertEqual((self.work / "runs" / run_id / "attempts.jsonl").read_bytes(), attempts)

    def test_resume_refuses_another_host_identity(self):
        lock, run_id = self.crashed_run()
        with self.assertRaisesRegex(BenchError, "the machine id changed"):
            self.execute(lock, resume=run_id, machine_id="synthetic-other")
        other = host.fingerprint() | {"python": "0.0.0-synthetic"}
        with mock.patch.object(host, "fingerprint", return_value=other), self.assertRaisesRegex(BenchError, "host fingerprint changed"):
            self.execute(lock, resume=run_id)

    def test_wrong_role_architecture_is_refused(self):
        lock = self.prepare([fb.case(self.work, "fixture-ok", "ok")], self.impls[:1], threads=(1,))
        other = "x86-64" if self.role == "arm64" else "arm64"
        with self.assertRaisesRegex(BenchError, f"role '{other}' needs"):
            run.execute(lock, other, self.work, MACHINE)
        with mock.patch.object(host, "_arch_facts", return_value=("aarch64", "x86_64", True)):
            with self.assertRaisesRegex(BenchError, "Rosetta"):
                self.execute(lock)
        self.assertFalse((self.work / "runs").exists())

    def test_second_invocation_is_refused_while_the_lock_is_held(self):
        lock = self.prepare([fb.case(self.work, "fixture-ok", "ok")], self.impls[:1], threads=(1,))
        other = self.root / "other-work"  # the lock is per host, not per work directory
        with host.HostLock():
            done = bench(ROOT, "run", "--lock", str(lock), "--role", self.role, "--work", str(other), "--machine-id", MACHINE)
            doctor = bench(ROOT, "doctor", *self.common(lock))
        self.assertEqual(done.returncode, 1)
        self.assertIn(f"holds {host.lock_path()} (process {os.getpid()})", done.stderr)
        self.assertIn(f"fail host lock: another harness invocation holds {host.lock_path()} (process {os.getpid()})", doctor.stdout)
        self.assertFalse(other.exists())
        self.assertIn(f"ok   host lock: {host.lock_path()} is free", bench(ROOT, "doctor", *self.common(lock)).stdout)

    def test_missing_input_needs_prepare(self):
        lock = self.prepare([fb.case(self.work, "fixture-ok", "ok")], self.impls[:1], threads=(1,))
        next((self.work / "corpus").iterdir()).unlink()
        with self.assertRaises(BenchError) as raised:
            self.execute(lock)
        self.assertEqual(raised.exception.exit_code, 3)
        self.assertIn("bench prepare", str(raised.exception))

    def test_doctor_reports_every_check_and_changes_nothing(self):
        lock = self.prepare([fb.case(self.work, "fixture-ok", "ok")], self.impls[:1], threads=(1, 2))

        def tree():
            return sorted((str(p), p.stat().st_mtime_ns) for p in self.work.rglob("*"))

        index = ROOT / ".git" / "index"
        harness_before = (index.stat().st_mtime_ns, index.read_bytes()) if index.exists() else None
        before = tree()
        done = bench(ROOT, "doctor", *self.common(lock), "--machine-id", MACHINE)
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertEqual(tree(), before)
        if harness_before:
            self.assertEqual((index.stat().st_mtime_ns, index.read_bytes()), harness_before)
        manifest = json.loads((self.work / "build" / A / self.impls[0]["commit"] / "build.json").read_text())
        self.assertIn(
            f"  build {A} {self.impls[0]['commit']} development {manifest['build_id']} {manifest['binary_sha256']}\n", done.stdout
        )
        lines = done.stdout.splitlines()
        lines = [line for line in lines if not line.startswith("  build ")]
        names = [line.split(":")[0].split(maxsplit=1)[1] for line in lines[:11]]
        self.assertEqual(names, ["lock", "architecture", "machine id", "harness", "tools", "corpus", "builds", "correctness", "disk", "cpus", "host lock"])
        self.assertIn("warn correctness: 0 of 2 cells have a row for the current builds", done.stdout)
        self.assertRegex(lines[11], r"^fingerprint sha256:[0-9a-f]{64}$")
        self.assertEqual(json.loads("\n".join(lines[12:]))["arch"], build.native_arch())

        (self.work / "build" / A / self.impls[0]["commit"] / "build.json").unlink()
        done = bench(ROOT, "doctor", *self.common(lock))
        self.assertEqual(done.returncode, 1)
        self.assertIn("fail builds: offline: a finished fixture-a build", done.stdout)


if __name__ == "__main__":
    unittest.main()
