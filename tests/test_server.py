"""Tests for optional FastAPI server surface."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from aigineering.server.app import app


def test_create_and_get_asset(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = TestClient(app)

    created = client.post(
        "/assets",
        json={"name": "api_doc", "content": "hello", "trust_tier": "human"},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "api_doc"
    assert body["content"] == "hello"
    assert body["definition_hash"].startswith("def:")

    fetched = client.get("/assets/api_doc")
    assert fetched.status_code == 200, fetched.text
    rows = fetched.json()
    assert rows[0]["id"] == body["id"]


def test_create_protected_asset_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = TestClient(app)

    result = client.post("/assets", json={"name": "_sys_secret", "content": "x"})
    assert result.status_code == 400
    assert "protected" in result.json()["detail"].lower()


def test_create_and_get_contract(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = TestClient(app)

    created = client.post(
        "/contracts",
        json={"name": "api_task", "inputs": ["api_doc"], "outputs": ["out"]},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "api_task"
    assert body["outputs"] == ["out"]

    fetched = client.get(f"/contracts/{body['id']}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["id"] == body["id"]
