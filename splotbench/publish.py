# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""`bench publish`: copy one assembled dataset to S3-compatible storage (intended target: Cloudflare R2).

Nothing here runs unless the owner starts it. Configuration comes from the environment only.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import http.client
import json
import os
import re
import ssl
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

from . import assemble, schema, validate
from .util import EXIT_FAILURE, EXIT_PREREQUISITE, BenchError

REGION = "auto"  # the only region name R2 documents
MAX_OBJECT_BYTES = 256 * 1024 * 1024  # one PutObject per file; there is no multipart upload
IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
INDEX_CACHE = "public, max-age=60, must-revalidate"
INDEX_KEY = "index.json"
REQUIRED = (
    "SPLOTBENCH_S3_ENDPOINT",
    "SPLOTBENCH_S3_BUCKET",
    "SPLOTBENCH_PUBLIC_BASE_URL",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
)
CONTENT_TYPES = (
    (".tar.gz", "application/gzip"),
    (".json", "application/json"),
    (".csv", "text/csv; charset=utf-8"),
    ("SHA256SUMS", "text/plain; charset=utf-8"),
)
LAST = ("manifest.json", "SHA256SUMS")  # the files that mark a dataset as present go up last
LOCAL_HOSTS = ("127.0.0.1", "localhost")
_PREFIX = re.compile(r"[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*")


