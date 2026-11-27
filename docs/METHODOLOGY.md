# Methodology `decode-cli-wall/1`

This page defines the builds, the decoder adapters, the correctness pass and the timed runs.
The data formats are in [FORMATS.md](FORMATS.md).

## Builds: policy `optimized-source/1`

`build.ensure(lock, role, work)` prepares one build per implementation of the lock.
`bench prepare --lock L --role R --work W [--jobs N] [--offline]` calls it after
the corpus stage. A build never runs during timing. With `--offline` nothing is
fetched or built: a missing checkout or a missing finished build stops with exit code 3.

1. Fetch exactly the locked commit from the locked repository into
   `W/src/<id>/<commit>/` (`git fetch --depth 1 <repository> <commit>`). A branch
   or a tag is never resolved here.
2. Check with `provenance.snapshot` that the checkout is clean and at the locked
   commit, and that it has no file that Git ignores (`git status --porcelain --ignored`).
   Any other checkout is refused; remove it and the next build fetches it again.
3. Build out of tree into `W/build/<id>/<commit>/` by the recipe of the adapter.
   The output of every step goes to `build.log`.
4. Check that the build did not change the source tree (the same check as in
   step 2, also for ignored files) and that SIMD or assembly
   is on (dav2d: `config.h` has `HAVE_ASM 1`; avmdec: `avm_config.h` has
   `HAVE_NEON 1` on arm64, `HAVE_AVX2 1` on x86-64).
5. Write `build.json` (`splotbench.build/1`): toolchain versions, target, the
   recipe arguments, configuration, lock-file digests, binary digest, version
   output, the permitted environment variables that were passed, and `build_id`.

A synthetic lock uses only the `fixture` adapter, and a standard lock never uses
it. `plan.py` (lock time) and `build.py` (build time) call the same rule,
`adapters.check_origin`, because a lock file can come from another person.

The policy:

- Release configuration of each project, assembly and SIMD on.
- x86-64: compilers target `x86-64-v3`. arm64: the toolchain default for the native target.
- No `-march=native`, no PGO, no patches.
- Compiler flags come from the recipe only. When `CFLAGS`, `CXXFLAGS`, `LDFLAGS`,
  `RUSTFLAGS`, `CARGO_ENCODED_RUSTFLAGS`, `CARGO_BUILD_RUSTFLAGS` or
  `CARGO_BUILD_TARGET` is set, the build stops with exit code 3 (they would not
  reach the build anyway; see below).
- A recipe that cannot follow the policy on a platform stops with a message.
- A missing tool stops with exit code 3 and names the tool.
- `--jobs` limits the parallel jobs (default: half of the logical CPUs).

`configuration.arguments` lists the recipe steps joined by `&&`. The tokens
`<src>`, `<build>` and `<jobs>` stand for the checkout, the build directory and the
job count, so the build identity does not depend on the work directory or `--jobs`.
`build.log` has the exact executed commands.

A finished build is reused only when the recorded source snapshot and
configuration are unchanged, the binary digest still matches, and the manifest is
synthetic exactly when the lock is. A changed binary is an error that names both
digests. A build of another snapshot, configuration or class is removed and built
again. On reuse, the fields that come from the lock (`class`, `source.repository`,
`identity`, `release`, `build_origin`) are taken from the current lock and the
manifest is written again; they are not part of `build_id`. The reserved build
origin `upstream-release-asset` is refused ("not supported").

### What the harness isolates

