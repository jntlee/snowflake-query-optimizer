# Recommendation: Clustering Key

Recommend a clustering key when the query has a **range/inequality
predicate** (or a date filter) that *should* prune partitions but doesn't,
because the table either has no clustering key or is clustered on the
wrong column. Clustering reorganizes a table's micro-partitions so that
rows with similar values for the clustering key end up in the same
partitions — Snowflake's pruner can then skip whole partitions when the
predicate is on that key.

## When to recommend

EXPLAIN plan signal:
- A `TableScan` with a range/inequality predicate (`>=`, `<=`, `>`, `<`,
  `BETWEEN`, `IN` over a contiguous range, or a date filter such as
  `event_date >= '2026-01-01'`) on a column that is *not* the table's
  clustering key (or the table has no clustering key).

Candidate metadata signal:
- `partitions_scanned / partitions_total ≥ 0.8` — pruning isn't happening.
  This is what the `pruning_opportunity = 1` flag flags.
- `bytes_scanned` is large relative to what the predicate's selectivity
  would imply — a strong cue that the table layout is fighting the query.

If the predicate is an equality lookup on a high-cardinality column,
prefer **search-optimization** instead — clustering helps with ranges,
not random point lookups.

## What to recommend (`recommendation_target`)

The clustering-key proposal as `<table>(<col>)` or
`<table>(<col1>, <col2>)` for compound keys.

Examples:
- Single key: `EVENTS(EVENT_DATE)`
- Compound key: `EVENTS(EVENT_DATE, TENANT_ID)` — order matters; put the
  most-frequently-filtered column first.

Choosing the column:
- **Date columns** are the most common good clustering key for
  time-series-shaped tables.
- The column's cardinality should be high enough that clustering buys
  pruning (a 10-value enum won't cluster meaningfully) but not so high
  that almost every value lands in its own partition.
- If the query joins on a column with a range filter, cluster on the
  filter column, not the join column.

## Caveats to note in `rationale`

- **Re-clustering cost**: enabling a clustering key triggers automatic
  background re-clustering, which incurs ongoing credits proportional to
  table churn. Best for tables that are queried far more than they're
  written.
- **Not retroactive**: clustering applies to the table layout going
  forward; existing partitions get re-clustered over time, not
  instantaneously. The first few queries after enabling may not see the
  full benefit.
- **One key per table**: a table can only be clustered on one key (which
  may itself be compound). If the table is filtered on multiple
  unrelated columns by different queries, clustering helps the dominant
  pattern; the others may need SOS or a different strategy.

## Example rationale

> EXPLAIN shows TableScan on EVENTS with `event_date BETWEEN ? AND ?`
> filter; query scans 950/1000 partitions despite the predicate being
> highly selective. Recommend a clustering key on `EVENTS(EVENT_DATE)`
> so range scans on this column can prune partitions. Re-clustering cost
> is justified given the table's read-heavy workload.
