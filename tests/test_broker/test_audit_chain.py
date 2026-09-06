"""A7 acceptance: the cloud verifier catches what the Mac cannot.

Every test here is an ATTACK, run against the same in-memory bucket a
real one would produce, and each one asserts a DISTINCT, specific
report naming the seq range. That last part is the requirement that
matters: a verifier which answers "the chain is broken" to a deletion,
an edit, a fork and a foreign key has told the reader nothing they can
act on, and after the third false-sounding alarm nobody reads it.

Nothing here touches Postgres, R2, or the network. The chain is built
from a fixed seed, signed with `cryptography`, and handed to the
verifier as parsed objects — which is exactly the shape
`audit_anchor.Anchor` produces.
"""

from __future__ import annotations

import json
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from astra.broker import audit_anchor as A
from astra.broker import audit_chain as C

SEED = bytes(range(32))
OTHER_SEED = bytes(range(100, 132))


def _key(seed: bytes = SEED) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(seed)


def _chain_id(seed: bytes = SEED) -> str:
    return _key(seed).public_key().public_bytes_raw().hex()


def make_record(seq: int, prev: str, *, reason: str = "auto", ts: int = 1_757_000_000_000):
    r = {
        "seq": seq,
        "ts_ms": ts + seq,
        "decision": "denied" if seq % 3 == 0 else "succeeded",
        "verb": "fs.read",
        "intent_id": str(seq),
        "args_sha256": "1" * 64,
        "display_sha256": "2" * 64,
        "receipt_sha256": "",
        "reason": reason,
        "prev_hash": prev,
    }
    r["record_hash"] = C.record_hash(r)
    return r


def wrap(rec, chain_id: str, seed: bytes = SEED, shipped_ms: int = 1_757_000_100_000):
    sig = _key(seed).sign(C.record_signing_bytes(rec["record_hash"])).hex()
    return {"chain_id": chain_id, "record": rec, "shipped_ms": shipped_ms, "sig": sig}


def build_chain(n: int = 6, first_seq: int = 1, first_prev: str = C.GENESIS, seed: bytes = SEED):
    """`n` linked records starting at `first_seq`, plus their wrappers."""
    cid = _chain_id(seed)
    prev = first_prev
    recs = []
    for i in range(n):
        rec = make_record(first_seq + i, prev)
        recs.append(rec)
        prev = rec["record_hash"]
    return cid, recs, [wrap(r, cid, seed) for r in recs]


def make_manifest(
    chain_id: str,
    first_seq: int = 1,
    first_prev: str = C.GENESIS,
    created_ms: int = 1_757_000_000_000,
    seed: bytes = SEED,
    sign: bool = True,
):
    obj = {
        "chain_id": chain_id,
        "audit_pubkey": chain_id,
        "body": "kunal-mbp",
        "broker_cdhash": "ab" * 20,
        "created_ms": created_ms,
        "first_seq": first_seq,
        "first_prev_hash": first_prev,
    }
    if sign:
        obj["sig"] = (
            _key(seed)
            .sign(C.chain_signing_bytes(chain_id, first_seq, first_prev, created_ms))
            .hex()
        )
    return obj


def make_skip(chain_id: str, seq: int, why: str = "audit_reason_not_closed",
              ms: int = 1_757_000_500_000, seed: bytes = SEED, sign: bool = True):
    obj = {"chain_id": chain_id, "seq": seq, "why": why, "skipped_ms": ms}
    if sign:
        obj["sig"] = _key(seed).sign(C.skip_signing_bytes(chain_id, seq, why, ms)).hex()
    else:
        obj["sig"] = "00" * 64
    return obj


def verified_manifest(chain_id: str, **kw):
    m, v = C.verify_manifest(make_manifest(chain_id, **kw), chain_id)
    assert m is not None, v.detail
    return m


# ── the cross-language contract ──────────────────────────────────────


