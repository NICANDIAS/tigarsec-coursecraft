# TIGARSEC Assess — System Architecture

**Status:** Design v1 — pre-build
**Owner:** Abdulhakeem Ekun / TIGARSEC Innovations
**Stack alignment:** FastAPI, SQLAlchemy, PostgreSQL, Docker Compose, Nginx, JWT/bcrypt (consistent with TIGARSEC CTF and SOCaaS builds)

---

## 1. Design Principles (read before building anything)

1. **Never regenerate a document — only annotate it.** The single most important constraint in this whole system. Every module that touches student work must be read-only on content, write-only on annotations (comments/track-changes), operating on the original file's XML structure.
2. **Human-in-the-loop is the product, not a fallback.** The AI proposes; the teacher disposes. Confidence scoring and a review queue are core features, not a "v2 nice-to-have."
3. **Build the vertical slice before the abstraction.** One working path (spec → essay marking → override capture) before a generic plugin framework — same lesson you already learned on VERITAS.
4. **Everything institution-facing must be explainable.** Every mark must trace back to: which criterion, which evidence in the text, what confidence, whether a human overrode it. This is your defence if a grade is ever challenged or moderated.
5. **Local-first, cloud-optional.** Runs standalone on a single college server on day one. Multi-tenant/hosted is a later phase, not a prerequisite.

---

## 2. High-Level Component Map

```
                          ┌─────────────────────────┐
                          │   Nginx (reverse proxy)  │
                          │   TLS termination, /app  │
                          └────────────┬─────────────┘
                                       │
                 ┌─────────────────────┼─────────────────────┐
                 │                     │                     │
        ┌────────▼────────┐  ┌────────▼─────────┐  ┌────────▼────────┐
        │   Web UI         │  │   API (FastAPI)  │  │  Admin/Moderation│
        │ (Jinja2/Tailwind │  │  REST endpoints  │  │  Dashboard       │
        │  or React later) │  │  Auth (JWT)      │  │                  │
        └──────────────────┘  └────────┬─────────┘  └──────────────────┘
                                        │
                     ┌──────────────────┼───────────────────────┐
                     │                  │                       │
            ┌────────▼───────┐ ┌────────▼────────┐   ┌──────────▼──────────┐
            │ Spec Ingestion  │ │  Marking Engine  │   │  Document Annotator │
            │ Service         │ │  (job dispatcher)│   │  Service            │
            └────────┬────────┘ └────────┬─────────┘   └──────────┬──────────┘
                     │                   │                        │
                     │          ┌────────▼─────────┐               │
                     │          │  Job Queue        │               │
                     │          │  (Redis + RQ/     │               │
                     │          │   Celery worker)  │               │
                     │          └────────┬─────────┘               │
                     │                   │                         │
                     │       ┌───────────▼────────────┐            │
                     │       │  Marking Modules        │            │
                     │       │  - essay_marker         │            │
                     │       │  - network_config_marker│            │
                     │       │  - mcq_marker           │◄───────────┘
                     │       │  (contract-based, see §5)│
                     │       └───────────┬────────────┘
                     │                   │
                     │          ┌────────▼─────────┐
                     │          │  LLM Gateway      │
                     │          │  (provider-       │
                     │          │   abstracted)     │
                     │          └───────────────────┘
                     │
            ┌────────▼────────┐        ┌──────────────────┐       ┌──────────────────┐
            │  PostgreSQL      │        │  Object Storage   │       │  Audit Log Store  │
            │  (structured     │        │  (uploaded docs,  │       │  (append-only)    │
            │   data, §4)      │        │   annotated docs) │       │                    │
            └──────────────────┘        └──────────────────┘       └──────────────────┘
```

**Why a job queue from day one:** marking a class set of 30 essays is not a request/response operation — it's a batch job. If you build the API to mark synchronously, the UI will hang or time out the moment you scale past a handful of submissions. Redis + RQ (simpler than Celery, fits a single-server deployment) gives you async jobs, retries, and progress tracking for free.

---

## 3. Tech Stack Decisions

| Layer | Choice | Why |
|---|---|---|
| API framework | FastAPI | Already your stack; async support matters here for LLM calls |
| DB | PostgreSQL (not SQLite) | You'll have concurrent writes (multiple teachers, background jobs) — SQLite's single-writer lock will bite you exactly like it would in the CTF platform if the college scales usage |
| ORM | SQLAlchemy | Consistency with existing projects |
| Job queue | Redis + RQ | Lightweight, easy to reason about, sufficient for single-college scale; Celery only if you outgrow this |
| Document handling | `python-docx` + direct OOXML manipulation for comment insertion | See §6 |
| File storage | Local volume initially → S3-compatible (MinIO) if multi-college | Keeps you Docker-Compose-simple at first |
| Auth | JWT + bcrypt | Matches CTF platform pattern |
| LLM access | Abstracted gateway, not hardcoded to one provider | See §7 — you don't want to be locked in, and cost/latency tradeoffs will matter at scale |
| Deployment | Docker Compose, Nginx reverse proxy | Matches SOCaaS pattern; known quantity for you |

