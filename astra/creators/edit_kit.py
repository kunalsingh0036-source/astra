"""
Brand-kit edit primitives — let Astra self-correct kits via tool calls.

Layer 1 of self-modification (the safest layer):
- Pure data edits (YAML + markdown), no code
- Scoped to business-kits/<slug>/ — bounded blast radius
- Atomic-by-default: each edit can auto-commit so half-committed
  kit state is impossible across redeploys
- Idempotent where possible (don't duplicate forbidden phrases)
- Section-aware markdown editing (find heading → append to that
  section's bullets, don't blindly append to file)

Why this is the highest-value layer first: ~80% of "Astra, change X"
requests are kit-level, not code-level. Kit changes don't risk the
runtime, don't need test suites, can't break production.

What we do NOT do here: code edits (Layer 2 — needs test suite as
prerequisite).
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from astra.creators.kits import _kits_root  # noqa: PLC2701 — internal helper, intentional reuse

logger = logging.getLogger(__name__)


# ── Path helpers ────────────────────────────────────────────────────


@dataclass
class KitPaths:
    """Resolved file paths for a single kit. Raises if slug not found."""
    slug: str
    root: Path
    brand_yml: Path
    voice_md: Path
    thesis_md: Path
    audiences_dir: Path
    proof_points_md: Path

    @classmethod
    def for_slug(cls, slug: str) -> "KitPaths":
        root_dir = _kits_root() / slug
        if not root_dir.exists() or not root_dir.is_dir():
            avail = [p.name for p in _kits_root().iterdir()
                     if p.is_dir() and not p.name.startswith("_")]
            raise FileNotFoundError(
                f"business-kit '{slug}' not found. Available: {avail}"
            )
        return cls(
            slug=slug,
            root=root_dir,
            brand_yml=root_dir / "brand.yml",
            voice_md=root_dir / "voice.md",
            thesis_md=root_dir / "thesis.md",
            audiences_dir=root_dir / "audiences",
            proof_points_md=root_dir / "content" / "proof-points.md",
        )


# ── brand.yml structured edits ──────────────────────────────────────


def load_brand_yml(paths: KitPaths) -> dict[str, Any]:
    if not paths.brand_yml.exists():
        raise FileNotFoundError(f"brand.yml missing for {paths.slug}")
    return yaml.safe_load(paths.brand_yml.read_text()) or {}


def save_brand_yml(paths: KitPaths, data: dict[str, Any]) -> None:
    """Write back, preserving the original header comments where possible.

    yaml.safe_dump strips comments — we read the original header (lines
    that start with #) and prepend them to the dumped YAML. Mid-file
    comments are lost; that's an acceptable tradeoff for now (the
    structural data is the source of truth).
    """
    original_text = paths.brand_yml.read_text() if paths.brand_yml.exists() else ""
    header_lines: list[str] = []
    for line in original_text.splitlines():
        if line.strip().startswith("#") or not line.strip():
            header_lines.append(line)
        else:
            break
    header = "\n".join(header_lines).rstrip("\n") + "\n\n" if header_lines else ""
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)
    paths.brand_yml.write_text(header + body, encoding="utf-8")


# ── Markdown section primitives ─────────────────────────────────────


# A "section" is the content under a `## Heading` line until the next
# `## Heading` line or end of file. We support both `## ` (level 2) and
# `### ` (level 3) for nested edits.

_SECTION_HEADING_RE = re.compile(r"^(#{2,3})\s+(.+?)\s*$", re.MULTILINE)


def find_section_indices(
    md: str, heading_pattern: str | re.Pattern[str], level: int = 2
) -> tuple[int, int] | None:
    """Find the [start, end) char indices of a section's BODY (not heading line).

    Args:
      md: full markdown text
      heading_pattern: regex pattern (case-insensitive) matched against the
        heading TEXT (not the # marks)
      level: 2 for ##, 3 for ###

    Returns: (body_start, body_end) or None if not found.
    body_start = position right after the heading line's trailing newline
    body_end = position of the next heading at the same level, or len(md)
    """
    if isinstance(heading_pattern, str):
        heading_pattern = re.compile(heading_pattern, re.IGNORECASE)
    target_marks = "#" * level
    same_or_higher_marks = "#" * level if level >= 2 else "##"

    body_start: int | None = None
    body_end: int | None = None

    for m in _SECTION_HEADING_RE.finditer(md):
        marks = m.group(1)
        text = m.group(2)
        # Find our target heading
        if body_start is None:
            if marks == target_marks and heading_pattern.search(text):
                body_start = m.end() + 1  # +1 to skip the trailing newline
                continue
        else:
            # We've started; look for the next same-or-higher-level heading
            # to mark the end of this section.
            if len(marks) <= len(same_or_higher_marks):
                body_end = m.start()
                break

    if body_start is None:
        return None
    if body_end is None:
        body_end = len(md)
    return (body_start, body_end)


def get_section(md: str, heading_pattern: str | re.Pattern[str], level: int = 2) -> str | None:
    indices = find_section_indices(md, heading_pattern, level)
    if indices is None:
        return None
    return md[indices[0]:indices[1]]


def append_to_section(
    md: str,
    heading_pattern: str | re.Pattern[str],
    new_lines: str,
    level: int = 2,
    *,
    create_if_missing: tuple[str, int] | None = None,
) -> tuple[str, str]:
    """Append `new_lines` to the END of the section's body (before the
    next same-level heading).

    Args:
      md: full markdown text
      heading_pattern: regex matched against heading text
      new_lines: content to append (caller provides newlines/leading spacing)
      level: heading level the pattern targets
      create_if_missing: if set, (heading_text, level) — create the section
        at the end of the file when not found

    Returns: (new_md, status) where status is one of:
      "appended"          — section found, content appended at its end
      "appended_created"  — section did not exist, created at end of file
      "section_not_found" — could not find AND not allowed to create

    Strips trailing whitespace from the section body before appending so
    we don't accumulate blank lines across many edits.
    """
    indices = find_section_indices(md, heading_pattern, level=level)
    if indices is None:
        if create_if_missing:
            heading_text, lvl = create_if_missing
            marks = "#" * lvl
            tail = md.rstrip() + "\n\n"
            new_md = tail + f"{marks} {heading_text}\n\n" + new_lines.lstrip("\n")
            if not new_md.endswith("\n"):
                new_md += "\n"
            return new_md, "appended_created"
        return md, "section_not_found"
    start, end = indices
    body = md[start:end]
    body_trimmed = body.rstrip() + "\n\n"
    new_body = body_trimmed + new_lines.lstrip("\n")
    if not new_body.endswith("\n"):
        new_body += "\n"
    return md[:start] + new_body + md[end:], "appended"


def list_sections(md: str, level: int = 2) -> list[str]:
    """List the section heading texts present at the given level."""
    target_marks = "#" * level
    return [
        m.group(2).strip()
        for m in _SECTION_HEADING_RE.finditer(md)
        if m.group(1) == target_marks
    ]


# ── Git scope (commit safety) ───────────────────────────────────────


def _kit_dir_relative_to_git(slug: str) -> Path:
    """Path of business-kits/<slug>/ relative to the git repo root.

    Astra runs both locally (where cwd may be the repo root) and in
    Railway containers (where cwd is /app and the repo IS /app). In
    both cases the kits live at <repo>/business-kits/<slug>/.
    """
    return Path("business-kits") / slug


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run a git command. Returns CompletedProcess (caller checks returncode)."""
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=30,
    )


