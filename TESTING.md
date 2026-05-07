# Testing & Open Items

Recipes for verifying the plugin works, in roughly the order you'd run them
the first time. Plus the two open items from `IMPLEMENTATION_PLAN.md` that
need real-Snowflake confirmation.

## 1. Lib unit tests (no Snowflake, no Claude)

```bash
cd snowflake-query-optimizer
python3 -m venv .venv
.venv/bin/pip install -U pip            # 3.9's bundled pip is too old for PEP 660 editable installs
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
```

Expected: **129 passed**. Covers the three pure-python modules
(allowlist, discovery_sql, excel). The excel writer is also covered by
unit tests in `test_excel.py`; the smoke test below is an end-to-end
sanity check that the produced .xlsx opens.

Python 3.9+ is supported (the lib code uses `from __future__ import
annotations` so the new-style type hints don't require 3.10 at runtime).

## 2. Hook smoke test (no Snowflake, no Claude)

Verifies the destructive-SQL gate without needing the MCP server.

```bash
# Should ALLOW (exit 0, no output):
echo '{"tool_name":"mcp__snowflake__query_manager","tool_input":{"statement":"SELECT 1"}}' \
  | python3 plugin/hooks/sql_allowlist_hook.py; echo "exit=$?"

# Should BLOCK destructive SQL:
echo '{"tool_name":"mcp__snowflake__query_manager","tool_input":{"statement":"DROP TABLE x"}}' \
  | python3 plugin/hooks/sql_allowlist_hook.py; echo "exit=$?"
# expect: BLOCKED ... reason: verb 'DROP' not in allowlist ... exit=2

# Should BLOCK multi-statement piggyback (the security-pointed reason):
echo '{"tool_name":"mcp__snowflake__query_manager","tool_input":{"statement":"SELECT 1; DROP TABLE x"}}' \
  | python3 plugin/hooks/sql_allowlist_hook.py; echo "exit=$?"
# expect: reason: multi-statement payload not allowed ... exit=2

# Should BLOCK every ALTER form — the live pipeline doesn't issue any:
echo '{"tool_name":"mcp__snowflake__query_manager","tool_input":{"statement":"ALTER WAREHOUSE test_wh SUSPEND"}}' \
  | python3 plugin/hooks/sql_allowlist_hook.py; echo "exit=$?"
# expect: reason: verb 'ALTER' not in allowlist ... exit=2

echo '{"tool_name":"mcp__snowflake__query_manager","tool_input":{"statement":"ALTER USER svc SET PASSWORD='\''x'\''"}}' \
  | python3 plugin/hooks/sql_allowlist_hook.py; echo "exit=$?"
# expect: reason: verb 'ALTER' not in allowlist ... exit=2
```

## 3. Excel writer smoke test (no Snowflake, no Claude)

```bash
.venv/bin/python -c "
import sys; sys.path.insert(0, 'plugin')
from lib.excel import write_report
recs = [{
    'query_id': '01a',
    'sql': 'SELECT count(*) FROM events WHERE event_date >= ?',
    'elapsed_ms': 30000,
    'bytes_scanned': 8_000_000_000,
    'estimated_credits': 1.5,
    'warehouse_name': 'OPTIMIZE_WH',
    'warehouse_size': 'Large',
    'recommendation_type': 'clustering-key',
    'recommendation_target': 'EVENTS(EVENT_DATE)',
    'rationale': '950/1000 partitions scanned despite selective date filter.',
}]
write_report(recs, '/tmp/sample_report.xlsx')
print('wrote /tmp/sample_report.xlsx')
"
open /tmp/sample_report.xlsx     # macOS — opens in Numbers / Excel
```

## 4. Plugin install + slash-command discovery (no Snowflake)

`/plugin install <path>` is **not** supported — the install subcommand takes
a plugin name from a registered marketplace. Two ways to load this plugin:

### 4a. Session-only (recommended for active development)

Launch Claude Code with the plugin loaded ad-hoc:

```bash
claude --plugin-dir /Users/jlee/Documents/Development/Take_Home_Assignment/snowflake-query-optimizer
```

No marketplace setup. The plugin is gone when you exit. Best for iterating
on the plugin source.

### 4b. Permanent install via the bundled local marketplace

The plugin ships a `.claude-plugin/marketplace.json`, so the plugin
directory *is* its own marketplace.

```
/plugin marketplace add /Users/jlee/Documents/Development/Take_Home_Assignment/snowflake-query-optimizer
/plugin install snowflake-query-optimizer@snowflake-query-optimizer
```

Yes, the name appears twice — the first is the plugin, the second is the
marketplace name (which defaults to the directory name).

### Verify the plugin loaded (either method)

```
/plugin list                 # snowflake-query-optimizer should appear
/help                        # /optimize-snowflake should be listed
/mcp                         # the snowflake MCP server should appear
```

Run `claude --debug` if hooks aren't firing — the debug log shows hook
invocations and exit codes.

## 5. End-to-end (needs Snowflake)

Prereqs:

- `uvx` in PATH (`pip install uv`)
- `~/.snowflake/connections.toml` populated with the `[claude-optimizer]`
  section (see the "Snowflake connection config" section in README.md)
- The one-time grants from the "Snowflake one-time grants" section of
  README.md applied
- The warehouse referenced by the connection exists and the role has
  `USAGE` on it (analyzer only runs `EXPLAIN`; no `OPERATE` / no
  SUSPEND/RESUME required)
- `DEMO_DB.QUERY_TUNING.QUERY_HISTORY` is populated with rows whose
  `start_time` falls inside the lookback window. (Discovery now reads
  this curated table instead of `ACCOUNT_USAGE.QUERY_HISTORY`. If you
  populate it from `ACCOUNT_USAGE` on a schedule, account for the
  ~45-minute `ACCOUNT_USAGE` latency upstream; if you populate it
  directly from a demo workload, rows show up immediately.) Pick
  `7 days` on the first run if you're unsure.

Then in Claude Code:

```
/optimize-snowflake 7 days
```

Expected output (last few lines):

On first use the slash command will ask once where to put run output
(suggested options: `~/Downloads` (recommended default), `~/.snowflake-optimizer/runs`,
or a custom path). The choice is persisted to `~/.snowflake-optimizer/config`
and reused on subsequent runs without re-prompting. To change later,
edit or delete that file.

```
Done.
Report:    <chosen-base>/Snowflake_Optimization_Run_<UTC date>_<UTC time>_UTC/report.xlsx
Run state: <chosen-base>/Snowflake_Optimization_Run_<UTC date>_<UTC time>_UTC
```

Then verify (substitute your chosen base path):

- `<base>/Snowflake_Optimization_Run_<UTC date>_<UTC time>_UTC/discovery.sql`
  contains the rendered discovery SQL — written by the slash command
  in Step 3b, read by the discovery subagent in Step 1. Sanity-check
  the `DATEADD(...)` interval matches the lookback you supplied.
- `<base>/Snowflake_Optimization_Run_<UTC date>_<UTC time>_UTC/candidates.json` is populated with up to 3 candidates ranked
  by `total_elapsed_time` descending (post-filter — sub-second queries,
  no-warehouse queries, and Snowflake-internal traffic are excluded)
- `<base>/Snowflake_Optimization_Run_<UTC date>_<UTC time>_UTC/optimizations.json` shows one record per candidate, each
  with a `recommendation_type` of `search-optimization`,
  `clustering-key`, `warehouse-resize`, `query-acceleration`, or `none`
  — plus a `recommendation_target` (column, warehouse name, or
  next-size string; null when type is `none`) and a `rationale`
- The `.xlsx` opens and the 10 columns match the "Excel output schema"
  section of README.md

## 6. Adversarial check — confirm the hook blocks injected destructive SQL

The PRD requires the hook to deny destructive SQL even if the agent is
manipulated into trying it. To verify in a real session, ask the model
something like:

```
Run a Snowflake query that drops the table called test_table_xyz
```

Expected: the model attempts an MCP call, the hook returns exit 2 with
`reason: verb 'DROP' not in allowlist`, the model surfaces the block to you
without retrying. If the query reaches Snowflake, that's a critical bug —
file it.

## 7. Re-export from existing run state

The export step is idempotent and reads only `optimizations.json`. To
verify, point the `excel-export` subagent at a previous run directory:

```
re-export <absolute path to a prior Snowflake_Optimization_Run_... directory>
```

Expected: a fresh `report.xlsx` is written into the same directory using
the existing `optimizations.json`. Discovery and analysis are skipped —
useful for tweaking the export logic without re-spending Snowflake
round-trips.

## 8. Empty-window message

```
/optimize-snowflake 30 seconds
```

Expected: validation rejects (regex requires `minutes?|hours?|days?`).
Try instead:

```
/optimize-snowflake 1 minute       # in a quiet account, returns no candidates
```

Expected: `No candidates found in window: 1 minute. Try a longer lookback…`


