# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Tests for bundles, dataset directories and `bench validate`. Archives are built here; none is committed."""

import gzip
import hashlib
import io
import json
import os
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import ROOT, bench
from splotbench import bundle
from splotbench.util import BenchError

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Digest of archive_bytes({"a.txt": b"synthetic a\n", "b/c.txt": b"synthetic c\n"}). CI checks it
# on Linux x86-64, Linux arm64 and macOS arm64, so equal input gives equal bytes on each platform.
GOLDEN = "sha256:0a87d1aa6807e4619e114000de1be5566ece391339b28b45c894cd47cf05507b"


def make_run_dir(parent: Path) -> Path:
    run_dir = parent / "synthetic-run"
    shutil.copytree(FIXTURES / "run", run_dir)
    shutil.copy2(FIXTURES / "lock.json", run_dir / "lock.json")
    return run_dir


def raw_archive(entries) -> bytes:
    """Build a tar.gz from (TarInfo, data) pairs without the writer's checks."""
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz", format=tarfile.GNU_FORMAT) as tar:
        for info, content in entries:
            info.size = len(content or b"")
            tar.addfile(info, io.BytesIO(content) if content is not None else None)
    return data.getvalue()


def regular(name, content):
    return tarfile.TarInfo(name), content


class BundleWriterTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temporary = Path(temporary.name)
        self.run_dir = make_run_dir(self.temporary)

    def test_written_bundle_is_valid_and_lists_every_file(self):
        archive = self.temporary / "synthetic.tar.gz"
        digest = bundle.write_bundle(self.run_dir, archive)
        self.assertEqual(digest, "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest())
        self.assertEqual(bundle.check_bundle(archive), [])
        members = bundle.read_bundle(archive)
        self.assertIn("logs/fixture-a.log", members)
        sums = members["SHA256SUMS"].decode().splitlines()
        self.assertEqual(len(sums), len(members) - 1)
        self.assertIn(f"{hashlib.sha256(members['run.json']).hexdigest()}  run.json", sums)
        with tarfile.open(archive) as tar:
            entries = tar.getmembers()
        self.assertEqual([entry.name for entry in entries], sorted(entry.name for entry in entries))
        for entry in entries:
            self.assertEqual((entry.mtime, entry.uid, entry.gid, entry.mode, entry.uname), (0, 0, 0, 0o644, ""))

    def test_same_content_gives_identical_bytes(self):
        first = self.temporary / "first.tar.gz"
        bundle.write_bundle(self.run_dir, first)
        # Other timestamps, permissions and creation order must not change the archive.
        other = self.temporary / "other"
        other.mkdir()
        for path in sorted(self.run_dir.rglob("*"), reverse=True):
            target = other / "synthetic-run" / path.relative_to(self.run_dir)
            if path.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())
                os.chmod(target, 0o600)
                os.utime(target, (1_000_000, 1_000_000))
        second = self.temporary / "second.tar.gz"
        bundle.write_bundle(other / "synthetic-run", second)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        header = first.read_bytes()[:10]
        self.assertEqual(header[4:8], b"\0\0\0\0")  # gzip mtime 0
        self.assertEqual(header[3] & 0x08, 0)  # no file name in the gzip header

    def test_archive_bytes_match_the_golden_digest(self):
        data = bundle.archive_bytes({"b/c.txt": b"synthetic c\n", "a.txt": b"synthetic a\n"})
        self.assertEqual("sha256:" + hashlib.sha256(data).hexdigest(), GOLDEN)

    def test_writer_refuses_links_and_existing_checksums(self):
        os.symlink("run.json", self.run_dir / "link.json")
        with self.assertRaises(BenchError):
            bundle.write_bundle(self.run_dir, self.temporary / "x.tar.gz")
        (self.run_dir / "link.json").unlink()
        (self.run_dir / "SHA256SUMS").write_text("")
        with self.assertRaises(BenchError):
            bundle.write_bundle(self.run_dir, self.temporary / "x.tar.gz")
        self.assertFalse((self.temporary / "x.tar.gz").exists())

    def test_path_too_long_for_ustar_is_a_bench_error(self):
        with self.assertRaises(BenchError) as caught:
            bundle.archive_bytes({"logs/" + "n" * 300: b"synthetic\n"})
        self.assertIn("Use a shorter path", str(caught.exception))

    def test_writer_refuses_an_invalid_member(self):
        (self.run_dir / "host.json").write_text('{"schema": "splotbench.host/1"}')
        with self.assertRaises(BenchError) as caught:
            bundle.write_bundle(self.run_dir, self.temporary / "x.tar.gz")
        self.assertIn("host.json: $: missing required field 'machine_id'", str(caught.exception))


class BundleReaderTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temporary = Path(temporary.name)
        run_dir = make_run_dir(self.temporary)
        archive = self.temporary / "valid.tar.gz"
        bundle.write_bundle(run_dir, archive)
        self.members = bundle.read_bundle(archive)

    def problems(self, entries) -> list[str]:
        path = self.temporary / "case.tar.gz"
        path.write_bytes(raw_archive(entries))
        return bundle.check_bundle(path)

    def valid_entries(self, members=None):
        return [regular(name, data) for name, data in sorted((members or self.members).items())]

    def test_valid_entries_pass(self):
        self.assertEqual(self.problems(self.valid_entries()), [])

    def test_unsafe_entries_are_rejected(self):
        symlink = tarfile.TarInfo("link.json")
        symlink.type, symlink.linkname = tarfile.SYMTYPE, "run.json"
        hardlink = tarfile.TarInfo("hard.json")
        hardlink.type, hardlink.linkname = tarfile.LNKTYPE, "run.json"
        device = tarfile.TarInfo("device")
        device.type = tarfile.CHRTYPE
        directory = tarfile.TarInfo("logs")
        directory.type = tarfile.DIRTYPE
        cases = {
            "absolute": (regular("/etc/synthetic", b"x"), "not a relative path"),
            "parent": (regular("../synthetic", b"x"), "contains '..'"),
            "not normalised": (regular("logs//synthetic", b"x"), "not a normalised path"),
            "symlink": ((symlink, None), "not a regular file"),
            "hardlink": ((hardlink, None), "not a regular file"),
            "device": ((device, None), "not a regular file"),
            "directory": ((directory, None), "not a regular file"),
        }
        for label, (entry, message) in cases.items():
            with self.subTest(label):
                problems = self.problems(self.valid_entries() + [entry])
                self.assertEqual(len(problems), 1, problems)
                self.assertIn(message, problems[0])

    def test_entry_not_in_checksums(self):
        problems = self.problems(self.valid_entries() + [regular("logs/extra.log", b"synthetic\n")])
        self.assertEqual(problems, ["logs/extra.log: not listed in SHA256SUMS"])

    def test_listed_entry_missing(self):
        members = dict(self.members)
        del members["logs/fixture-a.log"]
        self.assertEqual(
            self.problems(self.valid_entries(members)), ["logs/fixture-a.log: listed in SHA256SUMS but not in the bundle"]
        )

    def test_digest_mismatch(self):
        members = dict(self.members, **{"logs/fixture-a.log": b"synthetic tampered log\n"})
        self.assertEqual(self.problems(self.valid_entries(members)), ["logs/fixture-a.log: sha256 does not match SHA256SUMS"])

    def test_schema_invalid_member_with_matching_checksum(self):
        members = dict(self.members)
        run = json.loads(members["run.json"])
        run["state"] = "finished"
        members["run.json"] = json.dumps(run).encode()
        members["SHA256SUMS"] = bundle.checksums({k: v for k, v in members.items() if k != "SHA256SUMS"})
        problems = self.problems(self.valid_entries(members))
        self.assertEqual(len(problems), 1)
        self.assertIn('run.json: $.state: "finished" is not one of', problems[0])

    def test_invalid_row_reports_its_line(self):
        members = dict(self.members)
        members["attempts.jsonl"] += b'{"schema": "splotbench.attempt/9"}\n'
        members["SHA256SUMS"] = bundle.checksums({k: v for k, v in members.items() if k != "SHA256SUMS"})
        problems = self.problems(self.valid_entries(members))
        self.assertEqual(len(problems), 1)
        self.assertIn("attempts.jsonl: line 5: $.schema: unsupported schema version", problems[0])

    def test_missing_required_member(self):
        members = {k: v for k, v in self.members.items() if k != "run.json"}
        members["SHA256SUMS"] = bundle.checksums({k: v for k, v in members.items() if k != "SHA256SUMS"})
        self.assertEqual(self.problems(self.valid_entries(members)), ["missing run.json"])

    def test_pax_and_gnu_headers_are_rejected(self):
        pax = tarfile.TarInfo("logs/pax.log")
        pax.pax_headers = {"comment": "synthetic " * 1000}
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
            for info, content in self.valid_entries():
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
            pax.size = 0
            tar.addfile(pax, io.BytesIO(b""))
        path = self.temporary / "pax.tar.gz"
        path.write_bytes(data.getvalue())
        problems = bundle.check_bundle(path)
        self.assertIn("type b'x'", problems[0])
        long_name = regular("logs/" + "n" * 120 + ".log", b"synthetic\n")
        problems = self.problems(self.valid_entries() + [long_name])
        self.assertIn("type b'L'", problems[0])

    def test_limit_counts_refused_entries_before_reading_them(self):
        big = tarfile.TarInfo("logs/" + "n" * 120)  # a GNU long-name header with a large name block
        big.name = "logs/" + "n" * 200_000
        data = raw_archive(self.valid_entries() + [(big, b"")])
        path = self.temporary / "big.tar.gz"
        path.write_bytes(data)
        with mock.patch.object(bundle, "MAX_BYTES", 100_000):
            problems = bundle.check_bundle(path)
        self.assertEqual(problems[-1], "the content is larger than 100000 bytes")

    def test_negative_entry_size_is_refused(self):
        # A base-256 size field can encode a negative number; read(-n) would read the whole stream.
        info = tarfile.TarInfo("logs/negative.log")
        info.size = -512
        header = info.tobuf(tarfile.GNU_FORMAT)
        path = self.temporary / "negative.tar.gz"
        path.write_bytes(gzip.compress(header + bytes(10 * 1024 * 1024)))
        with mock.patch.object(bundle, "MAX_BYTES", 100_000):
            problems = bundle.check_bundle(path)
        self.assertIn("negative size", problems[0])

    def test_compressed_size_limit(self):
        path = self.temporary / "valid.tar.gz"
        with mock.patch.object(bundle, "MAX_ARCHIVE_BYTES", 100):
            self.assertEqual(bundle.check_bundle(path), ["the archive is larger than 100 bytes"])

    def test_checksums_cannot_list_themselves(self):
        members = dict(self.members)
        members["SHA256SUMS"] += f"{'0' * 64}  SHA256SUMS\n".encode()
        problems = self.problems(self.valid_entries(members))
        self.assertEqual(len(problems), 1)
        self.assertIn("SHA256SUMS cannot list itself", problems[0])

    def resummed(self, members):
        members["SHA256SUMS"] = bundle.checksums({k: v for k, v in members.items() if k != "SHA256SUMS"})
        return self.valid_entries(members)

    def test_known_members_must_have_their_kind(self):
        members = dict(self.members)
        members["run.json"] = members["host.json"]
        members["builds.json"] = json.dumps(json.loads(members["builds.json"])[0]).encode()
        problems = self.problems(self.resummed(members))
        self.assertEqual(
            problems,
            [
                "builds.json: expected a JSON array, found a JSON object",
                'run.json: $.schema: expected splotbench.run/1, found "splotbench.host/1"',
            ],
        )

    def test_logs_are_free_form_and_other_members_are_refused(self):
        members = dict(self.members, **{"logs/notes.json": b'{"synthetic": true}', "extra.json": b"{}"})
        problems = self.problems(self.resummed(members))
        self.assertEqual(len(problems), 1)
        self.assertIn("extra.json: unexpected member", problems[0])

    def test_not_a_bundle(self):
        path = self.temporary / "broken.tar.gz"
        path.write_bytes(b"synthetic, not gzip")
        problems = bundle.check_bundle(path)
        self.assertEqual(len(problems), 1)
        self.assertIn("not a readable tar.gz bundle", problems[0])
        with self.assertRaises(BenchError):
            bundle.read_bundle(path)
        path.write_bytes(gzip.compress(b"synthetic, not tar"))
        self.assertIn("not a readable tar.gz bundle", bundle.check_bundle(path)[0])


