# PICO P7.3 — Home Assistant Test Light Commissioning & Readiness Runbook
## Preparing the First Physical Action Safely Without Executing It

---

## 1. Overview & Operational Principles

Milestone **P7.3** establishes the software boundary, triple-digest integrity claims, and human-in-the-loop authorization gates for the first physical actuation in PICO.

Before any physical actuation can ever be authorized or executed against real hardware, PICO requires an explicit, inspectable, and auditable **Commissioning & Readiness Gate**.

### Core Constraints & Safety Invariants
1. **Strictly Single Test Light**: Physical actuation is strictly constrained to the single entity specified by `HOME_ASSISTANT_P7_TEST_LIGHT_ENTITY_ID` (restricted to the `light` domain).
2. **Read-Only Preflight**: Preflight never executes mutations, dispatches service calls, claims tickets, or enables actuation.
3. **Actuation Disabled by Default**: `HOME_ASSISTANT_ACTUATION_ENABLED` must remain `false` throughout commissioning readiness. If actuation is enabled prematurely, preflight reports `FAIL` on safety readiness.
4. **Zero Unsolicited Network Calls**: Offline preflight makes zero network calls. Live read-only connectivity inspection requires explicit operator opt-in (`check_live=true`).
5. **No Blind Retries**: If verification or connectivity is uncertain, PICO never retries mutations automatically. The operator must stop and investigate.
6. **No Hardware Claim**: Automated tests and preflights never claim physical device connectivity unless an explicitly requested live read-only check is performed and confirmed against a real Home Assistant instance.

---

## 2. Configuration & Secret Management

All sensitive secrets live exclusively in server-side configuration and are never logged, leaked in reports, or transmitted to models.

### Required Environment Settings
```bash
# Core Home Assistant Integration
HOME_ASSISTANT_ENABLED=true
HOME_ASSISTANT_BASE_URL="http://127.0.0.1:8123"  # http:// permitted only for loopback; https:// required for remote
HOME_ASSISTANT_ACCESS_TOKEN="<long-lived-access-token>"  # Generated in HA Profile -> Security

# Single Target Test Light (Must be in 'light' domain)
HOME_ASSISTANT_P7_TEST_LIGHT_ENTITY_ID="light.p7_test_lamp"

# Physical Actuation Master Switch (MUST REMAIN FALSE during readiness)
HOME_ASSISTANT_ACTUATION_ENABLED=false

# Timeouts and Polling
HOME_ASSISTANT_ACTUATION_TIMEOUT_SECONDS=5.0
HOME_ASSISTANT_VERIFICATION_TIMEOUT_SECONDS=5.0
HOME_ASSISTANT_VERIFICATION_POLL_INTERVAL_SECONDS=0.2

# Authorization & Approval Token (Fail-Closed Barrier)
APPROVAL_SECRET_TOKEN="<strong-random-secret-token>"
TRUSTED_APPROVER_IDENTITIES=["local_operator", "admin"]
```

> [!IMPORTANT]
> Keep `HOME_ASSISTANT_ACTUATION_ENABLED=false` until the offline and live read-only checks have both passed and you are actively conducting the supervised physical test.

---

## 3. Running the Readiness Preflight

PICO provides three interfaces for running the readiness preflight: CLI, REST API, and Tool Capability.

### 3.1 Offline Preflight (Zero Network Calls)

#### Via CLI:
```powershell
python -m pico.homeassistant.readiness
```
Or for machine-readable JSON:
```powershell
python -m pico.homeassistant.readiness --json
```

#### Via REST API:
```http
GET /api/homeassistant/actuation/readiness
```

#### Via Tool Engine:
Invoke capability `homeassistant.actuation_readiness` with input:
```json
{
  "check_live": false
}
```

Offline preflight validates:
- Integration configuration completeness.
- Target entity domain (`light.`) and syntax (`<domain>.<slug>`).
- Approval secret configuration (without revealing value).
- Authorization path availability.
- Transport security and scheme policies.
- Actuation disabled by default status (`HOME_ASSISTANT_ACTUATION_ENABLED=false`).

---

### 3.2 Live Read-Only Connectivity Check (Explicit Opt-In)

When Home Assistant is running and configured, perform an explicit live read-only inspection to verify that PICO can read the test light's reported state without mutating anything.

#### Via CLI:
```powershell
python -m pico.homeassistant.readiness --live
```
Or with JSON:
```powershell
python -m pico.homeassistant.readiness --live --json
```

#### Via REST API:
```http
GET /api/homeassistant/actuation/readiness?check_live=true
```
*(Remote callers must provide header `X-Pico-Approval-Token: <APPROVAL_SECRET_TOKEN>`)*

#### Via Tool Engine:
Invoke capability `homeassistant.actuation_readiness` with input:
```json
{
  "check_live": true
}
```

---

## 4. Understanding Check Items & Statuses

Each check item in `report.checks` yields one of four explicit statuses:

| Status | Meaning | Action Needed |
| :--- | :--- | :--- |
| `PASS` | The configuration, policy, or live check succeeded. | None. Check meets requirements. |
| `FAIL` | Configured value violates policy, failed authentication, or live inspection failed. | Inspect the `remediation` field and resolve the underlying issue. |
| `NOT_CONFIGURED` | An essential configuration variable is unset or empty. | Provide the missing setting in the server environment. |
| `NOT_CHECKED` | Check was omitted (e.g. live connectivity was not requested during offline run). | Request live check (`--live` or `check_live=true`) when ready. |

