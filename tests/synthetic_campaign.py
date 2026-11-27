# SPDX-License-Identifier: MIT
"""Build synthetic campaigns for tests: file:// Git repositories, file: streams and a corpus manifest."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from helpers import ROOT, git, make_repo
from splotbench import plan

HARNESS = {
    "vcs": "git",
    "commit": "1" * 40,
    "tree": "2" * 40,
    "dirty": False,
    "snapshot_sha256": "sha256:" + "3" * 64,
    "submodules": [],
}
# The fixture adapter builds this script, so `bench prepare` can build the synthetic implementations.
DECODER = (ROOT / "tests" / "fixture_decoder.py").read_bytes()
STREAMS = {"synthetic-8bit": (8, b"synthetic stream one " * 20), "synthetic-10bit": (10, b"synthetic stream two " * 30)}


def write_corpus(directory: Path, streams: dict[str, tuple[int, bytes]] = STREAMS) -> None:
    """Write the stream files, `corpus.toml` and `suites.toml` (quick: first stream, full: all) into `directory`."""
    lines = [
        'id = "synthetic-corpus"',
        'source = "https://example.invalid/synthetic/corpus"',
        "[imported_from]",
        'repository = "https://example.invalid/synthetic/splot"',
        f'commit = "{"4" * 40}"',
        'path = "tests/conformance/synthetic.toml"',
        f'sha256 = "sha256:{"5" * 64}"',
        "[oracle]",
        'description = "synthetic oracle, not a real decoder"',
    ]
    for name, (depth, data) in streams.items():
        (directory / f"{name}.ivf").write_bytes(data)
        lines += [
            "[[case]]",
            f'id = "{name}"',
            f'name = "{name}.ivf"',
            f'url = "{(directory / f"{name}.ivf").as_uri()}"',
            f'input_sha256 = "sha256:{hashlib.sha256(data).hexdigest()}"',
            f"bytes = {len(data)}",
            "width = 64",
            "height = 64",
            f"bit_depth = {depth}",
            'chroma = "4:2:0"',
            "frames = 2",
            f'reference_sha256 = "sha256:{"6" * 64}"',
            "raw_bytes = 12288",
        ]
    (directory / "corpus.toml").write_text("\n".join(lines) + "\n")
    ids = list(streams)
    (directory / "suites.toml").write_text(f"quick = {json.dumps(ids[:1])}\nfull = {json.dumps(ids)}\n")


def make_plan(directory: Path, *, replace: dict[str, str] | None = None, select_a: str = "main") -> Path:
    """Write the synthetic plan, its corpus files and two repositories; return the plan path.

    `replace` maps a plan text to another text, for example `"repetitions = 3"` to `"repetitions = 4"`.
    """
    directory.mkdir(parents=True, exist_ok=True)
    if not (directory / "corpus.toml").exists():
        write_corpus(directory)
    repo_a = directory / "repo-a" if (directory / "repo-a").exists() else make_repo(directory / "repo-a", {"a": b"a", "decoder.py": DECODER})
    repo_b = directory / "repo-b" if (directory / "repo-b").exists() else make_repo(directory / "repo-b", {"b": b"b", "decoder.py": DECODER})
    git(repo_a, "branch", "-M", "main")
    git(repo_b, "branch", "-M", "main")
    text = (ROOT / "tests/fixtures/plan-synthetic.toml").read_text()
    text = text.replace("@REPO_A@", repo_a.as_uri()).replace("@REPO_B@", repo_b.as_uri()).replace("@SELECT_A@", select_a)
    for old, new in (replace or {}).items():
        assert old in text, old
        text = text.replace(old, new)
    path = directory / "plan.toml"
    path.write_text(text)
    return path


def make_lock(directory: Path, **kwargs) -> dict:
    return plan.build_lock(make_plan(directory, **kwargs), harness=HARNESS)
