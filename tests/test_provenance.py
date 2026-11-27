# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Tests for `snapshot()` on synthetic temporary repositories."""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from helpers import git, harness_copy, isolate_git, make_repo, restore_git
from splotbench import schema
from splotbench.provenance import (
    binary_identity,
    build_id,
    build_origin,
    check_binary,
    harness_identity,
    identity_class,
    resolve,
    snapshot,
)
from splotbench.util import BenchError

FIXTURES = Path(__file__).resolve().parent / "fixtures"
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


PATTERN = r"v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)"
TEMPLATE = "https://example.invalid/synthetic/releases/{tag}?version={version}"


class ResolveTest(unittest.TestCase):
    """The upstream is a local synthetic repository reached through a file:// URL."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.upstream = make_repo(self.root / "synthetic-upstream", {"version.txt": b"1.0.0\n"})
        self.release_commit = git(self.upstream, "rev-parse", "HEAD")
        git(self.upstream, "tag", "v1.0.0")
        git(self.upstream, "tag", "-a", "-m", "synthetic annotated release", "v1.0.1")
        git(self.upstream, "tag", "build-7")
        # A later commit keeps the stale version text: version output is never release proof.
        (self.upstream / "other.txt").write_bytes(b"synthetic later change\n")
        git(self.upstream, "add", "other.txt")
        git(self.upstream, "commit", "-q", "-m", "synthetic post-release commit")
        self.later_commit = git(self.upstream, "rev-parse", "HEAD")
        self.branch = git(self.upstream, "branch", "--show-current")
        self.url = self.upstream.as_uri()

    def resolve(self, select):
        return resolve(self.url, select, PATTERN, TEMPLATE)

    def test_lightweight_release_tag(self):
        result = self.resolve("v1.0.0")
        self.assertEqual(result["commit"], self.release_commit)
        self.assertEqual(result["identity"], "release")
        self.assertEqual(
            result["release"],
            {"tag": "v1.0.0", "version": "1.0.0", "url": "https://example.invalid/synthetic/releases/v1.0.0?version=1.0.0"},
        )

    def test_annotated_tag_resolves_to_its_peeled_commit(self):
        result = self.resolve("v1.0.1")
        self.assertEqual(result["commit"], self.release_commit)
        self.assertEqual(result["release"]["tag"], "v1.0.1")

    def test_full_commit_at_a_release_is_a_release(self):
        result = self.resolve(self.release_commit)
        self.assertEqual(result["identity"], "release")
        self.assertEqual(result["release"]["tag"], "v1.0.0")

    def test_post_release_commit_with_stale_version_text_is_development(self):
        self.assertEqual((self.upstream / "version.txt").read_text(), "1.0.0\n")
        for select in (self.later_commit, self.branch):
            with self.subTest(select=select):
                result = self.resolve(select)
                self.assertEqual(result["commit"], self.later_commit)
                self.assertEqual((result["identity"], result["release"]), ("development", None))

    def test_tag_outside_the_pattern_is_never_the_release_tag(self):
        result = self.resolve("build-7")
        self.assertEqual(result["commit"], self.release_commit)
        self.assertEqual(result["identity"], "release")  # v1.0.0 also points here
        self.assertEqual(result["release"]["tag"], "v1.0.0")
        result = resolve(self.url, "build-7", None, None)
        self.assertEqual((result["identity"], result["release"]), ("development", None))

    def test_local_only_tag_is_not_release_proof(self):
        local = self.root / "synthetic-local"
        git(self.root, "clone", "-q", self.url, str(local))
        git(local, "tag", "v2.0.0")
        with self.assertRaises(BenchError):
            resolve(self.url, "v2.0.0", PATTERN, TEMPLATE)
        result = resolve(self.url, git(local, "rev-parse", "HEAD"), PATTERN, TEMPLATE)
        self.assertEqual((result["identity"], result["release"]), ("development", None))

    def test_moved_tag_does_not_change_a_stored_lock_entry(self):
        stored = json.loads(json.dumps(self.resolve("v1.0.0")))
        git(self.upstream, "tag", "-f", "v1.0.0", self.later_commit)
        self.assertEqual(self.resolve("v1.0.0")["commit"], self.later_commit)
        lock = json.loads((FIXTURES / "lock.json").read_text())
        lock["lock"]["implementations"][0].update(stored)
        lock["lock_digest"] = "sha256:" + hashlib.sha256(
            json.dumps(lock["lock"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        self.assertEqual(schema.validate(lock), [])
        self.assertEqual(lock["lock"]["implementations"][0]["commit"], self.release_commit)

    def test_unknown_and_ambiguous_selectors(self):
        with self.assertRaises(BenchError) as caught:
            self.resolve("synthetic-missing")
        self.assertEqual(caught.exception.exit_code, 1)
        git(self.upstream, "branch", "v1.0.0", self.later_commit)
        with self.assertRaises(BenchError) as caught:
            self.resolve("v1.0.0")
        self.assertIn("refs/tags/v1.0.0", str(caught.exception))
        self.assertEqual(self.resolve("refs/heads/v1.0.0")["commit"], self.later_commit)
        self.assertEqual(self.resolve("refs/tags/v1.0.0")["commit"], self.release_commit)

    def test_unreachable_upstream_is_a_missing_prerequisite(self):
        with self.assertRaises(BenchError) as caught:
            resolve((self.root / "synthetic-absent").as_uri(), "main", None, None)
        self.assertEqual(caught.exception.exit_code, 3)
        with self.assertRaises(BenchError):
            resolve(str(self.upstream), "main", None, None)


class IdentityClassTest(unittest.TestCase):
    def test_source_mutation_changes_snapshot_and_class(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = make_repo(Path(temporary, "synthetic-source"), FILES)
            locked = git(repo, "rev-parse", "HEAD")
            clean = snapshot(repo)
            self.assertEqual(identity_class(clean, locked), "verified")
            self.assertEqual(identity_class(clean, "0" * 40), "development")
            self.assertEqual(identity_class(clean, locked, synthetic=True), "synthetic")
            release = {"tag": "v1.0.0", "version": "1.0.0", "url": None}
            self.assertEqual(build_origin(release, clean, locked), "source-release")
            (repo / "a.txt").write_bytes(b"synthetic mutation\n")
            mutated = snapshot(repo)
        self.assertNotEqual(mutated["snapshot_sha256"], clean["snapshot_sha256"])
        self.assertEqual(identity_class(mutated, locked), "development")
        self.assertEqual(build_origin(release, mutated, locked), "source-development")

    def test_directory_without_git_is_development(self):
        with tempfile.TemporaryDirectory() as temporary:
            Path(temporary, "a.txt").write_bytes(b"synthetic\n")
            self.assertEqual(identity_class(snapshot(Path(temporary)), None), "development")

    def test_harness_change_changes_harness_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            harness = harness_copy(Path(temporary, "synthetic-harness"), with_git=True)
            locked = git(harness, "rev-parse", "HEAD")
            before = harness_identity(locked, harness)
            with open(harness / "splotbench" / "util.py", "a") as file:
                file.write("# synthetic change to the timer code\n")
            after = harness_identity(locked, harness)
        self.assertEqual(before["class"], "verified")
        self.assertEqual(after["class"], "development")
        self.assertNotEqual(after["snapshot_sha256"], before["snapshot_sha256"])
        self.assertEqual(after["commit"], before["commit"])


class BinaryIdentityTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name, "synthetic-work")
        (self.work / "build").mkdir(parents=True)
        self.binary = self.work / "build" / "fixture-a"
        self.binary.write_bytes(b"synthetic binary 1\n")
        self.library = self.work / "build" / "libfixture.so"
        self.library.write_bytes(b"synthetic library\n")
        self.manifest = json.loads((FIXTURES / "build-fixture-a-x86-64.json").read_text())
        self.manifest.update(binary_identity(self.work, self.binary, [self.library]))
        self.manifest["build_id"] = build_id(self.manifest)

    def test_identity_records_relative_paths_and_digests(self):
        self.assertEqual(self.manifest["binary_path"], "build/fixture-a")
        self.assertEqual(
            self.manifest["binary_sha256"], "sha256:" + hashlib.sha256(b"synthetic binary 1\n").hexdigest()
        )
        self.assertEqual(self.manifest["libraries"][0]["path"], "build/libfixture.so")
        self.assertEqual(schema.validate(self.manifest), [])
        check_binary(self.work, self.manifest)

    def test_replaced_binary_changes_build_id_and_fails_the_check(self):
        self.binary.write_bytes(b"synthetic binary 2\n")
        with self.assertRaises(BenchError) as caught:
            check_binary(self.work, self.manifest)
        self.assertIn("changed since its manifest", str(caught.exception))
        replaced = dict(self.manifest, **binary_identity(self.work, self.binary, [self.library]))
        self.assertNotEqual(build_id(replaced), self.manifest["build_id"])
        self.assertIn("does not match the build content", schema.validate(replaced)[0])

    def test_replaced_library_fails_the_check(self):
        self.library.write_bytes(b"synthetic library 2\n")
        with self.assertRaises(BenchError):
            check_binary(self.work, self.manifest)

    def test_missing_binary_and_outside_path(self):
        self.binary.unlink()
        with self.assertRaises(BenchError):
            check_binary(self.work, self.manifest)
        outside = self.work.parent / "synthetic-outside"
        outside.write_bytes(b"synthetic\n")
        with self.assertRaises(BenchError):
            binary_identity(self.work, outside)


if __name__ == "__main__":
    unittest.main()
