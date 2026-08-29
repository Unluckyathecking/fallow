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
   `mmproj_file_name/sha256/size_bytes` (set together or not at all). The
   companion lives beside the main blob — on the coordinator's disk, in the
   agent's cache dir, and as `--mmproj` on the llama-server command line — so
   no registry schema, record type, or assignment logic changes.
3. **Self-contained one-page units.** `flw ocr prepare` renders documents to
   page images on the submitting machine; the chunker emits one unit per page
   whose input is `{"schema": "ocr-unit/1", "prompt_version": N,
   "image_b64": ...}`. Unit identity (`sha256(model_id ‖ chunker_version ‖
   input_hash)`) therefore covers the page bytes, the prompt revision, and the
   model — re-running under a new prompt or model never reuses stale results,
   and identical corpora dedup for free. Rendering client-side keeps the
   coordinator and agents free of document dependencies; the cost is shipping
   page images to the coordinator once, acceptable on a LAN.
4. **Quality warnings vs. infrastructure failures.** Transport/replica errors
   raise, so the lease machinery retries the unit elsewhere. Empty or
   truncated model output is recorded in `warnings` on a SUCCEEDED unit — a
   retry cannot fix it and a poison page must not burn `max_attempts`. Each
   result carries `confidence` (geometric-mean token probability when the
   replica returns logprobs) so review effort can be triaged downstream.

## Consequences

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
