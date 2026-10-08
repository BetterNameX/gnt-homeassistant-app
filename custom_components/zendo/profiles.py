"""Site profiles listed by the control backend: people and device groups.

A device group holds the wall panels and tablets that belong to the home
rather than to a person. It can be the target of a doorbell ring, but never
of a push notification, so each kind of profile gets its own entity: a
notify entity for a person, an event entity for a device group.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol

from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

PROFILE_CAN_RECEIVE_PUSH_NOTIFICATIONS = "canReceivePushNotifications"


class ProfileEntity(Protocol):
    """An entity that stands for one profile."""

    @property
    def profile_id(self) -> str:
        """Return the profile this entity stands for."""

    async def async_remove(self, *, force_remove: bool = False) -> None:
        """Remove the entity from Home Assistant."""


def can_receive_push_notifications(profile: Mapping[str, Any]) -> bool:
    """Return True for a person, False for a device group."""
    # A profile cached by an older version of this integration has no flag and
    # counts as a person. That is safe because the backend never pushes to a
    # device group, whatever it is sent.
    return bool(profile.get(PROFILE_CAN_RECEIVE_PUSH_NOTIFICATIONS, True))


def people(profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the profiles that can receive push notifications."""
    return [p for p in profiles if can_receive_push_notifications(p)]


def device_groups(profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the device groups."""
    return [p for p in profiles if not can_receive_push_notifications(p)]


def profiles_predate_device_groups(profiles: list[dict[str, Any]]) -> bool:
    """Return True if any cached profile was stored without the flag."""
    return any(PROFILE_CAN_RECEIVE_PUSH_NOTIFICATIONS not in p for p in profiles)


async def async_sync_profile_entities(
    live_entities: Mapping[str, ProfileEntity],
    profiles: list[dict[str, Any]],
    async_add_entities: AddEntitiesCallback | None,
    create_entity: Callable[[dict[str, Any]], Entity],
) -> None:
    """Give each profile one live entity, and remove those left without one.

    ``live_entities`` maps entity IDs to the platform's live entities. An
    entity whose profile is gone (deleted, or now of the other kind) is
    removed from Home Assistant but keeps its registry entry, so it shows as
    unavailable: the same state it reaches after a restart, when the platform
    no longer creates it.
    """
    wanted = {profile["id"] for profile in profiles}
    covered: set[str] = set()

    for entity in list(live_entities.values()):
        if entity.profile_id in wanted:
            covered.add(entity.profile_id)
        else:
            await entity.async_remove()

    new_entities = [create_entity(p) for p in profiles if p["id"] not in covered]

    if async_add_entities and new_entities:
        async_add_entities(new_entities)
