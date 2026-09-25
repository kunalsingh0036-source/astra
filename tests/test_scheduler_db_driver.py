"""Scheduler construction must not depend on SQLAlchemy's default driver.

All URLs are synthetic. Engines are constructed but never connected; the
scheduler is never started and no database or external service is touched.
"""
import pytest
from sqlalchemy.engine import make_url

from astra.scheduler import app


@pytest.mark.parametrize("driver", ["postgresql", "postgresql+asyncpg", "postgresql+psycopg2"])
def test_sync_driver_is_explicit_and_preserves_url_fields(driver):
    # A global string replacement also corrupts passwords/query parameters.
    original = make_url(
        f"{driver}://test:p%40ss+asyncpg@127.0.0.1:1/synthetic"
        "?application_name=synthetic%2Basyncpg&sslmode=require"
    )
    actual = app._sync_jobstore_url(original.render_as_string(hide_password=False))
    assert actual == original.set(drivername="postgresql+psycopg2")
    assert actual.password == "p@ss+asyncpg"
    assert actual.query["application_name"] == "synthetic+asyncpg"


@pytest.mark.parametrize("url", [
    "sqlite:///:memory:",
    "postgresql+psycopg://test:synthetic@127.0.0.1:1/test",
])
def test_explicit_other_driver_or_backend_is_not_reinterpreted(url):
    assert app._sync_jobstore_url(url) == make_url(url)


@pytest.mark.parametrize("driver", ["postgresql", "postgresql+asyncpg"])
def test_real_jobstore_loads_declared_driver_without_connecting(monkeypatch, driver):
    monkeypatch.setattr(app.settings, "database_url", f"{driver}://test:synthetic@127.0.0.1:1/test")
    scheduler = app._build_scheduler()
    jobstore = scheduler._jobstores["default"]
    try:
        assert scheduler.running is False
        assert jobstore.engine.dialect.driver == "psycopg2"
        assert jobstore.engine.url.drivername == "postgresql+psycopg2"
        assert "broker_reap" in {job.id for job in scheduler.get_jobs()}
    finally:
        jobstore.engine.dispose()
