# MRIP — Build Roadmap

**Revision 3 · 2026-09-24** — organised around the problem statement's three
deliverables, on a production target.

## How this maps to SIH26023

The PS asks for the work to be "implemented in structured phases". Its phases are a
delivery narrative; the phases below are a **dependency order**. The mapping is exact:

| PS phase | Where it happens here |
|---|---|
| Requirement analysis | `docs/ARCHITECTURE.md` §0 and §3 — the deployment constraint is the requirement that drives everything else |
| Data digitization & pre-processing | Phases 1–2 (upload boundary, the six-stage pipeline, OCR, tables, spreadsheets) |
| Platform development | Phases 0, 3–6 (foundation, facts, then the three deliverables) |
| System testing | Every phase gate, run in CI — plus Phase 8's load, restore and authorisation matrices |
| Integration with CIL workflows | Phase 7 (connectors) and Phase 5's report templates, which are the actual workflow |
| Training | Phase 8.9 — operator runbook, user documentation, UAT with CMPDI officers |
| Continuous enhancement | The cross-cutting tracks; the gold corpus grows with every document class met |

And its three deliverables against the phases that build them:

| PS deliverable | Phase | State |
|---|---|---|
| **1. Automated Report Generation Platform** | Phase 5 | 🔄 **built** — templates, pinned manifests, publish lifecycle, four formats, reproduce-and-diff. Charts and the second template remain |
| **2. Word Cloud & Topic Identification** | Phase 6 | 🔄 **built** — deterministic TF-IDF, scope-aware cloud sized by document frequency, term→document drill-through. Embedding clustering remains optional and unbuilt |
| **3. AI-Based Query & Response System** | Phase 4 | 🔄 **built** — five intents routed, streaming narrative with numeral verification, structured refusals. The vector half of hybrid retrieval is still lexical-only |

Everything under them — the evidence store, the fact store with its traceability, the
conflict radar, identity and audit — is Phases 0–3, and is built.

Revision 1 ordered work so that a demo was runnable as early as possible. That ordering
is abandoned. Phases are now **dependency-ordered**: each one exists because the next
cannot be built correctly without it, and each ends at a **gate that is a test run, not a
screenshot**.

> **The rule that replaces "demo-first":** no phase is complete until its gate passes in
> CI. A feature that works on one machine is not done.

> **Where that rule currently stands.** The CI workflow exists
> (`.github/workflows/ci.yml`: ruff, `mypy --strict`, pytest against a real PostgreSQL
> service, a migration round-trip, a schema-drift check, a run with the model disabled,
> then `tsc`/eslint/`next build`, then a licence inventory) and **every one of those
> commands has been run locally and passes**. It has not yet run *on a CI server*, because
> this repository has no commits and no remote. Until it does, a ✅ below means "the gate's
> checks pass on a developer machine", which is weaker than what this rule asks for and is
> said here rather than implied.

---

## Current state

