"""Storage backend selection (no DynamoDB needed)."""

import pytest

from conftest import legacy_app, load_app_module


def test_default_backend_is_memory():
    assert type(legacy_app.store).__name__ == "MemoryStore"


def test_memory_ready_endpoint(client):
    r = client.get("/ready")
    assert r.status_code == 200 and r.get_json() == {"status": "ready"}


def test_unknown_backend_fails_fast(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "redis")
    with pytest.raises(RuntimeError, match="Unsupported STORAGE_BACKEND"):
        load_app_module()


def test_dynamodb_backend_requires_region(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "dynamodb")
    monkeypatch.setenv("DYNAMODB_TABLE_NAME", "t")
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    with pytest.raises(RuntimeError, match="AWS_REGION"):
        load_app_module()


def test_dynamodb_backend_accepts_aws_region_only(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "dynamodb")
    monkeypatch.setenv("DYNAMODB_TABLE_NAME", "t")
    monkeypatch.setenv("AWS_REGION", "eu-west-2")
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    store = load_app_module().store  # no network call is made at start-up
    assert store.client.meta.region_name == "eu-west-2"
    assert store.ready_client.meta.region_name == "eu-west-2"


def test_dynamodb_backend_requires_table_name(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "dynamodb")
    monkeypatch.delenv("DYNAMODB_TABLE_NAME", raising=False)
    with pytest.raises(RuntimeError, match="DYNAMODB_TABLE_NAME"):
        load_app_module()
