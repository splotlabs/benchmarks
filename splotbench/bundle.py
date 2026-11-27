# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Deterministic run bundles: write, check and read.

A bundle is a gzip-compressed USTAR archive of a run directory plus
`SHA256SUMS`. Entries are regular files only, sorted by path, with mtime 0,
uid/gid 0, empty owner names and mode 0644. The gzip header has no name and
mtime 0. The bundle digest is the sha256 of the archive file.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import os
import posixpath
import stat
import tarfile
from pathlib import Path

from . import schema
from .util import BenchError, sha256_bytes, write_atomic

CHECKSUMS = "SHA256SUMS"
REQUIRED = ("run.json", "lock.json", "host.json", "builds.json", "attempts.jsonl", "correctness.jsonl")
# shortcut: a fixed cap against decompression bombs; raise it when real runs approach it.
MAX_BYTES = 1 << 30


def checksums(members: dict[str, bytes]) -> bytes:
    """Return `SHA256SUMS` text (sha256sum format) for `members`, sorted by path."""
    return "".join(f"{hashlib.sha256(data).hexdigest()}  {path}\n" for path, data in sorted(members.items())).encode()


def parse_checksums(data: bytes) -> tuple[dict[str, str], list[str]]:
    sums, problems = {}, []
    for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
        digest, separator, path = line.partition("  ")
        if not separator or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            problems.append(f"{CHECKSUMS} line {number}: expected '<64 hex>  <path>'")
        elif path in sums:
            problems.append(f"{CHECKSUMS} line {number}: {path} is listed twice")
        else:
            sums[path] = digest
    return sums, problems


def _path_problem(path: str) -> str | None:
    if not path or path.startswith("/") or "\\" in path:
        return f"{path!r}: not a relative path"
    if ".." in path.split("/"):
        return f"{path!r}: contains '..'"
    if posixpath.normpath(path) != path:
        return f"{path!r}: not a normalised path"
    return None


def document_problems(name: str, data: bytes) -> list[str]:
    """Validate a .json file (an object or an array of objects) or a .jsonl file by each `schema` field."""
    documents = []
    if name.endswith(".jsonl"):
        for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            try:
                documents.append((f"line {number}: ", schema.loads(line)))
            except ValueError as error:
                return [f"line {number}: invalid JSON: {error}"]
    elif name.endswith(".json"):
        try:
            loaded = schema.loads(data)
        except ValueError as error:
            return [f"invalid JSON: {error}"]
        if isinstance(loaded, list):
            documents = [(f"[{index}] ", item) for index, item in enumerate(loaded)]
        else:
            documents = [("", loaded)]
    return [f"{where}{problem}" for where, document in documents for problem in schema.validate(document)]


def member_problems(members: dict[str, bytes]) -> list[str]:
    """Check the content rules of a bundle: checksums, required files and every document's schema."""
    if CHECKSUMS not in members:
        return [f"missing {CHECKSUMS}"]
    sums, problems = parse_checksums(members[CHECKSUMS])
    for path, data in sorted(members.items()):
        if path == CHECKSUMS:
            continue
        if path not in sums:
            problems.append(f"{path}: not listed in {CHECKSUMS}")
        elif hashlib.sha256(data).hexdigest() != sums[path]:
            problems.append(f"{path}: sha256 does not match {CHECKSUMS}")
        problems += [f"{path}: {problem}" for problem in document_problems(path, data)]
    problems += [f"{path}: listed in {CHECKSUMS} but not in the bundle" for path in sorted(sums.keys() - members.keys())]
    problems += [f"missing {path}" for path in REQUIRED if path not in members]
    return problems


def read_members(archive: Path) -> tuple[dict[str, bytes], list[str]]:
    """Read the regular-file entries of a bundle. Unsafe entries are problems and are not read."""
    members, problems, total = {}, [], 0
    try:
        with tarfile.open(archive, mode="r:gz") as tar:
            for entry in tar:
                problem = _path_problem(entry.name)
                if problem:
                    problems.append(problem)
                elif not entry.isreg():
                    problems.append(f"{entry.name}: not a regular file (links, directories and devices are not allowed)")
                elif entry.name in members:
                    problems.append(f"{entry.name}: appears twice")
                elif (total := total + entry.size) > MAX_BYTES:
                    problems.append(f"content is larger than {MAX_BYTES} bytes")
                    break
                else:
                    members[entry.name] = tar.extractfile(entry).read()
    except (OSError, EOFError, tarfile.TarError, gzip.BadGzipFile) as error:
        problems.append(f"not a readable tar.gz bundle: {error}")
    return members, problems


def check_bundle(archive: Path) -> list[str]:
    """Return one message per problem of the bundle at `archive`."""
    members, problems = read_members(archive)
    return problems + (member_problems(members) if not problems else [])


def read_bundle(archive: Path) -> dict[str, bytes]:
    """Return the members of a valid bundle, or raise BenchError with every problem."""
    members, problems = read_members(archive)
    problems = problems or member_problems(members)
    if problems:
        raise BenchError(f"invalid bundle {archive}:\n  " + "\n  ".join(problems))
    return members


def archive_bytes(members: dict[str, bytes]) -> bytes:
    """Return the deterministic tar.gz bytes of `members` (path -> content)."""
    tar_data = io.BytesIO()
    with tarfile.open(fileobj=tar_data, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for path, data in sorted(members.items()):
            entry = tarfile.TarInfo(path)
            entry.size, entry.mtime, entry.mode = len(data), 0, 0o644
            entry.uid = entry.gid = 0
            entry.uname = entry.gname = ""
            tar.addfile(entry, io.BytesIO(data))
    compressed = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=compressed, compresslevel=9, mtime=0) as stream:
        stream.write(tar_data.getvalue())
    return compressed.getvalue()


def write_bundle(run_dir: Path, output: Path) -> str:
    """Bundle the run directory into `output` and return the bundle digest."""
    run_dir = Path(run_dir)
    members = {}
    for root, dirs, files in os.walk(run_dir):
        for name in sorted(dirs + files):
            full = Path(root, name)
            relative = full.relative_to(run_dir).as_posix()
            mode = os.lstat(full).st_mode
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode):
                raise BenchError(f"{full} is not a regular file. A run directory holds regular files only.")
            members[relative] = full.read_bytes()
    if CHECKSUMS in members:
        raise BenchError(f"{run_dir} already contains {CHECKSUMS}. Bundle the run directory, not an extracted bundle.")
    members[CHECKSUMS] = checksums(members)
    problems = [problem for path in members if (problem := _path_problem(path))] + member_problems(members)
    if problems:
        raise BenchError(f"cannot bundle {run_dir}:\n  " + "\n  ".join(problems))
    data = archive_bytes(members)
    write_atomic(output, data)
    return sha256_bytes(data)
