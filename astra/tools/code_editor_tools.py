"""
MCP tools that let Astra self-correct its own Python code.

Layer 2 of self-modification. Read this AFTER you've read
kit_editor_tools.py — Layer 1 (kit data) is the safer pattern this
layer extends to code.

Tools:
  read_astra_file         — read a file from the repo
  list_astra_files        — list files in a directory
  edit_astra_file         — surgical edit (old_string → new_string)
  write_astra_file        — full file write (new file or overwrite)
  show_astra_diff         — git diff for code paths
  run_creator_tests       — pytest tests/test_creators/ (the safety gate)
  commit_code_changes     — stage + (test) + commit + push
  revert_last_code_commit — undo the most recent code self-edit

The safety stack:
  1. Path allowlist enforced in edit_code.py (astra/creators/,
     astra/tools/, tests/test_creators/, tests/fixtures/, plus
     pyproject.toml). Edit/write outside that fails with "denied".
  2. edit_code.py and code_editor_tools.py are themselves DENIED —
     Astra cannot disable its own safeguards from inside the loop.
  3. commit_code_changes runs run_creator_tests by default; if any
     test fails, the commit is blocked.
  4. The autonomy mode system can additionally require explicit
     human approval for any code-edit tool call.
  5. revert_last_code_commit gives a one-call undo path. The
     revert refuses to undo commits that touched files outside the
     code allowlist (so it can't accidentally revert hand-edits).
"""

from __future__ import annotations

from claude_agent_sdk import tool

from astra.creators.edit_code import (
    commit_code_changes,
    edit_astra_file,
    list_astra_files,
    read_astra_file,
    revert_last_code_commit,
    run_creator_tests,
    show_astra_diff,
    write_astra_file,
)


# ── Read tools (no autonomy gate — read-only) ───────────────────────


@tool(
    "read_astra_file",
    "Read a file from the Astra repo (your own codebase). Use to inspect "
    "code before proposing edits, look up tool implementations, read "
    "tests, or read the brand kits. Reads are permitted broadly across "
    "the repo (denied: .git/, .venv/, credentials/, .env*, .dumps/). "
    "Returns the file content + size + line count.",
    {
        "path": str,            # repo-relative or absolute (resolved into repo)
        "max_bytes": int,       # optional, default 1_000_000
    },
)
async def read_astra_file_tool(args: dict) -> dict:
    path = (args.get("path") or "").strip()
    max_bytes = int(args.get("max_bytes") or 1_000_000)
    if not path:
        return {"content": [{"type": "text", "text": "read_astra_file: path required"}]}
    try:
        result = read_astra_file(path, max_bytes=max_bytes)
    except ValueError as e:
        return {"content": [{"type": "text", "text": f"path error: {e}"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"read failed: {type(e).__name__}: {e}"}]}

    if "error" in result:
        return {"content": [{"type": "text", "text": (
            f"{result['path']}: {result['error']}"
        )}]}

    text = (
        f"# {result['path']}  ({result['byte_size']:,} bytes, "
        f"{result['line_count']} lines"
        + (", TRUNCATED" if result.get("truncated") else "")
        + ")\n\n"
        + result["content"]
    )
    return {"content": [{"type": "text", "text": text}]}


@tool(
    "list_astra_files",
    "List files in a directory under the Astra repo. Recursive (excludes "
    "__pycache__/ and dotfiles). Returns paths + sizes + line counts. "
    "Use to discover files before reading them, or to check the shape of "
    "a module.",
    {"directory": str},
)
async def list_astra_files_tool(args: dict) -> dict:
    directory = (args.get("directory") or "astra/creators/").strip()
    try:
        result = list_astra_files(directory)
    except ValueError as e:
        return {"content": [{"type": "text", "text": f"path error: {e}"}]}

    if "error" in result:
        return {"content": [{"type": "text", "text": (
            f"{result['dir']}: {result['error']}"
        )}]}

    files = result["files"]
    lines = [f"{result['dir']} — {len(files)} files:"]
    for f in files:
        lc = f"{f['line_count']:>5}L" if f.get("line_count") is not None else "  bin"
        lines.append(f"  {f['byte_size']:>8} B  {lc}  {f['path']}")
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