def test_this_module_reproduces_the_shared_anchor_vectors():
    """The one file three implementations are held to.

    astra-broker/Resources/audit_anchor_vectors.json is generated from
    these constructions, asserted against the Swift shipper by
    AuditAnchorVectorTests, and asserted against the verifier here. If
    someone changes a signing construction on either side, two suites
    go red on the same file rather than the bucket filling with
    objects nobody can verify.
    """
    v = C.load_anchor_vectors()
    chain_id = v["chain_id"]
    builders = {
        "record": lambda c: C.record_signing_bytes(c["record_hash"]),
        "head": lambda c: C.head_signing_bytes(c["seq"], c["record_hash"], c["ts_ms"]),
        "chain": lambda c: C.chain_signing_bytes(
            chain_id, c["first_seq"], c["first_prev_hash"], c["created_ms"]
        ),
        "skip": lambda c: C.skip_signing_bytes(
            chain_id, c["seq"], c["why"], c["skipped_ms"]
        ),
    }
    assert v["cases"], "the vector file holds no cases"
    for case in v["cases"]:
        msg = builders[case["kind"]](case)
        assert msg.hex() == case["signing_bytes_hex"], (
            f"{case['kind']}: this verifier builds different signing bytes than the "
            "file the Swift shipper is held to"
        )
        assert C._verifies(C.public_key(chain_id), case["sig"], msg)


def test_the_vector_file_exists_exactly_once():
    assert C.ANCHOR_VECTORS_PATH.exists(), C.ANCHOR_VECTORS_PATH
    root = C.ANCHOR_VECTORS_PATH.parents[2]
    found = list(root.rglob("audit_anchor_vectors.json"))
    assert len(found) == 1, f"more than one copy of the contract: {found}"


# ── the four attacks, four distinct reports ──────────────────────────


def test_an_intact_chain_reads_as_intact():
    cid, _recs, objs = build_chain(6)
    rep = C.verify_chain(objs, cid, manifest=verified_manifest(cid))
    assert rep.ok, rep.verdict.detail
    assert rep.records == 6
    assert rep.head_seq == 6
    assert "intact across 6 records" in str(rep)


def test_deleting_a_record_is_a_gap_naming_both_sides():
    cid, _recs, objs = build_chain(6)
    del objs[3]  # seq 4
    rep = C.verify_chain(objs, cid, manifest=verified_manifest(cid))
    assert rep.verdict.kind == "gap"
    assert rep.verdict.seq == 3 and rep.verdict.next_seq == 5
    assert "seq 4 is missing" in rep.verdict.detail


def test_editing_a_field_is_tampered_naming_the_seq():
    cid, recs, objs = build_chain(6)
    objs[2]["record"]["reason"] = "held"  # seq 3, hash left alone
    rep = C.verify_chain(objs, cid, manifest=verified_manifest(cid))
    assert rep.verdict.kind == "tampered"
    assert rep.verdict.seq == 3
    assert recs[2]["record_hash"] in rep.verdict.detail


def test_editing_and_rehashing_moves_the_failure_to_the_next_link():
    cid, _recs, objs = build_chain(6)
    edited = objs[2]["record"]
    edited["reason"] = "held"
    edited["record_hash"] = C.record_hash(edited)
    objs[2] = wrap(edited, cid)  # re-signed too: the attacker holds the key
    rep = C.verify_chain(objs, cid, manifest=verified_manifest(cid))
    assert rep.verdict.kind == "broken_link"
    assert rep.verdict.seq == 4, rep.verdict.detail
    assert "forks here" in rep.verdict.detail


def test_a_tail_resigned_under_another_key_is_sig_invalid_at_the_fork():
    cid, recs, objs = build_chain(6)
    # Seqs 4..6 re-signed with a key that is not the broker's. The
    # records themselves still hash and link correctly — only the
    # signature betrays the substitution.
    for i in (3, 4, 5):
        objs[i] = wrap(recs[i], cid, seed=OTHER_SEED)
    rep = C.verify_chain(objs, cid, manifest=verified_manifest(cid))
    assert rep.verdict.kind == "sig_invalid"
    assert rep.verdict.seq == 4


def test_a_chain_under_a_foreign_id_is_rejected_by_the_pin():
    other = _chain_id(OTHER_SEED)
    _cid, _recs, objs = build_chain(4, seed=OTHER_SEED)
    rep = C.verify_chain(objs, _chain_id(), manifest=None)
    assert rep.verdict.kind == "foreign_object"
    assert other[:16] in rep.verdict.detail


