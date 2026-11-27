# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Pinned decoder sources and builds under the declared policy optimized-source/1.

`ensure(lock, role, work)` fetches each locked commit into `W/src/<id>/<commit>/`,
builds it out of tree into `W/build/<id>/<commit>/` by the recipe of its adapter
and writes `build.json` (schema `splotbench.build/1`) and `build.log` there.
"""

from __future__ import annotations

import json
import os
import platform
import shlex
import shutil
import subprocess
from pathlib import Path

from . import adapters, bundle, provenance, schema
from .util import EXIT_PREREQUISITE, BenchError, recorded_env, run, sha256_file, write_atomic

# Flag variables would change the compiler settings behind the recipe; the policy takes flags from the recipe only.
POLICY_ENV = (
    "CFLAGS",
    "CXXFLAGS",
    "LDFLAGS",
    "RUSTFLAGS",
    "CARGO_ENCODED_RUSTFLAGS",
    "CARGO_BUILD_RUSTFLAGS",
    "CARGO_BUILD_TARGET",
)
NO_PROMPT = {"GIT_TERMINAL_PROMPT": "0"}
MANIFEST = "build.json"
LOG = "build.log"


def native_arch() -> str:
    machine = platform.machine().lower()
    return {"arm64": "aarch64", "aarch64": "aarch64", "amd64": "x86_64", "x86_64": "x86_64"}.get(machine, machine)


def default_jobs() -> int:
    return max(1, (os.cpu_count() or 2) // 2)


def source_dir(work: Path, implementation: dict) -> Path:
    return Path(work) / "src" / implementation["id"] / implementation["commit"]


def build_dir(work: Path, implementation: dict) -> Path:
    return Path(work) / "build" / implementation["id"] / implementation["commit"]


def read_manifest(path: Path) -> dict:
    data = path.read_bytes()
    problems = bundle.document_problems(path.name, data, ("object", "splotbench.build/1"))
    if problems:
        raise BenchError(f"{path} is not a valid build manifest: {problems[0]}. Remove {path.parent} and build again.")
    return schema.loads(data)


def load_manifest(work: Path, implementation: dict) -> dict:
    """Return the build manifest of a locked implementation; a missing build is a missing prerequisite."""
    path = build_dir(work, implementation) / MANIFEST
    if not path.is_file():
        raise BenchError(f"no build of {implementation['id']} at {path}. Run `bench prepare` first.", EXIT_PREREQUISITE)
    return read_manifest(path)


def adapter_for(lock: dict, implementation: dict):
    """Return the adapter module. Only a synthetic lock may use the fixture adapter."""
    if implementation["adapter"] == "fixture" and lock["lock"]["origin"] != "synthetic":
        raise BenchError(
            f"implementation {implementation['id']!r} uses the fixture adapter in a {lock['lock']['origin']} lock. "
            "Only a synthetic lock may use it."
        )
    return adapters.get(implementation["adapter"])


def role_arch(lock: dict, role: str) -> str:
    roles = lock["lock"]["roles"]
    if role not in roles:
        raise BenchError(f"role {role!r} is not in the lock; it has {', '.join(sorted(roles))}.", 2)
    arch = roles[role]["arch"]
    if arch != native_arch():
        raise BenchError(f"role {role!r} needs {arch}, but this host is {native_arch()}. Use a native {arch} host.")
    return arch


def ensure(lock: dict, role: str, work: Path, jobs: int | None = None) -> list[dict]:
    """Fetch, build or reuse every implementation of the lock for `role`. Return one manifest each."""
    work = Path(work)
    arch = role_arch(lock, role)
    found = [name for name in POLICY_ENV if name in os.environ]
    if found:
        raise BenchError(
            f"{', '.join(found)} is set. Policy optimized-source/1 takes compiler flags from the recipe only; "
            "unset it and run again.",
            EXIT_PREREQUISITE,
        )
    pairs = [(implementation, adapter_for(lock, implementation)) for implementation in lock["lock"]["implementations"]]
    for implementation, adapter in pairs:
        for tool in adapter.tools(arch):
            if shutil.which(tool) is None:
                raise BenchError(
                    f"{tool} is not on PATH; the {implementation['id']} build needs it. Install it and run again.",
                    EXIT_PREREQUISITE,
                )
    return [_build(lock, implementation, adapter, arch, work, jobs or default_jobs()) for implementation, adapter in pairs]


def _fetch(implementation: dict, src: Path) -> None:
    """Fetch exactly the locked commit. A branch or tag is never resolved here."""
    if src.exists():
        return
    temporary = src.with_name(f".{src.name}.partial")
    shutil.rmtree(temporary, ignore_errors=True)
    temporary.mkdir(parents=True)
    steps = (
        ["init", "-q"],
        ["fetch", "-q", "--depth", "1", implementation["repository"], implementation["commit"]],
        ["-c", "advice.detachedHead=false", "checkout", "-q", "--detach", "FETCH_HEAD"],
    )
    for step in steps:
        done = run(["git", "-C", str(temporary), *step], extra_env=NO_PROMPT)
        if done.returncode != 0:
            shutil.rmtree(temporary, ignore_errors=True)
            detail = done.stderr.decode("utf-8", "replace").strip()
            raise BenchError(
                f"cannot fetch {implementation['commit']} of {implementation['repository']}: {detail}. "
                "Check the network and that the upstream still serves the commit.",
                EXIT_PREREQUISITE,
            )
    os.replace(temporary, src)


def _clean_snapshot(src: Path, commit: str) -> dict:
    source = provenance.snapshot(src)
    if not provenance.clean_at(source, commit):
        raise BenchError(
            f"{src} is not a clean checkout of {commit} (commit {source['commit']}, dirty {source['dirty']}). "
            "Remove the directory; the next build fetches the locked commit again."
        )
    return source


def _first_line(argv: list[str], cwd: Path) -> str:
    done = run(argv, cwd=cwd)
    text = (done.stdout or done.stderr).decode("utf-8", "replace")
    if done.returncode != 0:
        raise BenchError(
            f"`{shlex.join(argv)}` failed in {cwd}: {done.stderr.decode('utf-8', 'replace').strip()}. "
            "Install the toolchain that the source pins.",
            EXIT_PREREQUISITE,
        )
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def _build(lock: dict, implementation: dict, adapter, arch: str, work: Path, jobs: int) -> dict:
    src, out = source_dir(work, implementation), build_dir(work, implementation)
    commit = implementation["commit"]
    _fetch(implementation, src)
    source = _clean_snapshot(src, commit)
    recipe = adapter.recipe(src, arch)
    arguments = [part for step in recipe["steps"] for part in ["&&", *step]][1:]
    configuration = {**recipe["configuration"], "arguments": arguments}

    manifest_path = out / MANIFEST
    if manifest_path.exists():
        recorded = read_manifest(manifest_path)
        if recorded["source"]["build_origin"] == "upstream-release-asset":
            raise BenchError(f"{manifest_path}: build origin upstream-release-asset is not supported by this harness.")
        if recorded["source"]["snapshot"] == source and recorded["configuration"] == configuration:
            provenance.check_binary(work, recorded)
            return recorded
    # Another snapshot or configuration, or an unfinished build: start from an empty build directory.
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    tokens = {"<src>": str(src), "<build>": str(out), "<jobs>": str(jobs)}

    def expand(text: str) -> str:
        for token, value in tokens.items():
            text = text.replace(token, value)
        return text

    toolchain = {name: _first_line(argv, src) for name, argv in recipe["toolchain"].items()}
    target_text = run(recipe["target"], cwd=src).stdout.decode("utf-8", "replace")
    host = [line.removeprefix("host:").strip() for line in target_text.splitlines() if line.startswith("host:")]
    target = host[0] if host else target_text.strip()

    log = out / LOG
    with open(log, "wb") as file:
        for step in recipe["steps"]:
            argv = [expand(part) for part in step]
            file.write(f"$ {shlex.join(argv)}\n".encode())
            file.flush()
            done = subprocess.run(argv, cwd=src, stdout=file, stderr=subprocess.STDOUT, check=False)
            if done.returncode != 0:
                raise BenchError(
                    f"build step `{shlex.join(argv)}` of {implementation['id']} failed with exit code "
                    f"{done.returncode}. See {log}."
                )

    if recipe["simd_check"]:
        relative, text = recipe["simd_check"]
        path = out / relative
        if not path.is_file() or text not in path.read_text(errors="replace"):
            raise BenchError(
                f"{path} does not contain {text!r}: the {implementation['id']} build has no SIMD/assembly "
                "on this platform, so it cannot follow optimized-source/1."
            )
    binary = Path(expand(recipe["binary"]))
    if not binary.is_file():
        raise BenchError(f"the {implementation['id']} build finished without {binary}. See {log}.")
    if provenance.snapshot(src) != source:
        raise BenchError(f"the {implementation['id']} build changed its source tree {src}. Report it; builds are out of tree.")

    manifest = {
        "schema": "splotbench.build/1",
        "build_id": "",
        "implementation": implementation["id"],
        "class": "synthetic" if lock["lock"]["origin"] == "synthetic" else provenance.identity_class(source, commit),
        "source": {
            "repository": implementation["repository"],
            "commit": commit,
            "identity": implementation["identity"],
            "release": implementation["release"],
            "build_origin": provenance.build_origin(implementation["release"], source, commit),
            "snapshot": source,
            "lockfiles": [{"path": path, "sha256": sha256_file(src / path)} for path in recipe["lockfiles"]],
        },
        **provenance.binary_identity(work, binary),
        "version_output": adapter.version(binary),
        "target": target,
        "toolchain": toolchain,
        "configuration": configuration,
        "environment": recorded_env(),
    }
    manifest["build_id"] = provenance.build_id(manifest)
    problems = schema.validate(manifest)
    if problems:
        raise BenchError(f"internal error: the {implementation['id']} build manifest is invalid: {problems[0]}")
    write_atomic(manifest_path, (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode())
    return manifest
