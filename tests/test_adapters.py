# SPDX-License-Identifier: MIT
"""Adapter recipes, commands, capability checks and output parsing. No real decoder runs."""

import os
import tempfile
import unittest
from pathlib import Path

from splotbench import adapters
from splotbench.util import BenchError

SPLOT, DAV2D, AVM = adapters.get("splot"), adapters.get("dav2d"), adapters.get("avm")
BIN, INPUT = Path("/synthetic/bin/decoder"), Path("/synthetic/in.ivf")

# Excerpts of the help texts at the inspected commits, reduced to the lines the adapters need.
HELP = {
    "splot": "Usage: splot decode [OPTIONS] <INPUT>\n      --output-format <OUTPUT_FORMAT>\n"
    "          [possible values: y4m, raw, hash, null]\n  -o, --output <OUTPUT>\n      --threads <THREADS>\n",
    "dav2d": " --input/-i $file:        input file\n --output/-o $file:       output file\n"
    " --muxer $name:           force muxer type\n                          ('md5', 'yuv', 'yuv4mpeg2', or 'null';\n"
    " --quiet/-q:              disable status messages\n --threads $num:          number of threads\n"
    " --filmgrain $bool:       enable film grain application\n",
    "avm": "            --rawvideo                Output raw YUV frames\n"
    "            --noblit                  Don't process the decoded frames\n"
    "            --summary                 Show timing summary\n"
    "  -o <arg>, --output=<arg>          Output file name pattern (see below)\n"
    "  -t <arg>, --threads=<arg>         Max threads to use\n\nIncluded decoders:\n\n"
    "    av2    - AOMedia Project AV2 Decoder v0.0.0-synthetic\n",
}


def canned(directory: Path, text: str) -> Path:
    """A fake decoder binary that prints `text` for any arguments."""
    (directory / "help.txt").write_text(text)
    script = directory / "decoder"
    script.write_text(f"#!/bin/sh\ncat '{directory / 'help.txt'}'\n")
    os.chmod(script, 0o755)
    return script


class CommandTest(unittest.TestCase):
    def test_commands_for_the_correctness_pass(self):
        self.assertEqual(
            SPLOT.verify_command(BIN, INPUT, 4),
            [str(BIN), "decode", str(INPUT), "--output-format", "raw", "-o", "-", "--threads", "4"],
        )
        self.assertEqual(
            DAV2D.verify_command(BIN, INPUT, 4),
            [str(BIN), "-q", "-i", str(INPUT), "--muxer", "yuv", "-o", "-", "--threads", "4", "--filmgrain", "1"],
        )
        self.assertEqual(
            AVM.verify_command(BIN, INPUT, 4),
            [str(BIN), "--rawvideo", "--summary", "-o", "-", "--threads=4", str(INPUT)],
        )

    def test_timed_commands_discard_each_picture(self):
        self.assertEqual(
            SPLOT.timed_command(BIN, INPUT, 4),
            [str(BIN), "decode", str(INPUT), "--output-format", "null", "--threads", "4"],
        )
        self.assertEqual(
            DAV2D.timed_command(BIN, INPUT, 4),
            [str(BIN), "-q", "-i", str(INPUT), "--muxer", "null", "--threads", "4", "--filmgrain", "1"],
        )
        self.assertEqual(AVM.timed_command(BIN, INPUT, 4), [str(BIN), "--noblit", "--summary", "--threads=4", str(INPUT)])
        self.assertEqual(
            adapters.get("fixture").timed_command(BIN, INPUT, 4), [str(BIN), "--threads", "4", "--discard", str(INPUT)]
        )

    def test_unknown_adapter(self):
        with self.assertRaisesRegex(BenchError, "unknown"):
            adapters.get("synthetic-other")


