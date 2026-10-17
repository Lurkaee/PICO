import pytest

from pico.core.config import settings
from pico.core.errors import ValidationError
from pico.services.routing_service import RoutingService


def test_routing_classification_normalization_defaults_to_private() -> None:
    router = RoutingService()
    assert router.normalize_classification(None) == "INTERNAL"
    assert router.normalize_classification("") == "INTERNAL"
    assert router.normalize_classification("invalid_level") == "INTERNAL"
    assert router.normalize_classification("public") == "PUBLIC"
    assert router.normalize_classification("secret") == "SECRET"


def test_routing_secret_data_hard_local_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    # Even if cloud is enabled
    monkeypatch.setattr(settings, "CLOUD_ENABLED", True)
    monkeypatch.setattr(settings, "DEFAULT_PROVIDER", "mock")

    router = RoutingService()
    decision = router.resolve_route(
        task_category="planning",
        data_classification="SECRET",
    )

    assert decision.selected_profile == "mock"
    assert decision.provider == "mock"
    assert decision.decision_rule == "RULE_SECRET_LOCAL_PIN"
    assert decision.cloud_approved is False
    assert decision.fallback_profiles == []

    # Attempting to explicitly request cloud on SECRET data raises ValidationError
    with pytest.raises(ValidationError) as exc_info:
        router.resolve_route(
            task_category="coding",
            data_classification="SECRET",
            requested_profile="cloud-strong",
        )
    assert "strictly prohibited" in str(exc_info.value)


def test_routing_private_data_remains_local_even_with_cloud_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "CLOUD_ENABLED", True)
    monkeypatch.setattr(settings, "DEFAULT_PROVIDER", "mock")

    router = RoutingService()

    for private_level in ["INTERNAL", "SENSITIVE"]:
        decision = router.resolve_route(
            task_category="planning",
            data_classification=private_level,
        )
        assert decision.selected_profile == "mock"
        assert decision.decision_rule == "RULE_PRIVATE_LOCAL_PIN"
        assert decision.cloud_approved is False

        # Explicitly requesting cloud on private data is rejected
        with pytest.raises(ValidationError):
            router.resolve_route(
                task_category="planning",
                data_classification=private_level,
                requested_profile="cloud-strong",
            )


def test_routing_public_data_with_cloud_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "CLOUD_ENABLED", False)
    monkeypatch.setattr(settings, "DEFAULT_PROVIDER", "mock")

    router = RoutingService()
    # Complex task with PUBLIC data but cloud is disabled
    decision = router.resolve_route(
        task_category="planning",
        data_classification="PUBLIC",
    )

    assert decision.selected_profile == "mock"
    assert decision.decision_rule == "RULE_CLOUD_DISABLED_FALLBACK"
    assert decision.cloud_approved is False


def test_routing_public_data_with_cloud_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "CLOUD_ENABLED", True)
    monkeypatch.setattr(settings, "CLOUD_MODEL", "gpt-4o-mini")

    router = RoutingService()
    decision = router.resolve_route(
        task_category="planning",
        data_classification="PUBLIC",
    )

    assert decision.selected_profile == "cloud-strong"
    assert decision.provider == "cloud"
    assert decision.model_name == "gpt-4o-mini"
    assert decision.decision_rule == "RULE_CLOUD_ELIGIBLE_APPROVED"
    assert decision.cloud_approved is True
    assert "mock" in decision.fallback_profiles or "local-small" in decision.fallback_profiles


def test_routing_routine_public_task_defaults_to_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "CLOUD_ENABLED", True)
    monkeypatch.setattr(settings, "DEFAULT_PROVIDER", "mock")

    router = RoutingService()
    decision = router.resolve_route(
        task_category="general_chat",
        data_classification="PUBLIC",
    )

    # Routine task stays local even if cloud is enabled
    assert decision.selected_profile == "mock"
    assert decision.decision_rule == "RULE_ROUTINE_LOCAL"
    assert decision.cloud_approved is False


def test_compute_effective_classification_hierarchy() -> None:
    router = RoutingService()
    # All public
    assert router.compute_effective_classification(["PUBLIC", "PUBLIC"]) == "PUBLIC"
    # Mixed internal + public -> internal
    assert router.compute_effective_classification(["PUBLIC", "INTERNAL"]) == "INTERNAL"
    # Mixed sensitive + internal + public -> sensitive
    assert router.compute_effective_classification(["PUBLIC", "INTERNAL", "SENSITIVE"]) == "SENSITIVE"
    # Secret dominates all
    assert router.compute_effective_classification(["PUBLIC", "SENSITIVE", "SECRET"]) == "SECRET"
    assert router.compute_effective_classification(["SECRET", "INTERNAL"]) == "SECRET"
    # Unknown or invalid defaults to internal (private)
    assert router.compute_effective_classification(["PUBLIC", "BOGUS_LEVEL"]) == "INTERNAL"
    assert router.compute_effective_classification(["PUBLIC", None]) == "INTERNAL"
    # Empty context defaults to internal
    assert router.compute_effective_classification([]) == "INTERNAL"


def test_resolve_route_mixed_context_pins_to_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "CLOUD_ENABLED", True)
    monkeypatch.setattr(settings, "DEFAULT_PROVIDER", "mock")

    router = RoutingService()
    # User message is PUBLIC on a reasoning task (which would normally go to cloud),
    # but context contains an INTERNAL message.
    decision = router.resolve_route(
        task_category="planning",
        data_classification="PUBLIC",
        context_classifications=["PUBLIC", "INTERNAL"],
    )

    assert decision.data_classification == "INTERNAL"
    assert decision.selected_profile == "mock"
    assert decision.provider == "mock"
    assert decision.decision_rule == "RULE_PRIVATE_LOCAL_PIN"
    assert decision.cloud_approved is False
    assert decision.fallback_profiles == []


def test_resolve_route_mixed_context_rejects_explicit_cloud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "CLOUD_ENABLED", True)
    monkeypatch.setattr(settings, "DEFAULT_PROVIDER", "mock")

    router = RoutingService()

    # Mixed with INTERNAL
    with pytest.raises(ValidationError) as exc_internal:
        router.resolve_route(
            task_category="planning",
            data_classification="PUBLIC",
            context_classifications=["PUBLIC", "INTERNAL"],
            requested_profile="cloud-strong",
        )
    assert "not eligible for cloud" in str(exc_internal.value)

    # Mixed with SECRET
    with pytest.raises(ValidationError) as exc_secret:
        router.resolve_route(
            task_category="coding",
            data_classification="PUBLIC",
            context_classifications=["PUBLIC", "SECRET"],
            requested_profile="cloud-strong",
        )
    assert "strictly prohibited" in str(exc_secret.value)
