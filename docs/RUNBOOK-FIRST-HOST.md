# Runbook: first host (normally ARM)

Use this runbook on the first machine of a two-machine campaign. It is normally the
arm64 machine. Either machine can be first. The second machine uses
[RUNBOOK-SECOND-HOST.md](RUNBOOK-SECOND-HOST.md).

This runbook prepares and measures one role. Assembly and publication are separate
steps ([RUNBOOK-ASSEMBLE-PUBLISH.md](RUNBOOK-ASSEMBLE-PUBLISH.md)). No step here uploads data.

## Prompt for an AI agent

Copy this text. Fill in the five values in angle brackets.

```text
Follow docs/RUNBOOK-FIRST-HOST.md in this checkout of splotlabs/benchmarks.
Inputs: plan or existing lock = <plan-or-lock path>; role = <role>;
machine id = <machine-id>; work directory = <work>; suite = <quick or full>.
Do the steps in order. Run `bench run` only if I wrote "measure now" in this task.
Do not change the lock, the corpus or any setting. Do not use a Rosetta or VM host.
When you finish, give the report that the runbook lists. If a step fails, stop
and report the exact command and its output.
```

## Placeholders

| Placeholder | Meaning |
| --- | --- |
| `<plan>` | A campaign plan file (TOML). Copy `campaigns/template.toml` to a path outside the harness checkout and edit the copy. |
| `<lock>` | The lock file (JSON), outside the harness checkout. |
| `<role>` | A role name of the lock, for example `arm64`. Its `arch` must be the native architecture of this host. |
| `<machine-id>` | The owner's name for this machine: 2 to 41 characters of `a-z`, `0-9`, `-`. For example `m2max-01`. Never a MAC address or a serial number. |
| `<work>` | The work directory. It holds the stream cache, sources, builds and runs. Keep it outside the harness checkout. |
| `<run-id>` | The run id that `bench run` prints. Its form is `<role>-<UTC time>-<8 hex>`. |
| `<bundle>` | A path for the bundle file, for example `<work>/<run-id>.tar.gz`. |

All commands run from the root of the harness checkout.

## Procedure

### 1. Check the host

Check native execution. Official data needs a native host, not a VM, not Rosetta.

```sh
uname -sm
```

On macOS, `sysctl sysctl.proc_translated` must print `0` or be unknown. `bench doctor`
refuses Rosetta and other emulation. A virtual machine is not detected by the
harness: do not use one for official data. Say in the report if you do not know.

