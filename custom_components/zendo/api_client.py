"""GraphQL client for the control backend API.

Lightweight wrapper around aiohttp that handles authentication, service
discovery, and the two GraphQL operations we need: fetching profiles and
sending push notifications.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import ClientTimeout

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import API_VERSION
from .service_discovery import get_control_backend_url

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = ClientTimeout(total=60)

QUERY_PROFILES = """
query HASitePushNotificationControlBackendDetails {
    res: sitePushNotificationControlBackendDetails {
        profiles {
            id
            label
        }
    }
}
"""

MUTATION_SEND_NOTIFICATION = """
mutation HASitePushNotificationSendToProfile($notifications: [SitePushNotificationProfileInput!]!) {
    res: sitePushNotificationSendToProfile(notifications: $notifications)
}
"""


def _build_headers(token: str) -> dict[str, str]:
    """Build request headers matching the app's user-agent convention."""
    return {
        "Authorization": f"Bearer {token}",
        "Accept-Encoding": "gzip",
        "user-agent": json.dumps({
            "client": "me.betterna.gnt.homeassistant",
            "clientAppVersion": API_VERSION,
            "platform": "homeassistant",
        }),
    }


async def _graphql_request(
    hass: HomeAssistant,
    token: str,
    query: str,
    variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a GraphQL request against the control backend."""
    session = async_get_clientsession(hass)
    url = await get_control_backend_url(hass)
    headers = _build_headers(token)

    payload: dict[str, Any] = {"query": query}
    if variables:
        payload["variables"] = variables

    async with session.post(
        url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT
    ) as resp:
        resp.raise_for_status()
        result = await resp.json()

    if result.get("errors"):
        error_messages = [e.get("message", "Unknown error") for e in result["errors"]]
        raise RuntimeError(f"GraphQL errors: {', '.join(error_messages)}")

    return result.get("data", {})


async def fetch_profiles(
    hass: HomeAssistant,
    token: str,
) -> list[dict[str, str]]:
    """Fetch the list of push notification profiles from the control backend."""
    data = await _graphql_request(hass, token, QUERY_PROFILES)
    profiles = data.get("res", {}).get("profiles", [])

    return [{"id": p["id"], "label": p["label"]} for p in profiles]


async def send_notification(
    hass: HomeAssistant,
    token: str,
    notifications: list[dict[str, Any]],
) -> None:
    """Send push notifications to the specified profiles."""
    await _graphql_request(
        hass,
        token,
        MUTATION_SEND_NOTIFICATION,
        variables={"notifications": notifications},
    )
