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
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import ROOT, git, isolate_git, restore_git
from splotbench import assemble, cli, publish, schema, validate
from splotbench.util import BenchError, json_bytes
from test_assemble import ARM, HARNESS, X86, X86_RUN, Base, lock_with

OTHER_HARNESS = {**HARNESS, "snapshot_sha256": "sha256:" + "b2" * 32}  # another harness gives another dataset id


def setUpModule():
    isolate_git()


def tearDownModule():
    restore_git()


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
        before, calls, real = self.files(), [], shutil.copyfile

        def broken(source, destination):
            calls.append(destination)
            if len(calls) == 3:
                raise OSError(28, "No space left on device")
            real(source, destination)

        with mock.patch.object(shutil, "copyfile", broken), self.assertRaises(BenchError) as raised:
            self.publish(second, promote=True)
        self.assertIn("Nothing was published", str(raised.exception))
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.files(), before)
        self.assertEqual(sorted(p.name for p in (self.target / "datasets").iterdir()), [first.name])
        self.assertEqual(self.publish(second, promote=True), 0)  # a later run completes
        self.assertEqual(self.index()["latest_complete"], second.name)

    def test_copy_that_does_not_match_the_checksums_is_not_installed(self):
        source = self.dataset()

        real = shutil.copyfile

        def damaged(src, destination):
            real(src, destination)
            if destination.name == "summary.csv":
                destination.write_bytes(b"x")

        with mock.patch.object(shutil, "copyfile", damaged), self.assertRaises(BenchError) as raised:
            self.publish(source)
        self.assertIn("SHA256SUMS", str(raised.exception))
        self.assertEqual(self.files(), {})

    def test_index_write_error_leaves_a_complete_unlisted_directory_and_a_rerun_completes_it(self):
        source = self.dataset()
        with mock.patch.object(publish, "write_atomic", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(BenchError) as raised:
                self.publish(source)
        self.assertIn("index.json", str(raised.exception))
        self.assertIn("run the same command again", str(raised.exception))
        self.assertEqual(raised.exception.exit_code, 1)
        self.assertEqual(validate.dataset_problems(self.target / "datasets" / source.name), [])
        self.assertFalse((self.target / "index.json").exists())
        self.assertIn("index.json: missing or not a regular file", validate.published_problems(self.target))
        self.assertEqual(self.publish(source), 0)  # the rerun lists the directory that is already there
        self.assertEqual([e["dataset_id"] for e in self.index()["datasets"]], [source.name])
        self.assertEqual(validate.published_problems(self.target), [])

    def test_unlisted_directory_after_the_rename_is_reported_with_an_index_present(self):
        first, second = self.dataset(), self.dataset(OTHER_HARNESS)
        self.publish(first)
        with mock.patch.object(publish, "write_atomic", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(BenchError):
                self.publish(second)
        self.assertEqual(validate.published_problems(self.target), [f"datasets/{second.name}: not listed in index.json"])
        self.assertEqual(self.publish(second), 0)
        self.assertEqual(validate.published_problems(self.target), [])

    def test_unusable_target_is_an_error_message_not_a_traceback(self):
        source = self.dataset()
        (self.tmp / "file").write_text("")
        with self.assertRaises(BenchError) as raised:
            self.publish(source, target=self.tmp / "file" / "x")
        self.assertEqual(raised.exception.exit_code, 1)
        self.assertIn("cannot create", str(raised.exception))
        self.assertIn("Nothing was published", str(raised.exception))

    def test_symbolic_link_inside_the_dataset_is_refused(self):
        source = self.dataset()
        (source / "extra.json").symlink_to(source / "lock.json")
        self.refused(source, "not a regular file")

    def test_index_that_is_a_json_array_is_refused(self):
        source = self.dataset()
        self.target.mkdir()
        (self.target / "index.json").write_text("[]\n")
        with self.assertRaises(BenchError) as raised:
            self.publish(source)
        self.assertIn("invalid", str(raised.exception))
        self.assertEqual(sorted(self.files()), ["index.json"])

    def test_dry_run_against_a_populated_target_changes_nothing(self):
        first, second = self.dataset(), self.dataset(OTHER_HARNESS)
        self.publish(first, promote=True)

        def listing():
            return {p.relative_to(self.target).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns, p.is_dir())
                    for p in sorted(self.target.rglob("*"))}  # fmt: skip

        before = listing()
        self.assertEqual(self.publish(second, dry_run=True, promote=True), 0)
        self.assertEqual(listing(), before)

    def test_listed_id_with_different_content_is_refused_before_any_copy(self):
        source = self.dataset()
        self.publish(source)
        shutil.rmtree(self.target / "datasets" / source.name)
        document = self.index()
        document["datasets"][0]["name"] = "changed"
        (self.target / "index.json").write_bytes(json_bytes(document))
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
            self.refused(local, "never goes into the published/", target=default / "mine", allow_local=True)
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
        # The committed directory holds whatever is published; it must be valid, whatever it holds.
        self.assertEqual(validate.published_problems(ROOT / "published"), [])
        document = json.loads((ROOT / "published" / "index.json").read_text())
        self.assertEqual(document["schema"], "splotbench.index/1")

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
        (self.target / "index.json").write_bytes(json_bytes(document))
        self.assertEqual(self.problems(), [f"datasets/{source.name}: synthetic test data is never published"])

    def test_bad_latest_complete(self):
        source = self.published()
        document = self.index()
        document["latest_complete"] = "ds-0123456789abcdef"
        (self.target / "index.json").write_bytes(json_bytes(document))
        self.assertTrue(any("latest_complete" in p for p in self.problems()))
        document["latest_complete"] = source.name  # listed and complete and official: valid
        (self.target / "index.json").write_bytes(json_bytes(document))
        self.assertEqual(self.problems(), [])

    def test_entry_that_disagrees_with_its_summary(self):
        source = self.published()
        document = self.index()
        document["datasets"][0]["name"] = "changed"
        (self.target / "index.json").write_bytes(json_bytes(document))
        self.assertEqual(self.problems(), [f"index.json: the entry of {source.name} differs from its summary.json"])

    def test_manifest_id_that_differs_from_the_directory_name(self):
        source = self.published()
        other = "ds-0123456789abcdef"
        (self.target / "datasets" / source.name).rename(self.target / "datasets" / other)
        document = self.index()
        document["datasets"][0].update(dataset_id=other, path=f"datasets/{other}/")
        (self.target / "index.json").write_bytes(json_bytes(document))
        self.assertIn(f"datasets/{other}: the manifest has dataset_id {source.name}", self.problems())

    def test_symbolic_links_and_a_datasets_file_are_rejected(self):
        source = self.published()
        real = self.tmp / "elsewhere"
        (self.target / "datasets" / source.name).rename(real)
        (self.target / "datasets" / source.name).symlink_to(real)
        self.assertEqual(self.problems(), [f"datasets/{source.name}: a symbolic link is not a dataset directory"])
        empty = self.tmp / "empty"
        empty.mkdir()
        (empty / "index.json").write_text(json.dumps({"schema": "splotbench.index/1", "latest_complete": None, "datasets": []}))
        (empty / "datasets").write_text("not a directory")
        self.assertEqual(validate.published_problems(empty), ["datasets: must be a directory, not a link or a file"])
        (empty / "datasets").unlink()
        (empty / "datasets").symlink_to(self.tmp)
        self.assertEqual(validate.published_problems(empty), ["datasets: must be a directory, not a link or a file"])

    def test_index_that_is_a_json_array(self):
        self.published()
        (self.target / "index.json").write_text("[]\n")
        self.assertTrue(any(p.startswith("index.json:") for p in self.problems()))

    def test_damaged_dataset_directory_is_not_taken_for_a_published_directory(self):
        source = self.dataset()
        (source / "manifest.json").unlink()
        self.assertFalse(validate.is_published_directory(source))
        self.assertIn("missing manifest.json", validate.problems_for(source))

    def test_file_above_the_limit_and_stray_files(self):
        self.published()
        (self.target / "big.bin").write_bytes(b"x" * 11)
        with mock.patch.object(validate, "MAX_PUBLISHED_FILE_BYTES", 10):
            problems = self.problems()
        self.assertTrue(any(p.startswith("big.bin: above") for p in problems))
        self.assertIn("big.bin: unexpected entry. Only index.json, README.md and datasets/ belong here.", problems)


class ImmutableTest(unittest.TestCase):
    """`validate --immutable-since` in temporary Git repositories with synthetic files."""

    ID = "ds-0123456789abcdef"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        git(self.repo, "init", "-q", "-b", "main")
        self.published = self.repo / "published"
        (self.published / "datasets" / self.ID).mkdir(parents=True)
        (self.published / "datasets" / self.ID / "summary.json").write_text("{}\n")
        (self.published / "index.json").write_text(json.dumps({"schema": "splotbench.index/1", "latest_complete": None, "datasets": []}))
        self.commit("base")
        git(self.repo, "tag", "base")
        git(self.repo, "switch", "-q", "-c", "change")

    def commit(self, message="change"):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)

    def problems(self):
        self.commit()
        return validate.immutable_problems(self.published, "base")

    def existing(self, name="summary.json"):
        return self.published / "datasets" / self.ID / name

    def test_new_dataset_directory_and_index_change_pass(self):
        (self.published / "datasets" / "ds-fedcba9876543210").mkdir()
        (self.published / "datasets" / "ds-fedcba9876543210" / "summary.json").write_text("{}\n")
        (self.published / "index.json").write_text("{}\n")
        self.assertEqual(self.problems(), [])

    def test_index_only_change_passes(self):
        (self.published / "index.json").write_text("changed\n")
        self.assertEqual(self.problems(), [])

    def test_modify_delete_rename_and_mode_change_fail(self):
        self.existing().write_text("changed\n")
        self.assertEqual(len(self.problems()), 1)
        git(self.repo, "switch", "-q", "main")
        git(self.repo, "switch", "-q", "-C", "change")
        self.existing().unlink()
        self.assertIn("deleted", self.problems()[0])
        git(self.repo, "switch", "-q", "-C", "change", "main")
        self.existing().rename(self.existing("renamed.json"))
        self.assertEqual(len(self.problems()), 2)  # the old name is deleted, the new name is added to an existing directory
        git(self.repo, "switch", "-q", "-C", "change", "main")
        self.existing().chmod(0o755)
        self.assertIn("changed", self.problems()[0])

    def test_added_files_in_an_existing_directory_fail_with_exact_names(self):
        for name in ("é.json", "with space.json", "-dash.json", "new line\".json"):
            git(self.repo, "switch", "-q", "-C", "change", "main")
            self.existing(name).write_text("x\n")
            problems = self.problems()
            self.assertEqual(len(problems), 1, name)
            self.assertIn(f"published/datasets/{self.ID}/{name}: added to the existing", problems[0])

    def test_published_replaced_by_a_link_to_altered_datasets_fails(self):
        shutil.copytree(self.published, self.repo / "other")
        (self.repo / "other" / "datasets" / self.ID / "summary.json").write_text("altered\n")
        shutil.rmtree(self.published)
        self.published.symlink_to("other")
        self.commit()
        with self.assertRaises(BenchError) as raised:
            validate.immutable_problems(self.published, "base")
        self.assertIn("symbolic link", str(raised.exception))
        self.assertEqual(validate.published_problems(self.published), ["the published directory is a symbolic link; it must be a real directory"])
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["validate", str(self.published), "--immutable-since", "base"]), 1)

    def test_a_link_above_the_published_directory_inside_the_repository_fails(self):
        (self.repo / "site").mkdir()
        (self.repo / "site" / "published").symlink_to(self.published)
        (self.repo / "alias").symlink_to("site")
        for path in (self.repo / "alias" / "published", self.repo / "site" / "published"):
            with self.assertRaises(BenchError, msg=str(path)):
                validate.immutable_problems(path, "base")
        # a real directory below a real path is fine, spelled with or without a link above the repository
        self.assertEqual(validate.immutable_problems(self.published, "base"), [])

    def test_a_link_above_the_repository_top_is_not_a_false_failure(self):
        with tempfile.TemporaryDirectory() as outside:
            alias = Path(outside) / "alias"
            alias.symlink_to(self.repo)  # like /tmp -> /private/tmp on macOS
            self.existing("added.json").write_text("x\n")
            self.commit()
            problems = validate.immutable_problems(alias / "published", "base")
            self.assertEqual(len(problems), 1)
            self.assertIn("added.json: added to the existing", problems[0])

    def test_bad_ref_and_command_line(self):
        with self.assertRaises(BenchError):
            validate.immutable_problems(self.published, "--output=x")
        with self.assertRaises(BenchError) as raised:
            validate.immutable_problems(self.published, "no-such-ref")
        self.assertEqual(raised.exception.exit_code, 3)
        index = {"schema": "splotbench.index/1", "latest_complete": None, "datasets": []}
        shutil.rmtree(self.published / "datasets")
        (self.published / "index.json").write_bytes(json_bytes(index))
        self.commit()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["validate", str(self.published), "--immutable-since", "base"])
        self.assertEqual(code, 1)  # the base dataset directory was deleted
        self.assertIn("deleted since base", err.getvalue())


if __name__ == "__main__":
    unittest.main()
