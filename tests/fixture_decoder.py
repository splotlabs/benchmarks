#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Synthetic fixture decoder for tests. It plays a decoder and decodes nothing.

Usage: decoder [--threads N] [-o PATH|-|--discard] INPUT. INPUT is JSON:
{"mode": ..., "fill": byte value, "bytes": output size, "frames": count, "pid_file": path,
"timed": {overrides for the timed run --discard}, "marker": path (apply "timed" once only)}.
Modes: ok, wrong (other bytes), short (half the bytes), long (one byte more), fail (exit 3),
unsupported, unsupported-option, hang, spawn-hang (start a descendant, write both pids, hang),
exit-after-output (correct bytes, exit 1), signal (correct bytes, then SIGKILL itself),
noisy (8 MiB on stderr, then correct bytes), orphan-pipe (start a descendant that keeps
stdout open, write both pids, write correct bytes, exit 0).
"""

import json
import os
import signal
import subprocess
import sys
import time


def main(argv: list[str]) -> int:
    if "--help" in argv:
        print("usage: decoder [--threads N] [-o PATH|-|--discard] INPUT  (synthetic fixture decoder)")
        return 0
    if "--version" in argv:
        print("fixture-decoder 1.0 (synthetic)")
        return 0
    words = iter(argv)
    sink, source = None, None
    for word in words:
        if word == "--threads":
            next(words)
        elif word == "-o":
            sink = next(words)
        elif word == "--discard":
            sink = None
        else:
            source = word
    with open(source) as file:
        spec = json.load(file)
    # The timed run (--discard) uses the "timed" overrides; with a "marker" path only once.
    if sink is None and not os.path.exists(spec.get("marker", "")):
        if "marker" in spec:
            open(spec["marker"], "w").close()
        spec.update(spec.get("timed", {}))
    mode = spec["mode"]
    if mode == "unsupported-option":
        print("decoder: unrecognized option '--threads'", file=sys.stderr)
        return 2
    if mode == "unsupported":
        print("fixture: unsupported stream", file=sys.stderr)
        return 1
    if mode == "fail":
        print("fixture: decode failed", file=sys.stderr)
        return 3
    if mode in ("hang", "spawn-hang"):
        if mode == "spawn-hang":
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)"])
            with open(spec["pid_file"], "w") as file:
                file.write(f"{os.getpid()} {child.pid}\n")
        time.sleep(3600)
    if mode == "noisy":
        sys.stderr.write("synthetic noise line\n" * ((8 << 20) // 21))
        sys.stderr.flush()
    if mode == "orphan-pipe":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)"], stdout=sys.stdout)
        with open(spec["pid_file"], "w") as file:
            file.write(f"{os.getpid()} {child.pid}\n")
    size = {"short": spec["bytes"] // 2, "long": spec["bytes"] + 1}.get(mode, spec["bytes"])
    fill = (spec["fill"] + (mode == "wrong")) % 256
    if sink is not None:
        output = sys.stdout.buffer if sink == "-" else open(sink, "wb")
        output.write(bytes([fill]) * size)
        output.flush()
    print(f"fixture: decoded {spec['frames']} frames", file=sys.stderr)
    if mode == "signal":
        os.kill(os.getpid(), signal.SIGKILL)
    return 1 if mode == "exit-after-output" else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
