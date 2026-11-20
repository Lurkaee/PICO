# PICO Complete Repository File Audit Report
**Execution Date:** 2026-11-20
**Repository:** `https://github.com/Lurkaee/PICO`
**Agent Role:** Second Antigravity engineering agent (Repository Governance & Stabilization)
**Scope:** Exhaustive audit covering the baseline revision (`36e0e14`, 447 files) and current reviewed tree (`fa6eb50`, 453 files), 71 untracked workspace items, and 60 ignored items.

## 1. Inventory Summary & Baseline Accounting
- **Baseline Tracked Files (`36e0e1448a3f815cd5b031b598c1ea6ecfe436a6`):** 447 files
- **Governance Documentation Additions (commit `fa6eb50`):** +6 files (`docs/PROJECT_STATE.md`, `docs/audit/file_inventory.json`, and 4 audit reports in `docs/audit/`)
- **Total Current Tracked Files:** 453 files
- **Classifications Breakdown:**
  - `ACTIVE`: 85 files
  - `REQUIRED`: 307 files
  - `SUPPORTING`: 61 files
- **Component Grouping Totals (42 distinct component groups):**
  - `desktop/tauri-rust`: 67 files
  - `testing/backend`: 63 files
  - `backend/capabilities`: 25 files
  - `docs/evaluation`: 18 files
  - `frontend/__tests__`: 18 files
  - `backend/models`: 18 files
  - `backend/repositories`: 16 files
  - `backend/schemas`: 16 files
  - `backend/api`: 15 files
  - `project/root-config`: 13 files
  - `backend/workflow`: 13 files
  - `backend/database-migrations`: 12 files
  - `frontend/config`: 11 files
  - `frontend/components`: 11 files
  - `backend/perception`: 11 files
  - `backend/homeassistant`: 10 files
  - `backend/services`: 10 files
  - `docs/specifications`: 8 files
  - `backend/os_automation`: 8 files
  - `backend/providers`: 8 files
  - `tools/bench`: 8 files
  - `backend/core`: 7 files
  - `backend/browser`: 7 files
  - `backend/scheduler`: 7 files
  - `docs/decisions`: 6 files
  - `backend/filesystem`: 6 files
  - `docs/audit`: 5 files
  - `tools/whisper_shim`: 5 files
  - `frontend/api`: 4 files
  - `backend/db`: 4 files
  - `tools/packaging`: 4 files
  - `ci/workflows`: 3 files
  - `frontend/app-root`: 3 files
  - `automation/scripts`: 3 files
  - `assets/brand`: 2 files
  - `backend/cli`: 2 files
  - `frontend/companion`: 1 files
  - `frontend/context`: 1 files
  - `frontend/styles`: 1 files
  - `frontend/test`: 1 files
  - `frontend/types`: 1 files
  - `tools/general`: 1 files

---

## 2. Tracked Files Component Inventory Table
The table below is generated directly from the component groups in `file_inventory.json`, ensuring complete mathematical consistency across documentation and machine-readable data:

