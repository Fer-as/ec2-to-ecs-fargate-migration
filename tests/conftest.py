"""Shared fixtures for the legacy API tests."""

import importlib.util
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))  # app.py imports storage.py from the same folder

# The tests assert the default metadata and the default (memory) backend, so
# the host environment must not override them before the module is imported.
for _var in ("APP_NAME", "APP_VERSION", "ENVIRONMENT", "STORAGE_BACKEND",
             "DYNAMODB_TABLE_NAME", "DYNAMODB_ENDPOINT_URL", "AWS_DEFAULT_REGION",
             "AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
    os.environ.pop(_var, None)

# Never read the developer's real AWS config or credentials (tests must not be able
# to reach a real account, and local profiles must not change test behaviour).
_no_aws_config = str(Path(__file__).resolve().parent / ".no-aws-config-for-tests")
os.environ["AWS_CONFIG_FILE"] = _no_aws_config
os.environ["AWS_SHARED_CREDENTIALS_FILE"] = _no_aws_config

_loaded = 0


def load_app_module():
    """Import app/app.py as a new, independent module (its own store and Flask app)."""
    global _loaded
    _loaded += 1
    spec = importlib.util.spec_from_file_location(f"legacy_app_{_loaded}", APP_DIR / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Default module for the contract tests: in-memory backend.
legacy_app = load_app_module()


def reset_state():
    """Put the in-memory store back to its startup values."""
    legacy_app.store.reset()


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


# --- DynamoDB Local --------------------------------------------------------------
# Integration tests run only when DYNAMODB_LOCAL_ENDPOINT points at DynamoDB Local,
# e.g. http://localhost:8000. They never talk to real AWS.

DYNAMODB_LOCAL_ENDPOINT = os.environ.get("DYNAMODB_LOCAL_ENDPOINT")

requires_dynamodb_local = pytest.mark.skipif(
    not DYNAMODB_LOCAL_ENDPOINT,
    reason="set DYNAMODB_LOCAL_ENDPOINT to run DynamoDB Local integration tests",
)


def dynamodb_local_env(table_name):
    """Environment for an app process using DynamoDB Local (dummy credentials)."""
    host = urlparse(DYNAMODB_LOCAL_ENDPOINT).hostname or ""
    if "amazonaws" in host:
        raise RuntimeError("DYNAMODB_LOCAL_ENDPOINT must be DynamoDB Local, not AWS")
    return {
        "STORAGE_BACKEND": "dynamodb",
        "DYNAMODB_TABLE_NAME": table_name,
        "DYNAMODB_ENDPOINT_URL": DYNAMODB_LOCAL_ENDPOINT,
        "AWS_ACCESS_KEY_ID": "local",
        "AWS_SECRET_ACCESS_KEY": "local",
        "AWS_REGION": "us-east-1",  # only AWS_REGION, as ECS and EC2 configs usually provide
        "AWS_EC2_METADATA_DISABLED": "true",
    }


@pytest.fixture
def ddb_table(monkeypatch):
    """A fresh, seeded table in DynamoDB Local; deleted afterwards. Yields its env."""
    if not DYNAMODB_LOCAL_ENDPOINT:
        pytest.skip("DYNAMODB_LOCAL_ENDPOINT not set")
    from seed_dynamodb import create_table
    from storage import DynamoDBStore

    env = dynamodb_local_env(f"legacy-api-test-{uuid.uuid4().hex[:12]}")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    admin = DynamoDBStore.from_env()
    create_table(admin.client, admin.table_name)
    admin.seed()
    yield env
    admin.client.delete_table(TableName=admin.table_name)


@pytest.fixture
def ddb_app(ddb_table):
    """Factory for independent app instances sharing the ddb_table store."""
    return load_app_module
