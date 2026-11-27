# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Tests for `bench publish` and `bench validate` of a published directory.

Datasets are assembled from synthetic bundles with an injected harness and written to temporary
directories only. The official ones come from a standard lock with verified synthetic runs.
"""

import contextlib
import fcntl
import io
import json
import os
import shutil
import unittest
from unittest import mock

from helpers import ROOT
from splotbench import assemble, cli, publish, schema, validate
from splotbench.util import BenchError
from test_assemble import ARM, HARNESS, X86, X86_RUN, Base, lock_with

OTHER_HARNESS = {**HARNESS, "snapshot_sha256": "sha256:" + "b2" * 32}  # another harness gives another dataset id


def official_lock():
    return lock_with(lambda lock: lock.update(origin="standard"))


class PublishBase(Base):
    def setUp(self):
        super().setUp()
        self.target = self.tmp / "published"
        self.output = []

    def dataset(self, harness=HARNESS, official=True, partial=False):
        """Assemble a dataset. Official and local ones use a standard lock and verified runs."""
        lock = official_lock()
        self.lock_path.write_text(json.dumps(lock))
        run = {"lock": lock, "run_class": "verified"}
        bundles = [self.run_bundle("arm64", ARM, **run)]
        if not partial:
            bundles.append(self.run_bundle("x86-64", X86, run_id=X86_RUN, machine="synthetic-x86", **run))
        return self.assemble(*bundles, official=official, harness=harness)

    def publish(self, directory, target=None, **kwargs):
        return publish.publish(directory, target or self.target, out=self.output.append, **kwargs)

    def files(self, root=None):
        root = root or self.target
        return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}

    def index(self):
        document = json.loads((self.target / "index.json").read_text())
        self.assertEqual(schema.validate(document), [])
        return document

    def refused(self, directory, text, target=None, **kwargs):
        with self.assertRaises(BenchError) as raised:
            self.publish(directory, target, **kwargs)
        self.assertIn(text, str(raised.exception))
        self.assertEqual(raised.exception.exit_code, 1)
        self.assertFalse((target or self.target).exists())  # refused before any write
        return str(raised.exception)



class PublishTest(PublishBase):
    # --- publication ---

    def test_publish_then_identical_second_run_changes_nothing(self):
        source = self.dataset()
        self.assertEqual(self.publish(source), 0)
        copied = self.target / "datasets" / source.name
        self.assertEqual(self.files(copied), self.files(source))
        self.assertEqual(validate.dataset_problems(copied), [])
        self.assertEqual([e["dataset_id"] for e in self.index()["datasets"]], [source.name])
        self.assertIsNone(self.index()["latest_complete"])
        self.assertEqual(self.index()["datasets"][0]["path"], f"datasets/{source.name}/")
        self.assertTrue(any("git add published" in line for line in self.output))
        before = self.files()
        self.output.clear()
        self.assertEqual(self.publish(source), 0)
        self.assertEqual(self.files(), before)  # same bytes, the old published_utc stays
        self.assertTrue(any("already present" in line for line in self.output))
        self.assertEqual(validate.published_problems(self.target), [])

    def test_existing_directory_with_different_bytes_is_refused_and_names_the_file(self):
        source = self.dataset()
        self.publish(source)
        (self.target / "datasets" / source.name / "summary.csv").write_bytes(b"changed")
        before = self.files()
        with self.assertRaises(BenchError) as raised:
            self.publish(source)
        self.assertIn("summary.csv differs", str(raised.exception))
        self.assertEqual(self.files(), before)

    def test_failure_in_the_middle_of_the_copy_leaves_no_directory_and_the_old_index(self):
        first, second = self.dataset(), self.dataset(OTHER_HARNESS)
        self.publish(first, promote=True)
        before, calls = self.files(), []

        def broken(source, destination):
            calls.append(destination)
            if len(calls) == 3:
                raise OSError(28, "No space left on device")
            shutil.copyfile(source, destination)

        with mock.patch.object(publish, "_copy", broken), self.assertRaises(BenchError) as raised:
            self.publish(second, promote=True)
        self.assertIn("Nothing was published", str(raised.exception))
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.files(), before)
        self.assertEqual(sorted(p.name for p in (self.target / "datasets").iterdir()), [first.name])
        self.assertEqual(self.publish(second, promote=True), 0)  # a later run completes
        self.assertEqual(self.index()["latest_complete"], second.name)

    def test_copy_that_does_not_match_the_checksums_is_not_installed(self):
        source = self.dataset()

        def damaged(src, destination):
            shutil.copyfile(src, destination)
            if destination.name == "summary.csv":
                destination.write_bytes(b"x")

        with mock.patch.object(publish, "_copy", damaged), self.assertRaises(BenchError) as raised:
            self.publish(source)
        self.assertIn("SHA256SUMS", str(raised.exception))
        self.assertEqual(self.files(), {})

    def test_listed_id_with_different_content_is_refused_before_any_copy(self):
        source = self.dataset()
        self.publish(source)
        shutil.rmtree(self.target / "datasets" / source.name)
        document = self.index()
        document["datasets"][0]["name"] = "changed"
        (self.target / "index.json").write_bytes(publish.index_bytes(document))
        before = self.files()
        with self.assertRaises(BenchError) as raised:
            self.publish(source)
        self.assertIn("different content", str(raised.exception))
        self.assertEqual(self.files(), before)

    def test_promote_moves_the_default_and_a_later_publication_does_not(self):
        first, second = self.dataset(), self.dataset(OTHER_HARNESS)
        self.publish(first, promote=True)
        self.assertEqual(self.index()["latest_complete"], first.name)
        self.publish(second)
        index = self.index()
        self.assertEqual(index["latest_complete"], first.name)
        self.assertEqual([e["dataset_id"] for e in index["datasets"]], [first.name, second.name])
        self.publish(second, promote=True)  # already listed: only the default moves
        self.assertEqual(self.index()["latest_complete"], second.name)
        self.assertEqual(len(self.index()["datasets"]), 2)

    def test_dry_run_writes_nothing(self):
        source = self.dataset()
        self.assertEqual(self.publish(source, dry_run=True, promote=True), 0)
        self.assertFalse(self.target.exists())
        text = "\n".join(self.output)
        for word in ("origin official", "complete True", "summary.json", "sha256:", "entry added; latest_complete None ->"):
            self.assertIn(word, text)

    # --- refusals ---

    def test_synthetic_local_and_incomplete_datasets_are_refused_before_any_write(self):
        synthetic = self.assemble(self.arm(), self.x86())
        self.refused(synthetic, "synthetic")
        self.refused(synthetic, "synthetic", allow_local=True)
        local = self.dataset(official=False)
        self.refused(local, "use --allow-local")
        self.refused(local, "--promote needs an official", allow_local=True, promote=True)
        partial = self.dataset(OTHER_HARNESS, official=False, partial=True)
        self.refused(partial, "incomplete", allow_local=True)
        damaged = self.dataset()
        (damaged / "summary.csv").write_bytes(b"changed")
        self.refused(damaged, "not a valid dataset")

    def test_local_dataset_goes_to_another_folder_never_the_default_directory(self):
        local = self.dataset(official=False)
        default = self.tmp / "checkout" / "published"
        with mock.patch.object(publish, "DEFAULT_TARGET", default):
            self.refused(local, "never goes into the published/", target=default, allow_local=True)
        self.assertEqual(self.publish(local, allow_local=True), 0)
        self.assertEqual(self.index()["datasets"][0]["origin"], "local")

    def test_file_above_the_size_limit_is_refused_and_named(self):
        source = self.dataset()
        with mock.patch.object(publish, "MAX_FILE_BYTES", 100):
            self.refused(source, "bytes; the limit")

    def test_second_publication_into_the_same_target_is_refused_while_locked(self):
        source = self.dataset()
        self.target.mkdir()
        descriptor = os.open(self.target, os.O_RDONLY)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        with self.assertRaises(BenchError) as raised:
            self.publish(source)
        self.assertIn("another publication", str(raised.exception))

    def test_command_line_prints_next_steps_and_the_default_target_is_the_checkout(self):
        self.assertEqual(publish.DEFAULT_TARGET, ROOT / "published")
        source = self.dataset()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["publish", str(source), "--to", str(self.target)])
        self.assertEqual((code, err.getvalue()), (0, ""))
        self.assertIn("pull request", out.getvalue())


class PublishedDirectoryTest(PublishBase):
    def published(self, *, promote=False):
        source = self.dataset()
        self.publish(source, promote=promote)
        return source

    def problems(self):
        return validate.published_problems(self.target)

    def test_committed_directory_is_valid(self):
        self.assertEqual(validate.published_problems(ROOT / "published"), [])
        document = json.loads((ROOT / "published" / "index.json").read_text())
        self.assertEqual(document, {"schema": "splotbench.index/1", "latest_complete": None, "datasets": []})

    def test_directory_with_a_published_dataset_is_valid(self):
        self.published(promote=True)
        self.assertEqual(self.problems(), [])
        self.assertEqual(validate.problems_for(self.target), [])

    def test_unlisted_dataset_directory(self):
        source = self.published()
        shutil.copytree(source, self.target / "datasets" / "ds-0123456789abcdef")
        self.assertIn("datasets/ds-0123456789abcdef: not listed in index.json", self.problems())

    def test_listed_but_missing_dataset_directory(self):
        source = self.published()
        shutil.rmtree(self.target / "datasets" / source.name)
        self.assertEqual(self.problems(), [f"datasets/{source.name}: listed in index.json but missing"])

    def test_changed_file_in_a_dataset_directory(self):
        source = self.published()
        (self.target / "datasets" / source.name / "lock.json").write_bytes(b"{}")
        self.assertTrue(any("lock.json" in p for p in self.problems()))

    def test_synthetic_dataset_is_rejected(self):
        source = self.assemble(self.arm(), self.x86())
        self.target.mkdir()
        shutil.copytree(source, self.target / "datasets" / source.name)
        summary = json.loads((source / "summary.json").read_text())
        document = {"schema": "splotbench.index/1", "latest_complete": None,
                    "datasets": [{**assemble.index_entry(summary), "published_utc": "2026-01-01T00:00:00Z"}]}  # fmt: skip
        (self.target / "index.json").write_bytes(publish.index_bytes(document))
        self.assertEqual(self.problems(), [f"datasets/{source.name}: synthetic test data is never published"])

    def test_bad_latest_complete(self):
        source = self.published()
        document = self.index()
        document["latest_complete"] = "ds-0123456789abcdef"
        (self.target / "index.json").write_bytes(publish.index_bytes(document))
        self.assertTrue(any("latest_complete" in p for p in self.problems()))
        document["latest_complete"] = source.name  # listed and complete and official: valid
        (self.target / "index.json").write_bytes(publish.index_bytes(document))
        self.assertEqual(self.problems(), [])

    def test_file_above_the_limit_and_stray_files(self):
        self.published()
        (self.target / "big.bin").write_bytes(b"x" * 11)
        with mock.patch.object(validate, "MAX_PUBLISHED_FILE_BYTES", 10):
            problems = self.problems()
        self.assertTrue(any(p.startswith("big.bin: above") for p in problems))
        self.assertIn("big.bin: unexpected entry. Only index.json, README.md and datasets/ belong here.", problems)


if __name__ == "__main__":
    unittest.main()
