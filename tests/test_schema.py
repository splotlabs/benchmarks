# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Tests for the schemas and the validator. All fixtures are synthetic."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from splotbench import schema
from splotbench.provenance import build_id
from splotbench.util import BenchError, canonical_json, sha256_bytes

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text())


def rows(name):
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines()]


def fixture_documents():
    for path in sorted(FIXTURES.rglob("*.json")):
        loaded = json.loads(path.read_text())
        for document in loaded if isinstance(loaded, list) else [loaded]:
            yield path.relative_to(FIXTURES).as_posix(), document
    for path in sorted(FIXTURES.rglob("*.jsonl")):
        for document in rows(path.relative_to(FIXTURES)):
            yield path.relative_to(FIXTURES).as_posix(), document


class SchemaFilesTest(unittest.TestCase):
    def test_every_schema_uses_only_implemented_keywords(self):
        # registry() loads every file and raises on a keyword the validator does not implement.
        ids = schema.known_ids()
        self.assertEqual(
            ids,
            sorted(f"splotbench.{kind}/1" for kind in
                   ("lock", "build", "host", "correctness", "attempt", "run", "dataset", "summary", "index")),
        )

    def test_unimplemented_keyword_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, "synthetic.schema.json")
            path.write_text('{"$id": "splotbench.synthetic/1", "properties": {"a": {"oneOf": []}}}')
            with self.assertRaises(BenchError) as caught:
                schema.load_schema_file(path)
        self.assertIn("does not implement keyword(s) oneOf", str(caught.exception))

    def test_unresolved_reference_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            Path(temporary, "synthetic.schema.json").write_text(
                '{"$id": "splotbench.synthetic/1", "properties": {"a": {"$ref": "#/$defs/missing"}}}'
            )
            schema.registry.cache_clear()
            self.addCleanup(schema.registry.cache_clear)
            with mock.patch.object(schema, "SCHEMA_DIR", Path(temporary)), self.assertRaises(BenchError) as caught:
                schema.registry()
        self.assertIn("$ref '#/$defs/missing' does not resolve", str(caught.exception))

    def test_every_fixture_is_valid_and_synthetic(self):
        kinds = set()
        for name, document in fixture_documents():
            with self.subTest(fixture=name, schema=document["schema"]):
                self.assertEqual(schema.validate(document), [])
                kinds.add(document["schema"])
        self.assertEqual(kinds, set(schema.known_ids()))
        text = "".join(path.read_text() for path in FIXTURES.rglob("*.json*"))
        for real_name in ('"splot"', '"dav2d"', '"avmdec"', '"official"'):
            self.assertNotIn(real_name, text)
        self.assertEqual(load("lock.json")["lock"]["origin"], "synthetic")
        self.assertEqual(load("dataset/summary.json")["origin"], "synthetic")


class SchemaVersionTest(unittest.TestCase):
    def test_unknown_kind(self):
        problems = schema.validate({"schema": "splotbench.encoderun/1"})
        self.assertEqual(len(problems), 1)
        self.assertIn("unknown schema kind 'splotbench.encoderun/1'", problems[0])

    def test_unknown_major_version(self):
        document = load("lock.json")
        document["schema"] = "splotbench.lock/2"
        problems = schema.validate(document)
        self.assertEqual(len(problems), 1)
        self.assertIn("unsupported schema version 'splotbench.lock/2'", problems[0])
        self.assertIn("splotbench.lock/1", problems[0])

    def test_missing_schema_field(self):
        self.assertIn("missing the 'schema' field", schema.validate({"lock": {}})[0])
        self.assertIn("expected an object", schema.validate([1])[0])

    def test_unknown_workload_kind_in_attempt_and_summary(self):
        attempt = rows("run/attempts.jsonl")[0]
        attempt["workload"]["kind"] = "encode"
        self.assertEqual(
            schema.validate(attempt), ['$.workload.kind: unsupported kind "encode"; this harness reads decode']
        )
        summary = load("dataset/summary.json")
        summary["workload"] = {"kind": "encode"}
        self.assertEqual(
            schema.validate(summary), ['$.workload.kind: unsupported kind "encode"; this harness reads decode']
        )


