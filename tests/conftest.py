"""Shared fixtures. Each test gets a fresh SQLite DB and a freshly-imported hub module."""
import importlib
import os
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SEED_ADMIN_KEY = "test-admin-key-1234567890"


def _reload_hub():
    """Reimport hub modules so they re-read env vars and re-init the DB."""
    for mod in list(sys.modules):
        if mod == "hub" or mod.startswith("hub."):
            del sys.modules[mod]
    return importlib.import_module("hub.main")


@pytest.fixture
def tmp_db(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setenv("AGENT_HUB_DB_PATH", tmp.name)
    monkeypatch.setenv("AGENT_HUB_ADMIN_KEY", SEED_ADMIN_KEY)
    monkeypatch.setenv("AGENT_HUB_TIMEOUT_SCAN_INTERVAL", "1")
    yield tmp.name
    os.unlink(tmp.name)


@pytest.fixture
def hub_main(tmp_db):
    return _reload_hub()


@pytest.fixture
def client(hub_main):
    return TestClient(hub_main.app)


@pytest.fixture
def admin_headers():
    return {"Authorization": f"Bearer {SEED_ADMIN_KEY}"}


@pytest.fixture
def agent_key(client, admin_headers):
    r = client.post(
        "/admin/keys",
        headers=admin_headers,
        json={"name": "agent-key", "can_register": True, "can_assign_tasks": True},
    )
    assert r.status_code == 200, r.text
    return r.json()["api_key"]


@pytest.fixture
def agent_headers(agent_key):
    return {"Authorization": f"Bearer {agent_key}"}


@pytest.fixture
def submitter_key(client, admin_headers):
    r = client.post(
        "/admin/keys",
        headers=admin_headers,
        json={"name": "submitter-key", "can_assign_tasks": True, "can_view_tasks": True},
    )
    assert r.status_code == 200
    return r.json()["api_key"]


@pytest.fixture
def viewer_key(client, admin_headers):
    r = client.post(
        "/admin/keys",
        headers=admin_headers,
        json={"name": "viewer-key", "can_view_agents": True, "can_view_tasks": True},
    )
    assert r.status_code == 200
    return r.json()["api_key"]
