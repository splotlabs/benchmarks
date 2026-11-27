# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""`bench validate`: check JSON and JSONL files, bundles and dataset directories."""

from __future__ import annotations

import argparse
import hashlib
import os
import stat
import sys
from pathlib import Path

from . import bundle, plan, schema
from .util import EXIT_FAILURE, EXIT_PREREQUISITE, BenchError, run

MAX_PUBLISHED_FILE_BYTES = 50 * 1024 * 1024  # a file in Git; the hosting limit is 100 MB
DATASET_DOCUMENTS = {
    "manifest.json": ("object", "splotbench.dataset/1"),
    "summary.json": ("object", "splotbench.summary/1"),
    "lock.json": ("object", "splotbench.lock/1"),
}


def _directory_files(directory: Path) -> tuple[dict[str, bytes], list[str]]:
    files, problems = {}, []
    for root, dirs, names in os.walk(directory):
        for name in sorted(dirs + names):
            full = Path(root, name)
            mode = os.lstat(full).st_mode
            if stat.S_ISDIR(mode):
                continue
            relative = full.relative_to(directory).as_posix()
            if stat.S_ISREG(mode):
                files[relative] = full.read_bytes()
            else:
                problems.append(f"{relative}: not a regular file")
    return files, problems


def dataset_problems(directory: Path) -> list[str]:
    """Check a dataset directory: checksums, documents, file list, bundles and shared ids."""
    files, problems = _directory_files(directory)
    if bundle.CHECKSUMS not in files:
        return problems + [f"missing {bundle.CHECKSUMS}"]
    sums, sum_problems = bundle.parse_checksums(files[bundle.CHECKSUMS])
    problems += sum_problems
    for path, data in sorted(files.items()):
        if path == bundle.CHECKSUMS:
            continue
        if path not in sums:
            problems.append(f"{path}: not listed in {bundle.CHECKSUMS}")
        elif hashlib.sha256(data).hexdigest() != sums[path]:
            problems.append(f"{path}: sha256 does not match {bundle.CHECKSUMS}")
    problems += [f"{path}: listed in {bundle.CHECKSUMS} but missing" for path in sorted(sums.keys() - files.keys())]

    documents = {}
    for name, expected in DATASET_DOCUMENTS.items():
        if name not in files:
            problems.append(f"missing {name}")
            continue
        found = bundle.document_problems(name, files[name], expected)
        problems += [f"{name}: {problem}" for problem in found]
        if not found:
            documents[name] = schema.loads(files[name])
    if problems:
        return problems

    manifest, summary, lock = documents["manifest.json"], documents["summary.json"], documents["lock.json"]
    for field in ("dataset_id", "campaign_id", "lock_digest", "origin", "complete"):
        if summary[field] != manifest[field]:
            problems.append(f"summary.json: {field} {summary[field]!r} differs from manifest.json {manifest[field]!r}")
    if lock["lock_digest"] != manifest["lock_digest"]:
        problems.append(f"lock.json: lock_digest differs from manifest.json {manifest['lock_digest']}")

    listed = {entry["path"]: entry for entry in manifest["files"]}
    expected = set(files) - {"manifest.json", bundle.CHECKSUMS}
    problems += [f"{path}: not listed in manifest.json files" for path in sorted(expected - listed.keys())]
    problems += [f"{path}: listed in manifest.json files but missing" for path in sorted(listed.keys() - expected)]
    for path in sorted(listed.keys() & expected):
        data = files[path]
        if "sha256:" + hashlib.sha256(data).hexdigest() != listed[path]["sha256"] or len(data) != listed[path]["bytes"]:
            problems.append(f"{path}: sha256 or size differs from manifest.json files")

    for run in manifest["runs"] + manifest.get("excluded_runs", []):
        path = run["bundle"]["path"]
        if listed.get(path) != run["bundle"]:
            problems.append(f"manifest.json: bundle of run {run['run_id']} is not the listed file {path}")
        elif path in files:
            problems += [f"{path}: {problem}" for problem in bundle.check_bundle(directory / path)]
    return problems


def published_problems(directory: Path) -> list[str]:
    """Check a published directory: index.json, its dataset directories and the size limit."""
    from . import assemble  # assemble imports this module

    if directory.is_symlink():
        return ["the published directory is a symbolic link; it must be a real directory"]
    problems = []
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.stat().st_size > MAX_PUBLISHED_FILE_BYTES:
            problems.append(f"{path.relative_to(directory).as_posix()}: above {MAX_PUBLISHED_FILE_BYTES} bytes (50 MiB)")
    problems += [
        f"{path.name}: unexpected entry. Only index.json, README.md and datasets/ belong here."
        for path in sorted(directory.iterdir())
        if path.name not in ("index.json", "README.md", "datasets")
    ]
    index_path = directory / "index.json"
    if index_path.is_symlink() or not index_path.is_file():
        return problems + ["index.json: missing or not a regular file"]
    found = bundle.document_problems("index.json", index_path.read_bytes(), ("object", "splotbench.index/1"))
    if found:
        return problems + [f"index.json: {problem}" for problem in found]
    index = schema.loads(index_path.read_bytes())
    listed = {}
    for entry in index["datasets"]:
        if entry["dataset_id"] in listed:
            problems.append(f"index.json: {entry['dataset_id']} is listed twice")
        listed[entry["dataset_id"]] = entry
    datasets = directory / "datasets"
    if datasets.is_symlink() or (datasets.exists() and not datasets.is_dir()):
        return problems + ["datasets: must be a directory, not a link or a file"]
    present = sorted(p.name for p in datasets.iterdir()) if datasets.is_dir() else []
    problems += [f"datasets/{name}: not listed in index.json" for name in present if name not in listed]
    for dataset_id, entry in listed.items():
        where = f"datasets/{dataset_id}"
        if (datasets / dataset_id).is_symlink():
            problems.append(f"{where}: a symbolic link is not a dataset directory")
            continue
        if not (datasets / dataset_id).is_dir():
            problems.append(f"{where}: listed in index.json but missing")
            continue
        found = dataset_problems(datasets / dataset_id)
        problems += [f"{where}: {problem}" for problem in found]
        if found:
            continue
        manifest = schema.loads((datasets / dataset_id / "manifest.json").read_bytes())
        summary = schema.loads((datasets / dataset_id / "summary.json").read_bytes())
        if manifest["dataset_id"] != dataset_id:
            problems.append(f"{where}: the manifest has dataset_id {manifest['dataset_id']}")
        if manifest["origin"] == "synthetic":
            problems.append(f"{where}: synthetic test data is never published")
        if {k: v for k, v in entry.items() if k != "published_utc"} != assemble.index_entry(summary):
            problems.append(f"index.json: the entry of {dataset_id} differs from its summary.json")
    return problems