def test_the_four_reports_are_distinct_strings():
    cid, recs, base = build_chain(6)
    man = verified_manifest(cid)

    a = list(base)
    del a[3]
    b = [dict(o) for o in base]
    b[2] = {**b[2], "record": {**b[2]["record"], "reason": "held"}}
    c = [dict(o) for o in base]
    ed = {**c[2]["record"], "reason": "held"}
    ed["record_hash"] = C.record_hash(ed)
    c[2] = wrap(ed, cid)
    d = [dict(o) for o in base]
    for i in (3, 4, 5):
        d[i] = wrap(recs[i], cid, seed=OTHER_SEED)

    reports = [str(C.verify_chain(x, cid, manifest=man).verdict) for x in (a, b, c, d)]
    assert len(set(reports)) == 4, reports


# ── the floor: chain.json is checked before it is believed ───────────


def test_an_unsigned_manifest_is_refused_before_first_seq_is_read():
    cid = _chain_id()
    m, v = C.verify_manifest(make_manifest(cid, first_seq=1_000_000, sign=False), cid)
    assert m is None
    assert v.kind == "manifest_unsigned"
    assert "1000000" not in v.detail  # the claim is not repeated as if it were a fact


def test_a_manifest_signed_by_another_key_does_not_move_the_floor():
    cid = _chain_id()
    forged = make_manifest(cid, first_seq=1_000_000, first_prev="ab" * 32, seed=OTHER_SEED)
    m, v = C.verify_manifest(forged, cid)
    assert m is None and v.kind == "manifest_sig_invalid"
    assert "1000000" in v.detail and "HOLE" in v.detail


def test_a_forged_floor_cannot_hide_the_holes_below_it():
    """The attack the signature exists to stop.

    A writer-token holder puts a manifest claiming the chain starts at
    seq 4 and deletes seqs 1..3. With the signature checked, the
    manifest is not a manifest, and the verifier falls back to "the
    lowest record is seq 4 and nothing signed says that is by design".
    """
    cid, _recs, objs = build_chain(6)
    objs = objs[3:]
    forged = make_manifest(cid, first_seq=4, first_prev=objs[0]["record"]["prev_hash"],
                           seed=OTHER_SEED)
    m, v = C.verify_manifest(forged, cid)
    assert m is None
    rep = C.verify_chain(objs, cid, manifest=m)
    assert rep.verdict.kind == "no_floor"
    assert "1..3" in rep.verdict.detail


def test_a_signed_mid_log_floor_is_accepted_and_said_out_loud():
    """Enrolment starts mid-log BY DESIGN and the report must say so.

    The pre-A7 records carry brain-chosen text and must never reach the
    bucket, so the anchor begins at the head at pin time. A verifier
    that called that tampering would cry wolf on its very first run.
    """
    cid, recs, objs = build_chain(4, first_seq=14, first_prev="3f" * 32)
    man = verified_manifest(cid, first_seq=14, first_prev="3f" * 32)
    rep = C.verify_chain(objs, cid, manifest=man)
    assert rep.ok, rep.verdict.detail
    assert any("mid-log at seq 14" in n for n in rep.notes)


def test_a_manifest_for_another_chain_is_refused_by_the_pin():
    cid, _recs, _objs = build_chain(2)
    other = _chain_id(OTHER_SEED)
    m, v = C.verify_manifest(make_manifest(other, seed=OTHER_SEED), cid)
    assert m is None and v.kind == "manifest_foreign"


def test_the_floor_must_match_the_lowest_record():
    cid, _recs, objs = build_chain(6, first_seq=1)
    man = verified_manifest(cid, first_seq=3, first_prev="7c" * 32)
    rep = C.verify_chain(objs, cid, manifest=man)
    assert rep.verdict.kind == "floor_mismatch"


# ── a gap is tampering unless a signed skip says otherwise ───────────


def test_a_gap_with_a_signed_skip_is_a_decision_not_a_hole():
    cid, _recs, objs = build_chain(6)
    del objs[3]  # seq 4
    skips, problems = C.verify_skips({4: make_skip(cid, 4)}, cid)
    assert not problems
    rep = C.verify_chain(objs, cid, manifest=verified_manifest(cid), skips=skips)
    assert rep.ok, rep.verdict.detail
    assert rep.records == 5
    assert any("seq 4 absent by a signed" in n for n in rep.notes)


