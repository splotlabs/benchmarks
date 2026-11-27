# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
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
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.src = Path(temporary.name)
        (self.src / ".cargo").mkdir()
        (self.src / ".cargo" / "config.toml").write_text(
            "[target.'cfg(target_arch = \"x86_64\")']\nrustflags = [\"-C\", \"target-cpu=x86-64-v3\"]\n"
        )
        (self.src / "Cargo.toml").write_text('[profile.release]\nlto = "thin"\n')

    def test_splot(self):
        recipe = SPLOT.recipe(self.src, "aarch64")
        self.assertEqual(
            recipe["steps"],
            [["cargo", "build", "--release", "-p", "splot-cli", "--locked", "--target-dir", "<build>", "-j", "<jobs>"]],
        )
        self.assertEqual((recipe["binary"], recipe["lockfiles"]), ("<build>/release/splot", ["Cargo.lock"]))
        self.assertEqual((recipe["configuration"]["target_cpu"], recipe["configuration"]["lto"]), ("toolchain-default", "thin"))
        self.assertEqual(SPLOT.recipe(self.src, "x86_64")["configuration"]["target_cpu"], "x86-64-v3")

    def test_splot_without_the_x86_64_v3_floor_is_refused_on_x86_64(self):
        (self.src / ".cargo" / "config.toml").write_text("# synthetic: no rustflags\n")
        with self.assertRaisesRegex(BenchError, "optimized-source/1"):
            SPLOT.recipe(self.src, "x86_64")
        self.assertEqual(SPLOT.recipe(self.src, "aarch64")["configuration"]["simd"], "on")

    def test_dav2d(self):
        setup = ["meson", "setup", "<build>", "<src>", "--buildtype", "release", "-Ddefault_library=static"]
        setup += ["-Denable_tools=true", "-Denable_tests=false", "-Denable_docs=false", "-Denable_asm=true"]
        setup += ["-Dxxhash_muxer=disabled"]
        build = ["ninja", "-C", "<build>", "-j", "<jobs>", "tools/dav2d"]
        self.assertEqual(DAV2D.recipe(self.src, "aarch64")["steps"], [setup, build])
        self.assertEqual(DAV2D.recipe(self.src, "x86_64")["steps"], [[*setup, "-Dc_args=-march=x86-64-v3"], build])
        self.assertEqual(DAV2D.recipe(self.src, "aarch64")["binary"], "<build>/tools/dav2d")
        self.assertIn("nasm", DAV2D.tools("x86_64"))
        self.assertNotIn("nasm", DAV2D.tools("aarch64"))

    def test_avm(self):
        configure = ["cmake", "-G", "Ninja", "-S", "<src>", "-B", "<build>", "-DCMAKE_BUILD_TYPE=Release"]
        configure += ["-DCONFIG_AV2_ENCODER=0", "-DENABLE_TESTS=0", "-DENABLE_DOCS=0", "-DENABLE_EXAMPLES=0", "-DENABLE_TOOLS=0"]
        build = ["cmake", "--build", "<build>", "--target", "avmdec", "-j", "<jobs>"]
        self.assertEqual(AVM.recipe(self.src, "aarch64")["steps"], [configure, build])
        x86 = [*configure, "-DENABLE_NASM=1", "-DCMAKE_C_FLAGS=-march=x86-64-v3", "-DCMAKE_CXX_FLAGS=-march=x86-64-v3"]
        self.assertEqual(AVM.recipe(self.src, "x86_64")["steps"], [x86, build])
        self.assertEqual(AVM.recipe(self.src, "aarch64")["simd_check"], ("config/avm_config.h", "#define HAVE_NEON 1"))

    def test_no_recipe_uses_native_or_pgo(self):
        for adapter in (SPLOT, DAV2D, AVM):
            for arch in ("aarch64", "x86_64"):
                recipe = adapter.recipe(self.src, arch)
                text = " ".join(part for step in recipe["steps"] for part in step)
                self.assertNotIn("native", text)
                self.assertNotIn("profile", text)
                self.assertFalse(recipe["configuration"]["pgo"])


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
