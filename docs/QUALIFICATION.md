# Qualification record

This page records what was really checked. It holds no timing or throughput value.

## Scope

On 2026-11-27 one machine ran an implementation-validation smoke of the harness. It is
test evidence. It is not a benchmark result. Its timing data is not published, and no
number from it appears in this repository.

## Native macOS arm64 record

Host: macOS 27.2.1 (Darwin 27.2.0), Apple M2 Max, 12 cores (8 performance, 4 efficiency),
32 GiB memory. The harness checkout was clean at commit
`64ed5c1560b51302e41ffe814e4534f8dd306585`.

Lock digest: `sha256:68693cfc1676440dd5d54a1cae06a4b2232d329f3e60993ed51bcf36f4efcaa3`.
Suite `quick` (six 1920x1080 streams, three 8-bit and three 10-bit). Role `arm64`,
requested threads 1 and 4, 1 warm-up, 2 repetitions.

| Decoder | Commit | Identity, build origin | Build id | Binary SHA-256 |
| --- | --- | --- | --- | --- |
| Splot | `136ac63cde8777270e6a5a1df170a9d29f0944c2` | development, source-development | `bld-e81a15433bf5182a` | `69b5c79067701382c423891e274addeb7f34481aa8fad45268d1aac1360d1845` |
| dav2d | `33b0d3d234c6f7b6b64938ac7b93bc6e6e53b19c` | development, source-development | `bld-8c08ef1795444f02` | `3b654b388143978e6cb2412a38c83c1b2614bb29bf2c255f63ccc225f56ccd08` |
| avmdec | `1cfda4fa4879ef2da411b02fe2f51597daab3451` | development, source-development | `bld-fea4e2bf742f40b4` | `b0539c454a60e721b07595aff157debd0c4364dc914e99b43a7bf64de0527c17` |

Toolchains: Splot with rustc 1.100.0 and cargo 1.100.0, target `aarch64-apple-darwin`.
dav2d with meson 1.12.1, ninja 1.13.2 and Apple clang 21.0.0. avmdec with cmake 4.5.1,
ninja 1.13.2, perl 5.34.1 and Apple clang 21.0.0. The dav2d and avmdec target is
`arm64-apple-darwin27.2.0`. All three are release builds with SIMD on, no LTO except
Splot (thin), no PGO. The build class is `verified`.

Sequence and outcome of each step:

| Step | Outcome |
| --- | --- |
| `bench lock` | Lock written, digest above. |
| `bench prepare` | Streams verified. Three decoders built. |
| `bench verify` | 36 of 36 checks `ok` (6 streams, 2 thread budgets, 3 decoders). |
| `bench doctor` | No `fail` line. |
| `bench run` | Run state `complete`, class `verified`. 108 of 108 attempts `ok`. |
| `bench bundle`, `bench validate` | The bundle is valid. |
| `bench assemble` (no `--official`) | Dataset origin `local`, `complete` false: the x86-64 role is absent. Coverage: 126 cells expected, 36 `ok`, 90 `not-run`. |
| `bench validate` of the dataset | Valid. |
| `bench publish` | Refused, as designed: the dataset is local and incomplete. |

Correctness cases: `artistic-concert-1920x1080-25fps-qp135-130f`,
`artistic-intro-1920x1080-2997fps-qp135-130f`,
`bigbuckbunnystudio1-1920x1080-60fps-10bit-420-020-0149-screen-qp135-130f`,
`debugging-1920x1080-30fps-8bit-420-screen-qp135-130f`,
`ritualdance-1920x1080-60fps-10bit-420-qp160-120f`,
`tunnelflag-1920x1080-60fps-10bit-420-qp160-73f`.

## Not verified

- Linux x86-64 and Linux arm64 native builds and runs
  ([issue 12](https://github.com/splotlabs/benchmarks/issues/12)).
- A virtual machine cannot be detected by the harness. Only Rosetta and emulation are.
- Film grain. The sequence headers of all corpus streams allow film grain. The correctness
  pass matches the reference output with grain processing on. That the timed discard modes
  do equal film-grain work rests on the source inspection in
  [METHODOLOGY.md](METHODOLOGY.md#inspected-source-facts), not on a measurement.
- An official publication end to end. Only tests with temporary directories exist.

## Software checks

CI runs the unit tests on Linux x86-64, Linux arm64 and macOS arm64, each with Python 3.11
and 3.14 (`.github/workflows/ci.yml`; see the checks of any pull request). The unit test
count changes and is not recorded here.

The documented two-host sequence runs with a synthetic plan and the synthetic fixture
decoder as a test: `tests/test_docs.py` (runbooks and reproduction guide) and
`tests/test_assemble.py` (assembly rules). These are synthetic data, never results.