- **Environment.** Every build step and toolchain probe runs with a new
  environment: `PATH`, `HOME`, `TMPDIR`, `SDKROOT`, `DEVELOPER_DIR`, `RUSTUP_HOME`
  and `CARGO_HOME` when they are set, the permitted variables `CC`, `CXX` and
  `MACOSX_DEPLOYMENT_TARGET`, and `LC_ALL=C`. Nothing else reaches the tools, so
  for example `CPPFLAGS`, `CARGO_PROFILE_RELEASE_*`, `CARGO_TARGET_<TRIPLE>_RUSTFLAGS`,
  `RUSTC_WRAPPER`, `RUSTUP_TOOLCHAIN`, `CMAKE_TOOLCHAIN_FILE`,
  `CMAKE_C_COMPILER_LAUNCHER`, `PKG_CONFIG_PATH`, `NASM_PATH` and `YASM_PATH`
  do not apply. `environment` in `build.json` lists exactly the permitted
  variables that were passed. Steps get no standard input.
- **Cargo configuration.** Cargo reads `.cargo/config.toml` and `.cargo/config` in
  every parent directory of the checkout and in the Cargo home (`CARGO_HOME`, else
  `~/.cargo`). When one of them has a table `build`, `target`, `profile`, `env`,
  `host`, `unstable` or `patch`, the build stops with exit code 3 and names the
  file: move it, or set `CARGO_HOME` to a directory without it. The checkout's own
  `.cargo/config.toml` is part of the source snapshot.
- **Rust toolchain.** A rustup directory override outranks `rust-toolchain.toml`.
  The build stops with exit code 3 when `rustc -V` does not show the pinned channel.
- **Meson.** Meson reads machine files only when `--native-file` or `--cross-file`
  is given; the recipe gives neither. Its environment inputs are removed (above).
- **CMake.** CMake reads presets only with `--preset`; the recipe gives none. A
  `CMakeUserPresets.json` in the checkout would make it unclean. Toolchain and
  launcher variables from the environment are removed (above).
- **Not isolated.** The tools on `PATH` (compiler, Meson, CMake, Ninja, nasm) are
  the host's. `build.json` records their versions; it does not pin them.

| Decoder | Recipe (arm64) | x86-64 addition | Binary |
| --- | --- | --- | --- |
| Splot | `cargo build --release -p splot-cli --locked --target-dir <build>`; toolchain from `rust-toolchain.toml`; release profile from `Cargo.toml` | `target-cpu=x86-64-v3` comes from `.cargo/config.toml`; the recipe parses that file and fails when the flag is missing | `release/splot` |
| dav2d | `meson setup --buildtype release -Ddefault_library=static -Denable_tools=true -Denable_tests=false -Denable_docs=false -Denable_asm=true -Dxxhash_muxer=disabled`, then `ninja tools/dav2d` | `-Dc_args=-march=x86-64-v3`; nasm | `tools/dav2d` |
| avmdec | `cmake -G Ninja -DCMAKE_BUILD_TYPE=Release -DCONFIG_AV2_ENCODER=0 -DENABLE_TESTS=0 -DENABLE_DOCS=0 -DENABLE_EXAMPLES=0 -DENABLE_TOOLS=0`, then `cmake --build --target avmdec` | `-DENABLE_NASM=1`, `-DCMAKE_C_FLAGS=-march=x86-64-v3`, `-DCMAKE_CXX_FLAGS=-march=x86-64-v3` | `avmdec` |

Native host paths that ran: **macOS arm64 only** (Apple clang, Rust 1.100.0).
The Linux x86-64 and Linux arm64 recipes are **not verified on a native host**.

## Output policy `decode-discard`

The timed work is the same for every decoder: decode the whole stream, apply
film grain to every shown picture, then discard each picture **without
serialising it**. The correctness pass runs a second command with the raw output
on stdout (`-o -`), a pipe that the harness hashes. The two commands of an adapter
differ only in the output step.

| Decoder | Verification command (raw output on the pipe) | Timed command (discard) | The timed command skips |
| --- | --- | --- | --- |
| Splot | `splot decode INPUT --output-format raw -o - --threads N` | `splot decode INPUT --output-format null --threads N` | raw serialisation of each picture and the writes |
| dav2d | `dav2d -q -i INPUT --muxer yuv -o - --threads N --filmgrain 1` | `dav2d -q -i INPUT --muxer null --threads N --filmgrain 1` | the yuv muxer: row copies through `fwrite` |
| avmdec | `avmdec --rawvideo --summary -o - --threads=N INPUT` | `avmdec --noblit --summary --threads=N INPUT` | the bit-depth conversion and the raw writer |