| Component | Lines | State |
|---|---|---|
| Unit normalizer (Indian numbering, ambiguous `MT`) | 432 | ✅ tested |
| Period normalizer (fiscal years, comparability refusal) | 331 | ✅ tested |
| Entity normalizer (CIL aliases, SCCL/NLCIL exclusion) | 326 | ✅ tested |
| Pydantic schemas (facts, evidence, conflicts, confidence) | ~400 | ✅ + production enums |
| FastAPI routers (documents, facts, normalize, system) | ~350 | ✅ scoped |
| Schema (`mrip/db/tables.py`) + baseline migration | ~600 | ✅ 9 tables, zero drift |
| Repositories (document / evidence / fact / conflict) + `Store` facade | ~900 | ✅ replaced DuckDB |
| Access scope (`mrip/auth/scope.py`) | ~140 | ✅ enforced by introspection test |
| Job queue, worker, scheduler (`mrip/jobs/`) | ~900 | ✅ SKIP LOCKED, lease-based |
| Identity: passwords, tokens, principal, service, user/audit repos | ~1,300 | ✅ Argon2id + JWT |
| Blob store (`mrip/blobs.py`) | 280 | ✅ content-addressed, atomic |
| Operator CLI (`mrip-admin`) | 400 | ✅ bootstrap without a default password |
| Upload boundary (`mrip/ingest/intake.py`) | 478 | ✅ refuses with a reason |
| Digitizer (`mrip/ingest/digitize.py`) | 395 | ✅ text, tables, sheets, OCR |
| Lifecycle + pipeline stages (`mrip/ingest/`) | 676 | ✅ six stages, idempotent |
| Metric lexicon + table extractor (`mrip/normalize`, `mrip/facts`) | 899 | ✅ refuses what it cannot resolve |
| Validation rules (`mrip/validate/rules.py`) | 325 | ✅ five domain rules |
| Backend total | **14,500** | ruff + mypy strict clean |
| Test suite | 5,100 | ✅ **515 passing in ~46 s** |
| Frontend (light theme, 9 surfaces, sign-in, upload, session proxy) | 5,300 | ✅ build green |

**515 tests green** against PostgreSQL 17.11 + pgvector 0.8.6. The normalizers and
schemas are storage-independent and carried forward untouched; `db.py` (664 lines of
DuckDB) was the one component the PostgreSQL decision rewrote, as ARCHITECTURE §12
predicted.

**Proven end to end, not only in tests.** A PDF uploaded over HTTP to a running stack is
hashed, sanitized, classified, digitized, extracted, normalized, validated and indexed by
`mrip-worker`, and the six resulting figures appear in the browser each citing its page,
table and cell — with the value the page literally printed shown beside the canonical
one.

---

## Phase 0 — Platform foundation ✅

The phase revision 1 didn't have. Everything after this assumes concurrency, identity and
migrations exist; retrofitting any of the three is a rewrite.

| # | Task | Notes |
|---|---|---|
| 0.1 | Monorepo, architecture, roadmap | ✅ done |
| 0.2 | Normalizers + schemas + test suite | ✅ done |
| 0.3 | **PostgreSQL 17 + pgvector, Alembic baseline migration** | ✅ done — forward-only, no boot-time DDL; round-trips up→down→up |
| 0.4 | **Repository layer** — split `Store` into document / evidence / fact / conflict repositories on SQLAlchemy Core | ✅ done — method signatures preserved, so the existing assertions carried over |
| 0.5 | Config profiles (`dev`/`test`/`prod`), typed settings, uniform error model, structured logging | ✅ done — `prod` refuses to boot on development defaults; structlog with contextvars, so every line inside a request or job carries its id, and secrets are redacted by a processor rather than by discipline |
| 0.6 | **Job queue** on `SELECT … FOR UPDATE SKIP LOCKED` — claim, heartbeat, retry with backoff, dead-letter | ✅ done — attempts counted at *claim* (a poison pill dead-letters instead of looping), lease-based claims, a lost lease cannot report an outcome, `dead` ≠ `failed`, idempotency keys |
| 0.7 | Worker + scheduler entrypoints; advisory lock on the scheduler | ✅ done — `mrip-worker` (thread pool, signal-handled drain) and `mrip-scheduler` (advisory-lock election, time-bucket idempotency keys, inline lease reclamation) |
| 0.8 | **Identity & scope** — local argon2id accounts, five roles, subsidiary scope through every repository method | ✅ done — Argon2id (OWASP profile), database-side lockout, JWT carrying *identity only* so a revoked grant or a demotion takes effect on the next request, session epoch for "sign out everywhere", `mrip-admin` for bootstrap. **OIDC/LDAP are a seam, not a branch** — `AuthSource` distinguishes them and a non-local account fails closed; the adapters land with a CIL IdP to test against (§Deferred) |
| 0.9 | **Append-only audit log**, `UPDATE`/`DELETE` revoked | ✅ done — immutability enforced by trigger (binds the owner too, unlike `REVOKE`); logins, failures, password changes, account changes and conflict resolutions all recorded, admin-only to read |
| 0.10 | `BlobStore` port + filesystem implementation | ✅ done — content-addressed (SHA-256 *is* the key), atomic (temp file + `fsync` + `os.replace`, so a power cut cannot leave a truncated PDF at a name that promises a whole one), immutable, streamed in 1 MiB chunks, re-verifiable. S3/MinIO is deferred (§Deferred) |
| 0.11 | `docker-compose`, `.env.example`, Dockerfiles | ✅ done — postgres, migrate, api, worker, scheduler, web + a tmpfs test database |
| 0.12 | **CI**: ruff, mypy strict, pytest on real Postgres, migration round-trip, drift check, `next build`, eslint, tsc, licence inventory | ✅ done |
| 0.13 | Frontend shell: light-theme tokens, primitives, API client, all five surfaces wired | ✅ done — dashboard, documents, facts, conflicts, normalizer |
| 0.14 | **Frontend session handling** | ✅ done — the token lives in an httpOnly cookie and `src/proxy.ts` turns it into a bearer header server-side, so no script in the browser can read a credential; sign-in is a Server Function, and a temporary password can reach only the change-password page |

