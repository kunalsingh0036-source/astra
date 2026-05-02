"""
MCP tools that let Astra self-correct brand kits.

This is Layer 1 of the self-modification stack: kit data only,
not Astra's code. Each tool maps to a common "Astra, change X"
request shape. All edits are scoped to business-kits/<slug>/ —
never touch code.

Tools:
  add_forbidden_phrase     — coordinated brand.yml + voice.md edit
  add_voice_note           — append to voice.md (does/never/sample)
  add_proof_point          — append to content/proof-points.md section
  add_audience_objection   — append objection+response to audience persona
  commit_kit_changes       — manual commit when auto_commit was disabled

Each edit tool auto-commits by default so kit state is durable
across Railway redeploys (Astra container filesystem is ephemeral;
git is the persistence layer).
"""

from __future__ import annotations

from claude_agent_sdk import tool

from astra.creators.edit_kit import (
    add_audience_objection,
    add_forbidden_phrase,
    add_proof_point,
    add_voice_note,
    commit_kit,
)


def _format_commit_summary(commit_result: dict | None) -> str:
    if not commit_result:
        return "  (auto_commit=False — call commit_kit_changes when ready)"
    status = commit_result.get("status")
    if status == "no_changes":
        return "  Commit: no changes detected"
    if status == "git_error":
        return f"  Commit: GIT ERROR — {commit_result.get('stderr','')[:200]}"
    parts = [
        f"  Commit: {commit_result.get('commit_hash','?')}"
        + (" (pushed)" if commit_result.get("pushed") else " (LOCAL ONLY)")
    ]
    if commit_result.get("push_error"):
        parts.append(f"    push error: {commit_result['push_error'][:200]}")
    return "\n".join(parts)


