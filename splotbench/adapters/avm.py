# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""avmdec of AVM. Inspected source facts are in docs/METHODOLOGY.md."""

from __future__ import annotations

import re
from pathlib import Path

from . import c_compiler, output_of, require_options


def tools(arch: str) -> list[str]:
    return ["cmake", "ninja", "perl", c_compiler()[0]] + (["nasm"] if arch == "x86_64" else [])


def recipe(src: Path, arch: str) -> dict:
    x86 = arch == "x86_64"
    configure = ["cmake", "-G", "Ninja", "-S", "<src>", "-B", "<build>", "-DCMAKE_BUILD_TYPE=Release"]
    configure += ["-DCONFIG_AV2_ENCODER=0", "-DENABLE_TESTS=0", "-DENABLE_DOCS=0", "-DENABLE_EXAMPLES=0"]
    configure += ["-DENABLE_TOOLS=0"]
    if x86:
        configure += ["-DENABLE_NASM=1", "-DCMAKE_C_FLAGS=-march=x86-64-v3", "-DCMAKE_CXX_FLAGS=-march=x86-64-v3"]
    toolchain = {
        "cmake": ["cmake", "--version"],
        "ninja": ["ninja", "--version"],
        "perl": ["perl", "-e", "print $^V"],
        "cc": [*c_compiler(), "--version"],
    }
    if x86:
        toolchain["nasm"] = ["nasm", "-v"]
    return {
        "steps": [configure, ["cmake", "--build", "<build>", "--target", "avmdec", "-j", "<jobs>"]],
        "binary": "<build>/avmdec",
        "lockfiles": [],
        "toolchain": toolchain,
        "target": [*c_compiler(), "-dumpmachine"],
        "simd_check": ("config/avm_config.h", "#define HAVE_AVX2 1" if x86 else "#define HAVE_NEON 1"),
        "configuration": {
            "summary": "cmake Release build of avmdec, decoder only, runtime CPU detection"
            + (", C/C++ with -march=x86-64-v3, nasm" if x86 else ", toolchain default CPU"),
            "target_cpu": "x86-64-v3" if x86 else "toolchain-default",
            "simd": "on",
            "lto": "off",
            "pgo": False,
        },
    }


def capabilities(binary: Path) -> None:
    options = ("--rawvideo", "--noblit", "--summary", "--output", "--threads")
    require_options(binary, [str(binary), "--help"], options)


def verify_command(binary: Path, input: Path, threads: int) -> list[str]:
    return [str(binary), "--rawvideo", "--summary", "-o", "-", f"--threads={threads}", str(input)]


def timed_command(binary: Path, input: Path, threads: int) -> list[str]:
    return [str(binary), "--noblit", "--summary", f"--threads={threads}", str(input)]


def version(binary: Path) -> str | None:
    # avmdec has no version option; `--help` lists the decoder with its `git describe` version.
    text = output_of([str(binary), "--help"])
    found = re.search(r"Included decoders:\s*\n((?:[ \t]+\S.*\n?)+)", text)
    return found.group(1).strip() if found else None


# The timed command always prints a frame counter (--summary prints the decoded and shown frames at the end).
TIMED_REPORTS_FRAMES = True


def parse_frames(stderr: str) -> int | None:
    found = re.findall(r"(\d+) decoded frames/(\d+) showed frames", stderr)
    return int(found[-1][1]) if found else None


def unsupported(stderr: str) -> bool:
    return bool(re.search(r"not supported by this decoder|Unrecognized option", stderr))