---

## 4. Data Model (core entities)

```
Institution
 └─ Course
     └─ SpecVersion (versioned! specs change — see §9)
         └─ Unit
             └─ LearningOutcome
                 └─ Criterion (Pass/Merit/Distinction descriptor, or mark band)

Teacher
 └─ StyleProfile (feedback exemplars, tone parameters)
 └─ TeacherOverride (per-mark corrections — feeds calibration loop)

SchemeOfWork
 └─ maps Lesson → LearningOutcome (optional, enriches auto-tagging)

Student
 └─ Submission
     ├─ original_file (immutable, stored once, never touched)
     ├─ submission_type (essay | network_config | mcq | report)
     └─ MarkingRun
         ├─ status (queued | in_progress | needs_review | approved | released)
         ├─ CriterionMark (per LO/criterion: ai_mark, confidence, evidence_span, final_mark)
         ├─ Annotation (comment text, anchor location in original doc, style-matched)
         └─ AuditLogEntry (who/what/when — AI proposal vs teacher decision, immutable)
```

**Key design choices:**
- `SpecVersion` is versioned, not a single row — Pearson updates specs, and every submission must be marked against the version that was in force at the time. Never overwrite a spec in place.
- `original_file` is write-once, content-hashed on upload. This is your integrity guarantee: you can always prove the annotated file's body text matches the hash of what the student submitted.
- `TeacherOverride` is the calibration data — this is literally the "gets better with use" mechanism from your earlier question. Store `(criterion_id, ai_mark, ai_confidence, final_mark, comment_diff, timestamp)` for every correction.
- `AuditLogEntry` is append-only (no updates, no deletes) — this is what protects you if a grade is challenged or a moderator asks how a mark was reached.

---

## 5. Marking Module Contract (plugin interface)

Same philosophy as your VERITAS adapter contract — define the interface once, keep modules swappable, don't special-case any one module in the core.

```python
class MarkingModule(Protocol):
    submission_type: str  # "essay", "network_config", "mcq", "report"

    def extract(self, file_path: str) -> ExtractedContent:
        """Read-only extraction. Never mutates the source file."""

    def map_to_criteria(
        self, content: ExtractedContent, spec: SpecVersion
    ) -> list[CriterionEvidence]:
        """Match content spans to LOs/criteria via retrieval (embeddings)."""

    def mark(
        self, evidence: list[CriterionEvidence], style_profile: StyleProfile
    ) -> list[CriterionMark]:
        """Produce mark + justification + confidence per criterion."""

    def annotate(
        self, original_file: str, marks: list[CriterionMark]
    ) -> AnnotatedFile:
        """Insert comments/corrections into the ORIGINAL file. No regeneration."""
```

