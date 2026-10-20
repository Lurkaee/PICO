import argparse
import asyncio
import json
import logging
import re
from typing import Any

from pico.core.config import settings
from pico.core.errors import AuthorizationError, EntityNotFoundError, ValidationError
from pico.homeassistant.client import HomeAssistantClient
from pico.homeassistant.contracts import (
    ActuationReadinessReport,
    HomeAssistantConnectionError,
    ReadinessCheckItem,
    ReadinessCheckStatus,
)
from pico.homeassistant.service import HomeAssistantService

logger = logging.getLogger(__name__)

ENTITY_ID_REGEX = re.compile(r"^[a-zA-Z0-9_]+\.[a-zA-Z0-9_]+$")


class HomeAssistantReadinessChecker:
    """Read-only preflight and commissioning readiness checker for the single configured test light.

    Invariants:
    1. NEVER calls a Home Assistant service endpoint or dispatches a mutation.
    2. NEVER operates a device or changes entity state.
    3. NEVER approves, claims, queues, or executes a capability.
    4. NEVER automatically sets HOME_ASSISTANT_ACTUATION_ENABLED=true.
    5. NEVER leaks secret tokens or credential values.
    6. Offline preflight performs strictly ZERO network requests.
    7. Live read-only check requires an explicit operator opt-in (check_live=True).
    """

    def __init__(
        self,
        service: HomeAssistantService | None = None,
        readonly_client: HomeAssistantClient | None = None,
    ) -> None:
        self._service = service
        self._readonly_client = readonly_client

    def _get_service(self) -> HomeAssistantService:
        if self._service is not None:
            return self._service
        return HomeAssistantService(client=self._readonly_client)

    async def evaluate_readiness(
        self,
        check_live: bool = False,
    ) -> ActuationReadinessReport:
        """Evaluate configuration, authorization, transport, and optional live readability."""
        checks: list[ReadinessCheckItem] = []
        configured_light = (settings.HOME_ASSISTANT_P7_TEST_LIGHT_ENTITY_ID or "").strip()

        # 1. Integration configuration & enablement
        integration_enabled = settings.HOME_ASSISTANT_ENABLED
        access_token_present = bool(
            settings.HOME_ASSISTANT_ACCESS_TOKEN and settings.HOME_ASSISTANT_ACCESS_TOKEN.strip()
        )

        if not integration_enabled and not access_token_present:
            checks.append(
                ReadinessCheckItem(
                    name="integration_configuration",
                    status=ReadinessCheckStatus.NOT_CONFIGURED,
                    message="Home Assistant integration is disabled and access token is unset.",
                    remediation="Set HOME_ASSISTANT_ENABLED=true and configure HOME_ASSISTANT_ACCESS_TOKEN in secure environment configuration.",
                )
            )
        elif not integration_enabled:
            checks.append(
                ReadinessCheckItem(
                    name="integration_configuration",
                    status=ReadinessCheckStatus.FAIL,
                    message="Home Assistant integration is disabled by configuration (HOME_ASSISTANT_ENABLED=false).",
                    remediation="Set HOME_ASSISTANT_ENABLED=true to enable integration awareness.",
                )
            )
        elif not access_token_present:
            checks.append(
                ReadinessCheckItem(
                    name="integration_configuration",
                    status=ReadinessCheckStatus.NOT_CONFIGURED,
                    message="Home Assistant access token is missing or empty.",
                    remediation="Configure HOME_ASSISTANT_ACCESS_TOKEN with a valid Home Assistant Long-Lived Access Token.",
                )
            )
        else:
            checks.append(
                ReadinessCheckItem(
                    name="integration_configuration",
                    status=ReadinessCheckStatus.PASS,
                    message="Home Assistant integration is enabled with configured access token (token value withheld).",
                    remediation=None,
                )
            )

        # 2. Target test light entity ID
        if not configured_light:
            checks.append(
                ReadinessCheckItem(
                    name="target_test_light",
                    status=ReadinessCheckStatus.NOT_CONFIGURED,
                    message="Target test light entity is not configured (HOME_ASSISTANT_P7_TEST_LIGHT_ENTITY_ID is unset).",
                    remediation="Configure HOME_ASSISTANT_P7_TEST_LIGHT_ENTITY_ID with the entity ID of the designated test light (e.g. 'light.test_light').",
                )
            )
        elif not ENTITY_ID_REGEX.match(configured_light):
            checks.append(
                ReadinessCheckItem(
                    name="target_test_light",
                    status=ReadinessCheckStatus.FAIL,
                    message=f"Target entity '{configured_light}' has invalid syntax. Format must be '<domain>.<name>'.",
                    remediation="Ensure HOME_ASSISTANT_P7_TEST_LIGHT_ENTITY_ID contains valid alphanumeric characters and underscores.",
                )
            )
        elif not configured_light.startswith("light."):
            domain = configured_light.split(".", 1)[0]
            checks.append(
                ReadinessCheckItem(
                    name="target_test_light",
                    status=ReadinessCheckStatus.FAIL,
                    message=f"Configured entity '{configured_light}' is in domain '{domain}'. Only the 'light' domain is permitted for physical actuation in Milestone P7.3.",
                    remediation="Change HOME_ASSISTANT_P7_TEST_LIGHT_ENTITY_ID to a light entity (e.g. 'light.test_light').",
                )
            )
        else:
            checks.append(
                ReadinessCheckItem(
                    name="target_test_light",
                    status=ReadinessCheckStatus.PASS,
                    message=f"Target test light '{configured_light}' is syntactically valid and strictly restricted to the 'light' domain.",
                    remediation=None,
                )
            )

        # 3. Approval secret credential
        approval_token_configured = bool(
            settings.APPROVAL_SECRET_TOKEN and settings.APPROVAL_SECRET_TOKEN.strip()
        )
        if not approval_token_configured:
            checks.append(
                ReadinessCheckItem(
                    name="approval_credential",
                    status=ReadinessCheckStatus.NOT_CONFIGURED,
                    message="Server-side approval secret is not configured (APPROVAL_SECRET_TOKEN is unset).",
                    remediation="Set APPROVAL_SECRET_TOKEN in server secure configuration. Physical actuation strictly fails closed without this token.",
                )
            )
        else:
            checks.append(
                ReadinessCheckItem(
                    name="approval_credential",
                    status=ReadinessCheckStatus.PASS,
                    message="Server-side approval secret token is configured (value withheld).",
                    remediation=None,
                )
            )

        # 4. Authorization path and fail-closed availability
        trusted_identities = getattr(settings, "TRUSTED_APPROVER_IDENTITIES", [])
        if not approval_token_configured or not trusted_identities:
            checks.append(
                ReadinessCheckItem(
                    name="authorization_path",
                    status=ReadinessCheckStatus.FAIL,
                    message="Authorization path fails closed: missing APPROVAL_SECRET_TOKEN or empty TRUSTED_APPROVER_IDENTITIES list.",
                    remediation="Configure APPROVAL_SECRET_TOKEN and at least one trusted approver identity in TRUSTED_APPROVER_IDENTITIES.",
                )
            )
        else:
            checks.append(
                ReadinessCheckItem(
                    name="authorization_path",
                    status=ReadinessCheckStatus.PASS,
                    message=f"Authorization path is active with {len(trusted_identities)} trusted approver identities, server-authenticated token validation, and fail-closed enforcement.",
                    remediation=None,
                )
            )

        # 5. Transport security and URL validation
        raw_url = (settings.HOME_ASSISTANT_BASE_URL or "").strip()
        if not raw_url:
            checks.append(
                ReadinessCheckItem(
                    name="transport_security",
                    status=ReadinessCheckStatus.NOT_CONFIGURED,
                    message="Home Assistant base URL is not configured (HOME_ASSISTANT_BASE_URL is empty).",
                    remediation="Configure HOME_ASSISTANT_BASE_URL in server environment.",
                )
            )
        else:
            try:
                sanitized_url = HomeAssistantClient._validate_and_sanitize_url(
                    raw_url,
                    allow_insecure_transport=settings.HOME_ASSISTANT_ALLOW_INSECURE_TRANSPORT,
                )
                checks.append(
                    ReadinessCheckItem(
                        name="transport_security",
                        status=ReadinessCheckStatus.PASS,
                        message=f"Configured transport URL '{sanitized_url}' satisfies scheme, host, and security policies.",
                        remediation=None,
                    )
                )
            except ValidationError as e:
                checks.append(
                    ReadinessCheckItem(
                        name="transport_security",
                        status=ReadinessCheckStatus.FAIL,
                        message=f"Transport URL validation failed: {str(e)}",
                        remediation="Ensure HOME_ASSISTANT_BASE_URL is a valid http/https URL. For non-loopback hosts, use https or set HOME_ASSISTANT_ALLOW_INSECURE_TRANSPORT=true.",
                    )
                )


        # 6. Actuation disabled by default
        actuation_enabled = bool(settings.HOME_ASSISTANT_ACTUATION_ENABLED)
        if not actuation_enabled:
            checks.append(
                ReadinessCheckItem(
                    name="actuation_disabled_by_default",
                    status=ReadinessCheckStatus.PASS,
                    message="Physical actuation is safely disabled by default (HOME_ASSISTANT_ACTUATION_ENABLED=false).",
                    remediation=None,
                )
            )
        else:
            checks.append(
                ReadinessCheckItem(
                    name="actuation_disabled_by_default",
                    status=ReadinessCheckStatus.FAIL,
                    message="Physical actuation is currently ENABLED (HOME_ASSISTANT_ACTUATION_ENABLED=true). For commissioning readiness preflight, actuation must remain disabled until the manual commissioning checklist is completed.",
                    remediation="Set HOME_ASSISTANT_ACTUATION_ENABLED=false until operator explicitly authorizes the first physical actuation test.",
                )
            )

        # 7. Live test-light read-only connectivity
        reported_state: str | None = None
        if not check_live:
            checks.append(
                ReadinessCheckItem(
                    name="live_test_light_readability",
                    status=ReadinessCheckStatus.NOT_CHECKED,
                    message="Live read-only connectivity check was not requested. Offline preflight performed zero network requests.",
                    remediation="To perform a live read-only state check, request readiness evaluation with check_live=true.",
                )
            )
        else:
            # Perform live read-only inspection using strictly GET /api/states/{id}
            if not integration_enabled or not access_token_present:
                checks.append(
                    ReadinessCheckItem(
                        name="live_test_light_readability",
                        status=ReadinessCheckStatus.FAIL,
                        message="Cannot perform live read check: Home Assistant integration is disabled or access token is missing.",
                        remediation="Configure integration and access token before requesting live connectivity check.",
                    )
                )
            elif not configured_light or not configured_light.startswith("light.") or not ENTITY_ID_REGEX.match(configured_light):
                checks.append(
                    ReadinessCheckItem(
                        name="live_test_light_readability",
                        status=ReadinessCheckStatus.FAIL,
                        message="Cannot perform live read check: target test light entity ID is invalid or unconfigured.",
                        remediation="Configure HOME_ASSISTANT_P7_TEST_LIGHT_ENTITY_ID before requesting live connectivity check.",
                    )
                )
            else:
                try:
                    svc = self._get_service()
                    entity_state = await svc.get_entity_state(configured_light)
                    reported_state = entity_state.state
                    checks.append(
                        ReadinessCheckItem(
                            name="live_test_light_readability",
                            status=ReadinessCheckStatus.PASS,
                            message=f"Successfully retrieved reported state for '{configured_light}': current reported state is '{reported_state}'. (Zero mutations performed).",
                            remediation=None,
                        )
                    )
                except EntityNotFoundError:
                    checks.append(
                        ReadinessCheckItem(
                            name="live_test_light_readability",
                            status=ReadinessCheckStatus.FAIL,
                            message=f"Target entity '{configured_light}' not found in Home Assistant (HTTP 404).",
                            remediation="Verify that the designated light entity exists and is exposed in Home Assistant.",
                        )
                    )
                except AuthorizationError:
                    checks.append(
                        ReadinessCheckItem(
                            name="live_test_light_readability",
                            status=ReadinessCheckStatus.FAIL,
                            message="Home Assistant rejected authentication during read-only check (HTTP 401/403).",
                            remediation="Verify that HOME_ASSISTANT_ACCESS_TOKEN is a valid Long-Lived Access Token.",
                        )
                    )
                except (HomeAssistantConnectionError, Exception) as e:
                    checks.append(
                        ReadinessCheckItem(
                            name="live_test_light_readability",
                            status=ReadinessCheckStatus.FAIL,
                            message=f"Failed to read entity state from Home Assistant: {str(e)}",
                            remediation="Verify that the Home Assistant hub is running and reachable at the configured base URL.",
                        )
                    )

        # Determine overall readiness verdict
        offline_checks_passed = all(
            c.status == ReadinessCheckStatus.PASS
            for c in checks
            if c.name != "live_test_light_readability"
        )
        live_check_item = next(
            (c for c in checks if c.name == "live_test_light_readability"), None
        )
        live_passed = live_check_item is not None and live_check_item.status == ReadinessCheckStatus.PASS

        # Ready for commissioning requires all configuration checks PASS and an explicitly requested live check PASS
        ready_for_commissioning = offline_checks_passed and live_passed

        if ready_for_commissioning:
            summary = (
                f"READY FOR COMMISSIONING: All configuration checks and live read-only inspection passed for "
                f"test light '{configured_light}' (current state: '{reported_state}'). Actuation remains safely disabled. "
                f"Operator may proceed to the manual commissioning checklist."
            )
        elif offline_checks_passed and not check_live:
            summary = (
                f"OFFLINE PREFLIGHT PASSED: All configuration, transport, and authorization checks passed. "
                f"Live read-only connectivity check has NOT been performed. Run with check_live=true to verify live entity inspection."
            )
        else:
            failed_checks = [c.name for c in checks if c.status in (ReadinessCheckStatus.FAIL, ReadinessCheckStatus.NOT_CONFIGURED)]
            summary = f"NOT READY: Commissioning preflight incomplete or failed on: {', '.join(failed_checks)}."

        return ActuationReadinessReport(
            ready_for_commissioning=ready_for_commissioning,
            target_entity_id=configured_light or None,
            live_check_performed=check_live,
            live_passed=live_passed if check_live else None,
            reported_state=reported_state,
            actuation_enabled=actuation_enabled,
            checks=checks,
            summary=summary,
        )


