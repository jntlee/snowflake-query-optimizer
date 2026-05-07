---
name: query-optimizer
description: Snowflake infrastructure-recommendation methodology for long-running queries. Use when proposing a Snowflake feature (Search Optimization Service, clustering key, warehouse resize, Query Acceleration Service) to speed up an expensive query, especially when an EXPLAIN plan and per-query metadata are available. Covers the four high-value recommendation patterns and the criteria for picking each. Invoked by the query-optimizer subagent for each candidate.
---

# Snowflake Infrastructure Recommender

Recommend a Snowflake configuration change that would speed up an expensive
query, without rewriting the SQL itself. The methodology is narrow and
deep: pick exactly one of four buckets based on the EXPLAIN plan plus the
candidate's runtime metadata. If none clearly applies, label
`recommendation_type: "none"` rather than guessing.

## Method

### 1. Always run EXPLAIN first

Use `EXPLAIN USING TEXT <original sql>`. The plan is the input to pattern
selection — never guess from the SQL text alone. EXPLAIN calls are free and
fast; the cost is one extra round trip per candidate.

### 2. Combine plan with metadata

The EXPLAIN plan tells you the *shape* of the work (point lookup vs range
scan, single-table vs multi-table, presence of aggregations and sorts).
The candidate metadata from `candidates.json` tells you whether that shape
is actually *expensive* in practice:

- `partitions_scanned`, `partitions_total` — pruning effectiveness
- `bytes_scanned` — total data read
- `bytes_spilled_to_local_storage`, `bytes_spilled_to_remote_storage` —
  warehouse memory pressure
- `pruning_opportunity`, `warehouse_size_opportunity`,
  `projection_opportunity` — auxiliary signal flags pre-computed in SQL

The plan alone or the metadata alone is rarely sufficient. A `TableScan`
with high partition count is wasteful only if the query also has a
selective filter that *should* have pruned. A spill is only worth
recommending a resize for if the plan actually shows an aggregation or
sort that drove it.

### 3. Pick exactly one bucket

In rough priority order — when multiple plausibly apply, target the one
tied to the highest-cost operator or the largest metric.

- **Point lookup** → Search Optimization Service. See
  [references/search-optimization.md](references/search-optimization.md).
  Plan signal: `TableScan` with an equality filter (`=`, `IN`) on a
  high-cardinality column, returning a small result. Metric signal:
  modest `bytes_scanned` relative to table size, but elapsed is high
  because the scan can't prune.

- **Range scan on unclustered data** → Clustering key. See
  [references/clustering-key.md](references/clustering-key.md). Plan
  signal: `TableScan` with a range/inequality predicate (`>=`, `<=`,
  `BETWEEN`, date ranges) on a column that is *not* the table's
  clustering key. Metric signal: `partitions_scanned / partitions_total
  ≥ 0.8` (this is what `pruning_opportunity = 1` flags).

- **Heavy aggregation that spills** → Warehouse resize. See
  [references/warehouse-sizing.md](references/warehouse-sizing.md).
  Plan signal: `Aggregate`, `Sort`, or `WindowFunction` operator
  processing a large input. Metric signal:
  `bytes_spilled_to_local_storage > 0` or
  `bytes_spilled_to_remote_storage > 0` (this is what
  `warehouse_size_opportunity = 1` flags). Recommend the next size up
  from the candidate's current `warehouse_size`.

- **Wide partition scan** → Query Acceleration Service. See
  [references/query-acceleration.md](references/query-acceleration.md).
  Plan signal: `TableScan` covering all or nearly all of a large table
  with no usable selective filter. Metric signal: very large
  `partitions_scanned` *and* very large `bytes_scanned`, no spill (so
  resize wouldn't help), no clustering key would help (the query needs
  the whole table).

If the plan shows an obviously fixable SQL antipattern (e.g. a correlated
subquery that should be a join, or `SELECT *` from a wide table), the
analyzer in this project does **not** recommend rewriting it. It records
`recommendation_type: "none"` and notes the antipattern in `rationale`.
SQL rewrites are out of scope.

### 4. Populate the recommendation

For each non-`none` bucket, fill `recommendation_target` with a concrete
string the user can act on (the column for SOS or clustering, the next
warehouse size for resize, the warehouse for QAS). Fill `rationale` with
one or two sentences citing the specific operator from EXPLAIN and the
specific metric from the candidate that triggered the choice.

For `recommendation_type: "none"`, leave `recommendation_target` null and
use `rationale` to explain what you saw and why no recommendation fits.

### 5. Caveats to mention in rationale (when relevant)

- **Search Optimization Service** has ongoing storage and compute cost
  for maintaining the search access path. Worth flagging if the query's
  workload is small.
- **Clustering keys** trigger automatic re-clustering, which incurs
  credits over time. Best for tables that are queried far more than
  written.
- **Warehouse resize** is the simplest change but doubles credits per
  unit time. Worth only when the spill is meaningful and the query runs
  often.
- **Query Acceleration Service** is regional — check eligibility for
  the account's region before recommending.

## Output format

The analyzer subagent assembles each record from your decisions. Per
candidate, decide:

```json
{
  "recommendation_type": "search-optimization|clustering-key|warehouse-resize|query-acceleration|none",
  "recommendation_target": "<concrete actionable string, or null>",
  "rationale": "<one or two sentences citing EXPLAIN operator + metric>"
}
```

That's the whole skill output: classify, target, justify, and stop.

## Pattern catalog

One reference file per pattern. Read only the one that matches the plan.

- [references/search-optimization.md](references/search-optimization.md) —
  point-lookup queries on high-cardinality columns
- [references/clustering-key.md](references/clustering-key.md) — range
  scans on unclustered (or wrongly clustered) data
- [references/warehouse-sizing.md](references/warehouse-sizing.md) —
  spilling aggregations and the size-step table
- [references/query-acceleration.md](references/query-acceleration.md) —
  wide partition scans with no selective filter
