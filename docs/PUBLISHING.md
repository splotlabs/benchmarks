# Publishing a dataset

Published data lives in this repository, in `published/` on `main`. The owner decided
that no object storage is used. A publication is a normal pull request that adds files
under `published/`. Nothing publishes automatically: no CI job, schedule, run or assemble
step calls `bench publish`, and the command never runs Git.

```
published/index.json                    schema splotbench.index/1: dataset entries and latest_complete
published/datasets/<dataset_id>/        one dataset, exactly as `bench assemble` wrote it
published/README.md
```

## Procedure

Assemble first (`bench assemble ... --official -o OUT`). Then, in a checkout of this
repository:

```sh
./bench publish OUT/datasets/<dataset_id> --dry-run   # check and report; writes nothing
./bench publish OUT/datasets/<dataset_id>             # copy the dataset and list it in the index
git switch -c publish/<dataset_id>
git diff --stat                                       # review: new files only, plus index.json
git add published
git commit -m "data: publish <dataset_id>"
# push the branch and open a pull request against main
```

To make the dataset the default, add `--promote` to the publish command. A dataset that
is published without `--promote` is listed but is not `latest_complete`. Publishing an
older dataset later never changes the default. You can run the command again with
`--promote` for a dataset that is already listed; it then changes only `latest_complete`.

`--to DIR` sets another target directory. The default is `published/` of the harness
checkout. The command changes files inside the target directory only.

### What the command checks

The dry run and the real run start with the same checks, before any write:

- The directory passes the rules of `bench validate`, and every file matches `SHA256SUMS`.
- Origin `official` and `complete`. Origin `synthetic` is always refused. Origin `local` is
  refused unless `--allow-local` is set, and then only into a folder other than the default
  `published/` directory (for a personal fork or folder). `--promote` needs `official`.
- No file is larger than 50 MiB (the hosting limit for one file in Git is 100 MB). The
  message names the file.
- An index entry with the same id and different content is refused.
- An existing dataset directory with different bytes is refused; the message names the
  first file that differs. With identical bytes it is "already present".

### What the command does

1. Copy the files into a temporary directory in `datasets/` of the target, check the copy
   against `SHA256SUMS`, and move it to `datasets/<dataset_id>/` with one rename.
2. Only then write `index.json` atomically: all existing entries stay, the new entry is
   added with `published_utc`, and `latest_complete` changes only with `--promote`.

The temporary directory is `datasets/.<dataset_id>.tmp` inside the target. A stale one is
removed when you publish the same dataset again; `bench validate published` reports it until then.

A failure before step 2 leaves the index and `latest_complete` unchanged and leaves no
partial dataset directory. A failure or interrupt after the rename and before the index
write leaves a complete dataset directory that the index does not list. `bench validate
published` reports it ("not listed in index.json"). Running `bench publish` again with the
same dataset completes it: the directory is "already present" and the index is written. Running the same command again after success changes nothing
(the first `published_utc` stays). One publication at a time can run into a target: the
command holds a lock on the target directory.

## Checks in CI

CI runs `./bench validate published`. On pull requests it adds
`--immutable-since origin/<base branch>`, which fails when the pull request changes,
renames or deletes a file of an existing `published/datasets/<id>/` directory, or adds files
to one. New dataset directories and changes to `published/index.json` are allowed. The
check reads Git with null-separated output, so every file name is exact. These checks look
at committed files only. You can run the same check locally with
`./bench validate published --immutable-since origin/main`.

`bench validate published` checks that the index is valid, that every listed dataset
directory exists and is valid, that every dataset directory is listed, that
`latest_complete` is null or names a listed complete official dataset, that no dataset is
synthetic, and that no file is above the size limit.

## How the website reads the data

The raw file URL form is
`https://raw.githubusercontent.com/splotlabs/benchmarks/main/published/`, followed by
`index.json` or `datasets/<dataset_id>/summary.json`. All paths inside the files are
relative to the file that holds them.

Observed on 2026-11-27 for a file of this repository (this is not a guarantee of GitHub):
`Cache-Control: max-age=300`, `Access-Control-Allow-Origin: *` and
`Content-Type: text/plain; charset=utf-8`. So a merged publication can take about five
minutes to show, and a reader must parse the body as JSON whatever the content type says.
Check the headers again before the website depends on them.

## Recovery

- **Interrupted copy** (disk full, Ctrl-C): no dataset directory and no index change remain
  (a temporary directory may remain after a hard kill; publishing the same dataset again removes it). Run the
  same command again. After the rename and before the index write the dataset directory
  stays; the same command completes it (see above).
- **Wrong dataset, pull request not merged**: delete that dataset directory from the
  branch, restore `published/index.json` from `main` (`git checkout origin/main --
  published/index.json`), and push. CI allows this only while the directory is new.
- **After a merge**: the dataset stays. A correction is a new dataset: assemble again,
  publish it, and promote it if it is the right default.
- **Wrong default**: publish with `--promote` for the right dataset, through a pull request.
