# Data formats

The JSON Schema files in [`schemas/`](../schemas/) are the source of truth.
This page explains them. If this page and a schema differ, the schema is correct.

All data files are JSON or JSON Lines, UTF-8, with LF line ends. Every document
has a `schema` field `splotbench.<kind>/<major>`. `bench validate` selects the
schema by this field. An unknown kind, an unknown major version and an unknown
`workload.kind` are errors with their own messages. NaN, infinite numbers and
duplicate keys are refused when a file is loaded.

## Identities

Each identity has one meaning. Do not use one in place of another.

| Name | Definition |
| --- | --- |
| schema id | `splotbench.<kind>/<major>`, per document |
| methodology | `decode-cli-wall/1` |
| aggregation version | `summary/1` |
| lock digest | `sha256:` + sha256 of the canonical JSON of the `lock` object. The digest is outside the hashed object. |
| campaign_id | `<plan name>-<first 12 hex of the lock digest>` |
| run_id | `<role>-<UTC yyyymmddThhmmssZ>-<8 random hex>`, one per host run, kept on resume |
| attempt_id | `<run_id>#<sequence number>`, never reused |
| build_id | `bld-` + first 16 hex of sha256 of the canonical JSON of `{implementation, commit, snapshot_sha256, configuration, binary_sha256, libraries}` (`libraries` is the sorted list of library sha256 values) |
| machine_id | chosen by the owner, `[a-z0-9][a-z0-9-]{1,40}`; never a MAC address or a serial number |
| fingerprint_digest | `sha256:` of the canonical JSON of the observed host fingerprint; separate from machine_id |
| dataset_id | `ds-` + first 16 hex of sha256 of the canonical JSON of `{lock digest, sorted run ids with bundle digests, aggregation version, origin, sorted excluded run ids with bundle digests, snapshot_sha256 of the assembling harness}` |
| series_id | `ser-` + 16 hex of a digest of `{corpus identity, methodology, build policy, suite, measurement settings, machine_id, fingerprint digest}` |

Canonical JSON is `json.dumps(value, sort_keys=True, separators=(",", ":"),
ensure_ascii=False)` encoded as UTF-8. Digested objects contain no floats.
Digest strings are `sha256:<64 hex>`.

## Files

| Schema | File | Purpose and key fields |
| --- | --- | --- |
| `splotbench.lock/1` | `LOCK.json`, `lock.json` | Every execution-relevant value, resolved once: `harness` snapshot, `implementations` (commit, identity, release), `corpus`, `suite.cases` with reference digests, `roles`, `measurement`. |
| `splotbench.build/1` | `W/build/<id>/<commit>/build.json`, `builds.json` (an array) | One build: `build_id`, `class`, `source` (repository, commit, identity, release, `build_origin`, snapshot, lock files), `binary_path`, `binary_sha256`, `libraries`, `version_output`, `target`, `toolchain`, `configuration`, `environment` (allow-listed variables only). |
| `splotbench.host/1` | `host.json` | `machine_id`, `fingerprint`, `fingerprint_digest`. |
| `splotbench.correctness/1` | `W/verify/<build_id>.jsonl`, `correctness.jsonl` | One untimed check per build, case and thread budget: `outcome`, output and reference byte counts and digests. |
| `splotbench.attempt/1` | `attempts.jsonl` | One raw timed process run: ids, `phase`, `round`, `outcome`, and the decode payload with `frames` and typed `metrics`. |
| `splotbench.run/1` | `run.json` | One host run and the manifest of its bundle: `run_id`, `campaign_id`, `lock_digest`, `role`, `machine_id`, `state`, `class`, `harness`, `fingerprint_digest`, `build_ids`. |
| `splotbench.dataset/1` | `manifest.json` | One assembled dataset: ids, `origin`, `complete`, `runs` with bundle digests, optional `excluded_runs`, `coverage` (with `status_counts`), `files`. |
| `splotbench.summary/1` | `summary.json` | Everything the website shows: methodology, machines, implementations, builds, cases, `cells`, `speedups`, `aggregates`, downloads. |
| `splotbench.index/1` | `index.json` | Published datasets and `latest_complete`. |

Paths in manifests are relative: to the work directory `W` in build manifests,
and to the containing file in datasets and summaries.

A bundle is a `tar.gz` of a run directory with `run.json`, `lock.json`,
`host.json`, `builds.json`, `attempts.jsonl`, `correctness.jsonl`, optional
`logs/` and `SHA256SUMS` (sha256sum format). Entries are regular files only,
sorted, with mtime 0, uid and gid 0, no owner names and mode 0644. The gzip
header has no name and mtime 0. The bundle digest is the sha256 of the file.
A reader refuses absolute paths, `..`, links, devices, directories, entries
missing from `SHA256SUMS`, digest mismatches and members that fail their schema.

