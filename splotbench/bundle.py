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
import json
import os
import posixpath
import stat
import tarfile
from pathlib import Path

from . import schema
from .util import BenchError, sha256_bytes, write_atomic

CHECKSUMS = "SHA256SUMS"
# Every member of a bundle outside logs/ and the form and schema it must have.
MEMBERS = {
    "run.json": ("object", "splotbench.run/1"),
    "lock.json": ("object", "splotbench.lock/1"),
    "host.json": ("object", "splotbench.host/1"),
    "builds.json": ("array", "splotbench.build/1"),
    "attempts.jsonl": ("rows", "splotbench.attempt/1"),
    "correctness.jsonl": ("rows", "splotbench.correctness/1"),
}
FORMS = {"object": "a JSON object", "array": "a JSON array", "rows": "JSON lines"}
FREE_FORM = "logs/"
# shortcut: fixed caps against decompression bombs; raise them when real runs approach them.
MAX_BYTES = 1 << 30
MAX_ARCHIVE_BYTES = 256 << 20
BLOCK = tarfile.BLOCKSIZE
CHUNK = 1 << 20


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
        elif path == CHECKSUMS:
            problems.append(f"{CHECKSUMS} line {number}: {CHECKSUMS} cannot list itself")
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


def _load_documents(name: str, data: bytes) -> tuple[str, list[tuple[str, object]]] | str:
    """Return (form, [(location, document)]) by file suffix, or the parse problem."""
    if name.endswith(".jsonl"):
        documents = []
        for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            try:
                documents.append((f"line {number}: ", schema.loads(line)))
            except ValueError as error:
                return f"line {number}: invalid JSON: {error}"
        return "rows", documents
    try:
        loaded = schema.loads(data)
    except ValueError as error:
        return f"invalid JSON: {error}"
    if isinstance(loaded, list):
        return "array", [(f"[{index}] ", item) for index, item in enumerate(loaded)]
    return "object", [("", loaded)]


def _kind(schema_id: object) -> str | None:
    return schema_id.partition("/")[0] if isinstance(schema_id, str) else None


def document_problems(name: str, data: bytes, expected: tuple[str, str] | None = None) -> list[str]:
    """Validate a .json file (an object or an array of objects) or a .jsonl file by each `schema` field.

    `expected` is (form, schema id): the file must have that form, and every document that schema.
    """
    loaded = _load_documents(name, data)
    if isinstance(loaded, str):
        return [loaded]
    form, documents = loaded
    if expected and form != expected[0]:
        return [f"expected {FORMS[expected[0]]}, found {FORMS[form]}"]
    problems = []
    for where, document in documents:
        found = document.get("schema") if isinstance(document, dict) else None
        # Another major version of the expected kind gets the "unsupported version" message from validate().
        if expected and _kind(found) != _kind(expected[1]):
            problems.append(f"{where}$.schema: expected {expected[1]}, found {json.dumps(found)}")
        else:
            problems += [f"{where}{problem}" for problem in schema.validate(document)]
    return problems


def member_problems(members: dict[str, bytes]) -> list[str]:
    """Check the content rules of a bundle: checksums, the known members and their schemas."""
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
        if path in MEMBERS:
            problems += [f"{path}: {problem}" for problem in document_problems(path, data, MEMBERS[path])]
        elif not path.startswith(FREE_FORM):
            problems.append(f"{path}: unexpected member; a bundle holds {', '.join(MEMBERS)} and {FREE_FORM}")
    problems += [f"{path}: listed in {CHECKSUMS} but not in the bundle" for path in sorted(sums.keys() - members.keys())]
    problems += [f"missing {path}" for path in MEMBERS if path not in members]
    return problems


class _LimitExceeded(Exception):
    pass


def _read(stream, size: int, state: dict) -> bytes:
    """Read exactly `size` decompressed bytes, counting them against MAX_BYTES."""
    state["total"] += size
    if state["total"] > MAX_BYTES:
        raise _LimitExceeded
    data = stream.read(size)
    if len(data) != size:
        raise EOFError("the archive ends inside an entry")
    return data


def _skip(stream, size: int, state: dict) -> None:
    while size > 0:
        _read(stream, min(size, CHUNK), state)
        size -= min(size, CHUNK)


def read_members(archive: Path | bytes) -> tuple[dict[str, bytes], list[str]]:
    """Read the regular-file entries of a bundle. Every other header type is a problem.

    Headers are parsed one 512-byte block at a time, so a pax or GNU header is
    never expanded. Every byte read, of kept or refused entries, counts against
    MAX_BYTES.
    """
    members, problems, state = {}, [], {"total": 0}
    try:
        size = len(archive) if isinstance(archive, bytes) else os.stat(archive).st_size
        if size > MAX_ARCHIVE_BYTES:
            return {}, [f"the archive is larger than {MAX_ARCHIVE_BYTES} bytes"]
        with (io.BytesIO(archive) if isinstance(archive, bytes) else open(archive, "rb")) as file, \
                gzip.GzipFile(fileobj=file, mode="rb") as stream:
            while True:
                block = _read(stream, BLOCK, state)
                if block == bytes(BLOCK):
                    break
                entry = tarfile.TarInfo.frombuf(block, "utf-8", "strict")
                if entry.size < 0:  # a base-256 size field can be negative; read(-n) would read everything
                    raise tarfile.HeaderError(f"{entry.name}: negative size")
                padded = -(-entry.size // BLOCK) * BLOCK
                problem = _path_problem(entry.name)
                if entry.type != tarfile.REGTYPE and entry.type != tarfile.AREGTYPE:
                    problems.append(
                        f"{entry.name}: not a regular file (type {entry.type!r}); links, directories, "
                        "devices and pax or GNU headers are not allowed"
                    )
                    _skip(stream, padded, state)
                elif problem or entry.name in members:
                    problems.append(problem or f"{entry.name}: appears twice")
                    _skip(stream, padded, state)
                else:
                    members[entry.name] = _read(stream, padded, state)[: entry.size]
    except _LimitExceeded:
        problems.append(f"the content is larger than {MAX_BYTES} bytes")
    except (OSError, EOFError, UnicodeDecodeError, tarfile.TarError, gzip.BadGzipFile) as error:
        problems.append(f"not a readable tar.gz bundle: {error}")
    return members, problems


def check_bundle(archive: Path) -> list[str]:
    """Return one message per problem of the bundle at `archive`."""
    members, problems = read_members(archive)
    return problems + (member_problems(members) if not problems else [])


def read_bundle(archive: Path | bytes, name: Path | str | None = None) -> dict[str, bytes]:
    """Return the members of a valid bundle (a path, or its bytes with a `name` for messages), or raise BenchError."""
    members, problems = read_members(archive)
    problems = problems or member_problems(members)
    if problems:
        raise BenchError(f"invalid bundle {name or archive}:\n  " + "\n  ".join(problems))
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
            try:
                tar.addfile(entry, io.BytesIO(data))
            except ValueError as error:
                raise BenchError(f"cannot store {path!r} in the bundle: {error}. Use a shorter path.") from None
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
