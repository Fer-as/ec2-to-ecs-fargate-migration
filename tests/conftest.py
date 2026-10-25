"""Shared fixtures for the legacy API tests."""

import copy
import importlib.util
import os
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"

# The tests assert the default metadata, so make sure the host environment
# cannot override it before the module reads these variables at import time.
for _var in ("APP_NAME", "APP_VERSION", "ENVIRONMENT"):
    os.environ.pop(_var, None)

# Load app/app.py by path so the module is not confused with the app/ folder.
_spec = importlib.util.spec_from_file_location("legacy_app", APP_DIR / "app.py")
legacy_app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(legacy_app)

INITIAL_PRODUCTS = copy.deepcopy(legacy_app.products_db)


def reset_state():
    """Put the in-memory stores back to their startup values."""
    legacy_app.products_db.clear()
    legacy_app.products_db.update(copy.deepcopy(INITIAL_PRODUCTS))
    legacy_app.orders_db.clear()
    legacy_app.order_counter = 1


@pytest.fixture(autouse=True)
def _fresh_state():
    reset_state()
    yield
    reset_state()


@pytest.fixture
def client():
    # TESTING is deliberately left off: it would make Flask re-raise errors
    # instead of returning the 500 responses production clients receive.
    return legacy_app.app.test_client()


def known_defect(reason):
    """Mark a test that asserts the *correct* behaviour of a known defect.

    It is expected to fail today. Once the defect is fixed it will XPASS,
    which fails the suite (strict) so the marker gets removed and the test
    becomes a normal regression test.
    """
    def decorate(func):
        func = pytest.mark.known_defect(func)
        return pytest.mark.xfail(strict=True, raises=AssertionError, reason=reason)(func)
    return decorate
