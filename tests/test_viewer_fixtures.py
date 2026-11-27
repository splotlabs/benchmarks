# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Tests for the viewer fixture generator: determinism, validity and the scenario properties."""

import collections
import json
import re
import tempfile
import unittest
from pathlib import Path

import make_viewer_fixtures as fixtures
from helpers import ROOT
from splotbench import assemble, bundle, schema, validate
from splotbench.util import BenchError


def tree(directory: Path) -> dict:
    return {p.relative_to(directory).as_posix(): p.read_bytes() for p in sorted(directory.rglob("*")) if p.is_file()}


class ViewerFixturesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.out = Path(cls.temporary.name) / "first"
        fixtures.generate(cls.out)
        cls.main = cls.out / "main"
        cls.index = json.loads((cls.main / "index.json").read_text())
        cls.datasets = [json.loads((cls.main / entry["path"] / "summary.json").read_text()) for entry in cls.index["datasets"]]

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_output_is_byte_identical_on_a_second_run(self):
        with tempfile.TemporaryDirectory() as other:
            fixtures.generate(Path(other) / "second")
            self.assertEqual(tree(self.out), tree(Path(other) / "second"))

    def test_every_json_file_and_dataset_is_valid(self):
        for path in sorted(self.out.rglob("*.json")):
            if path.name == "index.json":
                self.assertEqual(schema.validate(json.loads(path.read_text())), [], path)
        for entry in self.index["datasets"]:
            directory = self.main / entry["path"]
            self.assertEqual(validate.dataset_problems(directory), [], entry["dataset_id"])
            summary = json.loads((directory / "summary.json").read_text())
            self.assertEqual({k: v for k, v in entry.items() if k != "published_utc"}, assemble.index_entry(summary))
            self.assertEqual(summary["origin"], "synthetic")
            for name in ("lock.json", "summary.json", "manifest.json"):
                self.assertEqual(schema.validate(json.loads((directory / name).read_text())), [], name)
        manifest = (self.out / "MANIFEST.txt").read_text()
        for entry in self.index["datasets"]:
            self.assertIn(entry["dataset_id"], manifest)
        self.assertIn("command: python3 tests/make_viewer_fixtures.py OUT_DIR", manifest)

    def test_empty_scenario(self):
        index = json.loads((self.out / "empty" / "index.json").read_text())
        self.assertEqual(index, {"schema": "splotbench.index/1", "latest_complete": None, "datasets": []})
        self.assertEqual(schema.validate(index), [])

    def test_main_index_lists_four_datasets_and_no_latest_complete(self):
        self.assertIsNone(self.index["latest_complete"])
        self.assertEqual([e["complete"] for e in self.index["datasets"]], [True, True, True, False])
        self.assertEqual({e["origin"] for e in self.index["datasets"]}, {"synthetic"})

    def test_first_dataset_has_the_required_variety(self):
        first = self.datasets[0]
        self.assertEqual(
            collections.Counter(c["status"] for c in first["cells"]), {"ok": 45, "incorrect": 1, "unsupported": 1, "timeout": 1}
        )
        self.assertEqual(sum(c["noisy"] for c in first["cells"]), 1)
        self.assertEqual(len(first["cells"]), 2 * 4 * 2 * 3)
        cases = first["cases"]
        self.assertEqual(collections.Counter(c["bit_depth"] for c in cases), {8: 2, 10: 2})
        self.assertEqual(len({(c["width"], c["height"]) for c in cases}), 2)
        self.assertEqual({c["threads"] for c in first["cells"]}, {1, 4})
        identities = {i["id"]: i for i in first["implementations"]}
        self.assertEqual(identities["fixture-a"]["identity"], "development")
        self.assertIsNone(identities["fixture-a"]["release"])
        for name in ("fixture-b", "fixture-c"):
            self.assertEqual(identities[name]["identity"], "release")
            self.assertTrue(identities[name]["release"]["tag"] and identities[name]["release"]["version"])
        self.assertEqual({m["machine_id"]: m["role"] for m in first["machines"]}, {"fixture-arm-01": "arm64", "fixture-x86-01": "x86-64"})
        self.assertEqual({m["fingerprint"]["arch"] for m in first["machines"]}, {"aarch64", "x86_64"})
        for machine in first["machines"]:
            self.assertIn("Fixture", machine["fingerprint"]["cpu"]["model"])
        by_machine = collections.defaultdict(list)
        for cell in first["cells"]:
            if cell["status"] == "ok":
                by_machine[cell["machine_id"]].append(cell)
        self.assertTrue(all(c["cpu_ns_median"] is None and c["peak_rss_bytes_median"] is None for c in by_machine["fixture-x86-01"]))
        self.assertTrue(all(c["cpu_ns_median"] is not None and c["peak_rss_bytes_median"] is not None for c in by_machine["fixture-arm-01"]))

    def test_series_ids_connect_only_unchanged_machines(self):
        series = [{m["machine_id"]: m["series_id"] for m in d["machines"]} for d in self.datasets]
        self.assertEqual(series[0], series[1])  # same machines and fingerprints measured later
        self.assertEqual(series[2]["fixture-arm-01"], series[0]["fixture-arm-01"])
        self.assertNotEqual(series[2]["fixture-x86-01"], series[0]["fixture-x86-01"])  # fingerprint changed
        self.assertEqual(len({s for d in series[:3] for s in d.values()}), 3)
        commits = [{i["id"]: i["commit"] for i in d["implementations"]} for d in self.datasets]
        self.assertTrue(all(commits[0][name] != commits[1][name] for name in commits[0]))
        self.assertEqual(commits[0], commits[2])
        self.assertLess(self.datasets[0]["machines"][0]["measured_utc"]["last"], self.datasets[1]["machines"][0]["measured_utc"]["first"])

    def test_partial_dataset_has_a_missing_machine_and_an_interrupted_run(self):
        partial = self.datasets[3]
        self.assertFalse(partial["complete"])
        self.assertEqual([(m["role"], m["run_state"]) for m in partial["machines"]], [("x86-64", "interrupted")])
        statuses = collections.Counter(c["status"] for c in partial["cells"])
        self.assertGreater(statuses["interrupted"], 0)
        self.assertGreater(statuses["ok"], 0)
        manifest = json.loads((self.main / self.index["datasets"][3]["path"] / "manifest.json").read_text())
        self.assertEqual(manifest["coverage"]["roles_present"], ["x86-64"])
        self.assertGreater(manifest["coverage"]["status_counts"]["not-run"], 0)

    def test_no_real_decoder_name_outside_harness_identifiers(self):
        texts = {}
        for path, data in tree(self.out).items():
            if path.endswith(".tar.gz"):
                texts.update({f"{path}:{n}": d.decode() for n, d in bundle.read_bundle(self.out / path).items()})
            else:
                texts[path] = data.decode()
        pattern = re.compile(r"splot|dav2d|avmdec", re.IGNORECASE)
        for name, text in texts.items():
            allowed = re.sub(r"splotbench\.|splotlabs|avmdec-rawvideo-sha256", "", text)
            self.assertIsNone(pattern.search(allowed), name)

    def test_generator_never_writes_into_published_or_a_non_empty_directory(self):
        with self.assertRaisesRegex(BenchError, "inside published/"):
            fixtures.generate(ROOT / "published" / "viewer-fixtures-never-written")
        self.assertFalse((ROOT / "published" / "viewer-fixtures-never-written").exists())
        with tempfile.TemporaryDirectory() as other:
            (Path(other) / "file").write_text("x")
            with self.assertRaisesRegex(BenchError, "not empty"):
                fixtures.generate(Path(other))


if __name__ == "__main__":
    unittest.main()
