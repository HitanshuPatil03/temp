# Datasets & sources

Every figure MRIP produces traces back to a document a real organisation published.
This file lists the eleven publishers the corpus is harvested from, with their official
sources, and explains how provenance is recorded and verified.

The documents themselves — **3,404 files, 25.4 GB** — are **not** committed to this
repository (25 GB of public PDFs are not ours to redistribute, and git is the wrong place
for them). What *is* committed is the provenance record,
[`data/corpus/manifest.json`](../data/corpus/manifest.json): for every file it records the
publisher, the title, the **URL it was fetched from**, its **SHA-256**, its size, its media
type and when it was retrieved. That manifest is how anyone checks these figures came off
documents Coal India actually published, and it rebuilds the same corpus on another machine.

---

## The eleven publishers

Counts are files as harvested; the same document is sometimes published on both a
subsidiary's own site and on `coalindia.in`, so **3,404 files are 3,071 distinct documents**
(see "Provenance" below).

| Publisher | Code | Files | Official source |
|---|---|---:|---|
| Western Coalfields Limited | `wcl` | 1,125 | <https://www.westerncoal.in/> |
| Coal India Limited | `cil` | 771 | <https://www.coalindia.in/> |
| Mahanadi Coalfields Limited | `mcl` | 486 | <https://www.mahanadicoal.in/> |
| Ministry of Coal | `moc` | 418 | <https://coal.gov.in/> |
| Bharat Coking Coal Limited | `bccl` | 240 | <https://www.bcclweb.in/> |
| Central Mine Planning & Design Institute | `cmpdi` | 130 | <https://www.cmpdi.co.in/> |
| South Eastern Coalfields Limited | `secl` | 97 | <https://secl-cil.in/> |
| Coal Controller's Organisation | `coalcontroller` | 47 | <https://www.coalcontroller.gov.in/> |
| Central Coalfields Limited | `ccl` | 33 | <https://www.centralcoalfields.in/> |
| Eastern Coalfields Limited | `ecl` | 29 | <https://www.easterncoal.co.in/> |
| Northern Coalfields Limited | `ncl` | 28 | <https://www.nclcil.in/> |

**What kind of documents:** monthly production and offtake statements, provisional coal
statistics, annual reports and accounts, and performance statements — **3,393 PDFs and 11
spreadsheets**.

---

## How the corpus is harvested

```bash
mrip-admin fetch-corpus                 # crawl and download, resumable
```

- **One request per second per host**, `robots.txt` obeyed, backing off when a WAF starts
  serving CAPTCHAs instead of pages.
- **Filed by the company each document is about, not the page it was linked from.** CIL's
  annual-reports page carries every subsidiary's annual report, so filing by the linking
  page would defeat a company-wise corpus. The router re-files each document from its own
  URL; `apps/api/mrip/corpus.py` holds the routing table.
- **Media type by magic number, not by extension** — a `.pdf` that is really an HTML error
  page is recorded as what it is.

```bash
mrip-admin ingest-corpus --limit 20     # put them through all six pipeline stages
```

---

## Provenance — how the manifest is kept honest

`data/corpus/manifest.json` is held to its own rules by
[`apps/api/tests/test_corpus_routing.py`](../apps/api/tests/test_corpus_routing.py), which
runs on a fresh clone against the committed manifest:

- **One publisher name per company** — no document is filed under a subsidiary but stamped
  with the parent's name.
- **A source URL and a SHA-256 on every entry** — an entry with neither is not a provenance
  record.
- **No URL fetched twice** — two entries for one address would mean the harvester counted a
  single download as two documents.
- **Two different documents never share a hash** — the same *bytes* may appear under two
  companies (a subsidiary report republished on `coalindia.in`), but two *different* files
  sharing a hash would mean the record cannot tell them apart.

Because the intake boundary keys every file on the SHA-256 of its sanitized bytes,
re-uploading or re-harvesting the same document is a no-op — which is why 3,404 fetched
files collapse to **3,071 distinct documents** (20.7 GB distinct). The manifest records
every address rather than quietly dropping one; the 57 files that appear under two companies
are the copies `coalindia.in` republishes at paths carrying no subsidiary marker, and that
limit of URL-based routing is counted here rather than hidden.

---

## What the real corpus is measured to contain

A census over the whole corpus (not a sample):

- **187,424 pages** across the 3,060 distinct PDFs.
- **34,427 pages (18%) are true scans** with no text objects at all; 1,256 PDFs are scans
  end to end, and 521 more carry a recognised (saved-back OCR) layer on at least one page.
  Those scans are **not read yet** — recovering them needs OCR-with-geometry, stated as
  unfinished rather than counted as coverage.
- **96 documents** defeat the reader outright; they are reported as failed, and because
  every stage commits its own output, a failed document never corrupts a run.

See the README's "Real documents" section for what these files changed in the extractor.

---

## Note on scope

The seven CIL coal subsidiaries are ECL, BCCL, CCL, NCL, WCL, SECL and MCL, plus CMPDI and
the North Eastern Coalfields unit. **SCCL and NLCIL are not CIL subsidiaries**, and the
entity normalizer says so explicitly rather than resolving them into the group.
