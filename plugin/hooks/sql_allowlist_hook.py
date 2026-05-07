#!/usr/bin/env python3
"""PreToolUse SQL allowlist hook for snowflake-* MCP tools.

Reads the PreToolUse JSON envelope from stdin, extracts the SQL payload from
the tool input, and either:
  - exits 0 to allow the tool call (writes nothing)
  - exits 2 to block (writes a reason to stderr; Claude Code surfaces it
    back to the model)

Defense in depth: even if RBAC and the MCP `sql_statement_permissions` are
bypassed or misconfigured, this hook denies any SQL whose first non-whitespace
token is not in the allowlist or one of three exact ALTER patterns
(see lib/allowlist.py). Multi-statement payloads are always rejected.

Failure modes are biased toward "block legitimate query," not "allow
destructive query": JSON parse error -> block; missing envelope -> block;
unrecognized payload shape -> allow only if no SQL-shaped string is present
(the MCP server's own permissions are the next layer for non-SQL tools).
"""
from __future__ import annotations

import json
import os
import sys

_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

from lib.allowlist import check  # noqa: E402

_SQL_LEAD_TOKENS = (
    "SELECT", "WITH", "EXPLAIN", "SHOW", "DESC", "DESCRIBE", "USE",
    "ALTER", "DROP", "DELETE", "UPDATE", "INSERT", "MERGE", "TRUNCATE",
    "GRANT", "REVOKE", "CREATE", "REPLACE", "COPY", "CALL", "EXECUTE",
    "UNDROP", "PUT", "GET",
)


def _looks_like_sql(s: object) -> bool:
    if not isinstance(s, str) or len(s) == 0 or len(s) > 1_000_000:
        return False
    head = s.lstrip()[:32].upper()
    return any(head.startswith(t) for t in _SQL_LEAD_TOKENS) or head.startswith(("/*", "--", "//"))


_WALK_MAX_DEPTH = 32


def _extract_sql(tool_input: dict) -> str | None:
    """Pull the SQL string out of the tool input.

    snowflake-labs MCP `query_manager` uses `statement`; that's the path the
    live pipeline always takes. The `sql`/`query` aliases and the recursive
    walk are belt-and-braces fallbacks for hypothetical future MCP tools
    whose input shape differs — the MCP server's own
    `sql_statement_permissions` is the authoritative gate for any payload
    whose verb the named-key check doesn't see.
    """
    if not isinstance(tool_input, dict):
        return None
    for key in ("statement", "sql", "query"):
        v = tool_input.get(key)
        if isinstance(v, str) and v:
            return v

    def walk(obj, depth=0):
        if depth > _WALK_MAX_DEPTH:
            return
        if isinstance(obj, str):
            if _looks_like_sql(obj):
                yield obj
        elif isinstance(obj, dict):
            for v in obj.values():
                yield from walk(v, depth + 1)
        elif isinstance(obj, list):
            for v in obj:
                yield from walk(v, depth + 1)

    for s in walk(tool_input):
        return s
    return None


_STDIN_MAX_BYTES = 2_000_000  # 2 MB; the SQL payload itself caps at 1 MB


def main() -> int:
    raw = sys.stdin.read(_STDIN_MAX_BYTES + 1)
    if len(raw) > _STDIN_MAX_BYTES:
        sys.stderr.write(
            f"snowflake-query-optimizer hook: stdin envelope exceeds "
            f"{_STDIN_MAX_BYTES} bytes; refusing to process\n"
        )
        return 2
    if not raw.strip():
        sys.stderr.write("snowflake-query-optimizer hook: empty stdin envelope\n")
        return 2
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as e:
        sys.stderr.write(f"snowflake-query-optimizer hook: malformed JSON envelope: {e}\n")
        return 2

    tool_name = envelope.get("tool_name", "")
    tool_input = envelope.get("tool_input", {}) or {}

    sql = _extract_sql(tool_input)
    if sql is None:
        # Tool call has no SQL-shaped payload (e.g. semantic-view introspection).
        # The MCP server's own sql_statement_permissions is the next layer.
        return 0

    allowed, reason = check(sql)
    if allowed:
        return 0

    sys.stderr.write(
        "BLOCKED by snowflake-query-optimizer SQL allowlist hook.\n"
        f"  tool: {tool_name}\n"
        f"  reason: {reason}\n"
        f"  sql (first 200 chars): {sql[:200]!r}\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
