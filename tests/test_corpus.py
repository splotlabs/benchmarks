# SPDX-License-Identifier: MIT
"""Tests for the verified stream cache and `bench prepare`. Sources are file: URLs; no network."""

import copy
import hashlib
import json
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from helpers import ROOT, bench, isolate_git, restore_git
from splotbench import corpus, plan, schema
from splotbench.util import BenchError
from fixture_builds import native_role
from synthetic_campaign import HARNESS, STREAMS, make_plan


def setUpModule():
    isolate_git()


def tearDownModule():
    restore_git()


class Campaign(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temporary = Path(temporary.name)
        self.source = self.temporary / "source"
        self.plan_path = make_plan(self.source, replace={'suite = "quick"': 'suite = "full"'})
        self.lock = plan.build_lock(self.plan_path, harness=HARNESS)
        self.work = self.temporary / "work"
        self.names = {c["id"]: c["input_sha256"].removeprefix("sha256:") + ".ivf" for c in self.lock["lock"]["suite"]["cases"]}

    def cached(self, case_id: str) -> Path:
        return self.work / "corpus" / self.names[case_id]


class CorpusTest(Campaign):
    def test_download_then_cache_then_offline(self):
        paths = corpus.ensure(self.lock, self.work)
        self.assertEqual(sorted(paths), sorted(STREAMS))
        for case_id, (_depth, data) in STREAMS.items():
            self.assertEqual(paths[case_id], self.cached(case_id))
            self.assertEqual(paths[case_id].read_bytes(), data)
        self.assertEqual(sorted(p.name for p in (self.work / "corpus").iterdir()), sorted(self.names.values()))
        with mock.patch.object(corpus, "_open", side_effect=AssertionError("connection opened")):
            self.assertEqual(corpus.ensure(self.lock, self.work, offline=True), paths)
            self.assertEqual(corpus.ensure(self.lock, self.work), paths)  # a good cache needs no connection

    def test_corrupt_cached_file_with_the_right_size(self):
        corpus.ensure(self.lock, self.work)
        path = self.cached("synthetic-8bit")
        data = bytearray(path.read_bytes())
        data[0] ^= 1
        path.write_bytes(bytes(data))
        with self.assertRaises(BenchError) as raised:
            corpus.ensure(self.lock, self.work, offline=True)
        self.assertEqual(raised.exception.exit_code, 3)
        self.assertIn("synthetic-8bit", str(raised.exception))
        corpus.ensure(self.lock, self.work)  # online: the bad file is replaced
        self.assertEqual(path.read_bytes(), STREAMS["synthetic-8bit"][1])

    def test_wrong_bytes_from_the_source_are_never_accepted(self):
        (self.source / "synthetic-10bit.ivf").write_bytes(b"d" * 630)
        with self.assertRaises(BenchError) as raised:
            corpus.ensure(self.lock, self.work)
        message = str(raised.exception)
        wrong = "sha256:" + hashlib.sha256(b"d" * 630).hexdigest()
        case = next(c for c in self.lock["lock"]["suite"]["cases"] if c["id"] == "synthetic-10bit")
        for part in ("synthetic-10bit", case["url"], wrong, case["input_sha256"]):
            self.assertIn(part, message)
        self.assertEqual(raised.exception.exit_code, 1)
        self.assertFalse(self.cached("synthetic-10bit").exists())
        self.assertEqual([p.name for p in (self.work / "corpus").iterdir() if p.name.endswith(".part")], [])

    def test_oversized_source_is_cut_off_early(self):
        (self.source / "synthetic-8bit.ivf").write_bytes(b"x" * (1 << 20))
        case = next(c for c in self.lock["lock"]["suite"]["cases"] if c["id"] == "synthetic-8bit")
        served, real_open, sizes = [0], corpus._open, []

        class Counting:
            def __init__(self, response):
                self.response = response

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return self.response.__exit__(*exc)

            def read(self, count):
                data = self.response.read(count)
                served[0] += len(data)
                sizes.append(max((p.stat().st_size for p in (self.work / "corpus").glob("*.part")), default=0))
                return data

        Counting.work = self.work
        with (
            mock.patch.object(corpus, "CHUNK", 16),
            mock.patch.object(corpus, "_open", lambda url: Counting(real_open(url))),
        ):
            with self.assertRaises(BenchError) as raised:
                corpus.ensure(self.lock, self.work)
        self.assertLessEqual(served[0], case["bytes"] + 1)
        self.assertTrue(sizes)
        self.assertLessEqual(max(sizes), case["bytes"] + 16)
        for part in ("synthetic-8bit", case["url"], str(case["bytes"])):
            self.assertIn(part, str(raised.exception))
        self.assertEqual(list((self.work / "corpus").iterdir()), [])

    def test_slow_drip_ends_at_the_deadline(self):
        real_open = corpus._open

        class Drip:
            def __init__(self, response):
                self.response = response

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return self.response.__exit__(*exc)

            def read(self, count):
                time.sleep(0.05)
                return self.response.read(1)  # one byte per read: 400+ reads would take over 20 s

        started = time.monotonic()
        with (
            mock.patch.object(corpus, "DEADLINE_S", 0.3),
            mock.patch.object(corpus, "_open", lambda url: Drip(real_open(url))),
            self.assertRaises(BenchError) as raised,
        ):
            corpus.ensure(self.lock, self.work)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(raised.exception.exit_code, 3)
        self.assertIn("not finished within", str(raised.exception))
        self.assertEqual(list((self.work / "corpus").iterdir()), [])

    def test_truncated_transfer_is_a_transport_failure_and_the_mirror_is_tried(self):
        good = self.temporary / "mirror.ivf"
        good.write_bytes(STREAMS["synthetic-8bit"][1])
        (self.source / "synthetic-8bit.ivf").write_bytes(STREAMS["synthetic-8bit"][1][:50])
        paths = corpus.ensure(self.with_mirror(good), self.work)
        self.assertEqual(paths["synthetic-8bit"].read_bytes(), STREAMS["synthetic-8bit"][1])
        with self.assertRaises(BenchError) as raised:
            corpus.ensure(self.lock, self.temporary / "other-work")
        self.assertEqual(raised.exception.exit_code, 3)
        self.assertIn("ended after 50 of", str(raised.exception))

    def test_redirect_to_a_non_https_url_is_refused(self):
        handler = corpus._HttpsRedirects()
        request = urllib.request.Request("https://example.invalid/stream")
        with self.assertRaises(OSError):
            handler.redirect_request(request, None, 302, "Found", {}, "http://example.invalid/other")
        with self.assertRaises(OSError):
            handler.redirect_request(request, None, 302, "Found", {}, "file:///etc/passwd")
        self.assertIsNotNone(handler.redirect_request(request, None, 302, "Found", {}, "https://example.invalid/other"))

    def test_offline_writes_nothing(self):
        with self.assertRaises(BenchError):
            corpus.ensure(self.lock, self.work, offline=True)
        self.assertFalse(self.work.exists())

    def test_wrong_bytes_replace_nothing_in_the_cache(self):
        corpus.ensure(self.lock, self.work)
        self.cached("synthetic-8bit").write_bytes(b"bad")
        (self.source / "synthetic-8bit.ivf").write_bytes(b"also bad")
        with self.assertRaises(BenchError):
            corpus.ensure(self.lock, self.work)
        self.assertFalse(self.cached("synthetic-8bit").exists())

    def test_missing_offline_lists_every_missing_case_and_opens_no_connection(self):
        with mock.patch.object(corpus, "_open", side_effect=AssertionError("connection opened")):
            with self.assertRaises(BenchError) as raised:
                corpus.ensure(self.lock, self.work, offline=True)
        self.assertEqual(raised.exception.exit_code, 3)
        for case_id in STREAMS:
            self.assertIn(case_id, str(raised.exception))

    def test_unreachable_source_is_a_prerequisite_error(self):
        (self.source / "synthetic-8bit.ivf").unlink()
        with self.assertRaises(BenchError) as raised:
            corpus.ensure(self.lock, self.work)
        self.assertEqual(raised.exception.exit_code, 3)
        self.assertIn("synthetic-8bit", str(raised.exception))

    def with_mirror(self, mirror: Path) -> dict:
        lock = copy.deepcopy(self.lock)
        lock["lock"]["suite"]["cases"][0]["mirrors"] = [mirror.as_uri()]
        lock["lock_digest"] = plan.lock_document(lock["lock"])["lock_digest"]
        self.assertEqual(schema.validate(lock), [])
        return lock

    def test_mirror_is_tried_after_the_url(self):
        good = self.temporary / "mirror.ivf"
        good.write_bytes(STREAMS["synthetic-8bit"][1])
        (self.source / "synthetic-8bit.ivf").unlink()
        paths = corpus.ensure(self.with_mirror(good), self.work)
        self.assertEqual(paths["synthetic-8bit"].read_bytes(), STREAMS["synthetic-8bit"][1])

    def test_mirror_with_other_bytes_fails(self):
        bad = self.temporary / "mirror.ivf"
        bad.write_bytes(b"other")
        (self.source / "synthetic-8bit.ivf").unlink()
        with self.assertRaises(BenchError) as raised:
            corpus.ensure(self.with_mirror(bad), self.work)
        self.assertIn(bad.as_uri(), str(raised.exception))
        self.assertFalse(self.cached("synthetic-8bit").exists())

    def test_only_https_in_a_standard_lock(self):
        lock = copy.deepcopy(self.lock)
        lock["lock"]["origin"] = "standard"
        with self.assertRaises(BenchError) as raised:
            corpus.ensure(lock, self.work)
        self.assertIn("https", str(raised.exception))
        self.assertFalse(any((self.work / "corpus").iterdir()))
        with self.assertRaises(BenchError):  # http is not allowed either
            corpus.ensure({"lock": {"origin": "standard", "suite": {"cases": [{**lock["lock"]["suite"]["cases"][0], "url": "http://example.invalid/x"}]}}}, self.work)


class PrepareCommandTest(Campaign):
    def lock_file(self) -> Path:
        path = self.temporary / "lock.json"
        path.write_text(json.dumps(self.lock))
        return path

    def test_prepare_then_offline_prepare(self):
        lock, role = self.lock_file(), native_role()
        done = bench(ROOT, "prepare", "--lock", str(lock), "--role", role, "--work", str(self.work), "--jobs", "1")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("2 streams verified", done.stdout)
        self.assertEqual([line.split()[:2] for line in done.stdout.splitlines()[1:]], [["build:", "fixture-a"], ["build:", "fixture-b"]])
        done = bench(ROOT, "prepare", "--lock", str(lock), "--role", role, "--work", str(self.work), "--offline")
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_offline_without_cache_exits_3(self):
        done = bench(ROOT, "prepare", "--lock", str(self.lock_file()), "--role", "arm64", "--work", str(self.work), "--offline")
        self.assertEqual(done.returncode, 3)
        self.assertIn("synthetic-8bit", done.stderr)
        self.assertNotIn("Traceback", done.stderr)

    def test_unknown_role_and_edited_lock(self):
        lock = self.lock_file()
        done = bench(ROOT, "prepare", "--lock", str(lock), "--role", "riscv", "--work", str(self.work))
        self.assertEqual(done.returncode, 2)
        self.assertIn("riscv", done.stderr)
        self.assertFalse(self.work.exists())
        edited = json.loads(lock.read_text())
        edited["lock"]["measurement"]["warmups"] = 5
        lock.write_text(json.dumps(edited))
        done = bench(ROOT, "prepare", "--lock", str(lock), "--role", "arm64", "--work", str(self.work))
        self.assertEqual(done.returncode, 1)
        self.assertIn("lock_digest", done.stderr)
