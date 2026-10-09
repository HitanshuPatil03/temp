# Evaluating MRIP

A guide for reviewers and judges. It assumes nothing about this codebase and aims to get
you to a populated, working system in about ten minutes — then tells you where to look to
check whether the claims in the README hold.

---

## 1. Run it

**You need:** Docker. Nothing else — not Python, not Node, not a database.

```bash
git clone <this repository> && cd SIHc
```

```bash
./run.sh
```

On Windows, from PowerShell: `.
un.ps1`

That is the whole of it. The script checks the host, picks free ports if 3000, 8000 or
5432 are already taken, builds the images, waits for the API to answer, seeds a
demonstration corpus through the **real pipeline**, and prints the accounts to sign in
with. Then open the address it gives you, usually <http://localhost:3000>.

If something is wrong it says what to do about it rather than failing with a stack trace —
"Docker is installed but the daemon is not responding" is a different problem from "port
5432 is in use", and the script distinguishes them. Four other commands exist:

```bash
./run.sh status     # what is running, on which ports
./run.sh logs api   # follow one service
./run.sh stop       # stop it, keep the data
./run.sh reset      # destroy the data and start clean (asks first)
```

### The accounts it prints

| Username | Role | Scope | What it shows you |
|---|---|---|---|
| `admin` | admin | all entities | Account administration and the audit trail |
| `hq.officer` | officer | all entities | Uploading; the whole corpus |
| `secl.officer` | officer | **SECL only** | The access model — a visibly shorter document list |
| `cmpdi.reviewer` | reviewer | all entities | Resolving the conflict |
| `ministry.viewer` | viewer | all entities | Read-only; the resolve button is refused |

**Password for all five: `sih-demo-2026-mrip`**

It meets the same policy a real account does — 12 characters, five distinct, not the
username. The policy is not relaxed for the demonstration, because an evaluator who saw a
weak password accepted would be right to wonder what else was relaxed.

### What the corpus is

Four synthetic documents — a production PDF, a **revised** version of it whose SECL figure
disagrees, an offtake spreadsheet and a Word note — each put through the same intake
boundary, the same six stages and the same extractor a genuine upload uses. Nothing is
inserted directly into the fact store.

> **The language model is optional and off the figure path.** Everything in section 2 —
> extraction, traceability, conflicts, search, access control — works with no model
> installed, and `./run.sh` says whether it found one. Ask a "why" question without it and
> the answer names the model as the reason and still shows you the figures; it does not
> pretend the corpus is empty. To enable narrative answers: `ollama pull qwen3:8b`.

<details>
<summary>Running the pieces by hand (for development, or if Docker cannot start)</summary>

Docker Desktop needs an elevated service, which some managed laptops will not start. The
same PostgreSQL 17 + pgvector runs inside WSL2 with no administrator rights:

```bash
wsl -d Ubuntu -u root -- bash /mnt/c/full/path/to/SIHc/infra/wsl-postgres-setup.sh
```

```bash
bash infra/wsl-db-env.sh
```

The second script writes `apps/api/.env` with the address WSL assigned. Re-run it after a
reboot — WSL changes that address on every boot.

Then the backend, the corpus, and the three processes plus the web dev server:

```bash
cp .env.example .env
cd apps/api && python -m pip install -e ".[dev]" && alembic upgrade head
```

```bash
mrip-admin seed-demo
```

```bash
python -m uvicorn mrip.main:app --reload --port 8000
```

```bash
mrip-worker
```

```bash
mrip-scheduler
```

```bash
cd apps/web && npm install && npm run dev
```

This needs Python 3.12+ and Node 22+. It is the right path for changing code, and the
wrong one for a ten-minute evaluation.
</details>

### Or real documents, if you have the bandwidth

`seed-demo` is synthetic on purpose: it is four small files that produce a *known* answer
and a *deliberate* conflict in seconds, which is what a walkthrough needs. The extractor
itself is developed against real ones — 3,404 documents Coal India, its subsidiaries,
CMPDI, the Coal Controller and the Ministry of Coal published, listed with their source
URLs and SHA-256s in [`data/corpus/manifest.json`](../data/corpus/manifest.json). The
documents are 25 GB and are not in this repository; the manifest is, and it rebuilds them:

