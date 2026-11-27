# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Decoder adapters. Each module has the same functions; `docs/METHODOLOGY.md` is the contract.

recipe(src, arch)        build recipe: steps, binary, lock files, toolchain, configuration
tools(arch)              programs the recipe needs
capabilities(binary)     check the real binary for every option that `command` passes
verify_command(binary, input, threads)  argv of the correctness pass: raw output on stdout
timed_command(binary, input, threads)   argv of the timed run: decode, film grain, discard each picture
version(binary)          raw version text; never release proof
parse_frames(stderr)     frame count that the decode prints, or None
unsupported(stderr)      True when the decoder says it cannot decode the stream or an option
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

from ..util import EXIT_PREREQUISITE, BenchError

HELP_TIMEOUT_S = 60


class MissingOption(BenchError):
    """The binary runs, but its help text does not name an option that the adapter passes."""


def check_origin(adapter: str, origin: str, where: str) -> None:
    """A synthetic campaign uses only the fixture adapter; a standard campaign never uses it."""
    if (adapter == "fixture") != (origin == "synthetic"):
        raise BenchError(
            f"{where}: {adapter!r} does not fit origin {origin!r}. "
            "Only a synthetic campaign may use the 'fixture' adapter, and it may use only that adapter."
        )


def get(name: str):
    from . import avm, dav2d, fixture, splot  # the adapter modules import the helpers below

    adapters = {"splot": splot, "dav2d": dav2d, "avm": avm, "fixture": fixture}
    try:
        return adapters[name]
    except KeyError:
        raise BenchError(f"adapter {name!r} is unknown; this harness has {', '.join(adapters)}.") from None


def c_compiler() -> list[str]:
    """The C compiler that Meson and CMake select: `CC` when it is set, else `cc`."""
    return shlex.split(os.environ.get("CC", "cc")) or ["cc"]


def output_of(argv: list[str]) -> str:
    """Run a short informational command (help, version) and return stdout and stderr as text."""
    try:
        done = subprocess.run(argv, capture_output=True, timeout=HELP_TIMEOUT_S, check=False)
    except FileNotFoundError:
        raise BenchError(f"{argv[0]} does not exist. Run the build again.", EXIT_PREREQUISITE) from None
    except OSError as error:
        raise BenchError(f"cannot run {argv[0]}: {error.strerror}. Run the build again.", EXIT_PREREQUISITE) from None
    except subprocess.TimeoutExpired:
        raise BenchError(f"`{' '.join(argv)}` did not exit within {HELP_TIMEOUT_S} s.") from None
    return (done.stdout + done.stderr).decode("utf-8", "replace")


def require_options(binary: Path, help_argv: list[str], options: tuple[str, ...]) -> None:
    """Fail when the help text of `binary` does not name every option the adapter passes."""
    text = output_of(help_argv)
    # A whole token: `-o` must not match inside `--output`.
    missing = [option for option in options if not re.search(rf"(?<![\w-]){re.escape(option)}(?![\w-])", text)]
    if missing:
        raise MissingOption(
            f"{binary} does not support {', '.join(missing)} (not in `{' '.join(help_argv[1:])}` output). "
            "The adapter passes these options; lock a decoder revision that has them."
        )
