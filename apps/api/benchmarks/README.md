# Query plans at corpus scale

Three SQL files that answer one question: **do the indexes on `facts` cover the
queries the product actually runs, at the volume the real corpus will reach?**

They are not a test. Nothing here asserts a timing — a timing assertion fails on
a loaded laptop and passes on an idle server, which teaches a team to ignore it.
What these do is let you *read the plan* and see whether the planner found an
index or gave up and read the table.

## Running them

Needs a throwaway database on a reachable PostgreSQL. The scratch database is the
point: seeding a million rows into a database anyone is using would be rude, and
`TRUNCATE` on the wrong one is worse.

```bash
export PGPASSWORD=mrip_dev_only
psql -h 127.0.0.1 -U mrip -d postgres -c 'CREATE DATABASE mrip_bench'
MRIP_DATABASE_URL="postgresql+psycopg://mrip:mrip_dev_only@127.0.0.1:5432/mrip_bench" alembic upgrade head
psql -h 127.0.0.1 -U mrip -d mrip_bench -f benchmarks/seed.sql
psql -h 127.0.0.1 -U mrip -d mrip_bench -f benchmarks/plans.sql
psql -h 127.0.0.1 -U mrip -d postgres -c 'DROP DATABASE mrip_bench'
```

`series-index.sql` is the before/after for one specific index and builds it
itself; run it on a freshly seeded database.

## What the seed does, and the trap it fell into twice

`seed.sql` writes 3,404 documents — the real catalogued corpus size, from
`data/corpus/manifest.json` — and a million facts, about 300 per document. The
cardinalities mirror the lexicons: 29 entities, 20 metrics, 6 fiscal years, and a
period mix of one full fiscal year per cycle of twelve with months and cumulatives
inside it, because that mix is what the CIL monthly statements actually look like
and what makes the full-year filter in `entity_metric_series` necessary.

The dimensions are a **mixed-radix decomposition of the row number**, which is
what keeps them independent. Two earlier versions of the file did not bother, and
each measured nothing while looking perfectly fine:

- metric from `g % 20` and the full-year flag from `g % 12` — one needs an odd
  `g`, the other an even one, so **no row was ever both**;
- entity from `g % 30` and metric from `g % 20` — they share a factor of ten, so
  only a tenth of the `(entity, metric)` pairs existed.

Both produced confident plans reporting the cost of finding zero rows. The tail of
`seed.sql` now asserts the combinations the plans query, so a future version that
reintroduces a correlation says so instead of quietly reporting good news.

## What it found

Measured on 1,000,000 facts, PostgreSQL 17, three parallel workers.

| Query | Plan | Time |
|---|---|---|
| `resolve_figure` — one entity, metric, fiscal year | `ix_facts_lookup` | 1.0 ms |
| Comparison series, scoped to one subsidiary | `ix_facts_conflict_key` | 10.6 ms |
| **Comparison series, HQ-wide** | **parallel seq scan, 1M rows** | **124.7 ms** |
| Review queue, HQ-wide, first 200 | `ix_facts_lookup` | 17.7 ms |
| Conflict sweep, scoped | `ix_facts_lookup` | 304 ms |
| Conflict sweep, HQ-wide | full aggregate, 1M rows | 7.2 s |
| Documents list, newest 100 | seq scan, 3,404 rows | 1.0 ms |

Two things to take from it.

**The comparison series had no usable index, and only for the people with the
widest remit.** Both indexes on `facts` lead with `entity_id`; an HQ-wide scope
does not constrain that column, because an HQ officer sees every subsidiary. So
the chart on the dashboard read the whole table, while a subsidiary officer's
scoped version of the same chart took 10 ms. Fixed by migration `0007` with a
*partial* index over just the validated full-fiscal-year rows — 8% of the corpus,
720 kB against 9 MB for `ix_facts_lookup` — which takes the query to **3.8 ms**
and the scoped case to 0.3 ms. `series-index.sql` reproduces that before/after.

**The HQ-wide conflict sweep is 7.2 s and that is accepted.** It is a full-corpus
recompute by design and runs from the scheduler every 23 minutes, where seven
seconds costs nothing. But `POST /conflicts/detect` exposes the same work
synchronously for a reviewer who has just corrected something — 304 ms scoped to
one subsidiary, 7.2 s for an admin. At four or five times this corpus the admin
case stops being viable in an HTTP request and should become a job, the way
`/topics/extract` already did. Recorded in `docs/ROADMAP.md` rather than
pre-emptively restructured, because the scheduled sweep is what correctness
depends on and the endpoint is a convenience.
