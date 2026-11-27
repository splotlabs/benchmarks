# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Tests for `build.ensure` with the fixture decoder from synthetic file:// repositories."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fixture_builds as fb
from helpers import git, isolate_git, restore_git
from splotbench import build, provenance, schema
from splotbench.util import BenchError


def setUpModule():
    isolate_git()


def tearDownModule():
    restore_git()


class FixtureBuildTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.work = self.root / "work"
        url, self.commit = fb.decoder_repo(self.root / "fixture-a-source")
        self.impl = fb.implementation("fixture-a", url, self.commit)
        self.lock = fb.lock([self.impl], [fb.case(self.work, "fixture-case", "ok")])
        clean = {name: value for name, value in os.environ.items() if name not in build.POLICY_ENV}
        environ = mock.patch.dict(os.environ, {**clean, "CC": "synthetic-cc", "SYNTHETIC_SECRET": "x"}, clear=True)
        environ.start()
        self.addCleanup(environ.stop)

    def ensure(self, lock=None):
        return build.ensure(lock or self.lock, fb.native_role(), self.work, jobs=1)

    def test_manifest_is_complete_and_valid(self):
        (manifest,) = self.ensure()
        directory = self.work / "build" / "fixture-a" / self.commit
        self.assertEqual(schema.validate(manifest), [])
        self.assertEqual(json.loads((directory / "build.json").read_text()), manifest)
        self.assertTrue((directory / "build.log").read_text().startswith("$ cp "))
        self.assertEqual(manifest["class"], "synthetic")
        self.assertEqual(manifest["binary_path"], f"build/fixture-a/{self.commit}/decoder")
        self.assertEqual(manifest["source"]["commit"], self.commit)
        self.assertEqual(manifest["source"]["build_origin"], "source-development")
        self.assertFalse(manifest["source"]["snapshot"]["dirty"])
        self.assertEqual(manifest["configuration"]["arguments"], ["cp", "<src>/decoder.py", "<build>/decoder"])
        self.assertEqual(manifest["version_output"], "fixture-decoder 1.0 (synthetic)")
        self.assertEqual(manifest["build_id"], provenance.build_id(manifest))
        # Only allow-listed variables are recorded.
        self.assertEqual(manifest["environment"], {"CC": "synthetic-cc"})
        self.assertNotIn(str(self.work), json.dumps(manifest))  # paths are relative to the work directory

    def test_finished_build_is_reused(self):
        (first,) = self.ensure()
        log = self.work / "build" / "fixture-a" / self.commit / "build.log"
        log.write_text("synthetic marker\n")
        (second,) = self.ensure()
        self.assertEqual(second, first)
        self.assertEqual(log.read_text(), "synthetic marker\n")

    def test_replaced_binary_is_an_error_with_both_digests(self):
        (manifest,) = self.ensure()
        binary = self.work / manifest["binary_path"]
        binary.write_bytes(binary.read_bytes() + b"# synthetic change\n")
        with self.assertRaises(BenchError) as raised:
            self.ensure()
        self.assertIn(manifest["binary_sha256"], str(raised.exception))
        self.assertIn(provenance.binary_identity(self.work, binary)["binary_sha256"], str(raised.exception))

    def test_upstream_release_asset_is_not_supported(self):
        self.ensure()
        path = self.work / "build" / "fixture-a" / self.commit / "build.json"
        manifest = json.loads(path.read_text())
        manifest["source"]["build_origin"] = "upstream-release-asset"
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(BenchError, "not supported"):
            self.ensure()

    def test_fixture_adapter_is_refused_in_a_standard_lock(self):
        with self.assertRaisesRegex(BenchError, "Only a synthetic lock"):
            self.ensure(fb.lock([self.impl], self.lock["lock"]["suite"]["cases"], origin="standard"))

    def test_policy_flag_variable_is_refused(self):
        with mock.patch.dict(os.environ, {"RUSTFLAGS": "-C target-cpu=native"}):
            with self.assertRaises(BenchError) as raised:
                self.ensure()
        self.assertEqual(raised.exception.exit_code, 3)
        self.assertIn("RUSTFLAGS", str(raised.exception))

    def test_role_of_another_architecture_is_refused(self):
        other = "x86-64" if fb.native_role() == "arm64" else "arm64"
        with self.assertRaisesRegex(BenchError, "this host is"):
            build.ensure(self.lock, other, self.work, jobs=1)


class StandardCheckoutTest(unittest.TestCase):
    """A standard lock with a real adapter: the checkout is refused before any recipe or tool runs."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.work = root / "work"
        url, self.commit = fb.decoder_repo(root / "synthetic-source")
        self.source = self.work / "src" / "synthetic-splot" / self.commit
        git(root, "clone", "-q", url, str(self.source))
        impl = fb.implementation("synthetic-splot", url, self.commit, adapter="splot")
        self.lock = fb.lock([impl], [fb.case(self.work, "fixture-case", "ok")], origin="standard")
        which = mock.patch("splotbench.build.shutil.which", return_value="/synthetic/tool")
        which.start()
        self.addCleanup(which.stop)

    def test_dirty_checkout_is_refused(self):
        (self.source / "decoder.py").write_text("# synthetic edit\n")
        with self.assertRaisesRegex(BenchError, "not a clean checkout"):
            build.ensure(self.lock, fb.native_role(), self.work, jobs=1)

    def test_wrong_commit_is_refused(self):
        (self.source / "extra.txt").write_text("synthetic\n")
        git(self.source, "add", "-A")
        git(self.source, "commit", "-q", "-m", "synthetic second commit")
        with self.assertRaisesRegex(BenchError, f"not a clean checkout of {self.commit}"):
            build.ensure(self.lock, fb.native_role(), self.work, jobs=1)


if __name__ == "__main__":
    unittest.main()
