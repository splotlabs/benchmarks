# SPDX-License-Identifier: MIT
"""dav2d (`tools/dav2d`). Inspected source facts are in docs/METHODOLOGY.md."""

from __future__ import annotations

from pathlib import Path

from . import c_compiler, output_of, require_options


def tools(arch: str) -> list[str]:
    return ["meson", "ninja", c_compiler()[0]] + (["nasm"] if arch == "x86_64" else [])


def recipe(src: Path, arch: str) -> dict:
    x86 = arch == "x86_64"
    setup = ["meson", "setup", "<build>", "<src>", "--buildtype", "release", "-Ddefault_library=static"]
    setup += ["-Denable_tools=true", "-Denable_tests=false", "-Denable_docs=false", "-Denable_asm=true"]
    setup += ["-Dxxhash_muxer=disabled"] + (["-Dc_args=-march=x86-64-v3"] if x86 else [])
    toolchain = {"meson": ["meson", "--version"], "ninja": ["ninja", "--version"], "cc": [*c_compiler(), "--version"]}
    if x86:
        toolchain["nasm"] = ["nasm", "-v"]
    return {
        "steps": [setup, ["ninja", "-C", "<build>", "-j", "<jobs>", "tools/dav2d"]],
        "binary": "<build>/tools/dav2d",
        "lockfiles": [],
        "toolchain": toolchain,
        "target": [*c_compiler(), "-dumpmachine"],
        "simd_check": ("config.h", "#define HAVE_ASM 1"),
        "configuration": {
            "summary": "meson release build, static library, assembly on"
            + (", C code with -march=x86-64-v3" if x86 else ", toolchain default CPU"),
            "target_cpu": "x86-64-v3" if x86 else "toolchain-default",
            "simd": "on",
            "lto": "off",
            "pgo": False,
        },
    }


def capabilities(binary: Path) -> None:
    options = ("--input", "--output", "--muxer", "yuv", "null", "--threads", "--filmgrain", "--quiet")
    require_options(binary, [str(binary), "--help"], options)


def verify_command(binary: Path, input: Path, threads: int) -> list[str]:
    return [str(binary), "-q", "-i", str(input), "--muxer", "yuv", "-o", "-", "--threads", str(threads), "--filmgrain", "1"]


def timed_command(binary: Path, input: Path, threads: int) -> list[str]:
    # `-q`: without it dav2d prints a status line for every frame.
    return [str(binary), "-q", "-i", str(input), "--muxer", "null", "--threads", str(threads), "--filmgrain", "1"]


def version(binary: Path) -> str | None:
    return output_of([str(binary), "--version"]).strip() or None


# The timed command prints no frame counter (-q suppresses every status line).
TIMED_REPORTS_FRAMES = False


def parse_frames(stderr: str) -> int | None:
    return None  # `-q` turns the frame counter off.


def unsupported(stderr: str) -> bool:
    # ENOPROTOOPT is the "not supported" code of the library; getopt reports an unknown option.
    return "Protocol not available" in stderr or "unrecognized option" in stderr