The raw output of all three is the visible area, planes Y, U, V, 1 byte per
sample at 8 bit and 2 bytes little endian above 8 bit. This is the reference
convention `avmdec-rawvideo-sha256`.

Each adapter checks the real binary before use: the help text must name every
option of both commands. A missing option gives the outcome `unsupported` with
the option names.

## Inspected source facts

Inspected at Splot `a6ce057f53a1`, dav2d `33b0d3d234c6`, AVM `1cfda4fa4879`.
Paths are relative to each repository. Check them again when a lock names other commits.

### Splot

- Film grain: `--skip-film-grain` is off by default
  (`crates/splot-cli/src/commands/decode.rs:96-98`). Each output path passes
  the grain of the shown frame to `write_display`, which applies it before it
  calls the writer (`crates/splot-decode/src/output/film_grain.rs:37-51`,
  `crates/splot-decode/src/pipeline/frame_lifecycle.rs:131-135`).
- Verification: `-o -` runs the raw writer on stdout
  (`decode.rs:741-745`, `decode.rs:847-848`, `output/raw.rs:19-47`).
- Timed mode: `--output-format null` runs `decode_discard_reader`
  (`decode.rs:720-725`). Its writer is a no-op after the grain is applied
  (`output/raw.rs:75-100`). This is decode + film grain with no serialisation.
- Why `null` and not `raw -o /dev/null`: at this commit both run the same
  `decode_discard_reader`, because the raw path special-cases the exact path
  `/dev/null` (`decode.rs:768-770`). `null` is the documented discard mode
  ("Decode all requested frames without producing an output artifact",
  `decode.rs:44-45`); the path comparison is an undocumented shortcut.
- Threads: `--threads N` starts N row-worker threads `splot-row-<i>`
  (`crates/splot-parallel/src/rows.rs:926-951`); the calling thread drives
  parsing and output and is not counted. `0` or `auto` means the number of
  logical CPUs: the parser maps `auto` to 0 (`crates/splot-parallel/src/error.rs:36-47`),
  and 0 becomes `Auto` (`thread_count.rs:27-32`, `thread_count.rs:56-61`), which
  resolves to the available parallelism (`thread_count.rs:39-44`).
- Frame delay: `--frame-delay auto` (default) is the worker count
  (`decode.rs:87-92`, `crates/splot-parallel/src/frame_delay.rs:43-48`).
- Frame count: no counter. `parse_frames` gives `null`.
- `--version` prints `splot 0.0.0` (the workspace version). It names no commit.

### dav2d

- Film grain: on by default (`src/lib.c:80`). The library applies it in
  `output_image` (`src/lib.c:448`, `src/lib.c:487`), which `dav2d_get_picture`
  calls before it returns the picture (`src/lib.c:556-567`). So every muxer
  receives grained pictures. Only the `md5` and `xxh3` muxers turn grain off
  (`tools/dav2d_cli_parse.c:543-548`); the adapters also pass `--filmgrain 1`.
- Verification: `--muxer yuv -o -` writes each visible row with `fwrite` to stdout
  (`tools/output/yuv.c:45-46`, `yuv.c:55-89`). `--muxer yuv` is required: without
  it, `-o /dev/null` selects the null muxer (`tools/output/output.c:95-96`).
- Timed mode: `--muxer null` needs no `-o` (`dav2d_cli_parse.c:552`). The CLI
  calls `output_write` for every picture that `dav2d_get_picture` returns
  (`tools/dav2d.c:330-348`, `dav2d.c:371-390`), and the null muxer only releases
  the picture (`tools/output/null.c:34-37`, `output.c:220-232`).
