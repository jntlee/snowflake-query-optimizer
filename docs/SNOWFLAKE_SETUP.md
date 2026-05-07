# Snowflake setup

Everything that happens on the Snowflake side, once, before installing
the plugin. After this, the plugin can authenticate and query the demo
table.

- [Account prerequisites](#account-prerequisites)
- [One-time grants (run as ACCOUNTADMIN)](#one-time-grants-run-as-accountadmin)
- [Network policy and Programmatic Access Token](#network-policy-and-programmatic-access-token)
- [Connection config (`~/.snowflake/connections.toml`)](#connection-config-snowflakeconnectionstoml)

## Account prerequisites

- A **Snowflake account** with `DEMO_DB.QUERY_TUNING.QUERY_HISTORY`
  populated — a curated table mirroring `ACCOUNT_USAGE.QUERY_HISTORY`.
  Easiest to populate via a scheduled task that copies from
  `ACCOUNT_USAGE.QUERY_HISTORY`; the demo workload SQL also drives
  candidates into this table. Reading from a curated copy sidesteps
  the ~45-minute latency of `ACCOUNT_USAGE`.
- A **service account** with the grants below.
- A **warehouse** for the analyzer's EXPLAIN calls (any size; X-Small
  is fine — EXPLAIN is cheap and doesn't actually run the candidate
  queries).

## One-time grants (run as ACCOUNTADMIN)

Below is the exact setup used during development. Adjust the names
(`OPTIMIZE_WH`, `CLAUDE_SERVICE`, `SERVICE_ROLE`, `DEMO_DB.QUERY_TUNING`)
to match your environment. `TYPE = SERVICE` blocks password login (no
MFA bypass) — for this plugin, authenticate with a Programmatic Access
Token (PAT) issued to the service user. PAT generation is shown after
the grants.

```sql
USE ACCOUNTADMIN;

-- 1) Warehouse for the analyzer's EXPLAIN calls. EXPLAIN is cheap and
--    doesn't run the candidate queries, so the warehouse is not under
--    sustained load.
CREATE OR REPLACE WAREHOUSE OPTIMIZE_WH
  WITH
    WAREHOUSE_TYPE = 'STANDARD'
    GENERATION = '2'
    WAREHOUSE_SIZE = 'X-Small'
    MAX_CLUSTER_COUNT = 1
    MIN_CLUSTER_COUNT = 1
    SCALING_POLICY = STANDARD
    AUTO_SUSPEND = 300
    AUTO_RESUME = TRUE
    INITIALLY_SUSPENDED = TRUE
    ENABLE_QUERY_ACCELERATION = FALSE
    QUERY_ACCELERATION_MAX_SCALE_FACTOR = 8
    MAX_CONCURRENCY_LEVEL = 8
    STATEMENT_QUEUED_TIMEOUT_IN_SECONDS = 0
    STATEMENT_TIMEOUT_IN_SECONDS = 172800
;

-- 2) Service user. TYPE = SERVICE blocks password login (no MFA bypass);
--    we authenticate with a Programmatic Access Token (PAT) issued below.
--    If this plugin was to be productionalized we would instead use OAuth for better security.
CREATE USER CLAUDE_SERVICE
  DEFAULT_WAREHOUSE = OPTIMIZE_WH
  TYPE = SERVICE;

-- 3) Role + bind to user.
CREATE ROLE SERVICE_ROLE;
GRANT ROLE SERVICE_ROLE TO USER CLAUDE_SERVICE;

-- 4) Warehouse permissions. USAGE is enough for the analyzer (EXPLAIN
--    only).
GRANT USAGE ON WAREHOUSE OPTIMIZE_WH TO ROLE SERVICE_ROLE;

-- 5) Read access to the schema(s) you want to optimize queries against.
--    FUTURE TABLES covers tables added later without re-granting.
GRANT USAGE  ON DATABASE DEMO_DB                                TO ROLE SERVICE_ROLE;
GRANT USAGE  ON SCHEMA   DEMO_DB.QUERY_TUNING                   TO ROLE SERVICE_ROLE;
GRANT SELECT ON ALL TABLES    IN SCHEMA DEMO_DB.QUERY_TUNING    TO ROLE SERVICE_ROLE;
GRANT SELECT ON FUTURE TABLES IN SCHEMA DEMO_DB.QUERY_TUNING    TO ROLE SERVICE_ROLE;
```

## Network policy and Programmatic Access Token

After running the grants, configure a network policy for
`CLAUDE_SERVICE` and issue a Programmatic Access Token. Snowflake
requires service users to have a network policy attached before they
can authenticate with a PAT.

> ⚠️ **Demo only.** The network rule below allows ingress from any
> IPv4 address (`0.0.0.0/0`). This is acceptable for a single-developer
> evaluation, but for any real deployment you would scope the rule to
> specific developer IP ranges, your VPN egress IPs, or your CI/CD
> provider's egress ranges. Never give a service account unrestricted
> network access in production.

```sql
-- Network rule + policy (DEMO ONLY — restrict the IP list for real use).
CREATE NETWORK RULE IF NOT EXISTS DEMO_DB.QUERY_TUNING.ALLOW_ALL_RULE
  MODE = INGRESS
  TYPE = IPV4
  VALUE_LIST = ('0.0.0.0/0');

CREATE NETWORK POLICY ALLOW_ALL_POLICY
  ALLOWED_NETWORK_RULE_LIST = ('DEMO_DB.QUERY_TUNING.ALLOW_ALL_RULE');

ALTER USER CLAUDE_SERVICE SET NETWORK_POLICY = 'ALLOW_ALL_POLICY';

-- Issue the PAT. Snowflake returns the token value ONCE — copy it
-- immediately into ~/.snowflake/connections.toml. You can't retrieve
-- it again, only rotate.
ALTER USER IF EXISTS CLAUDE_SERVICE
  ADD PROGRAMMATIC ACCESS TOKEN svc_pat
    ROLE_RESTRICTION = 'SERVICE_ROLE'
    DAYS_TO_EXPIRY = 30;
```

`ROLE_RESTRICTION` ensures the token can only be used with
`SERVICE_ROLE` even if the user is granted other roles.
`DAYS_TO_EXPIRY = 30` is short on purpose for a demo — rotate before
it expires; bump to 90 if you want.

## Connection config (`~/.snowflake/connections.toml`)

Snowflake auth (account, user, role, warehouse, db, schema, PAT) lives
in the standard `~/.snowflake/connections.toml` file — the same file
`snowsql`, `snowflake-cli`, and the Python connector all read. The
plugin references the connection by name (`claude-optimizer`).

Why a named connection instead of `[default]`: pointing the plugin at
your default Snowflake connection risks accidentally querying your
prod account on every `/optimize-snowflake` run. A scoped connection
makes the target explicit and limits blast radius to whatever
role/warehouse you provisioned for this plugin.

The repo ships [`connections.toml.example`](../connections.toml.example)
as a template. If you don't already have a `connections.toml`, run:

```bash
mkdir -p ~/.snowflake
# Copy the template (or merge the [claude-optimizer] section into your existing file)
cp connections.toml.example ~/.snowflake/connections.toml
# Fill in account, paste the PAT into `password`
chmod 600 ~/.snowflake/connections.toml      # secrets — restrict perms
```

The `[claude-optimizer]` section needs:

| Key | Example | Notes |
|---|---|---|
| `account` | `xy12345.us-east-1` | account locator |
| `user` | `CLAUDE_SERVICE` | service-type user from the grants section above |
| `role` | `SERVICE_ROLE` | role with the grants from above |
| `warehouse` | `OPTIMIZE_WH` | default warehouse for queries |
| `database` | `DEMO_DB` | default database |
| `schema` | `QUERY_TUNING` | default schema |
| `authenticator` | `programmatic_access_token` | tells the connector to use PAT instead of password |
| `password` | `<your PAT value>` | the PAT from `ALTER USER … ADD PROGRAMMATIC ACCESS TOKEN`. Snowflake's connector accepts PATs via the `password` field |

If you'd rather use a different connection name, edit
`--connection-name` in [`plugin/.mcp.json`](../plugin/.mcp.json) to
match.

The analyzer agent uses whichever warehouse the connection points at;
there are no plugin-specific env vars to set. The `warehouse` field in
your `connections.toml` is the single source of truth.
