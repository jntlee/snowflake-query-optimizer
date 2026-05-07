# Recommendation: Search Optimization Service

Recommend Search Optimization Service (SOS) for **point-lookup** queries
on high-cardinality columns where the table is too large for an
unaccelerated `TableScan` to be fast. SOS builds and maintains a
Snowflake-managed search access path on the columns you specify; queries
with equality (or `IN`-list) predicates on those columns then hit the
search path instead of scanning micro-partitions.

## When to recommend

EXPLAIN plan signal:
- A `TableScan` whose filter is an equality predicate (`col = <value>`)
  or a small `IN` list (`col IN (a, b, c)`) on a high-cardinality column.
- The result row count is small relative to the table size — the query
  *should* return few rows, but is doing a wide scan to find them.

Candidate metadata signal:
- Modest `bytes_scanned` (the query isn't fundamentally a big aggregation
  — it's a needle-in-haystack lookup).
- High `partitions_scanned` relative to `partitions_total` — the existing
  partition layout doesn't help the predicate.
- Low spill (this is a lookup, not an aggregation; if there's spill,
  warehouse-resize is a better fit).

If the predicate is a *range* (`>`, `<`, `BETWEEN`, date range),
recommend **clustering-key** instead — SOS is for equality-style
predicates and certain substring searches, not arbitrary ranges.

## What to recommend (`recommendation_target`)

The column(s) referenced by the equality predicate, formatted as
`<table>.<col>` for a single column or
`<table>(<col1>, <col2>, ...)` for multi-column SOS.

Examples:
- Single column: `SALES.CUSTOMER_ID`
- Multi-column: `SALES(CUSTOMER_ID, ORDER_DATE)`

If the SQL references the table without an explicit schema, use the
table name as written (the operator knows the schema context).

## Caveats to note in `rationale`

- **Ongoing cost**: SOS has continuous storage cost (the search access
  path must be persisted) and continuous compute cost (it must be
  refreshed as the base table changes). This pays back only when the
  query — or others matching the same predicate shape — runs frequently.
- **Eligible column types**: equality on most scalar types is supported.
  Substring (`LIKE '%foo%'`) is supported but more expensive. Geography
  and complex types have limited support — flag if the column type is
  unusual.
- **Not a replacement for clustering**: if the predicate is genuinely a
  range, clustering pruning will be cheaper to maintain than SOS. SOS
  shines on truly random equality lookups across a large table.

## Example rationale

> EXPLAIN shows TableScan on ORDERS with `CUSTOMER_ID = ?` filter; query
> scans 850/1000 partitions but returns < 100 rows. Recommend Search
> Optimization Service on `ORDERS.CUSTOMER_ID` to skip the wide scan.
> SOS storage/refresh cost is justified if this lookup pattern repeats.