# ── Edit tools (gated by autonomy mode + path allowlist) ────────────


@tool(
    "edit_astra_file",
    "Surgical edit of a file in the Astra repo. The path must be in the "
    "code-edit allowlist (astra/creators/, astra/tools/, "
    "tests/test_creators/, tests/fixtures/, or pyproject.toml). "
    "old_string MUST be unique in the file unless replace_all=True; "
    "use enough context to disambiguate. The change is in the working "
    "tree only — call commit_code_changes to persist it.",
    {
        "path": str,
        "old_string": str,
        "new_string": str,
        "replace_all": bool,
    },
)
async def edit_astra_file_tool(args: dict) -> dict:
    path = (args.get("path") or "").strip()
    old_string = args.get("old_string") or ""
    new_string = args.get("new_string") or ""
    replace_all = bool(args.get("replace_all", False))
    if not path or not old_string:
        return {"content": [{"type": "text", "text": (
            "edit_astra_file requires: path, old_string, new_string"
        )}]}
    try:
        result = edit_astra_file(path, old_string, new_string, replace_all=replace_all)
    except ValueError as e:
        return {"content": [{"type": "text", "text": f"path error: {e}"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"edit failed: {type(e).__name__}: {e}"}]}

    status = result.get("status")
    if status == "denied":
        return {"content": [{"type": "text", "text": (
            f"DENIED: {result.get('reason','')}"
        )}]}
    if status == "old_string_missing":
        return {"content": [{"type": "text", "text": (
            f"old_string not found in {result['path']}. "
            "Re-read the file and use the exact text."
        )}]}
    if status == "old_string_ambiguous":
        return {"content": [{"type": "text", "text": (
            f"old_string matched {result['occurrences']} times in {result['path']}. "
            "Add surrounding context to make it unique, or pass replace_all=true."
        )}]}
    if status == "no_change":
        return {"content": [{"type": "text", "text": (
            f"{result['path']}: old_string equals new_string, nothing to do"
        )}]}
    if status == "file_not_found":
        return {"content": [{"type": "text", "text": (
            f"{result['path']}: file not found. Use write_astra_file to create."
        )}]}

    return {"content": [{"type": "text", "text": (
        f"edited {result['path']} ({result['replacements']} replacement"
        f"{'s' if result['replacements'] != 1 else ''})\n"
        "Next: show_astra_diff to review, run_creator_tests to verify, "
        "commit_code_changes to persist."
    )}]}


@tool(
    "write_astra_file",
    "Write a file in the Astra repo. Use for creating new files OR "
    "for full rewrites. For surgical changes prefer edit_astra_file. "
    "Path must be in the code-edit allowlist. Existing files are "
    "preserved unless overwrite_existing=true.",
    {
        "path": str,
        "content": str,
        "overwrite_existing": bool,
    },
)
async def write_astra_file_tool(args: dict) -> dict:
    path = (args.get("path") or "").strip()
    content = args.get("content")
    overwrite = bool(args.get("overwrite_existing", False))
    if not path or content is None:
        return {"content": [{"type": "text", "text": (
            "write_astra_file requires: path, content"
        )}]}
    try:
        result = write_astra_file(path, content, overwrite_existing=overwrite)
    except ValueError as e:
        return {"content": [{"type": "text", "text": f"path error: {e}"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"write failed: {type(e).__name__}: {e}"}]}

    status = result.get("status")
    if status == "denied":
        return {"content": [{"type": "text", "text": (
            f"DENIED: {result.get('reason','')}"
        )}]}
    if status == "exists":
        return {"content": [{"type": "text", "text": (
            f"{result['path']}: already exists. "
            "Pass overwrite_existing=true to replace, or use edit_astra_file."
        )}]}

    return {"content": [{"type": "text", "text": (
        f"{status} {result['path']} ({result.get('byte_size', '?')} bytes)\n"
        "Next: show_astra_diff to review, run_creator_tests to verify, "
        "commit_code_changes to persist."
    )}]}


# ── Diff + tests + commit + revert ──────────────────────────────────


