# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Tests for the correctness pass with the synthetic fixture decoder."""

import hashlib
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import fixture_builds as fb
from helpers import ROOT, bench, isolate_git, restore_git
from splotbench import build, schema, verify
from splotbench.util import BenchError

# Expected outcome and detail for each fixture mode, from the documented outcome rules.
EXPECTED = {
    "ok": ("ok", 0, None),
    "wrong": ("incorrect", 0, "output sha256 differs from the reference"),
    "short": ("incorrect", 0, f"short output: {fb.RAW_BYTES // 2} of {fb.RAW_BYTES} bytes"),
    "long": ("incorrect", 0, f"wrong byte count: {fb.RAW_BYTES + 1} bytes, the reference has {fb.RAW_BYTES}"),
    "fail": ("error", 3, "exit code 3: fixture: decode failed"),
    "unsupported": ("unsupported", 1, "exit code 1: fixture: unsupported stream"),
    "unsupported-option": ("unsupported", 2, "exit code 2: decoder: unrecognized option '--threads'"),
}


def setUpModule():
    isolate_git()


def tearDownModule():
    restore_git()


def gone(pid: int, wait_s: float = 5) -> bool:
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


class VerifyTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.work = self.root / "work"
        url, commit = fb.decoder_repo(self.root / "fixture-a-source")
        self.impl = fb.implementation("fixture-a", url, commit)
        self.role = fb.native_role()

    def prepare(self, cases, implementations=None, timeout_s=60):
        lock = fb.lock(implementations or [self.impl], cases, timeout_s=timeout_s)
        build.ensure(lock, self.role, self.work, jobs=1)
        return lock

    def test_every_outcome(self):
        cases = [fb.case(self.work, f"fixture-{mode}", mode) for mode in EXPECTED]
        lock = self.prepare(cases)
        rows = verify.run(lock, self.role, self.work)
        threads = lock["lock"]["roles"][self.role]["threads"]
        self.assertEqual([(row["case_id"], row["threads"]) for row in rows], [(f"fixture-{m}", t) for m in EXPECTED for t in threads])
        for row in rows:
            mode = row["case_id"].removeprefix("fixture-")
            self.assertEqual((row["outcome"], row["exit_code"], row["detail"]), EXPECTED[mode], mode)
            self.assertEqual(schema.validate(row), [])
        ok = rows[0]["workload"]
        self.assertEqual(ok["output_sha256"], "sha256:" + hashlib.sha256(b"\x07" * fb.RAW_BYTES).hexdigest())
        self.assertEqual((ok["output_bytes"], ok["frames"]), (fb.RAW_BYTES, 2))
        stored = (self.work / "verify" / f"{rows[0]['build_id']}.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(line) for line in stored], rows)
        log = self.work / "verify" / rows[0]["build_id"] / "fixture-fail-t1-1.log"
        self.assertIn("fixture: decode failed", log.read_text())

    def test_timeout_kills_the_child_and_its_descendant(self):
        pid_file = self.root / "pids.txt"
        lock = self.prepare([fb.case(self.work, "fixture-hang", "spawn-hang", pid_file=str(pid_file))], timeout_s=1)
        (row,) = verify.run(lock, self.role, self.work, threads=1)
        self.assertEqual((row["outcome"], row["exit_code"]), ("timeout", None))
        leader, descendant = map(int, pid_file.read_text().split())
        self.assertTrue(gone(leader), "the decoder process is still alive")
        self.assertTrue(gone(descendant), "the descendant process is still alive")

    def test_new_check_appends_and_keeps_earlier_rows(self):
        lock = self.prepare([fb.case(self.work, "fixture-ok", "ok")])
        (first,) = verify.run(lock, self.role, self.work, threads=1)
        path = self.work / "verify" / f"{first['build_id']}.jsonl"
        before = path.read_bytes()
        verify.run(lock, self.role, self.work, threads=1)
        after = path.read_bytes()
        self.assertTrue(after.startswith(before))
        self.assertEqual(len(after.splitlines()), 2)
        self.assertTrue((self.work / "verify" / first["build_id"] / "fixture-ok-t1-2.log").is_file())

    def test_missing_option_gives_unsupported_rows(self):
        url, commit = fb.decoder_repo(self.root / "fixture-b-source", usage="usage: decoder INPUT (synthetic)")
        rows = verify.run(self.prepare([fb.case(self.work, "fixture-ok", "ok")], [fb.implementation("fixture-b", url, commit)]), self.role, self.work)
        self.assertEqual({row["outcome"] for row in rows}, {"unsupported"})
        self.assertIn("does not support --threads, -o", rows[0]["detail"])
        self.assertIsNone(rows[0]["workload"]["output_bytes"])

    def test_replaced_binary_stops_the_pass(self):
        lock = self.prepare([fb.case(self.work, "fixture-ok", "ok")])
        binary = self.work / "build" / "fixture-a" / self.impl["commit"] / "decoder"
        binary.write_bytes(binary.read_bytes() + b"# synthetic change\n")
        with self.assertRaisesRegex(BenchError, "changed since its manifest"):
            verify.run(lock, self.role, self.work)

    def test_missing_input_is_a_missing_prerequisite(self):
        cases = [fb.case(self.work, "fixture-ok", "ok")]
        lock = self.prepare(cases)
        next((self.work / "corpus").iterdir()).unlink()
        with self.assertRaises(BenchError) as raised:
            verify.run(lock, self.role, self.work)
        self.assertEqual(raised.exception.exit_code, 3)

    def test_command_prints_one_line_per_check_and_exits_1_on_failure(self):
        lock = self.prepare([fb.case(self.work, "fixture-ok", "ok"), fb.case(self.work, "fixture-wrong", "wrong")])
        path = self.root / "lock.json"
        path.write_text(json.dumps(lock))
        args = ["verify", "--lock", str(path), "--role", self.role, "--work", str(self.work), "--threads", "1"]
        done = bench(ROOT, *args)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertEqual(
            done.stdout.splitlines(),
            [
                "ok: fixture-a fixture-ok threads=1",
                "incorrect: fixture-a fixture-wrong threads=1 (output sha256 differs from the reference)",
            ],
        )
        done = bench(ROOT, *args, "--case", "fixture-ok")
        self.assertEqual((done.returncode, done.stdout), (0, "ok: fixture-a fixture-ok threads=1\n"))



class PrepareThenVerifyTest(unittest.TestCase):
    """`bench prepare` (corpus and build stage) then `bench verify`, with the fixture adapter."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        url, commit = fb.decoder_repo(self.root / "fixture-a-source")
        self.implementation = fb.implementation("fixture-a", url, commit)

    def lock_file(self, work: Path) -> Path:
        path = self.root / "lock.json"
        path.write_text(json.dumps(fb.lock([self.implementation], [fb.case(work, "fixture-ok", "ok")])))
        return path

    def test_prepare_then_verify_then_offline(self):
        work = self.root / "work"
        lock = self.lock_file(work)
        common = ["--lock", str(lock), "--role", fb.native_role(), "--work", str(work)]
        done = bench(ROOT, "prepare", *common, "--jobs", "1")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertRegex(done.stdout, r"build: fixture-a bld-[0-9a-f]{16} in build/fixture-a/")
        self.assertEqual(bench(ROOT, "prepare", *common, "--offline").returncode, 0)
        done = bench(ROOT, "verify", *common)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.count("ok: fixture-a fixture-ok"), 2)

    def test_offline_without_a_checkout_fails_without_fetching(self):
        work = self.root / "work"
        lock = self.lock_file(work)
        shutil.rmtree(self.root / "fixture-a-source")  # a fetch attempt could not succeed either
        done = bench(ROOT, "prepare", "--lock", str(lock), "--role", fb.native_role(), "--work", str(work), "--offline")
        self.assertEqual(done.returncode, 3, done.stderr)
        self.assertIn("offline: the fixture-a checkout", done.stderr)
        self.assertFalse((work / "src").exists())

if __name__ == "__main__":
    unittest.main()
