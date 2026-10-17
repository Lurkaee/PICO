from fastapi.testclient import TestClient


def test_create_and_get_task(client: TestClient) -> None:
    # Setup parent project and conversation
    proj_res = client.post("/api/projects", json={"name": "Task Test Project"})
    proj_id = proj_res.json()["id"]

    conv_res = client.post("/api/conversations", json={"title": "Task Test Conversation"})
    conv_id = conv_res.json()["id"]

    # Create task linked to both
    task_payload = {
        "title": "Configure SQLite Write-Ahead Logging",
        "description": "Ensure PRAGMA journal_mode = WAL is set.",
        "project_id": proj_id,
        "conversation_id": conv_id,
        "priority": "high",
        "status": "pending",
        "metadata": {"tags": ["db", "sqlite"]},
    }
    res = client.post("/api/tasks", json=task_payload)
    assert res.status_code == 201
    data = res.json()
    assert data["id"].startswith("task_")
    assert data["title"] == "Configure SQLite Write-Ahead Logging"
    assert data["project_id"] == proj_id
    assert data["conversation_id"] == conv_id
    assert data["status"] == "pending"
    assert data["priority"] == "high"
    assert data["completed_at"] is None

    task_id = data["id"]

    # Get task
    get_res = client.get(f"/api/tasks/{task_id}")
    assert get_res.status_code == 200
    assert get_res.json()["id"] == task_id


def test_create_task_invalid_relationships(client: TestClient) -> None:
    # Invalid project_id
    res1 = client.post(
        "/api/tasks",
        json={"title": "Invalid Project Task", "project_id": "proj_missing_999"},
    )
    assert res1.status_code == 404
    assert "does not exist" in res1.json()["detail"]

    # Invalid conversation_id
    res2 = client.post(
        "/api/tasks",
        json={"title": "Invalid Conv Task", "conversation_id": "conv_missing_999"},
    )
    assert res2.status_code == 404
    assert "does not exist" in res2.json()["detail"]


def test_task_completion_lifecycle_invariant(client: TestClient) -> None:
    # Create pending task
    res = client.post("/api/tasks", json={"title": "Lifecycle Task", "status": "pending"})
    task_id = res.json()["id"]
    assert res.json()["completed_at"] is None

    # Complete task -> completed_at must be populated automatically
    complete_res = client.patch(
        f"/api/tasks/{task_id}",
        json={"status": "completed"},
    )
    assert complete_res.status_code == 200
    data_completed = complete_res.json()
    assert data_completed["status"] == "completed"
    assert data_completed["completed_at"] is not None

    completed_timestamp = data_completed["completed_at"]

    # Reopen task to in_progress -> completed_at must be cleared to None
    reopen_res = client.patch(
        f"/api/tasks/{task_id}",
        json={"status": "in_progress"},
    )
    assert reopen_res.status_code == 200
    data_reopened = reopen_res.json()
    assert data_reopened["status"] == "in_progress"
    assert data_reopened["completed_at"] is None


def test_list_and_filter_tasks(client: TestClient) -> None:
    proj_res = client.post("/api/projects", json={"name": "Filter Project"})
    proj_id = proj_res.json()["id"]

    client.post(
        "/api/tasks",
        json={"title": "Task A", "project_id": proj_id, "status": "pending"},
    )
    client.post(
        "/api/tasks",
        json={"title": "Task B", "project_id": proj_id, "status": "completed"},
    )
    client.post(
        "/api/tasks",
        json={"title": "Task C", "status": "pending"},
    )

    # Filter by project_id
    res_proj = client.get(f"/api/tasks?project_id={proj_id}")
    assert res_proj.status_code == 200
    items = res_proj.json()["items"]
    assert len(items) == 2
    assert all(item["project_id"] == proj_id for item in items)

    # Filter by project_id and status
    res_filtered = client.get(f"/api/tasks?project_id={proj_id}&status=completed")
    assert res_filtered.status_code == 200
    filtered_items = res_filtered.json()["items"]
    assert len(filtered_items) == 1
    assert filtered_items[0]["title"] == "Task B"


def test_get_nonexistent_task(client: TestClient) -> None:
    res = client.get("/api/tasks/task_nonexistent_777")
    assert res.status_code == 404
    assert "does not exist" in res.json()["detail"]
