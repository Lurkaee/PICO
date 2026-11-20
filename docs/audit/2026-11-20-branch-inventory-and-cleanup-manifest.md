# PICO Branch Inventory & Consolidation Manifest

- **Execution Date:** 2026-11-20
- **Last Reconciled:** 2026-11-20 (Governance Audit Correction)
- **Repository:** `https://github.com/Lurkaee/PICO`
- **Agent Role:** Second Antigravity engineering agent (Repository Governance & Stabilization)
- **Policy Checklist:** [x] Backup Bundle Verified | [x] Remote Archival Tags Pushed | [x] Active Worktrees Preserved | [x] 0 Commits Discarded

---

## 1. Executive Summary & Evolution Accounting

To ensure complete transparency and traceability, branch counts are distinguished across four explicit operational milestones:

1. **Initial Inventory (Preservation Checkpoint):**
   - **Local Branches:** 42 branches in the initial bundle's `refs/heads/*`. (36 common branches tracking remote + 6 local-only branches: `recovery/pico-frontend-redesign`, `feat/frontend-chat-redesign`, `feat/frontend-recovery-a258a04`, `feature/pic0-frontend-redesign`, `fix/dependabot-windows-runtime-lock-sync-pr`, and legacy `master`).
   - **Remote Branches:** 57 named remote tracking branches on `origin` (excluding `origin/HEAD`; 36 common branches with local + 21 remote-only branches).
   - **Total Unique Branch Names:** 63 named branches across local and remote.

2. **Consolidation Deletions & Preservation:**
   - **Remote Deletions:** 49 obsolete remote branches deleted on `origin` (57 initial - 49 deleted = 8 active canonical branches retained: `main` + 7 Phase 11.3 branches).
     - *Breakdown of 49 remote deletions:* 28 common branches deleted + 21 remote-only branches deleted = 49.
   - **Local Deletions:** 29 obsolete local branches deleted (42 initial - 29 deleted = 13 retained initial local branches).
     - *Breakdown of 29 local deletions:* 28 common branches deleted + 1 local-only legacy branch (`master`) deleted = 29.
   - **Archival Tag Anchoring:** 50 annotated tags (`refs/tags/archive/*`) created and pushed to GitHub.
     - *Breakdown of 50 archival tags:* 49 tags for all deleted remote branches + 1 tag (`archive/legacy/master` at `990efb14`) for the deleted local-only legacy `master` branch.
   - **Local vs Remote Deletion Reconciled:** The arithmetic is exact: 49 remote deletions minus 21 remote-only branches = 28 common branches deleted locally. Adding the 1 deleted local-only branch (`master`) yields exactly 29 local branch deletions. All 5 Cline-owned local branches (`recovery/pico-frontend-redesign`, `feat/frontend-chat-redesign`, `feat/frontend-recovery-a258a04`, `feature/pic0-frontend-redesign`, `fix/dependabot-windows-runtime-lock-sync-pr`) were protected and retained.

