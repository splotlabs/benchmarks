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
| dataset_id | `ds-` + first 16 hex of sha256 of the canonical JSON of `{lock digest, sorted run ids with bundle digests, aggregation version, origin}` |
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
| `splotbench.dataset/1` | `manifest.json` | One assembled dataset: ids, `origin`, `complete`, `runs` with bundle digests, `coverage`, `files`. |
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
