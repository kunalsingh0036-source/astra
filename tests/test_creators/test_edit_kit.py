"""
Unit tests for astra/creators/edit_kit.py — Layer 1 self-modification.

The MOST safety-critical tests in the creator stack. The edit_kit
primitives are how Astra modifies brand kits autonomously; bugs here
can corrupt kits or — worse — break the git-scoping invariant that
prevents Astra from accidentally committing code via the kit-edit
path.

Tests organized by primitive:
- find_section_indices / append_to_section / list_sections
- brand.yml round-trip
- High-level edit operations (add_forbidden_phrase, etc.)
- Git scoping (commit_kit) — invariant that ONLY business-kits/<slug>/
  files can be committed via this path
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml


# ── Markdown section primitives ─────────────────────────────────────


class TestFindSectionIndices:
    def test_finds_simple_section(self):
        from astra.creators.edit_kit import find_section_indices

        md = "# Title\n\n## Section A\n\nBody A\n\n## Section B\n\nBody B\n"
        indices = find_section_indices(md, r"Section A")
        assert indices is not None
        start, end = indices
        body = md[start:end]
        assert "Body A" in body
        assert "Body B" not in body

    def test_returns_none_for_missing_section(self):
        from astra.creators.edit_kit import find_section_indices

        md = "## Section A\nBody\n"
        assert find_section_indices(md, r"Nonexistent") is None

    def test_case_insensitive_match(self):
        from astra.creators.edit_kit import find_section_indices

        md = "## NEVER Uses\n\nBody\n"
        # Pattern is case-insensitive by default
        assert find_section_indices(md, r"never\s+uses") is not None

    def test_last_section_extends_to_end(self):
        from astra.creators.edit_kit import find_section_indices

        md = "## First\n\nBody1\n\n## Last\n\nBody last\n"
        indices = find_section_indices(md, r"Last")
        assert indices is not None
        start, end = indices
        assert end == len(md)

    def test_does_not_match_wrong_level(self):
        """find at level=2 should not return level=3 sections by default."""
        from astra.creators.edit_kit import find_section_indices

        md = "## Section\nBody\n### Subsection\nSub body\n"
        # At level 2, "Subsection" should not match
        assert find_section_indices(md, r"^Subsection$", level=2) is None


class TestAppendToSection:
    def test_appends_to_existing_section(self):
        from astra.creators.edit_kit import append_to_section

        md = "## Forbidden\n\n- existing\n\n## Other\n\nbody\n"
        new_md, status = append_to_section(md, r"Forbidden", "- new\n")
        assert status == "appended"
        assert "- existing" in new_md
        assert "- new" in new_md
        # New entry comes before the next section
        forbidden_idx = new_md.find("- new")
        other_idx = new_md.find("## Other")
        assert forbidden_idx < other_idx

    def test_creates_section_when_missing_and_allowed(self):
        from astra.creators.edit_kit import append_to_section

        md = "# Title\n\nIntro\n"
        new_md, status = append_to_section(
            md, r"Brand New",
            "- first entry\n",
            create_if_missing=("Brand New", 2),
        )
        assert status == "appended_created"
        assert "## Brand New" in new_md
        assert "- first entry" in new_md

    def test_returns_section_not_found_when_not_creating(self):
        from astra.creators.edit_kit import append_to_section

        md = "# Title\n\nIntro\n"
        new_md, status = append_to_section(md, r"Missing", "- x\n")
        assert status == "section_not_found"
        assert new_md == md  # unchanged

    def test_no_blank_line_accumulation(self):
        """Multiple appends shouldn't accumulate blank lines."""
        from astra.creators.edit_kit import append_to_section

        md = "## Section\n\n- one\n\n"
        for _ in range(5):
            md, _ = append_to_section(md, r"Section", "- more\n")
        # Should not have 5+ blank lines anywhere
        assert "\n\n\n\n" not in md

    def test_idempotent_section_creation(self):
        """If a section was created, calling append again finds it
        rather than creating a duplicate."""
        from astra.creators.edit_kit import append_to_section

        md = "# Title\n"
        md, _ = append_to_section(
            md, r"New Section",
            "- first\n",
            create_if_missing=("New Section", 2),
        )
        md, status = append_to_section(
            md, r"New Section",
            "- second\n",
            create_if_missing=("New Section", 2),
        )
        # Second call should append, not create
        assert status == "appended"
        # Only ONE "## New Section" heading exists
        assert md.count("## New Section") == 1