## Source, release and build provenance

`snapshot` describes a source directory: `vcs`, `commit`, `tree`, `dirty` and
`snapshot_sha256`, the sha256 of sorted lines `<mode> sha256:<hex> <path>` for
every tracked and untracked, not ignored, file (working-tree content), and
`submodules`. A directory without Git has `vcs: "none"` and is never clean.

Release rules:

1. At lock time, `provenance.resolve` resolves `select` (a full commit, a tag
   or a branch) with `git ls-remote` against the canonical upstream. An annotated tag resolves to
   its peeled commit.
2. `release` is `{tag, version, url}` only when an upstream tag that fully
   matches `release_tag_pattern` peels to exactly the resolved commit. The
   pattern group `version` gives the version. Otherwise `release` is `null` and
   `identity` is `development`.
3. A local tag, the nearest tag, a branch name or `--version` output is never
   release proof. `version_output` is stored as raw text only.
4. The lock keeps the resolved commit. Later steps fetch by commit. A tag that
   moves upstream later does not change a lock.

`build_origin` is `source-release` only for clean source at a verified release
commit, else `source-development`. `source-release` needs a release object.
`upstream-release-asset` is a valid, reserved value: the formats accept it, and
this harness has no path that produces it. A local build is never an upstream
binary.

Traceability is not determinism. The records identify the exact source, build
inputs and the executable that ran. They do not promise that a compiler gives
byte-identical output. Two builds of one source, for example arm64 and x86-64,
have different binary digests and different build ids, and both are valid.

`provenance.check_binary` hashes the executable and libraries again. A changed
file fails the check, and its build needs a new build id.

## Classes and origins

| Field | Values |
| --- | --- |
| `class` of a build, harness or run | `verified`: Git, clean, commit equals the lock. `development`: anything else; the real snapshot is kept. `synthetic`: from a synthetic lock. |
| `identity` of an implementation | `release` with a verified `release` object, else `development`. |
| `origin` of a lock | `standard` or `synthetic`. Only a synthetic lock may use the `fixture` adapter. |
| `origin` of a dataset | `official` (complete `verified` runs of every role, not synthetic), `local` or `synthetic`. |

Official data contains no `development` or `synthetic` record. Synthetic data
is marked in every file made from it.

## Metrics

| Name | Unit | Better | Notes |
| --- | --- | --- | --- |
| `wall_time` | ns | lower | process-wall timing boundary |
| `throughput` | frames/s | higher | derived in the summary |
| `cpu_time` | ns | lower | from `os.wait4` |
| `peak_rss` | bytes | lower | from `os.wait4`, normalised to bytes |
| `first_frame_latency` | ns | lower | defined, not measured: always `null` |

A value that was not measured or is not known is `null`, never 0. A measured 0
is data. These names belong to the `decode` workload. A future `encode`
workload gets its own payload and metric names; decode fields keep their names.

## Assembling a dataset

`bench assemble --lock L BUNDLE... -o OUT [--select ROLE=RUN_ID] [--official]`
writes `OUT/datasets/<dataset_id>/`:

```
manifest.json  summary.json  summary.csv  lock.json  SHA256SUMS
bundles/<run_id>.tar.gz     byte copies of the input bundles
```

The files are a pure function of the lock file, the selected bundles, the origin and the
aggregation version. No current time enters them; times come from run data. The same
inputs give the same bytes. A bundle is accepted only when it passes the bundle checks,
its `lock.json` has the digest of the given lock, `run.json` names the campaign and lock
digest, the role is in the lock, the host architecture is the role's `arch`, every build
is a build of a lock implementation at the lock commit, and every row names a known run,
build, case, thread budget and implementation. Attempt ids and sequence numbers are
unique. Binary digests may differ between roles. A synthetic lock needs `synthetic` runs and builds; a standard lock allows none. The runs that feed one dataset agree on the source commit and snapshot digest of each implementation; a difference means modified source and is refused. A run without a build of an implementation has `not-run` cells for it.

Run selection: the same run id with the same bytes is imported once. The same run id with
other bytes is a conflict. Two different runs for one role need `--select ROLE=RUN_ID`.
Runs that are not selected stay in `bundles/` and in `excluded_runs` of the manifest; they
feed no cell. The harness never picks a run by speed or time.