- `-q`: without it the CLI prints a status line after every frame
  (`tools/dav2d.c:355-356`, `dav2d.c:397-398`) and there is no final summary line
  (`dav2d.c:407-408` prints only a newline). A per-frame line during the timed run is not acceptable,
  so both commands pass `-q`, and dav2d has no frame counter: `parse_frames` gives `null`.
- Threads: `--threads N` sets `n_threads` (`dav2d_cli_parse.c:454-456`). With
  N > 1 the library starts N worker threads and the calling thread waits for
  pictures; with N = 1 it starts no worker and decodes on the calling thread
  (`src/lib.c:128-132`, `src/lib.c:272-285`).
- Frame delay: default 0 = auto, min(8, ceil(sqrt(N))) frames (`src/lib.c:130-132`).
- `--version` prints `git describe` output on stdout (`dav2d_cli_parse.c:495-496`).
  A shallow checkout has no tags, so it prints a short commit, for example `33b0d3d`.

### avmdec

- Film grain: the library applies it when avmdec fetches a frame with
  `avm_codec_get_frame` (`av2/av2_dx_iface.c:1254-1256` in `decoder_get_frame_`,
  `av2_dx_iface.c:1210`); only `--skip-film-grain` turns it off
  (`apps/avmdec.c:681`, `apps/avmdec.c:1037-1042`).
- Verification: the decoder returns 16-bit buffers for every stream
  (`av2/av2_iface_common.h:84`). For 8-bit streams avmdec converts each frame to
  8-bit before it writes (`apps/avmdec.c:1244-1252`, `common/rawenc.c:88-97`).
  `-o -` is stdout (`apps/avmdec.c:635-638`).
- Timed mode: `--noblit` (`apps/avmdec.c:78-79`, `apps/avmdec.c:792-793`) still
  fetches every frame from the decoder in the `avm_codec_get_frame` loop
  (`apps/avmdec.c:1139`) and counts it. It skips only the block that scales,
  converts and writes the frame (`apps/avmdec.c:1179-1374`) and opens no output
  file (`apps/avmdec.c:938`).
- Frame count: `--summary` prints `<in> decoded frames/<out> showed frames` once,
  at the end, on stderr (`apps/avmdec.c:391-396`, `apps/avmdec.c:1378-1381`).
  `parse_frames` returns the shown frames.
- Threads: `--threads=N` sets `max_threads` (`av2/av2_dx_iface.c:457`). The
  decoder starts N - 1 tile workers and the calling thread is the last worker
  (`av2/decoder/decodeframe.c:5287-5317`). Row multithreading is off by default
  (`av2/av2_dx_iface.c:111-112`), so more threads help only for streams with more
  than one tile (`decodeframe.c:9725-9730`).
- Frame delay: avmdec has no frame-parallel mode.
- Version: there is no version option. `--help` lists the decoder with its
  version from `git describe` (`apps/avmdec.c:282-288`). A shallow checkout gives a
  fallback version, for example `v1.0.0`.

### What version output proves

`version_output` is raw text for the details view. It is never a release proof
and never a label: the source commit, the snapshot and the binary digest identify
a build.

## Thread budgets: a limit of the comparison

The thread budget of the lock is the **requested threads**: the value passed to
the thread option of each decoder, without remapping. The decoders give that
value different meanings:

| Decoder | `--threads N` means | Reference |
| --- | --- | --- |
| Splot | N row-worker threads, plus the calling thread that drives parsing and output | `crates/splot-parallel/src/rows.rs:926-951` |
| dav2d | N worker threads (the calling thread waits); N = 1: no worker, decode on the calling thread | `src/lib.c:128-132`, `src/lib.c:272-285` |
| avmdec | N - 1 tile workers plus the calling thread; row multithreading off, so threads help only for multi-tile streams | `av2/decoder/decodeframe.c:5287-5317`, `av2/av2_dx_iface.c:111-112`, `decodeframe.c:9725-9730` |