class MetricTest(unittest.TestCase):
    def setUp(self):
        self.attempt = rows("run/attempts.jsonl")[1]

    def test_missing_metric_is_null(self):
        self.attempt["workload"]["metrics"]["cpu_time"] = None
        self.assertEqual(schema.validate(self.attempt), [])

    def test_zero_is_valid_data(self):
        self.attempt["workload"]["metrics"]["cpu_time"] = 0
        self.assertEqual(schema.validate(self.attempt), [])

    def test_missing_required_metric_field_is_invalid(self):
        del self.attempt["workload"]["metrics"]["cpu_time"]
        self.assertEqual(schema.validate(self.attempt), ["$.workload.metrics: missing required field 'cpu_time'"])

    def test_untyped_metric_is_refused(self):
        self.attempt["workload"]["metrics"]["psnr"] = 40
        self.assertEqual(schema.validate(self.attempt), ["$.workload.metrics: unknown field 'psnr'"])

    def test_wrong_types(self):
        self.attempt["workload"]["metrics"]["wall_time"] = -1
        self.attempt["threads"] = True
        self.assertEqual(
            schema.validate(self.attempt),
            ["$.threads: expected integer, found boolean", "$.workload.metrics.wall_time: -1 is less than 0"],
        )

    def test_summary_metric_dictionary_is_fixed(self):
        summary = load("dataset/summary.json")
        summary["metrics"]["throughput"]["direction"] = "lower"
        self.assertEqual(schema.validate(summary), ['$.metrics.throughput.direction: expected "higher", found "lower"'])


