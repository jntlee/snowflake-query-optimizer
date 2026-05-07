"""SQL allowlist parser used by the PreToolUse hook.

Allowlist semantics: a parser bug fails to "blocks legitimate query," never
"allows destructive query." The first non-whitespace, non-comment token must
be in ALLOWED_VERBS. Multi-statement payloads (a `;` followed by more SQL)
are always rejected, even if both halves would be allowed individually.

Pure module: no I/O, no Snowflake dependency. Tested in tests/test_allowlist.py.
"""
from __future__ import annotations

import re
from typing import Iterable

ALLOWED_VERBS: frozenset[str] = frozenset(
    {"SELECT", "WITH", "EXPLAIN", "SHOW", "DESC", "DESCRIBE", "USE"}
)


def _scan(sql: str) -> Iterable[tuple[str, str]]:
    """Yield (kind, text) runs. Kinds: code, line_comment, block_comment,
    string, ident_string. The scanner is SQL-aware enough to keep ; and
    comment markers inside string literals from being mistaken for
    statement separators or comment starts.
    """
    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if (c == "-" and nxt == "-") or (c == "/" and nxt == "/"):
            j = i
            while j < n and sql[j] != "\n":
                j += 1
            yield ("line_comment", sql[i:j])
            i = j
            continue
        if c == "/" and nxt == "*":
            j = i + 2
            while j < n and not (sql[j] == "*" and j + 1 < n and sql[j + 1] == "/"):
                j += 1
            j = min(j + 2, n)
            yield ("block_comment", sql[i:j])
            i = j
            continue
        if c == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'" and j + 1 < n and sql[j + 1] == "'":
                    j += 2
                    continue
                if sql[j] == "'":
                    j += 1
                    break
                j += 1
            yield ("string", sql[i:j])
            i = j
            continue
        if c == '"':
            j = i + 1
            while j < n:
                if sql[j] == '"' and j + 1 < n and sql[j + 1] == '"':
                    j += 2
                    continue
                if sql[j] == '"':
                    j += 1
                    break
                j += 1
            yield ("ident_string", sql[i:j])
            i = j
            continue
        # plain code run: consume until next special start
        j = i
        while j < n:
            cc = sql[j]
            nn = sql[j + 1] if j + 1 < n else ""
            if cc in ("'", '"'):
                break
            if (cc == "-" and nn == "-") or (cc == "/" and nn == "/"):
                break
            if cc == "/" and nn == "*":
                break
            j += 1
        yield ("code", sql[i:j])
        i = j


def strip_comments(sql: str) -> str:
    """Remove line and block comments. Strings are preserved verbatim."""
    return "".join(t for k, t in _scan(sql) if k not in ("line_comment", "block_comment"))


def _mask_strings(sql: str) -> str:
    """Strip comments and replace string/identifier contents with underscores
    so structural scans (first-token, multi-statement detection) cannot be
    fooled by quoted ; or quoted comment markers. Delimiters preserved so the
    overall character offsets are usable.
    """
    out: list[str] = []
    for kind, text in _scan(sql):
        if kind in ("line_comment", "block_comment"):
            continue
        if kind == "string":
            out.append("'" + "_" * max(0, len(text) - 2) + "'")
            continue
        if kind == "ident_string":
            out.append('"' + "_" * max(0, len(text) - 2) + '"')
            continue
        out.append(text)
    return "".join(out)


_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")


def first_token(sql: str) -> str | None:
    """Return the first SQL keyword/identifier (uppercase) after stripping
    comments and leading whitespace, or None if the input has no token."""
    s = _mask_strings(sql).lstrip()
    if not s:
        return None
    m = _TOKEN_RE.match(s)
    return m.group(0).upper() if m else None


def has_multi_statement(sql: str) -> bool:
    """True iff a `;` is followed by any non-whitespace, non-comment content.
    Trailing `;` alone is fine."""
    s = _mask_strings(sql)
    for m in re.finditer(r";", s):
        if s[m.end() :].strip():
            return True
    return False


def check(sql: str) -> tuple[bool, str]:
    """Return (allowed, reason). Deny by default. Multi-statement is checked
    first so it's reported even when the leading verb would otherwise also
    be a legitimate denial reason — the security-relevant fact is the
    piggyback, not the leading verb."""
    if not sql or not sql.strip():
        return False, "empty SQL"
    if has_multi_statement(sql):
        return False, "multi-statement payload not allowed"
    tok = first_token(sql)
    if tok is None:
        return False, "no SQL token found at start"
    if tok not in ALLOWED_VERBS:
        return False, f"verb {tok!r} not in allowlist"
    return True, f"verb {tok!r} permitted"
