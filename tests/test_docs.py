# SPDX-License-Identifier: MIT
"""Tests that keep the documents true.

Every `./bench` command in a fenced `sh` or `bash` block of README.md or docs/ must parse
with the real argument parser. The documented first-host, second-host and assemble
sequences also run end to end, with a synthetic plan and the synthetic fixture decoder.
Everything here is synthetic.
"""

import glob
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import fixture_builds as fb
from helpers import ROOT, git, harness_copy, isolate_git, restore_git
from splotbench import host
from splotbench.cli import build_parser
from splotbench.util import canonical_json, sha256_bytes
from synthetic_campaign import write_corpus

DOCS = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
FENCE = re.compile(r"^```(\w*)\s*$")
PLACEHOLDER = re.compile(r"<[a-z0-9-]+>")
RUN_LINE = re.compile(r"^run (\S+) \(", re.MULTILINE)
DATASET = re.compile(r"datasets/(ds-[0-9a-f]{16})")
VALUES = {  # test values for the documented placeholders (parse tests)
    "<plan>": "plan.toml", "<lock>": "lock.json", "<role>": "host-a", "<machine-id>": "synthetic-host",
    "<work>": "work", "<run-id>": "host-a-20260101T000000Z-0123abcd", "<bundle>": "bundle.tar.gz",
    "<bundle-a>": "a.tar.gz", "<bundle-b>": "b.tar.gz", "<first-bundle>": "a.tar.gz",
    "<other-bundle>": "c.tar.gz", "<out>": "out", "<out2>": "out2", "<dataset-id>": "ds-0123456789abcdef",
    "<dataset_id>": "ds-0123456789abcdef",
}  # fmt: skip


def setUpModule():
    isolate_git()


def tearDownModule():
    restore_git()


def section(path: Path, heading: str) -> str:
    """The text from the heading line that starts with `heading` to the next heading of the same or a higher level."""
    lines, out, level = path.read_text().splitlines(), [], None
    for line in lines:
        if level is None:
            if line.startswith(heading):
                level = len(line) - len(line.lstrip("#"))
        elif re.match(rf"^#{{1,{level}}} ", line):
            break
        if level is not None:
            out.append(line)
    assert out, f"{path}: no heading {heading!r}"
    return "\n".join(out)


def commands(text: str) -> list[str]:
    """The lines that start with `./bench` in the fenced sh and bash blocks of `text`."""
    found, language = [], None
    for line in text.splitlines():
        match = FENCE.match(line.strip())
        if match:
            language = None if language is not None else match.group(1)
        elif language in ("sh", "bash") and line.strip().startswith("./bench"):
            found.append(line.strip())
    return found


def arguments(line: str, values: dict[str, str]) -> list[str]:
    """Split a documented line, drop its comment, replace the placeholders and expand globs."""
    words = shlex.split(line, comments=True)

    def fill(word: str) -> str:
        return PLACEHOLDER.sub(lambda m: values[m.group(0)], word)  # a KeyError: an undocumented placeholder

    expanded = []
    for word in map(fill, words):
        expanded += sorted(glob.glob(word)) or [word] if "*" in word else [word]
    return expanded


def check_parses(parser, argv: list[str]) -> None:
    """Parse `argv` (without `./bench`). Argparse accepts an abbreviated option, so also require the exact spelling."""
    subparser = parser._subparsers._group_actions[0].choices[argv[0]]
    parser.parse_args(argv)
    for word in argv[1:]:
        if word.startswith("--") and word.split("=")[0] not in subparser._option_string_actions:
            raise SystemExit(f"{word} is not an option of bench {argv[0]}")