def _authorization(method: str, path: str, headers: dict[str, str], payload_hash: str, amz_date: str,
                   key_id: str, secret: str, region: str = REGION) -> str:  # fmt: skip
    """SigV4 `Authorization` value. `headers` has lower-case names and holds every signed header."""
    names = sorted(headers)
    canonical = "\n".join([
        method, path, "", "".join(f"{n}:{headers[n].strip()}\n" for n in names), ";".join(names), payload_hash,
    ])  # fmt: skip
    date = amz_date[:8]
    scope = f"{date}/{region}/s3/aws4_request"
    to_sign = f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}"
    key = ("AWS4" + secret).encode()
    for part in (date, region, "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return f"AWS4-HMAC-SHA256 Credential={key_id}/{scope}, SignedHeaders={';'.join(names)}, Signature={signature}"


class S3:
    """Path-style S3 object requests: GET and PUT with SigV4 and the real payload SHA-256."""

    def __init__(self, config: dict) -> None:
        self.endpoint = urlsplit(config["endpoint"])
        self.bucket, self.prefix = config["bucket"], config["prefix"]
        self.key_id, self.secret = config["key_id"], config["secret"]

    def request(self, method: str, key: str, body: bytes | None = None, headers: Mapping[str, str] = {}):
        """Return (status, response headers, body). One retry after a connection error."""
        key = f"{self.prefix}/{key}" if self.prefix else key
        path = "/" + quote(f"{self.bucket}/{key}", safe="/-_.~")
        payload_hash = hashlib.sha256(body or b"").hexdigest()
        for attempt in (1, 2):
            signed = {k.lower(): v for k, v in headers.items()}
            signed.update({
                "host": self.endpoint.netloc,
                "x-amz-content-sha256": payload_hash,
                "x-amz-date": (amz_date := datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")),
            })  # fmt: skip
            signed["authorization"] = _authorization(
                method, path, dict(signed), payload_hash, amz_date, self.key_id, self.secret
            )
            if self.endpoint.scheme == "https":
                connection = http.client.HTTPSConnection(self.endpoint.hostname, self.endpoint.port, timeout=120,
                                                         context=ssl.create_default_context())  # fmt: skip
            else:
                connection = http.client.HTTPConnection(self.endpoint.hostname, self.endpoint.port, timeout=120)
            try:
                connection.request(method, path, body=body, headers=signed)
                response = connection.getresponse()
                return response.status, response, response.read()
            except (OSError, http.client.HTTPException) as error:
                if attempt == 2:
                    raise BenchError(
                        f"cannot reach {self.endpoint.netloc} ({type(error).__name__}). "
                        "Check SPLOTBENCH_S3_ENDPOINT and the network, then run the command again.",
                        EXIT_PREREQUISITE,
                    ) from None
            finally:
                connection.close()

    def call(self, method: str, key: str, body: bytes | None = None, headers: Mapping[str, str] = {}):
        """Like `request`, but any status other than 200 is an error. Return (response, body)."""
        status, response, data = self.request(method, key, body, headers)
        self.check(status, data, method, key)
        return response, data

    @staticmethod
    def check(status: int, data: bytes, method: str, key: str) -> None:
        if status == 200:
            return
        refused = status in (401, 403)
        reason = "refused the credentials or the permission" if refused else "answered unexpectedly"
        detail = re.sub(r"\s+", " ", data.decode("utf-8", "replace"))[:200]
        raise BenchError(f"the storage {reason}: {method} {key} gave HTTP {status}. {detail}".strip(),
                         EXIT_PREREQUISITE if refused else EXIT_FAILURE)  # fmt: skip


def configuration(environ: Mapping[str, str], required: bool) -> dict | None:
    """Read the target from `environ`. With `required`, a missing name is exit 3; else return None."""
    missing = [name for name in REQUIRED if not environ.get(name)]
    if missing and required:
        raise BenchError(
            "no publication target. Set " + ", ".join(missing) + ". "
            "See docs/PUBLISHING.md; nothing was sent.",
            EXIT_PREREQUISITE,
        )
    endpoint = environ.get("SPLOTBENCH_S3_ENDPOINT", "")
    if endpoint:
        parts = urlsplit(endpoint)
        local = parts.scheme == "http" and parts.hostname in LOCAL_HOSTS
        if not (parts.scheme == "https" or local) or not parts.hostname or parts.path.strip("/") or parts.query:
            raise BenchError(
                "SPLOTBENCH_S3_ENDPOINT must be https://<host> without a path "
                "(http only for 127.0.0.1 or localhost). Fix the value.",
                EXIT_PREREQUISITE,
            )
    prefix = environ.get("SPLOTBENCH_S3_PREFIX", "")
    if prefix and not _PREFIX.fullmatch(prefix) or ".." in prefix.split("/"):
        raise BenchError(
            "SPLOTBENCH_S3_PREFIX must be path parts of letters, digits, '.', '_' and '-' with no leading "
            "or trailing '/'. Fix the value.",
            EXIT_PREREQUISITE,
        )
    if missing:
        return None
    return {
        "endpoint": endpoint, "bucket": environ["SPLOTBENCH_S3_BUCKET"], "prefix": prefix,
        "base_url": environ["SPLOTBENCH_PUBLIC_BASE_URL"].rstrip("/"),
        "key_id": environ["AWS_ACCESS_KEY_ID"], "secret": environ["AWS_SECRET_ACCESS_KEY"],
    }  # fmt: skip


def _content_type(path: str) -> str:
    for suffix, content_type in CONTENT_TYPES:
        if path.endswith(suffix):
            return content_type
    raise BenchError(f"{path}: no content type is defined for this file. A dataset holds only JSON, CSV, tar.gz and SHA256SUMS.")


def _plan(directory: Path) -> tuple[dict, list[dict]]:
    """Check the dataset locally and return (manifest, objects). Refuse what must never go out."""
    problems = validate.dataset_problems(directory)
    if problems:
        raise BenchError(f"{directory} is not a valid dataset ({len(problems)} problems):\n  " + "\n  ".join(problems))
    manifest = schema.loads((directory / "manifest.json").read_bytes())
    objects = []
    for path in sorted((p for p in directory.rglob("*") if p.is_file()), key=lambda p: (p.name in LAST, p.as_posix())):
        data = path.read_bytes()
        relative = path.relative_to(directory).as_posix()
        if len(data) > MAX_OBJECT_BYTES:
            raise BenchError(
                f"{relative} has {len(data)} bytes; the limit is {MAX_OBJECT_BYTES} (no multipart upload). "
                "Publish a smaller bundle."
            )
        objects.append({
            "path": relative, "data": data, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "content_type": _content_type(relative), "cache_control": IMMUTABLE_CACHE,
        })  # fmt: skip
    return manifest, objects


def _refuse(manifest: dict, allow_local: bool, promote: bool) -> None:
    origin = manifest["origin"]
    if origin == "synthetic":
        raise BenchError(f"{manifest['dataset_id']} is synthetic test data. It is never published.")
    if origin != "official" and not allow_local:
        raise BenchError(f"{manifest['dataset_id']} has origin {origin}. Only official datasets are published; "
                         "use --allow-local for a personal bucket.")  # fmt: skip
    if not manifest["complete"]:
        raise BenchError(f"{manifest['dataset_id']} is incomplete (required work is missing). Only complete datasets are published.")
    if promote and origin != "official":
        raise BenchError("--promote needs an official dataset: latest_complete names the latest complete official dataset.")


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


def _remote_state(client: S3, obj: dict, key: str) -> str:
    """Compare a remote object with the local bytes by SHA-256 of the content, never by ETag."""
    status, _, data = client.request("GET", key)
    if status == 404:
        return "absent"
    S3.check(status, data, "GET", key)
    return "identical" if hashlib.sha256(data).hexdigest() == obj["sha256"] else "different"


def _read_index(client: S3) -> tuple[dict | None, str | None]:
    status, response, data = client.request("GET", INDEX_KEY)
    if status == 404:
        return None, None
    S3.check(status, data, "GET", INDEX_KEY)
    problems = schema.validate(document := schema.loads(data))
    if problems:
        raise BenchError("the remote index.json is invalid; fix it by hand before publishing:\n  " + "\n  ".join(problems))
    return document, response.getheader("ETag")


def _put_index(client: S3, document: dict, etag: str | None) -> None:
    problems = schema.validate(document)
    if problems:  # a defect of this module
        raise BenchError("internal error: the new index.json is invalid:\n  " + "\n  ".join(problems))
    body = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode()
    condition = {"If-Match": etag} if etag else {"If-None-Match": "*"}
    headers = {"Content-Type": "application/json", "Cache-Control": INDEX_CACHE, **condition}
    status, _, data = client.request("PUT", INDEX_KEY, body, headers)
    if status in (409, 412):
        raise BenchError("another publication changed index.json while this one ran. The dataset files are uploaded "
                         "and complete; run the same command again.")  # fmt: skip
    S3.check(status, data, "PUT", INDEX_KEY)


def _upload(client: S3, key: str, obj: dict) -> bool:
    """Upload one object unless it exists. Return True when written, False when already present."""
    state = _remote_state(client, obj, key)
    if state == "absent":
        headers = {"Content-Type": obj["content_type"], "Cache-Control": obj["cache_control"], "If-None-Match": "*"}
        status, _, data = client.request("PUT", key, obj["data"], headers)
        if status == 200:
            return True
        if status not in (409, 412):  # 412: another writer stored it since the read; compare again
            S3.check(status, data, "PUT", key)
        state = _remote_state(client, obj, key)
    if state != "identical":
        raise BenchError(f"collision: {key} exists with different bytes. Datasets are immutable; nothing was changed. "
                         "Assemble the data again to get a new dataset id.")  # fmt: skip
    return False


def publish(directory: Path, environ: Mapping[str, str], *, dry_run: bool = False, promote: bool = False,
            allow_local: bool = False, out=print) -> int:  # fmt: skip
    """Validate, then (unless `dry_run`) publish the dataset in `directory`. Return the exit code."""
    manifest, objects = _plan(directory)
    _refuse(manifest, allow_local, promote)
    config = configuration(environ, required=not dry_run)
    dataset_id = manifest["dataset_id"]
    entry = assemble.index_entry(schema.loads((directory / "summary.json").read_bytes()))
    client = S3(config) if config else None
    prefix = f"datasets/{dataset_id}/"
    where = f"{config['endpoint']} bucket {config['bucket']} prefix {config['prefix'] or '(none)'}" if config else "(not configured)"

    if dry_run:
        out(f"dry run: nothing is written. Dataset {dataset_id}: origin {manifest['origin']}, complete {manifest['complete']}")
        out(f"destination: {where}")
        out(f"public URL: {config['base_url'] + '/' + prefix if config else '(SPLOTBENCH_PUBLIC_BASE_URL not set)'}")
        if config is None:
            names = ", ".join(n for n in REQUIRED if not environ.get(n))
            out(f"remote state not read; missing {names}")
        failed = False
        for obj in objects:
            state = _remote_state(client, obj, prefix + obj["path"]) if client else "not read"
            failed |= state == "different"
            out(f"  {prefix + obj['path']}  {obj['bytes']} bytes  sha256:{obj['sha256']}  {obj['content_type']}  {obj['cache_control']}  remote: {state}")
        if client:
            _, line = index_change(_read_index(client)[0], entry, promote)
            out(f"index.json ({INDEX_CACHE}): {line}")
        else:
            out(f"index.json: not read; entry for {dataset_id} is added unless listed; latest_complete "
                + (f"becomes {dataset_id}" if promote else "stays unchanged"))  # fmt: skip
        if failed:
            out("a remote object differs from the local file: publication would stop with a collision")
        return EXIT_FAILURE if failed else 0

    lock_path = directory.resolve().parent / ".publish.lock"
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise BenchError(f"another publication from this machine holds {lock_path}. Wait for it, then run again.") from None
        index_change(_read_index(client)[0], entry, promote)  # fail early on a conflicting entry, before any write
        written = []
        for obj in objects:
            key = prefix + obj["path"]
            if _upload(client, key, obj):
                written.append((key, obj))
                out(f"uploaded {key}")
            else:
                out(f"already present {key}")
        for key, obj in written:
            _, data = client.call("GET", key)
            if hashlib.sha256(data).hexdigest() != obj["sha256"] or len(data) != obj["bytes"]:
                raise BenchError(f"read-back mismatch: {key} in the storage differs from the local file. "
                                 "The index is unchanged. Check the storage, then run the command again.")  # fmt: skip
        document, etag = _read_index(client)
        new, line = index_change(document, entry, promote)
        if new is not None:
            _put_index(client, new, etag)
        out(f"index.json: {line}" + ("" if new is not None else " (not written)"))
        out(f"published {dataset_id}: {config['base_url']}/{prefix}manifest.json")
    return 0


def _publish(args: argparse.Namespace) -> int:
    return publish(args.dataset_dir, os.environ, dry_run=args.dry_run, promote=args.promote, allow_local=args.allow_local)


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "publish",
        help="publish an assembled dataset to S3-compatible storage",
        description="Validate DATASET_DIR (OUT/datasets/<dataset_id>), upload its files under "
        "<prefix>/datasets/<dataset_id>/ without overwriting, read them back, then add the dataset to "
        "<prefix>/index.json. Configure the target with SPLOTBENCH_S3_ENDPOINT, SPLOTBENCH_S3_BUCKET, "
        "SPLOTBENCH_PUBLIC_BASE_URL, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY and optionally SPLOTBENCH_S3_PREFIX. "
        "Nothing else publishes. See docs/PUBLISHING.md.",
    )
    parser.add_argument("dataset_dir", type=Path, metavar="DATASET_DIR")
    parser.add_argument("--dry-run", action="store_true", help="check and report; send no write request")
    parser.add_argument("--promote", action="store_true", help="make this dataset latest_complete (official datasets only)")
    parser.add_argument("--allow-local", action="store_true", help="allow origin local (never synthetic)")
    parser.set_defaults(handler=_publish)
