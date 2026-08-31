# ADR 105: OCR batch worker

Status: accepted · Date: 2026-08-29 · Module: fallow-protocol, fallow-coordinator/app, fallow-agent/workers, fallow-cli

## Context

The first mass-scale batch workload for Fallow is OCR of tens of thousands of
scanned exam pages (dense text plus mathematical notation) using a local
vision-language model. The platform already has everything hard: content-
addressed units, leases with expiry/requeue, model distribution with sha256
verification, VRAM-aware assignment, and suspend/resume preemption of
supervised replicas. What was missing was a `WorkerKind` for it, a way to turn
documents into units, and multimodal support in the replica launch path.

Two existing workers offered patterns. `EmbedWorker` dials a supervised
llama-server replica over local HTTP: the model arrives through the manifest
pipeline and preemption suspends the replica process. `TranscribeWorker` runs
faster-whisper in-process from a local config path: its model bypasses
manifest distribution, and the preemptor cannot suspend an in-process
computation.

## Decision

1. **`WorkerKind.OCR`, inference in a llama-server vision replica.** The OCR
   model is a normal `ModelManifest` (`worker_kind = "ocr"`), so distribution,
   hash verification, `min_vram_mb` assignment gating, and preemption all
   apply unchanged. The embed pattern, not the transcribe pattern: an
   in-process model could neither be suspended on user return nor yield the
   agent's event loop.
2. **Multimodal projector as manifest fields, not a second model.** A vision
   GGUF needs a companion `mmproj` file. The manifest gains
   `mmproj_file_name/sha256/size_bytes` (set together or not at all, and
   mandatory for an `ocr` model since it cannot serve a page without one). The
   companion lives beside the main blob — on the coordinator's disk, in the
   agent's cache dir, and as `--mmproj` on the llama-server command line — so
   no registry schema, record type, or assignment logic changes. Registration
   checks the declared companion is present beside the blob, like the blob
   itself.
3. **Self-contained one-page units.** `flw ocr prepare` renders documents to
   page images on the submitting machine; the chunker emits one unit per page
   whose input is `{"schema": "ocr-unit/1", "prompt_version": N,
   "page": "<source sha>-p<index>", "image_b64": ...}`. Unit identity
   (`sha256(model_id ‖ chunker_version ‖ input_hash)`) therefore covers the page
   bytes, the prompt revision, the model, and the page's stable name — re-running
   under a new prompt or model never reuses stale results, identical corpora
   dedup for free, and two byte-identical pages (repeated blanks) stay distinct
   units instead of collapsing into one through the queue's dedup. Rendering client-side keeps the
   coordinator and agents free of document dependencies; the cost is shipping
   page images to the coordinator once, acceptable on a LAN.
4. **Quality warnings vs. infrastructure failures.** Transport/replica errors
   raise, so the lease machinery retries the unit elsewhere. Empty or
   truncated model output is recorded in `warnings` on a SUCCEEDED unit — a
   retry cannot fix it and a poison page must not burn `max_attempts`. Each
   result carries `confidence` (geometric-mean token probability when the
   replica returns logprobs) so review effort can be triaged downstream.
5. **Results self-identify their page.** The worker echoes the unit's `page`
   into each `ocr-result/1` document. Unit `idx` follows the chunker's
   sorted-filename order, and page names are content-hashed, so idx order is
   unrelated to `corpus.json`'s document order — the echoed `page` is the only
   safe key for joining transcriptions back to their source pages.
6. **The OCR wire additions bump `PROTOCOL_VERSION` (1 → 2).** The `ocr` enum
   value and the optional `mmproj` manifest fields are shapes a pre-OCR agent
   cannot deserialize. Rather than advertise per-agent capabilities or gate
   assignment by version, the bump reuses the existing registration handshake: a
   v1 agent is refused against a v2 coordinator. This matches the v0.1 model —
   the fleet upgrades together (no in-place protocol negotiation), so an agent
   enrolls fresh at the coordinator's version. Fencing an agent that keeps its
   identity across an in-place coordinator upgrade would need graceful protocol
   rollout (versioned work leasing / snapshot invalidation); that is deferred.

## Consequences

- Splitting a corpus reads and base64-encodes every page, so `POST /jobs` runs
  the split on a worker thread (`asyncio.to_thread`); a tens-of-thousands-page
  OCR submit no longer stalls the coordinator's event loop.
- `payload_ref` is resolved on the coordinator host, exactly like a model's
  `blob_path`: v0.1 submits from the coordinator machine or a shared
  filesystem, and a corpus the coordinator cannot read is a 422. Streaming a
  prepared corpus to the coordinator at submit time is deferred (it spans every
  batch kind, not just OCR).
- `flw jobs fetch` writes a `.fallow-fetch.json` marker into its output
  directory and refuses to overwrite a directory without one, so a re-fetch
  replaces only directories it created and never clobbers a file that merely
  matches the result-name shape.

- Adding the kind touched exactly the enumerated switch points: the enum, the
  chunker, agent worker registration, and the schemas/Go codegen — scheduler,
  queue, upload, and preemption needed nothing.
- The mmproj sibling-file convention means `flw models register --mmproj`
  requires the companion in the same directory as the blob; `models pull` and
  the catalog do not stage mmproj files yet (register-only until a default OCR
  model is chosen).
- Result verification remains out of scope platform-wide (see architecture
  §Trust); replicated spot-checks stay a downstream concern.
- The Go agent gains only the generated protocol types; batch workers remain
  Python-agent-only.
