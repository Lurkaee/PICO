## Summary

<!-- What changed, and why? -->

## Scope and impact

<!-- Which subsystem(s) are affected? Note API, data, configuration, migration, or compatibility impact. -->

## Verification

- [ ] Backend compilation: `python -m compileall -q pico tests tools`
- [ ] Backend tests: `python -m pytest -v`
- [ ] Frontend typecheck: `pnpm --dir frontend run typecheck`
- [ ] Frontend tests: `pnpm --dir frontend run test`
- [ ] Frontend production build: `pnpm --dir frontend run build`
- [ ] `git diff --check`
- [ ] Any skipped check is explained below.

**Commands run and results:**

<!-- Paste concise results. Do not claim checks passed unless they were run. -->

## Security and privacy review

- [ ] No credentials, secrets, private conversations, real household data, or sensitive logs were added.
- [ ] Data classification and local/cloud routing rules remain intact (or the intentional change is explained).
- [ ] Untrusted model output and external content cannot authorize actions or grant permissions.
- [ ] Consequential actions retain explicit authorization, replay protection, and independent verification.
- [ ] Mock results are not presented as proof of live hardware or physical-device behavior.
- [ ] Security documentation and tests are updated where relevant.
- [ ] Not applicable to this change (explain why).

## Documentation and compatibility

- [ ] User-facing or developer documentation is updated where needed.
- [ ] Dependency manifests and lockfiles are consistent, if dependencies changed.
- [ ] Migration or compatibility impact is documented, if applicable.
