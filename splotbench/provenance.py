# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Provenance: source snapshots, release resolution, identity classes, binary identity, `bench identity`."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import tempfile
from pathlib import Path

from . import __version__
from .util import EXIT_PREREQUISITE, BenchError, canonical_json, run, sha256_bytes, sha256_file

HARNESS_ROOT = Path(__file__).resolve().parent.parent
HARNESS_REPOSITORY = "https://github.com/splotlabs/benchmarks"
GITLINK_MODE = "160000"
FULL_COMMIT = re.compile(r"^[0-9a-f]{40}$")


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
    dirty = bool(_git(directory, "--no-optional-locks", "status", "--porcelain"))  # never rewrites the index
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


def identity_class(source: dict, locked_commit: str | None, *, synthetic: bool = False) -> str:
    """Return `verified` only for a clean Git checkout at exactly the locked commit."""
    if synthetic:
        return "synthetic"
    if clean_at(source, locked_commit):
        return "verified"
    return "development"


def harness_identity(locked_commit: str | None, directory: Path = HARNESS_ROOT) -> dict:
    """Return the harness identity record (schema `common#/$defs/harness`) against a locked commit."""
    source = snapshot(directory)
    return {
        "repository": HARNESS_REPOSITORY,
        "commit": source["commit"],
        "snapshot_sha256": source["snapshot_sha256"],
        "class": identity_class(source, locked_commit),
    }


# --- release resolution (lock time only) --------------------------------------


def _remote_refs(repository: str) -> tuple[dict[str, str], dict[str, str]]:
    """Return (branches, tags) of the upstream. A tag maps to its peeled commit."""
    if not re.match(r"^(https|file)://", repository):
        raise BenchError(f"repository {repository!r} is not an https:// or file:// URL. Use the canonical upstream URL.")
    done = run(["git", "ls-remote", repository], extra_env={"GIT_TERMINAL_PROMPT": "0"})
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip()
        raise BenchError(
            f"cannot list the refs of {repository}: {detail}. Check the URL and the network.", EXIT_PREREQUISITE
        )
    branches, direct, peeled = {}, {}, {}
    for line in done.stdout.decode("utf-8", "replace").splitlines():
        commit, _, ref = line.partition("\t")
        if ref.startswith("refs/heads/"):
            branches[ref.removeprefix("refs/heads/")] = commit
        elif ref.startswith("refs/tags/") and ref.endswith("^{}"):
            peeled[ref.removeprefix("refs/tags/").removesuffix("^{}")] = commit
        elif ref.startswith("refs/tags/"):
            direct[ref.removeprefix("refs/tags/")] = commit
    return branches, {**direct, **peeled}


def _verify_commit(repository: str, select: str, object_id: str) -> None:
    """Fetch the resolved object into a temporary bare repository and check that it is a commit."""
    no_prompt = {"GIT_TERMINAL_PROMPT": "0"}
    with tempfile.TemporaryDirectory(prefix="splotbench-resolve-") as temporary:
        _git(Path(temporary), "init", "-q", "--bare")
        fetch = ["git", "-C", temporary, "fetch", "-q", "--depth", "1"]
        done = run([*fetch, "--filter=tree:0", repository, object_id], extra_env=no_prompt)
        if done.returncode != 0:  # the server may refuse filters; a plain shallow fetch still proves the object
            done = run([*fetch, repository, object_id], extra_env=no_prompt)
        if done.returncode != 0:
            detail = done.stderr.decode("utf-8", "replace").strip()
            raise BenchError(
                f"select {select!r}: {repository} does not serve {object_id}: {detail}. "
                "Use a commit, tag or branch that exists upstream."
            )
        kind = _git(Path(temporary), "cat-file", "-t", object_id).decode().strip()
    if kind != "commit":
        raise BenchError(f"select {select!r} resolves to a {kind} ({object_id}), not a commit. Select a commit.")


