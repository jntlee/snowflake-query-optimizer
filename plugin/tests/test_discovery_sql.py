"""Discovery-SQL builder tests. The function is pure (just produces a SQL
string), so we assert structural properties rather than full snapshots — keeps
the tests stable when we tweak whitespace or column ordering."""
from __future__ import annotations

import pytest

from lib.discovery_sql import build_discovery_sql


# ---- happy path: well-formed lookbacks ----

def test_24_hours():
    sql = build_discovery_sql("24 hours")
    assert "DATEADD(hour, -24" in sql
    assert "QUERY_HISTORY" in sql
    assert "ROW_NUMBER() OVER" in sql
    assert "PARTITION BY query_hash" in sql
    assert "ORDER BY total_elapsed_time DESC" in sql


def test_default_limit_is_3():
    """The default top-N is 3 — the analyzer surfaces the most expensive
    long-running candidates for infrastructure-level recommendations.
    Three is small enough to keep the analyzer's parallel EXPLAIN batch
    snappy while still surfacing more than one query class per run."""
    sql = build_discovery_sql("24 hours")
    assert sql.rstrip().endswith("LIMIT 3")


def test_7_days():
    assert "DATEADD(day, -7" in build_discovery_sql("7 days")


def test_30_days():
    assert "DATEADD(day, -30" in build_discovery_sql("30 days")


def test_minutes():
    assert "DATEADD(minute, -45" in build_discovery_sql("45 minutes")


def test_singular_unit():
    assert "DATEADD(day, -1" in build_discovery_sql("1 day")
    assert "DATEADD(hour, -1" in build_discovery_sql("1 hour")
    assert "DATEADD(minute, -1" in build_discovery_sql("1 minute")


def test_case_insensitive_unit():
    assert "DATEADD(day, -7" in build_discovery_sql("7 DAYS")
    assert "DATEADD(hour, -1" in build_discovery_sql("1 Hour")


def test_custom_limit():
    assert "LIMIT 5" in build_discovery_sql("24 hours", limit=5)
    assert "LIMIT 100" in build_discovery_sql("24 hours", limit=100)


def test_dedup_and_ranking_present():
    sql = build_discovery_sql("24 hours")
    # dedup-by-query_hash
    assert "PARTITION BY query_hash" in sql
    # representative is the longest single instance
    assert "ORDER BY total_elapsed_time DESC" in sql
    # final ranking + top-N
    assert "WHERE rn = 1" in sql


def test_credits_estimate_present():
    sql = build_discovery_sql("24 hours")
    assert "estimated_credits" in sql
    assert "X-Small" in sql  # the warehouse-size CASE
    assert "execution_time" in sql
    # divided to convert ms to hours
    assert "1000.0 * 3600.0" in sql


def test_status_and_type_filters():
    sql = build_discovery_sql("24 hours")
    assert "execution_status = 'SUCCESS'" in sql
    assert "query_type = 'SELECT'" in sql


# ---- new in this revision: filtering out system / non-warehouse queries ----

def test_excludes_queries_with_no_warehouse():
    """Snowflake-internal / metadata queries don't run on a warehouse and
    should be excluded — they're not optimization candidates."""
    sql = build_discovery_sql("24 hours")
    assert "warehouse_name IS NOT NULL" in sql


def test_does_not_filter_on_database_name():
    """Filtering on database_name dropped real, optimizable queries (queries
    that referenced fully-qualified db.schema.table without an active database
    on the session). The warehouse_name filter alone is enough to exclude
    Snowflake-internal / metadata-only queries."""
    sql = build_discovery_sql("24 hours")
    assert "database_name" not in sql


def test_excludes_snowflake_ui_worksheet_user():
    sql = build_discovery_sql("24 hours")
    assert "user_name NOT LIKE 'WORKSHEETS_APP%'" in sql


