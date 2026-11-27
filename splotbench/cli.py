# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Command-line interface. Each command module adds its parser with `register(subparsers)`."""

from __future__ import annotations

import argparse
import sys

from . import __version__, assemble, provenance, validate
from .util import BenchError

COMMAND_MODULES = (provenance, validate, assemble)

EXIT_CODES = """exit codes:
  0    success
  1    the operation ran and the result is a failure
  2    usage error
  3    missing prerequisite or configuration
  130  interrupted"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bench",
        description="Reproducible benchmark harness for AV2 decoders.",
        epilog=EXIT_CODES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="store_true", help="print the version and the harness commit")
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    for module in COMMAND_MODULES:
        module.register(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        commit = provenance.harness_commit()
        print(f"bench {__version__}" + (f" (commit {commit})" if commit else ""))
        return 0
    if args.command is None:
        parser.error("choose a command; see `bench --help`")
    try:
        return args.handler(args)
    except BenchError as error:
        print(f"bench {args.command}: {error}", file=sys.stderr)
        return error.exit_code
    except KeyboardInterrupt:
        return 130
