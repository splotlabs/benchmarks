# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Tests for `snapshot()` on synthetic temporary repositories."""

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from helpers import git, isolate_git, make_repo, restore_git
from splotbench.provenance import snapshot
from splotbench.util import BenchError

FILES = {"a.txt": b"synthetic alpha\n", "sub/b.txt": b"synthetic beta\n"}


def setUpModule():
    isolate_git()


def tearDownModule():
    restore_git()


def expected_digest(lines):
    """The documented formula: sha256 over sorted `<mode> sha256:<hex> <path>` lines."""
    return "sha256:" + hashlib.sha256("".join(sorted(lines)).encode()).hexdigest()


def line(mode, data, path):
    return f"{mode} sha256:{hashlib.sha256(data).hexdigest()} {path}\n"


class GitSnapshotTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = make_repo(Path(temporary.name, "synthetic-repo"), FILES)
        self.clean = snapshot(self.repo)

    def test_clean_repository(self):
        self.assertEqual(self.clean["vcs"], "git")
        self.assertEqual(self.clean["commit"], git(self.repo, "rev-parse", "HEAD"))
        self.assertEqual(self.clean["tree"], git(self.repo, "rev-parse", "HEAD^{tree}"))
        self.assertFalse(self.clean["dirty"])
        self.assertEqual(self.clean["submodules"], [])
        self.assertEqual(
            self.clean["snapshot_sha256"],
            expected_digest([line("100644", data, path) for path, data in FILES.items()]),
        )

    def test_edited_tracked_file_changes_digest_and_sets_dirty(self):
        (self.repo / "a.txt").write_bytes(b"synthetic alpha, edited\n")
        edited = snapshot(self.repo)
        self.assertTrue(edited["dirty"])
        self.assertEqual(edited["commit"], self.clean["commit"])
        self.assertNotEqual(edited["snapshot_sha256"], self.clean["snapshot_sha256"])

    def test_executable_bit_changes_digest(self):
        os.chmod(self.repo / "a.txt", 0o755)
        self.assertNotEqual(snapshot(self.repo)["snapshot_sha256"], self.clean["snapshot_sha256"])

    def test_deleted_tracked_file_changes_digest(self):
        (self.repo / "a.txt").unlink()
        deleted = snapshot(self.repo)
        self.assertTrue(deleted["dirty"])
        self.assertEqual(deleted["snapshot_sha256"], expected_digest([line("100644", FILES["sub/b.txt"], "sub/b.txt")]))

    def test_new_untracked_file_counts(self):
        (self.repo / "new.txt").write_bytes(b"synthetic new\n")
        added = snapshot(self.repo)
        self.assertTrue(added["dirty"])
        self.assertNotEqual(added["snapshot_sha256"], self.clean["snapshot_sha256"])

    def test_ignored_file_does_not_count(self):
        (self.repo / ".gitignore").write_bytes(b"*.log\n")
        git(self.repo, "add", ".gitignore")
        git(self.repo, "commit", "-q", "-m", "synthetic ignore")
        before = snapshot(self.repo)
        (self.repo / "run.log").write_bytes(b"synthetic log\n")
        after = snapshot(self.repo)
        self.assertFalse(after["dirty"])
        self.assertEqual(after["snapshot_sha256"], before["snapshot_sha256"])

    def test_submodule_edit_changes_parent_digest(self):
        child = make_repo(self.repo.parent / "synthetic-child", {"c.txt": b"synthetic child\n"})
        git(self.repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(child), "child")
        git(self.repo, "commit", "-q", "-m", "synthetic submodule")
        before = snapshot(self.repo)
        self.assertFalse(before["dirty"])
        self.assertEqual(before["submodules"][0]["path"], "child")
        self.assertEqual(before["submodules"][0]["commit"], git(child, "rev-parse", "HEAD"))
        (self.repo / "child" / "c.txt").write_bytes(b"synthetic child, edited\n")
        after = snapshot(self.repo)
        self.assertTrue(after["dirty"])
        self.assertTrue(after["submodules"][0]["snapshot"]["dirty"])
        self.assertNotEqual(after["snapshot_sha256"], before["snapshot_sha256"])

    def test_broken_git_directory_is_an_expected_error(self):
        broken = self.repo.parent / "synthetic-broken"
        broken.mkdir()
        (broken / ".git").write_text("gitdir: synthetic-missing-directory\n")
        with self.assertRaises(BenchError) as caught:
            snapshot(broken)
        self.assertEqual(caught.exception.exit_code, 1)


class PlainDirectorySnapshotTest(unittest.TestCase):
    def test_directory_without_git(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for path, data in FILES.items():
                (directory / path).parent.mkdir(parents=True, exist_ok=True)
                (directory / path).write_bytes(data)
            result = snapshot(directory)
        self.assertEqual(result["vcs"], "none")
        self.assertIsNone(result["commit"])
        self.assertIsNone(result["tree"])
        self.assertTrue(result["dirty"])
        self.assertEqual(
            result["snapshot_sha256"],
            expected_digest([line("100644", data, path) for path, data in FILES.items()]),
        )


if __name__ == "__main__":
    unittest.main()
