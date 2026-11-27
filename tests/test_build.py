# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Tests for `build.ensure` with the fixture decoder from synthetic file:// repositories."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fixture_builds as fb
from helpers import git, isolate_git, restore_git
from splotbench import build, provenance, schema
from splotbench.adapters import fixture
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
        environ = mock.patch.dict(os.environ, {**clean, "CC": "synthetic-cc", "SYNTHETIC_SECRET": "x", "CPPFLAGS": "-DSYNTHETIC", "RUSTUP_TOOLCHAIN": "x"}, clear=True)
        environ.start()
        self.addCleanup(environ.stop)

    def ensure(self, lock=None, **options):
        return build.ensure(lock or self.lock, fb.native_role(), self.work, jobs=1, **options)

    def directory(self):
        return self.work / "build" / "fixture-a" / self.commit

    def marked(self):
        """Mark the build log; a rebuild removes the mark."""
        (self.directory() / "build.log").write_text("synthetic marker\n")
        return lambda: (self.directory() / "build.log").read_text() == "synthetic marker\n"

    def recipe_with(self, **changes):
        original = fixture.recipe

        def changed(src, arch):
            recipe = original(src, arch)
            for key, value in changes.items():
                recipe[key] = value(recipe) if callable(value) else value
            return recipe

        return mock.patch.object(fixture, "recipe", changed)

    def edit_manifest(self, change):
        path = self.directory() / "build.json"
        manifest = json.loads(path.read_text())
        change(manifest)
        manifest["build_id"] = provenance.build_id(manifest)
        path.write_text(json.dumps(manifest))

    def test_build_steps_get_only_the_permitted_environment(self):
        steps = lambda recipe: [*recipe["steps"], ["env"]]  # noqa: E731
        with self.recipe_with(steps=steps):
            (manifest,) = self.ensure()
        log = (self.directory() / "build.log").read_text()
        self.assertIn("LC_ALL=C", log)
        self.assertIn("CC=synthetic-cc", log)
        for name in ("CPPFLAGS", "RUSTUP_TOOLCHAIN", "SYNTHETIC_SECRET"):
            self.assertNotIn(name, log)
        self.assertEqual(manifest["environment"], {"CC": "synthetic-cc"})

    def test_configuration_change_rebuilds(self):
        self.ensure()
        unchanged = self.marked()
        with self.recipe_with(configuration=lambda recipe: {**recipe["configuration"], "summary": "synthetic other"}):
            (manifest,) = self.ensure()
        self.assertFalse(unchanged())
        self.assertEqual(manifest["configuration"]["summary"], "synthetic other")

    def test_snapshot_change_rebuilds(self):
        self.ensure()
        self.edit_manifest(lambda m: m["source"]["snapshot"].update(snapshot_sha256="sha256:" + "0" * 64))
        unchanged = self.marked()
        (manifest,) = self.ensure()
        self.assertFalse(unchanged())
        self.assertNotEqual(manifest["source"]["snapshot"]["snapshot_sha256"], "sha256:" + "0" * 64)

    def test_reuse_takes_provenance_from_the_current_lock(self):
        self.ensure()
        unchanged = self.marked()
        release = {"tag": "v1.0.0", "version": "1.0.0", "url": "https://example.invalid/synthetic/v1.0.0"}
        impl = {**self.impl, "identity": "release", "release": release}
        (manifest,) = self.ensure(fb.lock([impl], self.lock["lock"]["suite"]["cases"]))
        self.assertTrue(unchanged())  # reused, not built again
        self.assertEqual((manifest["source"]["release"], manifest["source"]["build_origin"]), (release, "source-release"))
        self.assertEqual(json.loads((self.directory() / "build.json").read_text()), manifest)
        self.assertEqual(schema.validate(manifest), [])

    def test_a_manifest_of_another_class_is_not_reused(self):
        self.ensure()
        self.edit_manifest(lambda m: m.update({"class": "verified"}))
        unchanged = self.marked()
        (manifest,) = self.ensure()
        self.assertFalse(unchanged())
        self.assertEqual(manifest["class"], "synthetic")

    def test_offline_with_a_checkout_but_no_build(self):
        self.ensure()
        shutil.rmtree(self.work / "build")
        with self.assertRaises(BenchError) as raised:
            self.ensure(offline=True)
        self.assertEqual(raised.exception.exit_code, 3)
        self.assertIn("offline: a finished fixture-a build", str(raised.exception))

    def test_missing_tool_exits_3(self):
        real = shutil.which
        with mock.patch("splotbench.build.shutil.which", lambda name, **kw: None if name == "install" else real(name, **kw)):
            with self.assertRaises(BenchError) as raised:
                self.ensure()
        self.assertEqual(raised.exception.exit_code, 3)
        self.assertIn("install is not on PATH", str(raised.exception))

    def test_toolchain_other_than_the_pinned_one_is_refused(self):
        with self.recipe_with(expect={"python3": "Python 0.0.0-synthetic "}):
            with self.assertRaises(BenchError) as raised:
                self.ensure()
        self.assertEqual(raised.exception.exit_code, 3)
        self.assertIn("pins 'Python 0.0.0-synthetic'", str(raised.exception))

    def test_failed_simd_check_stops_the_build(self):
        with self.recipe_with(simd_check=("decoder", "#define HAVE_ASM 1")):
            with self.assertRaisesRegex(BenchError, "cannot follow optimized-source/1"):
                self.ensure()
        self.assertFalse((self.directory() / "build.json").exists())

    def test_build_that_writes_into_the_checkout_is_refused(self):
        steps = lambda recipe: [*recipe["steps"], ["touch", "<src>/stray.txt"]]  # noqa: E731
        with self.recipe_with(steps=steps):
            with self.assertRaisesRegex(BenchError, "changed its source tree"):
                self.ensure()

    def test_synthetic_lock_with_a_real_adapter_is_refused(self):
        impl = {**self.impl, "adapter": "dav2d"}
        with self.assertRaisesRegex(BenchError, "does not fit origin 'synthetic'"):
            self.ensure(fb.lock([impl], self.lock["lock"]["suite"]["cases"]))

    def test_manifest_is_complete_and_valid(self):
        (manifest,) = self.ensure()
        directory = self.work / "build" / "fixture-a" / self.commit
        self.assertEqual(schema.validate(manifest), [])
        self.assertEqual(json.loads((directory / "build.json").read_text()), manifest)
        self.assertTrue((directory / "build.log").read_text().startswith("$ install "))
        self.assertEqual(manifest["class"], "synthetic")
        self.assertEqual(manifest["binary_path"], f"build/fixture-a/{self.commit}/decoder")
        self.assertEqual(manifest["source"]["commit"], self.commit)
        self.assertEqual(manifest["source"]["build_origin"], "source-development")
        self.assertFalse(manifest["source"]["snapshot"]["dirty"])
        self.assertEqual(manifest["configuration"]["arguments"], ["install", "-m", "755", "<src>/decoder.py", "<build>/decoder"])
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
        with self.assertRaisesRegex(BenchError, "Only a synthetic campaign"):
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

    def test_ignored_files_in_the_checkout_are_refused(self):
        (self.source / ".git" / "info" / "exclude").write_text("*.o\n")
        (self.source / "stale.o").write_text("synthetic\n")
        with self.assertRaisesRegex(BenchError, "extra files: !! stale.o"):
            build.ensure(self.lock, fb.native_role(), self.work, jobs=1)

    def test_wrong_commit_is_refused(self):
        (self.source / "extra.txt").write_text("synthetic\n")
        git(self.source, "add", "-A")
        git(self.source, "commit", "-q", "-m", "synthetic second commit")
        with self.assertRaisesRegex(BenchError, f"not a clean checkout of {self.commit}"):
            build.ensure(self.lock, fb.native_role(), self.work, jobs=1)



class CargoConfigTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.src = self.root / "work" / "src" / "splot" / "c"
        self.src.mkdir(parents=True)
        self.env = {"HOME": str(self.root / "home"), "PATH": os.environ["PATH"]}

    def write(self, directory: Path, name: str, text: str) -> Path:
        (directory / ".cargo").mkdir(parents=True, exist_ok=True)
        path = directory / ".cargo" / name
        path.write_text(text)
        return path

    def test_build_affecting_tables_are_found_in_parents_and_home(self):
        self.assertEqual(build.cargo_config_problems(self.src, self.env), [])
        parent = self.write(self.root / "work", "config.toml", "[profile.release]\nlto = false\n")
        home = self.write(self.root / "home", "config", "[target.x86_64-unknown-linux-gnu]\nrustflags = []\n")
        self.write(self.src, "config.toml", "[build]\n")  # the checkout's own file is part of the snapshot
        self.assertEqual(
            build.cargo_config_problems(self.src, self.env), [f"{parent.resolve()} (profile)", f"{home} (target)"]
        )

    def test_cargo_home_replaces_the_home_directory(self):
        self.write(self.root / "home", "config.toml", "[env]\nX = '1'\n")
        self.write(self.root / "cargo-home", "config.toml", '[net]\ngit-fetch-with-cli = true\n')
        env = {**self.env, "CARGO_HOME": str(self.root / "cargo-home" / ".cargo")}
        self.assertEqual(build.cargo_config_problems(self.src, env), [])
        self.assertEqual(len(build.cargo_config_problems(self.src, self.env)), 1)


if __name__ == "__main__":
    unittest.main()
