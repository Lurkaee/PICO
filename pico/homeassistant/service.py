from typing import Any
from pico.core.errors import EntityNotFoundError, ValidationError
from pico.homeassistant.classification import compute_trusted_ha_classification
from pico.homeassistant.client import HomeAssistantClient
from pico.homeassistant.contracts import (
    EntityListOutput,
    EntityOverviewOutput,
    EntityState,
    HomeAssistantStatus,
)
from pico.homeassistant.sanitizer import sanitize_entity_attributes


class HomeAssistantService:
    """High-level service for Home Assistant read-only discovery and awareness."""

    def __init__(self, client: HomeAssistantClient | None = None) -> None:
        self._client = client or HomeAssistantClient()

    async def get_status(self) -> HomeAssistantStatus:
        """Get integration status and connectivity state."""
        return await self._client.get_status()

    def _normalize_entity(self, raw: dict[str, Any]) -> EntityState:
        entity_id = str(raw.get("entity_id", "")).strip()
        if "." not in entity_id:
            raise ValidationError(f"Invalid entity format in Home Assistant state: '{entity_id}'")

        domain = entity_id.split(".", 1)[0].lower()
        state = str(raw.get("state", "unknown"))
        raw_attrs = raw.get("attributes")
        clean_attrs = sanitize_entity_attributes(raw_attrs if isinstance(raw_attrs, dict) else {})

        friendly_name = clean_attrs.get("friendly_name")
        device_class = clean_attrs.get("device_class")
        unit_of_measurement = clean_attrs.get("unit_of_measurement")

        classification = compute_trusted_ha_classification(
            entity_id=entity_id,
            domain=domain,
            attributes=clean_attrs,
        )

        return EntityState(
            entity_id=entity_id,
            domain=domain,
            state=state,
            friendly_name=str(friendly_name) if friendly_name is not None else None,
            device_class=str(device_class) if device_class is not None else None,
            unit_of_measurement=str(unit_of_measurement) if unit_of_measurement is not None else None,
            data_classification=classification,
            last_changed=str(raw.get("last_changed")) if raw.get("last_changed") else None,
            last_updated=str(raw.get("last_updated")) if raw.get("last_updated") else None,
            attributes=clean_attrs,
        )

    async def list_entities(self, domain: str | None = None) -> EntityListOutput:
        """Discover and return reported entity states, optionally filtered by domain."""
        raw_states = await self._client.get_states()
        entities: list[EntityState] = []
        domain_filter = domain.strip().lower() if domain else None

        for raw in raw_states:
            try:
                norm = self._normalize_entity(raw)
                if domain_filter and norm.domain != domain_filter:
                    continue
                entities.append(norm)
            except Exception:
                continue

        # Effective classification across all returned entities
        effective_classification = "SENSITIVE" if any(e.data_classification == "SENSITIVE" for e in entities) else "INTERNAL"

        return EntityListOutput(
            total_count=len(entities),
            domain_filter=domain_filter,
            entities=entities,
            data_classification=effective_classification,
        )

    async def get_entity_state(self, entity_id: str) -> EntityState:
        """Retrieve the reported state of a specific smart-home entity."""
        raw = await self._client.get_state(entity_id)
        return self._normalize_entity(raw)

    async def get_overview(self, domains: list[str] | None = None) -> EntityOverviewOutput:
        """Retrieve a bounded summary overview of smart-home entities suitable for assistant awareness."""
        raw_states = await self._client.get_states()
        domain_counts: dict[str, int] = {}
        target_domains = {d.strip().lower() for d in domains} if domains else None

        highlights: list[EntityState] = []
        for raw in raw_states:
            try:
                norm = self._normalize_entity(raw)
                dom = norm.domain
                domain_counts[dom] = domain_counts.get(dom, 0) + 1

                if target_domains and dom not in target_domains:
                    continue

                # Collect a bounded selection of highlights (max 20 entities total)
                if len(highlights) < 20:
                    highlights.append(norm)
            except Exception:
                continue

        effective_classification = "SENSITIVE" if any(h.data_classification == "SENSITIVE" for h in highlights) else "INTERNAL"

        return EntityOverviewOutput(
            total_entities=sum(domain_counts.values()),
            domain_counts=domain_counts,
            highlights=highlights,
            data_classification=effective_classification,
        )