```bash
mrip-admin fetch-corpus
```

```bash
mrip-admin ingest-corpus --limit 20
```

`ingest-corpus` puts them through the same six stages and prints, at the end, a breakdown
of every cell it **refused** and why. That breakdown is the point: on real government PDFs
the honest output of a stage is often a refusal, and a pipeline that never refuses is one
that guesses. README, "Real documents", records what these files changed in the code.

---

## 2. What to look at, and what it demonstrates

### The claim: every figure is traceable

Go to **Facts** and click any row. The panel on the right shows the canonical value, the
value **as the page printed it**, the document, page, table and cell, the extraction
method, and confidence as three separate numbers.

Now go to **Documents**, select the production PDF, and page to the cell. The figure is
there, in the source.

> **Try to break it:** there is no endpoint that returns a figure without evidence. The
> schema forbids it — `facts` carries a composite foreign key to `documents(document_id,
> version)` — so a fact with no citable source cannot be inserted at all.

### The claim: conflicts are never merged

**Conflict radar** shows one open conflict: SECL production for FY2024-25 is 193.00 Mt in
the provisional statement and 191.50 Mt in the revision. Both sides are shown with their
pages.

Sign in as `ministry.viewer` and try to resolve it — refused, because a viewer may not
adjudicate. Sign in as `cmpdi.reviewer` and resolve it: the losing figure becomes
**rejected** (a person judged it wrong), not *superseded* (a newer version replaced it),
and the audit trail records who chose which and why.

> **Try to break it:** there is no endpoint that averages the two, picks the newer one, or
> hides the disagreement.

### The claim: access is row-level, not route-level

Sign in as `secl.officer`. The document list and the facts are SECL's only — and the
sidebar says *"SECL"* under the username, so an empty list is explained rather than
mysterious.

> **Try to break it:** ask for another subsidiary's fact by id —
> `GET /api/facts/<id from the HQ view>` — and it is a 404, not a 403. A 403 on a specific
> id would confirm that id exists.

### The claim: the boundary refuses, with a reason

On **Documents**, try uploading:

| File | What happens |
|---|---|
| A password-protected PDF | Refused: *"…no text or table can be read from it. Upload an unprotected copy."* |
| A `.txt` renamed to `.pdf` | Refused by content, not by extension — the magic bytes decide |
| A zip that expands 1000× | Refused before a byte is decompressed |
| The same file twice | Accepted, and the second is a **no-op** — identical bytes are one document |

### The claim: extraction refuses rather than guesses

The production table has a **Total** row. It produced no fact — storing a total beside its
parts double-counts every aggregate. `GET /api/documents/<id>/progress` shows the skip
report: what was refused and why.

---

## 3. Verifying it rather than trusting it

```bash
cd apps/api && pytest
```

**672 tests**, against a real PostgreSQL — not a stand-in, because partial unique indexes,
composite foreign keys and the audit log's append-only trigger are not behaviours a mock
reproduces. Roughly seventy seconds.

CI runs the whole suite **twice**, once with the model enabled and once with
`MRIP_LLM_ENABLED=false`, because a deterministic path that quietly depends on the model
is the failure this architecture exists to prevent.

Tests worth reading rather than just running:

| File | What it pins down |
|---|---|
| `tests/test_ingest.py` | A PDF in, evidence-backed facts out — including that a killed stage re-run produces *identical* rows, not duplicates |
| `tests/test_scope.py` | Enumerates **every** public repository method and fails if one can read rows without an access scope |
| `tests/test_auth.py` | A failed login is indistinguishable from an unknown username; a revoked grant takes effect on the next request |
| `tests/test_intake.py` | Each hostile file is built in the test itself, so you can see exactly what is refused |
| `tests/test_architecture.py` | Fails the build if the figure path ever imports a model client |
| `tests/test_jobs.py` | Two workers never claim one job — proven with two real concurrent transactions |
| `tests/test_performance_statement.py` | Cells transcribed out of two of CIL's own monthly statements, OCR damage included — and the refusals those pages force |
| `tests/test_corpus_routing.py` | Holds the committed corpus manifest to its own provenance claim: one publisher name per company, a source URL and a hash on every entry |
| `tests/test_integrity.py` | The blob store is outside the database, so no foreign key protects a document's bytes. Each test breaks one thing and asserts `mrip-admin verify` notices |
| `tests/test_concurrent_signoff.py` | Two **real** connections proving a published report cannot be sent back to review by a second approver acting at the same moment |
| `tests/test_fact_acceptance.py` | What may be accepted without a human, and — more to the point — what may not: a figure whose unit was ambiguous never is, because `MT` is a factor of a million |
| `tests/test_query_degradation.py` | With the model off, a "why" question names the model as the reason and still returns the figures. It must not report your corpus as empty |