def make_dataset(parent: Path) -> Path:
    """Assemble a synthetic dataset directory from the fixtures, with a bundle built here."""
    dataset = parent / "synthetic-dataset"
    dataset.mkdir()
    for name in ("manifest.json", "summary.json"):
        shutil.copy2(FIXTURES / "dataset" / name, dataset / name)
    shutil.copy2(FIXTURES / "lock.json", dataset / "lock.json")
    (dataset / "summary.csv").write_text("synthetic\n")
    manifest = json.loads((dataset / "manifest.json").read_text())
    bundle_path = manifest["runs"][0]["bundle"]["path"]
    (dataset / bundle_path).parent.mkdir()
    bundle.write_bundle(make_run_dir(parent), dataset / bundle_path)

    def entry(path):
        data = (dataset / path).read_bytes()
        return {"path": path, "sha256": "sha256:" + hashlib.sha256(data).hexdigest(), "bytes": len(data)}

    manifest["runs"][0]["bundle"] = entry(bundle_path)
    manifest["files"] = [entry(path) for path in ("lock.json", "summary.json", "summary.csv", bundle_path)]
    (dataset / "manifest.json").write_text(json.dumps(manifest, indent=2))
    files = {p.relative_to(dataset).as_posix(): p.read_bytes() for p in dataset.rglob("*") if p.is_file()}
    (dataset / "SHA256SUMS").write_bytes(bundle.checksums(files))
    return dataset


class DatasetAndCliTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temporary = Path(temporary.name)
        self.dataset = make_dataset(self.temporary)

    def test_valid_dataset_directory_and_files(self):
        paths = [self.dataset, FIXTURES / "index.json", FIXTURES / "run" / "attempts.jsonl", FIXTURES / "run" / "builds.json"]
        done = bench(ROOT, "validate", *map(str, paths))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.count("valid: "), 4)

    def test_tampered_dataset_reports_each_problem_and_exits_1(self):
        (self.dataset / "summary.csv").write_text("synthetic tampered\n")
        (self.dataset / "unlisted.txt").write_text("synthetic\n")
        done = bench(ROOT, "validate", str(self.dataset))
        self.assertEqual(done.returncode, 1)
        lines = done.stderr.splitlines()
        self.assertEqual(len(lines), 2, done.stderr)
        self.assertIn("summary.csv: sha256 does not match SHA256SUMS", lines[0])
        self.assertIn("unlisted.txt: not listed in SHA256SUMS", lines[1])
        self.assertNotIn("Traceback", done.stderr)

    def test_dataset_with_mismatched_ids(self):
        summary = json.loads((self.dataset / "summary.json").read_text())
        summary["dataset_id"] = "ds-0000000000000000"
        (self.dataset / "summary.json").write_text(json.dumps(summary))
        files = {p.relative_to(self.dataset).as_posix(): p.read_bytes() for p in self.dataset.rglob("*")
                 if p.is_file() and p.name != "SHA256SUMS"}
        (self.dataset / "SHA256SUMS").write_bytes(bundle.checksums(files))
        done = bench(ROOT, "validate", str(self.dataset))
        self.assertEqual(done.returncode, 1)
        self.assertIn("summary.json: dataset_id 'ds-0000000000000000' differs from manifest.json", done.stderr)
        self.assertIn("summary.json: sha256 or size differs from manifest.json files", done.stderr)

    def test_dataset_manifest_that_is_an_array(self):
        (self.dataset / "manifest.json").write_text("[]")
        files = {p.relative_to(self.dataset).as_posix(): p.read_bytes() for p in self.dataset.rglob("*")
                 if p.is_file() and p.name != "SHA256SUMS"}
        (self.dataset / "SHA256SUMS").write_bytes(bundle.checksums(files))
        done = bench(ROOT, "validate", str(self.dataset))
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stderr.strip().splitlines()[-1].split(": ", 2)[-1],
                         "manifest.json: expected a JSON object, found a JSON array")
        self.assertNotIn("Traceback", done.stderr)

    def test_lone_surrogate_in_a_file(self):
        lock = json.loads((FIXTURES / "lock.json").read_text())
        lock["lock"]["harness"]["version"] = "SURROGATE"
        path = self.temporary / "synthetic-lock.json"
        path.write_text(json.dumps(lock).replace("SURROGATE", "\\ud800"))
        done = bench(ROOT, "validate", str(path))
        self.assertEqual(done.returncode, 1)
        self.assertIn("lone surrogate", done.stderr)
        self.assertNotIn("Traceback", done.stderr)

    def test_unknown_input_and_missing_file(self):
        done = bench(ROOT, "validate", str(self.temporary / "synthetic.txt"), str(self.temporary / "absent.json"))
        self.assertEqual(done.returncode, 1)
        self.assertIn("unknown file type", done.stderr)
        self.assertIn("cannot read the file", done.stderr)

    def test_non_finite_number_in_a_file(self):
        path = self.temporary / "synthetic.json"
        path.write_text('{"schema": "splotbench.host/1", "x": NaN}')
        done = bench(ROOT, "validate", str(path))
        self.assertEqual(done.returncode, 1)
        self.assertIn("invalid JSON: non-finite number NaN is not allowed", done.stderr)


if __name__ == "__main__":
    unittest.main()
