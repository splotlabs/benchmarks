# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Fixture decoder for synthetic locks only: a Python script that plays a decoder (see tests/fixture_decoder.py).

The "build" installs `decoder.py` of the pinned synthetic source into the build tree.
`build.ensure` refuses this adapter in a standard lock.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import output_of, require_options


def tools(arch: str) -> list[str]:
    return ["install", "python3"]


def recipe(src: Path, arch: str) -> dict:
    return {
        "steps": [["install", "-m", "755", "<src>/decoder.py", "<build>/decoder"]],
        "binary": "<build>/decoder",
        "lockfiles": [],
        "toolchain": {"python3": ["python3", "--version"]},
        "target": ["python3", "-c", "import platform; print('synthetic-' + platform.machine())"],
        "simd_check": None,
        "configuration": {
            "summary": "synthetic fixture build: install decoder.py",
            "target_cpu": "toolchain-default",
            "simd": "off",
            "lto": "off",
            "pgo": False,
        },
    }


def capabilities(binary: Path) -> None:
    require_options(binary, [str(binary), "--help"], ("--threads", "-o", "--discard"))


def verify_command(binary: Path, input: Path, threads: int) -> list[str]:
    return [str(binary), "--threads", str(threads), "-o", "-", str(input)]


def timed_command(binary: Path, input: Path, threads: int) -> list[str]:
    return [str(binary), "--threads", str(threads), "--discard", str(input)]


def version(binary: Path) -> str | None:
    return output_of([str(binary), "--version"]).strip() or None


def parse_frames(stderr: str) -> int | None:
    found = re.findall(r"fixture: decoded (\d+) frames", stderr)
    return int(found[-1]) if found else None


def unsupported(stderr: str) -> bool:
    return "unsupported stream" in stderr or "unrecognized option" in stderr