Check the tools. Python 3.11 or newer and Git are always needed. `bench prepare` needs
the build tools of the recipes ([METHODOLOGY.md](METHODOLOGY.md#builds-policy-optimized-source1)):

| Decoder | Tools |
| --- | --- |
| Splot | `cargo` (with `rustup`; the toolchain comes from `rust-toolchain.toml` of the Splot source) |
| dav2d | `meson`, `ninja`, a C compiler (`cc`, or the command in `CC`), and `nasm` on x86-64 |
| avmdec | `cmake`, `ninja`, `perl`, a C compiler, and `nasm` on x86-64 |

Check the disk. The work directory needs space for the streams of the suite, three
source checkouts, three builds and the run logs. `bench doctor` fails below 1 GiB free.
The full suite needs much more. Check the free space and compare it with the `bytes`
of the cases in the lock.

```sh
df -h <work>
```

If `<work>` does not exist yet, check its parent directory.

### 2. Get the lock

If the owner gave you a lock, use it. Do not run `bench lock`. Check it:

```sh
./bench validate <lock>
```

`bench validate` checks the schema and recomputes the lock digest. It prints
`valid: <lock> (lock sha256:<hex>, campaign <id>)`. Record that digest and campaign id for
your report. Then go to step 3.

If the owner gave you a plan, make the lock once. The harness checkout must be clean
and at a commit that the harness repository serves, because the second host fetches that
commit. `bench lock` enforces this for a standard plan: it stops when the checkout is not
a clean Git commit or when the commit cannot be fetched from the harness repository.
A short pre-check saves time:

```sh
git status --porcelain
git fetch origin
git merge-base --is-ancestor HEAD origin/main && echo on-main
./bench identity
```

`git status` must print nothing. The last command must print `on-main`. `bench identity`
must show `"dirty": false`.

```sh
./bench lock <plan> -o <lock>
```

`bench lock` is the only command that reads moving references (branches). It writes the
lock once and refuses to overwrite a file. It prints the campaign id, the digest, the
suite, the harness commit and, per decoder, the full commit and `release` or
`development`. Copy that output into your report.

### 3. Prepare

`bench prepare` is the only step that needs the network, besides `bench lock`. It
downloads and verifies the streams, fetches the locked commits and builds the decoders.

```sh
./bench prepare --lock <lock> --role <role> --work <work>
```

Add `--jobs 2` on a machine that must stay usable. A finished build is reused. A failed
build stops the command with a message and `<work>/build/<id>/<commit>/build.log`.

### 4. Check the adapter facts

[METHODOLOGY.md](METHODOLOGY.md#inspected-source-facts) lists facts that were inspected
at three commits (Splot `a6ce057f53a1`, dav2d `33b0d3d234c6`, AVM `1cfda4fa4879`).
Compare them with the locked commits in your `bench lock` output (or in the lock file).
If a commit is another one, do not change the lock. Look at these points in
`<work>/src/<id>/<commit>/`, and report every difference:

- Film grain is on by default and the timed command does not turn it off (Splot:
  `--skip-film-grain`; dav2d: `--filmgrain`; avmdec: `--skip-film-grain`).
- The timed command still decodes every frame and only skips the output step
  (Splot `--output-format null`; dav2d `--muxer null`; avmdec `--noblit`).
- The thread option means what the table in METHODOLOGY.md says.

`bench verify` also checks that each binary names the options of both commands. A
decoder that lacks one gets the outcome `unsupported`.

### 5. Verify

The correctness pass decodes every case with every decoder and thread budget, hashes the
output and compares it with the reference. It is untimed. It exits 1 when a cell is not
`ok`. That is a result, not a reason to change anything. The last line is
`checks: <n> ok, <n> incorrect, <n> unsupported, <n> timeout, <n> error`. Copy it into your report.

```sh
./bench verify --lock <lock> --role <role> --work <work>
```

To check one cell first, add `--case <case-id>`, `--implementation <id>` or `--threads <n>`.

### 6. Run the preflight

```sh
./bench doctor --lock <lock> --role <role> --work <work> --machine-id <machine-id>
```

Read every line. A `fail` line stops you. A `warn` line needs a decision:

- `harness` warns when the checkout is not clean at the commit of the lock. The result
  is then `development` and not official. Fix the checkout, do not continue.
- `tools` warns about a missing build tool. It matters only for `bench prepare`.
- `correctness` warns when cells have no row yet. `bench run` checks them.
- `cpus` warns when the host has fewer logical CPUs than the largest thread budget.

`bench doctor` prints the `builds` line and then one line per build:
`build <impl> <commit> <release|development> <build_id> sha256:<hex>`. Copy these lines
into your report. They give the build ids and the binary digests.

`bench doctor` prints the host fingerprint at the end. Copy the line
`fingerprint sha256:...` into your report.

### 7. Measure (only on the owner's request)

Before the first measurement of a campaign, do steps 1 to 6 on the second host too, with
the same lock ([RUNBOOK-SECOND-HOST.md](RUNBOOK-SECOND-HOST.md), steps 1 to 4). A build
recipe that fails there needs a harness fix, and a harness fix needs a new lock. If you
find that after the first host measured, the first measurement is lost.

Run this step only when the owner asked for the measurement. Close other work on the
machine first. Keep the host on mains power and awake. Do not change any system setting.

```sh
./bench run --lock <lock> --role <role> --work <work> --machine-id <machine-id>
```

The command checks again, runs the correctness pass for cells without a row, then runs
every timed attempt, one process at a time. It prints one line per attempt. The first
lines name the run: `run <run-id> (verified) in <work>/runs/<run-id>`. A full suite can
take many hours. Do not start a second harness command in the same `<work>`.

### 8. Bundle and validate

```sh
./bench bundle --work <work> --run <run-id> -o <bundle>
./bench validate <bundle>
```

`bench bundle` prints the bundle digest. It does not upload anything.

## Report

Give the owner this report. Use the values that the commands printed.

1. Lock: path, digest and campaign id (from `bench lock` or `bench validate`).
2. Harness commit, and the output of `bench identity` (`dirty` must be false).
3. For each decoder: the full commit, `release <version> (<tag>)` or `development`, and
   the build id and the binary digest (the `build ...` lines of `bench doctor`).
4. Machine id, role, and the fingerprint digest of `bench doctor`.
5. Run id and run class (`verified`, `development` or `synthetic`), and the run state.
6. The `checks:` line of `bench verify`, and the final line of `bench run`
   (`run <id>: <state>, <n> correctness rows, <n> attempt rows`).
7. The paths of the lock and of the bundle, and the bundle digest.
8. Every `warn` line, every difference found in step 4, and every failure.

Say clearly if you did not run step 7.

Files to copy to the second host:

- The lock file `<lock>`. Always. Also give the digest of item 1 and the harness commit of item 2.
- The bundle `<bundle>` only to assemble both hosts on one machine. The second host does
  not need it to measure. Copy bundles as ordinary files (`scp`, a USB drive).

Do not copy `<work>`: the streams and builds are rebuilt on the other host.

## Recovery

- **Interrupted run.** `bench run` exits 130 and prints the exact `--resume` command.
  The rows stay. Run it again with the same lock, role, work directory and machine id:

  ```sh
  ./bench run --lock <lock> --role <role> --work <work> --machine-id <machine-id> --resume <run-id>
  ```

  A resume refuses when the lock, harness, builds, binaries, machine id or host
  fingerprint changed (for example another power source). Then start a new run
  without `--resume`. The old run keeps its rows and can still be bundled.
  Resume never rewrites a row. It may print `attempt <n> has a start record but no row;
  recorded it as interrupted`: the attempt is recorded and runs again under a new
  number. It may also print that an incomplete last line was moved to `logs/`: the
  partial line is kept there and is not a row. If `bench run` exits 130 before a run
  directory exists, it says so; nothing was started and you run the command again
  without `--resume`.
- **Host lock held after a crash.** If the harness process ended without cleanup,
  `bench run`, `bench verify` or `bench doctor` says that the lock is held and that a
  decoder process still runs. Find it with the `ps -A -o pid,pgid,command` command that
  the message gives. Stop that process group, then resume the run. The lock is one
  file for the whole host, `<tmp>/splotbench-host-<uid>.lock` in the system temporary
  directory: a `bench run`, `prepare` or `verify` in another work directory also holds
  it. `bench doctor` prints its path and whether it is free.
- **Failed build.** Read `<work>/build/<id>/<commit>/build.log`. Install the missing
  tool, then run `bench prepare` again. Do not patch the source and do not change the
  lock. If the recipe itself fails, stop and report the log. On a platform other than
  macOS arm64, a failing recipe is a finding for the harness; see the second-host runbook.
- **Input URL not available.** Do not change the lock or the corpus. `bench prepare`
  names the stream. Try again later. Report the stream and the error. A lock case may
  list `mirrors`; the harness uses them as the lock says.
- **Reused correctness rows.** A correctness row counts only when its reference values
  equal the lock case. A row from another lock is not reused; `bench run` checks the cell again.
- **Decoder gives wrong output.** The `incorrect` result stays in the evidence. The
  cell gets no timed attempt. Do not skip the case. Report it.
  Each attempt log in `<work>/runs/<run-id>/logs/` starts with `# attempt <row JSON>` and
  `$ <command>`. A log is capped at 1 MiB (head and tail are kept).
- **Bundle of an interrupted run.** `bench bundle` accepts it. `run.json` holds the
  state. An assembly with `--official` refuses it.
