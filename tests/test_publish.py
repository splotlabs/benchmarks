# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Tests for `bench publish` against an in-process fake S3 server that keeps objects in memory.

The server listens on 127.0.0.1 only, so no real storage is ever contacted and the official test
datasets (assembled here from synthetic bundles with an injected harness) cannot reach production.
"""

import contextlib
import fcntl
import hashlib
import io
import json
import os
import re
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.parse import unquote

from splotbench import cli, publish, schema
from splotbench.util import BenchError
from test_assemble import ARM, HARNESS, X86, X86_RUN, Base, lock_with

KEY_ID, SECRET = "TESTKEYID0123456789", "test-secret-value-must-never-be-printed"
AUTH = re.compile(
    rf"AWS4-HMAC-SHA256 Credential={KEY_ID}/\d{{8}}/auto/s3/aws4_request, "
    r"SignedHeaders=[a-z0-9;-]*host[a-z0-9;-]*x-amz-content-sha256;x-amz-date, Signature=[0-9a-f]{64}"
)


class FakeS3(ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.objects = {}  # key -> (body, headers, etag)
        self.log = []  # (method, key, status)
        self.puts = 0
        self.fail_put_at = None  # fail the Nth PUT with HTTP 500
        self.corrupt = set()  # keys whose stored bytes are damaged after the PUT
        self.before = None  # callable(method, key), runs before a request is handled
        self.stored = 0
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def store(self, key, body, headers=None):
        self.stored += 1
        self.objects[key] = (body, headers or {}, f'"etag-{self.stored}"')


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, body=b"", headers=None):
        self.server.log.append((self.command, self.key, status))
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def handle_request(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        _, _, self.key = unquote(self.path)[1:].partition("/")  # path style: /<bucket>/<key>
        if not AUTH.fullmatch(self.headers.get("Authorization", "")):
            return self.reply(403, b"bad authorization")
        if self.headers.get("x-amz-content-sha256") != hashlib.sha256(body).hexdigest():
            return self.reply(400, b"payload hash does not match the body")
        if self.server.before:
            self.server.before(self.command, self.key)
        current = self.server.objects.get(self.key)
        if self.command == "GET":
            if current is None:
                return self.reply(404, b"NoSuchKey")
            return self.reply(200, current[0], {"ETag": current[2]})
        self.server.puts += 1
        if self.server.puts == self.server.fail_put_at:
            return self.reply(500, b"injected failure")
        if self.headers.get("If-None-Match") == "*" and current is not None:
            return self.reply(412, b"PreconditionFailed")
        if "If-Match" in self.headers and (current is None or current[2] != self.headers["If-Match"]):
            return self.reply(412, b"PreconditionFailed")
        keep = {n: self.headers[n] for n in ("Content-Type", "Cache-Control") if n in self.headers}
        self.server.store(self.key, b"corrupt" if self.key in self.server.corrupt else body, keep)
        self.reply(200)

    do_GET = do_PUT = handle_request


def official_lock():
    return lock_with(lambda lock: lock.update(origin="standard"))


class PublishTest(Base):
    def setUp(self):
        super().setUp()
        self.server = FakeS3()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.env = {
            "SPLOTBENCH_S3_ENDPOINT": f"http://127.0.0.1:{self.server.server_port}",
            "SPLOTBENCH_S3_BUCKET": "bucket-placeholder",
            "SPLOTBENCH_PUBLIC_BASE_URL": "https://data.example.invalid/bench",
            "SPLOTBENCH_S3_PREFIX": "bench",
            "AWS_ACCESS_KEY_ID": KEY_ID,
            "AWS_SECRET_ACCESS_KEY": SECRET,
        }
        self.output = []

    def dataset(self, harness=HARNESS, official=True, bundles=None):
        """Assemble a dataset; official ones come from a standard lock and verified runs."""
        lock = official_lock() if official else None
        if lock:
            self.lock_path.write_text(json.dumps(lock))
        args = {"lock": lock} if lock else {}
        if bundles is None:
            kwargs = {"run_class": "verified"} if lock else {}
            bundles = [self.run_bundle("arm64", ARM, **args, **kwargs),
                       self.run_bundle("x86-64", X86, **args, run_id=X86_RUN, machine="synthetic-x86", **kwargs)]  # fmt: skip
        return self.assemble(*bundles, official=official and bool(lock), harness=harness)

    def publish(self, directory, **kwargs):
        return publish.publish(directory, self.env, out=self.output.append, **kwargs)

    def keys(self):
        return sorted(self.server.objects)

    def index(self):
        document = json.loads(self.server.objects["bench/index.json"][0])
        self.assertEqual(schema.validate(document), [])
        return document

    def puts(self, mark=0):
        return [(k, s) for m, k, s in self.server.log[mark:] if m == "PUT"]

    def refused(self, directory, text, **kwargs):
        mark = len(self.server.log)
        with self.assertRaises(BenchError) as raised:
            self.publish(directory, **kwargs)
        self.assertIn(text, str(raised.exception))
        self.assertEqual(raised.exception.exit_code, 1)
        self.assertEqual(self.server.log[mark:], [])  # refused before any request
        return str(raised.exception)

    def older(self):
        return self.dataset(harness={**HARNESS, "snapshot_sha256": "sha256:" + "b2" * 32})

    # --- publication ---

    def test_publish_then_identical_second_run_changes_nothing(self):
        target = self.dataset()
        self.assertEqual(self.publish(target), 0)
        prefix = f"bench/datasets/{target.name}/"
        expected = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()}
        self.assertEqual(self.keys(), sorted([prefix + p for p in expected] + ["bench/index.json"]))
        for path in expected:  # bytes, content type and cache header of every object
            body, headers, _ = self.server.objects[prefix + path]
            self.assertEqual(body, (target / path).read_bytes())
            self.assertEqual(headers["Cache-Control"], "public, max-age=31536000, immutable")
        types = {p: self.server.objects[prefix + p][1]["Content-Type"] for p in expected}
        self.assertEqual(types["summary.json"], "application/json")
        self.assertEqual(types["summary.csv"], "text/csv; charset=utf-8")
        self.assertEqual(types["SHA256SUMS"], "text/plain; charset=utf-8")
        self.assertEqual({t for p, t in types.items() if p.endswith(".tar.gz")}, {"application/gzip"})
        self.assertEqual(self.server.objects["bench/index.json"][1]["Cache-Control"], "public, max-age=60, must-revalidate")
        index = self.index()
        self.assertEqual([e["dataset_id"] for e in index["datasets"]], [target.name])
        self.assertIsNone(index["latest_complete"])  # no --promote
        self.assertEqual(index["datasets"][0]["path"], f"datasets/{target.name}/")
        before_index, mark = self.server.objects["bench/index.json"], len(self.server.log)
        self.assertEqual(self.publish(target), 0)
        self.assertEqual(self.puts(mark), [])  # no write request at all
        self.assertEqual(self.server.objects["bench/index.json"], before_index)

    def test_collision_with_different_bytes_stops_and_leaves_the_index(self):
        target = self.dataset()
        key = f"bench/datasets/{target.name}/summary.json"
        self.server.store(key, b"other bytes")
        with self.assertRaises(BenchError) as raised:
            self.publish(target)
        self.assertIn("collision", str(raised.exception))
        self.assertEqual(raised.exception.exit_code, 1)
        self.assertEqual(self.server.objects[key][0], b"other bytes")
        self.assertNotIn("bench/index.json", self.server.objects)

    def test_failure_in_the_middle_keeps_the_index_and_a_later_run_completes(self):
        first = self.dataset()
        self.publish(first, promote=True)
        second = self.older()
        self.server.fail_put_at = self.server.puts + 3
        with self.assertRaises(BenchError) as raised:
            self.publish(second, promote=True)
        self.assertEqual(raised.exception.exit_code, 1)
        self.assertEqual(self.index()["latest_complete"], first.name)
        self.assertEqual([e["dataset_id"] for e in self.index()["datasets"]], [first.name])
        stored = [k for k in self.keys() if second.name in k]
        self.assertEqual(len(stored), 2)
        self.server.fail_put_at, mark = None, len(self.server.log)
        self.assertEqual(self.publish(second, promote=True), 0)
        self.assertEqual(len(self.puts(mark)), 7 - 2 + 1)  # the 5 missing files and the index
        self.assertEqual(self.index()["latest_complete"], second.name)

    def test_read_back_mismatch_stops_before_the_index(self):
        target = self.dataset()
        self.server.corrupt.add(f"bench/datasets/{target.name}/summary.csv")
        with self.assertRaises(BenchError) as raised:
            self.publish(target)
        self.assertIn("read-back mismatch", str(raised.exception))
        self.assertNotIn("bench/index.json", self.server.objects)

    def test_index_conflict_keeps_the_other_writers_entry(self):
        other = {"schema": "splotbench.index/1", "latest_complete": None, "datasets": []}
        first, second = self.dataset(), self.older()

        def writer(entry_of):
            def before(method, key):
                if (method, key) == ("PUT", "bench/index.json") and not self.server.fired:
                    self.server.fired = True
                    document = json.loads(self.server.objects[key][0]) if key in self.server.objects else other
                    document["datasets"].append({**entry_of, "dataset_id": "ds-" + "0" * 16})
                    self.server.store(key, json.dumps(document).encode())
            return before

        entry = {"campaign_id": "other-0123456789ab", "name": "other", "origin": "official", "complete": True,
                 "workload": "decode", "methodology": "decode-cli-wall/1", "suite": "quick",
                 "measured_utc": {"first": None, "last": None}, "published_utc": "2026-01-01T00:00:00Z",
                 "path": "datasets/ds-0000000000000000/", "series": []}  # fmt: skip
        self.server.fired, self.server.before = False, writer(entry)  # the index does not exist yet
        with self.assertRaises(BenchError) as raised:
            self.publish(first)
        self.assertIn("run the same command again", str(raised.exception))
        self.assertEqual([e["dataset_id"] for e in self.index()["datasets"]], ["ds-" + "0" * 16])
        self.server.before = None
        self.assertEqual(self.publish(first), 0)  # the retry keeps the other entry
        self.assertEqual([e["dataset_id"] for e in self.index()["datasets"]], ["ds-" + "0" * 16, first.name])
        self.server.fired, self.server.before = False, writer({**entry, "name": "x"})  # index exists now: If-Match
        with self.assertRaises(BenchError) as raised:
            self.publish(second)
        self.assertEqual(raised.exception.exit_code, 1)
        self.assertNotIn(second.name, json.dumps(self.index()))

    def test_promote_moves_the_default_and_a_later_older_dataset_does_not(self):
        first, second = self.dataset(), self.older()
        self.publish(first, promote=True)
        self.assertEqual(self.index()["latest_complete"], first.name)
        self.publish(second)
        index = self.index()
        self.assertEqual(index["latest_complete"], first.name)
        self.assertEqual([e["dataset_id"] for e in index["datasets"]], [first.name, second.name])
        self.publish(second, promote=True)  # entry already listed: only the default moves
        self.assertEqual(self.index()["latest_complete"], second.name)
        self.assertEqual(len(self.index()["datasets"]), 2)

    def test_listed_id_with_different_content_is_refused_before_any_upload(self):
        target = self.dataset()
        self.publish(target)
        document = self.index()
        document["datasets"][0]["name"] = "changed"
        self.server.store("bench/index.json", json.dumps(document).encode())
        mark = len(self.server.log)
        with self.assertRaises(BenchError) as raised:
            self.publish(target)
        self.assertIn("different content", str(raised.exception))
        self.assertEqual(self.puts(mark), [])

    # --- local refusals ---

    def test_synthetic_local_incomplete_and_damaged_datasets_are_refused_before_any_request(self):
        synthetic = self.assemble(self.arm(), self.x86())
        self.refused(synthetic, "synthetic")
        self.refused(synthetic, "synthetic", allow_local=True)
        lock = official_lock()
        self.lock_path.write_text(json.dumps(lock))
        run = {"lock": lock, "run_class": "verified"}
        local = self.assemble(self.run_bundle("arm64", ARM, **run),
                              self.run_bundle("x86-64", X86, run_id=X86_RUN, machine="synthetic-x86", **run))  # fmt: skip
        self.refused(local, "use --allow-local")
        self.refused(local, "--promote needs an official", allow_local=True, promote=True)
        partial = self.assemble(self.run_bundle("arm64", ARM, **run), harness={**HARNESS, "commit": "c" * 40,
                                "snapshot_sha256": "sha256:" + "c3" * 32})  # fmt: skip
        self.refused(partial, "incomplete", allow_local=True)
        damaged = self.dataset()
        (damaged / "summary.csv").write_bytes(b"changed")
        self.refused(damaged, "not a valid dataset")
        self.assertEqual(self.server.objects, {})

    def test_local_dataset_can_go_to_a_personal_bucket(self):
        lock = official_lock()
        self.lock_path.write_text(json.dumps(lock))
        run = {"lock": lock, "run_class": "verified"}
        local = self.assemble(self.run_bundle("arm64", ARM, **run),
                              self.run_bundle("x86-64", X86, run_id=X86_RUN, machine="synthetic-x86", **run))  # fmt: skip
        self.assertEqual(self.publish(local, allow_local=True), 0)
        self.assertEqual(self.index()["datasets"][0]["origin"], "local")

    def test_file_above_the_size_limit_is_refused(self):
        target = self.dataset()
        with mock.patch.object(publish, "MAX_OBJECT_BYTES", 100):
            self.refused(target, "no multipart")

    # --- configuration ---

    def cli(self, *args, env):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["publish", *map(str, args)])
        return code, out.getvalue(), err.getvalue()

    def test_missing_configuration_exits_3_with_the_names_before_any_request(self):
        target = self.dataset()
        code, out, err = self.cli(target, env={"AWS_ACCESS_KEY_ID": KEY_ID})
        self.assertEqual(code, 3)
        for name in publish.REQUIRED[:3] + ("AWS_SECRET_ACCESS_KEY",):
            self.assertIn(name, err)
        self.assertNotIn("AWS_ACCESS_KEY_ID,", err)
        self.assertEqual(self.server.log, [])

    def test_endpoint_must_be_https_or_local(self):
        for endpoint in ("http://storage.example.invalid", "ftp://x", "https://x.example.invalid/path"):
            with self.assertRaises(BenchError) as raised:
                publish.configuration({**self.env, "SPLOTBENCH_S3_ENDPOINT": endpoint}, True)
            self.assertEqual(raised.exception.exit_code, 3)
        publish.configuration({**self.env, "SPLOTBENCH_S3_ENDPOINT": "https://a.example.invalid"}, True)
        with self.assertRaises(BenchError):
            publish.configuration({**self.env, "SPLOTBENCH_S3_PREFIX": "../x"}, True)

    def test_unreachable_endpoint_is_exit_3(self):
        target = self.dataset()
        self.server.server_close()
        with self.assertRaises(BenchError) as raised:
            self.publish(target)
        self.assertEqual(raised.exception.exit_code, 3)

    # --- dry run and secrets ---

    def test_dry_run_reads_but_never_writes(self):
        target = self.dataset()
        self.assertEqual(self.publish(target, dry_run=True), 0)
        text = "\n".join(self.output)
        self.assertIn("entry added; latest_complete None -> None", text)
        self.assertIn(f"sha256:{hashlib.sha256((target / 'summary.json').read_bytes()).hexdigest()}", text)
        for word in ("application/gzip", "immutable", "remote: absent", "origin official", "complete True"):
            self.assertIn(word, text)
        self.assertEqual(self.puts(), [])
        self.assertTrue(self.server.log)  # it did read the remote state
        self.publish(target)
        self.output.clear()
        self.assertEqual(self.publish(target, dry_run=True, promote=True), 0)
        self.assertIn(f"entry unchanged; latest_complete None -> {target.name}", "\n".join(self.output))

    def test_dry_run_needs_no_configuration_and_flags_a_collision(self):
        target = self.dataset()
        code, out, err = self.cli(target, "--dry-run", env={})
        self.assertEqual((code, self.server.log), (0, []))
        self.assertIn("SPLOTBENCH_S3_ENDPOINT", out)
        self.server.store(f"bench/datasets/{target.name}/lock.json", b"x")
        self.assertEqual(self.publish(target, dry_run=True), 1)
        self.assertIn("remote: different", "\n".join(self.output))

    def test_no_secret_in_any_output(self):
        target = self.dataset()
        code, out, err = self.cli(target, "--promote", env=self.env)
        self.assertEqual(code, 0, err)
        self.server.corrupt.add(f"bench/datasets/{self.older().name}/lock.json")
        code2, out2, err2 = self.cli(self.older(), env=self.env)
        self.assertEqual(code2, 1)
        self.assertIn("read-back mismatch", err2)
        bad = {**self.env, "AWS_ACCESS_KEY_ID": "WRONG"}
        code3, out3, err3 = self.cli(self.older(), env=bad)  # the fake answers 403
        self.assertEqual(code3, 3)
        for text in (out, err, out2, err2, out3, err3):
            self.assertNotIn(SECRET, text)

    def test_second_publication_from_this_machine_is_refused_while_the_lock_is_held(self):
        target = self.dataset()
        with open(target.parent / ".publish.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            self.refused(target, "another publication")


class SignatureTest(unittest.TestCase):
    def test_signature_matches_the_published_aws_example(self):
        # "GET Object" example of the AWS SigV4 documentation (service s3, region us-east-1).
        empty = hashlib.sha256(b"").hexdigest()
        headers = {"host": "examplebucket.s3.amazonaws.com", "range": "bytes=0-9",
                   "x-amz-content-sha256": empty, "x-amz-date": "20130524T000000Z"}  # fmt: skip
        value = publish._authorization("GET", "/test.txt", headers, empty, "20130524T000000Z",
                                       "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "us-east-1")  # fmt: skip
        self.assertTrue(value.endswith("Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"))
        self.assertIn("SignedHeaders=host;range;x-amz-content-sha256;x-amz-date", value)


if __name__ == "__main__":
    unittest.main()
