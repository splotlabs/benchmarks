# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Corpus manifest, suites, the verified stream cache and `bench prepare`."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import os
import tempfile
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path

from .provenance import HARNESS_ROOT
from .util import EXIT_PREREQUISITE, BenchError, canonical_json, sha256_bytes, sha256_file

DEFAULT_MANIFEST = HARNESS_ROOT / "corpus" / "ewouth-av2.toml"
DEFAULT_SUITES = HARNESS_ROOT / "corpus" / "suites.toml"
CHUNK = 1 << 20
TIMEOUT_S = 60  # socket timeout of each connection
REFERENCE_CONVENTION = "avmdec-rawvideo-sha256"
CASE_KEYS = (
    "id", "name", "url", "input_sha256", "bytes", "width", "height", "bit_depth", "chroma", "frames",
    "reference_sha256", "raw_bytes",
)  # fmt: skip


def _load_toml(path: Path, what: str) -> dict:
    try:
        return tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as error:
        raise BenchError(f"cannot read the {what} {path}: {error.strerror}. Check the path.") from None
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise BenchError(f"the {what} {path} is not valid TOML: {error}. Fix the file.") from None


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict:
    """Load a corpus manifest (TOML) and check the keys that the lock copies."""
    manifest = _load_toml(path, "corpus manifest")
    for key in ("id", "source", "imported_from", "oracle", "case"):
        if key not in manifest:
            raise BenchError(f"corpus manifest {path}: missing key {key!r}. Import the corpus again.")
    ids = set()
    for index, case in enumerate(manifest["case"]):
        where = f"case {case.get('id', f'#{index + 1}')}"
        missing = [key for key in CASE_KEYS if key not in case]
        if missing:
            raise BenchError(f"corpus manifest {path}: {where} misses {', '.join(missing)}. Import the corpus again.")
        if case["id"] in ids:
            raise BenchError(f"corpus manifest {path}: case id {case['id']!r} is used twice. Make ids unique.")
        ids.add(case["id"])
    return manifest


def manifest_digest(manifest: dict) -> str:
    """The corpus identity: sha256 of the canonical JSON of the parsed manifest. Comments do not count."""
    return sha256_bytes(canonical_json(manifest))


def suite_cases(manifest: dict, suites: dict, suite_id: str) -> list[dict]:
    """Return the manifest cases of a suite, in the order of the suite list."""
    if suite_id not in suites:
        raise BenchError(f"suite {suite_id!r} is not in the suites file (it has {', '.join(sorted(suites))}).")
    by_id = {case["id"]: case for case in manifest["case"]}
    unknown = [case_id for case_id in suites[suite_id] if case_id not in by_id]
    if unknown:
        raise BenchError(f"suite {suite_id!r} lists ids that are not in the corpus: {', '.join(unknown)}.")
    return [by_id[case_id] for case_id in suites[suite_id]]


def load_suites(path: Path = DEFAULT_SUITES) -> dict:
    return _load_toml(path, "suites file")


def lock_corpus(manifest: dict) -> dict:
    """The `corpus` object of a lock."""
    return {
        "id": manifest["id"],
        "digest": manifest_digest(manifest),
        "source": manifest["source"],
        "oracle": manifest["oracle"]["description"],
        "imported_from": manifest["imported_from"],
    }


def lock_case(manifest: dict, case: dict) -> dict:
    """Copy the facts of one case into the lock form. An absent `film_grain` is null (not known)."""
    copy = {
        key: case[key]
        for key in ("id", "name", "url", "input_sha256", "bytes", "width", "height", "bit_depth", "chroma", "frames")
    }
    copy["film_grain"] = case.get("film_grain")
    copy["reference"] = {
        "convention": REFERENCE_CONVENTION,
        "sha256": case["reference_sha256"],
        "raw_bytes": case["raw_bytes"],
    }
    copy["source_note"] = case.get("source_note")
    copy["terms_note"] = case.get("terms_note", manifest.get("terms_note"))
    copy["workload"] = {"kind": "decode"}
    if "mirrors" in case:
        copy["mirrors"] = case["mirrors"]
    return copy


# --- verified cache ------------------------------------------------------------


def cache_path(work: Path, case: dict) -> Path:
    digest = case["input_sha256"].removeprefix("sha256:")
    return Path(work) / "corpus" / (digest + (Path(case["name"]).suffix or ".bin"))


def _cache_problem(path: Path, case: dict) -> str | None:
    """Return why the cached file is not the stream of the case, or None when it is."""
    if not path.is_file():
        return "missing"
    size = path.stat().st_size
    if size != case["bytes"]:
        return f"has {size} bytes, expected {case['bytes']}"
    digest = sha256_file(path)
    if digest != case["input_sha256"]:
        return f"has {digest}, expected {case['input_sha256']}"
    return None


