# Data-policy sign-off — Fallow school pilot

A template for the deploying institution to record its own decision to run a Fallow
pilot. Fallow does not approve, certify, or endorse any deployment. This document is
not legal advice and is not a compliance attestation. It is a place for the
institution's own reviewers to write down what they decided and why, against the
technical scope Fallow actually supports.

Read [`docs/ai-act-scoping.md`](../ai-act-scoping.md) first. It states which uses
the pilot restricts itself to and which are out of scope. This sign-off assumes the
deployment stays inside that scope; if a use falls outside it, do not proceed on the
basis of this template.

Fill in every blank field below. Leave nothing implied.

---

## 1. Institution and owners

- Institution: __________________________
- Data-protection / DPO contact: __________________________
- Pilot owner (accountable): __________________________
- IT / security owner: __________________________
- Review date: __________________________
- Review period / expiry: __________________________

## 2. Intended use

State the specific use, and confirm it sits within the in-scope list in
[`ai-act-scoping.md`](../ai-act-scoping.md) (private RAG over institutional
documents, an internal coding assistant, or overnight indexing / embedding /
transcription of institution-owned material).

- Use being piloted: __________________________
- In-scope per `ai-act-scoping.md`? (yes / no): ____________
- Out-of-scope uses explicitly excluded (grading, admissions, behaviour monitoring
  or proctoring, profiling, emotion or biometric processing) — confirm excluded:
  ____________

## 3. Data

- Categories of data processed: __________________________
- Any personal data involved? (yes / no): ____________
- If yes, lawful basis and reference to existing policy: __________________________
- Retention of prompts, documents and outputs: __________________________
- Location of the coordinator and agents (all institution-controlled?): ____________

Note the technical baseline Fallow provides, to be verified locally, not assumed:
prompts, documents and model weights are intended to stay on institution-controlled
infrastructure; the coordinator keeps a per-request **metadata** log (`gateway.jsonl`
— the client key's name, the model and agent, timestamps and terminal status, plus a
prompt-length character count; **not** prompt text, document or response content, and
no end-user identity) and an agent-lifecycle event log (`events.jsonl`); a model can
be pulled fleet-wide with the kill switch (see the
[admin runbook](./admin-runbook.md)). Fallow has **not** had a production security
audit and is pre-alpha.

## 4. Risks and mitigations

- Identified risks: __________________________
- Mitigations and who owns them: __________________________
- Residual risk accepted by: __________________________

## 5. Decision

- Decision (approve pilot / decline / approve with conditions): ____________
- Conditions, if any: __________________________
- Signature: __________________________
- Name and role: __________________________
- Date: __________________________

---

This sign-off records an institutional decision only. It does not represent any
statement by the Fallow project, and running Fallow does not make a deployment
compliant with any law or standard.
