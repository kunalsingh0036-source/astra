"""Exercise every SQL string in astra/broker/store.py against production,
then roll back. Calls the REAL functions — a transcription of the SQL
would not catch an unbound parameter, which is the bug that 500'd the
bridge result path this morning.
"""
import asyncio, os, sys, pathlib, re

raw = pathlib.Path.home().joinpath(".config/astra-backup.env").read_text()
os.environ["DATABASE_URL"] = re.findall(r"postgres(?:ql)?://[^\s'\"]+", raw)[0]

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
import astra.db.engine as eng

URL = os.environ["DATABASE_URL"].replace("postgresql://", "postgresql+asyncpg://")
engine = create_async_engine(URL, poolclass=None)

ok = True
def check(label, cond, extra=""):
    global ok
    print(f"   {'ok ' if cond else '!! '}{label:52} {extra}")
    if not cond: ok = False

async def main():
    global ok
    conn = await engine.connect()
    outer = await conn.begin()          # everything inside, rolled back

    # Point the module's session factory at THIS connection so every
    # store call joins the same transaction and is undone together.
    eng.async_session = async_sessionmaker(
        bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
    import importlib
    from astra.broker import store
    importlib.reload(store)
    store.async_session = eng.async_session

    try:
        bid, tok = await store.mint_body_token("dryrun-body")
        check("mint_body_token", isinstance(bid, int) and len(tok) > 20, f"id={bid}")

        b = await store.validate_body_token(tok)
        check("validate_body_token (valid)", b is not None and b.id == bid)
        check("validate_body_token (garbage) -> None",
              (await store.validate_body_token("nope")) is None)
        check("validate_body_token (empty) -> None",
              (await store.validate_body_token("")) is None)

        await store.touch_body_poll(bid)
        check("touch_body_poll", True)

        iid = await store.submit_intent(
            body_id=bid, verb="fs.read", args={"path": "/private/tmp"},
            why="dry run", ttl_seconds=120, session_claim="sess-1")
        check("submit_intent", isinstance(iid, int), f"id={iid}")

        try:
            await store.submit_intent(
                body_id=bid, verb="fs.read", args={"p": "a\x00b"},
                why="nul", ttl_seconds=60)
            check("NUL in args refused BEFORE the insert", False, "not refused")
        except store.ArgsRejected as e:
            check("NUL in args refused BEFORE the insert", True,
                  str(e)[:40].replace("\n", " "))

        check("pending_depth", (await store.pending_depth(bid)) == 1)

        it = await store.claim_next_intent(bid)
        check("claim_next_intent", it is not None and it.id == iid,
              f"verb={it.verb if it else None} args={it.args if it else None}")
        check("claim again -> None (SKIP LOCKED, none pending)",
              (await store.claim_next_intent(bid)) is None)

        # THE ownership predicate.
        bid2, _ = await store.mint_body_token("other-body")
        wrong = await store.record_status(
            iid, body_id=bid2, status="succeeded", result_note="forged")
        check("record_status from the WRONG body -> False", wrong is False)

        right = await store.record_status(
            iid, body_id=bid, status="succeeded",
            receipt_bytes=b"\x00"*194, result_bytes=b"hello",
            display_bytes=b"Astra wants to: fs.read\n", result_note="fine")
        check("record_status from the RIGHT body -> True", right is True)

        replay = await store.record_status(
            iid, body_id=bid, status="failed", result_note="rewrite")
        check("replay onto a TERMINAL row -> False", replay is False)

        st = await store.get_intent_status(iid)
        check("get_intent_status returns RAW bytes",
              st is not None and st["receipt_bytes"] == b"\x00"*194
              and st["result_bytes"] == b"hello", f"status={st['status']}")
        check("no receipt_verified field is invented",
              "receipt_verified" not in (st or {}))

        a1 = await store.append_audit(
            body_id=bid, seq=1, ts=st["created_at"], decision="decided",
            prev_hash="0"*64, record_hash="a"*64, intent_id=iid, verb="fs.read")
        check("append_audit (new seq) -> True", a1 is True)
        a2 = await store.append_audit(
            body_id=bid, seq=1, ts=st["created_at"], decision="tampered",
            prev_hash="0"*64, record_hash="b"*64)
        check("append_audit replay of seq 1 -> False (not overwritten)", a2 is False)
        kept = (await conn.execute(
            __import__("sqlalchemy").text(
                "SELECT record_hash FROM intent_events WHERE body_id=:b AND seq=1"),
            {"b": bid})).scalar_one()
        check("original record_hash preserved", kept == "a"*64, kept[:8])

        iid2 = await store.submit_intent(
            body_id=bid, verb="fs.glob", args={"pattern": "/private/tmp/*"},
            why="reaper", ttl_seconds=-5)          # already expired
        check("claim skips an EXPIRED intent",
              (await store.claim_next_intent(bid)) is None)
        n = await store.expire_stale_intents()
        check("expire_stale_intents reaps it", n >= 1, f"reaped={n}")
        st2 = await store.get_intent_status(iid2)
        check("reaped intent is 'expired' with a reason",
              st2["status"] == "expired" and st2["deny_reason"], st2["status"])
    except Exception as e:
        import traceback; traceback.print_exc()
        check(f"UNEXPECTED {type(e).__name__}", False, str(e)[:70])
    finally:
        await outer.rollback()
        await conn.close()

    # Residue check on a fresh connection.
    c2 = await engine.connect()
    from sqlalchemy import text as _t
    left = (await c2.execute(_t(
        "SELECT (SELECT count(*) FROM bodies), (SELECT count(*) FROM intents),"
        " (SELECT count(*) FROM intent_events)"))).first()
    await c2.close()
    print(f"   residue: bodies={left[0]} intents={left[1]} events={left[2]}")
    clean = tuple(left) == (0, 0, 0)
    print("VERDICT:", "PASS" if (ok and clean) else "FAIL")
    await engine.dispose()
    return 0 if (ok and clean) else 1

sys.exit(asyncio.run(main()))
