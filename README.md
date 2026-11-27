# splot benchmarks

A reproducible benchmark harness for three AV2 decoders: Splot, dav2d and avmdec.
The harness is a small command-line tool, `bench`. It does not need a checkout of
the Splot repository or of the website.

The harness is implemented. **No benchmark results are published yet.** The first
official campaign is a later request of the owner. The work is tracked in the Epic
[splotlabs/splot#2046](https://github.com/splotlabs/splot/issues/2046).

## Prerequisites

- Python 3.11 or newer. The harness uses the standard library only; there is
  nothing to install.
- Git.

Python and Git are enough for `validate`, `assemble`, `publish` and the tests.
`bench prepare` also needs the decoder build tools: `cargo` with `rustup`, `meson`,
`ninja`, `cmake`, `perl`, a C compiler, and `nasm` on x86-64
([docs/RUNBOOK-FIRST-HOST.md](docs/RUNBOOK-FIRST-HOST.md#1-check-the-host)).

## Supported platforms

The harness targets Linux x86-64, Linux arm64 and macOS arm64, native only. What was
exercised:

- The unit tests run in CI on Linux x86-64, Linux arm64 and macOS arm64.
- The real decoder builds, the correctness pass and a short runner smoke ran natively
  only on macOS arm64 ([docs/QUALIFICATION.md](docs/QUALIFICATION.md)).
- The Linux build recipes are not verified on a native host yet
  ([issue 12](https://github.com/splotlabs/benchmarks/issues/12)).

Windows is not supported.

## Use

Run the commands from the repository root. This synopsis shows every command and
every option. The runbooks below give the commands in order.

```text
./bench --help            # commands and exit codes
./bench --version         # harness version and commit
./bench identity          # harness identity as JSON: commit, tree, dirty flag, content digest
./bench validate PATH... [--immutable-since REF]
                          # check JSON and JSONL files, bundles, dataset and published directories
./bench lock PLAN.toml -o LOCK.json          # resolve a campaign plan once into a lock
./bench matrix --lock LOCK [--role ROLE]     # list every expected cell
./bench prepare --lock LOCK --role ROLE --work W [--jobs N] [--offline]
                          # fetch and verify the corpus, fetch and build the pinned decoders
./bench verify --lock LOCK --role ROLE --work W [--case ID] [--implementation ID] [--threads N]
                          # untimed correctness pass of the prepared builds
./bench doctor --lock LOCK --role ROLE --work W [--machine-id ID]
                          # preflight checks; changes nothing
./bench run --lock LOCK --role ROLE --work W --machine-id ID [--resume RUN_ID]
                          # correctness pass and timed attempts into W/runs/<run_id>/
./bench bundle --work W --run RUN_ID -o FILE.tar.gz   # portable bundle of one run; no upload
./bench assemble --lock LOCK.json BUNDLE... -o OUT [--select ROLE=RUN_ID] [--official]
                          # check bundles and write OUT/datasets/<dataset_id>/
./bench publish DATASET_DIR [--to DIR] [--dry-run] [--promote] [--allow-local]
                          # copy a dataset into published/ for a pull request; see docs/PUBLISHING.md
```

The builds, the decoder adapters, the correctness pass and the timed runs are described in
[docs/METHODOLOGY.md](docs/METHODOLOGY.md).

The data formats and identities are described in [docs/FORMATS.md](docs/FORMATS.md).

Exit codes: 0 success, 1 the operation ran and the result is a failure,
2 usage error, 3 missing prerequisite or configuration, 130 interrupted.

## Documentation

| Document | For |
| --- | --- |
| [docs/RUNBOOK-FIRST-HOST.md](docs/RUNBOOK-FIRST-HOST.md) | The first machine of a two-machine campaign (normally arm64), with a prompt for an AI agent. |
| [docs/RUNBOOK-SECOND-HOST.md](docs/RUNBOOK-SECOND-HOST.md) | The second machine (normally x86-64): reuse the first lock, with a prompt for an AI agent. |
| [docs/RUNBOOK-ASSEMBLE-PUBLISH.md](docs/RUNBOOK-ASSEMBLE-PUBLISH.md) | Collect bundles, assemble, inspect and, on explicit request, publish. |
| [docs/REPRODUCE.md](docs/REPRODUCE.md) | Run and inspect a benchmark on one machine, without credentials. Results are `local`. |
| [docs/METHODOLOGY.md](docs/METHODOLOGY.md) | Builds, timing boundary, correctness, repetitions, aggregation and identities. |
| [docs/FORMATS.md](docs/FORMATS.md) | Data formats, identities and dataset rules. |
| [docs/PUBLISHING.md](docs/PUBLISHING.md) | How a dataset reaches `published/`. |
| [docs/QUALIFICATION.md](docs/QUALIFICATION.md) | What was really checked, and what was not. A record, not a result. |
| [docs/ENCODER-EXTENSION.md](docs/ENCODER-EXTENSION.md) | Design note for a future encoder workload. Not implemented. |

## Tests

```sh
python3 -m unittest discover -s tests
```

The tests need no network. CI runs them on Linux x86-64, Linux arm64 and macOS arm64
with Python 3.11 and 3.14.

## Directory ownership

| Path | Contents |
| --- | --- |
| `bench` | Entry script. |
| `splotbench/` | The harness package. `cli.py` registers one parser per command module. `schema.py` validates, `provenance.py` records source, release and binary identity, `bundle.py` writes and reads bundles, `assemble.py` checks bundles and builds datasets, `publish.py` copies a dataset into `published/` on explicit command. |
| `corpus/`, `campaigns/` | The pinned corpus manifest and suites, and the campaign plan template. `plan.py` makes locks and the matrix, `corpus.py` verifies the stream cache. |
| `splotbench/build.py`, `splotbench/adapters/`, `splotbench/verify.py` | Pinned decoder builds, one adapter per decoder (`splot`, `dav2d`, `avm`, `fixture`), the correctness pass (`bench verify`). |
| `splotbench/host.py`, `splotbench/run.py` | Host fingerprint, machine id and host lock; `bench doctor`, `bench run` (correctness pass, timed phase, resume) and `bench bundle`. |
| `published/` | Published official datasets and `index.json`, added only with `bench publish` through a pull request. Existing dataset directories never change. |
| `schemas/` | JSON Schema files: the source of truth for every data format. |
| `docs/` | Format documentation, the methodology (`METHODOLOGY.md`), the publishing procedure, the runbooks, the reproduction guide and the encoder design note. `tests/test_docs.py` keeps the commands in them true. |
| `tests/` | Unit tests and synthetic fixtures (`tests/fixtures/`). |
| `.github/workflows/` | CI: unit tests and the checks of committed published data. No benchmark runs, schedules or publication. |

Downloaded corpora, builds, raw runs, bundles and generated datasets do not go
into Git. `.gitignore` excludes them.

## Licence

The first-party material is source-available under
[PolyForm Noncommercial 1.0.0](LICENSE.md). It is not open source. The contribution
policy of SplotLabs is in
[splotlabs/.github CONTRIBUTING.md](https://github.com/splotlabs/.github/blob/main/CONTRIBUTING.md).