def is_published_directory(path: Path) -> bool:
    return path.is_dir() and ((path / "index.json").exists() or (path / "datasets").exists())


def _git(directory: Path, *args: str) -> bytes:
    done = run(["git", "-c", "core.quotePath=false", *args], cwd=directory)
    if done.returncode:
        raise BenchError(f"git {args[0]} failed in {directory}: {done.stderr.decode(errors='replace').strip()}",
                         EXIT_PREREQUISITE)  # fmt: skip
    return done.stdout


def immutable_problems(directory: Path, ref: str) -> list[str]:
    """List changes since `ref` that touch an existing dataset directory of a published directory.

    Only added files in new dataset directories are allowed. `git ... -z` keeps every file name exact.
    """
    if ref.startswith("-"):
        raise BenchError(f"{ref!r} is not a Git ref. Give a branch, tag or commit.", 2)
    if directory.is_symlink():
        raise BenchError(f"{directory} is a symbolic link. The published directory must be a real directory.")
    top = Path(_git(directory, "rev-parse", "--show-toplevel").decode().strip())
    inside = _git(directory, "rev-parse", "--show-prefix").decode().strip()  # the physical path below the top
    parts = [part for part in inside.split("/") if part]
    # Only links between the repository top and the directory matter; a link above the top is harmless.
    lexical = Path(os.path.abspath(directory))
    levels = [lexical, *lexical.parents][: len(parts)]
    if [level.name for level in reversed(levels)] != parts or any(level.is_symlink() for level in levels):
        raise BenchError(f"{directory} or a parent inside the repository is a symbolic link. "
                         "The published directory must be reached without links.")  # fmt: skip
    prefix = inside + "datasets/"
    existing = {
        name[len(prefix):].split("/")[0]
        for name in _git(top, "ls-tree", "-r", "-z", "--name-only", "--full-name", ref, "--", prefix).decode().split("\0")
        if name.startswith(prefix)
    }
    fields = _git(top, "diff", "--name-status", "-z", "--no-renames", f"{ref}...HEAD", "--", prefix).decode().split("\0")
    problems = []
    for status, name in zip(fields[0::2], fields[1::2]):
        dataset_id = name[len(prefix):].split("/")[0]
        if status != "A":
            problems.append(f"{name}: {'deleted' if status == 'D' else 'changed'} since {ref}. Published files never change.")
        elif dataset_id in existing:
            problems.append(f"{name}: added to the existing dataset directory {dataset_id}. Published datasets never change.")
    return problems


def problems_for(path: Path) -> list[str]:
    if path.is_dir():
        if is_published_directory(path):
            return published_problems(path)
        return dataset_problems(path)
    if path.name.endswith(".tar.gz"):
        return bundle.check_bundle(path)
    if not path.name.endswith((".json", ".jsonl")):
        return ["unknown file type. Give a .json, .jsonl or .tar.gz file or a dataset directory."]
    try:
        data = path.read_bytes()
    except OSError as error:
        return [f"cannot read the file: {error.strerror}"]
    return bundle.document_problems(path.name, data)


def _validate(args: argparse.Namespace) -> int:
    failed = False
    for path in args.paths:
        problems = problems_for(path)
        if args.immutable_since:
            if is_published_directory(path):
                problems += immutable_problems(path, args.immutable_since)
            else:
                problems.append("--immutable-since needs a published directory")
        for problem in problems:
            print(f"bench validate: {path}: {problem}", file=sys.stderr)
        if problems:
            failed = True
        else:
            print(f"valid: {path}{_lock_note(path)}")
    return EXIT_FAILURE if failed else 0


def _lock_note(path: Path) -> str:
    """For a valid lock file: its digest and campaign id, so nobody has to read them from the file."""
    if path.is_dir() or not path.name.endswith(".json"):
        return ""
    document = schema.loads(path.read_bytes())
    if not isinstance(document, dict) or document.get("schema") != "splotbench.lock/1":
        return ""
    return f" (lock {document['lock_digest']}, campaign {plan.campaign_id(document)})"


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "validate",
        help="validate data files, bundles and dataset directories",
        description="Validate each PATH by its schema field: a .json file (lock, manifest, summary, index, "
        "or an array of manifests), a .jsonl row file, a .tar.gz bundle, a dataset directory or a published directory (index.json and datasets/). "
        "Exit 1 when any PATH is invalid; each problem is one line on stderr.",
    )
    parser.add_argument("paths", nargs="+", type=Path, metavar="PATH")
    parser.add_argument("--immutable-since", metavar="REF", help="for a published directory: also fail when a change since "
                        "Git ref REF touches an existing dataset directory (CI runs it with the base branch)")  # fmt: skip
    parser.set_defaults(handler=_validate)
