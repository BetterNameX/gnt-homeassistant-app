"""DNS-based service discovery for the control backend.

Port of the app's service-discovery/index.ts - resolves the control
backend API URL from a DNS TXT record at
control-backend.v1.sd.gnt.betterna.me.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .dns_doh import query_dns_txt

SD_HOSTNAME = "control-backend.v1.sd"
SD_CACHE_KEY = "sys-service-discovery"


async def get_control_backend_url(hass: HomeAssistant) -> str:
    """Return the control backend API URL via DNS service discovery."""
    session = async_get_clientsession(hass)
    data = await query_dns_txt(session, SD_HOSTNAME, SD_CACHE_KEY)
    return data["cb"]