class TestListSections:
    def test_lists_level_2_only(self):
        from astra.creators.edit_kit import list_sections

        md = (
            "# Title\n## A\n## B\n### Sub\n## C\n"
        )
        sections = list_sections(md, level=2)
        assert sections == ["A", "B", "C"]

    def test_returns_empty_for_no_sections(self):
        from astra.creators.edit_kit import list_sections

        assert list_sections("# Title only\n", level=2) == []


# ── brand.yml round-trip ────────────────────────────────────────────


class TestBrandYmlRoundTrip:
    def test_load_and_save_preserves_data(self, tmp_kit_dir):
        from astra.creators.edit_kit import KitPaths, load_brand_yml, save_brand_yml
        from tests.test_creators.conftest import TEST_KIT_SLUG

        paths = KitPaths.for_slug(TEST_KIT_SLUG)
        original = load_brand_yml(paths)

        # Mutate
        original["forbidden_phrases"].append("buzzword")
        save_brand_yml(paths, original)

        # Reload
        reloaded = load_brand_yml(paths)
        assert "buzzword" in reloaded["forbidden_phrases"]
        # Sanity-check existing structure preserved
        assert reloaded["name"] == "TestCo"
        assert reloaded["brand"]["colors"]["primary"] == "#0F1C2E"

    def test_save_preserves_header_comments(self, tmp_kit_dir):
        """The original brand.yml has documentation comments at the top
        (sources, decisions). save_brand_yml must preserve them or kits
        lose their context after every self-edit."""
        from astra.creators.edit_kit import KitPaths, load_brand_yml, save_brand_yml
        from tests.test_creators.conftest import TEST_KIT_SLUG

        paths = KitPaths.for_slug(TEST_KIT_SLUG)
        # The fixture brand.yml starts with `# Test fixture kit — TestCo`
        text_before = paths.brand_yml.read_text()
        assert text_before.startswith("# Test fixture kit")

        # Round-trip
        data = load_brand_yml(paths)
        save_brand_yml(paths, data)

        text_after = paths.brand_yml.read_text()
        assert text_after.startswith("# Test fixture kit")


# ── add_forbidden_phrase (high-level) ───────────────────────────────


class TestAddForbiddenPhrase:
    def test_adds_to_brand_yml(self, tmp_kit_dir):
        from astra.creators.edit_kit import add_forbidden_phrase, KitPaths
        from tests.test_creators.conftest import TEST_KIT_SLUG

        result = add_forbidden_phrase(
            TEST_KIT_SLUG, "moonshot", auto_commit=False
        )
        assert result["status"] == "added"
        assert "added_to_brand_yml" in result["actions"]

        paths = KitPaths.for_slug(TEST_KIT_SLUG)
        data = yaml.safe_load(paths.brand_yml.read_text())
        assert "moonshot" in data["forbidden_phrases"]

    def test_adds_to_voice_md(self, tmp_kit_dir):
        from astra.creators.edit_kit import add_forbidden_phrase, KitPaths
        from tests.test_creators.conftest import TEST_KIT_SLUG

        add_forbidden_phrase(
            TEST_KIT_SLUG, "moonshot",
            rationale="vague",
            auto_commit=False,
        )
        paths = KitPaths.for_slug(TEST_KIT_SLUG)
        voice = paths.voice_md.read_text()
        assert "moonshot" in voice
        # Rationale appears in the bullet
        assert "vague" in voice

    def test_idempotent_on_repeat_call(self, tmp_kit_dir):
        from astra.creators.edit_kit import add_forbidden_phrase, KitPaths
        from tests.test_creators.conftest import TEST_KIT_SLUG

        # First call adds; second call should be no-op
        result1 = add_forbidden_phrase(TEST_KIT_SLUG, "moonshot", auto_commit=False)
        result2 = add_forbidden_phrase(TEST_KIT_SLUG, "moonshot", auto_commit=False)
        assert result1["status"] == "added"
        assert result2["status"] == "already_present"

        # And the phrase appears exactly once in brand.yml
        paths = KitPaths.for_slug(TEST_KIT_SLUG)
        data = yaml.safe_load(paths.brand_yml.read_text())
        assert data["forbidden_phrases"].count("moonshot") == 1

    def test_empty_phrase_raises(self, tmp_kit_dir):
        from astra.creators.edit_kit import add_forbidden_phrase
        from tests.test_creators.conftest import TEST_KIT_SLUG

        with pytest.raises(ValueError):
            add_forbidden_phrase(TEST_KIT_SLUG, "  ", auto_commit=False)

    def test_unknown_slug_raises(self, test_kits_dir):
        from astra.creators.edit_kit import add_forbidden_phrase

        with pytest.raises(FileNotFoundError):
            add_forbidden_phrase(
                "does-not-exist", "moonshot", auto_commit=False,
            )