| Component Group | File Count | Dominant Classification | Architectural Scope & Role |
| :--- | :--- | :--- | :--- |
| `desktop/tauri-rust` | 67 | REQUIRED | Native Tauri 2 desktop shell, loopback IPC bridge, Windows Job Object supervisor, and system tray |
| `testing/backend` | 63 | ACTIVE | Pytest test suite covering 1,060 automated unit, integration, and security test cases |
| `backend/capabilities` | 25 | REQUIRED | Allowlisted typed capability tools (core, OS, filesystem, browser, Home Assistant) |
| `docs/evaluation` | 18 | SUPPORTING | Hardware benchmark suites, evaluation cases, and animated companion screenshot evidence |
| `frontend/__tests__` | 18 | REQUIRED | Vitest frontend test suites covering 197 test cases across 18 test files |
| `backend/models` | 18 | REQUIRED | SQLAlchemy relational entities (conversations, messages, memory, workflows, projects, tasks) |
| `backend/repositories` | 16 | REQUIRED | Data access repositories with SQLite WAL transactional boundaries |
| `backend/schemas` | 16 | REQUIRED | Pydantic v2 data validation schemas, serialization models, and API contracts |
| `backend/api` | 15 | REQUIRED | FastAPI route handlers (/chat, /memory, /workflows, /companion, /pairing) |
| `project/root-config` | 13 | REQUIRED | Root project governance, license, contribution guidelines, pyproject.toml, and requirements |
| `backend/workflow` | 13 | REQUIRED | Autonomous workflow coordination engine, Kahn DAG validator, and compensation handlers |
| `backend/database-migrations` | 12 | REQUIRED | Alembic schema versioning scripts (migrations 0001 through 0004) |
| `frontend/config` | 11 | ACTIVE | Vite, Vitest, TypeScript, Tailwind, and package configuration files |
| `frontend/components` | 11 | REQUIRED | React UI components (ChatPanel, WorkflowTimeline, MemoryInspector, ApprovalInbox) |
| `backend/perception` | 11 | REQUIRED | Local audio session manager and perception pipeline adapters |
| `backend/homeassistant` | 10 | REQUIRED | Home Assistant client, read-only discovery, and allowlisted actuation adapters |
| `backend/services` | 10 | REQUIRED | Core domain services (context compiler, memory service, audio capture) |
| `docs/specifications` | 8 | SUPPORTING | Canonical architecture record, implementation plan, and data model contracts |
| `backend/os_automation` | 8 | REQUIRED | Host operating system automation and application launching adapters |
| `backend/providers` | 8 | REQUIRED | Model gateway adapters (deterministic mock, local Ollama, cloud provider) |
| `tools/bench` | 8 | SUPPORTING | Local hardware inference benchmarking harness (runner, mock, reporting) |
| `backend/core` | 7 | REQUIRED | Application settings (config.py), error definitions, and lifecycle utilities |
| `backend/browser` | 7 | REQUIRED | Playwright sandboxed browser automation adapter with origin allowlists |
| `backend/scheduler` | 7 | REQUIRED | Single-model logical worker resource scheduler and priority queues |
| `docs/decisions` | 6 | SUPPORTING | Architecture Decision Records (ADRs 0001 through 0006) |
| `backend/filesystem` | 6 | REQUIRED | Reversible filesystem operations with staged diff generation |
| `docs/audit` | 5 | SUPPORTING | Repository stabilization audit records, manifests, and file inventory JSON |
| `tools/whisper_shim` | 5 | SUPPORTING | Prototype C shim and build scripts for whisper.cpp qualification |
| `frontend/api` | 4 | REQUIRED | Frontend REST and SSE API client modules |
| `backend/db` | 4 | REQUIRED | SQLite database engine configuration and session dependency providers |
| `tools/packaging` | 4 | SUPPORTING | Windows NSIS packaging configuration and pinned runtime dependency lockfiles |
| `ci/workflows` | 3 | SUPPORTING | GitHub Actions continuous integration pipelines (Python 3.12/3.13/3.14 & Node 22) |
| `frontend/app-root` | 3 | REQUIRED | React application entrypoints (App.tsx, main.tsx, index.css, vite-env.d.ts) |
| `automation/scripts` | 3 | SUPPORTING | PowerShell & Bash preflight, verification, and benchmark orchestrators |
| `assets/brand` | 2 | ACTIVE | Brand identity logo images (pico-logo.jpg, pico-logo.png) |
| `backend/cli` | 2 | REQUIRED | CLI entrypoint for operator pairing and runtime management |
| `frontend/companion` | 1 | REQUIRED | Desktop companion state machine and animation controller |
| `frontend/context` | 1 | REQUIRED | React context providers (ConnectivityContext, CompanionContext) |
| `frontend/styles` | 1 | REQUIRED | Application CSS stylesheets and design tokens |
| `frontend/test` | 1 | REQUIRED | Frontend test setup and custom matcher configuration |
| `frontend/types` | 1 | REQUIRED | Frontend TypeScript type declarations |
| `tools/general` | 1 | SUPPORTING | Developer tooling package initialization |

---

## 3. Untracked Workspace Files Audit (Desktop Checkout)
In the primary development workspace (`C:\Users\AYUSH\OneDrive\Desktop\PICO`), 71 untracked items were inventoried:
1. **Cline Engineering Scratch Scripts (30 items):** Scripts like `check_classes.py`, `check_css_braces.py`, `complete_verify.py`, `debug_regex.py`, `write_system_status.py`, `write_chat_css.py`. These were generated during recent UI repair and recovery tasks.
   - *Disposition:* **RETAIN IN WORKSPACE; DO NOT COMMIT TO MAIN.** They belong to active concurrent UI engineering.
