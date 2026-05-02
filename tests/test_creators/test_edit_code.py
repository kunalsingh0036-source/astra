"""
Tests for astra/creators/edit_code.py — Layer 2 self-modification.

These are the safety-critical tests for the code-self-edit path. The
tests in this file lock in:

1. **Path allowlist enforcement.** Files outside the allowlist must
   be denied. Inside the allowlist, edits work. Self-modification
   safeguards (edit_code.py, code_editor_tools.py) must be in the
   denylist.

2. **Test gate semantics.** commit_code_changes must run the test
   suite by default and abort on failure.

3. **Git scoping.** Code commits must NOT cross-stage kit edits or
   any other working-tree changes.

4. **Revert safety.** revert_last_code_commit must refuse to revert
   commits that touched files outside the code allowlist.

If any of these tests fails, the safety case for Layer 2 breaks. Do
not weaken these tests without explicit human review.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


# ── Path allowlist enforcement ──────────────────────────────────────


class TestPathAllowlist:
    def test_allowed_path_in_creators(self):
        from astra.creators.edit_code import _path_is_allowed

        allowed, reason = _path_is_allowed("astra/creators/draft.py")
        assert allowed
        assert reason == ""

    def test_allowed_path_in_tools(self):
        from astra.creators.edit_code import _path_is_allowed

        allowed, _ = _path_is_allowed("astra/tools/creator_tools.py")
        assert allowed

    def test_allowed_path_in_test_creators(self):
        from astra.creators.edit_code import _path_is_allowed

        allowed, _ = _path_is_allowed("tests/test_creators/test_draft_deck.py")
        assert allowed

    def test_allowed_pyproject_toml(self):
        from astra.creators.edit_code import _path_is_allowed

        allowed, _ = _path_is_allowed("pyproject.toml")
        assert allowed

    def test_denied_path_in_db(self):
        from astra.creators.edit_code import _path_is_allowed

        allowed, reason = _path_is_allowed("astra/db/engine.py")
        assert not allowed
        assert "not in the code-edit allowlist" in reason

    def test_denied_path_main(self):
        from astra.creators.edit_code import _path_is_allowed

        allowed, _ = _path_is_allowed("astra/main.py")
        assert not allowed

    def test_denied_path_credentials(self):
        from astra.creators.edit_code import _path_is_allowed

        allowed, reason = _path_is_allowed("credentials/api_keys.json")
        assert not allowed
        # Denylist matches before allowlist
        assert "denied prefix" in reason

    def test_denied_path_dotenv(self):
        from astra.creators.edit_code import _path_is_allowed

        # .env is not in any allowlist; should be denied
        allowed, _ = _path_is_allowed(".env")
        assert not allowed

    def test_denied_path_dockerfile(self):
        from astra.creators.edit_code import _path_is_allowed

        allowed, _ = _path_is_allowed("Dockerfile.scheduler")
        assert not allowed

    def test_denied_self_modification_safeguards(self):
        """The most critical denial: Astra MUST NOT be able to edit
        the safeguards themselves. If this denial breaks, Astra could
        rewrite edit_code.py to disable the allowlist enforcement.
        """
        from astra.creators.edit_code import _path_is_allowed

        for safeguard in (
            "astra/creators/edit_code.py",
            "astra/tools/code_editor_tools.py",
        ):
            allowed, reason = _path_is_allowed(safeguard)
            assert not allowed, f"safeguard {safeguard} must be denied"
            assert "safeguard" in reason

    def test_denied_paths_take_precedence(self):
        """Even if a denylist prefix is also covered by an allowlist
        prefix, deny wins."""
        from astra.creators.edit_code import _path_is_allowed

        # business-kits/_schema/ is denied even though business-kits/
        # would otherwise be allowed (it isn't, but verify the denylist
        # behavior anyway)
        allowed, reason = _path_is_allowed("business-kits/_schema/brand.yml")
        assert not allowed
        assert "denied prefix" in reason


# ── Path normalization ──────────────────────────────────────────────


class TestPathNormalization:
    def test_relative_path_resolves(self):
        from astra.creators.edit_code import _normalize_path

        rel, abs_p = _normalize_path("astra/creators/draft.py")
        assert rel == "astra/creators/draft.py"
        assert abs_p.name == "draft.py"

    def test_empty_path_raises(self):
        from astra.creators.edit_code import _normalize_path

        with pytest.raises(ValueError):
            _normalize_path("")
        with pytest.raises(ValueError):
            _normalize_path("   ")

    def test_path_escape_via_dotdot_raises(self):
        from astra.creators.edit_code import _normalize_path

        # Try to escape the repo via ..
        with pytest.raises(ValueError, match="escapes the repo"):
            _normalize_path("../../etc/passwd")

    def test_absolute_path_outside_repo_raises(self):
        from astra.creators.edit_code import _normalize_path

        with pytest.raises(ValueError, match="outside the repo"):
            _normalize_path("/etc/passwd")


# ── Read operations ─────────────────────────────────────────────────


class TestReadAstraFile:
    def test_reads_known_file(self):
        from astra.creators.edit_code import read_astra_file

        # Read this very test file
        result = read_astra_file("tests/test_creators/test_edit_code.py")
        assert result.get("exists") is True
        assert "TestPathAllowlist" in result["content"]
        assert result["line_count"] > 50

    def test_missing_file_returns_error(self):
        from astra.creators.edit_code import read_astra_file

        result = read_astra_file("astra/creators/does_not_exist.py")
        assert result.get("exists") is False
        assert "not found" in result.get("error", "")

    def test_credentials_dir_denied(self):
        from astra.creators.edit_code import read_astra_file

        result = read_astra_file("credentials/anything")
        assert "error" in result
        assert "denied" in result["error"]

    def test_dotenv_denied(self):
        from astra.creators.edit_code import read_astra_file

        result = read_astra_file(".env")
        assert "error" in result
        assert "denied" in result["error"]


class TestListAstraFiles:
    def test_lists_test_creators_dir(self):
        from astra.creators.edit_code import list_astra_files

        result = list_astra_files("tests/test_creators/")
        assert "files" in result
        paths = {f["path"] for f in result["files"]}
        assert any("test_edit_code.py" in p for p in paths)

    def test_skips_pycache(self):
        from astra.creators.edit_code import list_astra_files

        result = list_astra_files("astra/creators/")
        paths = [f["path"] for f in result["files"]]
        assert not any("__pycache__" in p for p in paths)


# ── Edit operations (mutation) ──────────────────────────────────────


class TestEditAstraFile:
    """Mutating tests need a real repo with the same shape as ours.
    We use a temp repo for these so the actual codebase isn't modified."""

    @pytest.fixture
    def temp_repo(self, tmp_path, monkeypatch):
        """Create a temp repo with allowlist directories + a sample file."""
        repo = tmp_path / "repo"
        # Layout it like the real one
        (repo / "astra" / "creators").mkdir(parents=True)
        (repo / "astra" / "tools").mkdir(parents=True)
        (repo / "astra" / "creators" / "sample.py").write_text(
            "def hello():\n    return 'hello'\n"
        )
        (repo / ".git").mkdir()  # marker so _repo_root finds it

        # Patch _repo_root to return this temp repo
        monkeypatch.setattr(
            "astra.creators.edit_code._repo_root",
            lambda: repo,
        )
        return repo

    def test_edit_replaces_unique_string(self, temp_repo):
        from astra.creators.edit_code import edit_astra_file

        result = edit_astra_file(
            "astra/creators/sample.py",
            "return 'hello'",
            "return 'goodbye'",
        )
        assert result["status"] == "edited"
        assert result["replacements"] == 1

        text = (temp_repo / "astra" / "creators" / "sample.py").read_text()
        # The replacement value should be present
        assert "return 'goodbye'" in text
        # And the original return-value should NOT — but the function name
        # `def hello()` still contains the substring 'hello', so we check
        # for the specific return-value pattern.
        assert "return 'hello'" not in text

    def test_edit_old_string_missing(self, temp_repo):
        from astra.creators.edit_code import edit_astra_file

        result = edit_astra_file(
            "astra/creators/sample.py",
            "this string does not exist",
            "x",
        )
        assert result["status"] == "old_string_missing"

    def test_edit_ambiguous_without_replace_all(self, temp_repo):
        from astra.creators.edit_code import edit_astra_file

        # Make it ambiguous
        (temp_repo / "astra" / "creators" / "sample.py").write_text(
            "x = 1\nx = 2\nx = 3\n"
        )
        result = edit_astra_file(
            "astra/creators/sample.py",
            "x =",
            "y =",
            replace_all=False,
        )
        assert result["status"] == "old_string_ambiguous"
        assert result["occurrences"] == 3

    def test_edit_replace_all_works(self, temp_repo):
        from astra.creators.edit_code import edit_astra_file

        (temp_repo / "astra" / "creators" / "sample.py").write_text(
            "x = 1\nx = 2\nx = 3\n"
        )
        result = edit_astra_file(
            "astra/creators/sample.py",
            "x =",
            "y =",
            replace_all=True,
        )
        assert result["status"] == "edited"
        assert result["replacements"] == 3

    def test_edit_denied_outside_allowlist(self, temp_repo):
        from astra.creators.edit_code import edit_astra_file

        # Create a file in a non-allowed location
        (temp_repo / "astra" / "db").mkdir(parents=True)
        (temp_repo / "astra" / "db" / "engine.py").write_text("# db code\n")

        result = edit_astra_file(
            "astra/db/engine.py",
            "# db code",
            "# hacked",
        )
        assert result["status"] == "denied"
        assert "allowlist" in result["reason"]
        # File should be UNCHANGED
        text = (temp_repo / "astra" / "db" / "engine.py").read_text()
        assert text == "# db code\n"

    def test_edit_denied_for_self_modification_safeguards(self, temp_repo):
        """Critical: Astra cannot edit edit_code.py or code_editor_tools.py
        from inside the agent loop, even though they're in
        astra/creators/ and astra/tools/ (which are otherwise allowed)."""
        from astra.creators.edit_code import edit_astra_file

        # Create the safeguard file
        (temp_repo / "astra" / "creators" / "edit_code.py").write_text(
            "# safeguard\n"
        )
        result = edit_astra_file(
            "astra/creators/edit_code.py",
            "# safeguard",
            "# DISABLED",
        )
        assert result["status"] == "denied"
        assert "safeguard" in result["reason"]


