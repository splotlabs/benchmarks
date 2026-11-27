# Runbook: one machine, independent campaign

Use this runbook when each machine works alone. A machine runs its own campaign from a
plan in `campaigns/`, makes its own dataset and publishes it. No file moves between
machines. The Git repository is the only shared thing.

The plans of one campaign round pin the same decoder commits, so every machine measures
the same sources. Each machine has its own lock and its own dataset. The website lists
the datasets side by side; it never mixes machines in one view.

For one lock and one combined dataset for two machines, use
[RUNBOOK-FIRST-HOST.md](RUNBOOK-FIRST-HOST.md) and
[RUNBOOK-SECOND-HOST.md](RUNBOOK-SECOND-HOST.md) instead.

## Prompt for an AI agent

Copy this text. Fill in the values in angle brackets.

```text
You run one independent AV2 decoder benchmark campaign on this machine and publish it.

1. Clone https://github.com/splotlabs/benchmarks.git and check out commit
   <harness-commit> (a detached HEAD is correct).
2. Follow docs/RUNBOOK-SINGLE-HOST.md in that checkout, in order.

Inputs: plan = <plan>; role = <role>; machine id = <machine-id>;
work directory = <work> (outside the checkout).

Rules:
- Use a physical machine of the role's architecture. Do not use a virtual machine, a
  container with CPU limits, or emulation. Say so if you are not sure.
- Do not edit the plan, the corpus or any setting. Do not patch a decoder or a recipe.
  If a recipe fails, stop and report the command, its error and the end of the build log.
- measure now: when `bench prepare`, `bench verify` and `bench doctor` pass, run
  `bench run`. Keep the machine idle and on mains power during the run. If the run is
  interrupted, continue it with the `--resume` command that it prints.
- publish now: when the run is complete, do part 2 of the runbook: assemble with
  `--official`, publish with `--promote`, open the pull request, and merge it when all
  its checks are green.
When you finish, give the report that the runbook lists. If a step fails, stop and
report the exact command and its output.
```

## Placeholders

| Placeholder | Meaning |
| --- | --- |
| `<plan>` | A plan of this repository, for example `campaigns/2026-11-full-x86-64.toml`. Do not edit it. |
| `<lock>` | A new path for the lock file, outside the checkout. |
| `<role>` | The one role of the plan: `arm64` or `x86-64`. Its `arch` must be the native architecture. |
| `<machine-id>` | The owner's name for this machine: 2 to 41 characters of `a-z`, `0-9`, `-`. Never a serial number or a MAC address. Use the same name for later campaigns on this machine. |
| `<work>` | The work directory, outside the checkout. It holds streams, sources, builds and runs. |
| `<run-id>` | The run id that `bench run` prints. |
| `<bundle>` | A path for the bundle file, outside the checkout, for example `<work>/<run-id>.tar.gz`. |
| `<out>` | A new output directory for the dataset, outside the checkout. |
| `<dataset-id>` | The dataset id that `bench assemble` prints: `ds-` and 16 hex characters. |

All commands run from the root of the harness checkout. Keep the checkout clean: every
file you make goes outside it.

## Part 1: measure

### 1. Check the host and the checkout

Check the host as in
[step 1 of the first-host runbook](RUNBOOK-FIRST-HOST.md#1-check-the-host): native
execution, the build tools, free disk space. The full suite needs about 20 GB.

```sh
git status --porcelain
./bench identity
```

`git status` must print nothing. `bench identity` must show the commit that the owner
named and `"dirty": false`.

### 2. Make the lock

```sh
./bench lock <plan> -o <lock>
./bench validate <lock>
```

The plan pins the decoder commits, so `bench lock` reads no moving branch. It checks that
each commit exists upstream and records the harness commit. Copy its output into your
report: the campaign id, the digest and the decoder commits.

### 3. Prepare, verify and check

```sh
./bench prepare --lock <lock> --role <role> --work <work>
./bench verify --lock <lock> --role <role> --work <work>
./bench doctor --lock <lock> --role <role> --work <work> --machine-id <machine-id>
```

`bench prepare` downloads and verifies the streams and builds the decoders; it is the
only step that needs the network, besides `bench lock` and the publication. `bench
verify` is the untimed correctness pass. A cell that is not `ok` is a result: it stays in
the evidence and gets no timed attempt. `bench doctor` must print no `fail` line, and its
`harness` line must say "clean checkout of the lock commit".

Compare the adapter facts with the locked commits as
[step 4 of the first-host runbook](RUNBOOK-FIRST-HOST.md#4-check-the-adapter-facts) says.

### 4. Measure (only on the owner's request)

```sh
./bench run --lock <lock> --role <role> --work <work> --machine-id <machine-id>
```

Close other work on the machine first. The full suite takes many hours. The first lines
name the run: `run <run-id> (verified) in ...`. The last line must say `complete`.
Recovery is the same as in the [first-host runbook](RUNBOOK-FIRST-HOST.md#recovery).

### 5. Bundle

```sh
./bench bundle --work <work> --run <run-id> -o <bundle>
./bench validate <bundle>
```

## Part 2: assemble and publish (only on the owner's request)

### 6. Assemble the official dataset

The plan has one role, so one complete run makes a complete dataset.

```sh
git status --porcelain
./bench assemble --lock <lock> <bundle> -o <out> --official
./bench validate <out>/datasets/<dataset-id>
```

`git status` must print nothing. The command refuses and lists the reasons when the run
is not complete, a class is not `verified` or the checkout is not clean. It prints the
dataset path, `official dataset` and `complete`.

### 7. Publish with a pull request

Publication copies the dataset into `published/` of this repository
([PUBLISHING.md](PUBLISHING.md)). Start from the newest `main`, because another machine
may have published in the meantime:

```sh
git fetch origin
git switch -c publish-<dataset-id> origin/main
./bench publish <out>/datasets/<dataset-id> --dry-run
./bench publish <out>/datasets/<dataset-id> --promote
./bench validate published --immutable-since origin/main
git add published
git commit -m "data: publish <dataset-id>"
git push -u origin publish-<dataset-id>
gh pr create --base main --title "data: publish <dataset-id>" --body "Official dataset <dataset-id> of machine <machine-id>, role <role>."
```

`--promote` makes this dataset the default that the website opens first. The dataset of
the other machine stays listed and selectable. The pull request is the publication:
after the merge the website shows the dataset within some minutes. Merge it only when
the owner asked for that and all checks are green.

If the pull request gets a conflict in `published/index.json` (another dataset was merged
first), do not edit the index by hand. Make a new branch from the new `origin/main` and
run the two `bench publish` commands again; the dataset files are the same.

## Report

1. Harness commit, plan, campaign id and lock digest.
2. For each decoder: the full commit, `release <version> (<tag>)` or `development`, the
   build id and the binary SHA-256 (the `build` lines of `bench doctor`).
3. Machine id, role and the fingerprint digest of `bench doctor`.
4. The `checks:` line of `bench verify` and the last line of `bench run`.
5. Run id, run class and state; bundle path and digest.
6. Dataset id, `origin`, `complete` and the coverage counts of `manifest.json`.
7. The pull request URL and whether it is merged.
8. Every `warn` line, every difference found in step 3 and every failure.

Do not describe the speed of a decoder in your own words. The dataset holds the numbers.