2. **Design System & Styling Scratch Files (9 items):** `chat-styles.css`, `frontend/src/styles/index_new.css`, `frontend/src/styles/operational-workspaces.css`, `missing_css.txt`, `system_status_part1.tsx`, etc.
   - *Disposition:* **RETAIN FOR CLINE REVIEW.** Cline owns ongoing frontend recovery.
3. **Agent Configuration Directories (4 items):** `.agent/`, `.agents/`, `.gemini/`, `.gsd/`.
   - *Disposition:* **LOCAL-ONLY.** Agent tooling configuration; should remain gitignored or local.
4. **Repository Search & Validation Scripts (16 items):** Scripts in `scripts/` (e.g. `search_repo.ps1`, `setup_search.ps1`, `validate-agents.ps1`, `validate-all.ps1`).
   - *Disposition:* **INVESTIGATION-REQUIRED.** Standalone developer utilities; keep local until formally proposed.
5. **Root Artifacts (8 items):** `VERSION`, `model_capabilities.yaml`, `package.json`, `adapters/`, `commit_msg.txt`, `new_commit_msg.txt`, `pr_body.md`, `temp_wheels/`.
   - *Disposition:* **LOCAL-ONLY.** Scratch artifacts from dependency builds and PR submissions.

---

## 4. Ignored Files Audit
60 gitignored items were analyzed across runtime and development directories:
- **Python Virtual Environment (`.venv/`):** Pinned Python 3.14.7 runtime with 26 installed packages. Properly ignored.
- **Local Database Instance (`pico.db`, 8.2 MB):** SQLite WAL database with seeded local development records. Ignored by `*.db` rule. Zero production/real user data.
- **Frontend Build Artifacts:** `frontend/node_modules/`, `frontend/dist/`, `frontend/src-tauri/target/`, `frontend/src-tauri/staged/`. Properly excluded.
- **PICO Runtime Output Directories:** `logs/`, `bench_reports/`, `PICO_backup_recoverable/`, `PICO_publication/`. Properly ignored.

---

## 5. Security & Sensitive Data Audit
- **Secret Scanning:** All 453 tracked source files were scanned using regex patterns for OpenAI API keys (`sk-...`), GitHub personal access tokens (`ghp_...`), private keys, and hardcoded secrets.
  - **Findings:** Exactly 0 real production secrets found.
  - **Synthetic Test Tokens (15 occurrences):** All detected matches reside exclusively in test suites (`tests/test_cloud_provider.py`, `tests/test_bridge_proof.py`) or Rust unit test fixtures (`frontend/src-tauri/src/supervisor.rs`, `lib.rs`) with explicit dummy prefixes (`wrong_secret`, `old_secret_0123...`, `canary_secret = CANARY_SECRET_ALPHA...`, `api_key = sk-client-cloud-key`).
- **Environment Configuration (`.env`):** Only `.env.example` is tracked. All local `.env` and `.env.*` files are strictly gitignored.
- **Data Classification Enforced:** Zero household telemetry, private conversations, or non-public context was found committed.

---

## 6. Large Binary & Brand Asset Audit
- **Model Weights:** Zero large LLM weights, ONNX models, or GGUF binary files are committed to Git. Models are loaded at runtime from local Ollama (`qwen3.5:2b`).
- **Brand Asset Inventory (`assets/brand`):**
  - `assets/brand/pico-logo.jpg`: 5,430 bytes (JPEG brand logo preserved from archival tag `archive/docs/readme-brand-refresh`).
  - `assets/brand/pico-logo.png`: 159,003 bytes (high-resolution PNG brand logo).
  - *(Note: No `pico-icon.png` is tracked; prior references to `pico-icon.png` were erroneous and have been removed).*
- **Evaluation Screenshots (`docs/evaluation/screenshots`):**
  - Exactly 11 audit captures of expressive eye animations (`01_neutral.png` through `11_thinking.png`), ranging from **1,673 to 7,889 bytes each** (totaling 56,815 bytes across all 11 captures).
- **Git LFS:** 0 Git LFS objects; repository is completely lightweight (~15 MB total git history).
