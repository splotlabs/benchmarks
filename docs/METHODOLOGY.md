# Methodology `decode-cli-wall/1`

This page defines the builds, the decoder adapters and the correctness pass.
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
   commit. Any other checkout is refused; remove it and the next build fetches it again.
3. Build out of tree into `W/build/<id>/<commit>/` by the recipe of the adapter.
   The output of every step goes to `build.log`.
4. Check that the build did not change the source tree and that SIMD or assembly
   is on (dav2d: `config.h` has `HAVE_ASM 1`; avmdec: `avm_config.h` has
   `HAVE_NEON 1` on arm64, `HAVE_AVX2 1` on x86-64).
5. Write `build.json` (`splotbench.build/1`): toolchain versions, target, the
   recipe arguments, configuration, lock-file digests, binary digest, version
   output, allow-listed environment and `build_id`.

The policy:

- Release configuration of each project, assembly and SIMD on.
- x86-64: compilers target `x86-64-v3`. arm64: the toolchain default for the native target.
- No `-march=native`, no PGO, no patches.
- Compiler flags come from the recipe only. When `CFLAGS`, `CXXFLAGS`, `LDFLAGS`,
  `RUSTFLAGS`, `CARGO_ENCODED_RUSTFLAGS`, `CARGO_BUILD_RUSTFLAGS` or
  `CARGO_BUILD_TARGET` is set, the build stops with exit code 3.
- A recipe that cannot follow the policy on a platform stops with a message.
- A missing tool stops with exit code 3 and names the tool.
- `--jobs` limits the parallel jobs (default: half of the logical CPUs).

`configuration.arguments` lists the recipe steps joined by `&&`. The tokens
`<src>`, `<build>` and `<jobs>` stand for the checkout, the build directory and the
job count, so the build identity does not depend on the work directory or `--jobs`.
`build.log` has the exact executed commands.

A finished build is reused only when the recorded source snapshot and
configuration are unchanged and the binary digest still matches. A changed binary
is an error that names both digests. A build of another snapshot or configuration
is removed and built again. The reserved build origin `upstream-release-asset`
is refused ("not supported").

| Decoder | Recipe (arm64) | x86-64 addition | Binary |
| --- | --- | --- | --- |
| Splot | `cargo build --release -p splot-cli --locked --target-dir <build>`; toolchain from `rust-toolchain.toml`; release profile from `Cargo.toml` | `target-cpu=x86-64-v3` comes from `.cargo/config.toml`; the recipe fails when that line is missing | `release/splot` |
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
  logical CPUs (`crates/splot-parallel/src/thread_count.rs:39-44`).
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
   group, stderr to `W/verify/<build_id>/<case>-t<N>-<n>.log`.
4. Hash (SHA-256) and count the stream in the harness. The stream is not stored.
5. On the lock timeout: terminate the process group, wait 2 s, kill the group,
   reap. The group is killed after every check, so no descendant survives.
6. Append one `splotbench.correctness/1` row to `W/verify/<build_id>.jsonl`.
   Earlier rows stay; a new check appends.

| Outcome | Rule |
| --- | --- |
| `ok` | exit code 0, byte count equal to `raw_bytes`, digest equal to the reference |
| `incorrect` | exit code 0, but short output, a wrong byte count or a wrong digest (`detail` says which) |
| `unsupported` | the decoder says it cannot decode the stream, or the binary lacks a needed option |
| `timeout` | no exit within `timeout_s` |
| `error` | any other failure: other nonzero exit code, signal, or the decoder cannot start |

A zero exit code alone is never `ok`. The command prints one line per check and
exits 1 when a check is not `ok`.

## Fixture decoder

The `fixture` adapter runs `tests/fixture_decoder.py`, a synthetic child process
that plays a decoder. Its input selects the behaviour: correct bytes, wrong bytes,
short or long output, a nonzero exit, "unsupported", an unknown option, a hang,
and a hang with a descendant process. Only a synthetic lock may use it.
