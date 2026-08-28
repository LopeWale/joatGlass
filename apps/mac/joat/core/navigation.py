"""Provider-neutral navigation domain objects.

Provider adapters may change; the destination-confirmation and route-install
contract does not. Spoken query text is deliberately absent from these durable
objects: only provider-canonical candidates cross the HUD boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .protocol import NavigationProgressPolicy, NavigationRoutePlan

NavigationMode = Literal["walking", "cycling"]


@dataclass(frozen=True, slots=True)
class NavigationPoint:
    latitude: float
    longitude: float


@dataclass(frozen=True, slots=True)
class NavigationCandidate:
    choice_id: str
    label: str
    address: str
    point: NavigationPoint
    repair_hint: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderRoute:
    route_id: str
    eta_minutes: float
    plan: NavigationRoutePlan


@dataclass(frozen=True, slots=True)
class NavigationPolicies:
    """Explicit pre-field candidate policy per supported travel mode."""

    walking: NavigationProgressPolicy
    cycling: NavigationProgressPolicy

    def for_mode(self, mode: NavigationMode) -> NavigationProgressPolicy:
        return self.walking if mode == "walking" else self.cycling
