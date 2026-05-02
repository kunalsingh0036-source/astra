"""Shared test fixtures for Astra test suite."""

import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"