`aggregation.harness` is the identity of the harness checkout that runs `assemble`: repository,
commit, `snapshot_sha256`, and class `verified` for a clean Git checkout at a commit, else
`development` (`synthetic` for a synthetic lock). Its `snapshot_sha256` is part of the
`dataset_id`, so changed aggregation code never reuses an id. `reproduce.harness_commit` is the
harness commit of the lock, the commit that collected the data. `complete`
is true when every role of the lock has a selected run, every selected run has state
`complete`, and no cell is `not-run` or `interrupted`. A failed cell does not make a dataset
incomplete. Origin is `synthetic` for a synthetic lock, `official` with `--official`, else
`local`. `--official` needs every role, every run `complete` and class `verified`, a `verified` assembling harness, `verified` builds, a build of every implementation in every run, no
`not-run` or `interrupted` cell and a standard lock; else it exits 1, lists the reasons and
writes nothing. An existing dataset directory with other content is refused; with the same
content the command changes nothing. `manifest.coverage.status_counts` counts the whole
expected matrix; the cells of a role without a run count as `not-run` there. `summary.json`
has cells only for machines that have a run.

### Cell status

A cell is (machine, case, threads, implementation). The runner writes attempt rows only for cells whose correctness row is `ok`; for every other cell the correctness row is the explicit outcome, and `assemble` rejects a bundle with attempts for such a cell.

Rules, first match wins:

1. No correctness row: `not-run` when the run is `complete`, else `interrupted`.
2. The correctness outcome is not `ok`: that outcome (`incorrect`, `unsupported`, `timeout`, `error`).
3. Look at all attempts of the cell, warm-ups included. Any attempt with outcome `incorrect` makes the cell `incorrect`; else any `timeout` makes it `timeout`; else any `error`: `error`; else any `unsupported`: `unsupported`.
4. Attempts with outcome `interrupted` are ignored when the cell has the number of `ok` warm-up attempts (`warmups`) and `ok` measured attempts (`repetitions`) that the lock requires; a resumed run repeats an interrupted attempt under a new sequence number. If the counts are not full, the cell is `interrupted` in a run that is not `complete`, and `error` in a `complete` run.
5. Otherwise `ok`.

More `ok` attempts than the lock requires, an `ok` attempt whose `frames.expected` differs from the lock, or whose `frames.reported` is neither `null` nor equal to it, is an inconsistent bundle and is refused. A short decode must be recorded with outcome `incorrect`.

Only `ok` cells have numbers. `n_measured` counts measured attempts, `n_valid` those with outcome `ok`. Statistics use the `ok` measured attempts only.

### Statistics (`summary/1`)

All values come from the measured attempts of an `ok` cell; warm-ups never count. Nothing is rounded.

- `fps` of an attempt = frames / (wall_time / 1e9), with the frame count of the lock case. All medians, `fps_mad` and the `wall_ns_median` are floats.
- `wall_ns_median`, `fps_median`: the median (the mean of the two middle values for an even count).
- `fps_min`, `fps_max`; `fps_mad`: the median of |fps - `fps_median`|.
- `noisy`: (max wall - min wall) * 1000 > `noise_max_range_permille` * median wall.
- `cpu_ns_median`, `peak_rss_bytes_median`: the median, or `null` when any attempt lacks the value.
- `scaling_vs_1t`: `fps_median` over the `fps_median` of the 1-thread cell of the same machine, case and implementation; `null` without an `ok` 1-thread cell.
- `speedups`: for every ordered pair of implementations and every machine, case and thread budget where both cells are `ok`: `fps_median` of the first over the second.
- `aggregates`: per machine, threads, bit depth and ordered pair, the geometric mean of the per-case speedups (`null` when no case matches). `n_expected` is the number of cases of that bit depth, `n_matched` the cases where both cells are `ok`. `excluded` lists the other cases with the reason `<implementation>: <status>` for each implementation whose cell is not `ok`.

## How a result resolves its identity

A summary cell names `machine_id`, `case_id`, `implementation` and `build_id`.
Each id resolves inside the same summary, without a moving branch:

- `build_id` gives the build: binary digest, libraries, target, toolchain and
  configuration.
- `implementation` gives the upstream repository, the full commit, the release
  or `null`, the build origin and the source snapshot digest.
- `machine_id` gives the run id, the fingerprint and its digest, and the bundle
  with its digest. The bundle holds the raw rows, the host and build manifests
  and the lock.
- `case_id` gives the input digest; `corpus` gives the corpus and oracle
  identity; `lock_digest` gives the lock; `aggregation.harness` gives the
  harness commit and snapshot.

The validator implements this keyword subset: `type`, `const`, `enum`,
`pattern` (the whole string must match), `minimum`, `maximum`, `items`, `minItems`, `properties`,
`required`, `additionalProperties`, `propertyNames`, `anyOf`, `$ref` and the
splotbench keyword `discriminator` (`{"property": "kind", "mapping": {...}}`).
A schema with any other keyword fails to load.