@tool(
    "add_forbidden_phrase",
    "Add a phrase to a kit's forbidden list. Coordinates two edits in "
    "one call: appends to brand.yml's forbidden_phrases (machine-enforced "
    "post-draft scan) AND appends to the 'NEVER uses' section of voice.md "
    "(so the model sees it in prompt context). Idempotent — if the phrase "
    "is already present, it's not duplicated. Auto-commits + pushes by "
    "default; the change is live on the next request.",
    {
        "business": str,         # kit slug
        "phrase": str,           # the phrase to ban
        "rationale": str,        # optional one-line reason (saved as a note in voice.md)
        "auto_commit": bool,     # default True
    },
)
async def add_forbidden_phrase_tool(args: dict) -> dict:
    business = (args.get("business") or "").strip()
    phrase = (args.get("phrase") or "").strip()
    rationale = (args.get("rationale") or "").strip()
    auto_commit = args.get("auto_commit", True)
    if not (business and phrase):
        return {"content": [{"type": "text", "text": (
            "add_forbidden_phrase requires: business, phrase"
        )}]}
    try:
        result = add_forbidden_phrase(
            business, phrase, rationale=rationale, auto_commit=auto_commit,
        )
    except FileNotFoundError as e:
        return {"content": [{"type": "text", "text": str(e)}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Edit failed: {type(e).__name__}: {e}"}]}

    if result["status"] == "already_present":
        return {"content": [{"type": "text", "text": (
            f"Phrase {phrase!r} already in {business} kit's forbidden list. No change."
        )}]}
    actions = result.get("actions", [])
    text = (
        f"Added forbidden phrase to {business} kit\n"
        f"  Phrase: {phrase!r}\n"
        + (f"  Rationale: {rationale}\n" if rationale else "")
        + f"  Files updated: {', '.join(actions)}\n"
        + _format_commit_summary(result.get("commit"))
    )
    return {"content": [{"type": "text", "text": text}]}


@tool(
    "add_voice_note",
    "Append a note to a kit's voice.md. Three kinds: "
    "'does' = words/phrases the kit DOES use (positive guidance), "
    "'never' = words/phrases the kit avoids (advisory; for hard "
    "machine-enforced bans use add_forbidden_phrase instead), "
    "'sample' = a verbatim voice sample (quoted block with attribution). "
    "Auto-commits + pushes by default.",
    {
        "business": str,
        "kind": str,             # does | never | sample
        "content": str,          # the phrase OR the sample text
        "context": str,          # optional — short rationale OR attribution for samples
        "auto_commit": bool,
    },
)
async def add_voice_note_tool(args: dict) -> dict:
    business = (args.get("business") or "").strip()
    kind = (args.get("kind") or "").strip().lower()
    content = (args.get("content") or "").strip()
    context = (args.get("context") or "").strip()
    auto_commit = args.get("auto_commit", True)
    if not (business and kind and content):
        return {"content": [{"type": "text", "text": (
            "add_voice_note requires: business, kind (does|never|sample), content"
        )}]}
    try:
        result = add_voice_note(
            business, kind=kind, content=content, context=context,
            auto_commit=auto_commit,
        )
    except (FileNotFoundError, ValueError) as e:
        return {"content": [{"type": "text", "text": str(e)}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Edit failed: {type(e).__name__}: {e}"}]}

    if result["status"] == "section_not_found":
        avail = result.get("available_sections", [])
        return {"content": [{"type": "text", "text": (
            f"Could not find target section in {business} voice.md. "
            f"Available level-2 sections: {avail}"
        )}]}
    text = (
        f"Added voice note to {business} kit\n"
        f"  Kind: {kind}\n"
        f"  Content: {content[:120]!r}\n"
        + (f"  Context: {context}\n" if context else "")
        + f"  Section update: {result.get('status','?')}\n"
        + _format_commit_summary(result.get("commit"))
    )
    return {"content": [{"type": "text", "text": text}]}


@tool(
    "add_proof_point",
    "Append a proof point to a kit's content/proof-points.md, into the "
    "specified section. Common section hints: 'customers', 'traction', "
    "'team', 'press', 'awards', 'testimonials', 'capabilities', "
    "'competitive_positioning'. Content can be a bullet (starts with '-') "
    "or a paragraph. Auto-commits + pushes by default.",
    {
        "business": str,
        "section": str,         # short hint
        "content": str,         # bullet or paragraph
        "auto_commit": bool,
    },
)
async def add_proof_point_tool(args: dict) -> dict:
    business = (args.get("business") or "").strip()
    section = (args.get("section") or "").strip()
    content = (args.get("content") or "").strip()
    auto_commit = args.get("auto_commit", True)
    if not (business and section and content):
        return {"content": [{"type": "text", "text": (
            "add_proof_point requires: business, section, content"
        )}]}
    try:
        result = add_proof_point(
            business, section=section, content=content,
            auto_commit=auto_commit,
        )
    except FileNotFoundError as e:
        return {"content": [{"type": "text", "text": str(e)}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Edit failed: {type(e).__name__}: {e}"}]}

    if result["status"] == "section_not_found":
        avail = result.get("available_sections", [])
        return {"content": [{"type": "text", "text": (
            f"Section {section!r} not found in {business} proof-points.md. "
            f"Available level-2 sections: {avail}"
        )}]}
    text = (
        f"Added proof point to {business} kit\n"
        f"  Section: {section}\n"
        f"  Content: {content[:160]!r}\n"
        f"  Status: {result.get('status','?')}\n"
        + _format_commit_summary(result.get("commit"))
    )
    return {"content": [{"type": "text", "text": text}]}


@tool(
    "add_audience_objection",
    "Append an objection + response pair to an audience persona file under "
    "audiences/<audience>.md. Uses the kit's standard objection format. "
    "If the persona doesn't have a 'Common objections' section, one is "
    "created at the end of the file. Auto-commits + pushes by default.",
    {
        "business": str,
        "audience": str,        # persona slug — e.g. 'peak-xv-partner'
        "objection": str,
        "response": str,
        "auto_commit": bool,
    },
)
async def add_audience_objection_tool(args: dict) -> dict:
    business = (args.get("business") or "").strip()
    audience = (args.get("audience") or "").strip()
    objection = (args.get("objection") or "").strip()
    response = (args.get("response") or "").strip()
    auto_commit = args.get("auto_commit", True)
    if not (business and audience and objection and response):
        return {"content": [{"type": "text", "text": (
            "add_audience_objection requires: business, audience, "
            "objection, response"
        )}]}
    try:
        result = add_audience_objection(
            business, audience=audience, objection=objection,
            response=response, auto_commit=auto_commit,
        )
    except FileNotFoundError as e:
        return {"content": [{"type": "text", "text": str(e)}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Edit failed: {type(e).__name__}: {e}"}]}

    if result["status"] == "audience_not_found":
        avail = result.get("available_audiences", [])
        return {"content": [{"type": "text", "text": (
            f"Audience persona {audience!r} not found in {business} kit. "
            f"Available: {avail}"
        )}]}
    text = (
        f"Added objection+response to {business}/{audience}\n"
        f"  Objection: {objection[:120]!r}\n"
        f"  Response:  {response[:140]!r}\n"
        f"  Status: {result.get('status','?')}\n"
        + _format_commit_summary(result.get("commit"))
    )
    return {"content": [{"type": "text", "text": text}]}


@tool(
    "commit_kit_changes",
    "Manually commit + push pending changes for a kit (use when individual "
    "edit tools were called with auto_commit=False, OR when external file "
    "edits were made). Scoped to business-kits/<slug>/ — will NOT commit "
    "any other working-tree changes (no risk of accidentally committing "
    "code edits via this tool).",
    {
        "business": str,
        "message": str,         # optional — auto-generated if blank
        "push": bool,           # default True
    },
)
async def commit_kit_changes_tool(args: dict) -> dict:
    business = (args.get("business") or "").strip()
    message = (args.get("message") or "").strip() or None
    push = args.get("push", True)
    if not business:
        return {"content": [{"type": "text", "text": "commit_kit_changes requires: business"}]}
    try:
        result = commit_kit(business, message=message, push=push)
    except FileNotFoundError as e:
        return {"content": [{"type": "text", "text": str(e)}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Commit failed: {type(e).__name__}: {e}"}]}

    if result["status"] == "no_changes":
        return {"content": [{"type": "text", "text": (
            f"No pending changes in business-kits/{business}/"
        )}]}
    if result["status"] == "git_error":
        return {"content": [{"type": "text", "text": (
            f"Git error: {result.get('stderr','')[:300]}"
        )}]}
    files = result.get("files_changed", []) or []
    text = (
        f"Committed {business} kit changes\n"
        f"  Hash: {result.get('commit_hash','?')}\n"
        f"  Files ({len(files)}): {files[:8]}{'...' if len(files) > 8 else ''}\n"
        f"  Pushed: {result.get('pushed', False)}\n"
        + (f"  Push error: {result.get('push_error','')[:200]}\n"
           if result.get("push_error") else "")
    )
    return {"content": [{"type": "text", "text": text}]}


KIT_EDITOR_TOOLS = [
    add_forbidden_phrase_tool,
    add_voice_note_tool,
    add_proof_point_tool,
    add_audience_objection_tool,
    commit_kit_changes_tool,
]
