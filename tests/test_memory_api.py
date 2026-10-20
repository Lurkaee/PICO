from fastapi.testclient import TestClient


def test_memory_status_api(client: TestClient) -> None:
    res = client.get("/api/memory/status")
    assert res.status_code == 200
    data = res.json()
    assert "working_memory_active_items" in data
    assert "episodic_memory_total_events" in data
    assert "semantic_memory_total_facts" in data
    assert data["storage_type"] == "sqlite_fts5"


def test_working_memory_api_crud(client: TestClient) -> None:
    payload = {
        "session_id": "test_session_api",
        "key": "subtask_progress",
        "content": "Step 2 of 5 completed",
        "item_type": "working_state",
        "data_classification": "INTERNAL",
    }
    res = client.post("/api/memory/working", json=payload)
    assert res.status_code == 201
    item = res.json()
    assert item["id"].startswith("wmem_")
    assert item["key"] == "subtask_progress"

    # List working memory for session
    list_res = client.get("/api/memory/working/test_session_api")
    assert list_res.status_code == 200
    items = list_res.json()
    assert len(items) == 1
    assert items[0]["content"] == "Step 2 of 5 completed"

    # Clear working memory
    del_res = client.delete("/api/memory/working/test_session_api")
    assert del_res.status_code == 200
    assert del_res.json()["cleared_count"] == 1

    # Verify cleared
    empty_res = client.get("/api/memory/working/test_session_api")
    assert len(empty_res.json()) == 0


def test_episodic_memory_api(client: TestClient) -> None:
    payload = {
        "event_type": "benchmark_recorded",
        "description": "Baseline benchmark achieved 10.23 tok/s.",
        "source_provenance": "bench:qwen3.5:2b",
        "data_classification": "INTERNAL",
        "is_inferred": False,
        "metadata": {"tok_per_sec": 10.23},
    }
    res = client.post("/api/memory/events", json=payload)
    assert res.status_code == 201
    event = res.json()
    assert event["id"].startswith("epm_")
    assert event["event_type"] == "benchmark_recorded"

    # List events with filter
    list_res = client.get("/api/memory/events?event_type=benchmark_recorded")
    assert list_res.status_code == 200
    data = list_res.json()
    assert data["total"] >= 1
    assert data["items"][0]["event_type"] == "benchmark_recorded"


def test_semantic_memory_api_and_forget(client: TestClient) -> None:
    payload = {
        "subject": "User",
        "predicate": "favorite_editor",
        "content": "User prefers Visual Studio Code with Vim emulation.",
        "entity_type": "user_preference",
        "confidence": 1.0,
        "is_verified": True,
        "data_classification": "INTERNAL",
        "source_provenance": "user:explicit_statement",
    }
    res = client.post("/api/memory/facts", json=payload)
    assert res.status_code == 201
    fact = res.json()
    fact_id = fact["id"]
    assert fact_id.startswith("sem_")

    # Search fact via FTS5 endpoint
    search_res = client.get("/api/memory/facts/search?q=Studio")
    assert search_res.status_code == 200
    results = search_res.json()
    assert len(results) >= 1
    assert results[0]["id"] == fact_id

    # User-directed forget operation
    del_res = client.delete(f"/api/memory/facts/{fact_id}")
    assert del_res.status_code == 200
    assert del_res.json()["status"] == "forgotten"

    # Search should now return empty
    search_after = client.get("/api/memory/facts/search?q=Studio")
    assert len(search_after.json()) == 0


def test_procedural_memory_api_lifecycle(client: TestClient) -> None:
    payload = {
        "name": "benchmark_execution_skill",
        "version": "1.0.0",
        "description": "Standard procedure for executing local inference benchmarks.",
        "steps": [
            {"step": 1, "cmd": "check ollama preflight"},
            {"step": 2, "cmd": "run 5 repetitions"},
        ],
        "required_capabilities": ["ollama:chat", "kernel32:telemetry"],
        "data_classification": "INTERNAL",
    }
    # 1. Create draft
    create_res = client.post("/api/memory/procedures", json=payload)
    assert create_res.status_code == 201
    proc = create_res.json()
    proc_id = proc["id"]
    assert proc["lifecycle_status"] == "draft"

    # 2. Cannot activate directly from draft
    act_fail = client.post(f"/api/memory/procedures/{proc_id}/activate")
    assert act_fail.status_code == 422 or act_fail.status_code == 403

    # 3. Review procedure
    rev_res = client.post(f"/api/memory/procedures/{proc_id}/review")
    assert rev_res.status_code == 200
    assert rev_res.json()["lifecycle_status"] == "reviewed"

    # 4. Attempt approval with untrusted identity must return 403 Forbidden
    untrusted_res = client.post(
        f"/api/memory/procedures/{proc_id}/approve",
        json={"approver": "untrusted_model_or_caller", "notes": "I am claiming to be human."},
    )
    assert untrusted_res.status_code == 403
    assert "not authorized" in untrusted_res.json()["detail"]

    # 4b. Approve procedure with trusted human approver succeeds
    app_res = client.post(
        f"/api/memory/procedures/{proc_id}/approve",
        json={"approver": "lead_engineer", "notes": "Verified telemetry accuracy."},
    )
    assert app_res.status_code == 200
    approved = app_res.json()
    assert approved["lifecycle_status"] == "approved"
    assert approved["approved_by"] == "lead_engineer"

    # 5. Activate procedure
    act_res = client.post(f"/api/memory/procedures/{proc_id}/activate")
    assert act_res.status_code == 200
    assert act_res.json()["lifecycle_status"] == "active"

    # 6. Retire procedure
    ret_res = client.post(f"/api/memory/procedures/{proc_id}/retire")
    assert ret_res.status_code == 200
    assert ret_res.json()["lifecycle_status"] == "retired"
