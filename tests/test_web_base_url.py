"""web_base_url(): the localhost default must never reach a Railway link.

astra_web_base_url defaults to http://localhost:3000, and
ASTRA_WEB_BASE_URL was unset on every Railway service when Phase A5
shipped, so every ASK result in production sent Kunal to
http://localhost:3000/approvals. The helper is the one place that
resolves the base for links: on Railway a localhost or empty base
falls back to the canonical host and logs an error once; off Railway
the configured value is returned untouched so a local install keeps
working.
"""

from __future__ import annotations

import logging

import pytest

from astra import config
from astra.config import ASTRA_WEB_CANONICAL_URL, settings, web_base_url

DEFAULT = config.Settings.model_fields["astra_web_base_url"].default


@pytest.fixture
def railway(monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    monkeypatch.setenv("RAILWAY_SERVICE_NAME", "astra-stream")
    monkeypatch.setattr(config, "_web_base_url_fallback_logged", False)


@pytest.fixture
def laptop(monkeypatch):
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    monkeypatch.delenv("RAILWAY_SERVICE_NAME", raising=False)


def test_the_setting_default_is_a_dev_only_value():
    """The premise of the helper: the default is not a public host."""
    assert "localhost" in DEFAULT


@pytest.mark.parametrize(
    "base",
    [
        DEFAULT,
        "http://localhost:3000/",
        "http://127.0.0.1:3000",
        "localhost:3000",
        "",
        "   ",
    ],
)
def test_on_railway_a_local_or_empty_base_falls_back_loudly(
    railway, monkeypatch, caplog, base
):
    monkeypatch.setattr(settings, "astra_web_base_url", base)
    with caplog.at_level(logging.ERROR, logger="astra.config"):
        url = web_base_url()
    assert url == ASTRA_WEB_CANONICAL_URL
    assert "localhost" not in url and "127.0.0.1" not in url
    assert any(
        r.levelno == logging.ERROR and "ASTRA_WEB_BASE_URL" in r.getMessage()
        for r in caplog.records
    ), "the fallback must be logged as an error, not applied silently"


def test_on_railway_the_error_is_logged_once(railway, monkeypatch, caplog):
    monkeypatch.setattr(settings, "astra_web_base_url", DEFAULT)
    with caplog.at_level(logging.ERROR, logger="astra.config"):
        web_base_url()
        web_base_url()
    assert sum(1 for r in caplog.records if r.levelno == logging.ERROR) == 1


def test_on_railway_a_configured_base_is_used_as_is(
    railway, monkeypatch, caplog
):
    monkeypatch.setattr(
        settings, "astra_web_base_url", "https://astra.example.com/"
    )
    with caplog.at_level(logging.ERROR, logger="astra.config"):
        assert web_base_url() == "https://astra.example.com"
    assert not caplog.records


def test_railway_service_name_alone_marks_railway(monkeypatch):
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    monkeypatch.setenv("RAILWAY_SERVICE_NAME", "astra-scheduler")
    monkeypatch.setattr(config, "_web_base_url_fallback_logged", True)
    monkeypatch.setattr(settings, "astra_web_base_url", DEFAULT)
    assert web_base_url() == ASTRA_WEB_CANONICAL_URL


def test_off_railway_the_default_is_returned_unchanged(laptop, monkeypatch):
    """Local dev links stay local; the fallback is a Railway rule."""
    monkeypatch.setattr(settings, "astra_web_base_url", DEFAULT)
    assert web_base_url() == "http://localhost:3000"


def test_ask_result_link_never_points_at_localhost_on_railway(
    railway, monkeypatch
):
    """The link the model hands Kunal in every ASK result."""
    from astra.runtime.agent_loop import _approvals_url

    monkeypatch.setattr(settings, "astra_web_base_url", DEFAULT)
    assert _approvals_url() == f"{ASTRA_WEB_CANONICAL_URL}/approvals"
