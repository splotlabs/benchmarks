# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Source snapshots of the harness and of decoder checkouts, and `bench identity`."""

from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path

from . import __version__
from .util import BenchError, canonical_json, run, sha256_bytes, sha256_file

HARNESS_ROOT = Path(__file__).resolve().parent.parent
GITLINK_MODE = "160000"


def _git(directory: Path, *args: str) -> bytes:
    done = run(["git", "-C", str(directory), *args])
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip()
        raise BenchError(f"`git {' '.join(args)}` failed in {directory}: {detail}. Make sure it is a readable Git checkout.")
    return done.stdout


def _split(output: bytes) -> list[str]:
    return [os.fsdecode(entry) for entry in output.split(b"\0") if entry]


def head_commit(directory: Path) -> str | None:
    """Return the commit of HEAD, or None when the repository has no commit yet."""
    done = run(["git", "-C", str(directory), "rev-parse", "--verify", "--quiet", "HEAD^{commit}"])
    return done.stdout.decode().strip() if done.returncode == 0 else None


def _walk(directory: Path) -> list[str]:
    paths = []
    for root, dirs, files in os.walk(directory):
        # os.walk does not descend into a symbolic link to a directory; record the link itself.
        names = files + [name for name in dirs if os.path.islink(os.path.join(root, name))]
        paths += [Path(root, name).relative_to(directory).as_posix() for name in names]
    return paths


def _file_line(directory: Path, path: str) -> str | None:
    full = directory / path
    try:
        status = os.lstat(full)
    except FileNotFoundError:
        return None  # a deleted tracked file: its line goes away, so the digest changes
    if stat.S_ISLNK(status.st_mode):
        return f"120000 {sha256_bytes(os.fsencode(os.readlink(full)))} {path}\n"
    if stat.S_ISREG(status.st_mode):
        mode = "100755" if status.st_mode & stat.S_IXUSR else "100644"
        return f"{mode} {sha256_file(full)} {path}\n"
    # An untracked nested repository is listed as a directory. `dirty` already reports it.
    return None


def _digest(lines: list[str]) -> str:
    return sha256_bytes("".join(sorted(lines)).encode("utf-8", "surrogateescape"))


def snapshot(directory: Path) -> dict:
    """Describe the exact working-tree content of `directory`.

    `snapshot_sha256` covers every tracked and untracked, not ignored, file
    as `<mode> <sha256 of bytes> <path>` lines, so any edit changes it.
    Without `.git` it covers every file, and the snapshot is never clean.
    """
    directory = Path(directory)
    if not (directory / ".git").exists():
        lines = [line for path in _walk(directory) if (line := _file_line(directory, path))]
        # Without version control nothing can show that the content is clean.
        return {
            "vcs": "none",
            "commit": None,
            "tree": None,
            "dirty": True,
            "snapshot_sha256": _digest(lines),
            "submodules": [],
        }

    commit = head_commit(directory)
    tree = _git(directory, "rev-parse", "HEAD^{tree}").decode().strip() if commit else None
    dirty = bool(_git(directory, "status", "--porcelain"))
    gitlinks = {}
    for entry in _split(_git(directory, "ls-files", "--stage", "-z")):
        meta, _, path = entry.partition("\t")
        mode, object_id, _stage = meta.split()
        if mode == GITLINK_MODE:
            gitlinks[path] = object_id

    submodules = []
    lines = []
    for path, object_id in sorted(gitlinks.items()):
        initialized = (directory / path / ".git").exists()
        submodule = {"path": path, "commit": object_id, "snapshot": snapshot(directory / path) if initialized else None}
        submodules.append(submodule)
        lines.append(f"{GITLINK_MODE} {sha256_bytes(canonical_json(submodule))} {path}\n")
    for path in set(_split(_git(directory, "ls-files", "-co", "--exclude-standard", "-z"))) - gitlinks.keys():
        if line := _file_line(directory, path):
            lines.append(line)
    return {
        "vcs": "git",
        "commit": commit,
        "tree": tree,
        "dirty": dirty,
        "snapshot_sha256": _digest(lines),
        "submodules": submodules,
    }


def harness_commit() -> str | None:
    """Return the harness commit, or None when it is not known."""
    if not (HARNESS_ROOT / ".git").exists():
        return None
    try:
        return head_commit(HARNESS_ROOT)
    except BenchError:
        return None


def harness_identity() -> dict:
    return {"version": __version__, "snapshot": snapshot(HARNESS_ROOT)}


def _identity(_args: argparse.Namespace) -> int:
    print(json.dumps(harness_identity(), indent=2, sort_keys=True))
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "identity",
        help="print the harness identity as JSON",
        description="Print the harness version and a snapshot of its source: commit, tree, "
        "dirty flag and a digest of the working-tree content.",
    )
    parser.set_defaults(handler=_identity)