So equal budgets do not mean equal thread counts or equal parallel work.
Results at one budget compare the decoders as they are called with that option,
not per thread.

## Frame count

`frames.expected` comes from the lock. `frames.reported` comes from
`parse_frames` of the timed run (avmdec only; `null` for Splot and dav2d). If a
reported count differs from the expected count, the attempt is `incorrect`. A
reported short decode is never replaced with the expected count. The correctness
pass counts frames as output bytes divided by the frame size.

## Correctness pass

`bench verify --lock L --role R --work W [--case ID] [--implementation ID] [--threads N]`
runs outside timing and needs no benchmark run. For each build, case and thread
budget of the role:

1. Check the binary digest against `build.json`. A changed binary stops the pass.
2. Check the options of the binary (see above).
3. Run the verification command with the raw output on a pipe, in its own process
   group, stderr to `W/verify/<build_id>/<case>-t<N>-<n>.log`. The check of the
   options and the `unsupported` messages read only the decoder output, not the
   command line that the harness writes at the top of the log.
4. Hash (SHA-256) and count the stream in the harness. The stream is not stored.
5. On the lock timeout: terminate the process group, wait 2 s, kill the group,
   reap. When the decoder exits but the pipe is still open 1 s later, a descendant
   holds it: terminate the group and record `error` (the output is not
   trustworthy). The group is killed after every check, so no descendant survives.
6. Append one `splotbench.correctness/1` row to `W/verify/<build_id>.jsonl`.
   Earlier rows stay; a new check appends.

| Outcome | Rule |
| --- | --- |
| `ok` | exit code 0, byte count equal to `raw_bytes`, digest equal to the reference |
| `incorrect` | exit code 0, but short output, a wrong byte count or a wrong digest (`detail` says which) |
| `unsupported` | the decoder says it cannot decode the stream, or its help text lacks a needed option |
| `timeout` | no exit within `timeout_s` |
| `error` | any other failure: other nonzero exit code (also with complete, correct output), a signal, a descendant that keeps the output open, a help command that cannot run, or a decoder that cannot start |

A zero exit code alone is never `ok`. The command prints one line per check and
exits 1 when a check is not `ok`. The last line counts the checks per outcome:
`checks: <n> ok, <n> incorrect, <n> unsupported, <n> timeout, <n> error`.

## Host checks: `bench doctor`

`bench doctor --lock L --role R --work W [--machine-id ID]` changes nothing. It
prints one line per check, `ok`, `warn` or `fail`, then the host fingerprint, and
exits 1 when a check fails. `bench run` runs the same checks (except the host lock,
which it holds) and stops at the first failure.

| Check | `fail` when | `warn` when |
| --- | --- | --- |
| lock, role | the lock is invalid or has no such role | |
| architecture | the role `arch` is not the native architecture, or Python runs under emulation (Rosetta: `sysctl.proc_translated`; an interpreter of another architecture than the kernel) | |
| machine id | the id does not match `[a-z0-9][a-z0-9-]{1,40}` | no id is given |
| harness | | the checkout is not clean at `lock.harness` commit: results are `development`, no official run |
| tools | | a build tool of a recipe is not on `PATH` (only `bench prepare` needs them) |
| corpus | a stream is missing or has another size or SHA-256 (exit 3) | |
| builds | a build is missing (exit 3) or its binary digest changed; on `ok`, one line per build follows: `  build <implementation> <commit> <release or development> <build_id> <binary sha256>` | |
| correctness | | a cell has no correctness row for the current builds and this lock's reference, or a row is not `ok` |
| disk | less than 1 GiB is free in `W` | |
| cpus | | the logical CPUs are fewer than the largest thread budget |
| host lock | another process holds `W/host.lock` | |

## Host settings: recorded, never changed

The harness reads host facts; it never changes a system setting. The fingerprint
(`host.json`) holds: architecture, OS name, version and kernel, CPU model,
physical and logical cores, performance and efficiency cores (macOS), ISA features
(macOS `hw.optional.*`, Linux `flags` or `Features` of `/proc/cpuinfo`), memory,
the Python version, and:

