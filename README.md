# splot benchmarks

A reproducible benchmark harness for three AV2 decoders: Splot, dav2d and avmdec.
The harness is a small command-line tool, `bench`. It does not need a checkout of
the Splot repository or of the website.

This repository is new. The harness is not complete and **no benchmark results
exist yet**. The work is tracked in the Epic
[splotlabs/splot#2046](https://github.com/splotlabs/splot/issues/2046).

## Prerequisites

- Python 3.11 or newer. The harness uses the standard library only; there is
  nothing to install.
- Git.

## Supported platforms

- Linux x86-64, native.
- Linux arm64, native.
- macOS arm64, native.

Windows is not supported.

## Use

Run the commands from the repository root.

```sh
./bench --help            # commands and exit codes
./bench --version         # harness version and commit
./bench identity          # harness identity as JSON: commit, tree, dirty flag, content digest
./bench validate PATH...  # check JSON and JSONL files, bundles and dataset directories
./bench lock PLAN.toml -o LOCK.json          # resolve a campaign plan once into a lock
./bench matrix --lock LOCK [--role ROLE]     # list every expected cell
./bench prepare --lock LOCK --role ROLE --work W [--offline]   # fetch and verify the corpus
./bench assemble --lock LOCK.json BUNDLE... -o OUT [--select ROLE=RUN_ID] [--official]
                          # check bundles and write OUT/datasets/<dataset_id>/
./bench publish DATASET_DIR [--to DIR] [--dry-run] [--promote] [--allow-local]
                          # copy a dataset into published/ for a pull request; see docs/PUBLISHING.md
```

The data formats and identities are described in [docs/FORMATS.md](docs/FORMATS.md).

Exit codes: 0 success, 1 the operation ran and the result is a failure,
2 usage error, 3 missing prerequisite or configuration, 130 interrupted.

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
| `published/` | Published official datasets and `index.json`, added only with `bench publish` through a pull request. Existing dataset directories never change. |
| `schemas/` | JSON Schema files: the source of truth for every data format. |
| `docs/` | Format documentation and the publishing procedure. |
| `tests/` | Unit tests and synthetic fixtures (`tests/fixtures/`). |
| `.github/workflows/` | CI: unit tests and the checks of committed published data. No benchmark runs, schedules or publication. |

Downloaded corpora, builds, raw runs, bundles and generated datasets do not go
into Git. `.gitignore` excludes them.

## Licence

The first-party material is source-available under
[PolyForm Noncommercial 1.0.0](LICENSE.md). It is not open source. The contribution
policy of SplotLabs is in
[splotlabs/.github CONTRIBUTING.md](https://github.com/splotlabs/.github/blob/main/CONTRIBUTING.md).
