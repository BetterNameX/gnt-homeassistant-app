"""DNS-based service discovery for the control backend.

Port of the app's service-discovery/index.ts - resolves the control
backend API URL from a DNS TXT record at
control-backend.v1.sd.gnt.betterna.me.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .dns_doh import BNGntDnsReadError, query_dns_txt

SD_HOSTNAME = "control-backend.v1.sd"
SD_CACHE_KEY = "sys-service-discovery"


class BNGntControlBackendUrlError(RuntimeError):
    """The control backend's URL can't be resolved."""


async def get_control_backend_url(hass: HomeAssistant) -> str:
    """Return the control backend API URL via DNS service discovery.

    Raises ``BNGntControlBackendUrlError`` when it can't be resolved.
    """
    session = async_get_clientsession(hass)
    try:
        data = await query_dns_txt(session, SD_HOSTNAME, SD_CACHE_KEY)
    except BNGntDnsReadError as err:
        raise BNGntControlBackendUrlError("Can't resolve the control backend's URL") from err
    url = data.get("cb")
    if not isinstance(url, str) or not url:
        raise BNGntControlBackendUrlError("The service discovery record has no control backend URL")
    return url
