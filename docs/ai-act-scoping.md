# EU AI Act scoping (v0.1 pilot)

Fallow is deployment infrastructure for self-hosted open-weight models. The pilot
deliberately restricts itself to uses that are **not** high-risk under the EU AI Act:

**In scope (pilot):**
- Private RAG search over institutional policy documents and handbooks
- Internal coding assistant for staff/students in CS labs
- Overnight document indexing, embedding, transcription of institution-owned material

**Explicitly out of scope — do not build, do not enable:**
- Anything influencing **grading, assessment, or educational progression decisions**
- **Admissions** or enrolment decisions
- **Behaviour monitoring, proctoring, or profiling** of students or staff
- Emotion recognition; biometric anything

Education is a named sensitive domain (Annex III): systems determining access to education
or evaluating learning outcomes can be high-risk. The pilot avoids that category entirely
rather than attempting compliance with it.

**What Fallow provides that helps the deploying organisation:**
- A model inventory (manifest registry: source, license, hash, permitted use)
- Per-request audit records (which model, which policy, when, for whom)
- Central kill switch (deregister a model fleet-wide)
- Data locality: prompts, documents and weights never leave the organisation

Fallow is not marketed as "AI Act compliance". It is infrastructure that makes controlled,
documented, inspectable AI deployment easier.