def resolve(
    repository: str, select: str, release_tag_pattern: str | None, release_url_template: str | None
) -> dict:
    """Resolve `select` against the upstream with `git ls-remote`.

    `select` is a full 40-hex commit, a tag or a branch (`refs/tags/X` or
    `refs/heads/X` when a name is both). `release` is set only when an upstream
    tag that fully matches `release_tag_pattern` peels to exactly the commit.
    The group `version` of the pattern gives the version; without it the tag
    is the version. `{tag}` and `{version}` in `release_url_template` are
    replaced. A local tag, the nearest tag, a branch name or `--version`
    output is never release proof.
    """
    branches, tags = _remote_refs(repository)
    selected_tag = None
    if FULL_COMMIT.fullmatch(select):
        commit = select
    else:
        found = []
        if not select.startswith("refs/heads/") and select.removeprefix("refs/tags/") in tags:
            selected_tag = select.removeprefix("refs/tags/")
            found.append(tags[selected_tag])
        if not select.startswith("refs/tags/") and select.removeprefix("refs/heads/") in branches:
            found.append(branches[select.removeprefix("refs/heads/")])
        if not found:
            raise BenchError(
                f"select {select!r} is not a full commit, tag or branch of {repository}. "
                "Use a 40-hex commit or an upstream tag or branch."
            )
        if len(found) > 1:
            raise BenchError(
                f"select {select!r} is both a tag and a branch of {repository}. "
                f"Use refs/tags/{select} or refs/heads/{select}."
            )
        commit = found[0]
    _verify_commit(repository, select, commit)

    release = None
    if release_tag_pattern:
        try:
            pattern = re.compile(release_tag_pattern)
        except re.error as error:
            raise BenchError(f"release_tag_pattern {release_tag_pattern!r} is not a valid regular expression: {error}.")
        matching = sorted(tag for tag, target in tags.items() if target == commit and pattern.fullmatch(tag))
        if matching:
            tag = selected_tag if selected_tag in matching else matching[0]
            version = pattern.fullmatch(tag).groupdict().get("version") or tag
            url = None
            if release_url_template:
                url = release_url_template.replace("{tag}", tag).replace("{version}", version)
            release = {"tag": tag, "version": version, "url": url}
    return {"commit": commit, "identity": "release" if release else "development", "release": release}


def clean_at(source: dict, commit: str | None) -> bool:
    """True for a Git snapshot without local changes at exactly `commit`."""
    return source["vcs"] == "git" and not source["dirty"] and source["commit"] == commit


def build_origin(release: dict | None, source: dict, commit: str) -> str:
    """Only clean source at a verified release commit is `source-release`."""
    return "source-release" if release is not None and clean_at(source, commit) else "source-development"


# --- binary identity ----------------------------------------------------------


def _work_path(work: Path, path: Path) -> str:
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(Path(work).resolve()).as_posix()
    except ValueError:
        raise BenchError(f"{path} is outside the work directory {work}. Build inside the work directory.") from None


def _file_sha256(path: Path) -> str:
    try:
        return sha256_file(path)
    except OSError as error:
        raise BenchError(f"cannot read {path}: {error.strerror}. Run `bench prepare` to build it again.") from None


def binary_identity(work: Path, binary: Path, libraries: list[Path] = ()) -> dict:
    """Hash the executable that is invoked and the project-built decoder libraries."""
    return {
        "binary_path": _work_path(work, binary),
        "binary_sha256": _file_sha256(binary),
        "libraries": sorted(
            ({"path": _work_path(work, library), "sha256": _file_sha256(library)} for library in libraries),
            key=lambda entry: entry["path"],
        ),
    }


def build_id(manifest: dict) -> str:
    """Derive `bld-<16 hex>` from a build manifest. Another binary gives another id."""
    identity = {
        "implementation": manifest["implementation"],
        "commit": manifest["source"]["commit"],
        "snapshot_sha256": manifest["source"]["snapshot"]["snapshot_sha256"],
        "configuration": manifest["configuration"],
        "binary_sha256": manifest["binary_sha256"],
        "libraries": sorted(library["sha256"] for library in manifest["libraries"]),
    }
    return "bld-" + sha256_bytes(canonical_json(identity)).removeprefix("sha256:")[:16]


def check_binary(work: Path, manifest: dict) -> None:
    """Fail when the executable or a library differs from the build manifest."""
    work = Path(work)
    current = binary_identity(
        work, work / manifest["binary_path"], [work / library["path"] for library in manifest["libraries"]]
    )
    recorded = {key: manifest[key] for key in ("binary_path", "binary_sha256", "libraries")}
    if current != recorded:
        raise BenchError(
            f"the binary of build {manifest['build_id']} changed since its manifest: recorded "
            f"{recorded['binary_sha256']}, found {current['binary_sha256']} for {manifest['binary_path']} "
            "(or a library differs). Remove its build directory and run `bench prepare` again to rebuild."
        )


def _identity(_args: argparse.Namespace) -> int:
    print(json.dumps({"version": __version__, "snapshot": snapshot(HARNESS_ROOT)}, indent=2, sort_keys=True))
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "identity",
        help="print the harness identity as JSON",
        description="Print the harness version and a snapshot of its source: commit, tree, "
        "dirty flag and a digest of the working-tree content.",
    )
    parser.set_defaults(handler=_identity)
