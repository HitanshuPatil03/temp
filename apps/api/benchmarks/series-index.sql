-- Does a partial index fix the HQ-wide comparison chart?
--
-- Query 1 in bench_explain.sql is a parallel sequential scan over every fact to
-- find the few thousand that are a validated full fiscal year for one metric.
-- The filters are (metric, fiscal_year IS NOT NULL, status, full-year span), and
-- no existing index leads with metric — ix_facts_lookup and ix_facts_conflict_key
-- both lead with entity_id, which an HQ-wide scope does not constrain.
--
-- The candidate is a *partial* index over exactly the rows the series can return.
-- Only 8% of facts are a validated full year, so the index is small and most
-- inserts skip it entirely.
\timing on
\pset pager off

\echo ===== baseline: no index =====
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF)
SELECT entity_id, fiscal_year, unit, sum(value) AS value, count(*) AS fact_count
FROM facts
WHERE metric = 'metric_3' AND fiscal_year IS NOT NULL AND status = 'validated'
  AND (period_end - period_start) >= 350
GROUP BY entity_id, fiscal_year, unit
ORDER BY fiscal_year, entity_id;

\echo
\echo ===== building the candidate index =====
CREATE INDEX ix_facts_series ON facts (metric, fiscal_year, entity_id, unit)
WHERE status = 'validated' AND (period_end - period_start) >= 350;
ANALYZE facts;

\echo
\echo ===== with the index =====
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF)
SELECT entity_id, fiscal_year, unit, sum(value) AS value, count(*) AS fact_count
FROM facts
WHERE metric = 'metric_3' AND fiscal_year IS NOT NULL AND status = 'validated'
  AND (period_end - period_start) >= 350
GROUP BY entity_id, fiscal_year, unit
ORDER BY fiscal_year, entity_id;

\echo
\echo ===== and the scoped case must not regress =====
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF)
SELECT entity_id, fiscal_year, unit, sum(value) AS value, count(*) AS fact_count
FROM facts
WHERE metric = 'metric_3' AND fiscal_year IS NOT NULL AND status = 'validated'
  AND (period_end - period_start) >= 350 AND entity_id IN ('ent_5')
GROUP BY entity_id, fiscal_year, unit
ORDER BY fiscal_year, entity_id;

\echo
\echo ===== what it costs: size on disk =====
SELECT
  pg_size_pretty(pg_total_relation_size('facts')) AS facts_total,
  pg_size_pretty(pg_relation_size('ix_facts_series')) AS new_index,
  pg_size_pretty(pg_relation_size('ix_facts_lookup')) AS ix_lookup,
  pg_size_pretty(pg_relation_size('ix_facts_conflict_key')) AS ix_conflict;

\echo
\echo ===== the conflict sweep, scoped to one subsidiary =====
\echo The HQ-wide sweep is a background job, but POST /conflicts/detect is also a
\echo synchronous endpoint a reviewer can press. This is what they would wait for.
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF)
SELECT entity_id, metric, unit, period_label,
       min(value) AS value_low, max(value) AS value_high,
       array_agg(fact_id ORDER BY value) AS fact_ids
FROM facts
WHERE status NOT IN ('rejected', 'superseded') AND entity_id IN ('ent_5')
GROUP BY entity_id, metric, unit, period_label
HAVING count(DISTINCT value) > 1
   AND 0.005 <= (max(value) - min(value))
                / nullif(greatest(abs(max(value)), abs(min(value))), 0);