def test_excludes_mcp_service_account():
    """The discovery query runs as the MCP connection's service user, so its
    own EXPLAIN / HASH_AGG / discovery traffic from prior runs would otherwise
    rank as expensive candidates. Filter via CURRENT_USER() rather than a
    hardcoded name so this works for any connections.toml the user supplies."""
    sql = build_discovery_sql("24 hours")
    assert "user_name != CURRENT_USER()" in sql


def test_excludes_subsecond_queries():
    """Sub-second queries usually aren't worth the optimization effort.
    The MIN_ELAPSED_MS floor in the SQL prevents noise candidates."""
    sql = build_discovery_sql("24 hours")
    assert "total_elapsed_time >= 1000" in sql


# ---- new in this revision: optimization-opportunity flags ----

def test_pruning_opportunity_flag_present():
    """Queries with high partition-scan ratio get pruning_opportunity = 1."""
    sql = build_discovery_sql("24 hours")
    assert "pruning_opportunity" in sql
    assert "partitions_scanned * 1.0 / partitions_total >= 0.8" in sql


def test_warehouse_size_opportunity_flag_present():
    """Queries that spilled (local or remote) get warehouse_size_opportunity = 1."""
    sql = build_discovery_sql("24 hours")
    assert "warehouse_size_opportunity" in sql
    assert "bytes_spilled_to_local_storage > 0" in sql
    assert "bytes_spilled_to_remote_storage > 0" in sql


def test_projection_opportunity_flag_present():
    """Queries containing SELECT * (case-insensitive) get projection_opportunity = 1."""
    sql = build_discovery_sql("24 hours")
    assert "projection_opportunity" in sql
    assert "UPPER(query_text) LIKE '%SELECT *%'" in sql


def test_does_not_filter_on_opportunity_flags():
    """The opportunity flags are computed and surfaced as auxiliary signals,
    but they no longer filter the candidate list. A long-running query with
    none of the legacy flags set is still worth analyzing — the analyzer
    has to determine which infrastructure recommendation fits, not the
    discovery stage."""
    sql = build_discovery_sql("24 hours")
    # The flag = 1 disjunction that used to live in the outer WHERE must
    # not appear anywhere in the SQL.
    assert "pruning_opportunity = 1" not in sql
    assert "warehouse_size_opportunity = 1" not in sql
    assert "projection_opportunity = 1 " not in sql  # trailing space avoids matching the column def


def test_returns_spill_and_partition_columns_for_analyzer():
    """The analyzer agent uses these to choose a recommendation, so they must
    appear in the final SELECT, not just inside the CTE."""
    sql = build_discovery_sql("24 hours")
    # Sanity: each column should appear at least twice — once in the CTE
    # body, once in the final SELECT projection.
    assert sql.count("partitions_scanned") >= 2
    assert sql.count("bytes_spilled_to_local_storage") >= 2
    assert sql.count("bytes_spilled_to_remote_storage") >= 2
    assert sql.count("warehouse_name") >= 2  # name, not just size


# ---- rejection paths ----

@pytest.mark.parametrize(
    "bad",
    [
        "yesterday",
        "24h",
        "24",
        "24 weeks",
        "24 months",
        "24 years",
        "abc 24 hours",
        "24 hours; DROP TABLE x",  # injection attempt
        "24' OR 1=1 --",
        "",
        "  ",
        "-1 hours",  # regex requires \d+, but unary minus is rejected
    ],
)
def test_rejects_bad_lookback(bad):
    with pytest.raises(ValueError):
        build_discovery_sql(bad)


def test_rejects_non_string_lookback():
    with pytest.raises(ValueError):
        build_discovery_sql(24)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [0, -1, 1001, 5000])
def test_rejects_bad_limit(bad):
    with pytest.raises(ValueError):
        build_discovery_sql("24 hours", limit=bad)


@pytest.mark.parametrize("bad", ["10", 10.5, True])
def test_rejects_non_int_limit(bad):
    with pytest.raises(ValueError):
        build_discovery_sql("24 hours", limit=bad)  # type: ignore[arg-type]
