"""Discovery SQL builder for the demo QUERY_HISTORY table.

Reads from `DEMO_DB.QUERY_TUNING.QUERY_HISTORY`, a curated copy of
`SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY` shaped for the demo workload.
Using the curated table avoids the ~45-minute latency of ACCOUNT_USAGE
and lets the demo control which queries appear as candidates. Schema
matches ACCOUNT_USAGE.QUERY_HISTORY column-for-column, so the SELECT
projection here is unchanged.

Returns a parameterized SELECT that:
- filters by start_time within the validated lookback window,
- restricts to successful, non-empty SELECT-type queries that ran on a
  warehouse (excludes Snowflake-internal / metadata-only queries — note
  that we deliberately do NOT filter on database_name being non-null,
  since real queries that reference fully-qualified db.schema.table
  identifiers can run with no session-default database),
- excludes queries run by the MCP service account itself (CURRENT_USER()),
  so the analyzer never re-analyzes its own EXPLAIN traffic from prior runs,
- excludes Snowflake UI worksheet activity and sub-second queries,
- dedupes by query_hash (one row per unique text), choosing the longest
  single instance as the representative,
- estimates compute credits from execution_time and warehouse_size,
- computes three auxiliary signal flags (pruning, warehouse-size, projection)
  from QUERY_HISTORY columns — surfaced to the analyzer as extra context
  but NOT used to filter the candidate list,
- ranks by total_elapsed_time desc, returns top `limit` (default 3).

Lookback is validated against an allowlist regex so neither the unit nor the
integer can be SQL-injected. limit is bounded.
"""
from __future__ import annotations

import re

LOOKBACK_RE = re.compile(r"^\s*(\d+)\s+(minutes?|hours?|days?)\s*$", re.IGNORECASE)

# Sub-second queries usually aren't worth the optimization effort.
MIN_ELAPSED_MS = 1000

# A partition-scan ratio at or above this fraction means pruning isn't
# happening. Mirrors the cutoff used in skills/query-optimizer/SKILL.md.
PRUNING_PARTITION_RATIO = 0.8


def _validate_lookback(lookback: str) -> tuple[int, str]:
    if not isinstance(lookback, str):
        raise ValueError("lookback must be a string")
    m = LOOKBACK_RE.match(lookback)
    if not m:
        raise ValueError(
            f"lookback {lookback!r} invalid; expected '<int> <minutes|hours|days>' "
            "(e.g. '24 hours', '7 days')"
        )
    n = int(m.group(1))
    unit = m.group(2).lower().rstrip("s")  # "hours" -> "hour"
    if n < 1:
        raise ValueError("lookback magnitude must be >= 1")
    return n, unit


def build_discovery_sql(lookback: str, limit: int = 3) -> str:
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValueError("limit must be a plain int")
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")
    n, unit = _validate_lookback(lookback)
    return (
        f"WITH recent AS (\n"
        f"  SELECT\n"
        f"    query_id,\n"
        f"    query_hash,\n"
        f"    query_text,\n"
        f"    user_name,\n"
        f"    warehouse_name,\n"
        f"    warehouse_size,\n"
        f"    total_elapsed_time,\n"
        f"    execution_time,\n"
        f"    bytes_scanned,\n"
        f"    partitions_scanned,\n"
        f"    partitions_total,\n"
        f"    bytes_spilled_to_local_storage,\n"
        f"    bytes_spilled_to_remote_storage,\n"
        f"    CASE warehouse_size\n"
        f"      WHEN 'X-Small'  THEN 1\n"
        f"      WHEN 'Small'    THEN 2\n"
        f"      WHEN 'Medium'   THEN 4\n"
        f"      WHEN 'Large'    THEN 8\n"
        f"      WHEN 'X-Large'  THEN 16\n"
        f"      WHEN '2X-Large' THEN 32\n"
        f"      WHEN '3X-Large' THEN 64\n"
        f"      WHEN '4X-Large' THEN 128\n"
        f"      WHEN '5X-Large' THEN 256\n"
        f"      WHEN '6X-Large' THEN 512\n"
        f"      ELSE NULL\n"
        f"    END * (execution_time / (1000.0 * 3600.0)) AS estimated_credits,\n"
        f"    -- Auxiliary signal flags (1 = present, 0 = not detected),\n"
        f"    -- surfaced to the analyzer agent as extra context. Not used to\n"
        f"    -- filter the candidate list — top-N-by-elapsed wins.\n"
        f"    CASE\n"
        f"      WHEN partitions_total > 0\n"
        f"        AND partitions_scanned * 1.0 / partitions_total >= {PRUNING_PARTITION_RATIO}\n"
        f"      THEN 1 ELSE 0\n"
        f"    END AS pruning_opportunity,\n"
        f"    CASE\n"
        f"      WHEN bytes_spilled_to_local_storage > 0\n"
        f"        OR bytes_spilled_to_remote_storage > 0\n"
        f"      THEN 1 ELSE 0\n"
        f"    END AS warehouse_size_opportunity,\n"
        f"    CASE\n"
        f"      WHEN UPPER(query_text) LIKE '%SELECT *%'\n"
        f"      THEN 1 ELSE 0\n"
        f"    END AS projection_opportunity,\n"
        f"    start_time,\n"
        f"    ROW_NUMBER() OVER (\n"
        f"      PARTITION BY query_hash\n"
        f"      ORDER BY total_elapsed_time DESC\n"
        f"    ) AS rn\n"
        f"  FROM DEMO_DB.QUERY_TUNING.QUERY_HISTORY\n"
        f"  WHERE start_time >= DATEADD({unit}, -{n}, CURRENT_TIMESTAMP())\n"
        f"    AND execution_status = 'SUCCESS'\n"
        f"    AND query_type = 'SELECT'\n"
        f"    AND query_text IS NOT NULL\n"
        f"    -- Exclude Snowflake-internal / metadata-only queries.\n"
        f"    AND warehouse_name IS NOT NULL\n"
        f"    AND user_name NOT LIKE 'WORKSHEETS_APP%'\n"
        f"    -- Exclude queries run by the MCP service account itself — the\n"
        f"    -- analyzer's own discovery/EXPLAIN traffic from prior runs would\n"
        f"    -- otherwise compete for the top spot.\n"
        f"    AND user_name != CURRENT_USER()\n"
        f"    -- Exclude trivially fast queries — not worth the analysis effort.\n"
        f"    AND total_elapsed_time >= {MIN_ELAPSED_MS}\n"
        f")\n"
        f"SELECT\n"
        f"  query_id,\n"
        f"  query_hash,\n"
        f"  query_text,\n"
        f"  warehouse_name,\n"
        f"  warehouse_size,\n"
        f"  total_elapsed_time,\n"
        f"  bytes_scanned,\n"
        f"  partitions_scanned,\n"
        f"  partitions_total,\n"
        f"  bytes_spilled_to_local_storage,\n"
        f"  bytes_spilled_to_remote_storage,\n"
        f"  estimated_credits,\n"
        f"  pruning_opportunity,\n"
        f"  warehouse_size_opportunity,\n"
        f"  projection_opportunity,\n"
        f"  start_time\n"
        f"FROM recent\n"
        f"WHERE rn = 1\n"
        f"ORDER BY total_elapsed_time DESC\n"
        f"LIMIT {limit}"
    )
