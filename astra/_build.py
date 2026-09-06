"""Deploy marker.

scripts/deploy.sh rewrites this file with the commit it is shipping and
restores this committed sentinel afterwards. The sentinel is what a
hand-run `railway up` ships, so its values are deliberately the
"not attested" ones. Read by the stream service's /health.

Why a tracked file and not a gitignored, generated one: `railway up`
skips paths matched by .gitignore when it builds the upload archive
(that is what its --no-gitignore flag exists to override, and that
flag would also ship .env). A tracked file is always uploaded.

Contract (three module-level names, nothing else):
    build_sha:    40-hex commit sha, or "unknown"
    dirty:        False only when deploy.sh attested a clean tree;
                  True means "not attested" (this sentinel, or a
                  hand deploy)
    built_at_utc: ISO-8601 UTC timestamp with a trailing Z, or "unknown"
"""

build_sha = "unknown"
dirty = True
built_at_utc = "unknown"
