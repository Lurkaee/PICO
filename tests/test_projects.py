from fastapi.testclient import TestClient


def test_create_and_get_project(client: TestClient) -> None:
    payload = {
        "name": "Personal Knowledge Base",
        "description": "Unified semantic memory and indexing",
        "status": "active",
        "metadata": {"priority": 1},
    }
    res = client.post("/api/projects", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["id"].startswith("proj_")
    assert data["name"] == "Personal Knowledge Base"
    assert data["status"] == "active"
    assert "created_at" in data
    assert "updated_at" in data

    proj_id = data["id"]

    # Get project
    get_res = client.get(f"/api/projects/{proj_id}")
    assert get_res.status_code == 200
    assert get_res.json()["name"] == "Personal Knowledge Base"


def test_project_name_uniqueness_conflict(client: TestClient) -> None:
    payload = {"name": "Unique System Alpha"}
    res1 = client.post("/api/projects", json=payload)
    assert res1.status_code == 201

    # Attempt to create duplicate project
    res2 = client.post("/api/projects", json=payload)
    assert res2.status_code == 409
    assert "already exists" in res2.json()["detail"]


def test_list_and_filter_projects(client: TestClient) -> None:
    client.post("/api/projects", json={"name": "Active 1", "status": "active"})
    client.post("/api/projects", json={"name": "Archived 1", "status": "archived"})

    # Filter active
    res_active = client.get("/api/projects?status=active")
    assert res_active.status_code == 200
    active_items = res_active.json()["items"]
    assert all(item["status"] == "active" for item in active_items)

    # Filter archived
    res_archived = client.get("/api/projects?status=archived")
    assert res_archived.status_code == 200
    archived_items = res_archived.json()["items"]
    assert any(item["name"] == "Archived 1" for item in archived_items)


def test_update_project(client: TestClient) -> None:
    create_res = client.post("/api/projects", json={"name": "Initial Name"})
    proj_id = create_res.json()["id"]

    # Patch project status and description
    patch_res = client.patch(
        f"/api/projects/{proj_id}",
        json={"status": "completed", "description": "Successfully completed project."},
    )
    assert patch_res.status_code == 200
    data = patch_res.json()
    assert data["status"] == "completed"
    assert data["description"] == "Successfully completed project."


def test_get_nonexistent_project(client: TestClient) -> None:
    res = client.get("/api/projects/proj_nonexistent_000")
    assert res.status_code == 404
    assert "does not exist" in res.json()["detail"]