class StrictJsonTest(unittest.TestCase):
    def test_non_finite_numbers_are_rejected_at_load(self):
        for text in ('{"a": NaN}', '{"a": Infinity}', '{"a": -Infinity}', '{"a": 1e999}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                schema.loads(text)

    def test_deep_nesting_never_raises_recursion_error(self):
        # Some Python versions parse this; the others must report a ValueError.
        try:
            schema.loads("[" * 100000 + "]" * 100000)
        except ValueError:
            pass

    def test_lone_surrogate_is_rejected_at_load(self):
        for text in ('{"a": "\\ud800"}', '{"\\udfff": 1}', '["ok", ["\\ud800x"]]'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                schema.loads(text)
        self.assertEqual(schema.loads('"\\ud83d\\ude00"'), "\U0001f600")

    def test_duplicate_keys_are_rejected(self):
        with self.assertRaises(ValueError):
            schema.loads('{"a": 1, "a": 2}')


class RuleTest(unittest.TestCase):
    def test_lock_digest_must_match_content(self):
        lock = load("lock.json")
        lock["lock"]["measurement"]["repetitions"] = 4
        self.assertIn("does not match the lock content", schema.validate(lock)[0])

    def test_build_id_must_match_binary(self):
        build = load("build-fixture-a-x86-64.json")
        build["binary_sha256"] = "sha256:" + "0" * 64
        self.assertIn("does not match the build content", schema.validate(build)[0])

    def test_dirty_source_cannot_be_verified(self):
        build = load("build-fixture-a-x86-64.json")
        build["class"] = "verified"
        build["source"]["snapshot"]["dirty"] = True
        build["source"]["build_origin"] = "source-development"
        build["build_id"] = build_id(build)
        self.assertEqual(schema.validate(build), ["$.class: 'verified' needs a clean Git snapshot at source.commit"])

    def test_dirty_release_source_is_not_source_release(self):
        build = load("build-fixture-a-x86-64.json")
        build["source"]["snapshot"]["dirty"] = True
        build["build_id"] = build_id(build)
        self.assertEqual(
            schema.validate(build), ["$.source.build_origin: expected 'source-development' for this release and snapshot"]
        )

    def test_upstream_release_asset_is_valid_reserved_data(self):
        build = load("build-fixture-a-x86-64.json")
        build["source"]["build_origin"] = "upstream-release-asset"
        self.assertEqual(schema.validate(build), [])
        summary = load("dataset/summary.json")
        summary["implementations"][0]["build_origin"] = "upstream-release-asset"
        self.assertEqual(schema.validate(summary), [])

    def test_source_release_needs_a_release_in_every_format(self):
        summary = load("dataset/summary.json")
        summary["implementations"][1]["build_origin"] = "source-release"
        self.assertEqual(
            schema.validate(summary), ["$.implementations[1].build_origin: 'source-release' needs a release object"]
        )
        build = load("run/builds.json")[1]
        build["source"]["build_origin"] = "source-release"
        self.assertIn("$.source.build_origin: 'source-release' needs a release object", schema.validate(build))

    def test_lock_identity_needs_release_object(self):
        lock = load("lock.json")
        lock["lock"]["implementations"][0]["release"] = None
        lock["lock_digest"] = sha256_bytes(canonical_json(lock["lock"]))
        self.assertEqual(
            schema.validate(lock),
            ["$.lock.implementations[0].identity: 'release' needs a release object, and a release object needs 'release'"],
        )

    def test_pattern_must_match_the_whole_string(self):
        build = load("build-fixture-a-x86-64.json")
        build["source"]["commit"] += "\n"
        self.assertIn("does not match", schema.validate(build)[0])
        self.assertIn("unknown schema kind", schema.validate({"schema": "splotbench.lock/1\n"})[0])

    def test_release_identity_needs_release_object(self):
        summary = load("dataset/summary.json")
        summary["implementations"][1]["identity"] = "release"
        self.assertIn("'release' needs a release object", schema.validate(summary)[0])

    def test_host_fingerprint_digest(self):
        host = load("run/host.json")
        host["fingerprint"]["memory_bytes"] += 1
        self.assertIn("does not match the fingerprint", schema.validate(host)[0])

    def test_index_latest_complete_must_be_listed(self):
        index = load("index.json")
        index["latest_complete"] = index["datasets"][0]["dataset_id"]
        self.assertIn("is not a complete official dataset", schema.validate(index)[0])


class CrossArchitectureBuildTest(unittest.TestCase):
    def test_two_builds_of_one_source_keep_separate_ids(self):
        arm64 = load("run/builds.json")[0]
        x86_64 = load("build-fixture-a-x86-64.json")
        self.assertEqual(schema.validate(arm64), [])
        self.assertEqual(schema.validate(x86_64), [])
        self.assertEqual(arm64["source"], x86_64["source"])
        self.assertNotEqual(arm64["binary_sha256"], x86_64["binary_sha256"])
        self.assertNotEqual(arm64["build_id"], x86_64["build_id"])

    def test_build_id_follows_the_documented_formula(self):
        build = load("build-fixture-a-x86-64.json")
        identity = {
            "implementation": build["implementation"],
            "commit": build["source"]["commit"],
            "snapshot_sha256": build["source"]["snapshot"]["snapshot_sha256"],
            "configuration": build["configuration"],
            "binary_sha256": build["binary_sha256"],
            "libraries": [],
        }
        self.assertEqual(build["build_id"], "bld-" + sha256_bytes(canonical_json(identity))[7:23])


class SummaryProvenanceTest(unittest.TestCase):
    def test_every_cell_resolves_its_full_identity_through_ids(self):
        summary = load("dataset/summary.json")
        lock = load("lock.json")
        self.assertEqual(summary["lock_digest"], lock["lock_digest"])
        self.assertEqual(summary["campaign_id"], f"{lock['lock']['name']}-{lock['lock_digest'][7:19]}")
        self.assertEqual(summary["corpus"]["digest"], lock["lock"]["corpus"]["digest"])
        builds = {build["build_id"]: build for build in summary["builds"]}
        implementations = {entry["id"]: entry for entry in summary["implementations"]}
        machines = {machine["machine_id"]: machine for machine in summary["machines"]}
        cases = {case["id"]: case for case in summary["cases"]}
        locked = {entry["id"]: entry for entry in lock["lock"]["implementations"]}
        for cell in summary["cells"]:
            build = builds[cell["build_id"]]
            implementation = implementations[build["implementation"]]
            self.assertEqual(implementation["id"], cell["implementation"])
            self.assertEqual(implementation["commit"], locked[implementation["id"]]["commit"])
            self.assertEqual(implementation["release"], locked[implementation["id"]]["release"])
            self.assertRegex(implementation["snapshot_sha256"], r"^sha256:[0-9a-f]{64}$")
            self.assertRegex(build["binary_sha256"], r"^sha256:[0-9a-f]{64}$")
            self.assertRegex(machines[cell["machine_id"]]["fingerprint_digest"], r"^sha256:[0-9a-f]{64}$")
            self.assertEqual(cases[cell["case_id"]]["input_sha256"],
                             next(c["input_sha256"] for c in lock["lock"]["suite"]["cases"] if c["id"] == cell["case_id"]))
        self.assertEqual(summary["aggregation"]["harness"]["repository"], "https://github.com/splotlabs/benchmarks")

    def test_reference_to_an_unknown_build_is_invalid(self):
        summary = load("dataset/summary.json")
        summary["cells"][0]["build_id"] = "bld-0000000000000000"
        self.assertEqual(
            schema.validate(summary), ["$.cells[0].build_id: 'bld-0000000000000000' is not defined in this summary"]
        )

    def test_release_and_development_identities_stay_distinct(self):
        summary = load("dataset/summary.json")
        release, development = summary["implementations"]
        self.assertEqual((release["identity"], release["build_origin"]), ("release", "source-release"))
        self.assertEqual(release["release"]["version"], "1.0.0")
        self.assertEqual((development["identity"], development["release"]), ("development", None))
        self.assertEqual(development["build_origin"], "source-development")

    def test_copy_is_not_modified_by_validation(self):
        summary = load("dataset/summary.json")
        before = copy.deepcopy(summary)
        schema.validate(summary)
        self.assertEqual(summary, before)


if __name__ == "__main__":
    unittest.main()