class DocCommandsParseTest(unittest.TestCase):
    def test_every_documented_command_parses(self):
        parser, count = build_parser(), 0
        for path in DOCS:
            for line in commands(path.read_text()):
                with self.subTest(file=path.name, line=line):
                    words = shlex.split(line, comments=True)
                    argv = [PLACEHOLDER.sub(lambda m: VALUES[m.group(0)], word) for word in words]
                    try:
                        check_parses(parser, argv[1:])
                    except SystemExit:
                        self.fail("the argument parser refuses this command")
                    count += 1
        self.assertGreater(count, 30)

    def test_a_renamed_option_would_be_found(self):
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
            check_parses(build_parser(), ["run", "--lock", "l", "--role", "r", "--work", "w", "--machine", "m"])

    def test_runbooks_use_each_command_they_describe(self):
        used = {
            name
            for path in DOCS
            for line in commands(path.read_text())
            for name in [shlex.split(line, comments=True)[1]]
        }
        self.assertEqual(
            used, {"assemble", "bundle", "doctor", "identity", "lock", "prepare", "publish", "run", "validate", "verify"}
        )


def synopsis() -> dict[str, set[str]]:
    """The long options per command in the README synopsis (the `text` block of the section Use)."""
    block = section(ROOT / "README.md", "## Use").split("```text\n")[1].split("```")[0]
    found, command = {}, None
    for line in block.splitlines():
        if line.startswith("./bench "):
            command = line.split()[1]
            found[command] = set()
        text = line.split("#")[0]
        found[command] |= set(re.findall(r"--[a-z][a-z-]*", text))
        if re.search(r" -o ", text):  # the short form of --output
            found[command].add("--output")
    return found


class ReadmeSynopsisTest(unittest.TestCase):
    def test_synopsis_matches_the_parser(self):
        parser = build_parser()
        choices = parser._subparsers._group_actions[0].choices
        found = synopsis()
        self.assertEqual(set(found) - {"--help", "--version"}, set(choices))
        for name, subparser in choices.items():
            options = {o for a in subparser._actions for o in a.option_strings if o.startswith("--")} - {"--help"}
            with self.subTest(command=name):
                self.assertEqual(found[name], options)


