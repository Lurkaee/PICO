# Contributing to PICO

Thank you for taking an interest in PICO. Contributions are welcome, especially improvements that preserve its local-first, privacy-first architecture and make its behavior more reliable and verifiable.

Before changing code, read [AGENTS.md](AGENTS.md). It contains the repository's non-negotiable engineering, privacy, and safety rules. For current project state, active branches, and phase assignments, refer to [PROJECT_STATE.md](docs/PROJECT_STATE.md). For security-sensitive work, also read the [Security & Risk Register](docs/security-and-risk-register.md) and the relevant architecture/API documentation.

## Development Environment

The currently documented and CI-tested environment is:

- Windows 11.
- Python 3.12 or newer; CI currently exercises Python 3.12, 3.13, and 3.14.
- Node.js 22+ for the frontend.
- pnpm 10 (pinned to 10.28.0 via `frontend/package.json`), matching the CI workflow. pnpm 12 is untested and unsupported.
- PowerShell 5.1+ or PowerShell 7+ for the repository verification script.

### Backend setup

From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

### Frontend setup

From the repository root:

```powershell
pnpm --dir frontend install --frozen-lockfile
```

PICO defaults to deterministic mock behavior for development and tests. Live local inference, cloud providers, browser access, filesystem access, and Home Assistant integrations are optional and should not be enabled just to run the standard test suite.

## Verification Before Opening a Pull Request

Run the checks relevant to your change. For a full contribution, use the repository's standard commands:

```powershell
# Backend compilation and tests (1,060 test cases)
python -m compileall -q pico tests tools
python -m pytest -v

# Frontend checks (197 Vitest tests across 18 test suites)
pnpm --dir frontend run typecheck
pnpm --dir frontend run test
pnpm --dir frontend run build

# Repository verification wrapper (PowerShell; runs compileall, git diff --check, and pytest)
powershell -ExecutionPolicy Bypass -File .\scripts\pico-verify.ps1
```

The current GitHub Actions workflow runs the backend suite on Windows with Python 3.12, 3.13, and 3.14. It also runs frontend type checking, tests, and a production build with Node.js 22 and pnpm.

Do not report a check as passing unless it was actually run. If a check could not be run, state that clearly and explain why. A mock test or simulated benchmark is not evidence of live hardware or physical-device behavior.

## 12-Step Standard Development Workflow

To ensure high quality, auditable traceability, and collision-free collaboration across human engineers and AI agents, all contributions follow a structured 12-step lifecycle:

1. **Read Canonical Architecture:** Review [docs/architecture.md](docs/architecture.md), [AGENTS.md](AGENTS.md), and [docs/PROJECT_STATE.md](docs/PROJECT_STATE.md).
2. **Identify Phase & Acceptance Criteria:** Map work to an active roadmap phase (e.g. P11.3, P11.4, P11.5) and verify all prerequisite gates are satisfied.
3. **Create an Isolated Task Branch:** Branch from the latest, verified default branch (`main`):
   - Feature branches: `feature/p<phase>-<slug>` (e.g. `feature/p11-3-whisper-optimizations`)
   - Bugfix branches: `fix/<phase>-<slug>` (e.g. `fix/p10-3-inbox-race`)
   - Documentation branches: `docs/<phase>-<slug>` (e.g. `docs/p11-5-screen-spec`)
   - Governance/chores: `chore/<slug>` (e.g. `chore/dependency-sync`)
4. **Scope to One Logical Task:** Keep each pull request focused on one reviewable slice. Never bundle unrelated UI styling, backend refactoring, or dependency updates.
5. **Execute Targeted Tests:** Run specific tests corresponding to the modified subsystem early and frequently.
6. **Execute Full Qualification Suite:** Verify backend (`python -m pytest`) and frontend (`pnpm --dir frontend run test`, `pnpm --dir frontend run typecheck`, `pnpm --dir frontend run build`) regression suites.
7. **Document Architectural & Behavioral Changes:** Update relevant ADRs in `docs/decisions/`, data contracts in `docs/data-model.md`, or API contracts in `docs/api-contract.md`.
8. **Inspect Repository Diff:** Run `git diff --check` and `git status` to ensure zero stray files, temporary caches, or untracked test scripts are accidentally staged.
9. **Open Pull Request with Evidence:** Populate the PR template with exact verification commands, test output summaries, and security/privacy review checklists.
10. **Merge via Verified CI Review:** Merge only after automated CI checks pass and reviews are resolved.
11. **Update Canonical Project State:** Record completed milestone evidence and updated commit SHAs in [docs/PROJECT_STATE.md](docs/PROJECT_STATE.md).
12. **Retire Obsolete Branches with Preservation:** When a branch is merged or superseded, archive its commit reference via an annotated tag (`archive/<branch-name>`) before deleting the branch. Never force-delete unpreserved commits.

## PICO-Specific Engineering Expectations

All contributions must preserve the following boundaries:

1. **Classification follows the data.** Effective classification must account for user input, history, memory, retrieval, and tool outputs. A more restrictive classification must not be downgraded during context assembly.
2. **Private context stays local.** Only eligible `PUBLIC` effective context may be sent to a cloud provider, and only with explicit cloud opt-in (`CLOUD_ENABLED=true`). Unknown or non-public context must fail closed or remain local (`RULE_PRIVATE_LOCAL_PIN`).
3. **Secrets stay out of source and model context.** Never commit `.env` files, tokens, API keys, private keys, real user conversations, or household telemetry. Use synthetic fixtures and the ignored local `.env` file.
4. **Models propose; policy and humans authorize.** Model output and external content are untrusted. They must not grant permissions, approve actions, or bypass the capability registry and Policy A.
5. **Consequential actions need explicit authorization.** Preserve approval binding, single-use claims, replay protection, bounded execution, zero-blind-retry behavior, and independent postcondition verification.
6. **Do not overstate physical connectivity.** Mock or simulated Home Assistant results must never be presented as proof that a real device was reached or changed.
7. **Keep the system bounded and local-first.** Preserve resource limits, disabled-by-default integrations, and graceful offline behavior unless a reviewed design explicitly changes them.

If a contribution appears to conflict with an invariant in `AGENTS.md`, resolve that conflict in the design before implementing it.

## Dependency and Asset Changes

Before adding or upgrading a dependency, explain why it is needed and consider maintenance, footprint, transitive dependencies, license terms, and whether the same capability already exists in the project. Keep lockfiles (`tools/packaging/requirements-windows-runtime.lock`, `frontend/pnpm-lock.yaml`) and manifests consistent. Do not copy third-party code or assets without checking their license and attribution requirements.

## Questions & Vulnerability Reporting

For design questions, open a focused issue with enough context to discuss the proposal without posting secrets, private logs, or household data. For suspected security vulnerabilities, follow [SECURITY.md](SECURITY.md) instead of opening a public issue with technical details.
