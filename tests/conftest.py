"""Test fixtures — fresh in-memory db per test."""

from __future__ import annotations

import pytest

from dlms_cli import store


@pytest.fixture
def conn():
    c = store.connect(":memory:")
    store.init_schema(c)
    try:
        yield c
    finally:
        c.close()