def _repo_root() -> Path:
    """Find the git repo root containing business-kits/.

    We don't assume cwd; search from the kits root upward.
    """
    candidate = _kits_root().parent
    # Walk up until we find a .git directory
    cur = candidate
    for _ in range(6):
        if (cur / ".git").exists():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    # Fallback: trust that kits root's parent is the repo root
    return candidate


def commit_kit(
    slug: str,
    *,
    message: str | None = None,
    push: bool = True,
) -> dict[str, Any]:
    """Stage + commit + (optionally) push changes scoped to business-kits/<slug>/.

    Scoping is critical: we ONLY add files under business-kits/<slug>/.
    Any other working-tree changes are left untouched. This bounds the
    blast radius of self-edits — Astra cannot accidentally commit
    code changes via this tool.

    Returns a dict describing the result:
      {
        "status": "committed" | "no_changes" | "git_error",
        "message": <commit message used>,
        "commit_hash": <short SHA, when committed>,
        "files_changed": [<paths>],
        "pushed": <bool>,
        "stderr": <on error>
      }
    """
    repo = _repo_root()
    rel_dir = _kit_dir_relative_to_git(slug)
    abs_dir = repo / rel_dir
    if not abs_dir.exists():
        raise FileNotFoundError(
            f"kit dir {abs_dir} does not exist; can't commit"
        )

    # 1. Check what's changed in JUST the kit dir
    status = _git(["status", "--porcelain", "--", str(rel_dir)], cwd=repo)
    if status.returncode != 0:
        return {"status": "git_error", "stderr": status.stderr.strip()}
    changed_lines = [ln for ln in status.stdout.splitlines() if ln.strip()]
    if not changed_lines:
        return {
            "status": "no_changes",
            "message": "No changes to commit in business-kits/" + slug,
        }
    files_changed = [ln[3:].strip() for ln in changed_lines]

    # 2. Stage only the kit dir (NOT git add .)
    add = _git(["add", "--", str(rel_dir)], cwd=repo)
    if add.returncode != 0:
        return {"status": "git_error", "stderr": add.stderr.strip()}

    # 3. Compose commit message if not provided
    if not message:
        n = len(files_changed)
        message = (
            f"Update {slug} kit ({n} file{'s' if n != 1 else ''} changed)\n\n"
            f"Files:\n" + "\n".join(f"  - {f}" for f in files_changed)
            + "\n\n[auto-commit by Astra kit-editor]"
        )

    # 4. Commit
    commit = _git(["commit", "-m", message], cwd=repo)
    if commit.returncode != 0:
        return {
            "status": "git_error",
            "stderr": commit.stderr.strip(),
            "stdout": commit.stdout.strip(),
        }

    # 5. Resolve the commit hash
    sha = _git(["rev-parse", "--short", "HEAD"], cwd=repo)
    commit_hash = sha.stdout.strip() if sha.returncode == 0 else "?"

    pushed = False
    push_err: str | None = None
    if push:
        push_proc = _git(["push", "origin", "HEAD"], cwd=repo)
        if push_proc.returncode == 0:
            pushed = True
        else:
            push_err = push_proc.stderr.strip()

    return {
        "status": "committed",
        "message": message,
        "commit_hash": commit_hash,
        "files_changed": files_changed,
        "pushed": pushed,
        "push_error": push_err,
    }


