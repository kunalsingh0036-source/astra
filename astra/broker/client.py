"""The intent client: the one way anything in the cloud asks the body.

`run_intent` is the single chokepoint for every caller of the
capability broker's transport: the model's `submit_intent` tool
(astra/runtime/tools/physical.py), scheduler jobs, and any code path
that used to reach the Mac through the bridge's `_dispatch(...,
on_behalf_of=...)`. It files a row and, optionally, watches it. It
holds no authority and can grant none: the broker on the Mac
canonicalises the arguments itself, renders what Kunal reads itself,
and the fingerprint is the gate. What this module adds is everything
that should be refused BEFORE a row exists, with a message the caller
can act on:

  (a) arguments the broker would refuse anyway, checked here so the
      refusal names the offending path and costs no round trip;
  (b) verbs the executor cannot perform yet, refused before filing so
      no Touch ID prompt is ever raised for an action that will then
      fail ("intent #10" in GROUND-TRUTH: a real tap, then "not wired
      yet"). Every such prompt trains Kunal to tap for nothing;
  (c) the caller class: inside a turn any catalogue verb may be filed
      on any channel; outside a turn (scheduler jobs) only `auto`
      verbs, so no job can ever raise a fingerprint prompt;
  (d) a body that is not polling, refused in one round trip instead
      of a 300 s wait on a closed laptop; told apart from a body that
      is awake but BUSY (the broker serves one intent at a time and
      stops polling while it is inside one, for up to an hour on a
      signed verb with the Touch ID prompt on screen), which is
      refused as 'busy' naming the intent, never as 'offline'.

(c) is a HABITUATION CONTROL, NOT A SECURITY GATE. The turn claim is a
ContextVar the cloud sets about itself and writes to
intents.session_claim as exactly that, a claim; anything that can run
code in this process can set it. The security gate is unchanged and
lives on the Mac: a `signedNoStanding` verb executes only under a
Secure Enclave signature over a payload the broker rendered, and the
executor verifies that signature against a key compiled into its own
binary. Do not describe this rule as a gate anywhere; that is the
caller-asserted-permission class already fixed once in the browser ACT
queue.

`actor` is keyword-only and is never a tool argument, the same
unreachable-from-the-model property `on_behalf_of` had: the model
composes every tool argument, so a fact about who is calling can never
come from the args.

REFUSALS CARRY A CODE. Every pre-filing refusal sets
`IntentResult.refusal_code` to one of `REFUSAL_CODES`, and callers
branch on that field, never on the wording of `deny_reason`. The first
version of notes_tools and jobs matched `"not wired" in reason`, which
was the EXECUTOR's phrase; the client's own refusal said "cannot
perform ... yet", so the branch was dead and the chat tool told Kunal
to open a laptop that was already open. Prose is for Kunal; the code is
for code. Executor text (`result withheld: ...`) may still be matched,
but ONLY on `deny_reason` of a FILED row, where the executor wrote it.

The catalogue mirror below (`CATALOGUE`, and `WIRED_VERBS`,
`AUTO_VERBS`, `SIGNED_VERBS` derived from it) plus the path policy
tables (`ALLOWED_ROOTS`, `DENIED_*`, `SOURCE_EXTENSIONS`,
`FS_READ_MAX_LIMIT`, `AUTO_INTENTS_PER_HOUR`) are the cloud's copy of
the Swift sources: Sources/AstraCore/Catalog.swift (policies and
argument keys), Sources/AstraCore/Canonicalizer.swift (roots and name
denial), Sources/AstraCore/FileVerbs.swift (the read ceiling),
Sources/AstraCore/RateLimit.swift (the auto ceiling) and
Sources/AstraExecutor/main.swift (which verbs execute() actually
handles). They are pinned by tests/test_broker/test_catalogue_mirror.py,
which parses those files and fails if either repo changes alone.

WHICH SOURCE DECIDES "WIRED". The executor's own word, when this
process has it, beats the mirror: the broker POSTs its verb table to
/broker/catalog when it starts, and a build that puts a per-verb
`wired` flag in that POST is relaying the set the executor reports in
every challenge reply (`note_published_catalogue`, `wired_state`). The
mirror is the fallback for every case where this process has no such
publish: the scheduler (a separate process the POST never reaches), a
stream process restarted after the broker's last publish, and a broker
build whose POST carries no flag. It is deliberately NOT persisted
(services/stream/main.py: a cache that survived a restart would be a
configuration store), so "unknown" is the common state and the mirror
must stay pinned. A verb unwired by the deciding source is refused
before filing for auto and signed verbs alike; the blind "file it and
let the executor refuse" fail-open was NOT adopted for auto verbs
because the scheduler never has the published set and would file a
doomed notes.sync every 30 minutes, the audit-noise class. The
executor's Verifier remains the belt: it refuses any unwired verb the
cloud files anyway.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import posixpath
import time
import unicodedata
from typing import Any

from astra.broker import ace, store
from astra.config import settings

logger = logging.getLogger(__name__)

__all__ = [
    "VerbSpec", "CATALOGUE", "CATALOGUE_BY_NAME",
    "WIRED_VERBS", "AUTO_VERBS", "SIGNED_VERBS", "FORBIDDEN_ARG_KEYS",
    "ALLOWED_ROOTS", "FS_READ_MAX_LIMIT", "FS_READ_MAX_OFFSET",
    "WEB_SCREENSHOT_MAX_PIXELS", "AUTO_INTENTS_PER_HOUR",
    "AUTO_INTENT_WAIT_SEC", "DAILY_IRREVERSIBLE_MAX", "IRREVERSIBLE_VERBS",
    "SECOND_CONFIRMATION_VERBS", "EXEC_SHELL_MAX_TIMEOUT_MS",
    "DESTROYED_BLOB_KEYS", "budget_timing_note",
    "DISPLAY_MAX_CHARS", "CONTENT_PREVIEW_CHARS", "WHY_MAX_CHARS",
    "DISPLAY_MAX_LINES", "DISPLAY_MAX_LINE_CHARS", "display_line_budget",
    "display_width", "display_order",
    "REFUSAL_CODES", "IntentResult", "ArgsInvalid", "PreconditionFailed",
    "precheck_args",
    "run_intent", "status", "verify_receipt", "receipt_outcome",
    "validate_args", "denial_reason", "MAX_PENDING", "BODY_POLL_WINDOW_SEC",
    "PublishedCatalogue", "note_published_catalogue", "published_wired",
    "wired_state",
]


# ── The catalogue mirror ──────────────────────────────────


@dataclasses.dataclass(frozen=True)
class VerbSpec:
    name: str
    policy: str                    # "auto" | "signedNoStanding"
    arg_keys: frozenset[str]       # CLOSED: an unknown key is refused
    required_keys: frozenset[str]
    path_keys: frozenset[str]      # single filesystem targets: root-checked
    pattern_keys: frozenset[str]   # glob patterns: literal prefix root-checked
    int_keys: frozenset[str]       # must be non-negative integers
    # A CLOSED ENUM the executor resolves to a compiled path. Mirrors
    # VerbSpec.enumKeys in Catalog.swift. The shape is enforced on both
    # sides (a bare identifier, never a path) because a probe that could
    # name its own path would be an oracle for walking the filesystem
    # one open() at a time, under a policy that returns no bytes and so
    # looks harmless.
    enum_keys: frozenset[str]
    max_arg_bytes: int
    wired: bool                    # execute() in AstraExecutor handles it
    # Per-key integer RANGES, as (key, lo, hi) inclusive. Mirrors
    # VerbSpec.intBounds in Catalog.swift, which Catalog.assertWellFormed
    # requires to be complete over intKeys — so every integer argument
    # in the catalogue has a declared range on both sides and this
    # tuple is never a partial copy of it.
    #
    # BOTH ENDS, and that is the correction. This was a ceiling-only
    # mirror while `timeout_ms` was a number the jail quietly clamped
    # after Kunal had read it: `timeout_ms: 300000` rendered verbatim
    # on both Touch ID sheets, cost two fingerprints and one of the
    # day's three irreversible units, and was then cut to 55 s at the
    # moment of acting, killing the command mid-way. The Mac now
    # REFUSES an out-of-range integer at its precheck — before the
    # prompt and before the budget — so a ceiling-only, clamp-assuming
    # mirror here would file an intent that dies on arrival and spend
    # one of the ten signed slots an hour to learn it.
    int_bounds: tuple[tuple[str, int, int], ...] = ()
    # The CONTENT CLASS: arguments that are a payload rather than a
    # name, a number or a target. Mirrors VerbSpec.blobKeys in
    # Catalog.swift, and it is not cosmetic — two rules hang off it on
    # the Mac and both are visible from here:
    #
    #   the approval sheet SUMMARISES a payload
    #     "<n> bytes, sha256 <8 hex>, first <k> of <n> bytes: <escaped>"
    #     rather than showing it whole (Render.swift). Every other
    #     argument is still shown verbatim or the intent is refused, so
    #     a long `command` on exec.shell still refuses.
    #   a payload must already be NFC
    #     ACE-1 hashes strings as NFC UTF-8, so a decomposed payload
    #     would be signed as its composed form and the bytes on disk
    #     would not be the bytes sent. The Mac refuses it; this module
    #     refuses it first, with the key named.
    blob_keys: frozenset[str] = frozenset()
    # Counted against the daily irreversible budget (3 a day, shared by
    # every irreversible verb). Mirrors VerbSpec.irreversible.
    irreversible: bool = False

    @property
    def signed(self) -> bool:
        return self.policy != "auto"


def _v(name: str, policy: str, args: str, required: str, paths: str,
       patterns: str, ints: str, max_arg_bytes: int, *, wired: bool,
       enums: str = "", blobs: str = "", irreversible: bool = False,
       int_bounds: tuple[tuple[str, int, int], ...] = ()) -> VerbSpec:
    split = lambda s: frozenset(k for k in s.split() if k)  # noqa: E731
    return VerbSpec(name, policy, split(args), split(required), split(paths),
                    split(patterns), split(ints), split(enums), max_arg_bytes,
                    wired, int_bounds, split(blobs), irreversible)


# Mirror of FileVerbs.maxReadLimit (640 KiB): the reply frame is 1 MiB
# and carries base64, so a larger fs.read is refused by the executor.
FS_READ_MAX_LIMIT = 640 * 1024

# Mirror of the `offset` ceiling in Catalog.swift's fs.read intBounds.
# A read a terabyte into a file is a mistake, not a page.
FS_READ_MAX_OFFSET = 1 << 40

# Mirror of ShellJail.maxTimeoutMs, and of the UPPER end of exec.shell's
# declared `timeout_ms` range. Out of range is REFUSED at the Mac's
# precheck — before the sheet is composed and before the budget — never
# clamped at the moment of acting: the number Kunal reads on the Touch
# ID prompt is the number the command gets, or there is no prompt.
EXEC_SHELL_MAX_TIMEOUT_MS = 55_000

# Mirror of the width/height range in web.screenshot's intBounds. The
# verb is not wired, but the shape is typed on both sides so it cannot
# become a free string in the interval.
WEB_SCREENSHOT_MAX_PIXELS = 8192

# Mirror of RateLimit.Ceilings.autoPerHour. The broker refuses the 61st
# auto intent in a sliding hour; every paged reader must stay under it.
AUTO_INTENTS_PER_HOUR = 60

# How long one caller waits on ONE auto intent before saying "the Mac
# has not answered". DERIVED, not chosen: the live loop measured on
# production intents 5-10 (GROUND-TRUTH 2026-09-05, DB clock) was
# 1.0-7.6 s to claim and 3.2-12.5 s to resolve, so this is twice the
# measured worst. A page slower than that means the Mac is asleep or
# wedged, and the honest move is to stop and say so rather than wait.
# The model's submit_intent (physical.py) and the paged reader
# (reply_tools._READ_WAIT_SEC) both wait exactly this; their registry
# budgets are this plus a margin, pinned by
# tests/test_runtime/test_timeout_hierarchy_broker.py.
AUTO_INTENT_WAIT_SEC = 25

# Mirror of Catalog.swift `verbs` (same order, same ids) plus the
# executor's execute() switch. Pinned by test_catalogue_mirror.py.
CATALOGUE: tuple[VerbSpec, ...] = (
    _v("fs.read", "auto", "path offset limit", "path", "path", "",
       "offset limit", 4096, wired=True,
       int_bounds=(("limit", 0, FS_READ_MAX_LIMIT),
                   ("offset", 0, FS_READ_MAX_OFFSET))),
    _v("fs.glob", "auto", "pattern root", "pattern", "root", "pattern",
       "", 4096, wired=True),
    _v("fs.grep", "auto", "pattern path include", "pattern path", "path",
       "", "", 8192, wired=False),
    _v("web.screenshot", "auto", "url width height", "url", "", "",
       "width height", 4096, wired=False,
       int_bounds=(("height", 1, WEB_SCREENSHOT_MAX_PIXELS),
                   ("width", 1, WEB_SCREENSHOT_MAX_PIXELS))),
    _v("notes.sync", "auto", "", "", "", "", "", 64, wired=False),
    # The write verbs, wired 2026-09-06. Each is one Touch ID prompt
    # every time, no standing grant, and one unit of the day's three
    # irreversible actions. `content`, `old` and `new` are the content
    # class: summarised on the sheet, and NFC or refused.
    _v("fs.write", "signedNoStanding", "path content", "path content",
       "path", "", "", 1_048_576, wired=True, blobs="content",
       irreversible=True),
    _v("fs.edit", "signedNoStanding", "path old new", "path old new",
       "path", "", "", 1_048_576, wired=True, blobs="old new",
       irreversible=True),
    # cwd is REQUIRED: the jail's one writable subtree is the approved
    # cwd, so it is a security-relevant display line and there is no
    # honest default (the executor's home is /var/empty, and a default
    # the canonicaliser never sees is a default Kunal never reads).
    # timeout_ms is an integer with a DECLARED RANGE (1 …
    # EXEC_SHELL_MAX_TIMEOUT_MS), refused at the Mac's precheck rather
    # than clamped at the moment of acting, and mirrored here so the
    # refusal costs no signed-lane slot. It used to be a clamp: an
    # intent asking for 300000 showed Kunal `timeout_ms: 300000` on
    # both sheets, took two fingerprints and one of the day's three
    # irreversible units, and was then cut to 55 s with the command
    # killed mid-way and the clamp announced afterwards in the result.
    # A sheet that states a number the body will not honour is a sheet
    # that lies, so the number is now refused before it can be shown.
    #
    # `wired` for this verb is the one entry the Swift table cannot
    # settle statically: the executor adds exec.shell to its dispatch
    # table at boot ONLY if the sandbox-exec canary passes (a known-deny
    # read must fail and a known-allow must succeed). The mirror says
    # what the build can do; `published_wired` — the set the executor
    # itself reports in every challenge reply — is the authority when
    # this process has one, and the broker refuses an unwired verb
    # before any prompt either way.
    _v("exec.shell", "signedNoStanding", "command cwd timeout_ms",
       "command cwd", "cwd", "", "timeout_ms", 8192, wired=True,
       irreversible=True,
       int_bounds=(("timeout_ms", 1, EXEC_SHELL_MAX_TIMEOUT_MS),)),
    # "Can the body open X?", answered yes or no and never with bytes.
    # `target` is a CLOSED ENUM (messages, safari, mail, whatsapp,
    # documents) that the executor resolves to a compiled path; it is
    # deliberately not a path argument, because a probe that could name
    # its own path would be an oracle for walking the filesystem one
    # open() at a time under a policy that returns nothing and so looks
    # harmless. The canonicaliser enforces the shape (a bare
    # identifier) before anything is signed.
    _v("body.probe", "auto", "target", "target", "", "", "", 256,
       wired=True, enums="target"),
)

CATALOGUE_BY_NAME: dict[str, VerbSpec] = {v.name: v for v in CATALOGUE}
WIRED_VERBS: frozenset[str] = frozenset(v.name for v in CATALOGUE if v.wired)
AUTO_VERBS: frozenset[str] = frozenset(
    v.name for v in CATALOGUE if v.policy == "auto")
SIGNED_VERBS: frozenset[str] = frozenset(
    v.name for v in CATALOGUE if v.policy == "signedNoStanding")

# Mirror of Canonicalizer.swift `forbiddenKeys`: names that carry
# authority, refused as KEYS so the agent cannot even assert them.
FORBIDDEN_ARG_KEYS: frozenset[str] = frozenset({
    "approved", "approval", "tier", "body_id", "body", "epoch",
    "permission_epoch", "expiry", "expires", "not_before", "nonce",
    "challenge", "reason", "standing", "policy", "irreversible",
    "budget_seq", "verb_id", "intent_id", "sig", "signature", "token",
})

# Mirror of Canonicalizer.allowedRoots. The broker realpath()s first and
# compares second; the cloud cannot realpath a Mac path, so it compares
# the lexically normalised path and treats /tmp as /private/tmp (the
# macOS symlink the Swift comment explains). A path that passes here
# and resolves outside a root through a symlink is still refused on the
# Mac; this check exists so the common case is refused in zero round
# trips with the root set named.
ALLOWED_ROOTS: tuple[str, ...] = (
    "/Users/kunalsingh/Claude Code",
    "/Users/kunalsingh/Documents",
    "/private/tmp",
    # The personal stores, 2026-09-06, on Kunal's explicit instruction
    # (capabilities first, gates later). Each is the directory behind
    # one body.probe target — the narrowest widening that lets the
    # probes mean anything, rather than opening ~/Library wholesale.
    # Three independent layers still stand above every byte: the ACL
    # (uid 451 has search on the ancestors and read here, nothing else),
    # TCC (Full Disk Access, keyed to the executor's exact bytes), and
    # policy (this list, the name deny-list, and the content gate on
    # the way out).
    "/Users/kunalsingh/Library/Messages",
    "/Users/kunalsingh/Library/Safari",
    "/Users/kunalsingh/Library/Mail",
    "/Users/kunalsingh/Library/Group Containers/group.net.whatsapp.WhatsApp.shared",
)
_ROOT_ALIASES: tuple[tuple[str, str], ...] = (("/tmp", "/private/tmp"),)

# Mirror of Canonicalizer's name-denial tables. Closed and compiled on
# the Mac; a new shape is a code change in BOTH repos with a test.
DENIED_EXACT: frozenset[str] = frozenset({
    ".ssh", ".gnupg", ".aws", ".azure", ".gcloud", ".config", ".railway",
    ".docker", ".kube", ".terraform", ".vault-token", ".netrc", ".npmrc",
    ".pypirc", ".git-credentials", ".git", ".envrc", ".astra-body",
    ".htpasswd", "keychains",
    ".claude", ".cursor", ".idea", ".vscode",
    ".next", ".turbo", ".cache", ".parcel-cache", ".nuxt", ".svelte-kit",
    "node_modules",
})
DENIED_PREFIXES: tuple[str, ...] = (".env",)
DENIED_BASES: frozenset[str] = frozenset({
    "credentials", "credential", "secrets", "secret",
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
    "private_key", "privatekey", "private-key",
    "client_secret", "client-secret",
    "service-account", "serviceaccount", "service_account", "serviceaccountkey",
    "apikey", "api_key", "api-key",
})
DENIED_BASE_WORDS: frozenset[str] = frozenset({
    "credential", "credentials", "secret", "secrets", "token", "tokens",
    "adminsdk", "apikey", "password", "passwd", "passwords",
})
DENIED_EXTENSIONS: frozenset[str] = frozenset({
    "pem", "key", "p12", "pfx", "jks", "keystore", "kdbx", "gpg", "asc",
    "ppk", "keychain", "keychain-db", "p8", "ovpn",
    "sqlite", "sqlite3", "db-wal", "db-shm", "tfstate", "tfvars", "har",
})
SOURCE_EXTENSIONS: frozenset[str] = frozenset({
    "swift", "ts", "tsx", "js", "jsx", "mjs", "cjs", "py", "go", "rs",
    "java", "kt", "kts", "rb", "php", "c", "h", "cc", "cpp", "hpp", "cs",
    "m", "mm", "css", "scss", "less", "html", "htm", "vue", "svelte",
    "sh", "zsh", "bash", "sql", "graphql", "proto",
})

# A queue deeper than a human could plausibly approve is a flood, not a
# backlog. Cheap, cloud-side, and it bounds the case where the model
# loops. The broker's own per-hour rate limit (RateLimit.swift) is the
# real control.
MAX_PENDING = 20

# The broker long-polls /broker/intents/next with a 40 s client timeout
# and the route holds 25 s, so a live body touches last_poll_at at
# least every ~40 s. Older than this and the laptop is closed or the
# broker is down; either way nothing will claim a new intent soon.
BODY_POLL_WINDOW_SEC = 60

# TTLs. A signed verb takes as long as a human takes; an auto verb
# should be quick. Neither may sit forever: `claimed` is otherwise
# indistinguishable from `the broker died`.
TTL_SIGNED_SEC = 3600
TTL_AUTO_SEC = 300

# ── Policy the model has to be told the truth about ───────

# Mirror of BudgetPolicy.dailyIrreversibleMax (Catalog.swift). Every
# irreversible verb shares ONE counter, spent inside the executor's
# verification and never refunded. The red team's judgement, adopted:
# six a day is too many when one of them can be exec.shell.
DAILY_IRREVERSIBLE_MAX = 3
IRREVERSIBLE_VERBS: frozenset[str] = frozenset(
    v.name for v in CATALOGUE if v.irreversible)

# Mirror of BudgetPolicy.requiresSecondConfirmation. exec.shell takes
# TWO taps: the second is a separate enclave signature over
# SHA256("ASTRA-2ND-v1" ‖ P ‖ sig_human), checked by the EXECUTOR
# against its own copy of the approver key — so it is not a prompt the
# broker counts and could skip.
SECOND_CONFIRMATION_VERBS: frozenset[str] = frozenset({"exec.shell"})


def budget_timing_note() -> str:
    """WHEN a refusal costs one of the day's irreversible units.

    One sentence, in one place, because both model-facing surfaces
    carry it and an earlier version of each was WRONG in the same
    direction: they said a file that changed between the check and the
    write "DOES spend the unit, because the unit is spent on the
    attempt". That is only true for the last of three cases.

    The Mac decides in this order (Verify.swift): (a) the shape and
    signature checks, (b) step (j)/(j2) — the target and its parent
    must still be the objects that were approved, (j') — the verb's own
    preconditions re-asked against the file as it is NOW, and only then
    (k) the budget spend. So a file that changed during the approval
    window is caught at (j)/(j') and the counter never moves; what does
    move it is a failure INSIDE the act, after (k) — an EACCES on the
    write, a non-zero exit from the shell, or a target rewritten in the
    milliseconds between the last check and the rename.

    Saying otherwise is not a harmless overstatement: a model that
    believes a stale edit burned one of three will tell Kunal the day's
    allowance is gone when it is not, and will decline work it could
    have done. Both directions of that error are confabulation about a
    number this process cannot read.
    """
    return (
        "WHEN A UNIT IS ACTUALLY SPENT, in the order the Mac decides: "
        "refused BEFORE the prompt (an unwired verb, arguments the gate "
        "rejects, an `old` that matches zero or several times, a missing "
        "parent, a symlink target, a cwd that is not a directory — these "
        "come back as `precondition_failed`) costs no fingerprint and no "
        "unit; refused AFTER the prompt but before the action (the file "
        "or its directory changed while Kunal was reading, so the "
        "approved target is no longer the object on disk — "
        "`precondition_failed` or `path_changed`) costs the fingerprint "
        "and still NO unit, because the Mac checks that before it "
        "touches the counter; and a failure INSIDE the action "
        "(permission denied, a non-zero exit, a file rewritten in the "
        "last millisecond) DOES spend one, because by then the attempt "
        "was made. Never tell Kunal the day's allowance is gone on the "
        "strength of a refusal; only a finished attempt spends."
    )

# Mirror of Render.maxDisplayChars and maxContentPreviewChars. The
# approval sheet is capped and NOTHING is truncated to fit it: an
# over-long display refuses the intent. The one exception is the
# content class (`blob_keys`), summarised as
# "<n> bytes, sha256 <8 hex>, first <k> of <n> bytes: <escaped>" —
# which is what makes a 1 MiB fs.write possible at all. `why` can never
# shrink that preview: it has a COMPILED reservation on the sheet
# (Render.maxWhyLineChars), taken out whether or not a `why` is
# present, and one that does not fit the reservation refuses the intent
# rather than growing the sheet or shrinking the preview.
DISPLAY_MAX_CHARS = 400
CONTENT_PREVIEW_CHARS = 120

# Mirror of Render.maxDisplayLines and Render.maxDisplayLineChars — the
# two caps that bound the sheet's SHAPE rather than its total size.
#
# The character cap alone bounded the wrong quantity. A Touch ID dialog
# clips by visual ROW and it wraps, so one 250-character path is a
# single line by the newline count and roughly five rows on screen: a
# five-line sheet could occupy a dozen rows, and the rows that fall off
# the bottom are `because:` and, on an fs.edit, `old:` — the field that
# says what is about to be destroyed, with nothing on screen to say
# anything was withheld.
#
# So ONE ARGUMENT'S LINE has its own ceiling, counted the way
# `display_width` counts (escaped), and an intent whose path, cwd or
# command exceeds it is refused before Kunal sees anything. Payloads
# can never trip it — their preview budget is computed from what the
# fixed parts leave — so this is a rule about long paths and long shell
# commands, and both are refused rather than shortened.
DISPLAY_MAX_LINES = 6
DISPLAY_MAX_LINE_CHARS = 200

# What one ARGUMENT may be, once the sheet's own "  <key>: " label and
# newline are taken off. Verb- and key-dependent, so it is a function
# rather than a constant.


def display_line_budget(key: str) -> int:
    """How many sheet-characters `key`'s VALUE may occupy."""
    return DISPLAY_MAX_LINE_CHARS - len(f"  {key}: ") - 1

# Mirror of Render.maxWhyLineChars, minus the "  because: " label and
# the newline the Mac adds — so this is what `why` itself may be.
#
# Refused HERE as well as there because the Mac's refusal costs a round
# trip and reads as `unrenderable`, which sounds like the arguments are
# too big when what is too long is one sentence the model wrote. The
# count is on the ESCAPED form: every non-ASCII character becomes a
# visible \uXXXX on the sheet (up to 10 characters), so a cap counted
# in source characters is not a cap on what Kunal sees.
WHY_MAX_CHARS = 96 - len("  because: ") - 1


# Mirror of Render.destroyedBlobKeys: which of a verb's payloads names
# the bytes being DESTROYED rather than the bytes being written.
#
# The Mac keeps this as a per-verb table rather than a VerbSpec field
# because it cannot be derived — `old` and `new` are blobs of identical
# shape, and nothing in a type says which one disappears — and it
# refuses to boot (Render.assertOrderWellFormed) if a verb carries two
# payloads and names neither. Mirrored in the same shape so
# test_write_verbs can compare the two literals directly.
DESTROYED_BLOB_KEYS: dict[str, frozenset[str]] = {
    "fs.edit": frozenset({"old"}),
}


def display_order(spec: VerbSpec) -> tuple[str, ...]:
    """The order Kunal's approval sheet puts this verb's arguments in.

    Mirror of `Render.rank` / `Render.orderedKeys` on the Mac. Four
    classes, and the classes are the point:

      0   the filesystem target (path keys, pattern keys) — the thing
          being acted ON is read first. Plain alphabetical order put
          fs.write's 1 MiB `content` above its `path`, which is the
          wrong way round for a sheet somebody reads top to bottom.
      10  everything else, verbatim.
      15  the payload the verb DESTROYS (fs.edit's `old`).
      20  the rest of the content class, last: the only elastic and the
          only summarised part.

    Class 15 is the newest and it exists because the sheet's row
    capacity is still UNMEASURED against a real Touch ID dialog. If the
    dialog shows fewer rows than the sheet occupies it clips from the
    bottom and gives no sign that it did, so alphabetical order inside
    the content class — `new` then `old` — put the text being DELETED
    in the last argument row of an fs.edit. The removal now outranks
    the replacement, and what falls off a clipping dialog is the
    additive half.

    Ties inside a class break ALPHABETICALLY, exactly as Swift does it
    (`ra == rb ? a < b : ra < rb`). It is worth being explicit about
    that because the cloud had two other rules before this function
    existed — one that put required arguments first, one that ignored
    the classes entirely — and both were used to TELL the model what
    Kunal's prompt looks like. They disagreed with the sheet on
    exec.shell, where `cwd` is a path key and therefore the first line
    Kunal reads, while the prompt announced `command` first.

    A description of a sheet is a claim about another program. There
    is one rule here so there is one claim.
    """
    destroyed = DESTROYED_BLOB_KEYS.get(spec.name, frozenset())

    def rank(k: str) -> int:
        if k in spec.path_keys or k in spec.pattern_keys:
            return 0
        if k not in spec.blob_keys:
            return 10
        return 15 if k in destroyed else 20

    return tuple(sorted(spec.arg_keys, key=lambda k: (rank(k), k)))


def display_width(s: str) -> int:
    """What `s` costs on the approval sheet — the length of
    Render.safe(s), not of `s`.

    The renderer is deliberately hostile to its own input: printable
    ASCII passes through and EVERYTHING else becomes a visible escape,
    so "/Users/kunalsingh/pгod.db" cannot be mistaken for the
    Latin spelling. One Devanagari character therefore costs six
    columns and one emoji ten. A cap counted in `len(s)` is not a cap
    on the sheet, which is the mistake the Swift side already paid for
    once in its preview budget.
    """
    n = 0
    for ch in s:
        v = ord(ch)
        if 0x20 <= v <= 0x7E:
            n += 1
        elif v in (0x0A, 0x09):     # \n and \t are two characters each
            n += 2
        elif v <= 0xFFFF:
            n += 6                  # \uXXXX
        else:
            n += 10                 # \UXXXXXXXX
    return n


# ── The published catalogue (per process) ─────────────────


@dataclasses.dataclass(frozen=True)
class PublishedCatalogue:
    """What the broker last POSTed to /broker/catalog in THIS process.

    `wired` is the executor's own set when every published verb
    carried a boolean `wired` flag, else None (a broker build that
    predates the flag, or a partial publish, which is treated as no
    publish and logged). `at` is this process's clock at receipt.
    """
    body_id: int
    verbs: frozenset[str]
    wired: frozenset[str] | None
    build_cdhash: str
    at: float


_PUBLISHED: dict[int, PublishedCatalogue] = {}


def note_published_catalogue(
    body_id: int, verbs: list[Any], build_cdhash: str = "",
) -> PublishedCatalogue:
    """Record a /broker/catalog POST. Called by the stream route only;
    the value is display and pre-check input, never policy the broker
    reads back. Names outside the mirror are kept (the mirror test
    catches the drift), but a `wired` flag is honoured only when every
    verb carries one: half a set is not a set."""
    names: list[str] = []
    flags: dict[str, bool] = {}
    for v in verbs or ():
        if not isinstance(v, dict) or not v.get("name"):
            continue
        name = str(v["name"])
        names.append(name)
        w = v.get("wired")
        if isinstance(w, bool):
            flags[name] = w
    wired: frozenset[str] | None
    if names and len(flags) == len(names):
        wired = frozenset(n for n, w in flags.items() if w)
    else:
        wired = None
        if flags:
            logger.warning(
                "[broker] catalog publish from body #%s flags %d of %d verbs "
                "as wired/unwired; ignoring the flags (the mirror decides)",
                body_id, len(flags), len(names),
            )
    pub = PublishedCatalogue(
        body_id=int(body_id), verbs=frozenset(names), wired=wired,
        build_cdhash=str(build_cdhash or ""), at=time.time(),
    )
    _PUBLISHED[int(body_id)] = pub
    logger.info(
        "[broker] catalog published by body #%s: %d verbs, wired=%s",
        body_id, len(names),
        "unknown" if wired is None else sorted(wired),
    )
    return pub


def published_wired(body_id: int | None = None) -> PublishedCatalogue | None:
    """The publish whose `wired` set decides, or None when this process
    has none. With no body id (run_intent checks before it touches the
    database) the single published body decides; two or more bodies
    with publishes is the ambiguous case `_only_body` refuses anyway,
    so it falls back to the mirror rather than guess."""
    if body_id is not None:
        pub = _PUBLISHED.get(int(body_id))
    elif len(_PUBLISHED) == 1:
        pub = next(iter(_PUBLISHED.values()))
    else:
        pub = None
    if pub is None or pub.wired is None:
        return None
    return pub


def wired_state(verb: str, body_id: int | None = None) -> tuple[bool, str]:
    """(wired, source): the executor's published word when this process
    has it, else the mirror. `source` is 'published' or 'mirror' and is
    for the refusal text and body_status, never for branching."""
    pub = published_wired(body_id)
    if pub is not None:
        return verb in pub.wired, "published"
    return verb in WIRED_VERBS, "mirror"


# ── The result ────────────────────────────────────────────


# The closed vocabulary of pre-filing refusals. Callers branch on
# these. Adding one means adding a branch to every caller, which is
# the point: a new refusal is a new situation Kunal must be told about
# correctly, not a new sentence for a substring match to miss.
REFUSAL_CODES: frozenset[str] = frozenset({
    "unknown_verb",         # not a catalogue verb
    "args",                 # the broker would refuse these arguments
    # Canonical, and still impossible: an integer outside the range its
    # verb declares. Named after the Mac's own refusal so the model
    # reads one word for one cause whichever side saw it first, and
    # kept distinct from "args" because the Mac's canonicaliser would
    # have ACCEPTED these — a different layer said no.
    "precondition_failed",

    "signed_outside_turn",  # a fingerprint verb from a job
    "unwired",              # catalogued, but execute() cannot do it yet
    "no_body",              # no Mac has enrolled
    "ambiguous_body",       # more than one body; the client cannot choose
    "offline",              # the body is not polling (laptop closed)
    "busy",                 # not polling because it is inside an intent
    "queue_full",           # MAX_PENDING already pending
})


@dataclasses.dataclass
class IntentResult:
    """What a caller learns. `status` is the row's status vocabulary
    ('pending', 'claimed', 'awaiting_human', 'running', 'succeeded',
    'failed', 'denied', 'expired') or 'refused', which means NO ROW
    EXISTS: the request was turned away here, before filing,
    `refusal_code` says which case (one of REFUSAL_CODES) and
    `deny_reason` says why in words. For a FILED row `refusal_code`
    is '' and `deny_reason` is what the broker or executor wrote."""
    intent_id: int | None
    status: str
    verb: str = ""
    result_bytes: bytes | None = None
    deny_reason: str | None = None
    receipt_verdict: str = ""
    display: str = ""
    result_note: str = ""
    refusal_code: str = ""
    # The outcome byte the EXECUTOR signed inside the receipt: "ok",
    # "partial", "refused", "error", or "" when there is no receipt.
    #
    # Not a duplicate of `status`. The broker maps `partial` onto the
    # row status "succeeded" (it is a success: the thing happened), and
    # partial is precisely the case a caller must not be allowed to
    # read as a plain success — for fs.write it means the file landed
    # and the full-control ACE for Kunal did not, so he has a file in
    # his own repo that he cannot write; for exec.shell it means the
    # command ran and its output was withheld. Both are outcomes with a
    # consequence, and both would be invisible from `status` alone.
    receipt_outcome: str = ""

    @property
    def filed(self) -> bool:
        return self.intent_id is not None

    @property
    def refused(self) -> bool:
        return self.status == "refused"

    @property
    def terminal(self) -> bool:
        return self.status in store._TERMINAL

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"


def _refused(verb: str, code: str, why: str) -> IntentResult:
    assert code in REFUSAL_CODES, code
    return IntentResult(intent_id=None, status="refused", verb=verb,
                        deny_reason=why, refusal_code=code)


# ── (a) argument pre-validation ───────────────────────────


class ArgsInvalid(ValueError):
    """The arguments would be refused by the broker's canonicaliser.
    Raised here, before filing, with the offending path named."""


class PreconditionFailed(ValueError):
    """Canonical, and still impossible: the Mac would refuse this at
    `Precondition.check`, after canonicalisation and before the sheet.

    A separate exception from `ArgsInvalid` because they mirror two
    different Mac-side layers and the refusal codes differ — and
    because the shared boundary corpus pins `validate_args` against
    the canonicaliser alone, where these values are perfectly legal."""


def denial_reason(component: str) -> str | None:
    """Mirror of Canonical.denialReason(component:). The reason a path
    component is refused by NAME, or None. Same tables, same order."""
    c = component.lower()
    if c in DENIED_EXACT:
        return f"'{component}' is a secret or state store"
    for pfx in DENIED_PREFIXES:
        if c.startswith(pfx):
            return f"'{component}' is an environment file"
    dotted = c.startswith(".")
    core = c[1:] if dotted else c
    segs = core.split(".")
    base = segs[0] if segs else ""
    if not base:
        return None
    exts = segs[1:]
    last = exts[-1] if exts else ""
    is_source = bool(last) and last in SOURCE_EXTENSIONS
    for e in exts:
        if e in DENIED_EXTENSIONS:
            return f"'{component}' is key material"
    if base in DENIED_BASES and not is_source:
        return f"'{component}' is named like a credential"
    data_file = bool(last) and not is_source
    if data_file or (dotted and not exts):
        for w in base.replace("-", "_").split("_"):
            if w in DENIED_BASE_WORDS:
                return f"'{component}' is named like a credential"
    return None


def _assert_not_denied(key: str, original: str, path: str) -> None:
    """Mirror of Canonical.assertNotDenied: every component of an
    absolute path or pattern is judged by name; a wildcard component
    by its literal head."""
    for comp in path.split("/"):
        if not comp:
            continue
        w = next((i for i, ch in enumerate(comp) if ch in "*?["), None)
        if w is not None:
            head = comp[:w].lower()
            if not head:
                continue
            for pfx in DENIED_PREFIXES:
                if head.startswith(pfx) or (pfx.startswith(head) and len(head) >= 3):
                    raise ArgsInvalid(
                        f"args.{key} ({original}) is refused: '{comp}' matches "
                        "environment files. Secret-bearing files are outside "
                        "every verb's reach; this is policy, not a permission "
                        "you can be granted. Nothing was filed."
                    )
            for e in DENIED_EXACT:
                if len(head) >= 3 and e.startswith(head):
                    raise ArgsInvalid(
                        f"args.{key} ({original}) is refused: '{comp}' matches "
                        f"'{e}'. Secret-bearing files are outside every verb's "
                        "reach; this is policy, not a permission you can be "
                        "granted. Nothing was filed."
                    )
            continue
        why = denial_reason(comp)
        if why:
            raise ArgsInvalid(
                f"args.{key} ({original}) is refused: {why}. Secret-bearing "
                "files are outside every verb's reach regardless of lane; "
                "this is policy, not a permission you can be granted. "
                "Nothing was filed."
            )


def _lexical_resolve(p: str) -> str:
    """What the broker's realpath() would yield for the parts the cloud
    can know: `.`/`..`/`//` collapsed and the /tmp alias applied."""
    resolved = posixpath.normpath(p)
    for alias, real in _ROOT_ALIASES:
        if resolved == alias or resolved.startswith(alias + "/"):
            resolved = real + resolved[len(alias):]
    return resolved


def _inside_roots(resolved: str) -> bool:
    return any(resolved == r or resolved.startswith(r + "/") for r in ALLOWED_ROOTS)


def _check_path(key: str, value: Any) -> None:
    """Mirror of Canonical.resolvePath minus the filesystem."""
    if not isinstance(value, str):
        raise ArgsInvalid(
            f"args.{key} must be a string path, got {type(value).__name__}. "
            "Nothing was filed."
        )
    if not value.startswith("/"):
        raise ArgsInvalid(
            f"args.{key} must be an absolute path, got {value!r}. "
            "Nothing was filed."
        )
    _assert_not_denied(key, value, value)
    resolved = _lexical_resolve(value)
    if not _inside_roots(resolved):
        raise ArgsInvalid(
            f"args.{key} ({value}) is outside every compiled root "
            f"{list(ALLOWED_ROOTS)}. Widening a root is a code change and a "
            "re-sign on the Mac, not a chat command. Nothing was filed."
        )
    _assert_not_denied(key, value, resolved)


def _check_pattern(key: str, value: Any) -> None:
    """Mirror of Canonical.resolvePattern minus the filesystem."""
    if not isinstance(value, str):
        raise ArgsInvalid(
            f"args.{key} must be a string glob pattern, got "
            f"{type(value).__name__}. Nothing was filed."
        )
    if not value.startswith("/"):
        raise ArgsInvalid(
            f"args.{key} must be an absolute glob pattern, got {value!r}. "
            "Nothing was filed."
        )
    if ".." in value:
        raise ArgsInvalid(
            f"args.{key} ({value}) is refused: it contains '..'. A pattern "
            "names a SET of files, so no wildcard may climb out of its "
            "literal prefix. Nothing was filed."
        )
    _assert_not_denied(key, value, value)
    literal = ""
    for ch in value:
        if ch in "*?[]{}":
            break
        literal += ch
    slash = literal.rfind("/")
    literal = literal[:slash + 1] if slash >= 0 else ""
    if not literal.startswith("/") or len(literal) <= 1:
        raise ArgsInvalid(
            f"args.{key} ({value}) is refused: it has no literal directory "
            "prefix, so it would match across the whole filesystem. "
            "Nothing was filed."
        )
    prefix = _lexical_resolve(literal)
    if not _inside_roots(prefix):
        raise ArgsInvalid(
            f"args.{key} ({value}): its literal prefix {literal} is outside "
            f"every compiled root {list(ALLOWED_ROOTS)}. Nothing was filed."
        )


def _broker_measured_bytes(args: dict[str, Any]) -> int:
    """The size the BROKER checks against maxArgBytes: Foundation's
    JSONSerialization with .sortedKeys, which is compact, keeps
    non-ASCII as UTF-8 and escapes '/' as '\\/'. ACE-1 bytes are a
    different, smaller number; measuring those let an intent pass here
    and be refused as tooLarge on the Mac."""
    text = json.dumps(args, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).replace("/", "\\/")
    return len(text.encode("utf-8"))


def validate_args(spec: VerbSpec, args: dict[str, Any]) -> None:
    """Refuse what the broker would refuse, naming the path.

    Mirrors the canonicaliser's rules (Canonicalizer.swift) and ACE-1
    (astra/broker/ace.py): forbidden key names, the closed key set,
    required keys, scalar types only (str, bool, int; never None, a
    list, a dict or a float), non-negative integers for intKeys with
    the executor's per-key ceilings, int64 bounds, no NUL, absolute
    paths inside the compiled roots with no denied component, glob
    patterns with a rooted literal prefix and no '..', and the verb's
    size cap measured the way the broker measures it. None of this is
    trusted by the Mac; it exists so the refusal is readable and costs
    no round trip.
    """
    if not isinstance(args, dict):
        raise ArgsInvalid("args must be an object")
    for k in args:
        if not isinstance(k, str):
            raise ArgsInvalid(f"argument key {k!r} at args is not a string")
        if k.lower() in FORBIDDEN_ARG_KEYS:
            raise ArgsInvalid(
                f"args.{k} is a forbidden key: it names something only "
                "the broker decides. Remove it; nothing was filed."
            )
        if k not in spec.arg_keys:
            raise ArgsInvalid(
                f"args.{k} is not an argument of {spec.name}; it accepts "
                f"{sorted(spec.arg_keys) or 'no arguments'}. An unknown "
                "key is refused, never ignored. Nothing was filed."
            )
    missing = sorted(spec.required_keys - set(args))
    if missing:
        raise ArgsInvalid(
            f"{spec.name} requires {missing}; missing from args. "
            "Nothing was filed."
        )
    for k, v in args.items():
        # Canonical.scalar(): String, Bool or an integral NSNumber.
        # null, arrays and objects survive JSON and ACE but are badType
        # on the Mac, so they are refused here with the key named.
        if isinstance(v, float):
            raise ArgsInvalid(
                f"args.{k} is a float ({v!r}). One JSON number has several "
                "exact values, so the broker and the executor could sign "
                "and execute different intents. Send an integer or a "
                "decimal string. Nothing was filed."
            )
        if not isinstance(v, (str, bool, int)):
            raise ArgsInvalid(
                f"args.{k} has unusable type {type(v).__name__}; every "
                "argument is a string, a boolean or an integer. "
                "Nothing was filed."
            )
        if isinstance(v, int) and not isinstance(v, bool) and (
            v < ace.INT64_MIN or v > ace.INT64_MAX
        ):
            raise ArgsInvalid(f"args.{k} is outside int64. Nothing was filed.")
        if isinstance(v, str) and "\x00" in v:
            raise ArgsInvalid(
                f"args.{k} contains a NUL character (U+0000), which neither "
                "the canonical encoding nor Postgres JSONB can hold. "
                "Nothing was filed."
            )
    for k in spec.enum_keys & set(args):
        v = args[k]
        # Exactly Canonicalizer.swift's rule: a bare identifier, at most
        # 32 characters, lowercase letters, digits and underscore. The
        # executor still checks the name against its own table; this is
        # the SHAPE check, and it exists so a path-shaped value can
        # never reach a signature.
        if (not isinstance(v, str) or not v or len(v) > 32
                or not all(c.islower() and c.isascii() or c.isdigit() or c == "_"
                           for c in v)):
            raise ArgsInvalid(
                f"args.{k} must be one of a closed set of names (lowercase "
                f"letters, digits and _, at most 32) for {spec.name}, got "
                f"{v!r}. It names a capability; the path it stands for is "
                "compiled into the body and cannot be chosen here. "
                "Nothing was filed."
            )
    for k in spec.blob_keys & set(args):
        v = args[k]
        # A PAYLOAD is a string. `content: 5` would otherwise be
        # accepted here, refused on the Mac as badType, and cost a
        # round trip; worse, a validator that coerced it would decide
        # what bytes reach the disk three modules away from the write.
        if not isinstance(v, str):
            raise ArgsInvalid(
                f"args.{k} must be a string for {spec.name}, got "
                f"{type(v).__name__}. It is a PAYLOAD: those bytes are "
                "written to the file, so they are taken exactly as sent "
                "or not at all. Nothing was filed."
            )
        # ...and it must already be NFC. ACE-1 hashes strings as NFC
        # UTF-8 (astra/broker/ace.py and ACE.swift alike), so a
        # decomposed payload would be SIGNED as its composed form: the
        # bytes on disk would not be the bytes sent, and a later
        # fs.edit against your own copy would find zero occurrences
        # with no explanation. The Mac refuses it (Canonicalizer's
        # notNormalised); this refuses it first, with the key named.
        if unicodedata.normalize("NFC", v) != v:
            raise ArgsInvalid(
                f"args.{k} is a payload that is not Unicode-normalised "
                f"({len(v.encode('utf-8'))} bytes; "
                f"{len(unicodedata.normalize('NFC', v).encode('utf-8'))} as "
                "NFC). It is refused rather than normalised for you: a "
                "payload is signed, shown and written as its NFC bytes, so "
                "composing it here would write something other than what "
                "you sent. Send NFC. Nothing was filed."
            )
    for k in spec.int_keys & set(args):
        v = args[k]
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ArgsInvalid(
                f"args.{k} must be a non-negative integer for "
                f"{spec.name}, got {v!r} ({type(v).__name__}). "
                "Nothing was filed."
            )
    # UNCLASSIFIED MEANS STRING (Canonicalizer.swift's notAString).
    #
    # Every argument the catalogue does not classify as a path, a
    # pattern, an integer, an enum or a payload is a string whose exact
    # text is the thing being approved: exec.shell's `command`,
    # web.screenshot's `url`, fs.grep's `pattern` and `include`.
    # Without this, `command: 5` was accepted here AND canonicalised on
    # the Mac, rendered on the sheet as "command: 5", cost TWO
    # fingerprints, and then failed at dispatch as "missing command" —
    # the untyped-argument bug that already cost fs.read's `limit`, on
    # the one verb where the wasted taps are the scarce resource.
    _typed = (spec.path_keys | spec.pattern_keys | spec.int_keys
              | spec.enum_keys | spec.blob_keys)
    for k in sorted(set(args) - _typed):
        if not isinstance(args[k], str):
            raise ArgsInvalid(
                f"args.{k} must be a string for {spec.name}, got "
                f"{args[k]!r} ({type(args[k]).__name__}). An argument the "
                "catalogue does not type as an integer, an enum or a "
                "payload is a string whose exact text is what Kunal "
                "approves. Nothing was filed."
            )
    for k in spec.path_keys & set(args):
        _check_path(k, args[k])
    for k in spec.pattern_keys & set(args):
        _check_pattern(k, args[k])
    try:
        ace.encode(args)
    except ace.NotCanonical as e:
        raise ArgsInvalid(f"args are not canonically encodable: {e}. "
                          "Nothing was filed.") from e
    measured = _broker_measured_bytes(args)
    if measured > spec.max_arg_bytes:
        raise ArgsInvalid(
            f"args for {spec.name} are {measured} bytes as the broker "
            f"measures them, over its {spec.max_arg_bytes}-byte cap. Nothing "
            "is truncated, ever; split the work. Nothing was filed."
        )


def precheck_args(spec: VerbSpec, args: dict[str, Any]) -> None:
    """The preconditions this side can decide from the arguments alone.

    A SECOND layer, deliberately not folded into `validate_args`, and
    the split is the point. `validate_args` mirrors the CANONICALISER
    (Canonicalizer.swift) and is pinned against it by one shared corpus
    of boundary values — the file that exists because the two
    implementations of that one rule disagreed about `0` for months.
    A rule that belongs to a different Mac-side layer cannot be folded
    into that function without making the corpus unable to express the
    truth: `timeout_ms: 0` IS canonical (it is an integer, and reading
    it as a boolean was the bug the corpus pins), and it is REFUSED by
    `Precondition.check`, which runs after canonicalisation.

    So this mirrors `Precondition.check`'s intBounds loop instead. Same
    verdict, same reason, one round trip earlier. On the Mac an
    out-of-range integer is refused BEFORE the sheet is composed and
    before the budget; refusing it here as well costs the model nothing
    and saves one of the ten signed slots an hour.

    Ranges only, today. The rest of `Precondition.check` asks the
    filesystem (does `old` occur exactly once, is the parent a
    directory) and the answers are the Mac's to give — guessing at them
    from here would be a second source of truth for a fact that changes
    under us.
    """
    for k, lo, hi in spec.int_bounds:
        v = args.get(k)
        # `is not True/False`: bool is a subclass of int, and a JSON
        # boolean is refused by validate_args as a type error — not
        # silently range-checked as 0 or 1 here.
        if isinstance(v, int) and not isinstance(v, bool) and not lo <= v <= hi:
            raise PreconditionFailed(
                f"args.{k} = {v} is outside the {lo}…{hi} range {spec.name} "
                "declares. The Mac refuses it rather than adjusting it: the "
                "value you ask for is the value Kunal would read on the Touch "
                "ID prompt and sign, so a number the body will not honour is "
                "never shown. Nothing was filed."
            )


# ── (c) caller class ──────────────────────────────────────


def _turn_claim() -> str:
    """'turn:<id>' inside a turn, '' outside. Set by run_lean_turn
    (astra/autonomy/turn_context.py::current_turn). Least privilege
    by default: an unset context means "not a turn"."""
    from astra.autonomy.turn_context import current_turn
    return current_turn.get() or ""


# ── The body ──────────────────────────────────────────────


class _BodyRefusal(str):
    """A refusal sentence that also carries its code. It is a str so
    `_only_body()` keeps its (id, reason) shape for callers and test
    stubs; `run_intent` reads `.code` and never the words."""
    code: str

    def __new__(cls, text: str, code: str) -> "_BodyRefusal":
        o = str.__new__(cls, text)
        o.code = code
        return o


async def _only_body() -> tuple[int | None, str]:
    """The single registered body, or a reason there isn't one.

    Refuses with an honest message rather than filing intents nothing
    will ever claim (CHARTER §8: nothing may silently no-op). The
    reason is a `_BodyRefusal` whose `.code` is 'no_body' or
    'ambiguous_body'; a plain str (a test stub) counts as 'no_body'.
    """
    from sqlalchemy import text
    from astra.db import engine as _engine
    async with _engine.async_session() as s:
        rows = (await s.execute(text(
            "SELECT id, label FROM bodies WHERE revoked_at IS NULL "
            "ORDER BY id LIMIT 2"
        ))).fetchall()
    if not rows:
        return None, _BodyRefusal((
            "No body is registered, so there is nothing to carry this out. "
            "The capability broker's transport exists but no Mac has "
            "enrolled with it yet. Tell Kunal what you could not do and "
            "that it needs the broker enrolled; do not retry."
        ), "no_body")
    if len(rows) > 1:
        return None, _BodyRefusal((
            "More than one body is registered and this client cannot yet "
            "choose between them. Tell Kunal."
        ), "ambiguous_body")
    return int(rows[0][0]), ""


def _offline_message(live: store.BodyLiveness | None) -> str:
    age = (
        "has never polled" if live is None or live.poll_age_sec is None
        else f"last polled {int(live.poll_age_sec)} s ago"
    )
    return (
        f"body offline (laptop closed is normal): Kunal's Mac {age}, "
        f"outside the {BODY_POLL_WINDOW_SEC} s window, so nothing would "
        "claim this. Nothing was filed. Do not volunteer that the Mac "
        "is offline; explain it at the point of need, and offer to "
        "retry later or add_task it tagged 'body'."
    )


def _busy_message(live: store.BodyLiveness | None,
                  open_rows: list[dict[str, Any]]) -> str:
    """The body stopped polling because the broker is INSIDE an intent,
    not because the laptop is closed. Names the intent so the remedy
    is the true one (wait for that intent, or Kunal's fingerprint),
    never 'open the laptop'."""
    first = open_rows[0]
    status = str(first.get("status") or "")
    what = {
        "awaiting_human": "waiting on Kunal's fingerprint at the Touch ID prompt",
        "running": "still executing",
        "claimed": "claimed by the broker and not yet finished",
    }.get(status, status or "open")
    age = ""
    if first.get("claimed_age_sec") is not None:
        age = f" for {int(first['claimed_age_sec'])} s"
    more = f" (and {len(open_rows) - 1} more open)" if len(open_rows) > 1 else ""
    polled = (
        f"last polled {int(live.poll_age_sec)} s ago"
        if live is not None and live.poll_age_sec is not None
        else "has not polled since"
    )
    return (
        f"body busy: Kunal's Mac is up but occupied with intent #{first['id']} "
        f"({first.get('verb')}, {status}: {what}{age}){more}. The broker "
        f"serves one intent at a time and does not poll while inside one "
        f"({polled}), so nothing would claim this yet. Nothing was filed "
        "and Kunal was not asked. Do not say the Mac is offline or asleep: "
        f"say it is busy with #{first['id']}, and offer to try again once "
        "that resolves or to add_task it tagged 'body'."
    )


# ── Receipts ──────────────────────────────────────────────

_unverified_logged = False


# Receipt.swift's layout, mirrored: 194 bytes, of which [0:130] is the
# signed prefix — header(13) + payload_hash(32) + challenge(32) +
# OUTCOME(1) + started(8) + finished(8) + result_digest(32) +
# result_len(4). The outcome is therefore byte 77. Pinned against
# Token.swift by tests/test_broker/test_write_verbs.py, because an
# offset read from the wrong byte would report "ok" for a partial
# write and nothing would look wrong.
_RECEIPT_SIZE = 194
_RECEIPT_OUTCOME_OFFSET = 77
_RECEIPT_OUTCOMES: dict[int, str] = {
    0: "ok", 1: "refused", 2: "error", 3: "partial",
}


def receipt_outcome(row: dict[str, Any]) -> str:
    """The outcome the executor SIGNED, or "" when there is none.

    Read from the receipt rather than from a column, for the same
    reason `verify_receipt` recomputes its verdict: the brain is a
    Postgres superuser, so any status the database merely stores is a
    value it can write about itself. This byte is inside the bytes the
    executor's key covers.
    """
    rb = row.get("receipt_bytes")
    if not rb:
        return ""
    rb = bytes(rb)
    if len(rb) != _RECEIPT_SIZE:
        return ""
    return _RECEIPT_OUTCOMES.get(rb[_RECEIPT_OUTCOME_OFFSET], "")


def verify_receipt(row: dict[str, Any]) -> str:
    """Verify the executor's signature over the receipt, here, now.

    Returns a human-readable verdict. Says UNVERIFIED plainly when it
    cannot check: a verdict it cannot compute must never be reported as
    a pass, which is the whole reason there is no stored boolean (the
    brain is a Postgres superuser; a `receipt_verified` column would be
    a value it writes about itself).

    Receipt layout (Receipt.swift, 194 bytes): [0:130] signed payload,
    of which [94:126] is SHA-256 of the result bytes; [130:194] the
    Ed25519 signature.
    """
    global _unverified_logged
    rb = row.get("receipt_bytes")
    if not rb:
        return "none present"
    rb = bytes(rb)
    if len(rb) != 194:
        return f"MALFORMED ({len(rb)} bytes, expected 194)"
    key_hex = (settings.executor_pubkey_hex or "").strip()
    if not key_hex:
        if not _unverified_logged:
            logger.warning(
                "[broker] EXECUTOR_PUBKEY_HEX is empty: receipts are "
                "reported UNVERIFIED (logged once)"
            )
            _unverified_logged = True
        return (
            "UNVERIFIED: no executor public key is pinned in this build "
            "(EXECUTOR_PUBKEY_HEX is empty), so the signature cannot be "
            "checked. Do not treat the result as attested."
        )
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(key_hex))
        pub.verify(rb[130:194], rb[0:130])
    except Exception:
        return "SIGNATURE DID NOT VERIFY: treat this result as forged"

    result = row.get("result_bytes") or b""
    digest = hashlib.sha256(bytes(result)).digest()
    if digest != rb[94:126]:
        return (
            "signature valid but the RESULT DOES NOT MATCH its signed "
            "digest: treat the result as forged"
        )
    return "verified"


# How much of an executor error text becomes the deny_reason of a
# failed row. Same cut the broker applies to a refusal (ServeLoop.swift
# `result.prefix(400)`).
_FAILED_REASON_CHARS = 400


def _from_row(row: dict[str, Any]) -> IntentResult:
    display = ""
    if row.get("display_bytes"):
        try:
            display = bytes(row["display_bytes"]).decode("utf-8").rstrip()
        except Exception:
            display = "(display bytes present but not decodable)"
    st = row["status"]
    deny = row.get("deny_reason") or None
    result = bytes(row["result_bytes"]) if row.get("result_bytes") else None
    if deny is None and st in ("failed", "denied") and result:
        # ServeLoop.swift records an executor `.error` as status
        # 'failed' with denyReason NULL and the executor's message as
        # the result bytes ('.refused' already copies result.prefix(400)
        # into denyReason). Without this the caller reads '(none
        # given)' while the actual error ('not a regular file', 'limit
        # exceeds the ceiling', 'exceeded max_runtime_ms') sits in a
        # column nobody prints. The bytes are the executor's text by
        # construction; a non-UTF-8 result is named as such.
        try:
            deny = result.decode("utf-8")[:_FAILED_REASON_CHARS].strip() or None
        except UnicodeDecodeError:
            deny = f"executor error ({len(result)} bytes, not text)"
    return IntentResult(
        intent_id=int(row["id"]), status=st, verb=row.get("verb") or "",
        result_bytes=result, deny_reason=deny,
        receipt_verdict=verify_receipt(row) if st == "succeeded" else "",
        display=display, result_note=row.get("result_note") or "",
        receipt_outcome=receipt_outcome(row),
    )


# ── The chokepoint ────────────────────────────────────────


async def run_intent(
    verb: str, args: dict[str, Any], *, why: str, actor: str,
    wait_sec: float = 0, session_claim: str | None = None,
) -> IntentResult:
    """File one physical intent and, if asked, watch it.

    Args:
        verb: a catalogue verb name.
        args: the verb's arguments, validated here before anything is
            filed (see validate_args).
        why: one line, shown to Kunal on the approval prompt.
        actor: KEYWORD-ONLY and never a tool argument. Names the code
            path filing this ("chat", "notes_sync", "body_smoke");
            becomes the session claim `job:<actor>` outside a turn.
        wait_sec: 0 files and returns at once (the model's tool does
            this; it learns the outcome through poll_status or the
            next turn's resolved-intents block). Positive waits up to
            that long for a terminal status WITHOUT touching the row.
        session_claim: override the claim written to the row. Normally
            derived: `turn:<id>` inside a turn, `job:<actor>` outside.

    Refuses BEFORE any database call, in this order: unknown verb
    ('unknown_verb'); arguments the broker would refuse ('args', named
    path); a signed verb from outside a turn ('signed_outside_turn',
    the habituation rule, see the module docstring); a verb the
    executor cannot perform yet ('unwired', so no Touch ID prompt is
    ever raised for it; decided by wired_state: the broker's published
    flag when this process has it, else the mirror). Then, with the
    database: no or ambiguous body
    ('no_body' / 'ambiguous_body'); body not polling within
    BODY_POLL_WINDOW_SEC, which is 'busy' when the broker holds an
    open intent (store.open_intents: it stops polling while inside
    handle(), so a Touch ID prompt on screen would otherwise read as a
    closed laptop) and 'offline' otherwise (refused in one round trip,
    never a queued wait); queue already at MAX_PENDING ('queue_full').

    Every refusal returns status='refused', `refusal_code` from
    REFUSAL_CODES, `deny_reason` in words, and no row. Nothing here can
    approve anything.
    """
    verb = (verb or "").strip()
    why = (why or "").strip()
    if not verb:
        return _refused(verb, "args", "verb is required")
    if not why:
        return _refused(
            verb, "args",
            "why is required: it is the line Kunal reads when deciding",
        )
    if display_width(why) > WHY_MAX_CHARS:
        # ONE LINE. `why` has a fixed reservation on the sheet so it
        # can never take space from the payload preview Kunal has to
        # read; over that reservation the Mac refuses the whole intent
        # as `unrenderable`, which sounds like the arguments are too
        # big. Say the real thing here, before anything is filed.
        return _refused(verb, "args", (
            f"why is {display_width(why)} characters as the approval sheet "
            f"counts them (non-ASCII is escaped and costs up to 10 each) "
            f"and the sheet reserves {WHY_MAX_CHARS}. It is one line Kunal "
            "reads while deciding, not the place for the reasoning — put "
            "that in the conversation. Nothing was filed."
        ))
    if not actor or not isinstance(actor, str):
        raise TypeError("run_intent: actor must be a non-empty string")

    spec = CATALOGUE_BY_NAME.get(verb)
    if spec is None:
        return _refused(verb, "unknown_verb", (
            f"{verb!r} is not a catalogue verb. The catalogue is compiled "
            f"into the Mac binaries and closed: {sorted(CATALOGUE_BY_NAME)}. "
            "Nothing was filed."
        ))

    try:
        validate_args(spec, args)
    except ArgsInvalid as e:
        return _refused(verb, "args", str(e))

    # The second layer, in the same order the Mac runs it: canonicalise,
    # then precondition. Cheap (it reads the arguments, nothing else)
    # and it runs for every verb, not only the three the Mac asks its
    # body about — a range is a fact about the arguments, so it needs no
    # round trip to decide.
    try:
        precheck_args(spec, args)
    except PreconditionFailed as e:
        return _refused(verb, "precondition_failed", str(e))

    claim = session_claim if session_claim is not None else _turn_claim()
    in_turn = bool(claim) and claim.startswith("turn:")
    if spec.signed and not in_turn:
        return _refused(verb, "signed_outside_turn", (
            f"{verb} needs Kunal's fingerprint and this call is not inside "
            f"a turn (caller {actor!r}). Scheduler jobs and background "
            "code may file only auto verbs "
            f"({', '.join(sorted(AUTO_VERBS))}), so no job can ever raise "
            "a Touch ID prompt. Nothing was filed and Kunal was not asked."
        ))

    is_wired, source = wired_state(verb)
    if not is_wired:
        said_by = (
            "the broker's last publish to this process says the executor "
            "on his Mac does not handle it in this build"
            if source == "published" else
            "the executor on his Mac does not handle it in this build"
        )
        return _refused(verb, "unwired", (
            f"the body cannot perform {verb} yet; nothing was filed and "
            f"Kunal was not asked. The verb is in the catalogue but {said_by}. "
            "Say so plainly, and offer to add_task it tagged 'body' if it "
            "matters."
        ))

    if not claim:
        claim = f"job:{actor}"
    claim = claim[:64]

    body_id, reason = await _only_body()
    if body_id is None:
        return _refused(verb, getattr(reason, "code", "no_body"), str(reason))

    live = await store.body_liveness(body_id)
    if (live is None or live.revoked or live.poll_age_sec is None
            or live.poll_age_sec > BODY_POLL_WINDOW_SEC):
        # Not polling. Before calling that a closed laptop, ask
        # whether the broker is simply inside an intent: its serve
        # loop is single-threaded and touches last_poll_at only
        # between intents. A failed read here cannot tell busy from
        # closed, so the coarser truth (offline) stands and the
        # failure is logged; nothing is ever filed on this path.
        open_rows: list[dict[str, Any]] = []
        if live is not None and not live.revoked:
            try:
                open_rows = await store.open_intents(body_id)
            except Exception as e:  # noqa: BLE001 - refinement only
                logger.warning(
                    "[broker] open_intents read failed; refusing as "
                    "offline rather than busy: %s", e,
                )
        if open_rows:
            return _refused(verb, "busy", _busy_message(live, open_rows))
        return _refused(verb, "offline", _offline_message(live))

    depth = await store.pending_depth(body_id)
    if depth >= MAX_PENDING:
        return _refused(verb, "queue_full", (
            f"{depth} intents are already pending on Kunal's Mac, which is "
            "more than he could plausibly approve. Nothing was filed. "
            "Tell him the queue is backed up rather than adding to it."
        ))

    ttl = TTL_SIGNED_SEC if spec.signed else TTL_AUTO_SEC
    try:
        intent_id = await store.submit_intent(
            body_id=body_id, verb=verb, args=args, why=why,
            ttl_seconds=ttl, session_claim=claim,
        )
    except store.ArgsRejected as e:
        return _refused(verb, "args", str(e))
    logger.info("[broker] filed intent #%s %s by %s (%s)",
                intent_id, verb, actor, claim)

    if wait_sec and wait_sec > 0:
        row = await store.wait_for_intent(intent_id, timeout_sec=wait_sec)
        if row is None:
            row = await store.get_intent_status(intent_id)
        if row is not None:
            return _from_row(row)

    return IntentResult(intent_id=intent_id, status="pending", verb=verb)


async def status(intent_id: int) -> IntentResult | None:
    """One read of an intent, with the receipt verdict RECOMPUTED for a
    succeeded one. None if there is no such intent. Reading causes
    nothing."""
    row = await store.get_intent_status(int(intent_id))
    if row is None:
        return None
    return _from_row(row)


# Boot: say once, loudly, that receipts cannot be verified. Deferred
# test: refuse to boot with an enrolled body and no key.
if not (settings.executor_pubkey_hex or "").strip():
    logger.warning(
        "[broker] EXECUTOR_PUBKEY_HEX is not set: every executor receipt "
        "will be reported UNVERIFIED. Set it to the `pub=` value "
        "AstraExecutor enrol printed on the Mac."
    )
