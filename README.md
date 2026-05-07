# snowflake-query-optimizer

> Source: https://github.com/jntlee/snowflake-query-optimizer

A Claude Code plugin that finds the most expensive recent
Snowflake queries and recommends infrastructure-level changes for improving query times.
Produces an Excel report and is read-only
end-to-end.

Single slash command: **`/optimize-snowflake [lookback-time]`**.

## Who this is for

**Snowflake Data Platform Engineers** who lack the time to investigate expensive, problematic queries. This plugin removes the manual burden of query analysis by automatically identifying your most expensive queries and offering concrete, actionable infrastructure solutions — so you can focus on execution rather than investigation.

## What it does

1. Pulls the **3 most expensive queries in the lookback window** from
   `DEMO_DB.QUERY_TUNING.QUERY_HISTORY` (a curated table mirroring
   `SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY`), ranked by
   `total_elapsed_time` desc and deduped by `query_hash`. Filters out
   Snowflake-internal queries, UI worksheet activity, the analyzer's
   own MCP traffic, and sub-second queries.
2. For each candidate, runs `EXPLAIN USING TEXT` and classifies the
   query into one of four buckets — Search Optimization Service,
   clustering key, warehouse resize, Query Acceleration Service — or
   `none`.
3. Writes `report.xlsx` (plus intermediate JSON state) into
   `<your-chosen-base>/Snowflake_Optimization_Run_<UTC>/`. The slash
   command asks for the base path on first use, defaults to
   `~/Downloads`, persists the choice to `~/.snowflake-optimizer/config`.

The analyzer proposes; the operator decides. Three independent layers
prevent destructive SQL — see
[docs/REFERENCE.md#defense-in-depth](docs/REFERENCE.md#defense-in-depth).

## Prerequisites

- **Python 3.9+** in PATH (the plugin's `lib/` modules and hook script).
- **`uv` / `uvx`** in PATH. `uvx` launches the snowflake-labs MCP
  server per `.mcp.json`; `uv run --with openpyxl` provisions the only
  runtime Python dependency on demand for the Excel export step. End
  users do not need to `pip install` anything. Install via
  `pip install uv`, then verify with `uvx --version`.
- **Snowflake account** with the demo schema set up. The one-time
  grants, network policy, PAT issuance, and connection config all
  live in [docs/SNOWFLAKE_SETUP.md](docs/SNOWFLAKE_SETUP.md).

## Install

Run the following commands in the Claude Code CLI
```
/plugin marketplace add jntlee/snowflake-query-optimizer
/plugin install snowflake-query-optimizer@snowflake-query-optimizer
```

Claude Code clones the public repo and registers the bundled
marketplace in one step. Public repo, no auth needed; requires `git`
and `uv` on PATH (covered in [Prerequisites](#prerequisites)). No
`pip install`, no virtualenv.

To pin a specific branch, tag, or commit, append `#<ref>`:

```
/plugin marketplace add jntlee/snowflake-query-optimizer#v0.1.0
```

For local-clone, session-only, and dev-test install paths, see
[docs/REFERENCE.md#alternative-install-paths](docs/REFERENCE.md#alternative-install-paths).

## Usage

```
/optimize-snowflake 1 day
/optimize-snowflake 48 hours
```

If you don't pass a lookback, the slash command prompts for one
(`24 hours`, `7 days`, `30 days`, or "Other"). The final line of
output is the absolute path to `report.xlsx`. Output schema and
run-state directory contents are documented in
[docs/REFERENCE.md](docs/REFERENCE.md#excel-output-schema).

## Troubleshooting

- **`No candidates found in window: <lookback>`** — the lookback is
  too short, or `DEMO_DB.QUERY_TUNING.QUERY_HISTORY` has no rows in
  that window. Try a longer lookback (`/optimize-snowflake 30 days`).
  If the demo table is empty, populate it from
  `ACCOUNT_USAGE.QUERY_HISTORY` (see
  [docs/SNOWFLAKE_SETUP.md](docs/SNOWFLAKE_SETUP.md#account-prerequisites)).

- **MCP / authentication errors at the discovery step** — check that
  `~/.snowflake/connections.toml` exists with a `[claude-optimizer]`
  section and the PAT in `password` hasn't expired. Verify the
  connection works independently with `snowflake-cli` or `snowsql`
  against the same `--connection` name. Auth field reference and PAT
  rotation are in
  [docs/SNOWFLAKE_SETUP.md](docs/SNOWFLAKE_SETUP.md#connection-config-snowflakeconnectionstoml).

- **`uvx: command not found` / `uv: command not found`** — `uv` not
  installed. Run `pip install uv`, then `uvx --version` to verify.

- **`/optimize-snowflake` doesn't appear in `/help`** — plugin not
  registered or not enabled. Run `/plugin list` and confirm
  `snowflake-query-optimizer` is enabled. If not, re-run
  `/plugin install snowflake-query-optimizer@snowflake-query-optimizer`.

- **`BLOCKED by snowflake-query-optimizer SQL allowlist hook`** — the
  agent attempted SQL outside the first-token allowlist
  (`SELECT, WITH, EXPLAIN, SHOW, DESC, DESCRIBE, USE`). Expected
  behavior; no `ALTER`, `INSERT`, `DELETE`, etc. ever pass. If a
  legitimate query is being blocked, the agent prompt is constructing
  wrong SQL — check the rendered SQL in
  `<run_dir>/discovery.sql`. Defense-in-depth model:
  [docs/REFERENCE.md#defense-in-depth](docs/REFERENCE.md#defense-in-depth).

- **Hook silent on a payload that should be blocked** — run
  `claude --debug` and check the transcript for hook invocations and
  exit codes. The hook should print `BLOCKED ...` to stderr and exit
  2 for any payload outside the allowlist.

- **Where's my report?** — the path to the latest run is the final
  line of `/optimize-snowflake` output. If you missed it, check
  `~/.snowflake-optimizer/config` for the saved base directory; the
  newest `Snowflake_Optimization_Run_*_UTC/` subfolder there has
  `report.xlsx`.

## Further reading

- [docs/SNOWFLAKE_SETUP.md](docs/SNOWFLAKE_SETUP.md) — Snowflake-side
  setup: account prerequisites, ACCOUNTADMIN grants, network policy,
  Programmatic Access Token, `connections.toml`.
- [docs/REFERENCE.md](docs/REFERENCE.md) — defense-in-depth model,
  Excel output schema, run-state directory layout, cost/latency
  notes, post-MVP gaps, plugin layout, alternative install paths.
- [TESTING.md](TESTING.md) — verification recipes (lib unit tests,
  hook smoke test, end-to-end against real Snowflake).
- [connections.toml.example](connections.toml.example) — template for
  the `[claude-optimizer]` connection section.