def test_an_unsigned_skip_leaves_the_gap_a_gap():
    cid, _recs, objs = build_chain(6)
    del objs[3]
    skips, problems = C.verify_skips({4: make_skip(cid, 4, sign=False)}, cid)
    assert skips == {}
    assert problems and problems[0].kind == "skip_sig_invalid"
    rep = C.verify_chain(objs, cid, manifest=verified_manifest(cid), skips=skips)
    assert rep.verdict.kind == "gap"


def test_a_skip_signed_by_another_key_is_not_a_decision():
    cid, _recs, objs = build_chain(6)
    del objs[3]
    skips, problems = C.verify_skips({4: make_skip(cid, 4, seed=OTHER_SEED)}, cid)
    assert skips == {} and problems[0].kind == "skip_sig_invalid"


def test_a_skip_whose_key_and_body_disagree_is_refused():
    """One signed object must not excuse an arbitrary seq.

    Without this, an attacker copies the legitimate skips/000000000004
    object to skips/000000000009 and the second gap reads as a
    decision — the signature is valid, it just isn't about seq 9.
    """
    cid, _recs, _objs = build_chain(6)
    skips, problems = C.verify_skips({9: make_skip(cid, 4)}, cid)
    assert skips == {} and problems[0].kind == "skip_mismatched"


def test_a_skip_for_a_present_record_is_a_contradiction():
    cid, _recs, objs = build_chain(6)
    skips, _ = C.verify_skips({4: make_skip(cid, 4)}, cid)
    rep = C.verify_chain(objs, cid, manifest=verified_manifest(cid), skips=skips)
    assert rep.verdict.kind == "skip_contradicted"
    assert rep.verdict.seq == 4


def test_the_link_hole_a_skip_opens_is_exactly_one_seq_wide():
    """A skip excuses ITS seq and nothing else.

    After the skipped record the walk cannot recompute the link (the
    hash it would need is not in the bucket), so it resumes at the next
    record — and a second, unexcused deletion further along must still
    be caught.
    """
    cid, _recs, objs = build_chain(8)
    del objs[6]  # seq 7, no skip object
    del objs[3]  # seq 4, excused
    skips, _ = C.verify_skips({4: make_skip(cid, 4)}, cid)
    rep = C.verify_chain(objs, cid, manifest=verified_manifest(cid), skips=skips)
    assert rep.verdict.kind == "gap"
    assert "seq 7 is missing" in rep.verdict.detail


# ── heads ────────────────────────────────────────────────────────────


def test_a_head_above_the_newest_record_proves_records_were_removed():
    cid, recs, objs = build_chain(6)
    head_rec = recs[-1]
    objs = objs[:-2]  # seqs 5 and 6 removed after they were anchored
    rep = C.verify_chain(objs, cid, manifest=verified_manifest(cid))
    head = {
        "seq": head_rec["seq"],
        "record_hash": head_rec["record_hash"],
        "ts_ms": head_rec["ts_ms"],
        "sig": _key()
        .sign(
            C.head_signing_bytes(head_rec["seq"], head_rec["record_hash"], head_rec["ts_ms"])
        )
        .hex(),
    }
    v = C.verify_head(head, cid, rep)
    assert v.kind == "head_ahead"
    assert "5..6" in v.detail


# ── the low-trust mirror ─────────────────────────────────────────────


def test_cross_check_reports_the_pre_anchor_seqs_separately():
    cid, recs, _objs = build_chain(4, first_seq=14, first_prev="3f" * 32)
    pg = [{"seq": n, "record_hash": "de" * 32} for n in range(1, 14)] + [
        {"seq": r["seq"], "record_hash": r["record_hash"]} for r in recs
    ]
    x = C.cross_check(recs, pg, first_seq=14)
    assert x.ok
    assert x.below_floor == list(range(1, 14))
    assert "13 pre-anchor seqs" in str(x)


def test_cross_check_names_a_deleted_mirror_row_and_an_edited_hash():
    cid, recs, _objs = build_chain(5)
    pg = {r["seq"]: {"seq": r["seq"], "record_hash": r["record_hash"]} for r in recs}
    del pg[3]                              # a mirror row deleted
    pg[4]["record_hash"] = "ff" * 32       # a mirror row edited
    x = C.cross_check(recs, pg.values())
    assert x.missing_in_pg == [3]
    assert x.hash_mismatch == [4]
    assert not x.ok


