# Evaluating MRIP

A guide for reviewers and judges. It assumes nothing about this codebase and aims to get
you to a populated, working system in about ten minutes — then tells you where to look to
check whether the claims in the README hold.

---

## 1. Run it

**You need:** Python 3.12+, Node 22+, and either Docker or WSL2 for PostgreSQL.

```bash
git clone <this repository> && cd SIHc
cp .env.example .env
```

### Database

```bash
docker compose up -d postgres
```

<details>
<summary>No Docker? (Windows, or a laptop where Docker Desktop cannot start)</summary>

The same PostgreSQL 17 + pgvector runs inside WSL2 without administrator rights:

```bash
wsl -d Ubuntu -u root -- bash /mnt/c/full/path/to/SIHc/infra/wsl-postgres-setup.sh
```

```bash
bash infra/wsl-db-env.sh
```

The second script writes `apps/api/.env` with the address WSL assigned. Re-run it after a
reboot — WSL changes that address on every boot.
</details>

### Backend

```bash
cd apps/api
python -m pip install -e ".[dev]"
alembic upgrade head
```

### A corpus to look at

```bash
mrip-admin seed-demo
```

This builds four synthetic documents — a production PDF, a **revised** version of it whose
SECL figure disagrees, an offtake spreadsheet and a Word note — and puts each one through
the **real pipeline**: the same intake boundary, the same six stages, the same extractor
that a genuine upload uses. Nothing is inserted directly into the fact store.

It prints the accounts it created. All of them share one password:

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

### The three processes and the UI

```bash
python -m uvicorn mrip.main:app --port 8000
```

```bash
mrip-worker
```

```bash
cd apps/web && npm install && npm run dev
```

Open <http://localhost:3000> and sign in as any account above.

> **The language model is optional and off the figure path.** Everything described below —
> extraction, traceability, conflicts, search, access control — works with no model
> installed. To enable narrative answers later: `ollama pull qwen3:8b`.

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

**388 tests**, against a real PostgreSQL — not a stand-in, because partial unique indexes,
composite foreign keys and the audit log's append-only trigger are not behaviours a mock
reproduces. Roughly thirty seconds.

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

```bash
cd apps/api && ruff check . && mypy mrip
```

```bash
cd apps/web && npx tsc --noEmit && npx eslint . && npx next build
```

---

## 4. What is **not** built yet

Stated here rather than left for you to discover:

| Problem-statement deliverable | State |
|---|---|
| Report generation | **Designed, not built** — `docs/ARCHITECTURE.md` §11 |
| Word cloud & topics | **Designed, not built** — §12 |
| AI query & response | **Designed, not built** — §13. The lexical index it reads is built and live |

And no accuracy percentage appears anywhere in this repository. The extractor is not
short of real documents — `data/corpus/manifest.json` records 3,404 of them, published by
Coal India, its subsidiaries, CMPDI, the Coal Controller and the Ministry of Coal
(README, "Real documents"), and the monthly-statement tests above are transcribed cell
for cell out of two. But real is not the same as *labelled*: an accuracy percentage needs
ground truth to score against, and a percentage computed without it would be measuring
our own transcription rather than the extractor. `docs/ARCHITECTURE.md` §14 defines each
metric the problem statement asks for, its denominator, and what has to exist before it
can honestly be quoted.

What *is* built is the part all three deliverables stand on: ingestion, extraction into
evidence-backed facts, normalization, validation, conflict detection, identity with
row-level scope, and an append-only audit trail — with the tests above as the evidence.

---

## 5. Cleaning up

```bash
docker compose down -v
```

The blob store lives in `data/blobs` and is not in git; delete the directory to remove the
seeded documents' bytes.
