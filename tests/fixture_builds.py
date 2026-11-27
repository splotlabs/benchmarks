# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Synthetic locks, sources and inputs for the build and verify tests. Everything here is synthetic."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path

from helpers import git
from splotbench import build
from splotbench.util import canonical_json, sha256_bytes

HERE = Path(__file__).resolve().parent
DECODER = HERE / "fixture_decoder.py"
TEMPLATE = json.loads((HERE / "fixtures" / "lock.json").read_text())
USAGE = "usage: decoder [--threads N] [-o PATH|-|--discard] INPUT  (synthetic fixture decoder)"
# 64x64, 8 bit, 4:2:0: 4096 luma + 2 * 1024 chroma bytes per frame, 2 frames.
RAW_BYTES = 12288


def native_role() -> str:
    return "arm64" if build.native_arch() == "aarch64" else "x86-64"


def decoder_repo(directory: Path, usage: str = USAGE) -> tuple[str, str]:
    """A Git repository with the fixture decoder as an executable `decoder.py`. Return (file URL, commit)."""
    directory.mkdir(parents=True)
    script = directory / "decoder.py"
    script.write_text(DECODER.read_text().replace(USAGE, usage))
    os.chmod(script, 0o755)
    git(directory, "init", "-q")
    git(directory, "add", "-A")
    git(directory, "commit", "-q", "-m", "synthetic fixture decoder")
    return directory.as_uri(), git(directory, "rev-parse", "HEAD")


def implementation(identifier: str, url: str, commit: str, adapter: str = "fixture") -> dict:
    return {
        "id": identifier,
        "name": f"Fixture {identifier}",
        "adapter": adapter,
        "repository": url,
        "select": commit,
        "release_tag_pattern": None,
        "release_url_template": None,
        "commit": commit,
        "identity": "development",
        "release": None,
    }


def case(work: Path, case_id: str, mode: str, **extra) -> dict:
    """Write the fixture input for `mode` into the corpus cache of `work` and return the locked case."""
    spec = canonical_json({"mode": mode, "fill": 7, "bytes": RAW_BYTES, "frames": 2, **extra})
    digest = sha256_bytes(spec)
    (work / "corpus").mkdir(parents=True, exist_ok=True)
    (work / "corpus" / (digest.removeprefix("sha256:") + ".obu")).write_bytes(spec)
    locked = copy.deepcopy(TEMPLATE["lock"]["suite"]["cases"][0])
    locked.update(id=case_id, name=f"{case_id}.obu", input_sha256=digest, bytes=len(spec))
    locked["reference"].update(sha256="sha256:" + hashlib.sha256(b"\x07" * RAW_BYTES).hexdigest(), raw_bytes=RAW_BYTES)
    return locked


def lock(implementations: list[dict], cases: list[dict], *, origin: str = "synthetic", timeout_s: int = 60) -> dict:
    document = copy.deepcopy(TEMPLATE)
    body = document["lock"]
    body["origin"] = origin
    body["implementations"] = implementations
    body["suite"]["cases"] = cases
    body["measurement"]["timeout_s"] = timeout_s
    document["lock_digest"] = sha256_bytes(canonical_json(body))
    return document
