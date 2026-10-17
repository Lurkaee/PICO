# PICO — Personal Intelligence Coordinator & Operator

PICO is a local-first, multi-model personal intelligence system engineered for verifiable persistence, private context compilation, transparent multi-model routing, and safe tool execution.

---

## Architecture & Documentation

- [AGENTS.md](file:///c:/Users/AYUSH/OneDrive/Desktop/PICO/AGENTS.md) — Non-negotiable operating rules, hardware constraints, and engineering principles.
- [docs/architecture.md](file:///c:/Users/AYUSH/OneDrive/Desktop/PICO/docs/architecture.md) — System architecture, subsystem boundaries, and request lifecycle data flow.
- [docs/data-model.md](file:///c:/Users/AYUSH/OneDrive/Desktop/PICO/docs/data-model.md) — Relational persistence models, SQLite WAL configuration, and schema evolution.
- [docs/api-contract.md](file:///c:/Users/AYUSH/OneDrive/Desktop/PICO/docs/api-contract.md) — REST API endpoints, schemas, validation, and error envelopes.
- [docs/security-and-risk-register.md](file:///c:/Users/AYUSH/OneDrive/Desktop/PICO/docs/security-and-risk-register.md) — Threat modeling, local trust boundaries, and risk mitigations.
- [docs/implementation-plan.md](file:///c:/Users/AYUSH/OneDrive/Desktop/PICO/docs/implementation-plan.md) — Phased delivery plan and completion statuses.
- [docs/evaluation/README.md](file:///c:/Users/AYUSH/OneDrive/Desktop/PICO/docs/evaluation/README.md) — Evaluation framework and representative test case specifications.
- [docs/model-gateway-design.md](file:///c:/Users/AYUSH/OneDrive/Desktop/PICO/docs/model-gateway-design.md) — Per-step model router, profiles, and data classification policies.

---

## Local Setup & Quickstart (Windows PowerShell)

### 1. Prerequisites
- Python 3.12+ (tested on Python 3.14.7)
- Windows PowerShell

### 2. Environment Activation & Dependencies
```powershell
# Create virtual environment (if not already created)
python -m venv .venv

# Activate virtual environment
.\.venv\Scripts\Activate.ps1

# Install pinned dependencies
pip install -r requirements.txt
```

### 3. Database Migrations & Seeding
```powershell
# Apply Alembic schema migrations
alembic upgrade head

# (Optional) Seed deterministic local development data
python -m pico.db.seed
```

### 4. Run Backend Server
```powershell
uvicorn pico.main:app --host 127.0.0.1 --port 8000 --reload
```
Once running, the interactive OpenAPI documentation is available at `http://127.0.0.1:8000/docs` and health check at `http://127.0.0.1:8000/health`.

### 5. Run Automated Tests
```powershell
pytest -v
```
