# Reproduce a benchmark on your own machine

You can run the harness on one machine. You need no account, no credential and no
access to the owner's hosts. Nothing is uploaded unless you copy files yourself. Results
from your machine are origin `local`. They are separate from the owner's official
fixed-host series and never replace it.

For the owner's two-machine campaign, use
[RUNBOOK-FIRST-HOST.md](RUNBOOK-FIRST-HOST.md) and
[RUNBOOK-SECOND-HOST.md](RUNBOOK-SECOND-HOST.md).

## Placeholders

`<plan>`, `<lock>`, `<role>`, `<machine-id>`, `<work>`, `<run-id>` and `<bundle>` mean what
they mean in [RUNBOOK-FIRST-HOST.md](RUNBOOK-FIRST-HOST.md#placeholders). `<out>` is a new
output directory.

## What you need

- A native Linux x86-64, Linux arm64 or macOS arm64 machine. Not a VM, not Rosetta.
  The Linux builds passed on hosted virtual machines, not yet on a physical Linux host; see
  [METHODOLOGY.md](METHODOLOGY.md#builds-policy-optimized-source1).
- Python 3.11 or newer, Git, and the build tools in
  [RUNBOOK-FIRST-HOST.md](RUNBOOK-FIRST-HOST.md#1-check-the-host).
- Network access for `bench lock` and `bench prepare` only. Disk space for the streams.

## Steps

All commands run from the root of the harness checkout. Keep `<plan>`, `<lock>` and
`<work>` outside the checkout, so that the checkout stays clean.

1. Make a plan. Copy `campaigns/template.toml` to `<plan>`. Set `suite` to `quick` or
   `full`. Edit the `threads` list of your role to your CPU. Keep one role, and delete
   the other. Set `arch` to your architecture (`aarch64` or `x86_64`).
2. Make the lock once:

   ```sh
   ./bench lock <plan> -o <lock>
   ```

3. Prepare. This downloads the streams and builds the decoders:

   ```sh
   ./bench prepare --lock <lock> --role <role> --work <work>
   ```

4. Work offline from here. Check, then run:

   ```sh
   ./bench verify --lock <lock> --role <role> --work <work>
   ./bench doctor --lock <lock> --role <role> --work <work> --machine-id <machine-id>
   ./bench run --lock <lock> --role <role> --work <work> --machine-id <machine-id>
   ```

   `--offline` is not needed for these commands: they never open a connection. To prove
   it for `prepare`, run it again with `--offline`.

5. Bundle and check:

   ```sh
   ./bench bundle --work <work> --run <run-id> -o <bundle>
   ./bench validate <bundle>
   ```

## Quick and full suite

`quick` has six 1920x1080 streams (three 8-bit, three 10-bit). `full` has every
stream from 1280x720 up ([corpus/suites.toml](../corpus/suites.toml)). A quick campaign
is not a full campaign: it has its own campaign id and its own lock. Use `quick` to
check that your set-up works. A full run takes many hours.

## Inspect the results

Assemble your bundle into a local dataset. Do not use `--official`:

```sh
./bench assemble --lock <lock> <bundle> -o <out>
./bench validate <out>/datasets/<dataset-id>
```

Then open `summary.csv` and `summary.json` in `<out>/datasets/<dataset-id>/`. The dataset has
origin `local`. If your plan has two roles and you ran one, the dataset is incomplete
and the other role shows `not-run`. That is correct. See
[RUNBOOK-ASSEMBLE-PUBLISH.md](RUNBOOK-ASSEMBLE-PUBLISH.md#3-inspect-the-dataset).

## Share your bundle

A bundle is one `tar.gz` file with a `SHA256SUMS` file inside. It holds the lock, your
host facts (no host name, no user name, no path), the build records and the raw rows. It
holds no stream bytes. Share the file anywhere, and share its digest (`bench bundle`
prints it). Others check it with `bench validate <bundle>`.

Your results are not official data:

- `bench publish` refuses origin `local` for `published/`. Do not ask for it.
- A result from another machine, other builds or another lock is a different series. Do
  not compare it with the official numbers without saying which lock, machine and
  commits each came from.
- State your machine id, your harness commit and your lock digest with every claim.
