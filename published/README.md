# Published data

This directory holds benchmark datasets that the owner chose to publish, and the index
that lists them. The website reads these files through the raw file URL of the
repository. Files here are published evidence.

- `index.json` lists the published datasets and names `latest_complete`.
- `datasets/<dataset_id>/` is one dataset exactly as `bench assemble` wrote it.

An existing dataset directory never changes and is never deleted. A correction is a new
dataset. Add a dataset only with `bench publish`, through a pull request. See
[docs/PUBLISHING.md](../docs/PUBLISHING.md).

No results are published yet.
