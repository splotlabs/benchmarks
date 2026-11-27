# Runbook: second host (normally x86-64)

Use this runbook on the second machine of a two-machine campaign. It is normally the
x86-64 machine. The first machine used [RUNBOOK-FIRST-HOST.md](RUNBOOK-FIRST-HOST.md).
Either machine can be first.

The second host measures the other role with the exact lock of the first host. It never
makes a lock. No step here uploads data.

## Prompt for an AI agent

Copy this text. Fill in the values in angle brackets.

```text
Follow docs/RUNBOOK-SECOND-HOST.md in a checkout of splotlabs/benchmarks.
Inputs: lock file = <lock path>; expected lock digest = <digest from the first report>;
harness commit = <commit from the first report>; role = <role>;
machine id = <machine-id>; work directory = <work>; first host bundle = <bundle path or none>.
Never run `bench lock` for this campaign. Check out the harness commit first.
Run `bench run` only if I wrote "measure now" in this task.
Do not change the lock, the corpus or any setting. Do not use a Rosetta or VM host.
When you finish, give the report that the runbook lists. If a step fails, stop
and report the exact command and its output.
```

## Placeholders

`<lock>`, `<role>`, `<machine-id>`, `<work>`, `<run-id>` and `<bundle>` mean what they mean in
[RUNBOOK-FIRST-HOST.md](RUNBOOK-FIRST-HOST.md#placeholders). Here `<role>` is the role of
this host (for example `x86-64`), not the role of the first host. `<machine-id>` must be a
new name for this machine. `<lock>` is the copy of the first host's file.
`<first-bundle>` is the bundle that the first host made. `<harness-commit>` is the
harness commit of the lock. `<out>` is a new output directory for assembly.

## Status of the Linux recipes

The decoder builds ran natively only on macOS arm64. The Linux x86-64 and Linux arm64
recipes are **not verified on a native host**. The first x86-64 run is also the first
native check of the Linux recipes. If a recipe fails (`bench prepare` stops in a build
step), do not work around it. Report:

- the decoder id, the locked commit and the role;
- the OS, its version, the kernel and the CPU model (from `bench doctor`);
- the exact `bench prepare` command and its error line;
- the last 60 lines of `<work>/build/<id>/<commit>/build.log`;
- the versions of the tools that the recipe uses (`cmake --version`, `meson --version`,
  `ninja --version`, `nasm -v`, `cc --version`, `cargo --version`).

The owner then fixes the recipe in the harness. A new harness commit means a new lock,
so the campaign starts again. Do not edit the recipe in your checkout.

## Procedure

### 1. Get the lock and check it

Copy the first host's lock file to `<lock>` as an ordinary file. Check it:

```sh
./bench validate <lock>
grep '"lock_digest"' <lock>
```

`bench validate` recomputes the digest from the content. The `grep` shows the digest
stored in the file. Compare it with the expected digest from the first report. They must
be equal, character by character. If they differ, stop. Ask for the file again. Do not
edit the lock and do not make another one.

Never run `bench lock` for this campaign. A new lock reads the current upstream
branches and gives other commits, a different digest and a different campaign.

### 2. Use the harness commit of the lock

Official data needs a clean checkout at exactly the harness commit of the lock. Take
the commit from the first report. You can also read it from `bench doctor` in step 4: its
`harness` line names the lock commit. Check the commit out:

```sh
git fetch origin
git checkout <harness-commit>
git status --porcelain
./bench identity
```

`git status` must print nothing. `bench identity` must print the `<harness-commit>` as
`commit` and `"dirty": false`. A detached HEAD is correct. If `git checkout` cannot find
the commit, the first host locked a commit that is not on `origin`. Stop and report.

### 3. Check the host and prepare

Check native execution and the tools as in
[step 1 of the first-host runbook](RUNBOOK-FIRST-HOST.md#1-check-the-host). On x86-64 the
recipes also need `nasm`, and they build for `x86-64-v3`. A host without AVX2 cannot
run those builds.

```sh
./bench prepare --lock <lock> --role <role> --work <work>
```

Then compare the facts of
[step 4 of the first-host runbook](RUNBOOK-FIRST-HOST.md#4-check-the-adapter-facts) with the
locked commits, if they differ from the inspected commits.

### 4. Verify and run the preflight

```sh
./bench verify --lock <lock> --role <role> --work <work>
./bench doctor --lock <lock> --role <role> --work <work> --machine-id <machine-id>
```

`bench doctor` confirms the harness: its `harness` line must say "clean checkout of the
lock commit". It also refuses a role whose architecture is not the native one. The
`correctness` line must show that every cell has a row.

### 5. Measure (only on the owner's request)

```sh
./bench run --lock <lock> --role <role> --work <work> --machine-id <machine-id>
```

The command prints `run <run-id> (verified) in ...`. Recovery is the same as in the
[first-host runbook](RUNBOOK-FIRST-HOST.md#recovery).

### 6. Bundle and validate

```sh
./bench bundle --work <work> --run <run-id> -o <bundle>
./bench validate <bundle>
```

### 7. Assemble, if the first bundle is available

If you have the first host's bundle `<first-bundle>`, assemble both hosts on this
machine. Use [RUNBOOK-ASSEMBLE-PUBLISH.md](RUNBOOK-ASSEMBLE-PUBLISH.md):

```sh
./bench validate <first-bundle>
./bench assemble --lock <lock> <first-bundle> <bundle> -o <out>
```

Without `--official` the dataset is origin `local`. This is the inspection step. The
`--official` step and publication are the owner's explicit step.

If you do not have the first bundle, do not assemble. State the later command and its
inputs in the report: the lock `<lock>`, one bundle per role (the first host's bundle and
`<bundle>`), and an output directory `<out>`:

```text
./bench assemble --lock <lock> <first-bundle> <bundle> -o <out>
```

## Report

Give the same report as in the
[first-host runbook](RUNBOOK-FIRST-HOST.md#report), for this host. Add:

1. The lock digest that you checked, and that it equals the first host's digest.
2. The harness commit that you checked out and the output of `bench identity`.
3. Whether any Linux recipe failed, with the data from "Status of the Linux recipes".
4. Whether you assembled, and the dataset path, or the later assembly command.

Files to copy to the host that assembles: `<bundle>`. Copy it as an ordinary file.
The first host needs your bundle only if it assembles.
