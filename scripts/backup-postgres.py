"""
Nightly Postgres → Cloudflare R2 backup.

Runs as a Railway cron service (`backup`). Schedule: daily 03:00 UTC
(08:30 IST — middle of Kunal's deep-work window when load is low).

What it does:
  1. pg_dump the entire database to gzip'd SQL.
  2. Upload to R2 with a dated key: `astra/YYYY-MM-DD/astra-<ts>.sql.gz`.
  3. List the bucket and prune anything older than RETENTION_DAYS.
  4. Pings BetterStack heartbeat on success (separate from the
     scheduler heartbeat so a failed backup is detectable
     independent of scheduler liveness).

Failure modes handled:
  * pg_dump returns non-zero → log + exit 1 (Railway marks deploy
    failed; BetterStack monitor on heartbeat pages Kunal because
    the success ping didn't fire).
  * R2 upload fails → exit 1, same path.
  * Pruning fails → log warning, do NOT fail the run (the upload
    succeeded; pruning is bonus housekeeping).

Restore:
  $ aws --endpoint-url=$R2_ENDPOINT s3 cp s3://astra-backups/astra/YYYY-MM-DD/astra-<ts>.sql.gz - \\
      | gunzip | psql "$DATABASE_URL"
"""

from __future__ import annotations

import gzip
import logging
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
from botocore.client import Config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("astra-backup")


def _env(name: str, *, required: bool = True, default: str = "") -> str:
    v = os.environ.get(name, default)
    if required and not v:
        log.error("FATAL: missing env var %s", name)
        sys.exit(2)
    return v


def main() -> int:
    db_url = _env("DATABASE_URL")
    r2_endpoint = _env("R2_ENDPOINT")
    r2_bucket = _env("R2_BUCKET", default="astra-backups")
    r2_key_id = _env("R2_ACCESS_KEY_ID")
    r2_secret = _env("R2_SECRET_ACCESS_KEY")
    retention_days = int(_env("RETENTION_DAYS", required=False, default="30"))
    heartbeat_url = _env("BACKUP_HEARTBEAT_URL", required=False)

    # Strip the +asyncpg driver hint pg_dump won't understand.
    pg_url = db_url.replace("+asyncpg", "")

    ts = datetime.now(timezone.utc)
    date_partition = ts.strftime("%Y-%m-%d")
    obj_key = f"astra/{date_partition}/astra-{ts.strftime('%Y%m%dT%H%M%S')}.sql.gz"
    tmp_path = Path(f"/tmp/astra-{ts.strftime('%Y%m%dT%H%M%S')}.sql.gz")

    # 1. pg_dump | gzip → file on local disk
    log.info("starting pg_dump")
    with gzip.open(tmp_path, "wb", compresslevel=6) as gz:
        proc = subprocess.Popen(
            [
                "pg_dump",
                "--no-owner",
                "--no-acl",
                "--clean",
                "--if-exists",
                pg_url,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert proc.stdout is not None
        # stream chunks so memory stays low even on multi-GB DBs
        while True:
            chunk = proc.stdout.read(64 * 1024)
            if not chunk:
                break
            gz.write(chunk)
        rc = proc.wait()
        err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
    if rc != 0:
        log.error("pg_dump failed (rc=%s): %s", rc, err[:500])
        return 1
    size = tmp_path.stat().st_size
    log.info("pg_dump complete: %s bytes → %s", f"{size:,}", tmp_path)

    # 2. Upload to R2
    log.info("uploading to r2://%s/%s", r2_bucket, obj_key)
    s3 = boto3.client(
        "s3",
        endpoint_url=r2_endpoint,
        aws_access_key_id=r2_key_id,
        aws_secret_access_key=r2_secret,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )
    try:
        s3.upload_file(str(tmp_path), r2_bucket, obj_key)
    except Exception as e:
        log.error("R2 upload failed: %s", e)
        return 1
    log.info("uploaded ✓")

    # Cleanup local tmp
    try:
        tmp_path.unlink()
    except Exception:
        pass

    # 3. Prune (best-effort — never fail the run on prune errors)
    cutoff = ts - timedelta(days=retention_days)
    pruned = 0
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=r2_bucket, Prefix="astra/"):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                # Match the date partition we control: astra/YYYY-MM-DD/...
                m = re.match(r"astra/(\d{4}-\d{2}-\d{2})/", key)
                if not m:
                    continue
                obj_date = datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if obj_date < cutoff:
                    s3.delete_object(Bucket=r2_bucket, Key=key)
                    pruned += 1
        log.info("pruned %d objects older than %s", pruned, cutoff.strftime("%Y-%m-%d"))
    except Exception as e:
        log.warning("prune failed (non-fatal): %s", e)

    # 4. Heartbeat
    if heartbeat_url:
        try:
            urllib.request.urlopen(heartbeat_url, timeout=10).read()
            log.info("backup heartbeat pinged")
        except Exception as e:
            log.warning("backup heartbeat failed: %s", e)

    log.info(
        "DONE: %s/%s · %s bytes · pruned=%d",
        r2_bucket, obj_key, f"{size:,}", pruned,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
