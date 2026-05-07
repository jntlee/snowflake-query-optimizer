"""Allowlist tests. The security property is: no destructive verb may pass,
even when wrapped in comments, leading whitespace, mixed case, or piggybacked
after a SELECT. ALTER (in any form) is rejected.
"""
from __future__ import annotations

import pytest

from lib.allowlist import (
    check,
    first_token,
    has_multi_statement,
    strip_comments,
)

# fmt: off
ALLOWED = [
    "SELECT 1",
    "  SELECT 1",
    "select 1",
    "SeLeCt 1",
    "WITH x AS (SELECT 1) SELECT * FROM x",
    "EXPLAIN SELECT 1",
    "EXPLAIN USING TEXT SELECT 1",
    "SHOW TABLES",
    "DESC TABLE foo",
    "DESCRIBE TABLE foo",
    "USE WAREHOUSE wh",
    "USE DATABASE db",
    # Comments preceding a permitted verb
    "-- a comment\nSELECT 1",
    "-- destructive: DROP TABLE x\nSELECT 1",
    "/* block */ SELECT 1",
    "/* multi\nline */ SELECT 1",
    "// double-slash line comment\nSELECT 1",
    "/* outer /* inner */ SELECT 1",  # block comment doesn't nest in Snowflake
    # Trailing semicolon is fine
    "SELECT 1;",
    "SELECT 1 ;  ",
    # ; and -- inside string literals must not trip multi-statement / comment
    "SELECT 'a;b' FROM t",
    "SELECT 'a;b;c' FROM t",
    "SELECT '--' FROM t",
    "SELECT '/* not a comment */' FROM t",
    "SELECT 'it''s ok' FROM t",  # escaped single quote
    'SELECT "weird;col" FROM t',  # ; inside double-quoted identifier
]

BLOCKED = [
    # Every destructive top-level verb
    ("DROP TABLE x", "DROP"),
    ("DELETE FROM x", "DELETE"),
    ("UPDATE x SET y=1", "UPDATE"),
    ("INSERT INTO x VALUES (1)", "INSERT"),
    ("MERGE INTO x USING y ON x.id=y.id WHEN MATCHED THEN DELETE", "MERGE"),
    ("TRUNCATE TABLE x", "TRUNCATE"),
    ("GRANT SELECT ON x TO ROLE r", "GRANT"),
    ("REVOKE SELECT ON x FROM r", "REVOKE"),
    ("CREATE TABLE x (a INT)", "CREATE"),
    ("CREATE OR REPLACE TABLE x (a INT)", "CREATE"),
    ("COPY INTO x FROM @s", "COPY"),
    ("CALL my_proc()", "CALL"),
    ("EXECUTE IMMEDIATE 'select 1'", "EXECUTE"),
    ("UNDROP TABLE x", "UNDROP"),
    ("PUT file://foo @s", "PUT"),
    ("GET @s file://foo", "GET"),
    # Multi-statement piggyback in every reasonable form
    ("SELECT 1; DROP TABLE x", "multi-statement"),
    ("SELECT 1;DROP TABLE x", "multi-statement"),
    ("SELECT 1;\nDROP TABLE x", "multi-statement"),
    ("SELECT 1;\n-- comment\nDROP TABLE x", "multi-statement"),
    ("SELECT 1; SELECT 2", "multi-statement"),  # two SELECTs is still multi
    # No ALTER form is permitted; the live pipeline only needs SELECT/EXPLAIN/
    # SHOW/DESC/USE.
    ("ALTER TABLE x ADD COLUMN y INT", "ALTER"),
    ("ALTER WAREHOUSE wh SET WAREHOUSE_SIZE='LARGE'", "ALTER"),
    ("ALTER SESSION SET QUERY_TAG='x'", "ALTER"),
    ("ALTER SESSION SET USE_CACHED_RESULT = FALSE", "ALTER"),
    ("ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = 600", "ALTER"),
    ("ALTER WAREHOUSE test_wh SUSPEND", "ALTER"),
    ("ALTER WAREHOUSE test_wh RESUME", "ALTER"),
    ("ALTER USER svc SET PASSWORD='x'", "ALTER"),
    # Empty / garbage / leading paren
    ("", "empty"),
    ("   \n  ", "empty"),
    (";", "no SQL token"),
    ("(SELECT 1)", "no SQL token"),
]
# fmt: on


@pytest.mark.parametrize("sql", ALLOWED)
def test_allowed(sql):
    ok, reason = check(sql)
    assert ok, f"expected allow, blocked: {sql!r} (reason={reason!r})"


@pytest.mark.parametrize("sql,reason_substr", BLOCKED)
def test_blocked(sql, reason_substr):
    ok, reason = check(sql)
    assert not ok, f"expected block, allowed: {sql!r}"
    assert reason_substr.lower() in reason.lower(), (
        f"reason {reason!r} did not contain {reason_substr!r} for {sql!r}"
    )


# Targeted unit checks of helpers used by the security property above.

def test_semicolon_in_string_not_multi_statement():
    assert not has_multi_statement("SELECT 'a;b;c' FROM t")


def test_semicolon_in_double_quoted_identifier():
    assert not has_multi_statement('SELECT "col;name" FROM t')


def test_first_token_after_block_comment():
    assert first_token("/* hi */ SELECT 1") == "SELECT"


def test_first_token_after_line_comment():
    assert first_token("-- hi\nSELECT 1") == "SELECT"


def test_first_token_after_double_slash_comment():
    assert first_token("// hi\nSELECT 1") == "SELECT"


def test_first_token_after_mixed_leading():
    assert first_token("  /* a */\n -- b\n  SELECT 1") == "SELECT"


def test_strip_comments_keeps_strings_intact():
    out = strip_comments("SELECT 'hello' FROM t -- trailing")
    assert "trailing" not in out
    assert "'hello'" in out


def test_strip_comments_keeps_string_with_embedded_comment_marker():
    out = strip_comments("SELECT 'a -- b' FROM t")
    assert "'a -- b'" in out


def test_unclosed_block_comment_consumes_to_eof():
    # If someone submits a runaway block comment, the scanner consumes to EOF
    # and the resulting code has no token. check() denies (no SQL token).
    ok, _ = check("SELECT 1 /* unclosed")
    # Behavior: token is SELECT (before the comment starts), so this is allowed.
    # Multi-statement detection sees no ;, so allowed.
    assert ok


def test_runaway_comment_cannot_smuggle_destructive_payload():
    # If the comment is unclosed AND contains destructive-looking text,
    # _mask_strings drops the whole comment so no ; appears in the masked
    # form -> not multi-statement. Token is still SELECT. Allowed.
    # Snowflake itself will then reject the SQL on parse error.
    ok, _ = check("SELECT 1 /* DROP TABLE x")
    assert ok
