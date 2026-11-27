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
import tomllib
from pathlib import Path

from . import adapters, bundle, provenance, schema
from .util import EXIT_PREREQUISITE, BenchError, run, sha256_file, write_atomic

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
# Every build step and toolchain probe runs with a new environment of only these variables and LC_ALL=C.
PASSED_ENV = ("PATH", "HOME", "TMPDIR", "SDKROOT", "DEVELOPER_DIR", "RUSTUP_HOME", "CARGO_HOME")
# The variables of util.ENV_ALLOWLIST that a build may use; the manifest records the ones that were set.
PERMITTED_ENV = ("CC", "CXX", "MACOSX_DEPLOYMENT_TARGET")
# Cargo configuration tables that change what a build makes.
CARGO_BUILD_TABLES = ("build", "target", "profile", "env", "host", "unstable", "patch")
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
    """Return the adapter module. A synthetic lock uses only the fixture adapter, a standard lock never."""
    where = f"lock implementation {implementation['id']!r} adapter"
    adapters.check_origin(implementation["adapter"], lock["lock"]["origin"], where)
    return adapters.get(implementation["adapter"])


def build_env(environ=os.environ) -> dict[str, str]:
    """The whole environment of a build step: nothing else of the caller's environment reaches the tools."""
    env = {name: environ[name] for name in PASSED_ENV + PERMITTED_ENV if name in environ}
    env["LC_ALL"] = "C"
    return env


def cargo_config_problems(src: Path, env: dict[str, str]) -> list[str]:
    """Cargo configuration files outside the checkout that would change the build.

    Cargo reads `.cargo/config.toml` and `.cargo/config` in every parent directory of
    the checkout and in the Cargo home. The checkout's own file is part of the snapshot.
    """
    home = Path(env["CARGO_HOME"]) if "CARGO_HOME" in env else Path(env.get("HOME", "/nonexistent")) / ".cargo"
    places = [directory / ".cargo" for directory in Path(src).resolve().parents] + [home]
    problems = []
    for path in (place / name for place in dict.fromkeys(places) for name in ("config.toml", "config")):
        if not path.is_file():
            continue
        try:
            tables = sorted(set(tomllib.loads(path.read_text())) & set(CARGO_BUILD_TABLES))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
            tables = [f"unreadable: {error}"]
        if tables:
            problems.append(f"{path} ({', '.join(tables)})")
    return problems


def role_arch(lock: dict, role: str) -> str:
    roles = lock["lock"]["roles"]
    if role not in roles:
        raise BenchError(f"role {role!r} is not in the lock; it has {', '.join(sorted(roles))}.", 2)
    arch = roles[role]["arch"]
    if arch != native_arch():
        raise BenchError(f"role {role!r} needs {arch}, but this host is {native_arch()}. Use a native {arch} host.")
    return arch


def ensure(lock: dict, role: str, work: Path, jobs: int | None = None, *, offline: bool = False) -> list[dict]:
    """Fetch, build or reuse every implementation of the lock for `role`. Return one manifest each.

    With `offline` nothing is fetched or built: every checkout and finished build must be present.
    """
    work = Path(work)
    if jobs is not None and jobs < 1:
        raise BenchError(f"--jobs {jobs} is not a positive number of build jobs.", 2)
    arch = role_arch(lock, role)
    found = [name for name in POLICY_ENV if name in os.environ]
    if found:  # not passed to the build anyway; refused so that nobody believes they apply
        raise BenchError(
            f"{', '.join(found)} is set. Policy optimized-source/1 takes compiler flags from the recipe only; "
            "unset it and run again.",
            EXIT_PREREQUISITE,
        )
    pairs = [(implementation, adapter_for(lock, implementation)) for implementation in lock["lock"]["implementations"]]
    return [_build(lock, entry, adapter, arch, work, jobs if jobs is not None else default_jobs(), offline) for entry, adapter in pairs]


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
    """Snapshot of a clean checkout at `commit`. Files that Git ignores count too: a build could read them."""
    source = provenance.snapshot(src)
    ignored = run(["git", "-C", str(src), "--no-optional-locks", "status", "--porcelain", "--ignored"]).stdout.decode("utf-8", "replace")
    if not provenance.clean_at(source, commit) or ignored.strip():
        found = "; ".join(ignored.splitlines()[:3])
        raise BenchError(
            f"{src} is not a clean checkout of {commit} (commit {source['commit']}, dirty {source['dirty']}"
            + (f", extra files: {found}" if found else "")
            + "). Remove the directory; the next build fetches the locked commit again."
        )
    return source


def _provenance(lock: dict, implementation: dict, source: dict) -> dict:
    """The fields of a manifest that come from the lock. They are not part of build_id."""
    commit = implementation["commit"]
    synthetic = lock["lock"]["origin"] == "synthetic"
    return {
        "class": "synthetic" if synthetic else provenance.identity_class(source, commit),
        "repository": implementation["repository"],
        "identity": implementation["identity"],
        "release": implementation["release"],
        "build_origin": provenance.build_origin(implementation["release"], source, commit),
    }