# ── High-level edit operations ──────────────────────────────────────


def add_forbidden_phrase(
    slug: str,
    phrase: str,
    *,
    rationale: str = "",
    auto_commit: bool = True,
) -> dict[str, Any]:
    """Add a phrase to a kit's forbidden list.

    Coordinates two edits:
    1. brand.yml: append to forbidden_phrases (machine-enforced)
    2. voice.md: append to "Words and phrases X NEVER uses" section
       (so the model sees it in the prompt)

    Idempotent: if the phrase is already in either location, that
    location is left alone. If both are already present, returns
    status="already_present" and skips the commit.
    """
    paths = KitPaths.for_slug(slug)
    phrase = phrase.strip()
    if not phrase:
        raise ValueError("phrase cannot be empty")

    actions: list[str] = []

    # 1. brand.yml
    data = load_brand_yml(paths)
    forbidden = list(data.get("forbidden_phrases") or [])
    if phrase.lower() not in {p.lower() for p in forbidden}:
        forbidden.append(phrase)
        data["forbidden_phrases"] = forbidden
        save_brand_yml(paths, data)
        actions.append("added_to_brand_yml")

    # 2. voice.md
    if paths.voice_md.exists():
        md = paths.voice_md.read_text()
        # Check if it's already in the NEVER-uses section to avoid duplicate
        never_section = get_section(md, r"NEVER\s+uses?", level=2)
        already_in_voice = never_section and phrase.lower() in never_section.lower()
        if not already_in_voice:
            line = f'- **"{phrase}"**'
            if rationale:
                line += f" — {rationale}"
            line += "\n"
            new_md, status = append_to_section(
                md,
                r"NEVER\s+uses?",
                line,
                level=2,
                create_if_missing=("Words and phrases NEVER uses", 2),
            )
            paths.voice_md.write_text(new_md, encoding="utf-8")
            actions.append(f"voice_md_{status}")

    if not actions:
        return {
            "slug": slug,
            "phrase": phrase,
            "status": "already_present",
            "actions": [],
            "commit": None,
        }

    commit_result: dict[str, Any] | None = None
    if auto_commit:
        commit_result = commit_kit(
            slug,
            message=f'kit:{slug}: add forbidden phrase "{phrase}"' + (
                f" — {rationale}" if rationale else ""
            ),
        )

    return {
        "slug": slug,
        "phrase": phrase,
        "status": "added",
        "actions": actions,
        "commit": commit_result,
    }