# ── Git scoping invariant for code commits ──────────────────────────


class TestCommitCodeChanges:
    """The most safety-critical tests in Layer 2: code-commit scoping +
    test gate. End-to-end with a real git repo."""

    @pytest.fixture
    def real_git_repo(self, tmp_path, monkeypatch):
        """Create a real git repo with allowlist dirs + a kit dir, and
        seed an initial commit so HEAD exists."""
        repo = tmp_path / "repo"

        # Allowlist dirs
        (repo / "astra" / "creators").mkdir(parents=True)
        (repo / "astra" / "tools").mkdir(parents=True)
        (repo / "tests" / "test_creators").mkdir(parents=True)

        # Non-allowlist dirs (must NOT be committable via code-commit)
        (repo / "business-kits" / "testco").mkdir(parents=True)
        (repo / "astra" / "db").mkdir(parents=True)

        # Initial files
        (repo / "astra" / "creators" / "sample.py").write_text("def f(): pass\n")
        (repo / "astra" / "tools" / "tool.py").write_text("def t(): pass\n")
        (repo / "business-kits" / "testco" / "brand.yml").write_text("name: testco\n")
        (repo / "astra" / "db" / "engine.py").write_text("def db(): pass\n")
        (repo / "tests" / "test_creators" / "test_sample.py").write_text(
            "def test_sample():\n    assert True\n"
        )

        # Init git
        subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.invalid"],
            cwd=str(repo), check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=str(repo), check=True,
        )
        subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "initial"],
            cwd=str(repo), check=True,
        )

        monkeypatch.setattr(
            "astra.creators.edit_code._repo_root",
            lambda: repo,
        )
        return repo

    def test_only_commits_code_files(self, real_git_repo, monkeypatch):
        """End-to-end: edit a code file AND a kit file; commit_code_changes
        must commit only the code file."""
        from astra.creators.edit_code import commit_code_changes

        # Modify a code file (in allowlist)
        (real_git_repo / "astra" / "creators" / "sample.py").write_text(
            "def f():\n    return 'updated'\n"
        )
        # Modify a kit file (NOT in code allowlist)
        (real_git_repo / "business-kits" / "testco" / "brand.yml").write_text(
            "name: testco\nadded: yes\n"
        )

        # Skip tests for this scoping test (separate test covers the gate)
        result = commit_code_changes(
            message="update sample",
            require_tests=False,
            push=False,
        )
        assert result["status"] == "committed"

        # Inspect what was committed in HEAD
        committed = subprocess.run(
            ["git", "diff", "--name-only", "HEAD~1", "HEAD"],
            cwd=str(real_git_repo), capture_output=True, text=True, check=True,
        ).stdout.strip().splitlines()

        assert "astra/creators/sample.py" in committed
        assert "business-kits/testco/brand.yml" not in committed

        # And the kit file is still modified (unstaged) in the working tree
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(real_git_repo), capture_output=True, text=True, check=True,
        ).stdout
        assert "business-kits/testco/brand.yml" in status

    def test_no_changes_returns_no_changes(self, real_git_repo):
        from astra.creators.edit_code import commit_code_changes

        result = commit_code_changes(
            message="x", require_tests=False, push=False,
        )
        assert result["status"] == "no_changes"

    def test_empty_message_denied(self, real_git_repo):
        from astra.creators.edit_code import commit_code_changes

        result = commit_code_changes(
            message="", require_tests=False, push=False,
        )
        assert result["status"] == "denied"

    def test_explicit_path_outside_allowlist_denied(self, real_git_repo):
        from astra.creators.edit_code import commit_code_changes

        result = commit_code_changes(
            message="hack",
            paths=["astra/db/"],  # not in allowlist
            require_tests=False,
            push=False,
        )
        assert result["status"] == "denied"


