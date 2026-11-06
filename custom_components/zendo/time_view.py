"""The time view, ``GET /api/zendo/time``, and the URLs ``zendo/v2/info`` gives for it.

``time_urls.py`` builds the answer and the URLs, from what this module reads in
Home Assistant. See docs/managed-devices.md, "The Time View".
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from aiohttp import web

from homeassistant.components import network
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.hassio import is_hassio

from .const import DOMAIN
from .time_urls import (
    TIME_VIEW_PATH,
    build_time_urls,
    enabled_ipv4_addresses,
    supervisor_hostname,
    time_answer_body,
)

_LOGGER = logging.getLogger(__name__)

# Set in hass.data[DOMAIN], which an unload leaves in place, once the view has
# been tried.
_TIME_VIEW_TRIED = "time_view_tried"

# The Supervisor integration keeps its host info under this key, its
# DATA_HOST_INFO, as a dict or an object with `hostname`. It's read here
# directly, so the manifest doesn't depend on the Supervisor integration.
_SUPERVISOR_HOST_INFO = "hassio_host_info"


class BNGntTimeView(HomeAssistantView):
    """Answer Home Assistant's clock as ``{"now": <epoch milliseconds>}``.

    It takes no credential and no input: a query string or a body is ignored.
    """

    url = TIME_VIEW_PATH
    name = "api:zendo:time"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        """Answer the time."""
        return web.Response(
            body=time_answer_body(time.time_ns() // 1_000_000),
            content_type="application/json",
            headers={"Cache-Control": "no-store"},
        )


@callback
def async_register_time_view(hass: HomeAssistant) -> None:
    """Register the time view, tried once per Home Assistant start.

    A view can't be unregistered, and registering it again adds its route a
    second time, so it keeps answering after an unload, until Home Assistant
    restarts. A failure is logged, and setup goes on without it.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(_TIME_VIEW_TRIED):
        return
    domain_data[_TIME_VIEW_TRIED] = True
    try:
        hass.http.register_view(BNGntTimeView())
    except Exception:  # noqa: BLE001
        _LOGGER.warning("Couldn't register the time view", exc_info=True)


@callback
def async_time_urls(hass: HomeAssistant) -> list[str]:
    """The time view's plain ``http`` URLs, on local addresses only.

    ``[]`` when Home Assistant's HTTP server uses SSL, and on any error, which
    is logged at debug level. Each value is read on its own, so one that can't
    be read leaves out only the URLs that need it.
    """
    try:
        return build_time_urls(
            _read(
                "the network adapters",
                lambda: enabled_ipv4_addresses(network.async_get_loaded_adapters(hass)),
            ),
            _read("the HTTP port", lambda: hass.http.server_port),
            _read("whether HTTP uses SSL", lambda: hass.config.api.use_ssl),
            _read("the internal URL", lambda: hass.config.internal_url),
            _read("the Supervisor host name", lambda: _supervisor_host(hass)),
        )
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Couldn't build the time URLs", exc_info=True)
        return []


def _supervisor_host(hass: HomeAssistant) -> str | None:
    if not is_hassio(hass):
        return None
    return supervisor_hostname(hass.data.get(_SUPERVISOR_HOST_INFO))


def _read(what: str, read: Callable[[], Any]) -> Any:
    try:
        return read()
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Couldn't read %s for the time URLs", what, exc_info=True)
        return None