- `power`: macOS: the power source (`pmset -g batt`) and `powermode` or
  `lowpowermode` (`pmset -g`). Linux: the cpufreq governors of all CPUs.
- `affinity`: Linux: the allowed CPU set of the harness (`sched_getaffinity`).
  macOS has no affinity setting: `null`.

A fact that the OS does not give without privilege is `null`. The fingerprint
holds no host name, user name or path. Its digest is part of the run identity: a
change of power source or governor makes a new fingerprint, so a run cannot resume.

`W/host.lock` allows one harness invocation at a time: `bench run`, `bench prepare`
and `bench verify` take it. It is an OS file lock (`flock`). `bench run` and
`bench verify` pass its descriptor to every decoder, so the kernel releases it only
when the harness and every decoder that holds it have ended. A second invocation
exits 1 and names the holder: the harness process, or, when that process has
ended, a decoder that it left running.

## Timed phase: `bench run`

`bench run --lock L --role R --work W --machine-id ID [--resume RUN_ID]` executes
one role on one host:

1. Take the host lock, then run the doctor checks. Nothing is downloaded or built:
   a missing stream or build exits 3 and names `bench prepare`.
2. Correctness pass: each cell without a correctness row for the current builds is
   checked with `verify.run` (see above). The run keeps one row per cell in
   `correctness.jsonl`: the latest row of `W/verify/<build_id>.jsonl` whose
   `reference_sha256` and `reference_bytes` equal the case of this lock
   (`verify.matches_case`). A row checked against another reference is never
   reused, and `bench assemble` refuses a bundle with such a row.
3. Check every binary digest against its build manifest again.
4. Timed phase, one process at a time, in the order below. Only cells whose
   correctness outcome is `ok` get attempts; for every other cell the correctness
   row is the explicit outcome. Each attempt appends one `splotbench.attempt/1` row
   to `attempts.jsonl` with fsync before the next attempt starts.

Run class: `synthetic` for a synthetic lock, `verified` when the harness and all
builds are `verified`, else `development`. `run.json` has state `in-progress`
during the run, `complete` at the end and `interrupted` after SIGINT or SIGTERM.

### Order: policy `seeded-rotation/1`

With the lock seed `s`, and `rank(text)` the SHA-256 hex digest of the UTF-8 text:

1. Cases sort by `rank("<s>/case/<case id>")`.
2. For each case, the thread budgets ascend.
3. For each case and budget, the rounds are the `warmups` warm-up rounds, then the
   `repetitions` measured rounds.
4. The base order of the implementations sorts by
   `rank("<s>/<case id>/<threads>/<implementation id>")`. Round `k`, counted from 0
   over both phases, starts at position `k mod n` of the base order and wraps.

The harness reads the input of a case once before its first attempt, so every
attempt finds it in the page cache. No build, download or hash runs during the
timed phase.

### Timing boundary `process-wall`

The child runs in its own session and process group, with stdin from `/dev/null`
and stdout and stderr to `logs/<sequence>-<implementation>-<case>-t<threads>.log`.
The log is created exclusively, never truncated. Before the child starts, the
harness writes and fsyncs two lines: the start record `# attempt <row>` (the row
of this attempt with outcome `interrupted` and `null` metrics) and the exact
command, with paths relative to `W` (the working directory of the child). The
timeout timer starts before the clock. The wall time is the monotonic clock from
immediately before process creation to the return of `os.wait4`. It includes process start,
file read, demux, decode and film grain. CPU time (user + system) and peak RSS
come from `os.wait4`; `ru_maxrss` is normalised to bytes (bytes on macOS, KiB on
Linux). After the child exits, the harness kills the members left in its process
group (only when there are any) and waits until no member is left. Nothing polls
the child during the attempt. Then each log is capped at 1 MiB: the head, one line
`# harness: N bytes removed here; the log is capped at 1048576 bytes`, and the tail.
Correctness logs get the same cap.

