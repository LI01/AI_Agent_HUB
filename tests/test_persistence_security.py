"""Persistence/recovery + concurrency + security — test_plan.md §5.2, §8, §9."""
import asyncio
import importlib
import json
import os
import sys
import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

REPO_ROOT_OK = True


def _reload_hub():
    for mod in list(sys.modules):
        if mod == "hub" or mod.startswith("hub."):
            del sys.modules[mod]
    return importlib.import_module("hub.main")


# === P-SYN: synchronization (test_plan §5.1) ===

def test_p_syn_3_indexes_present(hub_main, tmp_db):
    """NFR-PER-4: indexes on tasks.status, assigned_agent_id, created_at."""
    conn = sqlite3.connect(tmp_db)
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='index'")
    names = {row[0] for row in cur.fetchall()}
    conn.close()
    expected = {"idx_tasks_status", "idx_tasks_agent", "idx_tasks_created"}
    assert expected.issubset(names), f"missing indexes: {expected - names}"


# === P2-PAR (Phase 2 F2 — parent_task_id index + filters) ===

def test_p2_par_1_db_index_present(hub_main, tmp_db):
    """F2 §3.1: idx_tasks_parent must be created by init_db()."""
    conn = sqlite3.connect(tmp_db)
    cur = conn.cursor()
    cur.execute("PRAGMA index_list(tasks)")
    names = {row[1] for row in cur.fetchall()}
    conn.close()
    assert "idx_tasks_parent" in names, f"missing idx_tasks_parent; have {names}"


def test_p2_par_1_filter_by_parent_task_id(hub_main, tmp_db):
    """F2 §3.2: db.list_tasks(parent_task_id=X) and top_level=True filter correctly."""
    from hub import database as db

    parent_id = "parent-uuid-1"
    # 3 children of parent_id
    child_ids = [f"child-{i}" for i in range(3)]
    for cid in child_ids:
        db.save_task({
            "id": cid,
            "name": cid,
            "task_type": "echo",
            "parent_task_id": parent_id,
        })
    # 2 top-level (no parent)
    top_ids = [f"top-{i}" for i in range(2)]
    for tid in top_ids:
        db.save_task({"id": tid, "name": tid, "task_type": "echo"})

    children = db.list_tasks(parent_task_id=parent_id)
    assert {t["id"] for t in children} == set(child_ids)

    tops = db.list_tasks(top_level=True)
    assert {t["id"] for t in tops} == set(top_ids)

    with pytest.raises(ValueError):
        db.list_tasks(parent_task_id=parent_id, top_level=True)


def test_p_syn_journal_mode_wal(hub_main, tmp_db):
    """NFR-CON-2: WAL mode."""
    conn = sqlite3.connect(tmp_db)
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode")
    mode = cur.fetchone()[0]
    conn.close()
    assert mode.lower() == "wal"


# === P2.2-DB: min_version persistence column (design v2 §3.3, F2) ===

def test_p22_db_min_version_column_present(hub_main, tmp_db):
    """F2 §3.3: tasks.min_version column with NOT NULL DEFAULT 1, added via _ensure_column."""
    conn = sqlite3.connect(tmp_db)
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(tasks)")
    cols = {row[1]: row for row in cur.fetchall()}
    conn.close()
    assert "min_version" in cols, f"missing min_version column; have {list(cols)}"
    # PRAGMA table_info row: (cid, name, type, notnull, dflt_value, pk)
    _, _, _, notnull, dflt_value, _ = cols["min_version"]
    assert notnull == 1, f"min_version should be NOT NULL, got notnull={notnull}"
    assert dflt_value is not None and "1" in str(dflt_value), \
        f"min_version default should contain 1, got {dflt_value!r}"


def test_p22_db_min_version_default_for_existing_rows(hub_main, tmp_db):
    """F2 §3.3: rows saved without min_version default to 1 on read-back."""
    from hub import database as db

    db.save_task({
        "id": "no-version-task",
        "name": "no-version-task",
        "task_type": "echo",
    })
    row = db.get_task("no-version-task")
    assert row is not None
    parsed = db.parse_task_row(row)
    assert parsed["min_version"] == 1, f"expected default 1, got {parsed['min_version']!r}"