### Overall Readiness Verdict (`ready_for_commissioning`)
- `ready_for_commissioning: true` **ONLY** when:
  1. All configuration checks report `PASS`.
  2. Actuation remains safely disabled (`HOME_ASSISTANT_ACTUATION_ENABLED=false`).
  3. An explicit live read-only check was requested (`check_live=true`) and passed (`live_passed=true`).
- If only offline checks pass, `ready_for_commissioning` is `false`, and the summary explicitly notes that live connectivity inspection has not been performed.

---

## 5. Troubleshooting & Remediation Guide

| Failure Scenario | Check Name | Remediation |
| :--- | :--- | :--- |
| Missing Base URL or Token | `integration_configuration` | Set `HOME_ASSISTANT_BASE_URL` and `HOME_ASSISTANT_ACCESS_TOKEN` in `.env` or server environment. |
| Integration Disabled | `integration_configuration` | Set `HOME_ASSISTANT_ENABLED=true`. |
| Missing Test Light Entity | `target_test_light` | Set `HOME_ASSISTANT_P7_TEST_LIGHT_ENTITY_ID="light.<name>"`. |
| Invalid Domain (e.g. `switch.lamp`) | `target_test_light` | Change entity ID to a `light` domain entity. Physical actuation in P7.3 is restricted strictly to lights. |
| Insecure Transport (Non-loopback HTTP) | `transport_security` | Use `https://` for remote Home Assistant instances, or set `HOME_ASSISTANT_ALLOW_INSECURE_TRANSPORT=true` only on isolated test networks. |
| Missing Approval Token | `approval_credential` | Set `APPROVAL_SECRET_TOKEN="<random-token>"`. Physical actuation fails closed without it. |
| Actuation Already Enabled | `actuation_disabled_by_default` | Set `HOME_ASSISTANT_ACTUATION_ENABLED=false`. Commissioning preflight requires actuation to be disabled by default. |
| 401/403 Authentication Error | `live_test_light_readability` | Check that `HOME_ASSISTANT_ACCESS_TOKEN` has not expired and has admin/read permissions in Home Assistant. |
| 404 Entity Not Found | `live_test_light_readability` | Ensure the entity exists in Home Assistant (`Developer Tools -> States`) and is visible. |

---

## 6. Manual Commissioning Checklist for Future First-Light Test

> [!CAUTION]
> This checklist is an operator protocol for future physical testing. **Do NOT automate these physical steps.** Follow each step deliberately.

- [ ] **Step 1: Confirm Exact Target Entity ID**
  Verify in the Home Assistant web interface that `HOME_ASSISTANT_P7_TEST_LIGHT_ENTITY_ID` corresponds to a physical, non-critical desk lamp or test bulb, not a room main light, security fixture, or critical circuit.
- [ ] **Step 2: Inspect Surrounding Environment**
  Visually inspect the test light and its immediate surroundings. Ensure nothing hazardous is near the bulb, cables are intact, and the lamp is powered at the wall switch.
- [ ] **Step 3: Run Full Readiness Preflight**
  Run `python -m pico.homeassistant.readiness --live` and verify `ready_for_commissioning: true`. Confirm that `actuation_enabled` is `false`.
- [ ] **Step 4: Enable Actuation for the Test Window**
  Only after Step 3 passes, temporarily set `HOME_ASSISTANT_ACTUATION_ENABLED=true` in the environment and restart the PICO service.
- [ ] **Step 5: Inspect Structured Plan Before Proposing**
  Call `POST /api/homeassistant/actuation/plan` with `target_state: "on"` (or `"off"`). Verify `is_mutation_needed`, preconditions, and expected postconditions.
- [ ] **Step 6: Propose and Deliberately Approve Specific Action**
  Submit proposal via `POST /api/homeassistant/actuation/propose` with valid `X-Pico-Approval-Token`. Approve the specific execution record using the approval endpoint. Confirm that the approval digest strictly matches the execution digest.
- [ ] **Step 7: Supervise Execution and Observe State Independently**
  Trigger execution. Observe whether the physical lamp changes state and confirm PICO's independent verification polling confirms the reported state.
- [ ] **Step 8: Re-Disable Actuation Immediately After Test**
  Set `HOME_ASSISTANT_ACTUATION_ENABLED=false` and restart PICO to return the system to safe standby.

---

## 7. Uncertainty & Failure Handling

If at any point during planning, proposal, dispatch, or verification:
- A network timeout occurs,
- Home Assistant returns an unexpected status code,
- Independent verification polling fails to observe the expected state, or
- The physical lamp state does not match the software report:

**STOP IMMEDIATELY AND INVESTIGATE.**
- **NEVER** resolve uncertainty by repeatedly pressing or dispatching mutations.
- Check the immutable event ledger (`SELECT * FROM event_ledger WHERE aggregate_id = ...`) to inspect exact recorded timestamps and responses.
- Check Home Assistant logbook and log file (`home-assistant.log`).
- Inspect the physical lamp and wall switch manually before attempting another test.