Measured: `wall_time`, `cpu_time`, `peak_rss` of the decoder process. Not measured:
`first_frame_latency` is always `null`; the thread count that a decoder really
starts is not observed (the row records the requested threads).

### Attempt outcomes

| Outcome | Rule | Metrics |
| --- | --- | --- |
| `ok` | exit code 0, and the reported frame count is `null` or equal to the lock | measured |
| `incorrect` | exit code 0, but the decoder reports another frame count than the lock | measured |
| `error` | nonzero exit code, a signal, the decoder cannot start, or a decoder whose timed mode always prints a frame count (`TIMED_REPORTS_FRAMES`: avmdec) printed none; the log ends with a `# harness:` line that says why | measured (`null` when it did not start) |
| `timeout` | no exit within `timeout_s`: terminate the group, wait 2 s, kill the group, reap | `null` |
| `interrupted` | SIGINT or SIGTERM during the attempt: the same stop sequence | `null` |

An `incorrect`, `error` or `timeout` attempt settles its cell: the cell gets no
further attempts, and the assembler shows that status. Warm-ups are attempts like
the others; they never enter the statistics.

Noise flag: the assembler flags a cell `noisy` when (max − min) × 1000 of its
measured wall times is above `noise_max_range_permille` × the median. Nothing is
dropped, and the runner never repeats an attempt because it is slow.

### Interruption and resume

SIGINT, SIGTERM or SIGHUP stops the child group, appends the attempt with outcome
`interrupted`, writes state `interrupted`, releases the host lock and exits 130.
During the correctness pass the current check stops at once and its process group
is killed. Elsewhere the signal takes effect at the next safe point: the run
directory is created under a temporary name and renamed when complete, and a row or
state file is never half written. The message says how many correctness and
attempt rows the run holds; before the run directory exists it says that no run
directory was created and no decoder was started.

A crash of the harness leaves state `in-progress`; resume treats it as interrupted.
A decoder that runs on keeps the host lock until it ends. At resume, every attempt
with a start record in its log but no row gets that recorded row (`interrupted`,
`null` metrics), and the next attempt takes the next free sequence number: a
sequence number or a log is never used twice. A last line of `attempts.jsonl` or
`correctness.jsonl` without its newline is not a row: resume moves it to
`logs/<file>-partial-<n>.txt`, says so and continues.

`--resume RUN_ID` continues under the same run id only when the lock digest, the
role, the machine id, the fingerprint digest, the harness identity, the build ids
and the build manifests (with their binary digests) are unchanged. Otherwise it
refuses and asks for a new run. A complete run cannot resume. Resume never rewrites
or deletes a row: it computes the remaining work from the rows present. An attempt
slot (implementation, case, threads, phase, round) is done when it has an `ok` row;
an interrupted slot runs again under a new sequence number. A replaced executable
fails the binary check, so it refuses both a resume and a new timed phase.

### Bundle

`bench bundle --work W --run RUN_ID -o FILE.tar.gz` packs the run directory with
the deterministic bundle writer (see FORMATS.md), for complete and interrupted runs;
`run.json` holds the state. `bench run` prints this command at the end. It never
bundles or uploads by itself. The bundle holds the lock, the host and build
manifests, the correctness and attempt rows and the logs; stream bytes stay out,
the lock keeps their URLs and digests. Records hold relative paths only.

## Fixture decoder

The `fixture` adapter runs `tests/fixture_decoder.py`, a synthetic child process
that plays a decoder. Its input selects the behaviour: correct bytes, wrong bytes,
short or long output, a nonzero exit, "unsupported", an unknown option, a hang,
and a hang with a descendant process. The `timed` overrides of its input apply to
the timed command only (once, with a `marker` file), so a test can pass the
correctness pass and then fail, hang or report another frame count in the timed
phase. Only a synthetic lock may use it.