def _download(case: dict, destination: Path, synthetic: bool) -> None:
    """Fetch the stream from `url`, then from each mirror, and rename it into the cache only when it is right.

    A source that cannot be reached gives way to the next one. A source that
    sends other bytes ends the download: no different stream is ever accepted.
    """
    sources = [case["url"], *case.get("mirrors", [])]
    for url in sources:
        scheme = urllib.parse.urlsplit(url).scheme
        if scheme != "https" and not (scheme == "file" and synthetic):
            raise BenchError(f"case {case['id']}: {url} is not allowed. Use an https: URL.")
    errors = []
    for url in sources:
        descriptor, temporary = tempfile.mkstemp(dir=destination.parent, prefix=f".{destination.name}.", suffix=".part")
        try:
            digest, size = hashlib.sha256(), 0
            try:
                with os.fdopen(descriptor, "wb") as out, urllib.request.urlopen(url, timeout=TIMEOUT_S) as response:
                    # Read at most expected + 1 bytes: a longer stream is wrong, and the disk must not fill.
                    while size <= case["bytes"] and (chunk := response.read(min(CHUNK, case["bytes"] - size + 1))):
                        digest.update(chunk)
                        size += len(chunk)
                        out.write(chunk)
                    out.flush()
                    os.fsync(out.fileno())
            except (OSError, http.client.HTTPException) as error:
                errors.append(f"{url}: {getattr(error, 'strerror', None) or error}")
                continue
            if size > case["bytes"]:
                raise BenchError(
                    f"case {case['id']}: {url} sends more than the expected {case['bytes']} bytes. "
                    "The transfer was stopped and the download deleted. Do not use this source."
                )
            actual = "sha256:" + digest.hexdigest()
            if size != case["bytes"] or actual != case["input_sha256"]:
                raise BenchError(
                    f"case {case['id']}: {url} sent other bytes than the lock expects: got {size} bytes and {actual}, "
                    f"expected {case['bytes']} bytes and {case['input_sha256']}. The download was deleted. "
                    "Do not use this source; the lock names the only valid stream."
                )
            os.replace(temporary, destination)
            return
        finally:
            Path(temporary).unlink(missing_ok=True)
    raise BenchError(
        f"case {case['id']}: no source could be reached ({'; '.join(errors)}). Check the network, then run the command again.",
        EXIT_PREREQUISITE,
    )


def ensure(lock: dict, work: Path, *, offline: bool = False) -> dict[str, Path]:
    """Make every stream of the lock's suite available in `work` and return its path per case id.

    `lock` is the lock document (the object that `plan.load_lock` returns). A stream
    is stored as `<work>/corpus/<input sha256 hex>.<ext>`. Size and SHA-256 are
    checked on every call, also for a cached file. A bad cached file is deleted and
    downloaded again. With `offline` no connection is opened, and the call fails
    with the list of cases that are missing or wrong.
    """
    body = lock["lock"]
    synthetic = body["origin"] == "synthetic"
    cache = Path(work) / "corpus"
    cache.mkdir(parents=True, exist_ok=True)
    paths, problems = {}, []
    for case in body["suite"]["cases"]:
        path = cache_path(work, case)
        problem = _cache_problem(path, case)
        if problem is None:
            paths[case["id"]] = path
        elif offline:
            problems.append(f"{case['id']}: {path.name} {problem}")
        else:
            path.unlink(missing_ok=True)
            _download(case, path, synthetic)
            paths[case["id"]] = path
    if problems:
        raise BenchError(
            f"offline: {len(problems)} streams are not in {cache} or are wrong ({'; '.join(problems)}). "
            "Run `bench prepare` with a network connection first.",
            EXIT_PREREQUISITE,
        )
    return paths


# --- bench prepare -------------------------------------------------------------


def _prepare(args: argparse.Namespace) -> int:
    from . import plan  # plan imports this module

    lock = plan.load_lock(args.lock)
    plan.check_role(lock, args.role)
    paths = ensure(lock, args.work, offline=args.offline)
    print(f"corpus: {len(paths)} streams verified in {Path(args.work) / 'corpus'}")
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "prepare",
        help="fetch and verify the corpus of a lock",
        description="Check the lock, check that ROLE is in it, then download each stream of the lock to "
        "WORK/corpus and verify its size and SHA-256. A cached file is verified again. "
        "With --offline no connection is opened and a missing or wrong stream is an error. "
        "Today this is the only stage; the build stage is added later.",
    )
    parser.add_argument("--lock", required=True, type=Path, metavar="LOCK")
    parser.add_argument("--role", required=True, metavar="ROLE")
    parser.add_argument("--work", required=True, type=Path, metavar="W")
    parser.add_argument("--offline", action="store_true", help="never open a connection")
    parser.set_defaults(handler=_prepare)