def add_voice_note(
    slug: str,
    *,
    kind: str,
    content: str,
    context: str = "",
    auto_commit: bool = True,
) -> dict[str, Any]:
    """Append a note to voice.md.

    kind:
      "does"   — append to "Words and phrases X DOES use" section
      "never"  — append to "Words and phrases X NEVER uses" section
                 (without machine-enforcement; for soft / advisory rules.
                 Use add_forbidden_phrase for hard bans.)
      "sample" — append to a "voice samples" section as a quoted block.
                 `context` becomes the attribution line.
    """
    if kind not in ("does", "never", "sample"):
        raise ValueError(f"kind must be one of: does, never, sample. Got {kind!r}")

    paths = KitPaths.for_slug(slug)
    if not paths.voice_md.exists():
        raise FileNotFoundError(f"voice.md missing for {slug}")
    md = paths.voice_md.read_text()

    if kind == "does":
        line = f'- **"{content.strip()}"**'
        if context:
            line += f" — {context}"
        line += "\n"
        pattern = r"DOES\s+use"
        create = ("Words and phrases DOES use", 2)
    elif kind == "never":
        line = f'- **"{content.strip()}"**'
        if context:
            line += f" — {context}"
        line += "\n"
        pattern = r"NEVER\s+use"
        create = ("Words and phrases NEVER uses", 2)
    else:  # sample
        # Multi-line block-quote sample
        block = "> *" + content.strip() + "*\n"
        if context:
            block += f"> — {context}\n"
        block += "\n"
        line = block
        pattern = r"voice\s+samples?"
        create = ("Voice samples", 2)

    new_md, status = append_to_section(
        md, pattern, line, level=2, create_if_missing=create,
    )
    if status == "section_not_found":
        return {
            "slug": slug,
            "kind": kind,
            "status": "section_not_found",
            "available_sections": list_sections(md, level=2),
        }
    paths.voice_md.write_text(new_md, encoding="utf-8")

    commit_result: dict[str, Any] | None = None
    if auto_commit:
        commit_result = commit_kit(
            slug,
            message=f"kit:{slug}: add voice note ({kind}): {content.strip()[:60]}",
        )

    return {
        "slug": slug,
        "kind": kind,
        "status": status,
        "commit": commit_result,
    }


