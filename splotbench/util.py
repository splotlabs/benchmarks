# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Shared helpers: errors, canonical JSON, digests, atomic writes, subprocesses."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

EXIT_FAILURE = 1
EXIT_PREREQUISITE = 3

# The only environment variables that may enter a record.
ENV_ALLOWLIST = (
    "CC",
    "CXX",
    "CFLAGS",
    "CXXFLAGS",
    "LDFLAGS",
    "RUSTFLAGS",
    "CARGO_BUILD_TARGET",
    "MACOSX_DEPLOYMENT_TARGET",
)


class BenchError(Exception):
    """An expected failure. The CLI prints the message without a traceback."""

    def __init__(self, message: str, exit_code: int = EXIT_FAILURE) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _reject_floats(value: object) -> None:
    if isinstance(value, float):
        raise ValueError(f"canonical JSON does not allow floats: {value!r}; use an integer or a string")
    if isinstance(value, Mapping):
        items = value.values()
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        return
    for item in items:
        _reject_floats(item)


def canonical_json(value: object) -> bytes:
    """Return the canonical UTF-8 JSON bytes that digests are computed over."""
    _reject_floats(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    with open(path, "rb") as file:
        return "sha256:" + hashlib.file_digest(file, "sha256").hexdigest()


def write_atomic(path: Path, data: bytes) -> None:
    """Replace `path` with `data`. A reader sees the old file or the new file, never a part."""
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def recorded_env(environ: Mapping[str, str] = os.environ) -> dict[str, str]:
    """Return the allow-listed subset of `environ`. Records never hold the whole environment."""
    return {name: environ[name] for name in ENV_ALLOWLIST if name in environ}


def run(
    args: Sequence[str], *, cwd: Path | None = None, extra_env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[bytes]:
    """Run a tool and capture its output as bytes. A missing tool is a missing prerequisite."""
    if shutil.which(args[0]) is None:
        raise BenchError(f"{args[0]} is not on PATH. Install it, then run the command again.", EXIT_PREREQUISITE)
    env = {**os.environ, **extra_env} if extra_env else None
    return subprocess.run(list(args), cwd=cwd, env=env, capture_output=True, check=False)
