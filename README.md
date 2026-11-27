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
```

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
| `splotbench/` | The harness package. `cli.py` registers one parser per command module. |
| `tests/` | Unit tests. All test data is synthetic. |
| `.github/workflows/` | CI: unit tests only. No benchmark runs, schedules or publication. |

Downloaded corpora, builds, raw runs, bundles and generated datasets do not go
into Git. `.gitignore` excludes them.

## Licence

The first-party material is source-available under
[PolyForm Noncommercial 1.0.0](LICENSE.md). It is not open source. Contributions are
accepted under the
[SplotLabs Contribution Terms](https://github.com/splotlabs/.github/blob/main/CONTRIBUTING.md).
