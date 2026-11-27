# SPDX-License-Identifier: MIT
"""Shared helpers for the tests. All repositories and files they make are synthetic."""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent

# Each test process, and each `bench` child it starts, gets its own temporary and runtime directory.
# So the host lock of the tests never meets a real run or a parallel CI job on the same runner.
TEMPORARY = tempfile.mkdtemp(prefix="splotbench-tests-")
os.environ["TMPDIR"] = os.environ["XDG_RUNTIME_DIR"] = tempfile.tempdir = TEMPORARY
atexit.register(shutil.rmtree, TEMPORARY, True)

# Keep the developer's global and system Git configuration out of the tests.
_isolated_git = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})


def isolate_git() -> None:
    _isolated_git.start()


def restore_git() -> None:
    _isolated_git.stop()


def git(directory: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=Synthetic Test", "-c", "user.email=synthetic@example.invalid", *args],
        cwd=directory,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def make_repo(directory: Path, files: dict[str, bytes]) -> Path:
    """Create a Git repository with `files` in one commit."""
    directory.mkdir(parents=True, exist_ok=True)
    git(directory, "init", "-q")
    for path, data in files.items():
        (directory / path).parent.mkdir(parents=True, exist_ok=True)
        (directory / path).write_bytes(data)
    git(directory, "add", "-A")
    git(directory, "commit", "-q", "-m", "synthetic commit")
    return directory


def harness_copy(directory: Path, *, with_git: bool) -> Path:
    """Copy the harness into `directory`, as a fresh Git repository when `with_git` is true."""
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "bench", directory / "bench")
    shutil.copy2(ROOT / ".gitignore", directory / ".gitignore")
    shutil.copytree(ROOT / "splotbench", directory / "splotbench", ignore=shutil.ignore_patterns("__pycache__"))
    if with_git:
        git(directory, "init", "-q")
        git(directory, "add", "-A")
        git(directory, "commit", "-q", "-m", "synthetic harness copy")
    return directory


def bench(harness: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(harness / "bench"), *args], capture_output=True, text=True, env=env, check=False
    )