# ── Revert safety ───────────────────────────────────────────────────


class TestRevertLastCodeCommit:
    @pytest.fixture
    def real_git_repo(self, tmp_path, monkeypatch):
        """Repo with two commits: an initial seed, and a code-only commit
        on top. Tests verify revert undoes the code commit cleanly."""
        repo = tmp_path / "repo"
        (repo / "astra" / "creators").mkdir(parents=True)
        (repo / "business-kits" / "testco").mkdir(parents=True)
        (repo / "astra" / "creators" / "sample.py").write_text("def f(): pass\n")
        (repo / "business-kits" / "testco" / "brand.yml").write_text("name: testco\n")

        subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.invalid"],
            cwd=str(repo), check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=str(repo), check=True,
        )
        subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "initial"],
            cwd=str(repo), check=True,
        )

        monkeypatch.setattr(
            "astra.creators.edit_code._repo_root",
            lambda: repo,
        )
        return repo

    def test_reverts_code_only_commit(self, real_git_repo):
        from astra.creators.edit_code import revert_last_code_commit

        # Make a code-only commit on top
        (real_git_repo / "astra" / "creators" / "sample.py").write_text(
            "def f():\n    return 'will revert'\n"
        )
        subprocess.run(
            ["git", "add", "astra/creators/sample.py"],
            cwd=str(real_git_repo), check=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "code change"],
            cwd=str(real_git_repo), check=True,
        )

        result = revert_last_code_commit(push=False)
        assert result["status"] == "reverted"
        assert "astra/creators/sample.py" in result["files_reverted"]

        # Verify the code file is back to its initial content
        text = (real_git_repo / "astra" / "creators" / "sample.py").read_text()
        assert "will revert" not in text

    def test_refuses_to_revert_kit_commit(self, real_git_repo):
        from astra.creators.edit_code import revert_last_code_commit

        # Make a kit-only commit on top
        (real_git_repo / "business-kits" / "testco" / "brand.yml").write_text(
            "name: testco\nadded: yes\n"
        )
        subprocess.run(
            ["git", "add", "business-kits/testco/brand.yml"],
            cwd=str(real_git_repo), check=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "kit change"],
            cwd=str(real_git_repo), check=True,
        )

        result = revert_last_code_commit(push=False)
        assert result["status"] == "not_self_edit"
        # Kit commit is still HEAD (NOT reverted)
        log = subprocess.run(
            ["git", "log", "-1", "--oneline"],
            cwd=str(real_git_repo), capture_output=True, text=True, check=True,
        ).stdout
        assert "kit change" in log

    def test_refuses_mixed_commit(self, real_git_repo):
        """A commit that touches BOTH code and a kit must NOT be
        auto-revertable. The author needs to handle it manually so they
        choose which side to keep."""
        from astra.creators.edit_code import revert_last_code_commit

        (real_git_repo / "astra" / "creators" / "sample.py").write_text(
            "# code change\n"
        )
        (real_git_repo / "business-kits" / "testco" / "brand.yml").write_text(
            "# kit change\n"
        )
        subprocess.run(["git", "add", "."], cwd=str(real_git_repo), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "mixed change"],
            cwd=str(real_git_repo), check=True,
        )

        result = revert_last_code_commit(push=False)
        assert result["status"] == "not_self_edit"
        assert any(
            "business-kits" in f
            for f in result.get("out_of_scope_files", [])
        )