def _write_manifest(path: Path, manifest: dict) -> None:
    problems = schema.validate(manifest)
    if problems:
        raise BenchError(f"internal error: the {manifest['implementation']} build manifest is invalid: {problems[0]}")
    write_atomic(path, (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode())


def _probe(argv: list[str], cwd: Path, env: dict[str, str]) -> str:
    """Run a toolchain probe in the build environment; return its output. A failure is a missing prerequisite."""
    if shutil.which(argv[0], path=env.get("PATH")) is None:
        raise BenchError(f"{argv[0]} is not on PATH. Install it, then run the command again.", EXIT_PREREQUISITE)
    done = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True, check=False)
    text = (done.stdout or done.stderr).decode("utf-8", "replace")
    if done.returncode != 0:
        raise BenchError(
            f"`{shlex.join(argv)}` failed in {cwd}: {done.stderr.decode('utf-8', 'replace').strip()}. "
            "Install the toolchain that the source pins.",
            EXIT_PREREQUISITE,
        )
    return text


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def _offline_error(what: str, path: Path) -> BenchError:
    return BenchError(f"offline: {what} is missing at {path}. Run `bench prepare` without --offline first.", EXIT_PREREQUISITE)


def _build(lock: dict, implementation: dict, adapter, arch: str, work: Path, jobs: int, offline: bool) -> dict:
    src, out = source_dir(work, implementation), build_dir(work, implementation)
    commit = implementation["commit"]
    if offline and not src.exists():
        raise _offline_error(f"the {implementation['id']} checkout of {commit}", src)
    _fetch(implementation, src)
    source = _clean_snapshot(src, commit)
    recipe = adapter.recipe(src, arch)
    arguments = [part for step in recipe["steps"] for part in ["&&", *step]][1:]
    configuration = {**recipe["configuration"], "arguments": arguments}

    current = _provenance(lock, implementation, source)
    env = build_env()
    manifest_path = out / MANIFEST
    if manifest_path.exists():
        recorded = read_manifest(manifest_path)
        if recorded["source"]["build_origin"] == "upstream-release-asset":
            raise BenchError(f"{manifest_path}: build origin upstream-release-asset is not supported by this harness.")
        same_kind = (recorded["class"] == "synthetic") == (current["class"] == "synthetic")
        if same_kind and recorded["source"]["snapshot"] == source and recorded["configuration"] == configuration:
            provenance.check_binary(work, recorded)
            # Another lock can name the same commit with another repository URL, release or origin.
            updated = {**recorded, "class": current.pop("class")}
            updated["source"] = {**recorded["source"], **current}
            if updated != recorded:
                _write_manifest(manifest_path, updated)
            return updated
    if offline:
        raise _offline_error(f"a finished {implementation['id']} build of this snapshot and configuration", manifest_path)
    if any(step[0] == "cargo" for step in recipe["steps"]):
        problems = cargo_config_problems(src, env)
        if problems:
            raise BenchError(
                f"Cargo configuration outside the checkout would change the {implementation['id']} build: "
                f"{'; '.join(problems)}. Move the file, or set CARGO_HOME to a directory without it.",
                EXIT_PREREQUISITE,
            )
    for tool in adapter.tools(arch):
        if shutil.which(tool, path=env.get("PATH")) is None:
            raise BenchError(
                f"{tool} is not on PATH; the {implementation['id']} build needs it. Install it and run again.",
                EXIT_PREREQUISITE,
            )
    # Another snapshot or configuration, or an unfinished build: start from an empty build directory.
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    tokens = {"<src>": str(src), "<build>": str(out), "<jobs>": str(jobs)}

    def expand(text: str) -> str:
        for token, value in tokens.items():
            text = text.replace(token, value)
        return text

    toolchain = {name: _first_line(_probe(argv, src, env)) for name, argv in recipe["toolchain"].items()}
    for name, prefix in recipe.get("expect", {}).items():
        if not toolchain[name].startswith(prefix):
            raise BenchError(
                f"the {implementation['id']} source pins {prefix.strip()!r}, but {toolchain[name]!r} runs. "
                "Remove the rustup override for this directory and install the pinned toolchain.",
                EXIT_PREREQUISITE,
            )
    target_text = _probe(recipe["target"], src, env)
    host = [line.removeprefix("host:").strip() for line in target_text.splitlines() if line.startswith("host:")]
    target = host[0] if host else target_text.strip()

    log = out / LOG
    with open(log, "wb") as file:
        for step in recipe["steps"]:
            argv = [expand(part) for part in step]
            file.write(f"$ {shlex.join(argv)}\n".encode())
            file.flush()
            done = subprocess.run(
                argv, cwd=src, env=env, stdin=subprocess.DEVNULL, stdout=file, stderr=subprocess.STDOUT, check=False
            )
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
    try:
        unchanged = _clean_snapshot(src, commit) == source
    except BenchError:
        unchanged = False
    if not unchanged:
        raise BenchError(f"the {implementation['id']} build changed its source tree {src}. Report it; builds are out of tree.")

    manifest = {
        "schema": "splotbench.build/1",
        "build_id": "",
        "implementation": implementation["id"],
        "class": current["class"],
        "source": {
            "repository": current["repository"],
            "commit": commit,
            "identity": current["identity"],
            "release": current["release"],
            "build_origin": current["build_origin"],
            "snapshot": source,
            "lockfiles": [{"path": path, "sha256": sha256_file(src / path)} for path in recipe["lockfiles"]],
        },
        **provenance.binary_identity(work, binary),
        "version_output": adapter.version(binary),
        "target": target,
        "toolchain": toolchain,
        "configuration": configuration,
        "environment": {name: env[name] for name in PERMITTED_ENV if name in env},
    }
    manifest["build_id"] = provenance.build_id(manifest)
    _write_manifest(manifest_path, manifest)
    return manifest
