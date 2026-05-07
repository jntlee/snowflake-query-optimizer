# Reference

Internal/technical detail. End users don't need most of this — see
[../README.md](../README.md) for install + usage. This doc covers:

- [Defense in depth](#defense-in-depth) — security model
- [Excel output schema](#excel-output-schema) — what's in `report.xlsx`
- [Run state directory](#run-state-directory) — intermediate files
- [Cost notes](#cost-notes) — why the pipeline is cheap
- [Latency notes](#latency-notes) — why the pipeline is fast
- [Post-MVP gaps](#post-mvp-gaps) — out of scope
- [Plugin layout](#plugin-layout) — directory structure
- [Alternative install paths](#alternative-install-paths) — local
  clone, session-only dev, running unit tests

## Defense in depth

The plugin **never** issues destructive SQL. Three independent layers
prevent it:

| Layer | What it blocks |
|---|---|
| Snowflake RBAC | Service account has read-only on the target schema; no write privileges anywhere |
| MCP `sql_statement_permissions` | Only Select/With/Describe/Show/Use/Explain (plus the parser's `Command`/`Unknown` fallback for Snowflake-specific syntax sqlglot can't classify) permitted at the MCP layer |
| `PreToolUse` hook | First-token allowlist `{SELECT, WITH, EXPLAIN, SHOW, DESC, DESCRIBE, USE}`; Hook has a similar allowlist to the MCP server, but the hook is needed to automatically reject multi-statement payloads |

No `ALTER` form is permitted at any layer — the warehouse-resize
recommendation surfaces in the Excel report; the operator runs the
resize themselves if they agree. Allowlist semantics: a parser bug
fails to "blocks legitimate query," not "allows destructive query."

## Excel output schema

Single sheet, one row per candidate (up to 3). Columns:

`query_id, sql, elapsed_sec, gb_scanned, estimated_credits,
warehouse_name, warehouse_size, recommendation_type,
recommendation_target, rationale`

Unit conventions:
- **`elapsed_sec`** is in **seconds** (raw stat from Snowflake is ms;
  the writer divides by 1000, 2-decimal precision).
- **`gb_scanned`** is in **GB** (decimal, /1e9, 2-decimal precision).
- The internal JSON (`optimizations.json`) keeps raw bytes/ms values
  for fidelity; conversion happens only at Excel write time.

`recommendation_type` is one of:
- `search-optimization` — point-lookup queries on high-cardinality
  columns. `recommendation_target` is the column(s) to enable SOS on.
- `clustering-key` — range scans on unclustered data.
  `recommendation_target` is the proposed clustering-key expression
  (e.g. `EVENTS(EVENT_DATE)`).
- `warehouse-resize` — heavy aggregations that spill.
  `recommendation_target` is the proposed next size up
  (e.g. `Medium → Large`).
- `query-acceleration` — wide partition scans with no selective filter.
  `recommendation_target` is the warehouse name to enable QAS on.
- `none` — no clear pattern match. `recommendation_target` is blank;
  `rationale` explains what the analyzer saw.

`rationale` is a one-or-two-sentence justification citing the specific
EXPLAIN operator and/or candidate metric that triggered the
recommendation.

## Run state directory

The slash command prompts for a base directory **once**, on the first
invocation. The choice is saved to `~/.snowflake-optimizer/config`
(single line containing the absolute path) and reused on subsequent
invocations without prompting.

Suggested defaults at first-run prompt:
- `~/Downloads` (recommended; the default if you skip the prompt —
  easy to find in Finder)
- `~/.snowflake-optimizer/runs` (hidden, out of the way)
- Custom path (the "Other" option in the prompt)

Inside the chosen base, each run writes a subdirectory named with a
UTC timestamp:
`Snowflake_Optimization_Run_<YYYY-MM-DD>_<HH-MM-SS>_UTC/`
(e.g. `Snowflake_Optimization_Run_2026-05-06_14-23-45_UTC`). UTC keeps
the names consistent across users and DST changes; same-second
collisions get `_2`, `_3` suffixes. Inside:

- `discovery.sql` — the rendered discovery SQL the slash command built
  via `lib.discovery_sql.build_discovery_sql`. The discovery subagent
  reads it directly instead of re-rendering it via a Python subprocess.
- `candidates.json` — output of discovery
- `optimizations.json` — output of the optimizer (one record per candidate)
- `report.xlsx` — output of export

Re-running the export step alone is supported — re-dispatch the
`excel-export` agent against an existing `RUN_DIR`.

## Cost notes

The analyzer pipeline is intentionally cheap:

- Discovery is one query against `DEMO_DB.QUERY_TUNING.QUERY_HISTORY`
  per run.
- The analyzer fires `EXPLAIN USING TEXT` for every candidate
  **in parallel** — all 3 EXPLAINs go out in a single batched
  dispatch, so the analysis phase costs roughly one round trip rather
  than three. EXPLAIN does not run the underlying query; it returns
  the operator tree only. Cost per EXPLAIN is essentially the
  round-trip time, not the candidate query's actual cost.
- The candidate SQL is **never executed** by this pipeline. None of
  the recommendations (Search Optimization, clustering keys, warehouse
  resize, Query Acceleration) are auto-applied; the operator runs them
  themselves if they agree.
- A resource monitor on the warehouse is still a good idea as a
  belt-and-braces cost ceiling, but the analyzer's own footprint is
  minimal.

## Latency notes

The dominant cost in the pipeline is model latency, not Snowflake. A
few choices keep the slash command snappy:

- **Sub-agent models are pinned per role.** Discovery is purely
  procedural (validate → run one SQL → write one file), so it runs on
  Claude Haiku 4.5 (`claude-haiku-4-5-20251001`). The analyzer needs
  to classify 3 candidates in one pass, which Claude Sonnet 4.6
  (`claude-sonnet-4-6`) handles with no quality loss vs Opus. The
  Excel exporter inherits whatever the parent runs, since it's
  already fast. See the `model:` field in each agent's frontmatter.
- **Classification criteria are inlined into the analyzer prompt.**
  The five-bucket selection rules and caveats are duplicated from
  [`plugin/skills/query-optimizer/SKILL.md`](../plugin/skills/query-optimizer/SKILL.md)
  into [`plugin/agents/query-optimizer.md`](../plugin/agents/query-optimizer.md)'s
  `## Classification criteria` section, so the subagent does not have
  to load the skill at runtime. The skill remains the source of truth
  for humans and for the skill being invoked outside this subagent —
  keep both copies in sync.
- **Discovery SQL is rendered once, by the slash command.** The slash
  command writes the rendered SQL to `$RUN_DIR/discovery.sql` before
  dispatching the discovery subagent; the subagent reads that file
  directly instead of shelling out to a Python subprocess to re-render
  it. Saves one subprocess and trims the discovery agent's tool list.
- **Obvious spill candidates skip EXPLAIN entirely.** The analyzer
  scans `candidates.json` first and pre-classifies any candidate with
  `bytes_spilled_to_local_storage > 0` or
  `bytes_spilled_to_remote_storage > 0` as `warehouse-resize` directly
  from the QUERY_HISTORY metadata — spill is unambiguous and EXPLAIN
  cannot change the conclusion. Only the remaining candidates go
  through the parallel-EXPLAIN batch, which is the per-candidate
  bottleneck.

## Post-MVP gaps

Things deliberately out of scope for the MVP. Knowing them is part of
graduating to production:

- **Query level optimization** The original plan for the demo was to
  optimize queries but due to time constraints from the demo, any
  runs of the optimized query would take up too much of the 90 second
  demo time frame.
- **Further Security** When conducting query optimizations, the
  service account must test the optimized queries. Would implement
  hashing to prevent any PII/PHI from being exposed along with other
  measures.
- **Query optimized log** This will be a reference table created in
  Snowflake so in repeated runs, queries that have already been
  reviewed in the past will not be reviewed again.
- **Per-user / per-run usage caps** (query count, bytes scanned,
  credit budget).
- **Audit log** of every issued query and recommendation diff. The
  per-run JSON files are debugging state, not a structured audit
  trail.
- **Secret manager / OS keychain integration**. The PAT lives in
  plain text in `~/.snowflake/connections.toml` (file-perm-restricted
  via `chmod 600`); a real deployment would route through Vault, AWS
  Secrets Manager, macOS Keychain, etc.
- **OAuth** instead of PAT. The Snowflake Python connector supports
  it (`authenticator = "oauth"` in connections.toml), but PAT is
  simpler for the demo and doesn't need an external IdP.
- **Notifications on completion** (Slack, email, PagerDuty).
- **Configurable lookback / top-N**. `lookback` is user-supplied per
  invocation; the candidate count is hardcoded at 3. The `limit` arg
  on `build_discovery_sql` accepts any value 1–1000; only the default
  needs lifting if you want more.
- **Auto-applying any recommendation**. By design, every
  recommendation (Search Optimization, clustering key, warehouse
  resize, Query Acceleration) is surfaced in the report only — the
  operator runs the corresponding DDL themselves if they agree. This
  keeps the hook's allowlist tight (no `ALTER … SET WAREHOUSE_SIZE`,
  no `ALTER TABLE … CLUSTER BY`, etc.) and keeps cost decisions
  human-owned.

## Plugin layout

```
snowflake-query-optimizer/                           # marketplace + project root
├── .claude-plugin/
│   └── marketplace.json                             # marketplace manifest
├── README.md, TESTING.md
├── docs/
│   ├── SNOWFLAKE_SETUP.md                           # one-time Snowflake setup
│   └── REFERENCE.md                                 # this file
├── pyproject.toml                                   # pytest + setuptools point at plugin/
├── .gitignore
└── plugin/                                          # the plugin proper
    ├── .claude-plugin/
    │   └── plugin.json
    ├── .mcp.json                                    # snowflake-labs MCP server
    ├── mcp/tools_config.yaml                        # narrowed sql_statement_permissions
    ├── commands/optimize-snowflake.md               # the slash command
    ├── agents/
    │   ├── query-discovery.md
    │   ├── query-optimizer.md
    │   └── excel-export.md
    ├── skills/query-optimizer/
    │   ├── SKILL.md
    │   └── references/{search-optimization,clustering-key,warehouse-sizing,query-acceleration}.md
    ├── lib/                                         # pure-python, unit-tested
    │   ├── allowlist.py
    │   ├── discovery_sql.py
    │   └── excel.py
    ├── hooks/
    │   ├── hooks.json                               # PreToolUse matcher → script
    │   └── sql_allowlist_hook.py                    # stdin envelope → lib.allowlist.check
    └── tests/                                       # pytest, no Snowflake mocks
```

The marketplace lives at the top, the plugin in `plugin/`. This
separation is what `/plugin marketplace add` and `/plugin install`
require — a marketplace cannot point at itself, only at a
subdirectory.

## Alternative install paths

The README documents the recommended GitHub install path. These are
the alternatives.

### From a local clone

Useful if you already have the repo on disk (e.g. you're modifying
the plugin and want Claude Code to load the working copy):

```bash
git clone https://github.com/jntlee/snowflake-query-optimizer.git
```

```
/plugin marketplace add /absolute/path/to/snowflake-query-optimizer
/plugin install snowflake-query-optimizer@snowflake-query-optimizer
```

### Session-only, for plugin development

No marketplace registration; the plugin is loaded for the current
`claude` session only:

```bash
claude --plugin-dir /absolute/path/to/snowflake-query-optimizer/plugin
```

Note `plugin/` at the end — `--plugin-dir` points directly at the
plugin, not the marketplace.

### Running the unit tests locally

Only needed if you're **modifying** the plugin's `lib/` modules. End
users on the GitHub or local-clone install path do not need this step
— `uv` handles runtime dependencies for them.

```bash
git clone https://github.com/jntlee/snowflake-query-optimizer.git
cd snowflake-query-optimizer
python3 -m venv .venv
.venv/bin/pip install -U pip      # 3.9 bundles an older pip that can't do PEP 660 editable installs
.venv/bin/pip install -e '.[dev]' # installs openpyxl + pytest, makes lib/ importable
.venv/bin/pytest                  # 129 unit tests; should all pass
```

See [`../TESTING.md`](../TESTING.md) for verification recipes.