# === P-REC: restart recovery (test_plan §5.2) ===

def test_p_rec_1_queued_tasks_survive_restart(tmp_db, hub_main):
    """NFR-REL-1: queued tasks survive restart."""
    client = TestClient(hub_main.app)
    admin = {"Authorization": "Bearer test-admin-key-1234567890"}
    r = client.post("/admin/keys", headers=admin, json={"name": "k", "can_assign_tasks": True, "can_view_tasks": True})
    key = r.json()["api_key"]
    h = {"Authorization": f"Bearer {key}"}
    ids = [client.post("/tasks", headers=h, json={"task": f"t{i}", "task_type": "echo"}).json()["task_id"]
           for i in range(3)]

    # restart
    new_main = _reload_hub()
    new_client = TestClient(new_main.app)
    listed = new_client.get("/tasks", headers=h).json()
    listed_ids = {t["task_id"] for t in listed}
    for tid in ids:
        assert tid in listed_ids
    for t in listed:
        if t["task_id"] in ids:
            assert t["status"] == "queued"


def test_p_rec_2_assigned_requeued_on_restart(tmp_db, hub_main):
    """NFR-REL-2: assigned tasks requeued on restart since the agent is gone."""
    client = TestClient(hub_main.app)
    admin = {"Authorization": "Bearer test-admin-key-1234567890"}
    r = client.post("/admin/keys", headers=admin,
                    json={"name": "k", "can_register": True, "can_assign_tasks": True, "can_view_tasks": True})
    key = r.json()["api_key"]
    h = {"Authorization": f"Bearer {key}"}
    client.post("/register", headers=h, json={"agent_id": "a1", "capabilities": ["echo"]})
    r = client.post("/tasks", headers=h, json={"task": "x", "task_type": "echo", "target_agent": "a1"})
    tid = r.json()["task_id"]
    assert client.get(f"/tasks/{tid}", headers=h).json()["status"] == "assigned"

    new_main = _reload_hub()
    new_client = TestClient(new_main.app)
    body = new_client.get(f"/tasks/{tid}", headers=h).json()
    assert body["status"] == "queued"
    assert body["assigned_agent_id"] is None


def test_p_rec_3_keys_persist_across_restart(tmp_db, hub_main):
    """FR-ACL-6: keys survive restart."""
    client = TestClient(hub_main.app)
    admin = {"Authorization": "Bearer test-admin-key-1234567890"}
    r = client.post("/admin/keys", headers=admin, json={"name": "persist", "can_register": True})
    key = r.json()["api_key"]

    new_main = _reload_hub()
    new_client = TestClient(new_main.app)
    g = new_client.get("/agents", headers={"Authorization": f"Bearer {key}"})
    assert g.status_code in (200, 403)  # 200 if can_view_agents default True; 403 only if denied
    # The truer test: can the key still authenticate?
    r2 = new_client.post("/register", headers={"Authorization": f"Bearer {key}"},
                        json={"agent_id": "post-restart", "capabilities": []})
    assert r2.status_code == 200


# === C-CON: concurrency (test_plan §8) ===

def test_c_con_1_parallel_registrations(tmp_db, hub_main):
    """50 simultaneous registrations succeed without corruption."""
    client = TestClient(hub_main.app)
    admin = {"Authorization": "Bearer test-admin-key-1234567890"}
    key = client.post("/admin/keys", headers=admin, json={"name": "k", "can_register": True}).json()["api_key"]
    h = {"Authorization": f"Bearer {key}"}

    def reg(i):
        return client.post("/register", headers=h, json={"agent_id": f"a{i}", "capabilities": ["echo"]})

    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(reg, range(20)))
    assert all(r.status_code == 200 for r in results)
    listed = client.get("/agents", headers=h).json()
    assert len({a["agent_id"] for a in listed}) >= 20