async def evaluate_actuation_readiness(
    check_live: bool = False,
    readonly_client: HomeAssistantClient | None = None,
) -> ActuationReadinessReport:
    """Convenience helper to evaluate test light actuation readiness."""
    checker = HomeAssistantReadinessChecker(readonly_client=readonly_client)
    return await checker.evaluate_readiness(check_live=check_live)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for offline and live readiness preflight."""
    parser = argparse.ArgumentParser(
        description="PICO P7.3 Home Assistant Test Light Commissioning & Readiness Preflight"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Explicitly perform live read-only connectivity inspection against Home Assistant.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output results in JSON format.",
    )
    args = parser.parse_args(argv)

    report = asyncio.run(evaluate_actuation_readiness(check_live=args.live))
    if args.json:
        print(report.model_dump_json(indent=2))
    else:
        print("=" * 70)
        print("PICO P7.3 Test Light Commissioning & Readiness Report")
        print("=" * 70)
        print(f"Target Entity ID : {report.target_entity_id or 'NOT CONFIGURED'}")
        print(f"Actuation Enabled: {report.actuation_enabled}")
        print(f"Live Check Run   : {report.live_check_performed}")
        if report.reported_state:
            print(f"Reported State   : {report.reported_state}")
        print("-" * 70)
        print("Checks:")
        for check in report.checks:
            print(f"  [{check.status.value:<14}] {check.name}: {check.message}")
            if check.remediation:
                print(f"                 Remediation: {check.remediation}")
        print("-" * 70)
        print(f"Verdict: {'READY' if report.ready_for_commissioning else 'NOT READY'}")
        print(f"Summary: {report.summary}")
        print("=" * 70)

    return 0 if report.ready_for_commissioning else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())