@tool(
    "show_astra_diff",
    "Show the working-tree diff for code paths. Scoped to the code "
    "allowlist (astra/creators/, astra/tools/, tests/test_creators/, "
    "tests/fixtures/, pyproject.toml). Use BEFORE commit_code_changes "
    "to review the proposed change. Pass staged=true to show what's "
    "currently staged for commit.",
    {
        "staged": bool,
        "paths": str,           # comma-separated, optional
    },
)
async def show_astra_diff_tool(args: dict) -> dict:
    staged = bool(args.get("staged", False))
    paths_csv = (args.get("paths") or "").strip()
    paths = [p.strip() for p in paths_csv.split(",") if p.strip()] if paths_csv else None
    try:
        result = show_astra_diff(paths=paths, staged=staged)
    except Exception as e:
        return {"content": [{"type": "text", "text": f"diff failed: {type(e).__name__}: {e}"}]}

    if "error" in result:
        return {"content": [{"type": "text", "text": (
            f"git error: {result.get('stderr', '')[:300]}"
        )}]}

    files = result.get("files_changed", []) or []
    if not files:
        return {"content": [{"type": "text", "text": (
            f"No {'staged' if staged else 'unstaged'} code changes."
        )}]}

    diff_text = result["diff"]
    # Cap diff size — agents read these and large diffs blow context.
    if len(diff_text) > 12_000:
        diff_text = diff_text[:12_000] + "\n\n[...diff truncated to 12k chars...]"

    text = (
        f"{'Staged' if staged else 'Working-tree'} diff "
        f"({len(files)} file{'s' if len(files) != 1 else ''}):\n"
        f"{result['stat']}\n\n"
        f"{diff_text}"
    )
    return {"content": [{"type": "text", "text": text}]}


@tool(
    "run_creator_tests",
    "Run the creator test suite (pytest tests/test_creators/). The safety "
    "gate for code self-edits — commit_code_changes calls this by default "
    "and blocks the commit on failure. Suite runs in <2s normally. "
    "Optionally filter via filter_pattern (pytest -k).",
    {
        "filter_pattern": str,    # optional pytest -k filter
        "timeout_seconds": int,
    },
)
async def run_creator_tests_tool(args: dict) -> dict:
    filter_pattern = (args.get("filter_pattern") or "").strip() or None
    timeout = int(args.get("timeout_seconds") or 60)
    try:
        result = run_creator_tests(
            filter_pattern=filter_pattern,
            timeout_seconds=timeout,
        )
    except Exception as e:
        return {"content": [{"type": "text", "text": (
            f"test runner failed: {type(e).__name__}: {e}"
        )}]}

    status = result["status"]
    text_parts = [
        f"Tests: {status.upper()}",
        f"  {result['summary']}",
        f"  passed={result.get('passed_count',0)} "
        f"failed={result.get('failed_count',0)} "
        f"errors={result.get('error_count',0)} "
        f"duration={result.get('duration_seconds',0):.2f}s",
    ]
    if result.get("failed_tests"):
        text_parts.append("\nFailed tests:")
        for t in result["failed_tests"][:10]:
            text_parts.append(f"  - {t}")
        if len(result["failed_tests"]) > 10:
            text_parts.append(f"  ... and {len(result['failed_tests']) - 10} more")
    if status != "passed":
        # Include tail of pytest output for diagnostic context
        text_parts.append("\nOutput (tail):")
        text_parts.append(result.get("output", "")[-2000:])
    return {"content": [{"type": "text", "text": "\n".join(text_parts)}]}