class DocSequencesTest(unittest.TestCase):
    """Run the documented sequences with a synthetic plan: two roles with the native architecture."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.harness = harness_copy(self.root / "harness", with_git=False)
        shutil.copytree(ROOT / "schemas", self.harness / "schemas")  # the copy needs the schemas to validate
        git(self.harness, "init", "-q")
        git(self.harness, "add", "-A")
        git(self.harness, "commit", "-q", "-m", "synthetic harness copy")
        self.plan = self.make_plan()

    def make_plan(self) -> Path:
        directory = self.root / "plan"
        directory.mkdir()
        spec = canonical_json({"mode": "ok", "fill": 7, "bytes": fb.RAW_BYTES, "frames": 2})
        write_corpus(directory, {"synthetic-8bit": (8, spec)})
        corpus = directory / "corpus.toml"
        corpus.write_text(corpus.read_text().replace("6" * 64, sha256_bytes(b"\x07" * fb.RAW_BYTES).removeprefix("sha256:")))
        arch = host.native_arch()
        text = [
            'name = "synthetic-docs"', 'origin = "synthetic"', 'suite = "quick"', 'corpus = "corpus.toml"',
            'suites = "suites.toml"', "warmups = 1", "repetitions = 2", "timeout_s = 60", "order_seed = 1",
            "noise_max_range_permille = 50",
        ]  # fmt: skip
        for name in ("fixture-a", "fixture-b"):
            url, commit = fb.decoder_repo(directory / f"source-{name}")
            text += ["[[implementation]]", f'id = "{name}"', 'adapter = "fixture"', f'repository = "{url}"', f'select = "{commit}"']
        text += ["[roles.host-a]", f'arch = "{arch}"', "threads = [1, 2]", "[roles.host-b]", f'arch = "{arch}"', "threads = [1]"]
        path = directory / "plan.toml"
        path.write_text("\n".join(text) + "\n")
        return path

    def bench(self, argv: list[str]) -> subprocess.CompletedProcess:
        assert argv[0] == "./bench", argv
        return subprocess.run([sys.executable, str(self.harness / "bench"), *argv[1:]], capture_output=True, text=True, cwd=self.harness)

    def play(self, text: str, values: dict[str, str], *, expect_failure=(), skip=()) -> None:
        """Run the `./bench` lines of `text` in order. A line with a word of `expect_failure` must exit 1."""
        lines = commands(text)
        self.assertTrue(lines)
        for line in lines:
            if any(word in line for word in skip):
                continue
            with self.subTest(line=line):
                argv = arguments(line, values)
                done = self.bench(argv)
                wanted = 1 if any(word in line for word in expect_failure) else 0
                self.assertEqual(done.returncode, wanted, f"{line}\n{done.stdout}\n{done.stderr}")
                if found := RUN_LINE.search(done.stdout):
                    values["<run-id>"] = found.group(1)
                if found := DATASET.search(done.stdout):
                    values["<dataset-id>"] = found.group(1)

    def host_values(self, name: str, role: str) -> dict[str, str]:
        directory = self.root / name
        directory.mkdir()
        return {
            **VALUES, "<plan>": str(self.plan), "<lock>": str(directory / "lock.json"), "<role>": role,
            "<machine-id>": f"synthetic-{name}", "<work>": str(directory / "work"), "<bundle>": str(directory / "bundle.tar.gz"),
        }  # fmt: skip

    def test_first_host_second_host_and_assembly(self):
        first = self.host_values("first", "host-a")
        procedure = section(ROOT / "docs/RUNBOOK-FIRST-HOST.md", "## Procedure")
        lock_line = next(line for line in commands(procedure) if line.startswith("./bench lock"))
        done = self.bench(arguments(lock_line, first))  # the owner gave a plan: the documented lock command
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertRegex(done.stdout, r"(?m)^digest sha256:[0-9a-f]{64}$")
        self.play(procedure, first, skip=("./bench lock",))
        run_a = first["<run-id>"]

        second = self.host_values("second", "host-b")
        (self.root / "second/lock.json").write_bytes((self.root / "first/lock.json").read_bytes())
        second["<first-bundle>"] = first["<bundle>"]
        second["<out>"] = str(self.root / "out-second")
        self.play(section(ROOT / "docs/RUNBOOK-SECOND-HOST.md", "## Procedure"), second)
        self.assertNotEqual(first["<run-id>"], second["<run-id>"])
        self.assertTrue(second["<run-id>"].startswith("host-b-"))

        values = {
            **VALUES, "<lock>": first["<lock>"], "<bundle-a>": first["<bundle>"], "<bundle-b>": second["<bundle>"],
            "<other-bundle>": str(self.root / "copy.tar.gz"), "<out>": str(self.root / "out"), "<out2>": str(self.root / "out2"),
            "<role>": "host-a", "<run-id>": run_a,
        }  # fmt: skip
        (self.root / "copy.tar.gz").write_bytes(Path(first["<bundle>"]).read_bytes())  # the same run, as another file
        runbook = ROOT / "docs/RUNBOOK-ASSEMBLE-PUBLISH.md"
        self.play(section(runbook, "## Part 1"), values, expect_failure=("--official",))  # a synthetic lock is never official
        dataset = self.root / "out/datasets" / values["<dataset-id>"]
        self.assertTrue((dataset / "summary.json").is_file())
        self.assertIn('"origin": "synthetic"', (dataset / "manifest.json").read_text())
        self.assertIn('"complete": true', (dataset / "manifest.json").read_text())
        self.assertTrue((self.root / "out2/datasets" / values["<dataset-id>"] / "SHA256SUMS").is_file())
        # Publication is explicit and refuses synthetic data; nothing is written.
        self.play(section(runbook, "## Part 2"), values, expect_failure=("publish",))
        self.assertFalse((self.harness / "published").exists())

    def test_reproduction_guide(self):
        values = self.host_values("community", "host-a")
        values["<out>"] = str(self.root / "out-community")
        self.play((ROOT / "docs/REPRODUCE.md").read_text(), values)
        dataset = self.root / "out-community/datasets" / values["<dataset-id>"]
        self.assertIn('"origin": "synthetic"', (dataset / "manifest.json").read_text())
        self.assertIn('"complete": false', (dataset / "manifest.json").read_text())  # the other role did not run


if __name__ == "__main__":
    unittest.main()
