"""
Layer 2 self-modification: Astra editing its own Python code.

Three things make this safe:

1. **Path allowlist.** Edits are bounded to a small set of
   directories that contain the creator capability + its tests. Code
   in astra/main.py, astra/agent/, astra/db/, infra config files,
   and credentials are off-limits via this path. They can be expanded
   later when the safety case is made.

2. **Test gate.** commit_code_changes() runs the test suite BEFORE
   the commit. If any creator test fails, the commit is blocked.
   The suite runs in <1 second so this gate is cheap.

3. **Git scoping per change kind.** Code commits stage files in the
   code allowlist; kit commits (Layer 1) stage files in
   business-kits/<slug>/. The two paths are disjoint — Astra cannot
   accidentally cross-commit data via the code-edit path or
   vice versa.

What's intentionally NOT included:
- Direct write of arbitrary content. Edits go through edit_astra_file
  which requires a unique old_string for the replacement (preventing
  accidental overwrites of the wrong section).
- Bypassing the test gate. require_tests=False is a parameter, not a
  default. The autonomy system can choose to forbid it entirely.
- Editing this file (edit_code.py) at runtime — see _is_self_path()
  guard. Otherwise Astra could disable the safeguards by editing
  the safeguards.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ── Allowlist ───────────────────────────────────────────────────────


# Path prefixes (relative to repo root) that Astra is allowed to edit.
# Order matters only for documentation — the code uses set membership.
_ALLOWED_PREFIXES: tuple[str, ...] = (
    "astra/creators/",
    "astra/tools/",
    "tests/test_creators/",
    "tests/fixtures/",
)

# Single files at the repo root that are allowed (e.g. for adding deps).
_ALLOWED_TOP_LEVEL_FILES: frozenset[str] = frozenset({
    "pyproject.toml",
})

# Path prefixes that are EXPLICITLY forbidden even if they would
# match an allowed prefix by string ops. Belt-and-braces against
# escaping via .. or symlinks.
_DENIED_PREFIXES: tuple[str, ...] = (
    ".git/",
    ".venv/",
    "node_modules/",
    "credentials/",
    ".dumps/",
    ".logs/",
    ".pids/",
    "business-kits/_schema/",
)

# Files Astra is explicitly forbidden from editing — including this
# module itself. Astra disabling its own safeguards would be a
# self-defeating no-op gate; the gate must be code Astra cannot
# rewrite from inside the agent loop.
_DENIED_FILES: frozenset[str] = frozenset({
    "astra/creators/edit_code.py",
    "astra/tools/code_editor_tools.py",
})


def _repo_root() -> Path:
    """Resolve the git repo root from this module's location.

    edit_code.py lives at <repo>/astra/creators/edit_code.py, so the
    repo root is two parents up. We verify by checking for .git/.
    """
    candidate = Path(__file__).resolve().parents[2]
    if (candidate / ".git").exists():
        return candidate
    # Fallback walk-up
    cur = candidate
    for _ in range(5):
        if (cur / ".git").exists():
            return cur
        cur = cur.parent
    return candidate


def _normalize_path(path: str) -> tuple[str, Path]:
    """Convert a user-supplied path to a (relative_str, absolute_path) pair.

    Raises ValueError if the path is empty, absolute outside the repo,
    or escapes the repo via ..

    The relative_str uses POSIX separators (forward slashes) so allowlist
    matching is consistent across OS.
    """
    if not path or not path.strip():
        raise ValueError("path cannot be empty")
    p_str = path.strip()

    repo = _repo_root()
    p = Path(p_str)
    if p.is_absolute():
        try:
            rel = p.resolve().relative_to(repo.resolve())
        except ValueError:
            raise ValueError(
                f"absolute path {p} is outside the repo {repo}"
            ) from None
        p = rel
    else:
        # Resolve relative to repo, but check it doesn't escape via ..
        abs_p = (repo / p).resolve()
        try:
            p = abs_p.relative_to(repo.resolve())
        except ValueError:
            raise ValueError(
                f"path {p_str} escapes the repo (resolves to {abs_p})"
            ) from None

    rel_str = p.as_posix()
    return rel_str, repo / rel_str


def _is_self_path(rel_path: str) -> bool:
    """Is the path one of the safeguards Astra is forbidden from editing?"""
    return rel_path in _DENIED_FILES


def _path_is_allowed(rel_path: str) -> tuple[bool, str]:
    """Return (allowed, reason). reason is empty if allowed, else explanation.

    Path matching handles both file paths and bare directory paths.
    Trailing slashes are normalized: "astra/creators/" and
    "astra/creators" both match the prefix "astra/creators/".
    """
    # Denylist wins — even if a path matches an allowed prefix, denied
    # prefixes block it.
    for denied in _DENIED_PREFIXES:
        if rel_path.startswith(denied) or rel_path == denied.rstrip("/"):
            return False, f"path is under denied prefix {denied!r}"

    if _is_self_path(rel_path):
        return False, (
            f"{rel_path} is a self-modification safeguard and cannot "
            f"be edited by Astra at runtime; ask the human to edit it directly"
        )

    # Top-level files allowed
    if rel_path in _ALLOWED_TOP_LEVEL_FILES:
        return True, ""

    # Allowlist — accept as a prefix match OR exact-dir match
    # (i.e. "astra/creators" matches the "astra/creators/" prefix even
    # though Path normalization strips the trailing slash)
    for allowed in _ALLOWED_PREFIXES:
        if rel_path.startswith(allowed) or rel_path == allowed.rstrip("/"):
            return True, ""

    return False, (
        f"path {rel_path!r} is not in the code-edit allowlist. "
        f"Allowed: {list(_ALLOWED_PREFIXES) + list(_ALLOWED_TOP_LEVEL_FILES)}"
    )


# ── File ops (read / list / edit / write) ───────────────────────────


def read_astra_file(path: str, *, max_bytes: int = 1_000_000) -> dict[str, Any]:
    """Read a file from the Astra repo.

    Returns: {path, exists, content, byte_size, line_count} OR
             {path, error} if the path is denied.

    Reading is allowed for any path under astra/, tests/, business-kits/,
    and a few top-level files — broader than the WRITE allowlist
    intentionally, since reads are non-destructive. The read allowlist
    is a thin denylist (no .git, no credentials, no .env).

    max_bytes caps the response size to keep prompt budgets sane.
    """
    rel, abs_path = _normalize_path(path)

    # Read denylist (separate from write — reads are broader)
    for denied in (".git/", ".venv/", "node_modules/", "credentials/",
                   ".dumps/", ".pids/"):
        if rel.startswith(denied):
            return {"path": rel, "error": f"reads denied under {denied!r}"}
    if rel in {".env", ".env.local", ".env.production"}:
        return {"path": rel, "error": f"reads of {rel} are denied (secrets)"}

    if not abs_path.exists():
        return {"path": rel, "exists": False, "error": "file not found"}
    if not abs_path.is_file():
        return {"path": rel, "exists": True, "error": "not a file"}

    raw = abs_path.read_bytes()
    truncated = False
    if len(raw) > max_bytes:
        raw = raw[:max_bytes]
        truncated = True
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {
            "path": rel, "exists": True, "error": "binary file (not UTF-8)",
            "byte_size": abs_path.stat().st_size,
        }

    return {
        "path": rel,
        "exists": True,
        "content": content,
        "byte_size": abs_path.stat().st_size,
        "line_count": content.count("\n") + (1 if content and not content.endswith("\n") else 0),
        "truncated": truncated,
    }


def list_astra_files(directory: str = "astra/creators/") -> dict[str, Any]:
    """List files in a directory under the read scope.

    Returns: {dir, files: [{path, byte_size, line_count}, ...]} OR
             {dir, error}.
    """
    rel, abs_dir = _normalize_path(directory)
    if not abs_dir.exists():
        return {"dir": rel, "error": "directory not found"}
    if not abs_dir.is_dir():
        return {"dir": rel, "error": "not a directory"}

    out: list[dict[str, Any]] = []
    for f in sorted(abs_dir.rglob("*")):
        if not f.is_file():
            continue
        if any(part.startswith(".") for part in f.parts[len(abs_dir.parts):]):
            continue  # skip dotfiles
        if "__pycache__" in f.parts:
            continue
        try:
            size = f.stat().st_size
            lines = sum(1 for _ in f.open("rb")) if size < 5_000_000 else None
        except OSError:
            continue
        out.append({
            "path": f.relative_to(_repo_root()).as_posix(),
            "byte_size": size,
            "line_count": lines,
        })
    return {"dir": rel, "files": out}


def edit_astra_file(
    path: str,
    old_string: str,
    new_string: str,
    *,
    replace_all: bool = False,
) -> dict[str, Any]:
    """Edit a file by exact-string replacement.

    Safer than write — caller must specify the exact text to replace,
    so accidentally overwriting unrelated content is impossible.

    Returns: {path, status, replacements} where status is one of:
      "edited"             — successful edit
      "old_string_missing" — old_string not found in file
      "old_string_ambiguous" — multiple matches and replace_all=False
      "denied"             — path not in allowlist
      "no_change"          — old_string equals new_string
    """
    rel, abs_path = _normalize_path(path)
    allowed, reason = _path_is_allowed(rel)
    if not allowed:
        return {"path": rel, "status": "denied", "reason": reason}

    if not abs_path.exists():
        return {"path": rel, "status": "file_not_found"}

    if old_string == new_string:
        return {"path": rel, "status": "no_change"}

    text = abs_path.read_text(encoding="utf-8")
    if old_string not in text:
        return {
            "path": rel,
            "status": "old_string_missing",
            "hint": "The exact string was not found. Re-read the file and try again.",
        }

    occurrences = text.count(old_string)
    if occurrences > 1 and not replace_all:
        return {
            "path": rel,
            "status": "old_string_ambiguous",
            "occurrences": occurrences,
            "hint": (
                "old_string matched in multiple places. Provide more "
                "surrounding context to make it unique, or pass replace_all=True."
            ),
        }

    if replace_all:
        new_text = text.replace(old_string, new_string)
        replacements = occurrences
    else:
        new_text = text.replace(old_string, new_string, 1)
        replacements = 1

    abs_path.write_text(new_text, encoding="utf-8")
    return {
        "path": rel,
        "status": "edited",
        "replacements": replacements,
    }


def write_astra_file(
    path: str,
    content: str,
    *,
    overwrite_existing: bool = False,
) -> dict[str, Any]:
    """Write a file (create new, or full overwrite when explicit).

    Use edit_astra_file for surgical changes. Use this for new files
    or for full rewrites where you've already read the existing content.

    Returns: {path, status} where status is one of:
      "created"      — new file written
      "overwritten"  — existing file fully replaced (overwrite_existing=True)
      "exists"       — file exists and overwrite_existing=False
      "denied"       — path not in allowlist
    """
    rel, abs_path = _normalize_path(path)
    allowed, reason = _path_is_allowed(rel)
    if not allowed:
        return {"path": rel, "status": "denied", "reason": reason}

    existed = abs_path.exists()
    if existed and not overwrite_existing:
        return {"path": rel, "status": "exists",
                "hint": "Pass overwrite_existing=True to replace the file's full content."}

    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(content, encoding="utf-8")
    return {
        "path": rel,
        "status": "overwritten" if existed else "created",
        "byte_size": len(content.encode("utf-8")),
    }


# ── Diff ────────────────────────────────────────────────────────────


def show_astra_diff(
    *,
    paths: list[str] | None = None,
    staged: bool = False,
) -> dict[str, Any]:
    """Show the working-tree diff for code paths.

    By default shows only changes under the code allowlist directories
    (astra/creators/, astra/tools/, tests/test_creators/, tests/fixtures/).
    Excludes any kit edits — those are reviewable via separate kit
    tooling.

    Returns: {stat, diff, files_changed}
    """
    repo = _repo_root()
    args: list[str] = ["diff"]
    if staged:
        args.append("--cached")

    # Scope to allowed prefixes if no explicit paths
    target_paths = paths or list(_ALLOWED_PREFIXES) + list(_ALLOWED_TOP_LEVEL_FILES)
    args += ["--", *target_paths]

    proc_stat = subprocess.run(
        ["git", "diff", "--stat"] + (["--cached"] if staged else []) +
        ["--", *target_paths],
        cwd=str(repo), capture_output=True, text=True, timeout=10,
    )
    proc_full = subprocess.run(
        ["git"] + args,
        cwd=str(repo), capture_output=True, text=True, timeout=15,
    )
    if proc_stat.returncode != 0 or proc_full.returncode != 0:
        return {
            "error": "git_error",
            "stderr": (proc_stat.stderr + proc_full.stderr).strip(),
        }

    # Parse files_changed from the --stat output (last line is summary,
    # earlier lines are "<path> | <changes>")
    files_changed: list[str] = []
    for line in proc_stat.stdout.splitlines():
        line = line.strip()
        if "|" in line:
            files_changed.append(line.split("|", 1)[0].strip())

    return {
        "stat": proc_stat.stdout.strip(),
        "diff": proc_full.stdout,
        "files_changed": files_changed,
        "staged": staged,
    }


# ── Test gate ───────────────────────────────────────────────────────


def run_creator_tests(
    *,
    filter_pattern: str | None = None,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    """Run the creator test suite. The test gate for code self-edits.

    Args:
      filter_pattern: optional pytest -k filter
      timeout_seconds: kill the test run if it hangs (default 60s,
        the full suite runs in <2s normally)

    Returns: {
      status: "passed" | "failed" | "errored" | "timeout",
      summary: short one-line summary,
      passed_count, failed_count, error_count,
      duration_seconds,
      output: pytest stdout/stderr (truncated to 8000 chars),
      failed_tests: [list of failing test ids when status=failed]
    }
    """
    repo = _repo_root()
    # Use the current interpreter so we work both in dev (.venv/bin/python)
    # and in temp test repos that don't have a venv.
    cmd = [sys.executable, "-m", "pytest", "tests/test_creators/",
           "--tb=short", "-q", "--no-header"]
    if filter_pattern:
        cmd += ["-k", filter_pattern]

    try:
        proc = subprocess.run(
            cmd, cwd=str(repo), capture_output=True, text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as e:
        return {
            "status": "timeout",
            "summary": f"Tests killed after {timeout_seconds}s",
            "duration_seconds": timeout_seconds,
            "output": (e.output or "")[-2000:] if e.output else "",
        }
    except FileNotFoundError as e:
        return {
            "status": "errored",
            "summary": f"pytest binary not found: {e}",
            "output": "",
        }

    output = (proc.stdout + "\n" + proc.stderr)[-8000:]
    # Parse the summary line — pytest prints "X passed, Y failed in Zs"
    summary_line = ""
    for line in proc.stdout.splitlines()[::-1]:
        if "passed" in line or "failed" in line or "error" in line:
            summary_line = line.strip()
            break

    passed = int((m := re.search(r"(\d+)\s+passed", summary_line)) and m.group(1) or 0)
    failed = int((m := re.search(r"(\d+)\s+failed", summary_line)) and m.group(1) or 0)
    errored = int((m := re.search(r"(\d+)\s+error", summary_line)) and m.group(1) or 0)
    duration = (m := re.search(r"in\s+([\d.]+)s", summary_line)) and float(m.group(1)) or 0.0

    failed_tests: list[str] = []
    if failed:
        # Pytest with -q prints "FAILED <test_id>" lines
        for line in proc.stdout.splitlines():
            if line.startswith("FAILED "):
                failed_tests.append(line.removeprefix("FAILED ").split(" - ")[0].strip())

    if proc.returncode == 0:
        status = "passed"
    elif failed:
        status = "failed"
    else:
        status = "errored"

    return {
        "status": status,
        "summary": summary_line or f"pytest exit {proc.returncode}",
        "passed_count": passed,
        "failed_count": failed,
        "error_count": errored,
        "duration_seconds": duration,
        "output": output,
        "failed_tests": failed_tests,
    }


# ── Commit + revert ─────────────────────────────────────────────────


def _git(args: list[str], cwd: Path, *, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args, cwd=str(cwd), capture_output=True,
        text=True, timeout=timeout,
    )


def commit_code_changes(
    *,
    message: str,
    paths: list[str] | None = None,
    require_tests: bool = True,
    push: bool = True,
) -> dict[str, Any]:
    """Stage + (run tests) + commit + push code changes.

    Scoped to the code allowlist. Will NOT commit kit edits or any
    other working-tree changes.

    Args:
      message: commit message (required, non-empty)
      paths: optional explicit list. Defaults to all code-allowlist
        prefixes — i.e. "anything in the working tree under the
        allowed directories."
      require_tests: if True, run_creator_tests must pass before the
        commit. Default True. Set False at your own risk; the
        autonomy system can forbid this entirely.
      push: if True, push to origin after commit. Default True.

    Returns: {
      status: "committed" | "no_changes" | "tests_failed"
              | "git_error" | "denied",
      message, commit_hash, files_changed, pushed,
      test_result: <run_creator_tests output, when require_tests=True>
      stderr: <on git error>
    }
    """
    if not message or not message.strip():
        return {"status": "denied", "reason": "message is required"}

    repo = _repo_root()
    target_paths = paths or list(_ALLOWED_PREFIXES) + list(_ALLOWED_TOP_LEVEL_FILES)

    # Validate: every target_path must pass the allowlist
    for p in target_paths:
        rel, _ = _normalize_path(p)
        allowed, reason = _path_is_allowed(rel)
        if not allowed:
            return {
                "status": "denied",
                "reason": f"path {rel!r} not in code-edit allowlist: {reason}",
            }

    # Filter to paths that actually exist on disk. git add fails on
    # missing pathspecs; this is benign filtering, not a security check
    # (the allowlist above is the security check).
    existing_paths = [
        p for p in target_paths if (repo / p).exists()
    ]
    if not existing_paths:
        return {"status": "no_changes",
                "message": "No code paths exist on disk"}
    target_paths = existing_paths

    # 1. Check what's changed under target_paths
    status_proc = _git(
        ["status", "--porcelain", "--", *target_paths], cwd=repo,
    )
    if status_proc.returncode != 0:
        return {"status": "git_error", "stderr": status_proc.stderr.strip()}
    changed_lines = [ln for ln in status_proc.stdout.splitlines() if ln.strip()]
    if not changed_lines:
        return {"status": "no_changes",
                "message": "No code changes detected in allowlist"}
    files_changed = [ln[3:].strip() for ln in changed_lines]

    # 2. Test gate
    test_result = None
    if require_tests:
        test_result = run_creator_tests()
        if test_result["status"] != "passed":
            return {
                "status": "tests_failed",
                "test_result": test_result,
                "files_changed_in_working_tree": files_changed,
                "hint": (
                    "Tests failed. Fix the failures or revert your changes "
                    "(write_astra_file with overwrite_existing=True restores; "
                    "or `git checkout -- <path>` from outside Astra)."
                ),
            }

    # 3. Stage allowed paths only
    add = _git(["add", "--", *target_paths], cwd=repo)
    if add.returncode != 0:
        return {"status": "git_error", "stderr": add.stderr.strip()}

    # 4. Verify the staged set doesn't include anything outside the allowlist
    staged_check = _git(["diff", "--cached", "--name-only"], cwd=repo)
    staged_files = staged_check.stdout.strip().splitlines() if staged_check.returncode == 0 else []
    out_of_scope = [
        f for f in staged_files
        if not any(f.startswith(ap) for ap in _ALLOWED_PREFIXES)
        and f not in _ALLOWED_TOP_LEVEL_FILES
    ]
    if out_of_scope:
        # Defensive: unstage them
        _git(["reset", "HEAD", "--", *out_of_scope], cwd=repo)
        return {
            "status": "denied",
            "reason": f"staged files outside allowlist: {out_of_scope}",
            "hint": "auto-unstaged; re-stage explicitly via paths= if intended",
        }

    # 5. Commit
    commit = _git(["commit", "-m", message + "\n\n[code self-edit by Astra]"],
                  cwd=repo)
    if commit.returncode != 0:
        return {"status": "git_error",
                "stderr": commit.stderr.strip(),
                "stdout": commit.stdout.strip()}

    sha_proc = _git(["rev-parse", "--short", "HEAD"], cwd=repo)
    commit_hash = sha_proc.stdout.strip() if sha_proc.returncode == 0 else "?"

    pushed, push_err = False, None
    if push:
        push_proc = _git(["push", "origin", "HEAD"], cwd=repo, timeout=60)
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
        "test_result": test_result,
    }


def revert_last_code_commit(*, push: bool = True) -> dict[str, Any]:
    """Revert the most recent commit IF it was a code-self-edit.

    Safety check: refuses to revert commits that touched files
    outside the code allowlist. This prevents accidentally undoing
    a hand-edit by Kunal.

    Returns: {
      status: "reverted" | "not_self_edit" | "no_commits" | "git_error",
      reverted_hash: <the commit that was reverted>,
      revert_hash: <new commit hash>,
      pushed
    }
    """
    repo = _repo_root()

    # 1. What did HEAD touch?
    head_files = _git(
        ["diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
        cwd=repo,
    )
    if head_files.returncode != 0:
        return {"status": "git_error", "stderr": head_files.stderr.strip()}
    head_paths = [p for p in head_files.stdout.strip().splitlines() if p]
    if not head_paths:
        return {"status": "no_commits", "reason": "HEAD is empty or root"}

    # 2. Are all of those paths within the code allowlist?
    out_of_scope = [
        p for p in head_paths
        if not any(p.startswith(ap) for ap in _ALLOWED_PREFIXES)
        and p not in _ALLOWED_TOP_LEVEL_FILES
    ]
    if out_of_scope:
        return {
            "status": "not_self_edit",
            "reason": "HEAD includes files outside the code allowlist; "
                      "won't auto-revert. Use git directly if intentional.",
            "out_of_scope_files": out_of_scope,
        }

    # 3. Capture the SHA we're reverting
    head_sha = _git(["rev-parse", "--short", "HEAD"], cwd=repo).stdout.strip()

    # 4. git revert
    revert_msg = f"Revert: {head_sha} (auto-revert by Astra)"
    rev = _git(["revert", "--no-edit", "-m", "1", "HEAD"], cwd=repo, timeout=20)
    # Note: -m 1 is for merge commits; on regular commits it's harmless and
    # `git revert HEAD` works too. We try with -m 1 first; if that errors,
    # fall back.
    if rev.returncode != 0:
        rev = _git(["revert", "--no-edit", "HEAD"], cwd=repo, timeout=20)
    if rev.returncode != 0:
        return {"status": "git_error", "stderr": rev.stderr.strip()}

    revert_sha = _git(["rev-parse", "--short", "HEAD"], cwd=repo).stdout.strip()

    pushed, push_err = False, None
    if push:
        push_proc = _git(["push", "origin", "HEAD"], cwd=repo, timeout=60)
        if push_proc.returncode == 0:
            pushed = True
        else:
            push_err = push_proc.stderr.strip()

    return {
        "status": "reverted",
        "reverted_hash": head_sha,
        "revert_hash": revert_sha,
        "files_reverted": head_paths,
        "pushed": pushed,
        "push_error": push_err,
    }
