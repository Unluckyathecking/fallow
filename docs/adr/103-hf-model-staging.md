# stage models from Hugging Face with derived minimums

## Status

Proposed

## Date

2026-08-26

## Goal

Make staging a model an operator can do correctly on the first try.

Today `flw models pull <url>` takes a URL the operator found by hand in a browser
and four flags they typed from memory, two of which (`--min-ram-mb` and
`--min-vram-mb`) default to `0` and are never checked against anything. ADR 048
auto-assign compares exactly those two numbers against an agent's free RAM and
free VRAM at enrolment, so a defaulted `0` says "fits anywhere", including a desk
with 2 GB free and a 3B model. The one number that decides where a model lands is
the number nobody has any way to compute, and the CLI never asks for it twice.

The file itself already answers most of the question. A GGUF header names its own
quantisation, and its size on disk is the floor of what serving it costs. This
change reads both and asks the operator only for what the file cannot say.

## Owned paths

- `packages/fallow-cli/src/fallow_cli/gguf.py`, `hf.py`, `catalog.py`, `pull.py`,
  `model_catalog.toml` (all new)
- `packages/fallow-cli/src/fallow_cli/main.py` (the `models pull` command),
  `blobs.py` (one `license` argument), `README.md`
- `packages/fallow-cli/tests/test_cli_gguf.py`, `test_cli_pull.py` (new),
  `cli_helpers.py`
- `docs/lan-site/operator-runbook.md` (§3), `docs/lan-site/README.md`,
  `deploy/README.md` (§3.1)
- `docs/adr/103-hf-model-staging.md`, `CHANGELOG.md`

No protocol change. `ModelManifest` is untouched, so `schemas/` and the Go
codegen are untouched.

## Decision

**An `hf:` source scheme.** `flw models pull hf:<owner>/<repo>/<file.gguf>`
resolves to `https://huggingface.co/<owner>/<repo>/resolve/<revision>/<file>`,
with `@<revision>` after the file selecting a branch or commit and `main` when it
is absent. Every segment must match `[A-Za-z0-9][A-Za-z0-9._-]*`, so a spec
cannot carry `..`, a query string, a fragment or a second scheme into the URL.
The parse is the sanitiser, and it runs before any client is built. Plain URLs
keep working exactly as before.

**Provenance goes where the manifest already has room.** `source_url` records the
canonical resolve URL and `license` records the catalog entry's licence name;
both are existing `ModelManifest` fields. There is no free-form field and adding
one would ripple into `schemas/` and the Go codegen for no serving benefit, so
the rest of the provenance (the `hf:` spec as typed, the pinned revision, which
values were used) is printed as one line on stderr at registration:

```text
registered qwen2.5-0.5b-instruct-q4km from hf:Qwen/Qwen2.5-0.5B-Instruct-GGUF/…@main
quant=Q4_K_M min_ram_mb=1051 min_vram_mb=0 license=apache-2.0 sha256=74a4da8c…
```

That is a record in the operator's terminal and their shell log, not a queryable
field. It is the honest scope of "no wire change".

**Derive the quantisation from the file.** `fallow_cli.gguf` reads the GGUF v2/v3
header (magic, version, tensor count, and the metadata KVs) with nothing but
the standard library, and maps `general.file_type` through llama.cpp's
`llama_ftype` to a name like `Q4_K_M`. It reads only the header: the magic is
checked on the first four bytes, values it does not need are seeked over rather
than read, and a bounds check on every skip means a truncated file fails instead
of being seeked past. Models are multi-GB; nothing here reads one twice.

v1 GGUF is rejected by name rather than misread: it used 32-bit lengths, which
is a different parser, and no current tooling emits it.

The `llama_ftype` values are not contiguous — 4, 5, 6 and 33–35 are removed or
withdrawn — so the table is transcribed, not counted. A table built by counting
shifts every entry above the first gap, and a shifted entry does not fail
loudly: it registers a model under a quantisation it is not. Spot values across
each gap are pinned by test, and the withdrawn numbers are pinned as deriving
nothing.

**A parse failure is never fatal.** Every malformed input raises `GgufError`,
which the pull path turns into "fall back to the flags": if `--quant` was given
it is used, and if it was not, the error names the file and the reason before
telling the operator to pass `--quant`. A file we cannot read is a reason to ask,
never a reason to fail a download that already succeeded.

The reason has to be worked out, not quoted. A header that parses raises no
`GgufError`, so the two cases where the file was fine and only the ftype was not
— no `general.file_type` key at all, and one whose number maps to no known
quantisation — have no error text to borrow, and the message read `(...)` with
nothing between the brackets. Each now says which it is, and the unmapped case
names the number, since that is what the operator would have to look up.

**A resolution that fails takes the blob with it.** `resolve_fields` runs after
the download has landed, and can still fail — an unmapped ftype with no
`--quant` is the ordinary way. Nothing resumes a half-finished pull, so the
operator's next attempt re-downloads the file regardless and a multi-GB blob kept
for a manifest that was never built is pure disk cost on their own machine. It is
deleted and the message says so, the same disposal `verify` already does on a
hash mismatch.

**A conservative RAM floor, stated once.** With no `--min-ram-mb`,
`min_ram_mb = ceil(size_bytes / MiB × 1.15) + 512`. The weights are the floor,
not the cost: llama.cpp also holds the KV cache, the compute buffers and its own
scratch on top of the mapped file, and at the few-thousand-token contexts a desk
pilot runs that lands well inside a fifth of the weights at these sizes. 15%
covers it with room; the flat 512 MiB covers the runtime, the tokenizer and the
loader's peak, which do not scale with the file. Both numbers are deliberately
blunt, and biased high: this value is only ever compared against an agent's free
RAM, so erring high costs one skipped desk and erring low costs a swap storm on a
machine somebody is using. A long context or a large batch stays the operator's
to declare with `--min-ram-mb`, which always wins.

