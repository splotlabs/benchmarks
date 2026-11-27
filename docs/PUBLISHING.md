# Publishing a dataset

`bench publish` copies one assembled dataset to S3-compatible storage. The intended
target is Cloudflare R2. Nothing publishes automatically: no CI job, schedule, run or
assemble step calls it. The owner starts it by hand.

## What the owner creates first

This repository creates none of these. Use placeholders in angle brackets below; do not
commit real values.

1. An R2 bucket: `<bucket>`.
2. An R2 API token with object read and write permission on `<bucket>` only. R2 shows
   the token as an access key id and a secret access key.
3. A public custom domain for the bucket: `<data domain>`. Do not use the `r2.dev` URL
   for production; it is rate limited and has no cache control of its own.
4. A CORS rule on the bucket: allow method `GET` from the website origin
   `<website origin>`, and no other origin.

The S3 endpoint is `https://<account id>.r2.cloudflarestorage.com`. The region is `auto`
(the publisher fixes it). The publisher uses path-style requests: `<endpoint>/<bucket>/<key>`.

## Environment

The publisher reads the environment only. It never prints, records or writes a secret.

| Variable | Meaning |
| --- | --- |
| `SPLOTBENCH_S3_ENDPOINT` | `https://<account id>.r2.cloudflarestorage.com`. Only `https:` is accepted (and `http://127.0.0.1` or `http://localhost` for tests). No path. |
| `SPLOTBENCH_S3_BUCKET` | `<bucket>` |
| `SPLOTBENCH_PUBLIC_BASE_URL` | The public URL that serves `index.json`: `https://<data domain>` when there is no prefix, `https://<data domain>/<prefix>` when there is one. |
| `SPLOTBENCH_S3_PREFIX` | Optional key prefix, for example `benchmarks`. Letters, digits, `.`, `_`, `-` and inner `/`. |
| `AWS_ACCESS_KEY_ID` | The token's access key id. |
| `AWS_SECRET_ACCESS_KEY` | The token's secret. |

A missing required variable is exit 3 with the names, before any request. A dry run needs
none of them.

## Procedure

Assemble first (`bench assemble ... --official -o OUT`), then:

```sh
./bench publish OUT/datasets/<dataset_id> --dry-run   # check and report
./bench publish OUT/datasets/<dataset_id>             # upload and list the dataset
./bench publish OUT/datasets/<dataset_id> --promote   # also make it latest_complete
```

**Dry run.** It validates the directory (as `bench validate`), then lists every object
key, size, SHA-256, content type and cache header, the destination, the origin and
completeness, and the exact index change: entry added or unchanged, and `latest_complete`
before and after. When the endpoint, bucket and credentials are set it also reads the remote
state (only GET requests). It sends no write request.

**Refused before any request.** A dataset that fails validation. Origin `synthetic`, always.
Origin `local`, unless `--allow-local` (for a personal bucket). An incomplete dataset.
`--promote` for a dataset that is not official (`latest_complete` names the latest complete
official dataset).

**Publish.** Order:

1. Each file goes to `<prefix>/datasets/<dataset_id>/<path>` with `If-None-Match: *`
   (the bundles first, `manifest.json` and `SHA256SUMS` last). An object that already
   exists is read and compared by the SHA-256 of its bytes: equal is "already present",
   different is a collision and the run stops (exit 1). An ETag is never used as a content
   hash.
2. Every object written by this run is read back and its SHA-256 is compared with the
   local file.
3. Only then `<prefix>/index.json` is read with its ETag (absent is allowed), the dataset
   entry is added (all existing entries stay), `latest_complete` is set only with
   `--promote`, and the file is written with `If-Match: <etag>` (`If-None-Match: *` when
   it did not exist).

A failure before step 3 leaves the index and the default unchanged. Publishing the same
dataset again with identical content writes nothing. An index entry with the same id and
different content is refused. A lock file `<dataset directory parent>/.publish.lock`
serialises publications from one machine.

Publishing an older dataset later does not change the default; use `--promote`.

## Content types and cache

| Object | Content-Type | Cache-Control |
| --- | --- | --- |
| `*.json` | `application/json` | dataset files: `public, max-age=31536000, immutable` |
| `summary.csv` | `text/csv; charset=utf-8` | same |
| `bundles/*.tar.gz` | `application/gzip` | same |
| `SHA256SUMS` | `text/plain; charset=utf-8` | same |
| `index.json` | `application/json` | `public, max-age=60, must-revalidate` |

Dataset files never change, so caches keep them for a year. `index.json` changes with each
publication, so browsers and the CDN revalidate it after 60 seconds. If the custom domain
sits behind a Cloudflare cache rule, make sure the rule respects origin `Cache-Control`.

## Recovery

- **Interrupted upload** (network error, HTTP error, Ctrl-C): the index was not changed.
  Run the same command again. Objects already stored are compared and skipped.
- **Read-back mismatch**: the index was not changed. Check the bucket, then run again. A
  bad object that differs from the local file stops every later run as a collision; delete
  that one object in the R2 dashboard (an owner action) and run again.
- **Collision** (an object exists with different bytes): the data in the bucket is not
  this dataset. Do not overwrite. Find out why; assemble again for a new dataset id.
- **Index conflict** (HTTP 412 on `index.json`): another publication changed it. The dataset
  files are complete. Run the same command again; it reads the new index and keeps the
  other entry.
- **Wrong default**: run `bench publish <dir> --promote` for the right dataset.

## Limits

One PutObject per file, no multipart upload. A file above 256 MiB is refused. There is one
retry after a connection error and no other retry. Only the S3 API of one endpoint is
supported; there is no other provider.