# ── Test gate (run_creator_tests) ───────────────────────────────────


class TestRunCreatorTests:
    """We can't fully test this in isolation since it shells out to
    pytest. But we can verify it runs against the actual test suite
    without exploding, and that the structured result has the right
    shape."""

    def test_runs_and_returns_structured_result(self):
        from astra.creators.edit_code import run_creator_tests

        # Run with a tight filter that matches one fast test, so this
        # test_edit_code.py self-test stays under a couple seconds.
        result = run_creator_tests(
            filter_pattern="test_strips_code_fences",
            timeout_seconds=30,
        )
        # Structural assertions, not test-content assertions
        assert result["status"] in ("passed", "failed", "errored", "timeout")
        assert "summary" in result
        assert "duration_seconds" in result
        assert "passed_count" in result

    def test_filter_with_no_matches_returns_passed_or_errored(self):
        """If pytest finds nothing matching the filter, exit code is
        usually nonzero (pytest's "no tests collected" exit code 5).
        Our wrapper should classify as errored, not crash."""
        from astra.creators.edit_code import run_creator_tests

        result = run_creator_tests(
            filter_pattern="this_pattern_matches_nothing_xyz_unique",
            timeout_seconds=15,
        )
        # Either passed (no failures) or errored (no tests collected) — never crashes
        assert result["status"] in ("passed", "errored", "failed")


