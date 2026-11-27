# Runbook: assemble, inspect and publish

Use this runbook after one or more hosts made bundles. It has two parts. Assembly is
local and changes nothing outside `<out>`. Publication is a separate, explicit, manual
step that the owner asks for. No measurement, assembly or validation step publishes data.

## Prompt for an AI agent

Copy this text. Fill in the values in angle brackets.

```text
Follow docs/RUNBOOK-ASSEMBLE-PUBLISH.md in a checkout of splotlabs/benchmarks.
Inputs: lock = <lock path>; bundles = <bundle paths, one per role>; output directory = <out>.
Do part 1 (assemble and inspect). Report the result.
Do part 2 (publish) only if I wrote "publish now" in this task.
Never change a lock or a bundle. Never merge the pull request.
```

## Placeholders

| Placeholder | Meaning |
| --- | --- |
| `<lock>` | The campaign lock, the exact file that all hosts used. |
| `<bundle-a>`, `<bundle-b>` | Bundles of two roles, as ordinary files. |
| `<out>` | A new output directory. Datasets go to `<out>/datasets/<dataset-id>/`. |
| `<dataset-id>` | The dataset id, `ds-` and 16 hex characters. `bench assemble` prints the dataset path. |
| `<role>`, `<run-id>` | A role of the lock and a run id (see [RUNBOOK-FIRST-HOST.md](RUNBOOK-FIRST-HOST.md#placeholders)). |

All commands run from the root of the harness checkout. For `--official` and for
publication the checkout must be clean (see step 2).

## Part 1: assemble and inspect

### 1. Collect and validate the bundles

Copy the bundles to this machine as ordinary files (`scp`, a USB drive). Nothing else
transfers state: the lock file and the bundles are the whole input. Validate each one:

```sh
./bench validate <bundle-a> <bundle-b>
./bench validate <lock>
```

`bench validate` checks the schemas, the checksums and the paths inside each bundle.
A bundle that fails is not assembled. Ask for it again.

### 2. Assemble for inspection

Start without `--official`. This accepts incomplete data and gives a dataset of origin
`local`. The dataset is not publishable by default.

```sh
./bench assemble --lock <lock> <bundle-a> <bundle-b> -o <out>
```

The command checks every bundle against the lock: the lock digest, the role and its
architecture, the build commits, and every row. If it refuses, it prints every reason
and writes nothing. It prints `<out>/datasets/<dataset-id>`, the origin, `complete` or
`incomplete`, and the count of `ok` cells. Assembling the same inputs again gives the
same bytes and changes nothing.

Choose a run with `--select` when there is a conflict. Two bundles of one role (for
example a second run after an interruption) are an error until you choose one:

```sh
./bench assemble --lock <lock> <bundle-a> <bundle-b> <other-bundle> -o <out> --select <role>=<run-id>
```

Give `--select` once per role. A run that is not selected stays in `bundles/` and in
`excluded_runs` of the manifest. It feeds no cell. Never pick a run because it is fast.
Pick the complete run of the campaign, and say why in the report.

### 3. Inspect the dataset

```sh
./bench validate <out>/datasets/<dataset-id>
```

Open the files in `<out>/datasets/<dataset-id>/`:

- `manifest.json`: `origin`, `complete`, `runs`, `excluded_runs` and `coverage`.
  `coverage.cells_expected` counts the whole matrix of the lock. `coverage.cells_ok`
  and `coverage.status_counts` count the cells by status (`ok`, `incorrect`, `unsupported`,
  `timeout`, `error`, `interrupted`, `not-run`). The cells of a role without a run count as `not-run`.
- `complete` is true when every role of the lock has a selected run, every selected run
  has state `complete`, and no cell is `not-run` or `interrupted`. A failed cell
  (`incorrect`, `error`, ...) does not make a dataset incomplete. It stays visible.
- `summary.csv` and `summary.json`: cells, speedups and aggregates. An aggregate
  states `n_matched` and `n_expected`: the cases where both decoders are `ok`, and the
  cases of that bit depth. The excluded cases have a reason. See
  [METHODOLOGY.md](METHODOLOGY.md#matched-case-aggregation-and-coverage).

Report `origin`, `complete`, the `status_counts` and every excluded run or case. Do not
describe the speed of a decoder in your own words; quote the files.

### 4. Regenerate a summary from a dataset

A dataset keeps its lock and its bundles. To regenerate the summary from them (for
example with a newer harness), assemble again into a new directory:

```sh
./bench assemble --lock <out>/datasets/<dataset-id>/lock.json <out>/datasets/<dataset-id>/bundles/*.tar.gz -o <out2>
```

`<out2>` is another new output directory. The new dataset id differs when the assembling
harness differs, because the harness snapshot is part of the id. Add `--select` if the
dataset had excluded runs.

### 5. Assemble the official dataset (owner's request)

An official dataset needs: every role of the lock, every run `complete` and of class
`verified`, a clean checkout of the harness at a commit (class `verified`), `verified`
builds, a build of every implementation, no `not-run` or `interrupted` cell, and a
standard (not synthetic) lock. The command refuses otherwise, lists the reasons and writes
nothing.

```sh
git status --porcelain
./bench assemble --lock <lock> <bundle-a> <bundle-b> -o <out> --official
```

`git status` must print nothing. Use the same `--select` options as in step 2.

## Part 2: publish (only on the owner's explicit request)

Publication copies the dataset into `published/` of this repository. A pull request
publishes it. [PUBLISHING.md](PUBLISHING.md) describes the checks, the pull request
steps, `--promote` and the recovery. Do not repeat or skip it. The short form:

```sh
./bench publish <out>/datasets/<dataset-id> --dry-run
./bench publish <out>/datasets/<dataset-id>
```

Add `--promote` to the second command only when the owner wants this dataset to be the
default (`latest_complete`). Then make a branch, review `git diff --stat`, commit
`published/`, push, and open a pull request. The pull request is the publication. The
owner merges it. Do not merge it yourself.

## Recovery

- **A host bundle is missing.** Assemble without `--official` with the bundles you have.
  The dataset is incomplete. The missing role shows `not-run`. Do not publish it.
  Later, assemble again with all bundles into a new directory.
- **Conflicting runs.** `bench assemble` names the conflict. The same run id with other
  bytes is a conflict: ask which file is the true one, and never overwrite a bundle.
  Two runs of one role need `--select <role>=<run-id>`.
- **Bundle for another lock.** The command names both digests. Use the lock that the
  bundle was made with, or ask for a bundle of this campaign.
- **Wrong architecture.** A bundle of a role must come from a host of the role's `arch`.
  Run the role again on the right machine.
- **Interrupted publish.** Run the same `bench publish` command again. It completes or
  reports "already present". `./bench validate published` shows what is not listed.
  See [PUBLISHING.md](PUBLISHING.md#recovery).
- **Existing dataset directory with other content.** `bench assemble` refuses to
  overwrite it. Use another `<out>`.