# ── add_voice_note ──────────────────────────────────────────────────


class TestAddVoiceNote:
    @pytest.mark.parametrize("kind", ["does", "never", "sample"])
    def test_appends_to_correct_section(self, tmp_kit_dir, kind):
        from astra.creators.edit_kit import add_voice_note, KitPaths
        from tests.test_creators.conftest import TEST_KIT_SLUG

        # Make the content unique per kind so we can grep for it
        content = f"unique-content-{kind}"
        result = add_voice_note(
            TEST_KIT_SLUG, kind=kind, content=content, auto_commit=False,
        )
        # status is 'appended' or 'appended_created'
        assert result["status"] in ("appended", "appended_created")

        paths = KitPaths.for_slug(TEST_KIT_SLUG)
        voice = paths.voice_md.read_text()
        assert content in voice

    def test_invalid_kind_raises(self, tmp_kit_dir):
        from astra.creators.edit_kit import add_voice_note
        from tests.test_creators.conftest import TEST_KIT_SLUG

        with pytest.raises(ValueError, match="kind"):
            add_voice_note(
                TEST_KIT_SLUG, kind="invalid", content="x",
                auto_commit=False,
            )


# ── add_proof_point ─────────────────────────────────────────────────


class TestAddProofPoint:
    def test_adds_to_named_section(self, tmp_kit_dir):
        from astra.creators.edit_kit import add_proof_point, KitPaths
        from tests.test_creators.conftest import TEST_KIT_SLUG

        result = add_proof_point(
            TEST_KIT_SLUG,
            section="traction",
            content="- New metric: 42 widgets shipped (as of 2026-05)",
            auto_commit=False,
        )
        assert result["status"] == "appended"

        paths = KitPaths.for_slug(TEST_KIT_SLUG)
        proof = paths.proof_points_md.read_text()
        assert "42 widgets shipped" in proof
        # Should land in the Traction section, not somewhere else
        traction_start = proof.index("Traction")
        next_section_start = proof.index("Team")
        between = proof[traction_start:next_section_start]
        assert "42 widgets shipped" in between

    def test_unknown_section_returns_error(self, tmp_kit_dir):
        from astra.creators.edit_kit import add_proof_point
        from tests.test_creators.conftest import TEST_KIT_SLUG

        result = add_proof_point(
            TEST_KIT_SLUG,
            section="nonexistent_section",
            content="- whatever",
            auto_commit=False,
        )
        assert result["status"] == "section_not_found"
        # Available sections list helps the caller self-correct
        assert "available_sections" in result


# ── add_audience_objection ──────────────────────────────────────────


class TestAddAudienceObjection:
    def test_appends_objection_response(self, tmp_kit_dir):
        from astra.creators.edit_kit import add_audience_objection
        from tests.test_creators.conftest import TEST_KIT_SLUG

        result = add_audience_objection(
            TEST_KIT_SLUG,
            audience="test-audience",
            objection="A unique test objection",
            response="A specific test response",
            auto_commit=False,
        )
        assert result["status"] in ("appended", "appended_created")

        kits_root, kit_dir = tmp_kit_dir
        aud_md = (kit_dir / "audiences" / "test-audience.md").read_text()
        assert "A unique test objection" in aud_md
        assert "A specific test response" in aud_md

    def test_unknown_audience_returns_available_list(self, tmp_kit_dir):
        from astra.creators.edit_kit import add_audience_objection
        from tests.test_creators.conftest import TEST_KIT_SLUG

        result = add_audience_objection(
            TEST_KIT_SLUG,
            audience="does-not-exist",
            objection="x",
            response="y",
            auto_commit=False,
        )
        assert result["status"] == "audience_not_found"
        assert "test-audience" in result["available_audiences"]