**First module to build:** `essay_marker` (text-based reports/essays — covers most T-Level written coursework).
**Second module:** `network_config_marker` for Packet Tracer — this one is actually easier to get *reliably accurate* than essay marking, because `.pkt` files are structured XML under the hood. You can do deterministic checks (expected VLANs, IPs, routing tables present/correct) rather than probabilistic judgment — closer to your CTF flag-checker logic than to essay marking. Good second target because it builds your confidence in the pipeline on a lower-risk module first.
**Third+:** MCQ marker (trivial — deterministic), then diagrams/screenshots (flag-for-review only, don't oversell automation here).

---

## 6. Document Annotation Service (the non-negotiable part)

This is its own isolated service because getting it wrong breaks trust in the whole tool.

**Pipeline:**
1. Load original `.docx` via `python-docx` (or raw `zipfile` + XML manipulation for anything python-docx doesn't expose cleanly — Word comments specifically often need direct XML work since python-docx's native comment support is limited).
2. Build a **text-offset map**: walk every run in every paragraph, record `(paragraph_index, run_index, char_start, char_end)` against a flattened plain-text version. This is what lets you translate "the AI flagged characters 1400–1550" back into "insert a comment anchored to paragraph 12, run 3" without touching formatting.
3. AI evidence spans (from `map_to_criteria`) are matched against this offset map.
4. Comments are inserted via OOXML `w:commentRangeStart` / `w:commentRangeEnd` / `w:comment` elements referencing the matched runs — paragraph properties (`w:pPr`, including justification/alignment) are never touched.
5. Save as the *same file* (same filename, optionally versioned internally for audit purposes, but presented to the teacher as one document).

**Test this early and adversarially** before trusting it on a full class set: tables, embedded images, existing tracked changes, multi-column layouts, footnotes. Formatting edge cases are exactly where "the paragraph lost its justification" bugs will hide.

---

## 7. LLM Gateway (provider abstraction)

Don't call a provider's SDK directly from marking modules. Put a thin gateway in between:

```python
class LLMGateway:
    def complete(self, prompt: str, schema: type[BaseModel] | None = None) -> LLMResponse:
        ...
```

**Why this matters at your scale of ambition, not just now:**
- Cost control — marking is a high-volume, repeated-structure task; you'll want to route to cheaper models for routine marking and reserve stronger ones for borderline/low-confidence cases.
- Data governance — if the college requires marking to happen on infrastructure with specific data residency or no-training-on-data guarantees, you swap the backend without touching marking logic.
- Structured output — force JSON-schema-constrained responses (mark, evidence span, confidence, comment text) so a malformed response fails loudly instead of silently corrupting a mark.

---

## 8. Security & Data Protection

This is student data — UK GDPR applies, and Pearson/BTEC moderation integrity is a live institutional concern (from your earlier question about disclosure).

- **Auth:** JWT with role-based access (teacher / course-lead-moderator / admin). Teachers only see their own courses' data by default; course-lead role needed for moderation sampling across a cohort.
- **Encryption at rest:** student submissions and marks are sensitive — encrypt the object storage volume, not just rely on filesystem permissions.
- **Data minimisation:** don't send more than necessary to the LLM gateway — strip student names/identifiers before sending content out for marking where possible, rehydrate identity only inside your own DB.
- **Audit log is immutable and complete:** every AI proposal and every teacher override, timestamped, non-deletable. This is your evidence trail if a grade is challenged.
- **Network isolation lesson from your CTF deployment:** confirm with IT early whether staff devices on the college network can actually reach a self-hosted server on a different VLAN — don't assume, you already got burned by this once.
- **Retention policy:** decide up front how long marked submissions and AI logs are retained, and make it configurable per institution — this will come up the moment you present it to Dudley or pitch it wider.

---

## 9. Durability & Versioning

- **SpecVersion immutability:** never edit a spec in place once a submission has been marked against it. New spec release → new `SpecVersion` row, old submissions keep their original reference.
- **Backups:** Postgres dump + object storage backup on a schedule (even a simple cron + `pg_dump` to a separate volume is fine at single-college scale — don't over-engineer this early).
- **Idempotent marking jobs:** a job that fails partway (LLM timeout, worker crash) must be safely re-runnable without double-marking or corrupting the annotated file. Store job state transitions, not just a final status.

---

## 10. Scalability Path

| Stage | Setup |
|---|---|
| **Now — single college** | Docker Compose on one server: Postgres, Redis, API, worker, Nginx. Matches your SOCaaS pattern exactly. |
| **Multi-department, same college** | Same stack, just more teacher accounts + course scoping. No architecture change needed — this is what the data model's Institution/Course scoping is already built for. |
| **Multi-college (if you commercialise)** | Object storage moves to MinIO/S3-compatible for shared access; consider moving from single-server Compose to a small orchestration (Docker Swarm or lightweight k8s) only once you have evidence of real multi-tenant load — don't build this prematurely. |

---

## 11. Integration Layer (Teams etc.)

Kept as a **separate, optional module** — not core, because it has an external dependency (Azure AD admin consent) the rest of the system doesn't need.

- **Phase 1 (build now):** one-click "download annotated original" — teacher uploads manually to Teams. Zero external dependency, ships immediately.
- **Phase 2 (pitch to IT later):** Microsoft Graph Education API integration for full round-trip (pull submissions, push grades). Requires institutional Azure AD app registration and admin consent — treat as a separate workstream once you have a working v1 demo to justify the ask.

---

## 12. Learning Loop (recap, now placed architecturally)

- `TeacherOverride` table is written every time a mark or comment is edited.
- A lightweight **calibration job** (batch, not real-time) periodically re-derives few-shot exemplars per teacher from their own override history — feeds back into the `mark()` step of the relevant module.
- Cross-teacher aggregation (only if/when multi-teacher) stays limited to **pattern-level signals** (which criteria get overridden most, confidence miscalibration) — never raw student content shared across teachers/institutions without explicit data-sharing agreement.

---

## 13. Build Order (recap, tightened)

1. Data model + spec ingestion (parse one real Pearson spec end-to-end)
2. Essay marker module (extract → map → mark → confidence)
3. Document annotator service (this is the part to test hardest before trusting it)
4. Review UI (approve/override, capture `TeacherOverride`)
5. One-click annotated-file output
6. Network config (Packet Tracer) marker — second module, proves the contract generalises
7. Calibration loop using accumulated overrides
8. Teams manual-export polish → Graph API integration (institutional conversation)
9. Moderation/IV sampling dashboard

---

*This document is meant to be revised as you build — treat mismatches between this design and what you actually build as signal, not as a spec to force reality into.*
