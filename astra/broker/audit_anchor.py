"""Read the astra-audit bucket and hand it to the verifier.

THE ONLY THING THIS FILE IS ALLOWED TO DO IS READ. The credential it
uses is an Object **Read only** R2 token, bucket-scoped to
`astra-audit`, minted separately from the writer token that lives in
the Mac's uid-450 trust store. That asymmetry is the whole point of the
design: the party that writes the chain cannot verify it, and the party
that verifies it cannot alter it. There is no `put_object`, no
`delete_object` and no `copy_object` call anywhere below except inside
`delete_probe`, whose entire purpose is to prove that a delete FAILS.

WHY THE ENV NAMES ARE DIFFERENT FROM THE OTHER R2 CREDENTIAL. The
stream service already carries `R2_ENDPOINT` / `R2_ACCESS_KEY_ID` /
`R2_SECRET_ACCESS_KEY` / `R2_BUCKET` for the creators' render pipeline,
and that token is DELETE-CAPABLE. If the verifier read those names, a
copy-paste of the wrong value into the wrong service would hand the
auditor the power to erase what it audits, and nothing would break
loudly. So: `AUDIT_R2_*`, always, and the audit reader never falls back
to the generic names.

THE PIN. `AUDIT_CHAIN_ID` is the 64-hex Ed25519 public key printed by
`AstraBroker pin-audit-sink` and written into Kunal's phone runbook.
The bucket cannot supply it — a writer token can create any number of
chains under any number of ids, and the lock makes every one of them
permanent. Verifying "some chain in the bucket" proves nothing; this
module refuses to run without the pin.

CLI:

    python -m astra.broker.audit_anchor verify      # the full walk
    python -m astra.broker.audit_anchor freshness   # is it still shipping?
    python -m astra.broker.audit_anchor probe       # prove the token is read-only

Exit code is 0 only on a clean result. Everything else is non-zero, so
this is usable from a scheduler job, a shell, or a health check without
anyone parsing prose.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

from astra.broker.audit_chain import (
    ChainReport,
    Manifest,
    Verdict,
    verify_chain,
    verify_head,
    verify_manifest,
    verify_skips,
)

# ── keys, mirrored from Sources/AstraCore/AuditShipper.swift ─────────


def records_prefix(chain_id: str) -> str:
    return f"chains/{chain_id}/records/"


def skips_prefix(chain_id: str) -> str:
    return f"chains/{chain_id}/skips/"


def heads_prefix(chain_id: str) -> str:
    return f"chains/{chain_id}/heads/"


def chain_key(chain_id: str) -> str:
    return f"chains/{chain_id}/chain.json"


def seq_of_key(key: str, prefix: str) -> int | None:
    """The seq a `<prefix><12 digits>.json` key names, or None.

    Strict on purpose: 12 digits exactly, nothing else in the name. A
    lenient parser would let `records/000000000041-old.json` masquerade
    as seq 41 and quietly shadow the real object in a dict.
    """
    if not key.startswith(prefix) or not key.endswith(".json"):
        return None
    name = key[len(prefix) : -len(".json")]
    if len(name) != 12 or not name.isdigit() or not name.isascii():
        return None
    return int(name)


class AnchorError(RuntimeError):
    pass


# ── configuration (env, because this side is a container) ────────────


@dataclass(frozen=True)
class Config:
    endpoint: str
    bucket: str
    key_id: str
    secret: str
    chain_id: str

    @staticmethod
    def from_env(env: dict[str, str] | None = None) -> "Config":
        e = dict(os.environ if env is None else env)
        missing = [
            n
            for n in (
                "AUDIT_R2_ENDPOINT",
                "AUDIT_R2_READ_KEY_ID",
                "AUDIT_R2_READ_SECRET",
                "AUDIT_CHAIN_ID",
            )
            if not (e.get(n) or "").strip()
        ]
        if missing:
            raise AnchorError(
                "the audit anchor reader is not configured: "
                + ", ".join(missing)
                + ". These are deliberately NOT the creators' R2_* names — that "
                "token can delete. AUDIT_CHAIN_ID is the chain id printed by "
                "pin-audit-sink and kept in the phone runbook; without it this "
                "would verify whichever chain the bucket happens to hold."
            )
        return Config(
            # Trailing whitespace and a stray newline in a dashboard-set
            # env var have cost this project a day before (a Vercel URL
            # with a trailing \n). Strip, every time.
            endpoint=e["AUDIT_R2_ENDPOINT"].strip(),
            bucket=(e.get("AUDIT_R2_BUCKET") or "astra-audit").strip(),
            key_id=e["AUDIT_R2_READ_KEY_ID"].strip(),
            secret=e["AUDIT_R2_READ_SECRET"].strip(),
            chain_id=e["AUDIT_CHAIN_ID"].strip().lower(),
        )


def client(cfg: Config):  # pragma: no cover - exercised via Stubber in tests
    """A read-only S3 client for R2. Same shape as scripts/backup-postgres.py."""
    import boto3
    from botocore.config import Config as BotoConfig

    return boto3.client(
        "s3",
        endpoint_url=cfg.endpoint,
        aws_access_key_id=cfg.key_id,
        aws_secret_access_key=cfg.secret,
        config=BotoConfig(signature_version="s3v4"),
        region_name="auto",
    )


# ── the reader ───────────────────────────────────────────────────────


class Anchor:
    """Everything that touches the network, in one small class.

    Kept separate from `audit_chain` so the verification logic can be
    tested without a stub, and this can be tested with `botocore.stub`
    and never a socket.
    """

    def __init__(self, cfg: Config, s3: Any):
        self.cfg = cfg
        self.s3 = s3

    # -- primitives

    def list_keys(self, prefix: str) -> list[str]:
        """Every key under a prefix, paginated.

        Written as an explicit ContinuationToken loop rather than a
        paginator so a truncated listing cannot be mistaken for a short
        chain: `IsTruncated` is read on every page and the loop only
        ends when the bucket says it is done.
        """
        out: list[str] = []
        token: str | None = None
        while True:
            kw: dict[str, Any] = {"Bucket": self.cfg.bucket, "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            resp = self.s3.list_objects_v2(**kw)
            for item in resp.get("Contents", []) or []:
                out.append(item["Key"])
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
            if not token:
                raise AnchorError(
                    f"the bucket reported a truncated listing of {prefix} with no "
                    "continuation token; refusing to treat a partial listing as the "
                    "whole chain"
                )
        return out

    def get_json(self, key: str) -> Any:
        resp = self.s3.get_object(Bucket=self.cfg.bucket, Key=key)
        body = resp["Body"].read()
        try:
            return json.loads(body)
        except ValueError as e:
            raise AnchorError(f"{key} is not JSON: {e}") from None

    # -- the four object kinds

    def manifest(self) -> tuple[Manifest | None, Verdict]:
        """chain.json, signature-checked BEFORE its floor is read.

        A missing manifest is NOT an error here: a chain that has never
        shipped has none, and the caller distinguishes "no anchor yet"
        from "the anchor lies". What is never allowed is reading
        `first_seq` from an unverified file, which is why the only way
        to get a Manifest object is through verify_manifest.
        """
        try:
            obj = self.get_json(chain_key(self.cfg.chain_id))
        except Exception as e:  # noqa: BLE001 - NoSuchKey and friends are per-vendor
            if _is_not_found(e):
                return None, Verdict(
                    "manifest_missing",
                    f"the bucket holds no chain.json for chain {self.cfg.chain_id}. "
                    "Without a signed floor, every absent seq below the lowest record "
                    "is a hole.",
                )
            raise
        return verify_manifest(obj, self.cfg.chain_id)

    def records(self) -> list[dict[str, Any]]:
        prefix = records_prefix(self.cfg.chain_id)
        out: list[dict[str, Any]] = []
        for key in self.list_keys(prefix):
            if seq_of_key(key, prefix) is None:
                # Not a record key. Reported by verify() as a stray, not
                # parsed: a bucket this small should contain nothing else.
                continue
            out.append(self.get_json(key))
        return out

    def strays(self) -> list[str]:
        prefix = records_prefix(self.cfg.chain_id)
        return [k for k in self.list_keys(prefix) if seq_of_key(k, prefix) is None]

    def skips(self) -> dict[int, Any]:
        prefix = skips_prefix(self.cfg.chain_id)
        out: dict[int, Any] = {}
        for key in self.list_keys(prefix):
            n = seq_of_key(key, prefix)
            if n is None:
                continue
            out[n] = self.get_json(key)
        return out

    def head_keys(self) -> list[str]:
        return sorted(self.list_keys(heads_prefix(self.cfg.chain_id)))

    def newest_head(self) -> dict[str, Any] | None:
        keys = self.head_keys()
        if not keys:
            return None
        # The stamp is `YYYY-MM-DDTHH-MM-SSZ`, fixed width and UTC, so
        # lexical order IS chronological order.
        return self.get_json(keys[-1])


def _is_not_found(e: Exception) -> bool:
    code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
    return code in ("NoSuchKey", "404", "NotFound")


# ── the runs ─────────────────────────────────────────────────────────


@dataclass
class Result:
    verdict: str
    detail: str
    ok: bool
    records: int = 0
    first_seq: int | None = None
    head_seq: int | None = None
    head_hash: str = ""
    notes: list[str] = field(default_factory=list)

    def lines(self) -> Iterator[str]:
        yield f"{'ok  ' if self.ok else 'FAIL'} {self.verdict}: {self.detail}"
        for n in self.notes:
            yield f"     · {n}"


def verify(anchor: Anchor) -> Result:
    """The full walk: manifest, records, skips, head.

    Order matters and is the order of trust. The manifest first,
    because it is the only thing that can make an absence legitimate;
    the skips next, for the same reason at a finer grain; then the
    records, which are checked against both.
    """
    manifest, m_verdict = anchor.manifest()
    notes = [m_verdict.detail]

    raw_skips = anchor.skips()
    skips, skip_problems = verify_skips(raw_skips, anchor.cfg.chain_id)
    if skip_problems:
        # An unsigned or mismatched skip is a failure in itself: it is a
        # claim that a record's absence was a decision, made by someone
        # who could not sign it.
        p = skip_problems[0]
        return Result(
            verdict=p.kind,
            detail=p.detail,
            ok=False,
            notes=notes + [q.detail for q in skip_problems[1:]],
        )

    strays = anchor.strays()
    if strays:
        notes.append(
            f"{len(strays)} object(s) under records/ do not name a seq "
            f"(first: {strays[0]}); they are permanent and were not written by the "
            "shipper"
        )

    report: ChainReport = verify_chain(
        anchor.records(), anchor.cfg.chain_id, manifest=manifest, skips=skips
    )
    notes.extend(report.notes)

    if not report.ok:
        return Result(
            verdict=report.verdict.kind,
            detail=report.verdict.detail,
            ok=False,
            records=report.records,
            first_seq=report.first_seq,
            head_seq=report.head_seq,
            head_hash=report.head_hash,
            notes=notes,
        )

    # An intact chain under an UNVERIFIED manifest is not a pass. The
    # walk proved the records it can see are consistent; the manifest
    # is what says which records should be there at all.
    if manifest is None:
        return Result(
            verdict=m_verdict.kind,
            detail=m_verdict.detail,
            ok=False,
            records=report.records,
            first_seq=report.first_seq,
            head_seq=report.head_seq,
            head_hash=report.head_hash,
            notes=notes + [report.verdict.detail],
        )

    head = anchor.newest_head()
    if head is None:
        notes.append(
            "the bucket holds no heads/ object; the broker publishes one at boot, at "
            "halt and once a UTC day, so this chain has not booted since A7"
        )
    else:
        h = verify_head(head, anchor.cfg.chain_id, report)
        notes.append(h.detail)
        if h.kind not in ("head_ok",):
            return Result(
                verdict=h.kind,
                detail=h.detail,
                ok=False,
                records=report.records,
                first_seq=report.first_seq,
                head_seq=report.head_seq,
                head_hash=report.head_hash,
                notes=notes,
            )

    return Result(
        verdict="intact",
        detail=report.verdict.detail,
        ok=True,
        records=report.records,
        first_seq=report.first_seq,
        head_seq=report.head_seq,
        head_hash=report.head_hash,
        notes=notes,
    )


def freshness(anchor: Anchor, now_ms: int | None = None, max_age_h: float = 25.0) -> Result:
    """How old the newest head is, and nothing else.

    Deliberately does NOT page on lag by itself. The broker only runs
    while the Mac is awake, so "behind" is the normal state of a closed
    laptop and alerting on it would produce exactly the nightly
    "bridge is down" ping that is forbidden. The caller pairs this with
    `bodies.last_poll_at`: stale head AND a body that is polling is a
    real fault; stale head and a quiet body is a lid.
    """
    now = int(time.time() * 1000) if now_ms is None else now_ms
    keys = anchor.head_keys()
    if not keys:
        return Result(
            verdict="no_head",
            detail="no heads/ object at all: nothing has ever been anchored",
            ok=False,
        )
    stamp = keys[-1].rsplit("/", 1)[-1][: -len(".json")]
    try:
        t = time.strptime(stamp, "%Y-%m-%dT%H-%M-%SZ")
    except ValueError:
        return Result(
            verdict="head_key_malformed",
            detail=f"the newest heads/ key is not a UTC stamp: {stamp!r}",
            ok=False,
        )
    import calendar

    head_ms = int(calendar.timegm(t) * 1000)
    age_h = (now - head_ms) / 3_600_000
    if age_h < -0.25:
        # A head stamped in the future never goes stale, so an
        # unbounded "fresh" would make a dead chain look alive for as
        # long as the skew lasts. Clock drift on the Mac and a
        # deliberately post-dated key look identical from here; both
        # are worth saying out loud rather than sleeping through.
        return Result(
            verdict="head_in_future",
            detail=(
                f"the newest head is stamped {stamp}, {-age_h:.1f} h in the FUTURE. "
                "A future stamp can never expire, so freshness cannot vouch for this "
                "chain until the clocks agree."
            ),
            ok=False,
            notes=[f"{len(keys)} head objects"],
        )
    ok = age_h <= max_age_h
    return Result(
        verdict="fresh" if ok else "stale",
        detail=(
            f"newest head is {age_h:.1f} h old (stamp {stamp}, limit {max_age_h:g} h)"
        ),
        ok=ok,
        notes=[f"{len(keys)} head objects"],
    )


def delete_probe(anchor: Anchor) -> Result:
    """Prove the READER token cannot delete. Two failures are possible.

    A delete that SUCCEEDS is the worst outcome this codebase can
    report: it means the auditor holds a credential that can destroy
    the evidence, and under an indefinite lock the object it just
    removed is gone in a way even Cloudflare cannot undo. That is a
    hard failure, loudly, and it is why this probe targets the oldest
    record — if it somehow works, the damage is bounded to one object
    whose loss is immediately visible as a floor mismatch.

    NOTE the division of labour: this proves the TOKEN is read-only.
    The Mac-side `audit-sink-probe` proves the BUCKET LOCK refuses a
    delete even from a token that is allowed to try. Neither replaces
    the other.
    """
    prefix = records_prefix(anchor.cfg.chain_id)
    keys = [k for k in anchor.list_keys(prefix) if seq_of_key(k, prefix) is not None]
    if not keys:
        return Result(
            verdict="nothing_to_probe",
            detail="the chain holds no records, so there is nothing to try to delete",
            ok=False,
        )
    target = sorted(keys)[0]
    try:
        anchor.s3.delete_object(Bucket=anchor.cfg.bucket, Key=target)
    except Exception as e:  # noqa: BLE001 - any refusal is the pass condition
        return Result(
            verdict="refused",
            detail=f"delete of {target} was REFUSED ({type(e).__name__}: {e})",
            ok=True,
        )
    return Result(
        verdict="DELETE_SUCCEEDED",
        detail=(
            f"the reader token DELETED {target}. The credential in this container is "
            "not read-only, the audit anchor is not an anchor, and the object is gone "
            "permanently. Revoke this token in the Cloudflare dashboard now and mint "
            "an Object Read only token scoped to this bucket."
        ),
        ok=False,
    )


# ── CLI ──────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m astra.broker.audit_anchor")
    ap.add_argument("command", choices=["verify", "freshness", "probe"])
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    a = ap.parse_args(argv)

    try:
        cfg = Config.from_env()
    except AnchorError as e:
        print(f"FAIL {e}", file=sys.stderr)
        return 2

    anchor = Anchor(cfg, client(cfg))
    try:
        if a.command == "verify":
            r = verify(anchor)
        elif a.command == "freshness":
            r = freshness(anchor)
        else:
            r = delete_probe(anchor)
    except Exception as e:  # noqa: BLE001 - a verifier that crashes must say so
        print(f"FAIL the verifier could not complete: {type(e).__name__}: {e}", file=sys.stderr)
        return 3

    if a.json:
        print(json.dumps(r.__dict__, sort_keys=True, default=str))
    else:
        for line in r.lines():
            print(line)
    return 0 if r.ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
