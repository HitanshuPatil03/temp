-- Hot-query plans at 1M facts / 3,404 documents.
--
-- Each block is the SQL a repository method actually emits, reconstructed from
-- mrip/db/repositories/facts.py. The question each answers is whether the
-- declared indexes cover the filters, or whether the planner falls back to a
-- sequential scan — which at this volume is the difference between a dashboard
-- that feels instant and one an officer waits on.
\timing on
\pset pager off

\echo
\echo ===== 1. entity_metric_series, HQ-wide (unrestricted scope) =====
\echo The comparison chart on the dashboard. No entity filter, because an HQ
\echo officer sees every subsidiary — so the leading column of every facts index
\echo (entity_id) is not available to the planner.
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF, SUMMARY ON)
SELECT entity_id, fiscal_year, unit, sum(value) AS value, count(*) AS fact_count
FROM facts
WHERE metric = 'metric_3'
  AND fiscal_year IS NOT NULL
  AND status = 'validated'
  AND (period_end - period_start) >= 350
GROUP BY entity_id, fiscal_year, unit
ORDER BY fiscal_year, entity_id;

\echo
\echo ===== 2. entity_metric_series, scoped to one subsidiary =====
\echo The same chart for a subsidiary officer. entity_id IS available here, so
\echo this is the case ix_facts_lookup was designed for.
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF, SUMMARY ON)
SELECT entity_id, fiscal_year, unit, sum(value) AS value, count(*) AS fact_count
FROM facts
WHERE metric = 'metric_3'
  AND fiscal_year IS NOT NULL
  AND status = 'validated'
  AND (period_end - period_start) >= 350
  AND entity_id IN ('ent_5')
GROUP BY entity_id, fiscal_year, unit
ORDER BY fiscal_year, entity_id;

\echo
\echo ===== 3. resolve_figure: one entity, metric, fiscal year =====
\echo The exact-figure path, and the one a parliamentary answer depends on.
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF, SUMMARY ON)
SELECT * FROM facts
WHERE entity_id = 'ent_5'
  AND metric = 'metric_3'
  AND fiscal_year = 'FY2024-25'
  AND status NOT IN ('rejected', 'superseded')
ORDER BY entity_id, metric, period_start, fact_id
LIMIT 200;

\echo
\echo ===== 4. the review queue: every needs_review fact, HQ-wide =====
\echo What a reviewer opens. Filters on status alone.
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF, SUMMARY ON)
SELECT * FROM facts
WHERE status = 'needs_review'
ORDER BY entity_id, metric, period_start, fact_id
LIMIT 200;

\echo
\echo ===== 5. conflict detection, HQ-wide =====
\echo The radar sweep the scheduler runs every 23 minutes.
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF, SUMMARY ON)
SELECT entity_id, metric, unit, period_label,
       min(value) AS value_low, max(value) AS value_high,
       array_agg(fact_id ORDER BY value) AS fact_ids
FROM facts
WHERE status NOT IN ('rejected', 'superseded')
GROUP BY entity_id, metric, unit, period_label
HAVING count(DISTINCT value) > 1
   AND 0.005 <= (max(value) - min(value))
                / nullif(greatest(abs(max(value)), abs(min(value))), 0);

\echo
\echo ===== 6. documents list, newest first =====
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF, SUMMARY ON)
SELECT * FROM documents
ORDER BY ingested_at DESC
LIMIT 100;
