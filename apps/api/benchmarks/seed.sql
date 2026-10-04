-- Benchmark seed: realistic corpus shape for index measurement.
--
-- 3,404 documents is the real catalogued corpus size (data/corpus/manifest.json).
-- ~300 facts per document gives ~1M facts, which is the volume the comparison
-- charts and the exact-figure path have to serve.
--
-- The dimensions are a **mixed-radix decomposition** of the row number, which is
-- what makes them independent. Two earlier versions of this file did not bother,
-- and each measured nothing while looking fine:
--
--   * metric from ``g % 20`` and the full-year flag from ``g % 12`` — one needs an
--     odd g, the other an even one, so no row was ever both;
--   * entity from ``g % 30`` and metric from ``g % 20`` — they share a factor of
--     ten, so only a tenth of the (entity, metric) pairs existed at all.
--
-- Both produced plans reporting the cost of finding zero rows. Synthetic data with
-- secretly correlated dimensions measures the wrong thing, confidently, so the
-- tail of this file asserts the combinations the plans actually query.
--
-- Period spans are the mix the corpus has: one full fiscal year per cycle of
-- twelve, the rest months and cumulatives inside it. That mix is what makes the
-- full-year filter in entity_metric_series necessary, and what decides how
-- selective an index on it would be.
--
-- Throwaway: lives only in the mrip_bench database, dropped afterwards.
SET client_min_messages = warning;

INSERT INTO documents (
  document_id, content_hash, filename, doc_class, version, page_count,
  size_bytes, owner_entity_id, publisher_entity_id, fiscal_year, ingested_at,
  state, sensitivity, is_synthetic, blob_key
)
SELECT
  'doc_bench_' || i,
  md5('bench' || i),
  'statement-' || i || '.pdf',
  'text_pdf',
  1,
  40,
  2000000,
  'ent_' || (i % 29),
  'ent_' || (i % 29),
  'FY20' || (19 + (i % 6)) || '-' || (20 + (i % 6)),
  now() - (i || ' hours')::interval,
  'ready',
  'internal',
  true,
  md5('bench' || i)
FROM generate_series(1, 3404) AS s(i);

-- Radix digits of g:  entity 29 | metric 20 | year 6 | span 12
--   entity = g % 29
--   metric = (g / 29) % 20
--   year   = (g / 580) % 6        -- 580 = 29 * 20
--   span   = (g / 3480) % 12      -- 3480 = 580 * 6
-- 29 is prime, so the entity digit is coprime to every stride above it.
INSERT INTO facts (
  fact_id, entity_id, metric, value, unit, dimension,
  raw_value, raw_unit, period_start, period_end, period_label, fiscal_year,
  document_id, document_version, page, extraction_method,
  conf_parse, status, review_state, unit_ambiguous, extracted_at
)
SELECT
  'fct_bench_' || g,
  'ent_' || (g % 29),
  'metric_' || ((g / 29) % 20),
  1000000 + (g % 900000),
  't',
  'mass',
  1.0 + (g % 900),
  'MT',
  make_date(2019 + ((g / 580) % 6), 4, 1),
  CASE WHEN (g / 3480) % 12 = 0
       -- The authoritative annual figure: a full fiscal year.
       THEN make_date(2020 + ((g / 580) % 6), 3, 31)
       -- A month or a part-year cumulative inside it.
       ELSE make_date(2019 + ((g / 580) % 6), 4, 1)
            + (((g / 3480) % 12) || ' months')::interval
  END,
  CASE WHEN (g / 3480) % 12 = 0
       THEN 'FY20' || (19 + ((g / 580) % 6)) || '-' || (20 + ((g / 580) % 6))
       ELSE 'cum_' || ((g / 3480) % 12)
            || '_FY20' || (19 + ((g / 580) % 6))
  END,
  'FY20' || (19 + ((g / 580) % 6)) || '-' || (20 + ((g / 580) % 6)),
  'doc_bench_' || (1 + (g % 3404)),
  1,
  1 + (g % 40),
  'table_lattice',
  0.95,
  -- 19 is prime and coprime to 29, so review status is independent of entity.
  CASE WHEN g % 19 = 0 THEN 'needs_review' ELSE 'validated' END,
  'pending',
  false,
  now()
FROM generate_series(1, 1000000) AS s(g);

ANALYZE documents;
ANALYZE facts;

-- Volume, and then the assertions. A zero in the last three rows means the plans
-- below are measuring the cost of finding nothing.
SELECT 'documents' AS relation, count(*) FROM documents
UNION ALL SELECT 'facts', count(*) FROM facts
UNION ALL SELECT 'full-year facts',
  count(*) FROM facts WHERE (period_end - period_start) >= 350
UNION ALL SELECT 'needs_review facts',
  count(*) FROM facts WHERE status = 'needs_review'
UNION ALL SELECT 'ASSERT metric_3 + full-year > 0',
  count(*) FROM facts
  WHERE metric = 'metric_3' AND (period_end - period_start) >= 350
UNION ALL SELECT 'ASSERT ent_5 + metric_3 + FY2024-25 > 0',
  count(*) FROM facts
  WHERE entity_id = 'ent_5' AND metric = 'metric_3'
    AND fiscal_year = 'FY2024-25'
UNION ALL SELECT 'ASSERT ent_5 + metric_3 + full-year > 0',
  count(*) FROM facts
  WHERE entity_id = 'ent_5' AND metric = 'metric_3'
    AND (period_end - period_start) >= 350;
