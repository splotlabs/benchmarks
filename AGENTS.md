# Instructions for humans and agents

This repository is the benchmark harness for AV2 decoders (Splot, dav2d, avmdec).
These rules are canonical. `CLAUDE.md` points to this file.

## Scope

- This repository holds the harness only: the `bench` command, its package
  `splotbench/`, data schemas, corpus and campaign manifests, and documentation.
- Do not add a scheduler, a runner service, a self-hosted agent, a database,
  a backend or an encoder runner.
- CI checks the software with synthetic fixtures only. Do not add scheduled
  benchmarks, remote runners or automatic publication.
- Register and document a command only when it is implemented. Do not add stubs
  or empty placeholder modules.

## Code

- Use Python 3.11 or newer and the standard library only. Get the owner's
  approval before you add any dependency or build step.
- Start every `.py` file with `# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0`.
- Each command module adds its parser with `register(subparsers)` in `cli.py`.
- Raise `BenchError` for an expected failure. The CLI prints one actionable
  message (what failed, which file or value, what to do) without a traceback.
- Keep the exit codes: 0 success, 1 failure of the operation, 2 usage error,
  3 missing prerequisite or configuration, 130 interrupted.
- Write nothing outside the user-selected work directory and explicit `-o` paths.
- Record no environment variable outside `util.ENV_ALLOWLIST`.

## Data

- JSON Schema files are the single source of truth for every data format.
  Do not write a second, hand-coded check for the same format.
- Digests are `sha256:<64 hex>` over canonical JSON (`util.canonical_json`).
  Digested objects contain no floats.
- Never fabricate results or performance numbers. Fixtures are synthetic and
  their names say so. Fixture decoders are `fixture-a`, `fixture-b`, never the
  name of a real decoder.
- Do not commit stream bytes, builds, raw runs, bundles or generated datasets.
- Do not put secrets, account identifiers, private paths or user names in files,
  records or commits.

## Gate

Run the gate before each handover:

```sh
python3 -m unittest discover -s tests
```

Tests cover behaviour and real failure modes with independently known expected
values. They need no network.

## Commits

Use Conventional Commit subjects, for example `feat: add the bundle writer`.
Work on a named branch. Do not push to `main`.

## Owner decisions

Ask the owner before you change licence, contribution or privacy terms.
Do not edit `LICENSE.md`. Say "source-available", never "open source".

## Autonomous Work

The owner turns autonomous mode on in the task ("autonomous", "take it to
merge") or by handing over a GitHub issue. Without it, a commit, push, PR or
merge needs the task to name it.

In autonomous mode, do the whole path without asking or reporting progress:
branch, implement, commit, run the gate and a code review, push, open the PR
(linked to its issue, if any), address every review comment (fix it, or reply
with the reason it stays), and merge when CI is green. Diagnose failing checks
and conflicts, and continue.

Stop only for a decision that is the owner's:

- The requirement is unclear.
- The best solution is a larger restructure than the task implies, or several
  sound designs exist.
- A rule in this file says to ask or needs human sign-off.
- The action is destructive or cannot be undone.

Ask once, with the options, their pros, cons and effort, and a recommendation.
Continue the work that does not depend on the answer. Never reach the merge
with a hack, workaround, weakened check, skipped test or dismissed comment.
