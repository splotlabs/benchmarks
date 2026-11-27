# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Splot (`splot decode`). Inspected source facts are in docs/METHODOLOGY.md."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from ..util import BenchError
from . import output_of, require_options

X86_CPU = "target-cpu=x86-64-v3"


def tools(arch: str) -> list[str]:
    return ["cargo"]


def recipe(src: Path, arch: str) -> dict:
    if arch == "x86_64" and X86_CPU not in (src / ".cargo" / "config.toml").read_text(errors="replace"):
        raise BenchError(
            f"{src}/.cargo/config.toml does not set {X86_CPU}, so the build cannot follow optimized-source/1 "
            "on x86-64. Lock a Splot revision that sets it."
        )
    lto = tomllib.loads((src / "Cargo.toml").read_text())["profile"]["release"].get("lto", False)
    return {
        "steps": [
            ["cargo", "build", "--release", "-p", "splot-cli", "--locked", "--target-dir", "<build>", "-j", "<jobs>"]
        ],
        "binary": "<build>/release/splot",
        "lockfiles": ["Cargo.lock"],
        "toolchain": {"rustc": ["rustc", "-V"], "cargo": ["cargo", "-V"]},
        "target": ["rustc", "-vV"],
        "simd_check": None,  # Splot selects its SIMD backend at run time; there is no build switch.
        "configuration": {
            "summary": f"cargo release profile (lto {lto}), package splot-cli, --locked; "
            + ("target-cpu x86-64-v3 from .cargo/config.toml" if arch == "x86_64" else "toolchain default CPU"),
            "target_cpu": "x86-64-v3" if arch == "x86_64" else "toolchain-default",
            "simd": "on",
            "lto": str(lto),
            "pgo": False,
        },
    }


def capabilities(binary: Path) -> None:
    options = ("--output-format", "raw", "null", "-o", "--threads")
    require_options(binary, [str(binary), "decode", "--help"], options)


def verify_command(binary: Path, input: Path, threads: int) -> list[str]:
    return [str(binary), "decode", str(input), "--output-format", "raw", "-o", "-", "--threads", str(threads)]


def timed_command(binary: Path, input: Path, threads: int) -> list[str]:
    # The null format runs the discard path: film grain applied, no raw serialisation.
    return [str(binary), "decode", str(input), "--output-format", "null", "--threads", str(threads)]


def version(binary: Path) -> str | None:
    return output_of([str(binary), "--version"]).strip() or None


def parse_frames(stderr: str) -> int | None:
    return None  # `splot decode` prints no frame counter.


def unsupported(stderr: str) -> bool:
    return bool(re.search(r"detail_kind: unsupported_|unexpected argument", stderr))