@tool(
    "commit_code_changes",
    "Stage + (run tests) + commit + push code changes. Scoped to the "
    "code allowlist; will NOT touch kit edits or other working-tree "
    "changes. By default runs the test suite and blocks the commit on "
    "failure. The autonomy system may additionally require human "
    "approval before this tool runs.",
    {
        "message": str,             # required
        "paths": str,               # comma-separated, optional
        "require_tests": bool,      # default True
        "push": bool,               # default True
    },
)
async def commit_code_changes_tool(args: dict) -> dict:
    message = (args.get("message") or "").strip()
    paths_csv = (args.get("paths") or "").strip()
    paths = [p.strip() for p in paths_csv.split(",") if p.strip()] if paths_csv else None
    require_tests = bool(args.get("require_tests", True))
    push = bool(args.get("push", True))
    if not message:
        return {"content": [{"type": "text", "text": (
            "commit_code_changes requires: message"
        )}]}
    try:
        result = commit_code_changes(
            message=message, paths=paths,
            require_tests=require_tests, push=push,
        )
    except Exception as e:
        return {"content": [{"type": "text", "text": (
            f"commit failed: {type(e).__name__}: {e}"
        )}]}

    status = result["status"]
    if status == "no_changes":
        return {"content": [{"type": "text", "text": (
            "No code changes to commit"
        )}]}
    if status == "denied":
        return {"content": [{"type": "text", "text": (
            f"DENIED: {result.get('reason','')}"
        )}]}
    if status == "tests_failed":
        tr = result["test_result"]
        text = [
            "TESTS FAILED — commit blocked.",
            f"  passed={tr.get('passed_count',0)} "
            f"failed={tr.get('failed_count',0)} "
            f"errors={tr.get('error_count',0)}",
            "Failed:",
        ]
        for t in tr.get("failed_tests", [])[:10]:
            text.append(f"  - {t}")
        text.append("\n" + result.get("hint", ""))
        return {"content": [{"type": "text", "text": "\n".join(text)}]}
    if status == "git_error":
        return {"content": [{"type": "text", "text": (
            f"git error: {result.get('stderr','')[:300]}"
        )}]}

    files = result.get("files_changed", []) or []
    text = [
        f"Code change committed: {result.get('commit_hash','?')}",
        f"  Pushed: {result.get('pushed', False)}",
        f"  Files ({len(files)}): {files[:8]}{'...' if len(files) > 8 else ''}",
    ]
    if result.get("test_result"):
        tr = result["test_result"]
        text.append(
            f"  Tests: {tr.get('passed_count',0)} passed in "
            f"{tr.get('duration_seconds',0):.2f}s"
        )
    if result.get("push_error"):
        text.append(f"  Push error: {result['push_error'][:200]}")
    return {"content": [{"type": "text", "text": "\n".join(text)}]}


@tool(
    "revert_last_code_commit",
    "Revert the most recent commit IF it was a code self-edit (touched "
    "only files in the code allowlist). The one-call undo for when a "
    "code change broke production. Refuses to revert commits that "
    "touched files outside the allowlist — those need git directly.",
    {"push": bool},
)
async def revert_last_code_commit_tool(args: dict) -> dict:
    push = bool(args.get("push", True))
    try:
        result = revert_last_code_commit(push=push)
    except Exception as e:
        return {"content": [{"type": "text", "text": (
            f"revert failed: {type(e).__name__}: {e}"
        )}]}

    status = result["status"]
    if status == "not_self_edit":
        return {"content": [{"type": "text", "text": (
            f"Refused: last commit touched files outside the code "
            f"allowlist:\n  {result.get('out_of_scope_files', [])}\n"
            "Use git directly if intentional."
        )}]}
    if status == "no_commits":
        return {"content": [{"type": "text", "text": (
            "No commits to revert (HEAD is empty or root)"
        )}]}
    if status == "git_error":
        return {"content": [{"type": "text", "text": (
            f"git error: {result.get('stderr','')[:300]}"
        )}]}

    text = [
        f"Reverted: {result.get('reverted_hash','?')} → {result.get('revert_hash','?')}",
        f"  Pushed: {result.get('pushed', False)}",
        f"  Files reverted: {result.get('files_reverted', [])}",
    ]
    if result.get("push_error"):
        text.append(f"  Push error: {result['push_error'][:200]}")
    return {"content": [{"type": "text", "text": "\n".join(text)}]}


CODE_EDITOR_TOOLS = [
    read_astra_file_tool,
    list_astra_files_tool,
    edit_astra_file_tool,
    write_astra_file_tool,
    show_astra_diff_tool,
    run_creator_tests_tool,
    commit_code_changes_tool,
    revert_last_code_commit_tool,
]
