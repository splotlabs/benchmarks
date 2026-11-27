# Corpus

`ewouth-av2.toml` is a pinned import of `tests/conformance/ewouth-av2.toml` from
https://github.com/splotlabs/splot (commit and file sha256 are in `[imported_from]`).
It has 88 streams. The stream bytes are not in Git. The harness downloads them from the
publisher and checks size and SHA-256 (`bench prepare`).

Each stream has its URL, input sha256 and size, the frame count, the sha256 and size of
the `avmdec --rawvideo` output (the reference), and facts read from the stream: fourcc,
profile, size, bit depth, chroma and tiles of the first frame. `[oracle]` names the AVM
revisions that made the reference hashes.

`suites.toml` lists the case ids of `quick` and `full`. The rule is in the file. A test checks it.

## How the import was made

1. Take `tests/conformance/ewouth-av2.toml` at a Splot commit on `main`. Record the commit and the file sha256.
2. Download every stream. Check its sha256 against the Splot manifest.
3. Read the IVF header (fourcc, width, height) and run `splot inspect --json` on each stream
   for bit depth, chroma, profile and tiles. Check that `raw_bytes / frames` equals the computed frame size.
4. Write one `[[case]]` per stream. The `id` is the file name in lower case without `.ivf`,
   with every run of other characters replaced by `-`.
5. Write `film_grain` for a stream only when the frames are known to apply grain. No stream has the key.

To import again, repeat these steps, change `[imported_from]`, and make a new lock.