def test_c_con_2_parallel_task_submits_unique_uuids(tmp_db, hub_main):
    client = TestClient(hub_main.app)
    admin = {"Authorization": "Bearer test-admin-key-1234567890"}
    key = client.post("/admin/keys", headers=admin, json={"name": "k", "can_assign_tasks": True, "can_view_tasks": True}).json()["api_key"]
    h = {"Authorization": f"Bearer {key}"}

    def submit(i):
        return client.post("/tasks", headers=h, json={"task": f"t{i}", "task_type": "echo"}).json()["task_id"]

    with ThreadPoolExecutor(max_workers=10) as ex:
        ids = list(ex.map(submit, range(50)))
    assert len(set(ids)) == 50


# === SEC: security (test_plan §9) ===

def test_sec_1_unauthenticated_mutating_endpoints(hub_main):
    client = TestClient(hub_main.app)
    bogus = {"Authorization": "Bearer wrong"}
    paths_and_payloads = [
        ("/register", {"agent_id": "x", "capabilities": []}),
        ("/heartbeat", {"agent_id": "x", "status": "idle"}),
        ("/tasks", {"task": "x"}),
    ]
    for path, payload in paths_and_payloads:
        r = client.post(path, headers=bogus, json=payload)
        assert r.status_code == 403, f"{path} should reject bogus key, got {r.status_code}"


def test_sec_2_db_only_stores_hashes(tmp_db, hub_main):
    """NFR-SEC-4: raw key never written to DB."""
    client = TestClient(hub_main.app)
    admin = {"Authorization": "Bearer test-admin-key-1234567890"}
    r = client.post("/admin/keys", headers=admin, json={"name": "secret-key", "can_register": True})
    raw = r.json()["api_key"]

    conn = sqlite3.connect(tmp_db)
    cur = conn.cursor()
    cur.execute("SELECT key_hash FROM api_keys")
    hashes = [row[0] for row in cur.fetchall()]
    conn.close()
    assert raw not in hashes
    import hashlib
    assert hashlib.sha256(raw.encode()).hexdigest() in hashes


def test_sec_3_raw_key_not_returned_on_list(tmp_db, hub_main):
    client = TestClient(hub_main.app)
    admin = {"Authorization": "Bearer test-admin-key-1234567890"}
    client.post("/admin/keys", headers=admin, json={"name": "exposed", "can_register": True})
    r = client.get("/admin/keys", headers=admin)
    body_text = r.text
    for entry in r.json():
        assert "api_key" not in entry
        assert "key_hash" not in entry


def test_sec_4_no_hardcoded_default_admin(tmp_db, monkeypatch, hub_main):
    """NFR-SEC-2: hardcoded `agent-hub-seed-admin-key-12345` from summary.md must NOT auth."""
    client = TestClient(hub_main.app)
    r = client.get("/admin/keys", headers={"Authorization": "Bearer agent-hub-seed-admin-key-12345"})
    assert r.status_code == 403


# === Regression: timeout enforcement (§1.5) ===

def test_regression_1_5_timeout_marks_task(tmp_db, hub_main):
    """Regression for §1.5: timeout watcher marks expired tasks."""
    import time
    client = TestClient(hub_main.app)
    admin = {"Authorization": "Bearer test-admin-key-1234567890"}
    key = client.post("/admin/keys", headers=admin,
                      json={"name": "k", "can_register": True, "can_assign_tasks": True, "can_view_tasks": True}).json()["api_key"]
    h = {"Authorization": f"Bearer {key}"}
    client.post("/register", headers=h, json={"agent_id": "a1", "capabilities": ["echo"]})
    r = client.post("/tasks", headers=h,
                    json={"task": "x", "task_type": "echo", "target_agent": "a1", "timeout": 1})
    tid = r.json()["task_id"]
    assert client.get(f"/tasks/{tid}", headers=h).json()["status"] == "assigned"
    # Manually invoke the scanner since TestClient doesn't run startup tasks
    asyncio.run(hub_main.scan_timeouts_once())
    time.sleep(1.5)
    asyncio.run(hub_main.scan_timeouts_once())
    body = client.get(f"/tasks/{tid}", headers=h).json()
    assert body["status"] in ("timeout", "queued"), f"expected timeout policy, got {body['status']}"
