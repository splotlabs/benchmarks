# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""`bench publish`: copy one assembled dataset into a `published/` directory of this repository.

The command changes files inside the target directory only. It never runs Git: the owner
reviews the diff and opens a pull request. Nothing here runs unless the owner starts it.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import filecmp
import hashlib
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import assemble, bundle, schema, validate
from .util import BenchError, json_bytes, write_atomic

MAX_FILE_BYTES = validate.MAX_PUBLISHED_FILE_BYTES
DEFAULT_TARGET = Path(__file__).resolve().parent.parent / "published"


def index_change(index: dict | None, entry: dict, promote: bool) -> tuple[dict | None, str]:
    """Return (new index or None when nothing changes, description). Refuse an entry that differs."""
    index = index or {"schema": "splotbench.index/1", "latest_complete": None, "datasets": []}
    found = [e for e in index["datasets"] if e["dataset_id"] == entry["dataset_id"]]
    new = {**index, "datasets": list(index["datasets"])}
    if found:
        if {k: v for k, v in found[0].items() if k != "published_utc"} != {k: v for k, v in entry.items() if k != "published_utc"}:
            raise BenchError(f"index.json already lists {entry['dataset_id']} with different content. Datasets are immutable; "
                             "assemble again to get another dataset id.")  # fmt: skip
        action = "entry unchanged"
    else:
        new["datasets"].append({**entry, "published_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
        action = "entry added"
    if promote:
        new["latest_complete"] = entry["dataset_id"]
    line = f"{action}; latest_complete {index['latest_complete']} -> {new['latest_complete']}"
    return (None if new == index else new), line


def _plan(directory: Path) -> tuple[dict, list[dict]]:
    """Check the dataset locally and return (manifest, files)."""
    problems = validate.dataset_problems(directory)
    if problems:
        raise BenchError(f"{directory} is not a valid dataset ({len(problems)} problems):\n  " + "\n  ".join(problems))
    manifest = schema.loads((directory / "manifest.json").read_bytes())
    files = []
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        data = path.read_bytes()
        relative = path.relative_to(directory).as_posix()
        if len(data) > MAX_FILE_BYTES:
            raise BenchError(
                f"{relative} has {len(data)} bytes; the limit for a file in Git is {MAX_FILE_BYTES} (50 MiB). "
                "Remove the run that makes this bundle from the dataset, or ask the owner."
            )
        files.append({"path": relative, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    return manifest, files


def _refuse(manifest: dict, allow_local: bool, promote: bool, target: Path) -> None:
    origin, name = manifest["origin"], manifest["dataset_id"]
    if origin == "synthetic":
        raise BenchError(f"{name} is synthetic test data. It is never published.")
    if origin != "official":
        if not allow_local:
            raise BenchError(f"{name} has origin {origin}. Only official datasets are published; "
                             "use --allow-local with --to for a personal folder.")  # fmt: skip
        if target.resolve().is_relative_to(DEFAULT_TARGET.resolve()):
            raise BenchError(f"{name} has origin {origin}. A local dataset never goes into the published/ directory "
                             "of the harness checkout; give another folder with --to.")  # fmt: skip
    if not manifest["complete"]:
        raise BenchError(f"{name} is incomplete (required work is missing). Only complete datasets are published.")
    if promote and origin != "official":
        raise BenchError("--promote needs an official dataset: latest_complete names the latest complete official dataset.")


def _read_index(target: Path) -> dict | None:
    path = target / "index.json"
    if not path.exists():
        return None
    problems = bundle.document_problems("index.json", path.read_bytes(), ("object", "splotbench.index/1"))
    if problems:
        raise BenchError(f"{path} is invalid; fix it by hand before publishing:\n  " + "\n  ".join(problems))
    return schema.loads(path.read_bytes())


def _names(directory: Path) -> set[str]:
    return {p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_file()}


@contextlib.contextmanager
def _io(action: str, path: Path, state: str):
    try:
        yield
    except OSError as error:
        raise BenchError(f"cannot {action} {error.filename or path}: {error.strerror}. {state}") from None


def _require_same(source: Path, final: Path) -> None:
    """Return when `final` holds the same bytes as `source`. A difference raises and names the first file."""
    for path in sorted(_names(final) | _names(source)):
        if not (final / path).is_file() or not (source / path).is_file() or not filecmp.cmp(final / path, source / path, shallow=False):
            raise BenchError(f"{final} exists with different bytes: {path} differs. Datasets never change. "
                             "Assemble again to get a new dataset id; do not edit the published directory.")  # fmt: skip


def _install(source: Path, target: Path, dataset_id: str) -> bool:
    """Copy the dataset to <target>/datasets/<id>/ through a temporary directory and one rename.

    Return False when the directory is already there with the same bytes.
    """
    final = target / "datasets" / dataset_id
    if final.exists():
        _require_same(source, final)
        return False
    temporary = final.with_name(f".{dataset_id}.tmp")
    shutil.rmtree(temporary, ignore_errors=True)
    try:
        with _io("copy the dataset to", temporary, "Nothing was published."):
            for path in sorted(p for p in source.rglob("*") if p.is_file()):
                copy = temporary / path.relative_to(source)
                copy.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, copy)
        problems = validate.dataset_problems(temporary)
        if problems:
            raise BenchError("the copy does not match SHA256SUMS; nothing was published:\n  " + "\n  ".join(problems))
        with _io("move the copy to", final, "Nothing was published."):
            os.rename(temporary, final)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    return True


def publish(directory: Path, target: Path, *, dry_run: bool = False, promote: bool = False,
            allow_local: bool = False, out=print) -> int:  # fmt: skip
    """Validate, then (unless `dry_run`) copy the dataset in `directory` into `target`. Return the exit code."""
    manifest, files = _plan(directory)
    _refuse(manifest, allow_local, promote, target)
    dataset_id = manifest["dataset_id"]
    entry = assemble.index_entry(schema.loads((directory / "summary.json").read_bytes()))
    index = _read_index(target)
    _, line = index_change(index, entry, promote)  # refuses an entry with the same id and other content
    final = target / "datasets" / dataset_id
    present = final.exists()
    if present:
        _require_same(directory, final)

    if dry_run:
        out(f"dry run: nothing is written. Dataset {dataset_id}: origin {manifest['origin']}, complete {manifest['complete']}")
        out(f"target: {final}  ({'already present, same bytes' if present else 'new directory'})")
        for item in files:
            out(f"  {item['path']}  {item['bytes']} bytes  sha256:{item['sha256']}")
        out(f"{target / 'index.json'}: {line}")
        return 0

    with _io("create", target, "Nothing was published."):
        (target / "datasets").mkdir(parents=True, exist_ok=True)
        descriptor = os.open(target, os.O_RDONLY)
    try:  # one publication at a time into this target
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise BenchError(f"another publication holds {target}. Wait for it, then run again.") from None
        installed = _install(directory, target, dataset_id)
        out(f"{'copied' if installed else 'already present'} {final}")
        new, line = index_change(_read_index(target), entry, promote)
        if new is not None:
            with _io("write", target / "index.json",
                     f"The dataset directory {final} is complete but not listed; run the same command again."):
                write_atomic(target / "index.json", json_bytes(new))
        out(f"{target / 'index.json'}: {line}" + ("" if new is not None else " (not written)"))
    finally:
        os.close(descriptor)
    if installed or new is not None:
        out("next: create a branch, then `git add published`, commit, and open a pull request.")
        out("review the diff first; a published dataset directory is never changed after the pull request merges.")
    return 0


def _publish(args: argparse.Namespace) -> int:
    return publish(args.dataset_dir, args.to, dry_run=args.dry_run, promote=args.promote, allow_local=args.allow_local)


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "publish",
        help="copy an assembled dataset into the published/ directory",
        description="Validate DATASET_DIR (OUT/datasets/<dataset_id>), copy it to <target>/datasets/<dataset_id>/ "
        "without overwriting, check the copy against SHA256SUMS, then add it to <target>/index.json. "
        "The command never runs Git; it prints the next steps. See docs/PUBLISHING.md.",
    )
    parser.add_argument("dataset_dir", type=Path, metavar="DATASET_DIR")
    parser.add_argument("--to", type=Path, default=DEFAULT_TARGET, metavar="DIR",
                        help="target directory (default: published/ of the harness checkout)")  # fmt: skip
    parser.add_argument("--dry-run", action="store_true", help="check and report; write nothing")
    parser.add_argument("--promote", action="store_true", help="make this dataset latest_complete (official datasets only)")
    parser.add_argument("--allow-local", action="store_true", help="allow origin local into a folder other than published/ (never synthetic)")
    parser.set_defaults(handler=_publish)
