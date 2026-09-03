"""Execute the A3 migration against production, then roll back.

This is the bar, adopted after two production breakages in one session:
a migration that batched two commands (scheduler down 25 min) and a
route whose `:tok::int` was a syntax error (every bridge result 500'd).
Both passed review. Neither had been executed.

Always rolls back in a finally. Asserts zero residue afterwards.
"""
import asyncio, re, pathlib, sys, importlib.util, types

raw = pathlib.Path.home().joinpath(".config/astra-backup.env").read_text()
URL = re.findall(r"postgres(?:ql)?://[^\s'\"]+", raw)[0]

# Load the migration and CAPTURE its statements rather than trusting a
# transcription of them — the file that ships is the file under test.
spec = importlib.util.spec_from_file_location(
    "mig", "astra/db/migrations/versions/x2q58r3o9m1m_capability_broker.py")
mig = importlib.util.module_from_spec(spec)
class _Op:
    def __init__(self): self.stmts = []
    def execute(self, s): self.stmts.append(str(s))
fake = types.ModuleType("alembic"); fake.op = _Op()
sys.modules["alembic"] = fake
sys.modules["mig"] = mig
spec.loader.exec_module(mig)
mig.upgrade()
STMTS = fake.op.stmts

async def main():
    import asyncpg
    c = await asyncpg.connect(URL)
    ver = await c.fetchval("SELECT version()")
    head = await c.fetchval("SELECT version_num FROM alembic_version")
    before = await c.fetchval(
        "SELECT count(*) FROM pg_tables WHERE schemaname='public'")
    print(f"server : {ver.split(' on ')[0]}")
    print(f"head   : {head}          tables : {before}")
    print(f"statements captured from the migration file: {len(STMTS)}")

    ok = True
    tr = c.transaction(); await tr.start()
    try:
        await c.execute("SET LOCAL statement_timeout = '15s'")
        for i, s in enumerate(STMTS, 1):
            await c.execute(s)
            first = " ".join(s.split())[:64]
            print(f"   {i}..OK  {first}")

        bid = await c.fetchval(
            "INSERT INTO bodies (label, token_hash) VALUES ($1,$2) RETURNING id",
            "dryrun", "0"*64)

        # The probes that matter: prove each CHECK FIRES.
        # EACH PROBE IN ITS OWN SAVEPOINT. asyncpg aborts the whole
        # transaction on the first constraint violation, so without
        # this every probe after the first reports
        # InFailedSQLTransactionError — a FALSE ok that tests nothing.
        # The first run of this script did exactly that. Same class as
        # a best-effort read poisoning its caller's transaction.
        async def refused(label, sql, *args):
            nonlocal ok
            sp = c.transaction()
            await sp.start()
            try:
                await c.execute(sql, *args)
                await sp.rollback()
                print(f"      !! NOT REFUSED: {label}"); ok = False
            except Exception as e:
                await sp.rollback()
                name = getattr(e, "constraint_name", None) or type(e).__name__
                if name == "InFailedSQLTransactionError":
                    print(f"      !! {label}: probe ran in a poisoned txn "
                          f"— proves nothing"); ok = False
                else:
                    print(f"      ok {label:38} : {name}")

        await refused("args_raw > 1 MiB refused",
            "INSERT INTO intents (body_id, verb, args_raw, expires_at) "
            "VALUES ($1,'fs.read',$2::jsonb, now()+interval '1 min')",
            bid, '{"x":"' + "A"*1_100_000 + '"}')
        await refused("status='authorized' refused",
            "INSERT INTO intents (body_id, verb, status, expires_at) "
            "VALUES ($1,'fs.read','authorized', now()+interval '1 min')", bid)
        await refused("receipt of wrong length refused",
            "INSERT INTO intents (body_id, verb, receipt_bytes, expires_at) "
            "VALUES ($1,'fs.read',$2, now()+interval '1 min')", bid, b"\x00"*100)
        await refused("token of wrong length refused",
            "INSERT INTO intents (body_id, verb, token_bytes, expires_at) "
            "VALUES ($1,'fs.read',$2, now()+interval '1 min')", bid, b"\x00"*266)
        await refused("display over 4 KiB refused",
            "INSERT INTO intents (body_id, verb, display_bytes, expires_at) "
            "VALUES ($1,'fs.read',$2, now()+interval '1 min')", bid, b"D"*5000)
        await refused("NUL escape in args_raw refused (not a 500)",
            "INSERT INTO intents (body_id, verb, args_raw, expires_at) "
            "VALUES ($1,'fs.read',$2::jsonb, now()+interval '1 min')",
            bid, '{"a":"\\u0000"}')
        # A legitimate seq=1 event, then a duplicate that must violate.
        await c.execute(
            "INSERT INTO intent_events (body_id,seq,ts,decision,prev_hash,record_hash)"
            " VALUES ($1,1,now(),'x','a','b')", bid)
        await refused("duplicate (body_id, seq) refused",
            "INSERT INTO intent_events (body_id,seq,ts,decision,prev_hash,record_hash)"
            " VALUES ($1,1,now(),'y','c','d')", bid)

        # A legitimate row must still insert.
        iid = await c.fetchval(
            "INSERT INTO intents (body_id, verb, args_raw, why, expires_at) "
            "VALUES ($1,'fs.read','{\"path\":\"/private/tmp\"}','because',"
            " now()+interval '2 min') RETURNING id", bid)
        print(f"      ok legitimate intent inserted       : id={iid}")

        # No FK on intent_events.intent_id: the chain must survive the
        # intent row being deleted.
        await c.execute(
            "INSERT INTO intent_events (body_id,seq,ts,intent_id,decision,"
            "prev_hash,record_hash) VALUES ($1,2,now(),$2,'decided','a','b')",
            bid, iid)
        await c.execute("DELETE FROM intents WHERE id=$1", iid)
        left = await c.fetchval(
            "SELECT count(*) FROM intent_events WHERE intent_id=$1", iid)
        if left == 1:
            print("      ok audit event SURVIVES its intent being deleted")
        else:
            print("      !! audit event vanished with the intent"); ok = False
    except Exception as e:
        print(f"   FAILED: {type(e).__name__}: {e}"); ok = False
    finally:
        await tr.rollback()

    after = await c.fetchval(
        "SELECT count(*) FROM pg_tables WHERE schemaname='public'")
    head2 = await c.fetchval("SELECT version_num FROM alembic_version")
    resid = await c.fetchval(
        "SELECT count(*) FROM pg_tables WHERE schemaname='public' "
        "AND tablename IN ('bodies','intents','intent_events')")
    print(f"residue: {resid} of 3   tables: {after} (was {before})   head: {head2}")
    ok = ok and after == before and resid == 0 and head2 == head
    print("VERDICT:", "PASS" if ok else "FAIL")
    await c.close()
    return 0 if ok else 1

sys.exit(asyncio.run(main()))