**Status:** 515 tests green in ~39 s against PostgreSQL 17.11 + pgvector 0.8.6 ·
ruff clean · mypy strict clean on 48 modules · zero schema drift ·
`mrip-scheduler` elects itself and enqueues, `mrip-worker` drains ·
sign-in verified in a browser through Next → proxy → FastAPI → Postgres, with an
unauthenticated page redirected and an unauthenticated API call refused.

**Gate.** Migrations apply to an empty database and round-trip. A repository method
cannot read rows without a scope — enforced by an introspection test that enumerates
*every* public repository method and requires either a `scope` parameter or an
explicit registration with a written reason (prefix-matching was tried first and
already missed one hole). Two workers never claim one job — tested with two real
concurrent transactions, not a mock. `docker compose up` on a clean checkout yields a
working stack. CI green on all of the above.

**Deferred, with the reason.** Two items in this phase are *specified and seamed
but not implemented*, because implementing them now would mean shipping untested
code against a dependency this project cannot stand up:

- **OIDC and LDAP adapters.** `AuthSource` already distinguishes them, the schema
  refuses a non-local account with a password hash, and `authenticate()` fails
  closed on one. Writing the adapters without CIL's Active Directory or IdP to
  test against would produce plausible code whose first real contact is a
  deployment. The seam is the deliverable; the adapter follows the credential.
- **S3/MinIO blob store.** `BlobStore` is a five-method Protocol and
  `FilesystemBlobStore` implements it. The CIL deployment puts blobs on the same
  backed-up volume as the database, so object storage buys nothing here yet.

---

## Phase 1 — Document lifecycle ✅

Getting a file in, safely and exactly once.

