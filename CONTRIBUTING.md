# Contributing to PICO

Thank you for taking an interest in PICO. Contributions are welcome, especially improvements that preserve its local-first, privacy-first architecture and make its behavior more reliable and verifiable.

Before changing code, read [AGENTS.md](AGENTS.md). It contains the repository's non-negotiable engineering, privacy, and safety rules. For security-sensitive work, also read the [Security & Risk Register](docs/security-and-risk-register.md) and the relevant architecture/API documentation.

## Development Environment

The currently documented and CI-tested environment is:

- Windows 11.
- Python 3.12 or newer; CI currently exercises Python 3.12, 3.13, and 3.14.
- Node.js 22 for the frontend.
- pnpm 10 for the frontend, matching the current CI setup.
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
# Backend compilation and tests
python -m compileall -q pico tests tools
python -m pytest -v

# Frontend checks
pnpm --dir frontend run typecheck
pnpm --dir frontend run test
pnpm --dir frontend run build

# Repository verification wrapper (PowerShell; runs compileall, git diff --check, and pytest)
powershell -ExecutionPolicy Bypass -File .\scripts\pico-verify.ps1
```

The current GitHub Actions workflow runs the backend suite on Windows with Python 3.12, 3.13, and 3.14. It also runs frontend type checking, tests, and a production build with Node.js 22 and pnpm 10.

Do not report a check as passing unless it was actually run. If a check could not be run, state that clearly and explain why. A mock test or simulated benchmark is not evidence of live hardware or physical-device behavior.

## Branches and Pull Requests

- Start from the latest `main` branch and create a focused branch for your change.
- Use a descriptive branch name. Existing repository conventions include prefixes such as `feature/`, `fix/`, `docs/`, and `test/`.
- Keep pull requests focused and reviewable. Avoid combining unrelated refactors, dependency upgrades, and behavior changes.
- Explain the problem, the intended behavior, the scope of the change, and any compatibility or migration impact.
- Include the exact verification commands run and their results.
- Add or update tests for behavior changes, especially authorization, privacy, failure handling, replay protection, and independent verification.
- Update documentation when a change affects an API contract, security invariant, operator workflow, configuration option, or supported capability.

The pull request template includes a checklist for safety, privacy, and verification.

## PICO-Specific Engineering Expectations

All contributions must preserve the following boundaries:

1. **Classification follows the data.** Effective classification must account for user input, history, memory, retrieval, and tool outputs. A more restrictive classification must not be downgraded during context assembly.
2. **Private context stays local.** Only eligible `PUBLIC` effective context may be sent to a cloud provider, and only with explicit cloud opt-in. Unknown or non-public context must fail closed or remain local.
3. **Secrets stay out of source and model context.** Never commit `.env` files, tokens, API keys, private keys, real user conversations, or household telemetry. Use synthetic fixtures and the ignored local `.env` file.
4. **Models propose; policy and humans authorize.** Model output and external content are untrusted. They must not grant permissions, approve actions, or bypass the capability registry and Policy A.
5. **Consequential actions need explicit authorization.** Preserve approval binding, single-use claims, replay protection, bounded execution, zero-blind-retry behavior, and independent postcondition verification.
6. **Do not overstate physical connectivity.** Mock or simulated Home Assistant results must never be presented as proof that a real device was reached or changed.
7. **Keep the system bounded and local-first.** Preserve resource limits, disabled-by-default integrations, and graceful offline behavior unless a reviewed design explicitly changes them.

If a contribution appears to conflict with an invariant in `AGENTS.md`, resolve that conflict in the design before implementing it.

## Dependency and Asset Changes

Before adding or upgrading a dependency, explain why it is needed and consider maintenance, footprint, transitive dependencies, license terms, and whether the same capability already exists in the project. Keep lockfiles and manifests consistent. Do not copy third-party code or assets without checking their license and attribution requirements.

## Questions

For design questions, open a focused issue with enough context to discuss the proposal without posting secrets, private logs, or household data. For suspected security vulnerabilities, follow [SECURITY.md](SECURITY.md) instead of opening a public issue with technical details.
