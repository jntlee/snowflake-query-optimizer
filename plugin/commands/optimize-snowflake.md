---
description: Find the top 3 most expensive recent Snowflake queries and recommend infrastructure changes (Search Optimization Service, clustering key, warehouse resize, Query Acceleration Service) to speed them up. Emits an Excel report.
argument-hint: "[lookback, e.g. '24 hours' or '7 days']"
allowed-tools: Bash, Read, Write, Task, AskUserQuestion
---

# /optimize-snowflake

You orchestrate the three-agent Snowflake query analysis pipeline. The
heavy lifting lives in the subagents and `lib/` modules; your job is
sequencing, state setup, and user-facing messages. Do not call MCP tools
yourself — dispatch the subagents.

## Step 1 — Resolve the lookback window

`$ARGUMENTS` is the user-supplied lookback (e.g. `"24 hours"`, `"7 days"`).

- If `$ARGUMENTS` is empty, ask the user with AskUserQuestion (offer
  `"24 hours"`, `"7 days"`, `"30 days"`). Do not pick a default silently.
- If the lookback is shorter than 1 hour, warn the user once that
  `SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY` has roughly 45-minute latency and
  the pipeline may find fewer candidates than recent activity would suggest.

Validate the lookback by running the builder — it will raise if the format
or unit is wrong. Pass `$ARGUMENTS` as a separate argv value rather than
interpolating it into the Python source, so a malicious lookback can't
escape the string literal:

```bash
PYTHONPATH="${CLAUDE_PLUGIN_ROOT}" python3 -c "import sys; from lib.discovery_sql import build_discovery_sql; build_discovery_sql(sys.argv[1])" "$ARGUMENTS"
```

If that exits non-zero, surface the error verbatim and stop.

## Step 2 — Resolve where to write run output

Run output base is stored once at `~/.snowflake-optimizer/config` (a single
line containing the absolute path). Ask only on the first invocation.

### 2a. Check for a saved choice

```bash
CONFIG_FILE="$HOME/.snowflake-optimizer/config"
if [ -s "$CONFIG_FILE" ]; then
  RUN_BASE="$(head -n1 "$CONFIG_FILE")"
  echo "Using saved runs base: $RUN_BASE"
fi
```

If `RUN_BASE` is now set and the directory exists/is writable, skip to
Step 3. If the saved path is no longer writable (deleted, permissions
changed), treat as if no saved choice exists and re-prompt.

### 2b. First-time prompt

If no saved choice, ask the user via AskUserQuestion. Offered options:

- `~/Downloads` — **(Recommended)** the macOS default; visible in Finder,
  easy to find the Excel report. **Default if the user doesn't specify
  a value** (skip / cancel / empty answer).
- `~/.snowflake-optimizer/runs` — hidden home directory; keeps the runs
  out of your visible folders.

The "Other" option is provided automatically by AskUserQuestion if they
want a custom path.

Take the user's answer as `RUN_BASE` (or default to `~/Downloads` if no
answer was given). Expand any `~` or `$VAR` via Python rather than
`eval` — `eval` on user-supplied input is a shell-injection vector
(`~/foo; rm -rf ~` would execute the trailing command):

```bash
RUN_BASE="$(python3 -c 'import os, sys; print(os.path.expanduser(os.path.expandvars(sys.argv[1])))' "${RUN_BASE:-~/Downloads}")"
mkdir -p "$RUN_BASE" || { echo "Cannot create $RUN_BASE — pick a writable path."; exit 1; }
```

### 2c. Persist the choice

Save the resolved path so future runs don't re-prompt:

```bash
mkdir -p "$HOME/.snowflake-optimizer"
printf '%s\n' "$RUN_BASE" > "$CONFIG_FILE"
echo "Saved runs base to $CONFIG_FILE — delete or edit this file to change it."
```

Future invocations of `/optimize-snowflake` will read from this file and
skip the prompt.

## Step 3 — Create the per-run state directory

Folder name format: `Snowflake_Optimization_Run_<UTC date>_<UTC time>_UTC`
(e.g. `Snowflake_Optimization_Run_2026-05-06_14-23-45_UTC`). UTC keeps
folder names consistent across users and avoids DST surprises; the
hyphenated date format is filesystem-safe and lexicographically sortable.

```bash
RUN_DIR="$RUN_BASE/Snowflake_Optimization_Run_$(date -u +%Y-%m-%d_%H-%M-%S)_UTC"

# Same-second collision (rare): append _2, _3, ...
base="$RUN_DIR"
i=2
while [ -e "$RUN_DIR" ]; do
  RUN_DIR="${base}_${i}"
  i=$((i + 1))
done

mkdir -p "$RUN_DIR"
echo "Run directory: $RUN_DIR"
export RUN_DIR
export LOOKBACK="$ARGUMENTS"
```

`RUN_DIR` and `LOOKBACK` must be visible to subagents. The export runs in
the same process tree, so subagent shell tasks inherit them.

## Step 3b — Render the discovery SQL once

Render the discovery SQL string here, in the slash command, and write
it to `$RUN_DIR/discovery.sql`. The discovery subagent reads that file
instead of shelling out to Python — saves one subprocess round trip
and keeps the subagent purely model + Read + MCP.

```bash
PYTHONPATH="${CLAUDE_PLUGIN_ROOT}" python3 -c "import sys; from lib.discovery_sql import build_discovery_sql; sys.stdout.write(build_discovery_sql(sys.argv[1]))" "$LOOKBACK" > "$RUN_DIR/discovery.sql"
```

If that exits non-zero, surface the error verbatim and stop — the
lookback was already validated in Step 1, so the only realistic cause
of failure here is a missing/broken `lib.discovery_sql` import, which
warrants stopping rather than retrying.

## Step 4 — Discover candidates

Dispatch the `query-discovery` subagent via the Task tool (`subagent_type:
query-discovery`). Pass it the RUN_DIR and LOOKBACK in the prompt so it
doesn't have to guess.

If the subagent reports `empty: true`, print:

```
No candidates found in window: <LOOKBACK>.
Try a longer lookback (e.g. '7 days' or '30 days').
```

…and stop. Do not proceed to optimization or export.

## Step 5 — Analyze each candidate

Dispatch the `query-optimizer` subagent. For each of up to 3 candidates,
it runs `EXPLAIN USING TEXT`, classifies the query against the four
infrastructure-recommendation buckets (Search Optimization Service,
clustering key, warehouse resize, Query Acceleration Service) plus
`none`, and writes `optimizations.json`. The agent is read-only — no
warehouse SUSPEND/RESUME, no candidate SQL execution. Wait for completion
before step 6.

## Step 6 — Export

Dispatch the `excel-export` subagent. It writes `$RUN_DIR/report.xlsx`.

## Step 7 — Surface the artifacts

Print exactly:

```
Done.
Report:    <absolute path to report.xlsx>
Run state: <absolute path to RUN_DIR>
```

The user opens the .xlsx; the run dir contains all intermediate JSON for
debugging or re-running the export step.

## Re-running export only

If the user follows up with something like `re-export <RUN_DIR>`, skip
steps 1–5 and dispatch the `excel-export` subagent against the existing
`RUN_DIR`. Discovery and optimization don't have to repeat — that's the
whole point of persisting `optimizations.json`.
