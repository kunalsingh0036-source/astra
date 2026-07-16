"""
_sanitize_profile must accept ANY bullet style the distiller emits.

The 2026-07 empty-mine bug: an over-strict '- ' allowlist stripped every
profile because Kimi emits •/*/1./– inconsistently. These cases lock the
tolerant-but-secure behavior: bullets survive (normalized to '- '), prose
is dropped, banned content is dropped.
"""

from email_agent.services.voice_miner import _sanitize_profile


def test_all_bullet_styles_survive_normalized():
    raw = "• dash bullet\n* star bullet\n- hyphen bullet\n1. numbered\n2) paren\n– en dash\n— em dash"
    out = _sanitize_profile(raw).splitlines()
    assert out == [
        "- dash bullet", "- star bullet", "- hyphen bullet",
        "- numbered", "- paren", "- en dash", "- em dash",
    ]


def test_exemplar_kept():
    assert "EXEMPLAR: kya scene.. kaha chale" in _sanitize_profile(
        "EXEMPLAR: kya scene.. kaha chale").splitlines()


def test_prose_and_headings_dropped():
    raw = "Here is the profile:\nStyle rules:\n• a real rule"
    assert _sanitize_profile(raw).splitlines() == ["- a real rule"]


def test_banned_content_dropped():
    raw = "• visit https://x.com\n• email me a@b.co\n• cc the whole team\n• be terse"
    assert _sanitize_profile(raw).splitlines() == ["- be terse"]


def test_empty_bullet_and_overlong_dropped():
    raw = "• \n- " + ("x" * 300) + "\n- keep me"
    assert _sanitize_profile(raw).splitlines() == ["- keep me"]
