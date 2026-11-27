# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Tests for the `bench` command line, run as a real child process on synthetic harness copies."""

import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, bench, git, harness_copy, isolate_git, restore_git
from splotbench import __version__
from splotbench.cli import build_parser


def setUpModule():
    isolate_git()


def tearDownModule():
    restore_git()


class CliTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temporary = Path(temporary.name)

    def test_help_lists_only_implemented_commands(self):
        done = bench(ROOT, "--help")
        self.assertEqual(done.returncode, 0)
        self.assertIn("identity", done.stdout)
        self.assertIn("exit codes:", done.stdout)
        subparsers = next(a for a in build_parser()._actions if isinstance(a, argparse._SubParsersAction))
        self.assertEqual(sorted(subparsers.choices), ["assemble", "identity", "validate", "verify"])
        for command in subparsers.choices:
            self.assertEqual(bench(ROOT, command, "--help").returncode, 0)

    def test_version_with_known_commit(self):
        harness = harness_copy(self.temporary / "synthetic-harness", with_git=True)
        done = bench(harness, "--version")
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stdout, f"bench {__version__} (commit {git(harness, 'rev-parse', 'HEAD')})\n")

    def test_version_without_git(self):
        harness = harness_copy(self.temporary / "synthetic-harness", with_git=False)
        done = bench(harness, "--version")
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stdout, f"bench {__version__}\n")

    def test_usage_errors_exit_2(self):
        for args in ((), ("synthetic-unknown-command",), ("identity", "--synthetic-unknown-option"), ("validate",)):
            with self.subTest(args=args):
                done = bench(ROOT, *args)
                self.assertEqual(done.returncode, 2)
                self.assertIn("usage: bench", done.stderr)
                self.assertNotIn("Traceback", done.stderr)

    def test_missing_git_exits_3_without_traceback(self):
        harness = harness_copy(self.temporary / "synthetic-harness", with_git=True)
        empty_path = self.temporary / "synthetic-empty-path"
        empty_path.mkdir()
        done = bench(harness, "identity", env={**os.environ, "PATH": str(empty_path)})
        self.assertEqual(done.returncode, 3)
        self.assertEqual(done.stdout, "")
        self.assertEqual(done.stderr, "bench identity: git is not on PATH. Install it, then run the command again.\n")

    def test_identity_of_clean_git_harness(self):
        harness = harness_copy(self.temporary / "synthetic-harness", with_git=True)
        done = bench(harness, "identity")
        self.assertEqual(done.returncode, 0, done.stderr)
        identity = json.loads(done.stdout)
        self.assertEqual(identity["version"], __version__)
        snapshot = identity["snapshot"]
        self.assertEqual(snapshot["vcs"], "git")
        self.assertEqual(snapshot["commit"], git(harness, "rev-parse", "HEAD"))
        # The run writes __pycache__; the harness .gitignore must keep the copy clean.
        self.assertFalse(snapshot["dirty"])
        self.assertRegex(snapshot["snapshot_sha256"], r"^sha256:[0-9a-f]{64}$")
        self.assertNotIn(str(self.temporary), done.stdout)

    def test_identity_of_modified_harness(self):
        harness = harness_copy(self.temporary / "synthetic-harness", with_git=True)
        clean = json.loads(bench(harness, "identity").stdout)["snapshot"]
        with open(harness / "splotbench" / "__init__.py", "a") as file:
            file.write("# synthetic edit\n")
        modified = json.loads(bench(harness, "identity").stdout)["snapshot"]
        self.assertTrue(modified["dirty"])
        self.assertNotEqual(modified["snapshot_sha256"], clean["snapshot_sha256"])

    def test_identity_without_git(self):
        harness = harness_copy(self.temporary / "synthetic-harness", with_git=False)
        done = bench(harness, "identity")
        self.assertEqual(done.returncode, 0, done.stderr)
        snapshot = json.loads(done.stdout)["snapshot"]
        self.assertEqual(snapshot["vcs"], "none")
        self.assertIsNone(snapshot["commit"])


if __name__ == "__main__":
    unittest.main()