3. **Subsequent Branch Creation & Publication:**
   - `chore/repository-stabilization-and-governance` was created locally and pushed to `origin` as the dedicated PR #104 working branch (+1 local, +1 remote).
   - `docs/p11-5-desktop-perception-spec` was preserved in its active desktop worktree (`<PERCEPTION_SPEC_WORKTREE>`), committed to, and published to `origin` (+1 local worktree, +1 remote). (Note: This branch was not in the initial bundle's 42 `refs/heads/*` because it was initialized in the separate desktop worktree prior to commit publication).

4. **Current Status (Reconciled):**
   - **Current Local Branches:** **15 branches** (13 preserved initial + 1 governance + 1 Phase 11.5).
   - **Current Remote Branches:** **10 branches** on GitHub (8 preserved initial + 1 governance + 1 Phase 11.5).

---

## 2. Classification Criteria

| Classification | Action | Definition & Criteria |
| :--- | :--- | :--- |
| **KEEP** | Retained | Canonical default branch (`main`), active phase branches (Phases 11.3, 11.4, 11.5), active worktrees, and Cline frontend/recovery branches. |
| **ARCHIVE** | Tagged & Deleted | Branches containing unique commits not present in `main` (e.g. PR closed unmerged, or squash-merged with unique commit graph). Preserved permanently via annotated tag `archive/<branch-name>`. |
| **DELETE-CANDIDATE** | Tagged & Deleted | Branches whose commits are 100% ancestors of `origin/main` (behind `main`, 0 ahead), with associated PRs merged. Tagged for complete traceability before reference removal. |

---

## 3. Retained Branches (KEEP — Exactly 15 Local / 10 Remote)

| Branch Name | Location | Local SHA | Remote SHA | Owner / Active Phase | Justification & Safeguards |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `chore/repository-stabilization-and-governance` | both | `d0b7024a` | `d0b7024a` | Governance / PR #104 | Dedicated repository stabilization branch (snapshot at `fa6eb50a`, remediation at `68b86963`, CI whitespace remediation at `d0b7024a`) |
| `docs/p11-3-proposal-review-closure` | both | `869a6615` | `869a6615` | Phase 11.3 Voice | Active Phase 11.3 review closure branch |
| `docs/p11-3-voice-engine-qualification` | both | `dd011634` | `dd011634` | Phase 11.3 Voice | Active Phase 11.3 voice engine qualification documentation branch |
| `docs/p11-5-desktop-perception-spec` | both | `914cf4bc` | `914cf4bc` | Phase 11.5 Perception | Active Phase 11.5 perception worktree (draft `c113199`, security fix `35e51ddb`, intermediate `410d8e02`, verified tip `914cf4bc`) |
| `feat/frontend-chat-redesign` | local | `7b5c48c2` | `N/A` | Cline Frontend | Active Cline frontend redesign local branch |
| `feat/frontend-recovery-a258a04` | local | `a258a04c` | `N/A` | Cline Frontend | Active Cline frontend recovery local branch |
| `feature/p11-3-1-voice-runtime-and-capture-slice` | both | `f914f171` | `f914f171` | Phase 11.3 Voice | Active Phase 11.3 voice runtime implementation branch |
| `feature/p11-3-2-native-whisper-qualification` | both | `711258df` | `614319cc` | Phase 11.3 Voice | Active Phase 11.3 native whisper qualification prototype branch |
| `feature/p11-3-stt-qualification-closure` | both | `d965299f` | `4ad5a551` | Phase 11.3 Voice | Active Phase 11.3 STT qualification closure branch |
| `feature/pic0-frontend-redesign` | local | `6a03a932` | `N/A` | Cline Frontend | Active Cline frontend redesign local branch |
| `fix/dependabot-windows-runtime-lock-sync-pr` | local | `b611ea25` | `N/A` | Cline Frontend | Active local branch associated with working tree stash |
| `fix/p11-3-1-post-merge-voice-hardening` | both | `693d3570` | `693d3570` | Phase 11.3 Voice | Active Phase 11.3 voice hardening branch |
| `main` | both | `00b21e8a` | `36e0e144` | Core / Canonical | Default canonical branch (local is 0 ahead, 81 behind origin/main; 0 unpushed commits) |
| `proposal/p11-3-qualification-promotion-rules` | both | `b9b629d8` | `e06d75ba` | Phase 11.3 Voice | Active Phase 11.3 promotion rules branch |
| `recovery/pico-frontend-redesign` | local | `6ae32844` | `N/A` | Cline Frontend | Active working tree branch for Cline frontend redesign on Desktop |

---

## 4. Archived Branches (Unique Commits Preserved — 10 Branches)

| Branch Name | Original SHA | Divergence (Ahead/Behind) | Associated PR | Archival Tag | Preserved Content Summary |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `dependabot/pip/fastapi-0.162.2` | `85970c69` | +1 / -59 | PR #83 | `archive/dependabot/pip/fastapi-0.162.2` | Contains 1 unique commit(s) not directly merged to main (PRs: [83]). Must preserve exact commit history via annotated tag before removal. |
| `docs/readme-brand-refresh` | `7bb9f86f` | +2 / -174 | PR #49 | `archive/docs/readme-brand-refresh` | Contains 2 unique commit(s) not directly merged to main (PRs: [49]). Must preserve exact commit history via annotated tag before removal. |
| `feature/p1-p4-foundation` | `80d062d2` | +1 / -264 | PR #7, PR #4 | `archive/feature/p1-p4-foundation` | Contains 1 unique commit(s) not directly merged to main (PRs: [7, 4]). Must preserve exact commit history via annotated tag before removal. |
| `feature/p10-2-3-render-safe-cache-bounds` | `ca85f710` | +1 / -213 | PR #34 | `archive/feature/p10-2-3-render-safe-cache-bounds` | Contains 1 unique commit(s) not directly merged to main (PRs: [34]). Must preserve exact commit history via annotated tag before removal. |
| `feature/p11-2-4-2-windows-installer-qualification` | `1e42f400` | +2 / -87 | PR #73 | `archive/feature/p11-2-4-2-windows-installer-qualification` | Contains 2 unique commit(s) not directly merged to main (PRs: [73]). Must preserve exact commit history via annotated tag before removal. |
| `feature/phase-1-slice-2-model-gateway` | `dd5049c0` | +1 / -267 | PR #1 | `archive/feature/phase-1-slice-2-model-gateway` | Contains 1 unique commit(s) not directly merged to main (PRs: [1]). Must preserve exact commit history via annotated tag before removal. |
| `feature/phase-1.1-privacy-ci` | `36336c4f` | +1 / -266 | PR #2 | `archive/feature/phase-1.1-privacy-ci` | Contains 1 unique commit(s) not directly merged to main (PRs: [2]). Must preserve exact commit history via annotated tag before removal. |
| `feature/phase-1.1.1-reliability-fixes` | `ac06a1dd` | +1 / -265 | PR #3 | `archive/feature/phase-1.1.1-reliability-fixes` | Contains 1 unique commit(s) not directly merged to main (PRs: [3]). Must preserve exact commit history via annotated tag before removal. |
| `fix/vercel-static-frontend-deployment` | `212aa96f` | +3 / -36 | PR #92 | `archive/fix/vercel-static-frontend-deployment` | Contains 3 unique commit(s) not directly merged to main (PRs: [92]). Must preserve exact commit history via annotated tag before removal. |
| `master` | `990efb14` | +1 / -268 | None | `archive/legacy/master` | Contains 1 unique commit(s) not directly merged to main (PRs: []). Must preserve exact commit history via annotated tag before removal. |

---

## 5. Consolidated Fully-Merged Branches (DELETE-CANDIDATE — 40 Branches)

| Branch Name | Original SHA | Behind `main` | Merged PR | Archival Tag | Status |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `dependabot/github_actions/actions/checkout-7` | `e4f923aa` | -120 | PR #62 (merged) | `archive/dependabot/github_actions/actions/checkout-7` | Remote ref deleted; SHA anchored by tag |
| `dependabot/github_actions/pnpm/action-setup-5` | `d591b77d` | -142 | PR #57 (merged) | `archive/dependabot/github_actions/pnpm/action-setup-5` | Remote ref deleted; SHA anchored by tag |
| `dependabot/npm_and_yarn/frontend/testing-library/jest-dom-7.0.1` | `449aefb1` | -102 | PR #64 (merged) | `archive/dependabot/npm_and_yarn/frontend/testing-library/jest-dom-7.0.1` | Remote ref deleted; SHA anchored by tag |
| `dependabot/npm_and_yarn/frontend/vitejs/plugin-react-5.2.0` | `d55aee3d` | -104 | PR #61 (merged) | `archive/dependabot/npm_and_yarn/frontend/vitejs/plugin-react-5.2.0` | Remote ref deleted; SHA anchored by tag |
| `dependabot/npm_and_yarn/frontend/vitest-5.1.0` | `251897ef` | -58 | PR #84 (merged) | `archive/dependabot/npm_and_yarn/frontend/vitest-5.1.0` | Remote ref deleted; SHA anchored by tag |
| `dependabot/pip/anyio-4.16.0` | `34bf6213` | -85 | PR #79 (merged) | `archive/dependabot/pip/anyio-4.16.0` | Remote ref deleted; SHA anchored by tag |
| `dependabot/pip/fastapi-0.162.0` | `f18e2427` | -142 | PR #63 (merged) | `archive/dependabot/pip/fastapi-0.162.0` | Remote ref deleted; SHA anchored by tag |
| `docs/add-mit-license` | `f215af01` | -150 | PR #54 (merged), PR #52 (merged) | `archive/docs/add-mit-license` | Remote ref deleted; SHA anchored by tag |
| `docs/open-source-readiness` | `1aa8eb4c` | -144 | PR #53 (merged) | `archive/docs/open-source-readiness` | Remote ref deleted; SHA anchored by tag |
| `docs/p10-3-3-post-merge-assurance` | `4fd3eb86` | -175 | PR #47 (merged) | `archive/docs/p10-3-3-post-merge-assurance` | Remote ref deleted; SHA anchored by tag |
| `docs/p11-2-desktop-companion-adr` | `9c2b1ef3` | -153 | PR #51 (merged) | `archive/docs/p11-2-desktop-companion-adr` | Remote ref deleted; SHA anchored by tag |
| `docs/p11-roadmap-desktop-companion` | `6c588da6` | -169 | PR #48 (merged) | `archive/docs/p11-roadmap-desktop-companion` | Remote ref deleted; SHA anchored by tag |
| `feature/p10-1-desktop-web-experience` | `4efe6360` | -193 | PR #30 (merged), PR #27 (merged) | `archive/feature/p10-1-desktop-web-experience` | Remote ref deleted; SHA anchored by tag |
| `feature/p10-3-2-operator-execution-lifecycle` | `a0433576` | -178 | PR #46 (merged) | `archive/feature/p10-3-2-operator-execution-lifecycle` | Remote ref deleted; SHA anchored by tag |
| `feature/p11-1-conversational-workflow-synthesis` | `66a8d408` | -159 | PR #50 (merged) | `archive/feature/p11-1-conversational-workflow-synthesis` | Remote ref deleted; SHA anchored by tag |
| `feature/p11-2-1-tauri-desktop-shell` | `f160479f` | -135 | PR #69 (merged) | `archive/feature/p11-2-1-tauri-desktop-shell` | Remote ref deleted; SHA anchored by tag |
| `feature/p11-2-2-bridge-qualification` | `d359ba56` | -98 | PR #71 (merged) | `archive/feature/p11-2-2-bridge-qualification` | Remote ref deleted; SHA anchored by tag |
| `feature/p11-2-4-windows-packaging-qualification` | `df20c3db` | -92 | PR #72 (merged) | `archive/feature/p11-2-4-windows-packaging-qualification` | Remote ref deleted; SHA anchored by tag |
| `feature/p11-native-expressive-eyes` | `173e5503` | -33 | PR #91 (merged) | `archive/feature/p11-native-expressive-eyes` | Remote ref deleted; SHA anchored by tag |
| `feature/p7-3-commissioning-readiness` | `7fcfce9c` | -246 | PR #14 (merged) | `archive/feature/p7-3-commissioning-readiness` | Remote ref deleted; SHA anchored by tag |
| `feature/p7-3-verified-test-light` | `701c7be1` | -253 | PR #11 (merged) | `archive/feature/p7-3-verified-test-light` | Remote ref deleted; SHA anchored by tag |
| `feature/p7-4-2-2-climate-setpoint-actuation` | `c6c31f3d` | -201 | PR #42 (merged) | `archive/feature/p7-4-2-2-climate-setpoint-actuation` | Remote ref deleted; SHA anchored by tag |
| `feature/p7-4-2-3-readonly-dual-setpoint-planning` | `3d998c21` | -190 | PR #43 (merged) | `archive/feature/p7-4-2-3-readonly-dual-setpoint-planning` | Remote ref deleted; SHA anchored by tag |
| `feature/p7-4-2-4-dual-setpoint-actuation` | `c30af347` | -187 | PR #44 (merged) | `archive/feature/p7-4-2-4-dual-setpoint-actuation` | Remote ref deleted; SHA anchored by tag |
| `feature/p7-home-assistant-read-only` | `1288ad07` | -258 | PR #9 (merged) | `archive/feature/p7-home-assistant-read-only` | Remote ref deleted; SHA anchored by tag |
| `feature/p8-1-os-automation` | `55c1728e` | -242 | PR #16 (merged) | `archive/feature/p8-1-os-automation` | Remote ref deleted; SHA anchored by tag |
| `feature/p8-3-sandboxed-browser-automation` | `f2739afc` | -227 | PR #23 (merged) | `archive/feature/p8-3-sandboxed-browser-automation` | Remote ref deleted; SHA anchored by tag |
| `feature/p9-2-single-model-resource-scheduler` | `7a2d1053` | -222 | PR #25 (merged) | `archive/feature/p9-2-single-model-resource-scheduler` | Remote ref deleted; SHA anchored by tag |
| `feature/p9-3-independent-verification` | `6dd0f1c5` | -220 | PR #26 (merged) | `archive/feature/p9-3-independent-verification` | Remote ref deleted; SHA anchored by tag |
| `fix/dependabot-windows-runtime-lock-sync` | `15b15416` | -2 | PR #102 (merged) | `archive/fix/dependabot-windows-runtime-lock-sync` | Remote ref deleted; SHA anchored by tag |
| `fix/frontend-vitest5-typescript7-ci` | `691c109d` | -110 | PR #70 (merged) | `archive/fix/frontend-vitest5-typescript7-ci` | Remote ref deleted; SHA anchored by tag |
| `fix/p6-security-functional-hardening` | `0640d6cb` | -260 | PR #8 (merged) | `archive/fix/p6-security-functional-hardening` | Remote ref deleted; SHA anchored by tag |
| `fix/p7-3-atomic-dispatch-claim` | `d11ae891` | -251 | PR #12 (merged) | `archive/fix/p7-3-atomic-dispatch-claim` | Remote ref deleted; SHA anchored by tag |
| `fix/p7-3-bind-atomic-claim-to-input` | `d131e422` | -249 | PR #13 (merged) | `archive/fix/p7-3-bind-atomic-claim-to-input` | Remote ref deleted; SHA anchored by tag |
| `fix/p7-home-assistant-readonly-hardening` | `9d331e7e` | -255 | PR #10 (merged) | `archive/fix/p7-home-assistant-readonly-hardening` | Remote ref deleted; SHA anchored by tag |
| `fix/p8-1-bind-approval-to-launch-profile` | `793ba011` | -240 | PR #17 (merged) | `archive/fix/p8-1-bind-approval-to-launch-profile` | Remote ref deleted; SHA anchored by tag |
| `fix/p8-2-filesystem-write-boundary` | `42e4bea3` | -235 | PR #19 (merged) | `archive/fix/p8-2-filesystem-write-boundary` | Remote ref deleted; SHA anchored by tag |
| `fix/p8-2-target-identity-race` | `df5c1af5` | -231 | PR #21 (merged) | `archive/fix/p8-2-target-identity-race` | Remote ref deleted; SHA anchored by tag |
| `fix/p8-2-temp-handle-identity` | `32fd28f7` | -229 | PR #22 (merged) | `archive/fix/p8-2-temp-handle-identity` | Remote ref deleted; SHA anchored by tag |
| `test/p7-4-2-4-partial-match-verification` | `5c5a25ad` | -185 | PR #45 (merged) | `archive/test/p7-4-2-4-partial-match-verification` | Remote ref deleted; SHA anchored by tag |

---

## 6. Remote Verification Evidence

All 50 archival tags were verified on the remote repository via `git ls-remote --tags origin 'archive/*'`. All 50 tag refs and their dereferenced commit objects resolve to the exact SHAs recorded above.
Remote branch count was reduced from 57 to 10 active branches (main, governance, Phase 11.5, and 7 Phase 11.3 branches).