# ── a verdict is never truthy by accident ────────────────────────────


def test_a_verdict_refuses_to_be_used_as_a_boolean():
    with pytest.raises(TypeError):
        bool(C.Verdict("gap", "seq 4 is missing"))


# ── the reader, over an in-memory bucket ─────────────────────────────


class FakeS3:
    """Enough of the S3 API to drive Anchor, with forced pagination.

    Pagination is not incidental: the listing is the only thing that
    decides how long the chain is, so a verifier that stops at the
    first page reports a truncated chain as a complete one. Page size 2
    guarantees the loop runs.
    """

    def __init__(self, objects: dict[str, bytes], page: int = 2):
        self.objects = dict(objects)
        self.page = page
        self.deletes: list[str] = []
        self.delete_raises: Exception | None = None

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):  # noqa: N803
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        start = 0 if ContinuationToken is None else int(ContinuationToken)
        chunk = keys[start : start + self.page]
        nxt = start + self.page
        out = {"Contents": [{"Key": k} for k in chunk]}
        if nxt < len(keys):
            out["IsTruncated"] = True
            out["NextContinuationToken"] = str(nxt)
        else:
            out["IsTruncated"] = False
        return out

    def get_object(self, Bucket, Key):  # noqa: N803
        if Key not in self.objects:
            e = Exception("NoSuchKey")
            e.response = {"Error": {"Code": "NoSuchKey"}}
            raise e
        import io

        return {"Body": io.BytesIO(self.objects[Key])}

    def delete_object(self, Bucket, Key):  # noqa: N803
        if self.delete_raises:
            raise self.delete_raises
        self.deletes.append(Key)
        self.objects.pop(Key, None)
        return {}


def bucket_for(cid, objs, manifest=None, skips=None, heads=None) -> FakeS3:
    store: dict[str, bytes] = {}
    for o in objs:
        seq = o["record"]["seq"]
        store[f"{A.records_prefix(cid)}{seq:012d}.json"] = json.dumps(o).encode()
    if manifest is not None:
        store[A.chain_key(cid)] = json.dumps(manifest).encode()
    for n, s in (skips or {}).items():
        store[f"{A.skips_prefix(cid)}{n:012d}.json"] = json.dumps(s).encode()
    for stamp, h in (heads or {}).items():
        store[f"{A.heads_prefix(cid)}{stamp}.json"] = json.dumps(h).encode()
    return FakeS3(store)


def anchor_for(cid, s3) -> A.Anchor:
    return A.Anchor(
        A.Config(
            endpoint="https://example.invalid",
            bucket="astra-audit",
            key_id="k",
            secret="s",
            chain_id=cid,
        ),
        s3,
    )


def head_object(rec, seed: bytes = SEED):
    return {
        "seq": rec["seq"],
        "record_hash": rec["record_hash"],
        "ts_ms": rec["ts_ms"],
        "sig": _key(seed)
        .sign(C.head_signing_bytes(rec["seq"], rec["record_hash"], rec["ts_ms"]))
        .hex(),
    }


def test_the_reader_walks_a_paginated_chain_and_passes():
    cid, recs, objs = build_chain(7)
    s3 = bucket_for(
        cid, objs, manifest=make_manifest(cid),
        heads={"2026-09-05T10-15-00Z": head_object(recs[-1])},
    )
    r = A.verify(anchor_for(cid, s3))
    assert r.ok, list(r.lines())
    assert r.records == 7 and r.head_seq == 7


def test_the_reader_refuses_a_truncated_listing_with_no_token():
    cid, _recs, objs = build_chain(4)
    s3 = bucket_for(cid, objs, manifest=make_manifest(cid))

    def broken(Bucket, Prefix, ContinuationToken=None):  # noqa: N803
        return {"Contents": [], "IsTruncated": True}

    s3.list_objects_v2 = broken
    with pytest.raises(A.AnchorError):
        anchor_for(cid, s3).list_keys("chains/")