**VRAM is never guessed.** `min_vram_mb` stays `0` (CPU) unless the operator
passes `--min-vram-mb`. A non-zero floor here is precisely what makes ADR 048
prefer a GPU desk, and no fact in a GGUF header says a model should hold VRAM on
a machine somebody is working at. Declaring a GPU model is a deliberate act.

**A curated catalog, shipped inside the package.**
`packages/fallow-cli/src/fallow_cli/model_catalog.toml` carries four known-good
GGUFs (Qwen2.5 0.5B/1.5B/3B Instruct Q4_K_M and nomic-embed-text v1.5 Q4_K_M)
each with its `hf:` source, sha256, size, quant, worker kind, both minimums, a
licence name and a one-line note. `flw models pull --catalog <id>` resolves the
source, applies the metadata and verifies the download against the recorded
hash, refusing and deleting the blob on a mismatch.

It lives in the package rather than under `deploy/models/` because a coordinator
host installs the CLI and does not necessarily hold a checkout of this
repository: a catalog that only resolved relative to the source tree would be
missing on precisely the machine that runs `pull`. `deploy/` stays the home of
things a host runs from disk; this is data the CLI carries.

**Hashes are recorded, not invented.** Each `sha256` is the SHA-256 of the file
content, taken from the Hugging Face LFS object id for that path. `pull`
recomputes it from the bytes that landed and compares, so a wrong value here
fails a pull rather than mislabelling a blob: the failure mode is a refusal. The
smallest entry was downloaded and hashed to confirm the LFS oid is the content
digest before the other three were recorded the same way. An entry whose hash
nobody has confirmed leaves `sha256 = ""`; `pull` then registers the blob and
prints the digest it computed, and that value is what belongs in the file.

**Egress stays on the coordinator.** `pull` runs on the coordinator host, which
is the only machine that ever dials huggingface.co. Agents fetch blobs from the
coordinator's blob endpoint and are never given an internet source. A site with
no egress at all does not use `pull`: the file is downloaded elsewhere, carried
in, and registered with `flw models register --file`, which is unchanged. Both
paths are stated in the operator runbook §3 and in `deploy/README.md` §3.1.

## Verification

`packages/fallow-cli/tests/test_cli_gguf.py` drives the parser against headers
assembled byte by byte in `cli_helpers`: a v2 and a v3 header with a string
array and a float to skip over, an unknown and a missing `file_type`, a wrong
magic, a v1 file, an empty file, a truncation mid-string, a truncation hidden
behind a skipped array, an implausible KV count, and an unknown value type. The
min-RAM rule is asserted against the real 0.5B blob size that is in the catalog.

`test_cli_pull.py` covers `hf:` parsing including traversal, query, fragment,
double-revision, empty-owner, leading-dot and non-GGUF rejections; catalog
loading, an unknown id, an unknown field and a bad source; and the precedence
ladder (flags over catalog over file) plus the fallback when the header cannot
be read. The three ways a file yields no quantisation are told apart by their
messages — unreadable header, no `general.file_type` key, an ftype that maps to
nothing — and none of them may leave empty brackets. A failed resolution is
asserted to delete the blob and to keep its reason in the message; a successful
one to leave the file alone. Four tests drive the whole command through `CliRunner` with an injected
`MockTransport` serving a hand-built GGUF: an `hf:` pull, a catalog pull, a
sha256 mismatch (which must not register and must delete the blob), and an
unknown catalog id.

None of that touches the network. One test does, and only on request:
`FALLOW_LIVE_HF_TEST=1` pulls the smallest catalog entry end to end from
huggingface.co and asserts the manifest's hash and size match the catalog. It
skips by default.

## Compatibility

Additive. `flw models pull <url> --model-id … --family … --quant …` behaves
exactly as before; the three flags are now optional rather than required, so
commands that pass them are unaffected and commands that omit them get a derived
value or a message naming what is missing. `flw models register` is untouched.

## Exclusions and honest gaps

**No licence acceptance flow.** The catalog names a licence; it does not present
it, gate on it, or record an acceptance. Two of the four entries are Apache-2.0
and one is the Qwen Research licence, which is not. Reading the licence and
deciding whether a pilot may use the model remains entirely the operator's, and
nothing in this change should be read as having done it for them.

**No resume beyond what `blobs.py` already does, which is none.** An interrupted
`pull` restarts from zero. The agent-side model cache resumes (ADR 004); the CLI
download does not, and a 2 GB pull over a school link is where that will be felt
first. Adding `Range` resume to `download_to` is a contained change nobody has
needed yet. It is also why a failed pull deletes its blob rather than keeping it
for a retry: there is no retry that would use it.

**Single-file GGUFs only.** Qwen2.5 7B Instruct ships as two shards, and a
manifest holds one `file_name` and one `sha256`, so it is not in the catalog and
`pull` cannot stage it. Split GGUF support is a protocol question, not a CLI one.

**The catalog is a snapshot, not a feed.** Four entries, pinned to `main` rather
than to a commit, checked in by hand. If an upstream repository force-pushes a
file, the recorded hash stops matching and `pull` refuses, which is the right
failure, but the fix is a commit to this file, not a refresh command. Pinning
each entry to a commit sha would close that; it was left out because `main` is
what an operator reads off the model page and a mismatch is already loud.

**Nothing verifies the catalog against upstream.** No test asserts that the four
recorded hashes still match what huggingface.co serves. The live test proves one
entry, and only when someone runs it. A weekly job that checks all four is the
obvious next thing and is not here.
