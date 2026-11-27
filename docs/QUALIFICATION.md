# Qualification record

This page records what was really checked. It holds no timing or throughput value.

## Scope

On 2026-11-27 one machine ran an implementation-validation smoke of the harness. It is
test evidence. It is not a benchmark result. Its timing data is not published, and no
number from it appears in this repository.

## Native macOS arm64 record

Host: one Apple silicon Mac, native macOS arm64 (no Rosetta, no virtual machine). The
record names no machine details, because this run is not a published result. The harness
checkout was clean at commit
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

## Linux hosted runners

On 2026-11-27 the manual workflow `recipe-smoke.yml` ran once on GitHub-hosted runners:
[run 46040527264](https://github.com/splotlabs/benchmarks/actions/runs/46040527264). Hosted runners are **virtual machines**. This is a recipe,
correctness and runner smoke. It is not a benchmark result: its timing data was discarded,
and the workflow uploads nothing but the lock.

| Role | Runner image | Architecture | OS and kernel (from `bench doctor`) |
| --- | --- | --- | --- |
| `x86-64` | `ubuntu-24.04` (image version 20261122.23) | x86_64, not emulated | Ubuntu 24.04.5 LTS, Linux 7.0.0-1018-aws |
| `arm64` | `ubuntu-24.04-arm` (image version 20261122.193) | aarch64, not emulated | Ubuntu 24.04.5 LTS, Linux 7.0.0-1020-azure |

The harness checkout was clean at commit `9c48f5fb711775ccb4f4fc855adec14ccde4170b`. One
lock, made in the same run, was validated on both roles:
`sha256:0867c40171cb1e76bbd61ea9d8d2db054ef4910df18b3e4fb0259e9e04fd9433` (campaign
`recipe-smoke-0867c40171cb`). Suite `quick` (six 1920x1080 streams), requested threads 1
and 4, no warm-up, 1 repetition. Rust 1.100.0 on both roles.

| Decoder | Commit | Role | Build id | Binary SHA-256 |
| --- | --- | --- | --- | --- |
| Splot | `e36d8b0030f765c7209c6622f450dd996ad12105` | `x86-64` | `bld-196e7284ead90ea4` | `c8dfb1d11645dbb22a2cae65c0616fc4d7991ab967df8835c206b4d65be0fb4f` |
| Splot | `e36d8b0030f765c7209c6622f450dd996ad12105` | `arm64` | `bld-b3c4c164bbed00c2` | `9a76e1108b6d747fbe0660a926281a1e395f01cd3053ec9674aaab5aaca879fc` |
| dav2d | `33b0d3d234c6f7b6b64938ac7b93bc6e6e53b19c` | `x86-64` | `bld-8e4d1f575ea7a268` | `15c4af71e4b9f8e0462b360ef6b785dd58ed9f7930160537cb4a58bbc9591b7f` |
| dav2d | `33b0d3d234c6f7b6b64938ac7b93bc6e6e53b19c` | `arm64` | `bld-d6da445350779548` | `8c240fd6e9165d26724119fb2f28f99d85434a7bbde69d497660e567e80c1ba6` |
| avmdec | `1cfda4fa4879ef2da411b02fe2f51597daab3451` | `x86-64` | `bld-6255b170003eeef9` | `05ab98408cdf06b5a743ec2c7578a0d49464349eb3343356168ebaecd97d3c48` |
| avmdec | `1cfda4fa4879ef2da411b02fe2f51597daab3451` | `arm64` | `bld-1312f77cccf0de06` | `a94e9df5a68a716810c8663b7f1098c1dd6b3d6417529dae64d5fc3bef7ee59c` |

All builds have identity `development` and class `verified`.

| Role | `bench verify` | Final `bench run` line |
| --- | --- | --- |
| `x86-64` | `checks: 36 ok, 0 incorrect, 0 unsupported, 0 timeout, 0 error` | `run x86-64-20261127T172418Z-befc74a9: complete, 36 correctness rows, 36 attempt rows` |
| `arm64` | `checks: 36 ok, 0 incorrect, 0 unsupported, 0 timeout, 0 error` | `run arm64-20261127T172911Z-04556256: complete, 36 correctness rows, 36 attempt rows` |

`bench doctor` had no `fail` line on either role. Every attempt was `ok`. The bundles
and the assembled datasets (origin `local`, incomplete, 36 `ok` cells each) were valid.
On the arm64 runner the CPU model is `null`: Linux on arm64 has no model name in
`/proc/cpuinfo`.

A second run repeated the smoke on `main` at `1bb9b8982e19ded320e237dda8ad6a36b46c48a3`:
[run 46046916203](https://github.com/splotlabs/benchmarks/actions/runs/46046916203). Both roles
validated the lock `sha256:25d2648f69084ce5d532c91f98fdd3bc1d5fd0b2b5f88a018532dc069a2344c1`
(Splot `768c1e05e87d5e98247f27f28856c6cf238040f4`, dav2d and avmdec at the same commits as
before). Each role again gave `checks: 36 ok, 0 incorrect, 0 unsupported, 0 timeout, 0 error`
and a complete run with 36 correctness rows and 36 attempt rows. The dav2d and avmdec build
ids equal those of the first run on each architecture.

## One lock on physical ARM hardware and on x86-64

The lock of the second hosted run (`sha256:25d2648f69084ce5d532c91f98fdd3bc1d5fd0b2b5f88a018532dc069a2344c1`)
also ran on physical hardware: the Apple silicon Mac of the first record ran its `arm64`
role on 2026-11-27, with the harness checked out clean at the lock commit
`1bb9b8982e19ded320e237dda8ad6a36b46c48a3`. `bench doctor` reported "clean checkout of the
lock commit" and no `fail` line. The run class was `verified`.

| Decoder | Commit | Build id | Binary SHA-256 |
| --- | --- | --- | --- |
| Splot | `768c1e05e87d5e98247f27f28856c6cf238040f4` | `bld-84e91c7be02ec67e` | `59274d18d8360b836559909e666781afc41b9069c0c4c52534cffc330a674533` |
| dav2d | `33b0d3d234c6f7b6b64938ac7b93bc6e6e53b19c` | `bld-8c08ef1795444f02` | `3b654b388143978e6cb2412a38c83c1b2614bb29bf2c255f63ccc225f56ccd08` |
| avmdec | `1cfda4fa4879ef2da411b02fe2f51597daab3451` | `bld-fea4e2bf742f40b4` | `b0539c454a60e721b07595aff157debd0c4364dc914e99b43a7bf64de0527c17` |

Outcome: `checks: 36 ok, 0 incorrect, 0 unsupported, 0 timeout, 0 error`;
`run arm64-20261127T191013Z-b4a98221: complete, 36 correctness rows, 36 attempt rows`; the
bundle and the local dataset (36 `ok` cells) were valid. The dav2d and avmdec build ids and
binary digests equal those of the first macOS record, which used another work directory.

So one frozen lock ran its `arm64` role on physical ARM hardware (macOS) and on a hosted
Linux arm64 virtual machine, and its `x86-64` role on a hosted Linux x86-64 virtual machine.
Each host built the decoders itself and needed no account or credentials for the harness.

## Not verified

- A physical Linux host. The Linux recipes, the correctness pass and a minimal runner
  smoke passed only on hosted Linux x86-64 and arm64 virtual machines (above). A run on
  real hardware is the owner's first campaign.
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
