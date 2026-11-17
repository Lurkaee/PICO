# Vercel deployment scope

PICO's Vercel project publishes the **static frontend only**. It is a UI preview, not a remotely hosted PICO runtime.

## Build configuration

The Vercel project must use:

- **Root Directory:** `frontend`
- **Framework Preset:** Vite
- **Node.js:** 22.x (aligned with the frontend CI job)
- **Install Command:** `pnpm install --frozen-lockfile`
- **Build Command:** `pnpm run build`
- **Output Directory:** `dist`

These build commands are also recorded in `frontend/vercel.json`. The repository root is not a Vercel app root, and Vercel must not invoke the Tauri/Rust desktop build.

## Runtime and privacy boundary

The frontend API client calls same-origin `/api/*` routes. In normal local operation, the FastAPI Core serves the frontend and those API routes from the same local process. Vite's `/api` and `/health` proxies are development-server configuration; they do not create API routes on Vercel.

The hosted Vercel deployment therefore serves the static UI shell only. It does **not** deploy or provide:

- the local FastAPI Core;
- local model providers or Ollama;
- the local SQLite database and memory;
- the Tauri native bridge or operating-system capabilities;
- Home Assistant or other workstation integrations.

The hosted UI should be treated as disconnected from Core. Do not enter real conversations, credentials, household data, or other private information into a hosted preview. Do not add a remote API rewrite or deploy the local Core as a public backend as a shortcut; that would change PICO's local-first privacy and security boundary and requires a separate architecture and threat review.

For an operational PICO session, run the Core and browser/desktop frontend locally as described in the root README. Build and package the Tauri desktop application through the Windows CI/toolchain, not through Vercel.
