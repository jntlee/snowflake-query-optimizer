# Recommendation: Query Acceleration Service

Recommend Query Acceleration Service (QAS) for queries that perform a
**wide partition scan** with no usable selective filter. QAS lets a single
query temporarily borrow extra serverless compute to parallelize the scan
beyond what the warehouse alone provides — useful when the query simply
needs to read a lot of data and there's no shape to prune.

## When to recommend

EXPLAIN plan signal:
- A `TableScan` covering all or nearly all of a large table.
- No selective filter, or the filter is on a column whose cardinality or
  layout means most partitions need to be read regardless. Common cases:
  full-table aggregations, broad reporting queries that span the whole
  history of a fact table, or analytics over event streams without a
  time predicate.

Candidate metadata signal:
- Very large `partitions_scanned` *and* very large `bytes_scanned`.
- `partitions_scanned ≈ partitions_total` (the table is being read
  essentially in full).
- No or low spill — if there's significant spill, the bottleneck is
  warehouse memory, not parallelism, and **warehouse-resize** is the
  better fit.

If the wide scan is happening despite a *selective* predicate that
should have pruned, the table is mis-clustered — recommend
**clustering-key** instead. QAS speeds up scans that genuinely need to
read everything; it doesn't fix a missing index.

## What to recommend (`recommendation_target`)

QAS is enabled per warehouse, so the target is the warehouse name from
the candidate's `warehouse_name`, e.g. `OPTIMIZE_WH`.

Format: just the warehouse name as a bare string, e.g. `"OPTIMIZE_WH"`.

The operator enables it on the warehouse with something like:

```sql
ALTER WAREHOUSE OPTIMIZE_WH SET QUERY_ACCELERATION_MAX_SCALE_FACTOR = 8;
```

(Don't include this DDL in `rationale` — keep it human-readable. The
operator already knows the syntax.)

## Caveats to note in `rationale`

- **Regional availability**: QAS is available in most but not all
  regions/cloud providers. Flag that the operator should verify
  eligibility for their account's region before enabling.
- **Edition**: QAS requires Enterprise Edition or higher.
- **Cost model**: QAS bills serverless compute by the second of
  acceleration used. The cost only kicks in when a query actually
  benefits — Snowflake routes only eligible scans to it. So enabling
  QAS is low-risk; it doesn't add cost to queries that wouldn't gain
  from it.
- **Eligibility of the workload**: QAS accelerates large table scans
  with filtering and aggregation, but only a subset of operators are
  eligible. Note this in the rationale: "if the optimizer routes this
  query to QAS, scan time should drop materially."

## Example rationale

> EXPLAIN shows TableScan on FACT_EVENTS with no usable filter; query
> reads 9800/10000 partitions and 850 GB. No spill. Recommend enabling
> Query Acceleration Service on the `OPTIMIZE_WH` warehouse — full-table
> scans of this size are the canonical QAS use case. Verify regional
> availability and Enterprise Edition before enabling.