# ── Git scoping invariant ───────────────────────────────────────────


class TestGitScopingInvariant:
    """The most safety-critical tests in the suite.

    commit_kit() must NEVER commit files outside business-kits/<slug>/.
    If this invariant breaks, Astra could accidentally commit code
    changes via the kit-edit path. Verified end-to-end with a real
    git repo in tmp_path.
    """

    @pytest.fixture
    def real_git_repo(self, tmp_path, monkeypatch):
        """Create a real git repo with a kits dir and a code file.

        We commit_kit in this repo and assert the code file remains
        unstaged afterward.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        # Set up git
        subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.invalid"],
            cwd=str(repo), check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=str(repo), check=True,
        )

        # Lay down a kits dir with the test fixture
        import shutil
        from tests.test_creators.conftest import _FIXTURE_KIT_PATH, TEST_KIT_SLUG

        kits_root = repo / "business-kits"
        kits_root.mkdir()
        shutil.copytree(_FIXTURE_KIT_PATH, kits_root / TEST_KIT_SLUG)

        # And a non-kit file (simulating Astra's code) — this is what
        # MUST NOT be committed via commit_kit.
        code_dir = repo / "astra" / "creators"
        code_dir.mkdir(parents=True)
        (code_dir / "draft.py").write_text("# code that should not be committed via kit-edit\n")

        # Make an initial commit so we have a HEAD
        subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "initial"],
            cwd=str(repo), check=True,
        )

        monkeypatch.setenv("BUSINESS_KITS_DIR", str(kits_root))
        return repo

    def test_commit_kit_only_commits_kit_files(self, real_git_repo, monkeypatch):
        """The invariant: commit_kit() stages only files under
        business-kits/<slug>/. Modify a kit file AND a code file;
        commit_kit should commit only the kit file."""
        from astra.creators.edit_kit import commit_kit
        from tests.test_creators.conftest import TEST_KIT_SLUG

        repo = real_git_repo
        # Modify the brand.yml in the kit
        brand_yml = repo / "business-kits" / TEST_KIT_SLUG / "brand.yml"
        text = brand_yml.read_text()
        brand_yml.write_text(text + "\n# kit-edit\n")

        # Modify the code file too — this should NOT be committed
        code_file = repo / "astra" / "creators" / "draft.py"
        code_file.write_text("# CODE EDIT — must not be committed\n")

        # The commit_kit function uses _repo_root() which walks up from
        # the kits root. Patch _kits_root to point at our test repo's
        # kits dir.
        monkeypatch.setattr(
            "astra.creators.edit_kit._kits_root",
            lambda: repo / "business-kits",
        )

        # Run commit, push=False so we don't try to talk to a remote
        result = commit_kit(TEST_KIT_SLUG, message="kit edit", push=False)
        assert result["status"] == "committed"

        # Now verify: the kit file IS committed
        committed_files = subprocess.run(
            ["git", "diff", "--name-only", "HEAD~1", "HEAD"],
            cwd=str(repo), capture_output=True, text=True, check=True,
        ).stdout.strip().splitlines()
        assert any("brand.yml" in f for f in committed_files)

        # And the code file is NOT committed — it's still modified in
        # the working tree
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo), capture_output=True, text=True, check=True,
        ).stdout
        assert "astra/creators/draft.py" in status
        # Specifically: unstaged modification (M not staged) — leading space
        assert any(
            ln.startswith(" M") and "draft.py" in ln
            for ln in status.splitlines()
        )

    def test_commit_kit_with_no_changes_returns_no_changes(self, real_git_repo, monkeypatch):
        from astra.creators.edit_kit import commit_kit
        from tests.test_creators.conftest import TEST_KIT_SLUG

        monkeypatch.setattr(
            "astra.creators.edit_kit._kits_root",
            lambda: real_git_repo / "business-kits",
        )

        # Nothing has been modified — commit_kit should detect that
        result = commit_kit(TEST_KIT_SLUG, push=False)
        assert result["status"] == "no_changes"
