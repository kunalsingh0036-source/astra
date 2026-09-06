"""Every column named in store.py's raw SQL must exist in the schema.

`last_successful_probes` shipped selecting `args ->> 'target'`. The
column is `args_raw`. Nothing caught it: the only caller returned early
on a control check that always failed first, so the query was
unreachable until that control was fixed — and then the capability
watcher crashed on every run.

This reads BOTH sides from the repository rather than from a database:
the columns from the migration that creates the table, the references
from the SQL text itself. No session is opened, so the prod-DB guard
in conftest is not involved and cannot be worked around.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
STORE = REPO / "astra" / "broker" / "store.py"
MIGRATION = (REPO / "astra" / "db" / "migrations" / "versions"
             / "x2q58r3o9m1m_capability_broker.py")


def _table_columns(table: str) -> set[str]:
    src = MIGRATION.read_text()
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n        \)",
                  src, re.S)
    assert m, f"no CREATE TABLE for {table} in {MIGRATION.name}"
    cols = set()
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("CONSTRAINT") or line.startswith("--"):
            continue
        name = line.split()[0]
        if name.isidentifier():
            cols.add(name.lower())
    assert cols, f"parsed no columns for {table}"
    return cols


def _sql_blocks() -> list[str]:
    src = STORE.read_text()
    out = []
    for m in re.finditer(r'text\(\s*(?:f?"""(.*?)"""|f?"([^"]*)")', src, re.S):
        out.append(m.group(1) or m.group(2) or "")
    assert out, "expected raw SQL in store.py"
    return out


def test_every_jsonb_access_names_a_real_column():
    """`args ->> 'target'` is the bug that shipped. A JSON operator is
    applied to a column name and nothing else, so this position can be
    checked exactly — no word list, no guessing."""
    known = (_table_columns("intents") | _table_columns("bodies")
             | _table_columns("intent_events"))
    bad: list[str] = []
    for sql in _sql_blocks():
        for m in re.finditer(r"\b(?:\w+\.)?([a-z_][a-z0-9_]*)\s*->>?\s*'", sql):
            col = m.group(1).lower()
            if col not in known:
                bad.append(f"{col} in: {sql.strip()[:70]}")
    assert not bad, (
        "these are used as JSON columns but no table defines them "
        f"(this is exactly how `args` vs `args_raw` shipped): {bad}"
    )


def test_every_insert_into_intents_names_real_columns():
    """The INSERT column list is the other unambiguous position."""
    cols = _table_columns("intents")
    checked = 0
    for sql in _sql_blocks():
        m = re.search(r"INSERT\s+INTO\s+intents\s*\((.*?)\)", sql, re.S | re.I)
        if not m:
            continue
        checked += 1
        named = [c.strip().lower() for c in m.group(1).split(",") if c.strip()]
        unknown = [c for c in named if c.isidentifier() and c not in cols]
        assert not unknown, f"INSERT INTO intents names non-columns: {unknown}"
    assert checked, "expected at least one INSERT INTO intents to check"


def test_the_probe_history_query_uses_the_real_args_column():
    src = STORE.read_text()
    assert "args_raw ->> 'target'" in src
    assert re.search(r"\bargs\s*->>", src) is None, (
        "`args ->> ...` names a column that does not exist; it is `args_raw`"
    )
