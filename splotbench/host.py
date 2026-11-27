# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Host inspection: native architecture, observed fingerprint, machine id and the host lock.

Nothing here changes a system setting. A fact that the OS does not give without
privilege is null. The fingerprint holds no host name, user name or path.
"""

from __future__ import annotations

import fcntl
import os
import platform
import re
import shutil
import stat
import struct
import subprocess
import sys
import sysconfig
from pathlib import Path

from .util import BenchError, canonical_json, process_alive, sha256_bytes

ARCH = {"arm64": "aarch64", "aarch64": "aarch64", "amd64": "x86_64", "x86_64": "x86_64"}
MACHINE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
LOCK_DIRECTORY = None  # None: see lock_directory(); tests point it elsewhere
COMMAND_TIMEOUT_S = 10


def _output(argv: list[str]) -> str | None:
    """Stdout of a plain informational command, or None when it is missing or fails."""
    path = shutil.which(argv[0]) or shutil.which(argv[0], path="/usr/sbin:/usr/bin:/sbin:/bin")
    if path is None:
        return None
    try:
        done = subprocess.run([path, *argv[1:]], capture_output=True, timeout=COMMAND_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.decode("utf-8", "replace").strip() if done.returncode == 0 else None


def _sysctl(name: str) -> str | None:
    return _output(["sysctl", "-n", name])


def _int(text: str | None) -> int | None:
    return int(text) if text and text.strip().isdigit() else None


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(errors="replace").strip()
    except OSError:
        return None


# --- architecture ---------------------------------------------------------------


def _arch_facts() -> tuple[str | None, str | None, bool]:
    """Return (kernel architecture, architecture of this Python process, translated by Rosetta)."""
    if sys.platform == "darwin":
        kernel = "aarch64" if _sysctl("hw.optional.arm64") == "1" else "x86_64"
        return kernel, ARCH.get(platform.machine().lower()), _sysctl("sysctl.proc_translated") == "1"
    process = ARCH.get(sysconfig.get_platform().rsplit("-", 1)[-1].lower())
    return ARCH.get(os.uname().machine.lower()), process, False


def native_arch() -> str:
    """Return the native architecture. Refuse emulation and a non-64-bit interpreter."""
    kernel, process, translated = _arch_facts()
    if translated or process != kernel or struct.calcsize("P") != 8:
        raise BenchError(
            f"this Python runs as {process or platform.machine()} on a {kernel or os.uname().machine} kernel "
            f"({'Rosetta translation' if translated else 'emulation or another architecture'}). "
            f"Run the harness with a native 64-bit {kernel} Python."
        )
    if kernel is None:
        raise BenchError(f"architecture {os.uname().machine} is not supported. Use an aarch64 or x86_64 host.")
    return kernel


def check_role(lock: dict, role: str) -> str:
    """Return the native architecture when it is the `arch` of the role, else refuse."""
    arch = native_arch()
    wanted = lock["lock"]["roles"][role]["arch"]
    if wanted != arch:
        raise BenchError(f"role {role!r} needs {wanted}, but this host is {arch}. Run this role on a native {wanted} host.")
    return arch


def check_machine_id(machine_id: str) -> None:
    if not MACHINE_ID.fullmatch(machine_id):
        raise BenchError(
            f"machine id {machine_id!r} is not valid. Choose 2 to 41 characters a-z, 0-9 and '-', starting with a "
            "letter or digit, for example 'm2max-01'. Never use a MAC address or a serial number.",
            2,
        )


# --- fingerprint ----------------------------------------------------------------


def _macos() -> dict:
    perf_levels = _int(_sysctl("hw.nperflevels"))
    split = perf_levels == 2
    features = []
    for line in (_output(["sysctl", "hw.optional"]) or "").splitlines():
        name, _, value = line.partition(":")
        if value.strip() == "1" and name.startswith("hw.optional."):
            features.append(name.removeprefix("hw.optional."))
    power = []
    battery = _output(["pmset", "-g", "batt"]) or ""
    if found := re.search(r"drawing from '([^']+)'", battery):
        power.append(f"source={found.group(1)}")
    for line in (_output(["pmset", "-g"]) or "").splitlines():
        if found := re.fullmatch(r"\s*(lowpowermode|powermode)\s+(\d+)\s*", line):
            power.append(f"{found.group(1)}={found.group(2)}")
    return {
        "os": {"name": "macOS", "version": platform.mac_ver()[0] or None, "kernel": f"Darwin {os.uname().release}"},
        "cpu": {
            "model": _sysctl("machdep.cpu.brand_string"),
            "physical_cores": _int(_sysctl("hw.physicalcpu")),
            "logical_cores": _int(_sysctl("hw.logicalcpu")),
            "performance_cores": _int(_sysctl("hw.perflevel0.physicalcpu")) if split else None,
            "efficiency_cores": _int(_sysctl("hw.perflevel1.physicalcpu")) if split else None,
            "isa": sorted(features),
        },
        "memory_bytes": _int(_sysctl("hw.memsize")),
        "power": "; ".join(power) or None,
        "affinity": None,  # macOS has no CPU affinity setting
    }


def _ranges(numbers: list[int]) -> str:
    """`[0, 1, 2, 5]` -> `0-2,5`."""
    parts, start = [], None
    for index, number in enumerate(numbers):
        if start is None:
            start = number
        if index + 1 == len(numbers) or numbers[index + 1] != number + 1:
            parts.append(str(start) if start == number else f"{start}-{number}")
            start = None
    return ",".join(parts)


def _linux() -> dict:
    cpuinfo = _read("/proc/cpuinfo") or ""
    first = {}
    for line in cpuinfo.split("\n\n")[0].splitlines():
        key, _, value = line.partition(":")
        first.setdefault(key.strip(), value.strip())
    cores = set()
    for topology in Path("/sys/devices/system/cpu").glob("cpu[0-9]*/topology"):
        package, core = _read(f"{topology}/physical_package_id"), _read(f"{topology}/core_id")
        if package is not None and core is not None:
            cores.add((package, core))
    governors = sorted(
        {g for p in Path("/sys/devices/system/cpu").glob("cpu[0-9]*/cpufreq/scaling_governor") if (g := _read(str(p)))}
    )
    try:
        os_version = platform.freedesktop_os_release().get("PRETTY_NAME")
    except OSError:
        os_version = None
    pages = os.sysconf("SC_PHYS_PAGES") if "SC_PHYS_PAGES" in os.sysconf_names else -1
    return {
        "os": {"name": "Linux", "version": os_version, "kernel": f"Linux {os.uname().release}"},
        "cpu": {
            "model": first.get("model name") or first.get("Model") or None,
            "physical_cores": len(cores) or None,
            "logical_cores": os.cpu_count(),
            "performance_cores": None,
            "efficiency_cores": None,
            "isa": sorted(set((first.get("flags") or first.get("Features") or "").split())),
        },
        "memory_bytes": pages * os.sysconf("SC_PAGE_SIZE") if pages > 0 else None,
        "power": f"governor={','.join(governors)}" if governors else None,
        "affinity": f"cpus={_ranges(sorted(os.sched_getaffinity(0)))}",
    }


def fingerprint() -> dict:
    """Observe the host facts (schema `common#/$defs/fingerprint`). Each fact is read, never set."""
    arch = native_arch()
    if sys.platform == "darwin":
        facts = _macos()
    elif sys.platform == "linux":
        facts = _linux()
    else:
        raise BenchError(f"platform {sys.platform} is not supported. Use Linux or macOS.")
    return {"arch": arch, **facts, "python": platform.python_version()}


def host_document(machine_id: str, facts: dict) -> dict:
    """The `splotbench.host/1` document."""
    return {
        "schema": "splotbench.host/1",
        "machine_id": machine_id,
        "fingerprint_digest": sha256_bytes(canonical_json(facts)),
        "fingerprint": facts,
    }


# --- host lock ------------------------------------------------------------------


def _own_directory(path: str | None) -> bool:
    return bool(path) and os.path.isdir(path) and not os.path.islink(path) and os.stat(path).st_uid == os.getuid()


def lock_directory() -> Path:
    """A directory that only this user can write, the same for every work directory.

    `$XDG_RUNTIME_DIR/splotbench` when that is an existing directory of this user, else
    `$XDG_CACHE_HOME/splotbench`, else `~/.cache/splotbench`. A shared temporary directory
    would let another user squat the lock file.
    """
    if LOCK_DIRECTORY:
        return Path(LOCK_DIRECTORY)
    runtime, cache = os.environ.get("XDG_RUNTIME_DIR"), os.environ.get("XDG_CACHE_HOME")
    base = Path(runtime) if _own_directory(runtime) else Path(cache) if cache else Path.home() / ".cache"
    return base / "splotbench"


def lock_path() -> Path:
    """The one host lock of this user."""
    return lock_directory() / "host.lock"


def _check_directory(directory: Path, create: bool) -> None:
    """Create the lock directory with mode 0700; refuse a link or anything but a directory of this user."""
    if create:
        directory.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            pass
    status = os.lstat(directory)
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode) or status.st_uid != os.getuid():
        raise BenchError(
            f"the host lock directory {directory} is a symbolic link or not a directory of this user. "
            "Remove it, then run again."
        )


def held_message(path: Path, holder: str) -> str:
    """Name the holder of the host lock: the harness, or a decoder that a harness left running."""
    if holder.isdigit() and process_alive(int(holder)):
        return f"another harness invocation holds {path} (process {holder}). Wait until it ends, then run the command again."
    return (
        f"{path} is held, but harness process {holder} has ended: a decoder process that it started still runs and "
        "keeps the lock. Find it with `ps -A -o pid,pgid,command`, stop its process group, then run the command again."
    )


def _open_lock(path: Path, flags: int) -> int:
    """Open the lock file without following a link; refuse anything but a regular file of this user."""
    _check_directory(path.parent, create=bool(flags & os.O_CREAT))
    try:
        descriptor = os.open(path, flags | os.O_NOFOLLOW, 0o600)
    except FileNotFoundError:
        raise
    except OSError as error:  # ELOOP: a symbolic link
        raise BenchError(f"cannot open the host lock {path}: {error.strerror}. Remove it, then run again.") from None
    status = os.fstat(descriptor)
    if not stat.S_ISREG(status.st_mode) or status.st_uid != os.getuid():
        os.close(descriptor)
        raise BenchError(f"the host lock {path} is not a regular file of this user. Remove it, then run again.")
    return descriptor


class HostLock:
    """One harness run at a time on this host. The kernel releases the lock when the last process
    that has it open ends; `bench run` and `bench verify` pass it to each decoder, so an orphaned
    decoder keeps it."""

    def __init__(self) -> None:
        self.path = lock_path()
        self.descriptor = None

    def __enter__(self) -> "HostLock":
        descriptor = _open_lock(self.path, os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = os.read(descriptor, 64).decode("utf-8", "replace").strip() or "unknown"
            os.close(descriptor)
            raise BenchError(held_message(self.path, holder)) from None
        os.ftruncate(descriptor, 0)
        os.write(descriptor, f"{os.getpid()}\n".encode())
        self.descriptor = descriptor
        return self

    def __exit__(self, *_exc) -> None:
        os.close(self.descriptor)


def lock_holder() -> str | None:
    """Return the process that holds the host lock, or None when it is free. Creates nothing."""
    try:
        descriptor = _open_lock(lock_path(), os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return None
    except BlockingIOError:
        return os.read(descriptor, 64).decode("utf-8", "replace").strip() or "unknown"
    finally:
        os.close(descriptor)