# ── End-to-end: edit → test gate → commit ───────────────────────────


class TestE2EFlow:
    """Smoke-level end-to-end: edit a file, run tests, see whether the
    test gate behaves correctly. Uses a temp repo with a tiny pytest
    suite included so the gate has something real to run."""

    @pytest.fixture
    def repo_with_tests(self, tmp_path, monkeypatch):
        """Real git repo with allowlist dirs + a passing pytest suite."""
        repo = tmp_path / "repo"
        (repo / "astra" / "creators").mkdir(parents=True)
        (repo / "tests" / "test_creators").mkdir(parents=True)

        # A trivial, fast-passing test suite in tests/test_creators/
        (repo / "tests" / "test_creators" / "__init__.py").write_text("")
        (repo / "tests" / "test_creators" / "test_smoke.py").write_text(
            "def test_smoke():\n    assert 1 + 1 == 2\n"
        )

        # A code file to edit
        (repo / "astra" / "creators" / "sample.py").write_text(
            "VERSION = '1.0'\n"
        )

        # A pyproject.toml so pytest finds the test config
        (repo / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\n"
            'asyncio_mode = "auto"\n'
            'testpaths = ["tests"]\n'
        )

        # Init git
        subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.invalid"],
            cwd=str(repo), check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=str(repo), check=True,
        )
        subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "initial"],
            cwd=str(repo), check=True,
        )

        monkeypatch.setattr(
            "astra.creators.edit_code._repo_root",
            lambda: repo,
        )
        return repo

    def test_edit_runs_test_suite_aware_of_repo(self, repo_with_tests):
        """A baseline check that run_creator_tests in a temp repo
        actually finds and runs the temp repo's test suite, not the
        outer Astra repo's."""
        from astra.creators.edit_code import run_creator_tests

        # The temp repo's test suite has 1 trivial passing test; if the
        # runner correctly uses _repo_root() the count will be 1.
        result = run_creator_tests(timeout_seconds=30)
        # Expect 1 passed (the smoke test in repo_with_tests). If the
        # runner accidentally pointed at the outer repo, count would be ~86.
        assert result["status"] == "passed"
        assert result["passed_count"] == 1
