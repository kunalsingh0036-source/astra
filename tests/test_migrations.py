"""Alembic revision discipline, read from the files.

services/stream/main.py's /health/deep takes the ALPHABETICALLY LAST
file in astra/db/migrations/versions/ as the on-disk head and compares
it with alembic_version. A revision id that sorts before the current
head makes that check report `degraded` forever; a monitor that cries
wolf permanently is the class that already burned this repo twice. And
alembic runs online through asyncpg, which PREPARES statements and
refuses a string holding two commands: on 2026-08-30 one batched
`DROP TRIGGER; CREATE TRIGGER` took the scheduler down for 25 minutes
with Railway reporting SUCCESS (x2q58r3o9m1m's docstring).

These tests read the migration files with ast, never importing them,
and never touch a database.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_VERSIONS = pathlib.Path(__file__).resolve().parents[1] / "astra/db/migrations/versions"

# From this revision on, every op.execute() must carry ONE statement
# and every upgrade must open with SET lock_timeout. Older files
# predate the rule and are not rewritten (a migration that has run is
# history).
_RULES_FROM = "x2q58r3o9m1m"

_A6_RETIRE = "y3r69s4p0n2n"
_A6_PARENT = "x2q58r3o9m1m"


def _files() -> list[pathlib.Path]:
    return sorted(p for p in _VERSIONS.glob("*.py") if not p.name.startswith("__"))


def _module(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def _const(tree: ast.Module, name: str):
    for node in tree.body:
        target = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign):
            target = node.target
        if isinstance(target, ast.Name) and target.id == name:
            assert isinstance(node.value, ast.Constant), f"{name} is not a literal"
            return node.value.value
    raise AssertionError(f"{name} not found in {tree}")


def _revisions() -> dict[str, tuple[str, str | None]]:
    out = {}
    for p in _files():
        t = _module(p)
        out[p.name] = (_const(t, "revision"), _const(t, "down_revision"))
    return out


def _func(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"def {name} not found")


def _op_execute_strings(fn: ast.FunctionDef) -> list[str]:
    """The literal SQL of every op.execute(...) call, in source order.
    A non-literal argument is a failure: the rules below cannot be
    checked on a string built at run time."""
    out = []
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "op"):
            assert node.args and isinstance(node.args[0], ast.Constant), (
                f"op.execute at line {node.lineno} is not a string literal"
            )
            out.append(str(node.args[0].value))
    return out


# ── the chain ─────────────────────────────────────────────


def test_every_filename_starts_with_its_revision():
    for name, (rev, _) in _revisions().items():
        assert name.startswith(rev + "_"), (name, rev)


def test_single_head_and_it_is_the_alphabetically_last_file():
    """The /health/deep contract: the last file by sort order IS the
    alembic head. A new revision whose id sorts earlier passes alembic
    and breaks the monitor."""
    revs = _revisions()
    ids = {rev for rev, _ in revs.values()}
    downs = {down for _, down in revs.values() if down}
    heads = ids - downs
    assert len(heads) == 1, f"expected one head, found {sorted(heads)}"
    last_file = _files()[-1].name
    assert revs[last_file][0] in heads, (
        f"the alphabetically last file {last_file} is not the alembic head "
        f"{heads}; /health/deep would report degraded forever"
    )
    assert downs <= ids, f"down_revision names an unknown revision: {sorted(downs - ids)}"


def test_the_chain_is_linear():
    revs = _revisions()
    downs = [down for _, down in revs.values() if down]
    assert len(downs) == len(set(downs)), "two revisions share a parent (a branch)"


# ── the statement rules, from x2q58r3o9m1m on ─────────────


@pytest.mark.parametrize(
    "path", [p for p in _files() if p.name.split("_", 1)[0] >= _RULES_FROM],
    ids=lambda p: p.name,
)
def test_one_statement_per_execute_and_lock_timeout_first(path):
    tree = _module(path)
    stmts = _op_execute_strings(_func(tree, "upgrade"))
    assert stmts, f"{path.name}: upgrade() executes nothing"
    assert stmts[0].strip().upper().startswith("SET LOCK_TIMEOUT"), (
        f"{path.name}: the first statement must be SET lock_timeout, or a "
        "blocked migration hangs forever with no error anywhere"
    )
    for sql in stmts:
        body = sql.strip().rstrip(";")
        assert ";" not in body, (
            f"{path.name}: one op.execute() carries two commands; asyncpg "
            f"prepares statements and refuses this: {sql[:80]!r}"
        )
        assert "CONCURRENTLY" not in body.upper(), (
            f"{path.name}: CREATE INDEX CONCURRENTLY inside alembic's "
            "transaction raises 25001"
        )


# ── the A6 revision ───────────────────────────────────────


def test_retire_bridge_revision_sorts_after_the_broker_revision():
    revs = _revisions()
    by_id = {rev: (name, down) for name, (rev, down) in revs.items()}
    assert _A6_RETIRE in by_id, "y3r69s4p0n2n_retire_bridge.py is missing"
    name, down = by_id[_A6_RETIRE]
    assert down == _A6_PARENT
    assert _A6_RETIRE > _A6_PARENT, "the id must sort after its parent"
    assert name > by_id[_A6_PARENT][0], "the file must sort after its parent's file"


def test_retire_bridge_revokes_every_token_after_the_lock_timeout():
    path = next(p for p in _files() if p.name.startswith(_A6_RETIRE))
    tree = _module(path)
    stmts = [s.strip() for s in _op_execute_strings(_func(tree, "upgrade"))]
    assert stmts[0].upper().startswith("SET LOCK_TIMEOUT = '5S'"), stmts[0]
    updates = [s for s in stmts if s.upper().startswith("UPDATE BRIDGE_TOKENS")]
    assert len(updates) == 1, stmts
    sql = " ".join(updates[0].split()).lower()
    assert "set revoked_at = now()" in sql
    assert sql.endswith("where revoked_at is null"), (
        "revoke ALL live rows, idempotently; not a hand-picked id"
    )
    assert stmts.index(updates[0]) > 0, "the UPDATE must come after SET lock_timeout"
    # No DROP here: the tables stay one release as a record.
    assert not any("DROP" in s.upper() for s in stmts)


def test_retire_bridge_has_no_automated_downgrade():
    path = next(p for p in _files() if p.name.startswith(_A6_RETIRE))
    fn = _func(_module(path), "downgrade")
    assert any(isinstance(n, ast.Raise) for n in ast.walk(fn)), (
        "downgrade() must raise: un-revoking a leaked bearer is not a rollback"
    )
    assert _op_execute_strings(fn) == []
