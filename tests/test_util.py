# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Tests for the shared helpers. All data is synthetic."""

import tempfile
import unittest
from pathlib import Path

from splotbench import util

SHA256_ABC = "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


class CanonicalJsonTest(unittest.TestCase):
    def test_known_bytes(self):
        value = {"b": 1, "a": ["é", None, True], "c": {"z": "", "y": -2}}
        self.assertEqual(
            util.canonical_json(value),
            b'{"a":["\xc3\xa9",null,true],"b":1,"c":{"y":-2,"z":""}}',
        )

    def test_key_order_does_not_change_bytes(self):
        self.assertEqual(util.canonical_json({"x": 1, "y": [1, 2]}), util.canonical_json({"y": [1, 2], "x": 1}))

    def test_float_is_rejected_at_any_depth(self):
        for value in (1.5, {"a": [{"b": 0.0}]}, [1, (2, 3.25)]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                util.canonical_json(value)


class DigestTest(unittest.TestCase):
    def test_bytes_and_file_digests_agree_with_known_value(self):
        self.assertEqual(util.sha256_bytes(b"abc"), SHA256_ABC)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, "synthetic.bin")
            path.write_bytes(b"abc")
            self.assertEqual(util.sha256_file(path), SHA256_ABC)


class WriteAtomicTest(unittest.TestCase):
    def test_replaces_content_and_leaves_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, "synthetic.json")
            path.write_bytes(b"old")
            util.write_atomic(path, b"new")
            self.assertEqual(path.read_bytes(), b"new")
            self.assertEqual(sorted(p.name for p in Path(temporary).iterdir()), ["synthetic.json"])

    def test_failed_write_keeps_the_old_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, "synthetic.json")
            path.write_bytes(b"old")
            with self.assertRaises(TypeError):
                util.write_atomic(path, "not bytes")
            self.assertEqual(path.read_bytes(), b"old")
            self.assertEqual(sorted(p.name for p in Path(temporary).iterdir()), ["synthetic.json"])


class SubprocessTest(unittest.TestCase):
    def test_recorded_env_keeps_only_the_allow_list(self):
        environ = {"CC": "cc", "PATH": "/synthetic/bin", "AWS_SECRET_ACCESS_KEY": "synthetic", "HOME": "/synthetic"}
        self.assertEqual(util.recorded_env(environ), {"CC": "cc"})

    def test_missing_tool_is_a_missing_prerequisite(self):
        with self.assertRaises(util.BenchError) as caught:
            util.run(["splotbench-synthetic-missing-tool"])
        self.assertEqual(caught.exception.exit_code, 3)
        self.assertIn("splotbench-synthetic-missing-tool is not on PATH", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
