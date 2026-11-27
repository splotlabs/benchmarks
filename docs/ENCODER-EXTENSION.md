# Encoder workload: design note

This is a design note only. The harness has **no encoder runner, no quality tool, no
rate-distortion analysis and no BD-rate analysis**. Nothing here is implemented. Do not
add an encoder runner or a plugin framework for it
([AGENTS.md](../AGENTS.md#scope)). This page says what the smallest schema extension
would be, so that a later change adds a workload kind and does not rewrite decode data.

## What the current code does

Every attempt row, correctness row and lock case carries `workload: {"kind": "decode", ...}`.
The schemas hold this as a discriminated union with one member. Any other kind is an
explicit error ("unknown workload.kind"), and so is an unknown schema major version.
Decode names (`wall_time`, `throughput`, `cpu_time`, `peak_rss`, `first_frame_latency`)
keep their meaning ([FORMATS.md](FORMATS.md#metrics)). A new kind adds members and
leaves them alone.

## Facts that an encode result needs

| Group | Fields |
| --- | --- |
| Source video | Identity of the source: width, height, bit depth, chroma format, sample format (planar, 8 or 16 bit container), frame count, frame rate, and the SHA-256 of the source file and of its raw samples. |
| Encoder | Implementation id, source commit and snapshot, release or development identity, build id and binary SHA-256 (the same build records as for decoders), and the settings: every option and value that the command line passed, thread count, and any preset or CRF label as a label only. |
| Output bitstream | SHA-256 and size in bytes of the output file, the container, and the duration (frame count and frame rate of the bitstream). |
| Encode time | Wall time, CPU time and peak RSS with the same boundary rules as decode (`process-wall`). |
| Decode validity | The output decoded by an independent decoder (not the encoder's own reconstruction): decoder identity, exit code, frame count and the SHA-256 of its raw output. |
| Quality tool | Implementation, model (for example a metric model file) and its SHA-256, version, and the preprocessing (colour conversion, scaling, frame alignment, which frames and planes). The score values are then data of that tool identity. |

## The smallest schema extension

1. **Workload discriminator.** Add the member `"encode"` to the `discriminator.mapping` of
   `workload` in `schemas/attempt.schema.json` and `schemas/correctness.schema.json`, and
   to `workload_decode_kind` in `schemas/common.schema.json` (rename it, because it
   then holds two kinds). That definition is used by the lock cases and by
   `summary.schema.json`. The `encode` member of an attempt carries the output bitstream
   identity and the encode metrics. The `encode` member of a correctness row carries the
   independent decode validity.
2. **A new metric dictionary.** `metric_dictionary` in `schemas/common.schema.json` is
   closed (`additionalProperties: false`) and holds decode names only. Add a second
   dictionary for encode (`encode_time`, `output_size`, and the quality scores of a named
   tool). Do not widen the decode dictionary and do not add a generic metric bag.
3. **Lock.** A lock for encode needs a corpus of source videos (the identity group above),
   encoder implementations with their settings, and the quality tool identity. These are
   new fields of an encode lock. The lock schema major version changes only if a decode
   field changes. Otherwise a new kind is an addition.
4. **Index.** `index.schema.json` limits `workload` to `["decode"]`. Add `"encode"`.
5. **Readers.** `bench assemble`, `bench validate` and the website must show an
   "unsupported data version" state for a kind they do not know. This already holds for decode-only readers.

The runner needs one new command shape per encoder (an adapter function), the same way
decoders have `verify_command` and `timed_command`. That is future work.

## Limits that the documents must keep

- Equal preset or CRF labels do not mean equal quality. Encoders map the same label to
  different tools and different rate-distortion points. A comparison of encode time at
  equal labels is not a comparison at equal quality. Record the settings as data. Do not rank by label.
- Encoder outputs need not be byte-identical, not even for one encoder on two hosts
  (threading, floating point and SIMD paths can change the bitstream). So an encode
  result identifies its output by hash and verifies it by independent decoding. It does
  not require that two runs match.
- Quality values depend on the tool, the model, the version and the preprocessing. A value
  without that identity is not comparable.
- Rate-distortion curves and BD-rate need several operating points per encoder, a fitting
  method and its own identity. They are **future work and not implemented**. The harness
  would compute them in the summary, never the website.
