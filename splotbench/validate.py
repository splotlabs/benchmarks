# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""`bench validate`: check JSON and JSONL files, bundles and dataset directories."""

from __future__ import annotations

import argparse
import hashlib
import os
import stat
import sys
from pathlib import Path

from . import bundle, schema
from .util import EXIT_FAILURE

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


def problems_for(path: Path) -> list[str]:
    if path.is_dir():
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
        for problem in problems:
            print(f"bench validate: {path}: {problem}", file=sys.stderr)
        if problems:
            failed = True
        else:
            print(f"valid: {path}")
    return EXIT_FAILURE if failed else 0


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "validate",
        help="validate data files, bundles and dataset directories",
        description="Validate each PATH by its schema field: a .json file (lock, manifest, summary, index, "
        "or an array of manifests), a .jsonl row file, a .tar.gz bundle or a dataset directory. "
        "Exit 1 when any PATH is invalid; each problem is one line on stderr.",
    )
    parser.add_argument("paths", nargs="+", type=Path, metavar="PATH")
    parser.set_defaults(handler=_validate)