class RecipeTest(unittest.TestCase):
    """The properties of policy optimized-source/1 in each recipe, not a copy of the argv lists."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.src = Path(temporary.name)
        (self.src / ".cargo").mkdir()
        self.config = self.src / ".cargo" / "config.toml"
        self.config.write_text(
            "[target.'cfg(target_arch = \"x86_64\")']\nrustflags = [\"-C\", \"target-cpu=x86-64-v3\"]\n"
        )
        (self.src / "Cargo.toml").write_text('[profile.release]\nlto = "thin"\n')

    def words(self, adapter, arch):
        recipe = adapter.recipe(self.src, arch)
        return recipe, [part for step in recipe["steps"] for part in step]

    def test_release_configuration_and_job_count(self):
        release = {"splot": "--release", "dav2d": "release", "avm": "-DCMAKE_BUILD_TYPE=Release"}
        for name, adapter in (("splot", SPLOT), ("dav2d", DAV2D), ("avm", AVM)):
            for arch in ("aarch64", "x86_64"):
                recipe, words = self.words(adapter, arch)
                self.assertIn(release[name], words, name)
                self.assertIn("<jobs>", recipe["steps"][-1], name)
                self.assertEqual(recipe["steps"][-1][recipe["steps"][-1].index("<jobs>") - 1], "-j", name)
                self.assertIn("<build>", " ".join(words), name)  # out of tree
                self.assertFalse(recipe["configuration"]["pgo"], name)
                self.assertEqual(recipe["configuration"]["simd"], "on", name)
                self.assertFalse(any("native" in word or "profile-" in word for word in words), name)

    def test_assembly_is_on_and_checked(self):
        for arch in ("aarch64", "x86_64"):
            recipe, words = self.words(DAV2D, arch)
            self.assertIn("-Denable_asm=true", words)
            self.assertEqual(recipe["simd_check"], ("config.h", "#define HAVE_ASM 1"))
            recipe, words = self.words(AVM, arch)
            self.assertFalse(any("ENABLE_ASM" in word or "RUNTIME_CPU_DETECT=0" in word for word in words))
            self.assertEqual(recipe["simd_check"][0], "config/avm_config.h")
        self.assertIn("HAVE_AVX2 1", AVM.recipe(self.src, "x86_64")["simd_check"][1])
        self.assertIn("HAVE_NEON 1", AVM.recipe(self.src, "aarch64")["simd_check"][1])

    def test_x86_64_v3_only_on_x86_64(self):
        for adapter in (DAV2D, AVM):
            self.assertTrue(any("-march=x86-64-v3" in word for word in self.words(adapter, "x86_64")[1]))
            self.assertFalse(any("x86-64" in word for word in self.words(adapter, "aarch64")[1]))
        self.assertEqual(SPLOT.recipe(self.src, "x86_64")["configuration"]["target_cpu"], "x86-64-v3")
        self.assertEqual(SPLOT.recipe(self.src, "aarch64")["configuration"]["target_cpu"], "toolchain-default")
        self.assertIn("nasm", DAV2D.tools("x86_64"))
        self.assertNotIn("nasm", DAV2D.tools("aarch64"))

    def test_splot_x86_64_floor_is_parsed_not_searched(self):
        self.config.write_text('# rustflags = ["-C", "target-cpu=x86-64-v3"]\n')
        with self.assertRaisesRegex(BenchError, "optimized-source/1"):
            SPLOT.recipe(self.src, "x86_64")
        self.config.write_text('[target.x86_64-unknown-linux-gnu]\nrustflags = ["-Ctarget-cpu=x86-64-v3"]\n')
        self.assertEqual(SPLOT.recipe(self.src, "x86_64")["configuration"]["target_cpu"], "x86-64-v3")
        self.config.unlink()
        with self.assertRaisesRegex(BenchError, "optimized-source/1"):
            SPLOT.recipe(self.src, "x86_64")
        self.assertEqual(SPLOT.recipe(self.src, "aarch64")["configuration"]["lto"], "thin")

    def test_splot_expects_the_pinned_rustc(self):
        self.assertEqual(SPLOT.recipe(self.src, "aarch64")["expect"], {})
        (self.src / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "1.100.0"\n')
        self.assertEqual(SPLOT.recipe(self.src, "aarch64")["expect"], {"rustc": "rustc 1.100.0 "})


class OriginTest(unittest.TestCase):
    def test_adapter_fits_origin_in_both_directions(self):
        adapters.check_origin("fixture", "synthetic", "synthetic")
        adapters.check_origin("dav2d", "standard", "synthetic")
        with self.assertRaisesRegex(BenchError, "does not fit origin 'standard'"):
            adapters.check_origin("fixture", "standard", "synthetic")
        with self.assertRaisesRegex(BenchError, "does not fit origin 'synthetic'"):
            adapters.check_origin("splot", "synthetic", "synthetic")


class CapabilityTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def test_options_present(self):
        for name, text in HELP.items():
            adapters.get(name).capabilities(canned(self.directory, text))

    def test_missing_option_is_named(self):
        text = HELP["dav2d"].replace(" --filmgrain $bool:       enable film grain application\n", "")
        with self.assertRaisesRegex(BenchError, r"does not support --filmgrain\b"):
            DAV2D.capabilities(canned(self.directory, text))

    def test_short_option_is_a_whole_token(self):
        text = HELP["splot"].replace("  -o, --output <OUTPUT>\n", "      --output <OUTPUT>\n")
        with self.assertRaisesRegex(BenchError, "does not support -o"):
            SPLOT.capabilities(canned(self.directory, text))

    def test_binary_without_execute_bit_is_an_error_not_a_traceback(self):
        script = canned(self.directory, HELP["avm"])
        os.chmod(script, 0o644)
        with self.assertRaises(BenchError) as raised:
            AVM.capabilities(script)
        self.assertNotIsInstance(raised.exception, adapters.MissingOption)
        self.assertIn("cannot run", str(raised.exception))

    def test_versions(self):
        self.assertEqual(AVM.version(canned(self.directory, HELP["avm"])), "av2    - AOMedia Project AV2 Decoder v0.0.0-synthetic")
        self.assertEqual(SPLOT.version(canned(self.directory, "splot 0.0.0\n")), "splot 0.0.0")


class OutputParsingTest(unittest.TestCase):
    def test_frame_counters(self):
        self.assertEqual(AVM.parse_frames("2 decoded frames/2 showed frames in 1 us (synthetic)\r\n"), 2)
        self.assertIsNone(AVM.parse_frames(""))
        self.assertIsNone(SPLOT.parse_frames("anything"))
        self.assertIsNone(DAV2D.parse_frames("anything"))
        self.assertEqual(adapters.get("fixture").parse_frames("fixture: decoded 3 frames\n"), 3)

    def test_unsupported_messages(self):
        self.assertTrue(SPLOT.unsupported("rule_id: x\ndetail_kind: unsupported_feature\n"))
        self.assertTrue(DAV2D.unsupported("Error decoding frame: Protocol not available\n"))
        self.assertTrue(AVM.unsupported("Failed to decode frame 1: Bitstream not supported by this decoder\n"))
        for adapter in (SPLOT, DAV2D, AVM):
            self.assertFalse(adapter.unsupported("synthetic: out of memory\n"))


if __name__ == "__main__":
    unittest.main()