def test_an_intact_chain_with_no_manifest_is_not_a_pass():
    cid, recs, objs = build_chain(5)
    s3 = bucket_for(cid, objs, heads={"2026-09-05T10-15-00Z": head_object(recs[-1])})
    r = A.verify(anchor_for(cid, s3))
    assert not r.ok
    assert r.verdict == "manifest_missing"


def test_the_reader_reports_a_gap_the_mac_would_also_see():
    cid, recs, objs = build_chain(6)
    del objs[2]
    s3 = bucket_for(cid, objs, manifest=make_manifest(cid),
                    heads={"2026-09-05T10-15-00Z": head_object(recs[-1])})
    r = A.verify(anchor_for(cid, s3))
    assert not r.ok and r.verdict == "gap"
    assert "seq 3 is missing" in r.detail


def test_a_stray_object_under_records_is_named_not_parsed():
    cid, recs, objs = build_chain(3)
    s3 = bucket_for(cid, objs, manifest=make_manifest(cid),
                    heads={"2026-09-05T10-15-00Z": head_object(recs[-1])})
    s3.objects[f"{A.records_prefix(cid)}000000000002-old.json"] = b"{}"
    r = A.verify(anchor_for(cid, s3))
    assert r.ok, list(r.lines())
    assert any("do not name a seq" in n for n in r.notes)


def test_seq_of_key_is_strict_about_the_name():
    p = A.records_prefix("ab" * 32)
    assert A.seq_of_key(p + "000000000041.json", p) == 41
    for bad in ("41.json", "000000000041.txt", "000000000041-old.json", "00000000004１.json"):
        assert A.seq_of_key(p + bad, p) is None


def test_the_delete_probe_passes_only_when_the_delete_is_refused():
    cid, _recs, objs = build_chain(3)
    s3 = bucket_for(cid, objs, manifest=make_manifest(cid))
    s3.delete_raises = PermissionError("AccessDenied")
    assert A.delete_probe(anchor_for(cid, s3)).ok

    s3b = bucket_for(cid, objs, manifest=make_manifest(cid))
    r = A.delete_probe(anchor_for(cid, s3b))
    assert not r.ok and r.verdict == "DELETE_SUCCEEDED"
    assert s3b.deletes == [f"{A.records_prefix(cid)}000000000001.json"]


def test_freshness_is_age_only_and_never_pages_on_a_closed_lid():
    import calendar

    cid, recs, objs = build_chain(2)
    s3 = bucket_for(cid, objs, manifest=make_manifest(cid),
                    heads={"2026-09-05T10-15-00Z": head_object(recs[-1])})
    a = anchor_for(cid, s3)
    head_ms = calendar.timegm(time.strptime("2026-09-05T10-15-00Z", "%Y-%m-%dT%H-%M-%SZ")) * 1000
    assert A.freshness(a, now_ms=head_ms + 5 * 60_000).ok
    stale = A.freshness(a, now_ms=head_ms + 26 * 3_600_000)
    assert not stale.ok and stale.verdict == "stale"
    # A head stamped in the future can never expire, so it must not
    # read as fresh: a skewed clock would otherwise vouch for a chain
    # that stopped shipping days ago.
    ahead = A.freshness(a, now_ms=head_ms - 5 * 3_600_000)
    assert not ahead.ok and ahead.verdict == "head_in_future"


def test_the_config_refuses_the_delete_capable_r2_names():
    with pytest.raises(A.AnchorError) as e:
        A.Config.from_env(
            {
                "R2_ENDPOINT": "https://example.invalid",
                "R2_ACCESS_KEY_ID": "k",
                "R2_SECRET_ACCESS_KEY": "s",
            }
        )
    assert "AUDIT_R2_ENDPOINT" in str(e.value)
    assert "AUDIT_CHAIN_ID" in str(e.value)


def test_the_config_strips_a_dashboard_pasted_newline():
    cfg = A.Config.from_env(
        {
            "AUDIT_R2_ENDPOINT": "https://example.invalid\n",
            "AUDIT_R2_READ_KEY_ID": " k ",
            "AUDIT_R2_READ_SECRET": "s\n",
            "AUDIT_CHAIN_ID": _chain_id().upper() + "\n",
        }
    )
    assert cfg.endpoint == "https://example.invalid"
    assert cfg.key_id == "k"
    assert cfg.chain_id == _chain_id()
