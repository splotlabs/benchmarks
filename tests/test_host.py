# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Tests for host inspection, the machine id and the host lock."""

import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import ROOT
from splotbench import build, host, schema
from splotbench.util import BenchError


class ArchitectureTest(unittest.TestCase):
    def test_native_host_is_accepted(self):
        self.assertEqual(host.native_arch(), build.native_arch())

    def test_emulation_is_refused(self):
        for facts, text in (
            (("aarch64", "x86_64", True), "Rosetta translation"),
            (("x86_64", "aarch64", False), "emulation or another architecture"),
            (("aarch64", None, False), "emulation or another architecture"),
        ):
            with self.subTest(facts=facts), mock.patch.object(host, "_arch_facts", return_value=facts):
                with self.assertRaisesRegex(BenchError, text):
                    host.native_arch()


class FingerprintTest(unittest.TestCase):
    def test_fingerprint_is_valid_stable_and_has_no_host_name(self):
        first, second = host.fingerprint(), host.fingerprint()
        self.assertEqual(first, second)
        document = host.host_document("synthetic-host", first)
        self.assertEqual(schema.validate(document), [])
        self.assertEqual(first["arch"], build.native_arch())
        self.assertGreaterEqual(first["cpu"]["logical_cores"], 1)
        name = socket.gethostname()
        if len(name) >= 4:
            self.assertNotIn(name, str(document))

    def test_cpu_ranges(self):
        self.assertEqual(host._ranges([0, 1, 2, 5, 7, 8]), "0-2,5,7-8")

    def test_machine_id_pattern(self):
        host.check_machine_id("m2max-01")
        for bad in ("x", "M2", "-a", "aa:bb:cc:dd:ee:ff", "a" * 42):
            with self.subTest(bad=bad), self.assertRaises(BenchError) as raised:
                host.check_machine_id(bad)
            self.assertEqual(raised.exception.exit_code, 2)


class HostLockTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name) / "work"

    def test_free_lock_creates_nothing(self):
        self.assertIsNone(host.lock_holder(self.work))
        self.assertFalse(self.work.exists())

    def test_second_holder_is_refused_with_the_pid(self):
        with host.HostLock(self.work):
            self.assertEqual(host.lock_holder(self.work), str(os.getpid()))
            with self.assertRaisesRegex(BenchError, f"process {os.getpid()}"):
                host.HostLock(self.work).__enter__()
        self.assertIsNone(host.lock_holder(self.work))

    def test_the_kernel_releases_the_lock_of_a_killed_process(self):
        code = "import sys, time; from splotbench import host\nwith host.HostLock(sys.argv[1]):\n print('held', flush=True); time.sleep(60)"
        child = subprocess.Popen([sys.executable, "-c", code, str(self.work)], cwd=ROOT, stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.assertEqual(child.stdout.readline(), "held\n")
        self.assertEqual(host.lock_holder(self.work), str(child.pid))
        child.kill()
        child.wait()
        with host.HostLock(self.work):
            pass


if __name__ == "__main__":
    unittest.main()