def add_proof_point(
    slug: str,
    *,
    section: str,
    content: str,
    auto_commit: bool = True,
) -> dict[str, Any]:
    """Append a proof point to content/proof-points.md.

    section: short hint matched against actual section headings.
      Common values: "customers", "traction", "team", "press", "awards",
      "testimonials", "capabilities", "competitive_positioning".

    Content is treated as a single bullet OR paragraph depending on
    whether it starts with `-`. Bullets get appended directly; paragraphs
    get appended with surrounding blank lines.
    """
    paths = KitPaths.for_slug(slug)
    if not paths.proof_points_md.exists():
        raise FileNotFoundError(
            f"content/proof-points.md missing for {slug}"
        )
    md = paths.proof_points_md.read_text()

    # Map short section hints to regex patterns matching real headings
    section_patterns = {
        "customers": r"Customers?(\s*/\s*clients?)?",
        "clients": r"Customers?(\s*/\s*clients?)?",
        "traction": r"Traction\s+metrics?",
        "team": r"^Team\b",
        "press": r"Press(\s*/\s*coverage)?",
        "coverage": r"Press(\s*/\s*coverage)?",
        "awards": r"Awards?(\s*/\s*recognition)?",
        "recognition": r"Awards?(\s*/\s*recognition)?",
        "testimonials": r"Testimonial",
        "capabilities": r"Capabilit",
        "scope": r"Capabilit",
        "competitive_positioning": r"Competitive\s+positioning",
        "competitive": r"Competitive\s+positioning",
        "open_sensitive": r"Open\s*/\s*sensitive",
        "sensitive": r"Open\s*/\s*sensitive",
    }
    pattern = section_patterns.get(section.lower().strip(), section)

    content = content.rstrip() + "\n"
    if not content.lstrip().startswith(("-", "*", "•")):
        content = "\n" + content + "\n"

    new_md, status = append_to_section(
        md, pattern, content, level=2,
    )
    if status == "section_not_found":
        return {
            "slug": slug,
            "section": section,
            "status": "section_not_found",
            "available_sections": list_sections(md, level=2),
        }
    paths.proof_points_md.write_text(new_md, encoding="utf-8")

    commit_result: dict[str, Any] | None = None
    if auto_commit:
        commit_result = commit_kit(
            slug,
            message=f"kit:{slug}: add proof point to '{section}': {content.strip()[:60]}",
        )

    return {
        "slug": slug,
        "section": section,
        "status": status,
        "commit": commit_result,
    }


def add_audience_objection(
    slug: str,
    *,
    audience: str,
    objection: str,
    response: str,
    auto_commit: bool = True,
) -> dict[str, Any]:
    """Append an objection + response pair to an audience persona file.

    Looks for the "Common objections + how to handle" section. Uses
    the same bullet format as the existing audience files:

      - **Objection:** ...
        **Response:** ...
    """
    paths = KitPaths.for_slug(slug)
    aud_file = paths.audiences_dir / f"{audience}.md"
    if not aud_file.exists():
        avail = sorted(p.stem for p in paths.audiences_dir.glob("*.md"))
        return {
            "slug": slug,
            "audience": audience,
            "status": "audience_not_found",
            "available_audiences": avail,
        }

    md = aud_file.read_text()
    block = (
        f"- **Objection:** {objection.strip()}\n"
        f"  **Response:** {response.strip()}\n\n"
    )
    new_md, status = append_to_section(
        md, r"Common\s+objections?", block, level=2,
        create_if_missing=("Common objections + how to handle", 2),
    )
    aud_file.write_text(new_md, encoding="utf-8")

    commit_result: dict[str, Any] | None = None
    if auto_commit:
        commit_result = commit_kit(
            slug,
            message=(
                f"kit:{slug}: add {audience} objection: "
                + objection.strip()[:60]
            ),
        )

    return {
        "slug": slug,
        "audience": audience,
        "status": status,
        "commit": commit_result,
    }