```bash
cd apps/api && ruff check . && mypy mrip
```

```bash
cd apps/web && npx tsc --noEmit && npx eslint . && npx next build
```

---

## 4. What is built, and what is not

Stated here rather than left for you to discover. All three problem-statement deliverables
are built; what follows is what is missing *within* them, and what is missing around them.

| Problem-statement deliverable | What is built | What is not |
|---|---|---|
| Report generation (§11) | Manifests persisted with every figure pinned to a `fact_id` and `document@version`; `draft → in_review → approved → published` with four-eyes separation on approval; `.docx` / `.xlsx` / `.pptx` / Markdown; reproduce-and-diff against the current corpus | Scheduled recurring reports; a template editor — templates are code |
| Word cloud & topics (§12) | Deterministic TF-IDF with a domain stoplist, scope-aware cloud sized by document frequency, term → documents → pages drill-through | Phrase extraction beyond unigrams and bigrams; topic clustering |
| AI query & response (§13) | Five intents routed; the exact-figure path reaches **no model at all**; streaming narrative with every number verified against the facts it was handed; structured refusals carrying their evidence | The **vector** half of hybrid retrieval — `pgvector` is installed and the column exists, but retrieval is lexical only |

Beyond the deliverables, and worth knowing before you judge the gaps:

- **Document bytes are not served.** A citation shows the snippet, page, table and cell
  from the database; there is no endpoint that hands you the original PDF. Signed
  short-lived URLs are roadmap 8.2.
- **`sensitivity` is recorded, not enforced.** The label is on every document and settable
  on upload, and **nothing reads it** — entity scope is the only access control. The
  docstring and the upload form both say so, and `tests/test_scope.py` pins it. What the
  three levels should *mean* is a policy decision for CMPDI, and a classification enforced
  the wrong way is worse than one honestly marked unenforced.
- **Read auditing.** Writes are audited exhaustively — every sign-in, upload, review,
  resolution, transition and query. Who *read* what is not yet recorded (ARCHITECTURE §9).
- **Backup and the DR drill.** The *verify* half exists — `mrip-admin verify` checks that
  every registered document's bytes are present, re-hashes them against their own names
  with `--deep`, and confirms every figure a stored report pinned still resolves. The
  backup and restore-drill halves are roadmap 8.5.

And no accuracy percentage appears anywhere in this repository. The extractor is not
short of real documents — `data/corpus/manifest.json` records 3,404 of them, published by
Coal India, its subsidiaries, CMPDI, the Coal Controller and the Ministry of Coal
(README, "Real documents"), and the monthly-statement tests above are transcribed cell
for cell out of two. But real is not the same as *labelled*: an accuracy percentage needs
ground truth to score against, and a percentage computed without it would be measuring
our own transcription rather than the extractor. `docs/ARCHITECTURE.md` §14 defines each
metric the problem statement asks for, its denominator, and what has to exist before it
can honestly be quoted.

Underneath all three is the part they stand on, and the part the tests above are mostly
about: ingestion, extraction into evidence-backed facts, normalization, validation,
conflict detection, identity with row-level scope, and an append-only audit trail.

---

## 5. Cleaning up

```bash
./run.sh reset
```

That stops the stack and destroys its volumes — the database and the blob store together,
which is the only correct way to remove them. They are one backup unit: a database
restored without its blobs leaves a corpus that answers every query and can substantiate
none of them, which is what `mrip-admin verify` exists to detect.

It asks for confirmation first. To leave the data in place and only stop the containers,
use `./run.sh stop`.
