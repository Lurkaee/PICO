import argparse
import asyncio
import json
import logging
import re
from typing import Any
from urllib.parse import urlparse

from pico.core.config import settings
from pico.core.errors import AuthorizationError, EntityNotFoundError, ValidationError
from pico.homeassistant.client import HomeAssistantClient
from pico.homeassistant.actuation import validate_switch_allowlist
from pico.homeassistant.climate import (
    HomeAssistantClimateService,
    validate_climate_allowlist,
    validate_climate_grid_origins,
)
from pico.homeassistant.contracts import (
    ActuationReadinessReport,
    ClimateObservation,
    ClimateReadinessReport,
    HomeAssistantConnectionError,
    ReadinessCheckItem,
    ReadinessCheckStatus,
    ReadinessErrorCategory,
    SwitchActuationReadinessReport,
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
    5. NEVER leaks secret tokens, credentials, query parameters, or raw exception strings.
    6. Offline preflight performs strictly ZERO network requests.
    7. Live read-only check requires an explicit operator opt-in (check_live=True).
    8. Structured error categories (INVALID_URL, INSECURE_TRANSPORT, CONNECTION_FAILED,
       AUTHENTICATION_REJECTED, ENTITY_NOT_FOUND) provide actionable diagnostics safely.
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
                    error_category=ReadinessErrorCategory.MISSING_CONFIGURATION,
                    message="Home Assistant integration is disabled and access token is unset.",
                    remediation="Set HOME_ASSISTANT_ENABLED=true and configure HOME_ASSISTANT_ACCESS_TOKEN in secure environment configuration.",
                )
            )
        elif not integration_enabled:
            checks.append(
                ReadinessCheckItem(
                    name="integration_configuration",
                    status=ReadinessCheckStatus.FAIL,
                    error_category=ReadinessErrorCategory.MISSING_CONFIGURATION,
                    message="Home Assistant integration is disabled by configuration (HOME_ASSISTANT_ENABLED=false).",
                    remediation="Set HOME_ASSISTANT_ENABLED=true to enable integration awareness.",
                )
            )
        elif not access_token_present:
            checks.append(
                ReadinessCheckItem(
                    name="integration_configuration",
                    status=ReadinessCheckStatus.NOT_CONFIGURED,
                    error_category=ReadinessErrorCategory.MISSING_CONFIGURATION,
                    message="Home Assistant access token is missing or empty.",
                    remediation="Configure HOME_ASSISTANT_ACCESS_TOKEN with a valid Home Assistant Long-Lived Access Token.",
                )
            )
        else:
            checks.append(
                ReadinessCheckItem(
                    name="integration_configuration",
                    status=ReadinessCheckStatus.PASS,
                    error_category=None,
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
                    error_category=ReadinessErrorCategory.MISSING_CONFIGURATION,
                    message="Target test light entity is not configured (HOME_ASSISTANT_P7_TEST_LIGHT_ENTITY_ID is unset).",
                    remediation="Configure HOME_ASSISTANT_P7_TEST_LIGHT_ENTITY_ID with the entity ID of the designated test light (e.g. 'light.test_light').",
                )
            )
        elif not ENTITY_ID_REGEX.match(configured_light):
            checks.append(
                ReadinessCheckItem(
                    name="target_test_light",
                    status=ReadinessCheckStatus.FAIL,
                    error_category=ReadinessErrorCategory.INVALID_ENTITY,
                    message="Target entity ID has invalid syntax. Format must be '<domain>.<name>' using alphanumeric characters and underscores.",
                    remediation="Ensure HOME_ASSISTANT_P7_TEST_LIGHT_ENTITY_ID contains valid alphanumeric characters and underscores.",
                )
            )
        elif not configured_light.startswith("light."):
            domain = configured_light.split(".", 1)[0]
            checks.append(
                ReadinessCheckItem(
                    name="target_test_light",
                    status=ReadinessCheckStatus.FAIL,
                    error_category=ReadinessErrorCategory.INVALID_ENTITY,
                    message=f"Target entity domain '{domain}' is prohibited. Only the 'light' domain is permitted for physical actuation in Milestone P7.3.",
                    remediation="Change HOME_ASSISTANT_P7_TEST_LIGHT_ENTITY_ID to a light entity (e.g. 'light.test_light').",
                )
            )
        else:
            checks.append(
                ReadinessCheckItem(
                    name="target_test_light",
                    status=ReadinessCheckStatus.PASS,
                    error_category=None,
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
                    error_category=ReadinessErrorCategory.MISSING_CONFIGURATION,
                    message="Server-side approval secret is not configured (APPROVAL_SECRET_TOKEN is unset).",
                    remediation="Set APPROVAL_SECRET_TOKEN in server secure configuration. Physical actuation strictly fails closed without this token.",
                )
            )
        else:
            checks.append(
                ReadinessCheckItem(
                    name="approval_credential",
                    status=ReadinessCheckStatus.PASS,
                    error_category=None,
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
                    error_category=ReadinessErrorCategory.AUTHORIZATION_UNAVAILABLE,
                    message="Authorization path fails closed: missing APPROVAL_SECRET_TOKEN or empty TRUSTED_APPROVER_IDENTITIES list.",
                    remediation="Configure APPROVAL_SECRET_TOKEN and at least one trusted approver identity in TRUSTED_APPROVER_IDENTITIES.",
                )
            )
        else:
            checks.append(
                ReadinessCheckItem(
                    name="authorization_path",
                    status=ReadinessCheckStatus.PASS,
                    error_category=None,
                    message=f"Authorization path is active with {len(trusted_identities)} trusted approver identities, server-authenticated token validation, and fail-closed enforcement.",
                    remediation=None,
                )
            )

        # 5. Transport security and URL validation
        raw_url = (settings.HOME_ASSISTANT_BASE_URL or "").strip()
        transport_valid = False
        transport_category: ReadinessErrorCategory | None = None

        if not raw_url:
            checks.append(
                ReadinessCheckItem(
                    name="transport_security",
                    status=ReadinessCheckStatus.NOT_CONFIGURED,
                    error_category=ReadinessErrorCategory.MISSING_CONFIGURATION,
                    message="Home Assistant base URL is not configured (HOME_ASSISTANT_BASE_URL is empty).",
                    remediation="Configure HOME_ASSISTANT_BASE_URL in server environment.",
                )
            )
        else:
            try:
                parsed_u = urlparse(raw_url)
                if parsed_u.username or parsed_u.password:
                    raise ValidationError("Embedded credentials and userinfo are prohibited in base URL configuration.")
                HomeAssistantClient._validate_and_sanitize_url(
                    raw_url,
                    allow_insecure_transport=settings.HOME_ASSISTANT_ALLOW_INSECURE_TRANSPORT,
                )
                transport_valid = True
                checks.append(
                    ReadinessCheckItem(
                        name="transport_security",
                        status=ReadinessCheckStatus.PASS,
                        error_category=None,
                        message="Configured transport URL satisfies scheme, host, and security policies.",
                        remediation=None,
                    )
                )
            except ValidationError as e:
                err_text = str(e)
                if "Insecure plaintext transport" in err_text:
                    transport_category = ReadinessErrorCategory.INSECURE_TRANSPORT
                    checks.append(
                        ReadinessCheckItem(
                            name="transport_security",
                            status=ReadinessCheckStatus.FAIL,
                            error_category=ReadinessErrorCategory.INSECURE_TRANSPORT,
                            message="[INSECURE_TRANSPORT] Plaintext HTTP transport to non-loopback host is prohibited by security policy.",
                            remediation="Use 'https://' for remote hosts, or set HOME_ASSISTANT_ALLOW_INSECURE_TRANSPORT=true only in isolated testing environments.",
                        )
                    )
                else:
                    transport_category = ReadinessErrorCategory.INVALID_URL
                    checks.append(
                        ReadinessCheckItem(
                            name="transport_security",
                            status=ReadinessCheckStatus.FAIL,
                            error_category=ReadinessErrorCategory.INVALID_URL,
                            message="[INVALID_URL] Configured transport URL has invalid scheme, missing host, malformed port, or prohibited components (query parameters, fragments, or user credentials).",
                            remediation="Ensure HOME_ASSISTANT_BASE_URL is a valid http:// or https:// URL without query parameters, fragments, or credentials.",
                        )
                    )
            except (ValueError, Exception):
                transport_category = ReadinessErrorCategory.INVALID_URL
                checks.append(
                    ReadinessCheckItem(
                        name="transport_security",
                        status=ReadinessCheckStatus.FAIL,
                        error_category=ReadinessErrorCategory.INVALID_URL,
                        message="[INVALID_URL] Configured transport URL has malformed syntax, invalid port, or invalid characters.",
                        remediation="Ensure HOME_ASSISTANT_BASE_URL is a valid http:// or https:// URL format.",
                    )
                )

        # 6. Actuation disabled by default
        actuation_enabled = bool(settings.HOME_ASSISTANT_ACTUATION_ENABLED)
        if not actuation_enabled:
            checks.append(
                ReadinessCheckItem(
                    name="actuation_disabled_by_default",
                    status=ReadinessCheckStatus.PASS,
                    error_category=None,
                    message="Physical actuation is safely disabled by default (HOME_ASSISTANT_ACTUATION_ENABLED=false).",
                    remediation=None,
                )
            )
        else:
            checks.append(
                ReadinessCheckItem(
                    name="actuation_disabled_by_default",
                    status=ReadinessCheckStatus.FAIL,
                    error_category=ReadinessErrorCategory.ACTUATION_ENABLED,
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
                    error_category=None,
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
                        error_category=ReadinessErrorCategory.MISSING_CONFIGURATION,
                        message="Cannot perform live read check: Home Assistant integration is disabled or access token is missing.",
                        remediation="Configure integration and access token before requesting live connectivity check.",
                    )
                )
            elif not transport_valid:
                checks.append(
                    ReadinessCheckItem(
                        name="live_test_light_readability",
                        status=ReadinessCheckStatus.FAIL,
                        error_category=transport_category or ReadinessErrorCategory.INVALID_URL,
                        message="Cannot perform live read check: transport URL configuration is invalid or rejected by security policy.",
                        remediation="Correct HOME_ASSISTANT_BASE_URL before requesting live connectivity check.",
                    )
                )
            elif not configured_light or not configured_light.startswith("light.") or not ENTITY_ID_REGEX.match(configured_light):
                checks.append(
                    ReadinessCheckItem(
                        name="live_test_light_readability",
                        status=ReadinessCheckStatus.FAIL,
                        error_category=ReadinessErrorCategory.INVALID_ENTITY,
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
                            error_category=None,
                            message=f"Successfully retrieved reported state for '{configured_light}': current reported state is '{reported_state}'. (Zero mutations performed).",
                            remediation=None,
                        )
                    )
                except EntityNotFoundError:
                    checks.append(
                        ReadinessCheckItem(
                            name="live_test_light_readability",
                            status=ReadinessCheckStatus.FAIL,
                            error_category=ReadinessErrorCategory.ENTITY_NOT_FOUND,
                            message=f"[ENTITY_NOT_FOUND] Target entity '{configured_light}' not found in Home Assistant (HTTP 404).",
                            remediation="Verify that the designated light entity exists and is exposed in Home Assistant.",
                        )
                    )
                except AuthorizationError:
                    checks.append(
                        ReadinessCheckItem(
                            name="live_test_light_readability",
                            status=ReadinessCheckStatus.FAIL,
                            error_category=ReadinessErrorCategory.AUTHENTICATION_REJECTED,
                            message="[AUTHENTICATION_REJECTED] Home Assistant rejected authentication during read-only check (HTTP 401/403).",
                            remediation="Verify that HOME_ASSISTANT_ACCESS_TOKEN is a valid Long-Lived Access Token.",
                        )
                    )
                except (HomeAssistantConnectionError, Exception):
                    checks.append(
                        ReadinessCheckItem(
                            name="live_test_light_readability",
                            status=ReadinessCheckStatus.FAIL,
                            error_category=ReadinessErrorCategory.CONNECTION_FAILED,
                            message="[CONNECTION_FAILED] Failed to connect to Home Assistant hub. Connection refused, timed out, or network unreachable.",
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


class HomeAssistantSwitchReadinessChecker:
    """Read-only preflight and commissioning readiness checker for Stage C allowlisted switch actuation.

    Invariants:
    1. NEVER calls a Home Assistant service endpoint or dispatches a mutation.
    2. NEVER operates a device or changes entity state.
    3. NEVER approves, claims, queues, or executes a capability.
    4. NEVER automatically sets HOME_ASSISTANT_SWITCH_ACTUATION_ENABLED=true.
    5. NEVER leaks secret tokens, credentials, query parameters, or raw exception strings.
    6. Offline preflight performs strictly ZERO network requests.
    7. Live read-only check requires an explicit operator opt-in (check_live=True).
    8. Structured error categories provide actionable diagnostics safely.
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
        target_entity_id: str | None = None,
        check_live: bool = False,
    ) -> SwitchActuationReadinessReport:
        checks: list[ReadinessCheckItem] = []

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
                    error_category=ReadinessErrorCategory.MISSING_CONFIGURATION,
                    message="Home Assistant integration is disabled and access token is unset.",
                    remediation="Set HOME_ASSISTANT_ENABLED=true and configure HOME_ASSISTANT_ACCESS_TOKEN in secure environment configuration.",
                )
            )
        elif not integration_enabled:
            checks.append(
                ReadinessCheckItem(
                    name="integration_configuration",
                    status=ReadinessCheckStatus.FAIL,
                    error_category=ReadinessErrorCategory.MISSING_CONFIGURATION,
                    message="Home Assistant integration is disabled by configuration (HOME_ASSISTANT_ENABLED=false).",
                    remediation="Set HOME_ASSISTANT_ENABLED=true to enable integration awareness.",
                )
            )
        elif not access_token_present:
            checks.append(
                ReadinessCheckItem(
                    name="integration_configuration",
                    status=ReadinessCheckStatus.NOT_CONFIGURED,
                    error_category=ReadinessErrorCategory.MISSING_CONFIGURATION,
                    message="Home Assistant access token is missing or empty.",
                    remediation="Configure HOME_ASSISTANT_ACCESS_TOKEN with a valid Home Assistant Long-Lived Access Token.",
                )
            )
        else:
            checks.append(
                ReadinessCheckItem(
                    name="integration_configuration",
                    status=ReadinessCheckStatus.PASS,
                    error_category=None,
                    message="Home Assistant integration is enabled with configured access token (token value withheld).",
                    remediation=None,
                )
            )

        # 2. Stage C Switch Actuation enablement
        stage_c_enabled = bool(getattr(settings, "HOME_ASSISTANT_SWITCH_ACTUATION_ENABLED", False))
        actuation_enabled = bool(settings.HOME_ASSISTANT_ACTUATION_ENABLED)
        if not stage_c_enabled:
            checks.append(
                ReadinessCheckItem(
                    name="switch_actuation_disabled_by_default",
                    status=ReadinessCheckStatus.PASS,
                    error_category=None,
                    message="Stage C switch actuation is safely disabled by default (HOME_ASSISTANT_SWITCH_ACTUATION_ENABLED=false).",
                    remediation=None,
                )
            )
        else:
            checks.append(
                ReadinessCheckItem(
                    name="switch_actuation_disabled_by_default",
                    status=ReadinessCheckStatus.FAIL,
                    error_category=ReadinessErrorCategory.ACTUATION_ENABLED,
                    message="Stage C switch actuation is currently ENABLED. For commissioning readiness preflight, actuation must remain disabled until the manual commissioning checklist is completed.",
                    remediation="Set HOME_ASSISTANT_SWITCH_ACTUATION_ENABLED=false until operator explicitly authorizes switch actuation.",
                )
            )

        # 3. Switch allowlist validation
        allowlisted_set: set[str] = set()
        allowlist_valid = False
        try:
            allowlisted_set = validate_switch_allowlist()
            allowlist_valid = True
            checks.append(
                ReadinessCheckItem(
                    name="switch_allowlist",
                    status=ReadinessCheckStatus.PASS,
                    error_category=None,
                    message=f"Stage C switch allowlist is valid with {len(allowlisted_set)} configured switch entities: {sorted(list(allowlisted_set))}.",
                    remediation=None,
                )
            )
        except ValidationError as e:
            err_msg = str(e)
            if "empty or unconfigured" in err_msg or "empty" in err_msg:
                checks.append(
                    ReadinessCheckItem(
                        name="switch_allowlist",
                        status=ReadinessCheckStatus.NOT_CONFIGURED,
                        error_category=ReadinessErrorCategory.MISSING_CONFIGURATION,
                        message="Stage C switch allowlist is empty or unconfigured (HOME_ASSISTANT_SWITCH_ALLOWLIST).",
                        remediation="Configure HOME_ASSISTANT_SWITCH_ALLOWLIST with a JSON list of switch.* entity IDs.",
                    )
                )
            else:
                checks.append(
                    ReadinessCheckItem(
                        name="switch_allowlist",
                        status=ReadinessCheckStatus.FAIL,
                        error_category=ReadinessErrorCategory.INVALID_ENTITY,
                        message=f"Stage C switch allowlist is invalid: {err_msg}",
                        remediation="Ensure HOME_ASSISTANT_SWITCH_ALLOWLIST contains only valid, exact switch.* entity IDs without wildcards.",
                    )
                )

        # 4. Target switch entity validation
        target_valid = False
        if target_entity_id is not None:
            clean_target = target_entity_id.strip()
            if not clean_target:
                checks.append(
                    ReadinessCheckItem(
                        name="target_switch_entity",
                        status=ReadinessCheckStatus.FAIL,
                        error_category=ReadinessErrorCategory.INVALID_ENTITY,
                        message="Specified target switch entity ID is empty.",
                        remediation="Provide a non-empty switch entity ID.",
                    )
                )
            elif target_entity_id != clean_target:
                checks.append(
                    ReadinessCheckItem(
                        name="target_switch_entity",
                        status=ReadinessCheckStatus.FAIL,
                        error_category=ReadinessErrorCategory.INVALID_ENTITY,
                        message="Target entity ID contains leading or trailing whitespace.",
                        remediation="Remove leading/trailing whitespace from target entity ID.",
                    )
                )
            elif not clean_target.startswith("switch."):
                checks.append(
                    ReadinessCheckItem(
                        name="target_switch_entity",
                        status=ReadinessCheckStatus.FAIL,
                        error_category=ReadinessErrorCategory.INVALID_ENTITY,
                        message=f"Target entity '{clean_target}' is not in the 'switch' domain.",
                        remediation="Only 'switch.*' entities are permitted for Stage C switch actuation.",
                    )
                )
            elif not allowlist_valid or clean_target not in allowlisted_set:
                checks.append(
                    ReadinessCheckItem(
                        name="target_switch_entity",
                        status=ReadinessCheckStatus.FAIL,
                        error_category=ReadinessErrorCategory.ENTITY_NOT_FOUND,
                        message=f"Target entity '{clean_target}' is not in the Stage C allowlist.",
                        remediation="Add target entity to HOME_ASSISTANT_SWITCH_ALLOWLIST before attempting actuation.",
                    )
                )
            else:
                target_valid = True
                checks.append(
                    ReadinessCheckItem(
                        name="target_switch_entity",
                        status=ReadinessCheckStatus.PASS,
                        error_category=None,
                        message=f"Target switch '{clean_target}' is explicitly allowlisted and valid.",
                        remediation=None,
                    )
                )
        else:
            if allowlist_valid and allowlisted_set:
                target_valid = True
                checks.append(
                    ReadinessCheckItem(
                        name="target_switch_entity",
                        status=ReadinessCheckStatus.PASS,
                        error_category=None,
                        message=f"Stage C allowlist is populated with {len(allowlisted_set)} entities; specific target not specified for preflight.",
                        remediation=None,
                    )
                )
            else:
                checks.append(
                    ReadinessCheckItem(
                        name="target_switch_entity",
                        status=ReadinessCheckStatus.NOT_CONFIGURED,
                        error_category=ReadinessErrorCategory.MISSING_CONFIGURATION,
                        message="No target entity specified and allowlist is empty or invalid.",
                        remediation="Configure HOME_ASSISTANT_SWITCH_ALLOWLIST with at least one switch.* entity.",
                    )
                )

        # 5. Approval secret credential
        approval_token_configured = bool(
            settings.APPROVAL_SECRET_TOKEN and settings.APPROVAL_SECRET_TOKEN.strip()
        )
        if not approval_token_configured:
            checks.append(
                ReadinessCheckItem(
                    name="approval_credential",
                    status=ReadinessCheckStatus.NOT_CONFIGURED,
                    error_category=ReadinessErrorCategory.MISSING_CONFIGURATION,
                    message="Server-side approval secret is not configured (APPROVAL_SECRET_TOKEN is unset).",
                    remediation="Set APPROVAL_SECRET_TOKEN in server secure configuration. Physical actuation strictly fails closed without this token.",
                )
            )
        else:
            checks.append(
                ReadinessCheckItem(
                    name="approval_credential",
                    status=ReadinessCheckStatus.PASS,
                    error_category=None,
                    message="Server-side approval secret token is configured (value withheld).",
                    remediation=None,
                )
            )

        # 6. Authorization path and fail-closed availability
        trusted_identities = getattr(settings, "TRUSTED_APPROVER_IDENTITIES", [])
        if not approval_token_configured or not trusted_identities:
            checks.append(
                ReadinessCheckItem(
                    name="authorization_path",
                    status=ReadinessCheckStatus.FAIL,
                    error_category=ReadinessErrorCategory.AUTHORIZATION_UNAVAILABLE,
                    message="Authorization path fails closed: missing APPROVAL_SECRET_TOKEN or empty TRUSTED_APPROVER_IDENTITIES list.",
                    remediation="Configure APPROVAL_SECRET_TOKEN and at least one trusted approver identity in TRUSTED_APPROVER_IDENTITIES.",
                )
            )
        else:
            checks.append(
                ReadinessCheckItem(
                    name="authorization_path",
                    status=ReadinessCheckStatus.PASS,
                    error_category=None,
                    message=f"Authorization path is active with {len(trusted_identities)} trusted approver identities, server-authenticated token validation, and fail-closed enforcement.",
                    remediation=None,
                )
            )

        # 7. Transport security and URL validation
        raw_url = (settings.HOME_ASSISTANT_BASE_URL or "").strip()
        transport_valid = False
        transport_category: ReadinessErrorCategory | None = None

        if not raw_url:
            checks.append(
                ReadinessCheckItem(
                    name="transport_security",
                    status=ReadinessCheckStatus.NOT_CONFIGURED,
                    error_category=ReadinessErrorCategory.MISSING_CONFIGURATION,
                    message="Home Assistant base URL is not configured (HOME_ASSISTANT_BASE_URL is empty).",
                    remediation="Configure HOME_ASSISTANT_BASE_URL in server environment.",
                )
            )
        else:
            try:
                parsed_u = urlparse(raw_url)
                if parsed_u.username or parsed_u.password:
                    raise ValidationError("Embedded credentials and userinfo are prohibited in base URL configuration.")
                HomeAssistantClient._validate_and_sanitize_url(
                    raw_url,
                    allow_insecure_transport=settings.HOME_ASSISTANT_ALLOW_INSECURE_TRANSPORT,
                )
                transport_valid = True
                checks.append(
                    ReadinessCheckItem(
                        name="transport_security",
                        status=ReadinessCheckStatus.PASS,
                        error_category=None,
                        message="Configured transport URL satisfies scheme, host, and security policies.",
                        remediation=None,
                    )
                )
            except ValidationError as e:
                err_text = str(e)
                if "Insecure plaintext transport" in err_text:
                    transport_category = ReadinessErrorCategory.INSECURE_TRANSPORT
                    checks.append(
                        ReadinessCheckItem(
                            name="transport_security",
                            status=ReadinessCheckStatus.FAIL,
                            error_category=ReadinessErrorCategory.INSECURE_TRANSPORT,
                            message="[INSECURE_TRANSPORT] Plaintext HTTP transport to non-loopback host is prohibited by security policy.",
                            remediation="Use 'https://' for remote hosts, or set HOME_ASSISTANT_ALLOW_INSECURE_TRANSPORT=true only in isolated testing environments.",
                        )
                    )
                else:
                    transport_category = ReadinessErrorCategory.INVALID_URL
                    checks.append(
                        ReadinessCheckItem(
                            name="transport_security",
                            status=ReadinessCheckStatus.FAIL,
                            error_category=ReadinessErrorCategory.INVALID_URL,
                            message="[INVALID_URL] Configured transport URL has invalid scheme, missing host, malformed port, or prohibited components.",
                            remediation="Ensure HOME_ASSISTANT_BASE_URL is a valid http:// or https:// URL without query parameters, fragments, or credentials.",
                        )
                    )
            except Exception:
                transport_category = ReadinessErrorCategory.INVALID_URL
                checks.append(
                    ReadinessCheckItem(
                        name="transport_security",
                        status=ReadinessCheckStatus.FAIL,
                        error_category=ReadinessErrorCategory.INVALID_URL,
                        message="[INVALID_URL] Configured transport URL has malformed syntax.",
                        remediation="Ensure HOME_ASSISTANT_BASE_URL is a valid http:// or https:// URL format.",
                    )
                )

        # 8. Live switch readability (read-only)
        reported_state: str | None = None
        state_readiness: str | None = None
        target_to_probe = target_entity_id or (sorted(list(allowlisted_set))[0] if allowlisted_set else None)

        if not check_live:
            checks.append(
                ReadinessCheckItem(
                    name="live_switch_readability",
                    status=ReadinessCheckStatus.NOT_CHECKED,
                    error_category=None,
                    message="Live read-only connectivity check was not requested. Offline preflight performed zero network requests.",
                    remediation="To perform a live read-only state check, request readiness evaluation with check_live=true.",
                )
            )
        else:
            if not integration_enabled or not access_token_present:
                state_readiness = "state_unavailable"
                checks.append(
                    ReadinessCheckItem(
                        name="live_switch_readability",
                        status=ReadinessCheckStatus.FAIL,
                        error_category=ReadinessErrorCategory.MISSING_CONFIGURATION,
                        message="Cannot perform live read check: Home Assistant integration is disabled or access token is missing.",
                        remediation="Configure integration and access token before requesting live connectivity check.",
                    )
                )
            elif not transport_valid:
                state_readiness = "state_unavailable"
                checks.append(
                    ReadinessCheckItem(
                        name="live_switch_readability",
                        status=ReadinessCheckStatus.FAIL,
                        error_category=transport_category or ReadinessErrorCategory.INVALID_URL,
                        message="Cannot perform live read check: transport URL configuration is invalid or rejected by security policy.",
                        remediation="Correct HOME_ASSISTANT_BASE_URL before requesting live connectivity check.",
                    )
                )
            elif not target_to_probe:
                state_readiness = "state_unavailable"
                checks.append(
                    ReadinessCheckItem(
                        name="live_switch_readability",
                        status=ReadinessCheckStatus.FAIL,
                        error_category=ReadinessErrorCategory.MISSING_CONFIGURATION,
                        message="Cannot perform live read check: no allowlisted switch target entity available.",
                        remediation="Configure HOME_ASSISTANT_SWITCH_ALLOWLIST with at least one switch.* entity.",
                    )
                )
            else:
                try:
                    svc = self._get_service()
                    entity_state = await svc.get_entity_state(target_to_probe)
                    raw_st = str(entity_state.state).strip().lower()
                    reported_state = raw_st
                    if raw_st in ("on", "off"):
                        state_readiness = "ready"
                        checks.append(
                            ReadinessCheckItem(
                                name="live_switch_readability",
                                status=ReadinessCheckStatus.PASS,
                                error_category=None,
                                message=f"Successfully retrieved reported state for '{target_to_probe}': current reported state is '{reported_state}' (state_readiness: ready). (Zero mutations performed).",
                                remediation=None,
                            )
                        )
                    elif raw_st in ("unavailable", "unreachable"):
                        state_readiness = "state_unavailable"
                        checks.append(
                            ReadinessCheckItem(
                                name="live_switch_readability",
                                status=ReadinessCheckStatus.FAIL,
                                error_category=ReadinessErrorCategory.STATE_UNAVAILABLE,
                                message=f"[STATE_UNAVAILABLE] Target switch '{target_to_probe}' reported state '{reported_state}'. Reliable precondition cannot be established.",
                                remediation="Verify that the physical switch/plug device is powered on, connected to the network, and accessible in Home Assistant.",
                            )
                        )
                    else:
                        state_readiness = "state_unknown"
                        checks.append(
                            ReadinessCheckItem(
                                name="live_switch_readability",
                                status=ReadinessCheckStatus.FAIL,
                                error_category=ReadinessErrorCategory.STATE_UNKNOWN,
                                message=f"[STATE_UNKNOWN] Target switch '{target_to_probe}' reported indeterminate state '{reported_state}'. Reliable precondition cannot be established.",
                                remediation="Verify that the switch integration in Home Assistant reports a valid 'on' or 'off' state.",
                            )
                        )
                except EntityNotFoundError:
                    reported_state = "unavailable"
                    state_readiness = "state_unavailable"
                    checks.append(
                        ReadinessCheckItem(
                            name="live_switch_readability",
                            status=ReadinessCheckStatus.FAIL,
                            error_category=ReadinessErrorCategory.ENTITY_NOT_FOUND,
                            message=f"[ENTITY_NOT_FOUND] Target entity '{target_to_probe}' not found in Home Assistant (HTTP 404).",
                            remediation="Verify that the switch entity exists and is exposed in Home Assistant.",
                        )
                    )
                except AuthorizationError:
                    reported_state = "unavailable"
                    state_readiness = "state_unavailable"
                    checks.append(
                        ReadinessCheckItem(
                            name="live_switch_readability",
                            status=ReadinessCheckStatus.FAIL,
                            error_category=ReadinessErrorCategory.AUTHENTICATION_REJECTED,
                            message="[AUTHENTICATION_REJECTED] Home Assistant rejected authentication during read-only check (HTTP 401/403).",
                            remediation="Verify that HOME_ASSISTANT_ACCESS_TOKEN is a valid Long-Lived Access Token.",
                        )
                    )
                except (HomeAssistantConnectionError, Exception):
                    reported_state = "unavailable"
                    state_readiness = "state_unavailable"
                    checks.append(
                        ReadinessCheckItem(
                            name="live_switch_readability",
                            status=ReadinessCheckStatus.FAIL,
                            error_category=ReadinessErrorCategory.CONNECTION_FAILED,
                            message="[CONNECTION_FAILED] Failed to connect to Home Assistant hub. Connection refused, timed out, or network unreachable.",
                            remediation="Verify that the Home Assistant hub is running and reachable at the configured base URL.",
                        )
                    )

        # Verdict
        offline_checks_passed = all(
            c.status == ReadinessCheckStatus.PASS
            for c in checks
            if c.name != "live_switch_readability"
        )
        live_check_item = next(
            (c for c in checks if c.name == "live_switch_readability"), None
        )
        live_passed = live_check_item is not None and live_check_item.status == ReadinessCheckStatus.PASS
        ready_for_commissioning = offline_checks_passed and live_passed

        if ready_for_commissioning:
            summary = (
                f"READY FOR COMMISSIONING: All configuration checks and live read-only inspection passed for "
                f"switch target '{target_to_probe}' (current state: '{reported_state}'). Stage C switch actuation remains safely disabled. "
                f"Operator may proceed to the manual commissioning checklist."
            )
        elif offline_checks_passed and not check_live:
            summary = (
                f"OFFLINE PREFLIGHT PASSED: All configuration, transport, allowlist, and authorization checks passed. "
                f"Live read-only connectivity check has NOT been performed. Run with check_live=true to verify live entity inspection."
            )
        else:
            failed_checks = [c.name for c in checks if c.status in (ReadinessCheckStatus.FAIL, ReadinessCheckStatus.NOT_CONFIGURED)]
            summary = f"NOT READY: Stage C switch commissioning preflight incomplete or failed on: {', '.join(failed_checks)}."

        return SwitchActuationReadinessReport(
            ready_for_commissioning=ready_for_commissioning,
            target_entity_id=target_to_probe,
            allowlisted_entities=sorted(list(allowlisted_set)),
            stage_c_enabled=stage_c_enabled,
            actuation_enabled=actuation_enabled,
            live_check_performed=check_live,
            live_passed=live_passed if check_live else None,
            reported_state=reported_state,
            state_readiness=state_readiness,
            checks=checks,
            summary=summary,
        )


async def evaluate_switch_actuation_readiness(
    target_entity_id: str | None = None,
    check_live: bool = False,
    readonly_client: HomeAssistantClient | None = None,
) -> SwitchActuationReadinessReport:
    """Convenience helper to evaluate Stage C allowlisted switch actuation readiness."""
    checker = HomeAssistantSwitchReadinessChecker(readonly_client=readonly_client)
    return await checker.evaluate_readiness(
        target_entity_id=target_entity_id,
        check_live=check_live,
    )


class HomeAssistantClimateReadinessChecker:
    """Read-only preflight and commissioning readiness checker for Stage D climate awareness.

    Invariants:
    1. NEVER calls a Home Assistant service endpoint or dispatches a mutation.
    2. NEVER operates a thermostat or changes entity state.
    3. NEVER approves, claims, queues, or executes an actuation capability.
    4. NEVER leaks secret tokens, credentials, query parameters, or raw exception strings.
    5. Offline preflight performs strictly ZERO network requests.
    6. Live read-only check requires an explicit operator opt-in (check_live=True).
    7. Structured error categories provide actionable diagnostics safely.
    """

    def __init__(
        self,
        service: HomeAssistantService | None = None,
        readonly_client: HomeAssistantClient | None = None,
    ) -> None:
        self._service = service
        self._readonly_client = readonly_client
        self._climate_service = HomeAssistantClimateService(
            service=service,
            readonly_client=readonly_client,
        )

    async def evaluate_readiness(
        self,
        target_entity_id: str | None = None,
        check_live: bool = False,
    ) -> ClimateReadinessReport:
        checks: list[ReadinessCheckItem] = []

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
                    error_category=ReadinessErrorCategory.MISSING_CONFIGURATION,
                    message="Home Assistant integration is disabled and access token is unset.",
                    remediation="Set HOME_ASSISTANT_ENABLED=true and configure HOME_ASSISTANT_ACCESS_TOKEN in secure environment configuration.",
                )
            )
        elif not integration_enabled:
            checks.append(
                ReadinessCheckItem(
                    name="integration_configuration",
                    status=ReadinessCheckStatus.FAIL,
                    error_category=ReadinessErrorCategory.MISSING_CONFIGURATION,
                    message="Home Assistant integration is disabled by configuration (HOME_ASSISTANT_ENABLED=false).",
                    remediation="Set HOME_ASSISTANT_ENABLED=true to enable integration awareness.",
                )
            )
        elif not access_token_present:
            checks.append(
                ReadinessCheckItem(
                    name="integration_configuration",
                    status=ReadinessCheckStatus.NOT_CONFIGURED,
                    error_category=ReadinessErrorCategory.MISSING_CONFIGURATION,
                    message="Home Assistant access token is missing or empty.",
                    remediation="Configure HOME_ASSISTANT_ACCESS_TOKEN with a valid Home Assistant Long-Lived Access Token.",
                )
            )
        else:
            checks.append(
                ReadinessCheckItem(
                    name="integration_configuration",
                    status=ReadinessCheckStatus.PASS,
                    error_category=None,
                    message="Home Assistant integration is enabled with configured access token (token value withheld).",
                    remediation=None,
                )
            )

        # 2. Stage D Climate Awareness enablement
        climate_awareness_enabled = bool(getattr(settings, "HOME_ASSISTANT_CLIMATE_AWARENESS_ENABLED", False))
        if not climate_awareness_enabled:
            checks.append(
                ReadinessCheckItem(
                    name="climate_awareness_configuration",
                    status=ReadinessCheckStatus.FAIL,
                    error_category=ReadinessErrorCategory.MISSING_CONFIGURATION,
                    message="Stage D climate awareness is disabled by configuration (HOME_ASSISTANT_CLIMATE_AWARENESS_ENABLED=false).",
                    remediation="Set HOME_ASSISTANT_CLIMATE_AWARENESS_ENABLED=true to enable climate awareness and setpoint feasibility planning.",
                )
            )
        else:
            checks.append(
                ReadinessCheckItem(
                    name="climate_awareness_configuration",
                    status=ReadinessCheckStatus.PASS,
                    error_category=None,
                    message="Stage D climate awareness is enabled by configuration.",
                    remediation=None,
                )
            )

        # 3. Climate allowlist validation
        allowlisted_set: set[str] = set()
        allowlist_valid = False
        try:
            allowlisted_set = validate_climate_allowlist()
            allowlist_valid = True
            checks.append(
                ReadinessCheckItem(
                    name="climate_allowlist",
                    status=ReadinessCheckStatus.PASS,
                    error_category=None,
                    message=f"Stage D climate allowlist is valid with {len(allowlisted_set)} configured climate entities: {sorted(list(allowlisted_set))}.",
                    remediation=None,
                )
            )
        except ValidationError as exc:
            err_msg = str(exc)
            if "empty" in err_msg.lower() or "unconfigured" in err_msg.lower():
                checks.append(
                    ReadinessCheckItem(
                        name="climate_allowlist",
                        status=ReadinessCheckStatus.NOT_CONFIGURED,
                        error_category=ReadinessErrorCategory.MISSING_CONFIGURATION,
                        message="Stage D climate allowlist is empty or unconfigured (HOME_ASSISTANT_CLIMATE_ALLOWLIST).",
                        remediation="Configure HOME_ASSISTANT_CLIMATE_ALLOWLIST with a JSON list of climate.* entity IDs.",
                    )
                )
            else:
                checks.append(
                    ReadinessCheckItem(
                        name="climate_allowlist",
                        status=ReadinessCheckStatus.FAIL,
                        error_category=ReadinessErrorCategory.INVALID_ENTITY,
                        message=f"Stage D climate allowlist is invalid: {err_msg}",
                        remediation="Ensure HOME_ASSISTANT_CLIMATE_ALLOWLIST contains only valid, exact climate.* entity IDs without wildcards.",
                    )
                )

        # 4. Target entity membership check
        target_to_probe: str | None = None
        if target_entity_id:
            clean_target = target_entity_id.strip()
            if not clean_target.startswith("climate."):
                checks.append(
                    ReadinessCheckItem(
                        name="target_climate_entity_membership",
                        status=ReadinessCheckStatus.FAIL,
                        error_category=ReadinessErrorCategory.INVALID_ENTITY,
                        message=f"Target entity '{clean_target}' is not in the 'climate' domain.",
                        remediation="Ensure target entity ID begins with 'climate.'.",
                    )
                )
            elif not allowlist_valid or clean_target not in allowlisted_set:
                checks.append(
                    ReadinessCheckItem(
                        name="target_climate_entity_membership",
                        status=ReadinessCheckStatus.FAIL,
                        error_category=ReadinessErrorCategory.NOT_ALLOWLISTED,
                        message=f"Target entity '{clean_target}' is not in HOME_ASSISTANT_CLIMATE_ALLOWLIST: {sorted(list(allowlisted_set))}.",
                        remediation="Add target entity to HOME_ASSISTANT_CLIMATE_ALLOWLIST before attempting observation.",
                    )
                )
            else:
                target_to_probe = clean_target
                checks.append(
                    ReadinessCheckItem(
                        name="target_climate_entity_membership",
                        status=ReadinessCheckStatus.PASS,
                        error_category=None,
                        message=f"Target entity '{clean_target}' is explicitly present in HOME_ASSISTANT_CLIMATE_ALLOWLIST.",
                        remediation=None,
                    )
                )
        else:
            if allowlist_valid and allowlisted_set:
                target_to_probe = sorted(list(allowlisted_set))[0]
                checks.append(
                    ReadinessCheckItem(
                        name="target_climate_entity_membership",
                        status=ReadinessCheckStatus.PASS,
                        error_category=None,
                        message=f"No specific target entity specified. Defaulting to first allowlisted entity: '{target_to_probe}'.",
                        remediation=None,
                    )
                )
            else:
                checks.append(
                    ReadinessCheckItem(
                        name="target_climate_entity_membership",
                        status=ReadinessCheckStatus.NOT_CONFIGURED,
                        error_category=ReadinessErrorCategory.MISSING_CONFIGURATION,
                        message="Cannot evaluate target entity membership because allowlist is invalid or empty.",
                        remediation="Configure HOME_ASSISTANT_CLIMATE_ALLOWLIST with at least one climate.* entity.",
                    )
                )

        # 5. Trusted grid origins configuration check
        try:
            grid_origins_map = validate_climate_grid_origins(
                allowlist=allowlisted_set if allowlist_valid else None
            )
            checks.append(
                ReadinessCheckItem(
                    name="climate_grid_origins",
                    status=ReadinessCheckStatus.PASS,
                    error_category=None,
                    message=f"Stage D trusted climate grid origins configuration is valid ({len(grid_origins_map)} configured entity origins).",
                    remediation=None,
                )
            )
        except ValidationError as exc:
            checks.append(
                ReadinessCheckItem(
                    name="climate_grid_origins",
                    status=ReadinessCheckStatus.FAIL,
                    error_category=ReadinessErrorCategory.INVALID_ENTITY,
                    message=f"Stage D trusted climate grid origins configuration is invalid: {exc}",
                    remediation="Ensure HOME_ASSISTANT_CLIMATE_GRID_ORIGINS contains only valid JSON mapping allowlisted climate.* entities to {'origin': float, 'unit': str}.",
                )
            )

        # 6. Actuation boundary check
        actuation_enabled = bool(
            integration_enabled
            and access_token_present
            and climate_awareness_enabled
            and settings.HOME_ASSISTANT_ACTUATION_ENABLED
            and getattr(settings, "HOME_ASSISTANT_CLIMATE_ACTUATION_ENABLED", False)
        )
        if actuation_enabled:
            checks.append(
                ReadinessCheckItem(
                    name="actuation_boundary",
                    status=ReadinessCheckStatus.PASS,
                    error_category=None,
                    message="Climate physical actuation is explicitly enabled (HOME_ASSISTANT_ENABLED=true, access token present, climate awareness enabled, HOME_ASSISTANT_ACTUATION_ENABLED=true, and HOME_ASSISTANT_CLIMATE_ACTUATION_ENABLED=true).",
                    remediation=None,
                )
            )
        else:
            checks.append(
                ReadinessCheckItem(
                    name="actuation_boundary",
                    status=ReadinessCheckStatus.PASS,
                    error_category=None,
                    message="Climate physical actuation is disabled by policy or missing prerequisites (strictly read-only awareness mode).",
                    remediation=None,
                )
            )

        # 7. Transport security check
        base_url = settings.HOME_ASSISTANT_BASE_URL
        allow_insecure = settings.HOME_ASSISTANT_ALLOW_INSECURE_TRANSPORT
        try:
            parsed_url = urlparse(base_url)
            scheme = (parsed_url.scheme or "").lower()
            host = parsed_url.hostname or ""
            if scheme not in ("http", "https"):
                checks.append(
                    ReadinessCheckItem(
                        name="transport_security",
                        status=ReadinessCheckStatus.FAIL,
                        error_category=ReadinessErrorCategory.INVALID_URL,
                        message=f"Invalid base URL scheme '{scheme}'. Must be http or https.",
                        remediation="Configure HOME_ASSISTANT_BASE_URL starting with http:// or https://.",
                    )
                )
            elif scheme == "http" and host not in ("127.0.0.1", "localhost", "::1") and not allow_insecure:
                checks.append(
                    ReadinessCheckItem(
                        name="transport_security",
                        status=ReadinessCheckStatus.FAIL,
                        error_category=ReadinessErrorCategory.INSECURE_TRANSPORT,
                        message=f"Insecure transport ('http://') to non-loopback host '{host}' is prohibited.",
                        remediation="Use 'https://' or set HOME_ASSISTANT_ALLOW_INSECURE_TRANSPORT=true for non-loopback connections.",
                    )
                )
            else:
                checks.append(
                    ReadinessCheckItem(
                        name="transport_security",
                        status=ReadinessCheckStatus.PASS,
                        error_category=None,
                        message=f"Transport configuration is valid and satisfies security policy ({scheme}://{host}).",
                        remediation=None,
                    )
                )
        except Exception as exc:
            checks.append(
                ReadinessCheckItem(
                    name="transport_security",
                    status=ReadinessCheckStatus.FAIL,
                    error_category=ReadinessErrorCategory.INVALID_URL,
                    message=f"Failed to parse HOME_ASSISTANT_BASE_URL: {exc}",
                    remediation="Configure HOME_ASSISTANT_BASE_URL as a valid RFC-compliant URL.",
                )
            )

        # 8. Live Read-Only Connectivity Check
        live_passed: bool | None = None
        reported_state: str | None = None
        state_readiness: str | None = None
        observation: ClimateObservation | None = None

        if not check_live:
            checks.append(
                ReadinessCheckItem(
                    name="live_climate_readability",
                    status=ReadinessCheckStatus.NOT_CHECKED,
                    error_category=None,
                    message="Live read-only connectivity check not performed (offline preflight). Pass check_live=True to probe.",
                    remediation=None,
                )
            )
        else:
            if not target_to_probe:
                live_passed = False
                checks.append(
                    ReadinessCheckItem(
                        name="live_climate_readability",
                        status=ReadinessCheckStatus.FAIL,
                        error_category=ReadinessErrorCategory.MISSING_CONFIGURATION,
                        message="Live read-only check skipped: no valid allowlisted target entity available to probe.",
                        remediation="Configure HOME_ASSISTANT_CLIMATE_ALLOWLIST with at least one climate.* entity.",
                    )
                )
            else:
                try:
                    obs = await self._climate_service.get_climate_observation(target_to_probe)
                    observation = obs
                    reported_state = obs.state
                    state_readiness = obs.state_readiness

                    if obs.state_readiness == "state_unavailable":
                        live_passed = False
                        checks.append(
                            ReadinessCheckItem(
                                name="live_climate_readability",
                                status=ReadinessCheckStatus.FAIL,
                                error_category=ReadinessErrorCategory.STATE_UNAVAILABLE,
                                message=f"Live climate readability failed: entity '{target_to_probe}' is unreachable or unavailable.",
                                remediation="Verify Home Assistant server is running and climate entity is online.",
                            )
                        )
                    elif obs.state_readiness == "state_unknown":
                        live_passed = False
                        checks.append(
                            ReadinessCheckItem(
                                name="live_climate_readability",
                                status=ReadinessCheckStatus.FAIL,
                                error_category=ReadinessErrorCategory.STATE_UNKNOWN,
                                message=f"Live climate readability failed: reported state or bounds for '{target_to_probe}' are unknown or invalid ({obs.reasons}).",
                                remediation="Ensure climate device reports valid finite operating temperature bounds.",
                            )
                        )
                    elif obs.state_readiness == "unit_unknown":
                        live_passed = False
                        checks.append(
                            ReadinessCheckItem(
                                name="live_climate_readability",
                                status=ReadinessCheckStatus.FAIL,
                                error_category=ReadinessErrorCategory.UNIT_UNKNOWN,
                                message=f"Live climate readability failed: temperature unit for '{target_to_probe}' cannot be established.",
                                remediation="Ensure climate device reports a trustworthy unit_of_measurement (°C or °F).",
                            )
                        )
                    else:
                        live_passed = True
                        checks.append(
                            ReadinessCheckItem(
                                name="live_climate_readability",
                                status=ReadinessCheckStatus.PASS,
                                error_category=None,
                                message=f"Live read-only inspection succeeded for '{target_to_probe}': mode={obs.state}, temp={obs.current_temperature}{obs.temperature_unit or ''}, bounds=[{obs.min_temp}, {obs.max_temp}].",
                                remediation=None,
                            )
                        )
                except EntityNotFoundError:
                    live_passed = False
                    checks.append(
                        ReadinessCheckItem(
                            name="live_climate_readability",
                            status=ReadinessCheckStatus.FAIL,
                            error_category=ReadinessErrorCategory.ENTITY_NOT_FOUND,
                            message=f"Live read-only inspection failed: entity '{target_to_probe}' was not found in Home Assistant.",
                            remediation="Verify entity exists in Home Assistant under Developer Tools -> States.",
                        )
                    )
                except AuthorizationError:
                    live_passed = False
                    checks.append(
                        ReadinessCheckItem(
                            name="live_climate_readability",
                            status=ReadinessCheckStatus.FAIL,
                            error_category=ReadinessErrorCategory.AUTHENTICATION_REJECTED,
                            message="Live read-only inspection failed: Home Assistant rejected the access token (401 Unauthorized).",
                            remediation="Generate a new Long-Lived Access Token in Home Assistant and configure HOME_ASSISTANT_ACCESS_TOKEN.",
                        )
                    )
                except (ConnectionError, HomeAssistantConnectionError) as exc:
                    live_passed = False
                    checks.append(
                        ReadinessCheckItem(
                            name="live_climate_readability",
                            status=ReadinessCheckStatus.FAIL,
                            error_category=ReadinessErrorCategory.CONNECTION_FAILED,
                            message=f"Live read-only inspection failed: could not connect to Home Assistant at '{base_url}'.",
                            remediation="Verify Home Assistant server is running and reachable from PICO.",
                        )
                    )
                except Exception as exc:
                    live_passed = False
                    checks.append(
                        ReadinessCheckItem(
                            name="live_climate_readability",
                            status=ReadinessCheckStatus.FAIL,
                            error_category=ReadinessErrorCategory.CONNECTION_FAILED,
                            message=f"Live read-only inspection failed: {type(exc).__name__}",
                            remediation="Check Home Assistant logs and network reachability.",
                        )
                    )

        ready_for_observation = all(
            c.status == ReadinessCheckStatus.PASS
            for c in checks
            if c.status != ReadinessCheckStatus.NOT_CHECKED
        )

        if ready_for_observation:
            summary = (
                f"READY: Stage D climate awareness preflight passed for '{target_to_probe}'. "
                + (
                    "Climate setpoint actuation is enabled (approval still required)."
                    if actuation_enabled
                    else "Climate awareness is ready (physical actuation disabled)."
                )
            )
        else:
            failed_checks = [
                c.name
                for c in checks
                if c.status in (ReadinessCheckStatus.FAIL, ReadinessCheckStatus.NOT_CONFIGURED)
            ]
            summary = f"NOT READY: Stage D climate awareness preflight incomplete or failed on: {', '.join(failed_checks)}."

        return ClimateReadinessReport(
            ready_for_observation=ready_for_observation,
            target_entity_id=target_to_probe,
            allowlisted_entities=sorted(list(allowlisted_set)),
            climate_awareness_enabled=climate_awareness_enabled,
            actuation_enabled=actuation_enabled,
            live_check_performed=check_live,
            live_passed=live_passed if check_live else None,
            state_readiness=state_readiness,
            observation=observation,
            checks=checks,
            summary=summary,
        )


async def evaluate_climate_readiness(
    target_entity_id: str | None = None,
    check_live: bool = False,
    readonly_client: HomeAssistantClient | None = None,
) -> ClimateReadinessReport:
    """Convenience helper to evaluate Stage D climate awareness readiness."""
    checker = HomeAssistantClimateReadinessChecker(readonly_client=readonly_client)
    return await checker.evaluate_readiness(
        target_entity_id=target_entity_id,
        check_live=check_live,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for offline and live readiness preflight."""
    parser = argparse.ArgumentParser(
        description="PICO Home Assistant Commissioning & Readiness Preflight"
    )
    parser.add_argument(
        "--switch",
        action="store_true",
        default=False,
        help="Evaluate Stage C switch actuation readiness instead of P7.3 test light.",
    )
    parser.add_argument(
        "--climate",
        action="store_true",
        default=False,
        help="Evaluate Stage D climate awareness and readiness.",
    )
    parser.add_argument(
        "--entity",
        type=str,
        default=None,
        help="Specific target entity ID to check (for Stage C switch or Stage D climate).",
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

    if args.climate:
        report = asyncio.run(
            evaluate_climate_readiness(
                target_entity_id=args.entity,
                check_live=args.live,
            )
        )
        title = "PICO P7.4.2 Stage D Climate Awareness & Readiness Report"
    elif args.switch:
        report = asyncio.run(
            evaluate_switch_actuation_readiness(
                target_entity_id=args.entity,
                check_live=args.live,
            )
        )
        title = "PICO P7.4.1 Stage C Switch Commissioning & Readiness Report"
    else:
        report = asyncio.run(evaluate_actuation_readiness(check_live=args.live))
        title = "PICO P7.3 Test Light Commissioning & Readiness Report"

    if args.json:
        print(report.model_dump_json(indent=2))
    else:
        print("=" * 70)
        print(title)
        print("=" * 70)
        print(f"Target Entity ID : {report.target_entity_id or 'NOT SPECIFIED'}")
        print(f"Actuation Enabled: {report.actuation_enabled}")
        if hasattr(report, "climate_awareness_enabled"):
            print(f"Climate Enabled  : {report.climate_awareness_enabled}")
        if hasattr(report, "stage_c_enabled"):
            print(f"Stage C Enabled  : {report.stage_c_enabled}")
        if hasattr(report, "allowlisted_entities"):
            print(f"Allowlisted      : {report.allowlisted_entities}")
        print(f"Live Check Run   : {report.live_check_performed}")
        if getattr(report, "reported_state", None):
            print(f"Reported State   : {report.reported_state}")
        print("-" * 70)
        print("Checks:")
        for check in report.checks:
            cat_str = f" [{check.error_category.value}]" if check.error_category else ""
            print(f"  [{check.status.value:<14}]{cat_str} {check.name}: {check.message}")
            if check.remediation:
                print(f"                 Remediation: {check.remediation}")
        print("-" * 70)
        verdict = getattr(report, "ready_for_observation", getattr(report, "ready_for_commissioning", False))
        print(f"Verdict: {'READY' if verdict else 'NOT READY'}")
        print(f"Summary: {report.summary}")
        print("=" * 70)

    verdict = getattr(report, "ready_for_observation", getattr(report, "ready_for_commissioning", False))
    return 0 if verdict else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