| # | Task | State |
|---|---|---|
| 1.1 | Document registry: SHA-256 content addressing, dedup, version chains, supersession | ✅ the hash is of the bytes **after** sanitizing, so it identifies what the corpus holds rather than what was sent. Supersession is now reachable from the upload route: an officer names the document a correction replaces, it is validated under their own scope before any bytes are written (404 for one they cannot see — confirming it exists is disclosure), and the old version's facts retire to `superseded` in the same transaction. Retired rather than deleted, so "what did the earlier version say?" stays answerable. Until this landed the column and the repository method existed with no caller, and a reissued statement became an unlinked second document whose facts the conflict radar flagged as a disagreement between sources |
| 1.2 | Sensitivity labels (`public` / `internal` / `restricted`) | 🔄 stored and set at upload; they do not yet *narrow* access beyond entity scope, and ARCHITECTURE §9 says so rather than implying otherwise |
| 1.3 | **Upload boundary hardening** | ✅ type by magic number (the filename is the uploader's choice), size cap enforced mid-stream, page cap, encrypted PDF refused, `/OpenAction` + JavaScript + embedded files stripped **and verified gone**, archive expansion ratio and zip-slip refused |
| 1.4 | **Lifecycle state machine** | ✅ a table of legal transitions, not an ordering — `received → ready` raises rather than lying |
| 1.5 | **Per-stage idempotency** | ✅ each stage replaces its own output for its own document version; a re-run produces the same rows, asserted in `tests/test_ingest.py` |
| 1.6 | Per-stage resource ceilings (memory, wall clock) | ⬜ the database side exists (statement and lock timeouts); the per-stage ceiling does not |
| 1.7 | Progress reporting + pipeline health endpoint | ✅ counters written **outside** the stage's transaction, so "OCR 142/400" is visible while it runs — and now actually shown: the documents table renders the live counter as a bar beside the state badge, and polls only while a document is still moving. The claim was false until that landed; the counters were written and nothing read them, so a 400-page scan showed a static badge for minutes with no way to tell working from stuck, which is the state in which an officer re-uploads. Two fixes went with it: `set_state(progress=…)` **merged** rather than replaced (it was discarding the extraction skip report — "37 cells had no unit" — when the next stage reported its own count seconds later), and a failed document became recoverable on screen, since `POST /documents/{id}/retry` had existed with no caller |
| 1.8 | Retention policy + scheduled enforcement | ⬜ |
| 1.9 | **A dead ingestion job marks its document failed** | ✅ a job that gives up — a handler calling it permanent, or the attempts running out, whether reported by the worker or reclaimed by the scheduler — now moves its document to `failed` with the stage name and the error, in the same transaction that makes the job terminal. Before this, a dead `document.digitize` left its document at `classified` forever, indistinguishable from one merely slow on a 400-page report: nobody learned the upload had failed, and the figures it should have produced were quietly absent. Registered per job kind in `mrip/jobs/hooks.py` so the queue stays generic |

**Gate.** Killing a worker mid-ingest and resuming produces byte-identical evidence with
zero duplicates — tested by re-running a stage after forcing the state backwards. A zip
bomb, an encrypted PDF, a renamed spreadsheet and a corrupt archive each stop at the
boundary with a sentence naming what to do about it (`tests/test_intake.py`).

---

## Phase 2 — Digitization ✅ (OCR path unmeasured)

| # | Task | State |
|---|---|---|
| 2.1 | Document class detection (text-PDF / scanned / mixed / sheet / image) | ✅ **per page**, so a 200-page report with twelve scanned annexures OCRs twelve pages rather than all of them |
| 2.2 | PyMuPDF text spans → evidence with page + bbox | ✅ one row per *line*: a word makes an unreadable citation, a paragraph makes a useless highlight |
| 2.3 | RapidOCR path for scanned pages, per-span OCR confidence | 🔄 implemented, including the per-line confidence that becomes a fact's `ocr` stage. A deployment without the `ocr` extra **fails the document with that sentence** rather than ingesting it empty. Not yet measured on a scanned corpus |
| 2.4 | Page raster cache for click-to-source highlighting | ⬜ |
| 2.5 | Table extraction (pdfplumber lattice + stream) → cells + parse confidence | ✅ and the *strategy that found it* is recorded: a lattice table's structure is read from the document, a stream table's is inferred, and they carry different parse confidence |
| 2.6 | XLSX typed ingestion with source-cell provenance | ✅ real cell references (`C14`), read `data_only` so a formula yields the value the source system computed rather than one we recompute |
| 2.6b | **DOCX ingestion** — paragraphs and tables | ✅ the PS names "digital documents"; a Word note exists long before its PDF. No page number is claimed, because Word computes pagination at render time |
| 2.6c | **Standalone images** routed through the same OCR path | ✅ they were accepted at the boundary and then failed at digitize until `tests/test_architecture.py` made "every accepted class has a digitizer" a build failure |
| 2.7 | Low-confidence routing: below threshold → review, never silently accepted | ✅ its own stage, so lowering the threshold re-routes existing facts without re-reading a PDF |
| 2.8 | Optional VLM stage for hard pages, behind a capability flag | ⬜ |

**Gate.** A PDF with a ruled production table becomes a page-addressable evidence store
and then six facts, each citing `p.1 p1t1 r2c1` — verified end to end in
`tests/test_ingest.py` and by hand against a running stack. Zero GPU.

---

## Phase 3 — Facts 🔄

Where the product's claim is either true or it isn't.

| # | Task | State |
|---|---|---|
| 3.1 | Table → candidate facts: metric / entity / period inference | ✅ from the row label, the column header **or the table's caption** — the common CIL shape names the metric in neither row nor column. Merged cells and multi-row headers are not yet handled |
| 3.2 | Normalizers applied; canonical **and** raw value/unit both stored | ✅ `193.00` under a "(Figures in Million Tonnes)" caption becomes `193000000 t`, with `193.0 Million Tonnes` kept beside it |
| 3.3 | Evidence linkage enforced by a **foreign key** | ✅ composite FK to `documents(document_id, version)`; a fact with no citable source cannot be inserted |
| 3.4 | Validation rules | 🔄 five domain rules, each now keyed correctly after the hardening audit: negative values, implausible magnitude, production-vs-capacity (entity-level, since mine_or_block is not populated), offtake-vs-production (keyed on the exact period, not just the fiscal year), year-on-year swing (against the *adjacent* year). **Cross-total reconciliation** — the stated CIL total against the sum of its subsidiaries — is deliberately still unwritten: it needs the published total retained rather than dropped as a total row, and a rule that false-flags on incomplete subsidiary data would train reviewers to ignore it. Designed, not shipped half-formed |
| 3.5 | Conflict detection + resolution with the reviewer's identity and reason | ✅ resolution is audited with the reviewer's id; losers become `rejected`, not `superseded` |
| 3.5b | **Review queue is actionable** — validate / correct / reject a needs-review fact | ✅ `POST /facts/{id}/review`, reviewer-gated and audited, with a UI on the facts page. Previously a low-confidence fact could only leave the queue through a conflict, so the dashboard counted a queue nobody could clear |
| 3.6 | **Gold corpus** — 50+ public documents with ground-truth facts | ⬜ the one item that cannot be faked. The documents are in hand — 3,404 published ones, catalogued in `data/corpus/manifest.json` — so what is missing is the *ground truth*: a person transcribing the figures a document really states, independently of what the extractor read |
| 3.7 | **Accuracy harness in CI** with failing thresholds | ⬜ blocked on 3.6, and deliberately so: an accuracy number scored against our own reading of a document measures nothing |

**A note on what is *not* claimed.** No accuracy percentage appears anywhere in this
repository. It is not for want of real documents: the extractor is developed against
3,404 that Coal India, its subsidiaries, CMPDI, the Coal Controller and the Ministry of
Coal published, and the monthly-statement tests are transcribed cell for cell out of two
of them, OCR damage included. But a test we transcribed scores our own transcription, and
the corpus also holds scanned pages with no text objects at all, which are not read yet.
3.6 and 3.7 are what turn "correct on what we have tested" into a number.

---

## Phase 4 — **PS Deliverable 3**: AI query & response 🔄

The deliverable that most invites a chatbot, built so that it is not one. Design in
ARCHITECTURE §13.

| # | Task | Notes |
|---|---|---|
| 4.1 | **Intent router** — exact / comparison / discovery / narrative / draft | ✅ Routing on the question's *shape*, using the normalizers' own vocabularies: a question naming a metric, an entity and a period has a numeric answer and never reaches a model. Unsure → the conservative branch |
| 4.2 | **Exact + comparison routes with no model client in scope** | ✅ Enforced by an import test, not a convention (§7) |
| 4.3 | Lexical retrieval over `evidence.search_vector` | ✅ the discovery route queries it and ranks by `ts_rank`; the index is a **generated** column so it cannot drift |
| 4.4 | Chunking + local embeddings → pgvector HNSW | ⬜ Embeddings computed on the host; the `ml` extra, not the core |
| 4.5 | Hybrid fusion (reciprocal rank) | ⬜ Lexical alone misses "offtake" when the page says "despatch"; vector alone misses an exact mine name |
| 4.6 | **Narrative route** — Ollama + Qwen3 8B over retrieved passages and pinned facts | ✅ streaming, with Thinking mode off by default; citations restricted to what was passed in; a numeral not in the pinned facts is stripped and the answer flagged. Two holes on the *streaming* path are closed: the browser no longer falls back to the raw token buffer when the numeral check rejects every sentence (`done.prose || buffer` → `??`, and a fully-rejected answer now says so instead of rendering blank), and the audit row is written after the stream rather than before it — so a runtime that dies mid-answer is recorded as `stream_failed`, not as a delivered narrative, and the trail carries whether the check edited the prose |
| 4.7 | Refusal path — ambiguous unit, open conflict, out of corpus, no validated fact | ✅ Structured responses carrying the evidence that caused them, rendered as answers rather than errors |
| 4.8 | Evidence panel: answer → source page, region highlighted | 🔄 citations resolve to document, page, table and cell; the raster highlight waits on the page cache (2.4) |
| 4.9 | Gold query set (50+ labelled) + citation-accuracy KPI in CI | ⬜ blocked on the same ground truth as 3.6 |
| 4.10 | **Suggestions from the corpus**, not a fixed example list | ✅ `/query/suggestions` offers only questions the corpus can answer, scope-aware, prose intents gated on the model being reachable — a chip never leads to a refusal the user cannot distinguish from a broken product |

**Gate.** Citation accuracy ≥ 95% in CI. **Switch the model off entirely
(`MRIP_LLM_ENABLED=false`) and the exact-figure, comparison, search and report paths
still work** — asserted by a CI run with the backend disabled. Structured query p95 < 2 s.

**Local model, as installed:** Ollama 0.34 with `qwen3:8b` (Q4_K_M, 5.2 GB, Apache-2.0).
Pinned in the deployment manifest like any other dependency, and swappable without
touching the figure path — because the figure path has no model client to swap.

---

## Phase 5 — **PS Deliverable 1**: automated report generation 🔄

Design in ARCHITECTURE §11.

| # | Task | State |
|---|---|---|
| 5.1 | **Template engine** — declared required fields, charts, evidence blocks | ✅ YAML data, versioned; `{{entity}}`/`{{period}}` filled from a normalized context, so one template serves every subsidiary |
| 5.2 | Production Summary template → `.docx` with a sources appendix | ✅ the appendix lists documents rather than repeating one citation per figure. Server-rendered charts not yet drawn |
| 5.3 | Parliamentary / Administrative Response template | ✅ offtake is **required** rather than optional, the narrative leads and the table supports it, and the evidence appendix is not optional — the three things that make it a separate template rather than a flag |
| 5.4 | `.xlsx` and `.pptx` export | ✅ pure-Python, offline. Excel keeps canonical and printed values in separate columns, because a spreadsheet is where someone sums a column |
| 5.5 | **Evidence manifest** — pins every `fact_id`, `document_id@version`, template version, approver | ✅ stored whole as JSONB; `entity_id` lifted into an indexed column because every read is scoped |
| 5.6 | Approval workflow: draft → in_review → approved → published; published immutable | ✅ a table of legal transitions, not an ordering — draft cannot skip review and nothing leaves published. Sending a report back withdraws the approval with it |
| 5.7 | **Reproduce-and-diff** — re-render from an old manifest, diff against current facts | ✅ `reproduce()` needs no store at all; `diff()` re-resolves each figure and names the version that moved it |
| 5.8 | Click a number in the generated report → open its source page | 🔄 the locator travels into every format and the UI shows it; the page-raster highlight waits on 2.4 |
| 5.9 | A missing required field **fails loudly**; the generator never asks a model for a figure | ✅ enforced twice — `ReportIncompleteError` on render, and an import test that forbids the writers from reaching a model |
| 5.10 | Narrative sections are optional and checked | ✅ `reports/narrate.py` is the only module in `mrip/reports/` that may import a model, asserted by name in the architecture test. It reuses the query path's verifier rather than copying it, so "is this numeral supported?" has one definition |

**Gate.** Re-publish a report from a six-month-old manifest and get byte-identical
figures, with a diff naming every underlying document that has since been revised. Render
the same report with the model disabled and get every figure, table and chart, with the
narrative sections replaced by a stated note.

---

## Phase 6 — **PS Deliverable 2**: word cloud & topic identification 🔄

Deliberately compact. The value is the click-through, and overbuilding this module buys
nothing. Design in ARCHITECTURE §12.

| # | Task | State |
|---|---|---|
| 6.1 | **Deterministic keyphrase extraction** — TF-IDF over evidence text with a domain stoplist | ✅ no model, and no stemmer either: stemming would merge `mining` into `mine` and a reviewer would click a term to find documents that never contain it |
| 6.2 | Stored per document version as a job output | ✅ stored in `document_keyphrases`, replaced per version so a re-run produces the same table rather than a second copy |
| 6.3 | Optional embedding clustering into named topics (`ml` extra) | ⬜ pass 1 stands alone, which is the designed degradation |
| 6.4 | Word cloud with fiscal-year / subsidiary / document-class filters | ✅ filters are part of the query — narrowing to a year *re-weights* the cloud. Laid out in plain SVG rather than `d3-cloud`: no new dependency to licence-review or vendor offline |
| 6.5 | **Sized by document frequency**, raw count on hover | ✅ `COUNT(DISTINCT document_id)`, with the raw count kept beside it because the two answer different questions |
| 6.6 | Term → documents → pages → evidence drill-through | ✅ term → documents is a route and a test asserts no term is a dead end; the documents view carries it to the page |
| 6.7 | Longitudinal topic prevalence across fiscal years | ✅ `/topics/{term}/prevalence` |

**Gate.** Every term in the cloud reaches a specific page. No term is a dead end. The
cloud is built from the caller's scope — a term appearing only in another subsidiary's
unpublished filings does not leak the existence of those filings.

---

## Phase 7 — Integrations

| # | Task |
|---|---|
| 7.1 | Connector interface + contract tests (the tests are the specification) |
| 7.2 | OCBIS / MDMS / National Coal Portal adapters — mock implementation plus a real one enabled by config |
| 7.3 | Scheduled sync + reconciliation against ingested documents, with conflicts routed to the radar |
| 7.4 | Clear provenance labelling: a fact from a live feed is distinguishable from a fact from a PDF |

**Gate.** Contract tests pass against the mock. Enabling a real endpoint is a config
change and nothing else. No claim of live access appears in any UI or document until
credentials exist.

---

## Phase 8 — Release readiness

| # | Task |
|---|---|
| 8.1 | Authorisation matrix tests — every role × every route × every scope |
| 8.2 | Security pass: dependency audit, secret scanning, SQL/path-traversal review, rate limiting, signed short-lived document URLs |
| 8.3 | Audit log export + retention compliance |
| 8.4 | Observability: OpenTelemetry traces, Prometheus metrics, dashboards, alert rules — all local |
| 8.5 | **Backup, restore and a DR drill** — restore into a clean host and verify, not just `pg_dump` in cron |
| 8.6 | Load + soak test with p95 gates in CI |
| 8.7 | **Offline install bundle** — image, migrations, vendored wheels, model weights with checksums, upgrade and rollback runbook |
| 8.8 | Accessibility audit: WCAG 2.2 AA, keyboard-complete, colour-vision validated |
| 8.9 | Operator runbook + user documentation; UAT with CMPDI reporting officers |

**Gate.** A fresh Linux host goes from nothing to serving users using **only** the
offline bundle and the runbook, performed by someone who did not write the code.

---

## Phase 9 — Measuring what the PS asks to be quantified

The PS asks for report-time reduction, accuracy and automation "calculated in
percentage". This phase is how each number becomes real rather than asserted; the
definitions are in ARCHITECTURE §14.

| # | Task | Blocked on |
|---|---|---|
| 9.1 | **Automation rate** — published figures that needed no human touch, from `review_state` | Nothing; computable today |
| 9.2 | **Review burden** — facts routed to review per 100 extracted, by reason | Nothing |
| 9.3 | **Time to first answer**, p50/p95 by intent | Phase 4 |
| 9.4 | **Report preparation time**, upload → published | Phase 5, plus a **manual baseline recorded with CMPDI** |
| 9.5 | **Extraction + attribution accuracy** by document class | The gold corpus (3.6) |
| 9.6 | An operations dashboard where **every percentage names its denominator** | 9.1–9.5 |

**Gate.** Every figure quoted about this platform's performance is produced by a command
in this repository that anyone can re-run, against data that is not synthetic. A number
without a denominator does not ship.

---

## Cross-cutting tracks

These run continuously and are not phases. Work in them is scheduled inside every phase.

| Track | Standing obligation |
|---|---|
| **Security** | Every new route ships with an authorisation test. Every dependency addition is licence-checked in CI (see below). |
| **Accuracy regression** | The gold corpus grows with every document class encountered. Thresholds never move down. |
| **Accessibility** | Keyboard-complete and colour-vision-validated as written, not audited at the end. |
| **Documentation** | Architecture decisions land in the decision log with a date. |
| **Offline integrity** | No dependency that requires runtime network access survives review. |

### Licence policy

Permissive by default — **MIT, Apache-2.0, BSD, PSF**. **LGPL is acceptable for an
unmodified imported library**, which imposes no obligation on MRIP's own source.
**AGPL and plain GPL are not**, and CI fails the build on either.

The one LGPL dependency is recorded rather than glossed:

| Package | Licence | Why it is used anyway |
|---|---|---|
| `psycopg` (3.x) | LGPL-3.0-only | The reference PostgreSQL driver. Imported unmodified, so the licence reaches no MRIP code. The permissive alternative (`pg8000`, pure Python) is materially slower on the bulk evidence inserts that dominate ingestion. |

CI emits a full licence inventory as a build artifact on every commit, because a
government deployment gets asked what it runs and under what terms, and that
question should not become an archaeology exercise at handover.

---

## Sequencing rules

1. **Phases 0–3 are the critical path, and they are done.** The three PS deliverables
   sit on top of them: a report generator, a word cloud and a query system are each only
   as good as the fact store beneath, and an unauditable figure is fatal to all three.
   That is why the foundation was built first rather than the three demos.
2. **Deterministic core before anything generative.** If the LLM is removed entirely,
   Phases 1, 2, 3, 5 and the exact-figure query path must still work. This is enforced by
   a test, not a promise.
3. **No phase ships without its gate green in CI.**
4. **Schema-shaped concerns are never deferred.** Identity, scope and audit are Phase 0
   because they are columns and constraints, not features. This is the single biggest
   correction revision 2 makes to revision 1.
5. **Measured, not asserted.** Every number that appears in a presentation is produced by
   a command anyone can re-run.
