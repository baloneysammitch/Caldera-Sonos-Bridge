# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Lewis Menzies (Music Duck / MusicD)
"""The Plex Companion surface: what a Plex controller talks to.

A Plex player is a smaller thing than it sounds - a device that announces itself
and answers a handful of HTTP requests.  This module is that handful, one server
per room, so that each Sonos room is a separate player with its own identity and
its own address.  One port cannot be two players: a controller identifies a
player by the endpoint it answers on.

Controllers learn about state in one of two ways, and both are served here.
They either poll ``/player/timeline/poll``, optionally hanging on the request
until something changes, or they subscribe and have timelines pushed to them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from xml.sax.saxutils import quoteattr

import aiohttp
from aiohttp import web

from .config import BRIDGE_VERSION
from .player import RoomPlayer
from .plexauth import CAPABILITIES, DEVICE_CLASS, PLATFORM, PRODUCT
from .security import SecurityError
from .soap import UPnPError

LOGGER = logging.getLogger(__name__)

PLAYER_KEY: web.AppKey = web.AppKey("player")
SUBS_KEY: web.AppKey = web.AppKey("subscribers")

XML_TYPE = 'text/xml; charset="utf-8"'

#: A long poll that never returned would hold a controller's connection open
#: forever; Plex clients expect an answer within about half a minute either way.
MAX_POLL_WAIT = 30.0

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "x-plex-token, x-plex-client-identifier, "
    "x-plex-device-name, x-plex-target-client-identifier, accept, content-type",
    "Access-Control-Expose-Headers": "X-Plex-Client-Identifier",
}


def _headers(player: RoomPlayer) -> dict[str, str]:
    return {
        "X-Plex-Client-Identifier": player.machine_identifier,
        "X-Plex-Protocol": "1.0",
    }


def _xml(player: RoomPlayer, body: str, status: int = 200) -> web.Response:
    return web.Response(
        body=body.encode("utf-8"),
        status=status,
        headers={"Content-Type": XML_TYPE, **_headers(player)},
    )


def _ok(player: RoomPlayer) -> web.Response:
    return _xml(
        player, '<?xml version="1.0" encoding="utf-8"?>\n<Response code="200" status="OK" />'
    )


def resources_xml(player: RoomPlayer) -> str:
    """``/resources`` - who this player is, asked before it is offered."""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<MediaContainer size="1">'
        f"<Player title={quoteattr(player.name)}"
        f" machineIdentifier={quoteattr(player.machine_identifier)}"
        f" product={quoteattr(PRODUCT)} platform={quoteattr(PLATFORM)}"
        f" platformVersion={quoteattr(BRIDGE_VERSION)} version={quoteattr(BRIDGE_VERSION)}"
        ' protocol="plex" protocolVersion="1"'
        f" protocolCapabilities={quoteattr(CAPABILITIES)}"
        f" deviceClass={quoteattr(DEVICE_CLASS)}/>"
        "</MediaContainer>"
    )


class TimelineSubscribers:
    """Controllers that asked to be told, rather than to ask.

    A subscriber is a host and port to POST timelines at.  They are dropped when
    they unsubscribe, and also when they stop answering - a phone that walked out
    of the house would otherwise be retried forever.
    """

    #: Consecutive delivery failures before a subscriber is presumed gone.
    MAX_FAILURES = 3

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._subs: dict[str, dict[str, object]] = {}

    def add(self, host: str, port: int, protocol: str, client_id: str) -> None:
        if (
            not client_id
            or len(client_id) > 128
            or not 1024 <= port <= 65535
            or protocol not in ("http", "https")
        ):
            raise SecurityError("Invalid timeline callback")
        now = time.monotonic()
        self._subs = {k: v for k, v in self._subs.items() if v["expires"] > now}
        key = f"{host}|{client_id}"
        if key not in self._subs and len(self._subs) >= 32:
            raise SecurityError("Timeline callback limit reached")
        self._subs[key] = {
            "expires": now + 300,
            "url": f"{protocol or 'http'}://{host}:{port}/:/timeline",
            "failures": 0,
        }
        LOGGER.debug("Timeline subscriber %s", self._subs[key]["url"])

    def remove(self, client_id: str, host: str = "") -> None:
        self._subs.pop(f"{host}|{client_id}", None)

    def __len__(self) -> int:
        return len(self._subs)

    async def publish(self, body: str, command_id: str) -> None:
        for key, sub in list(self._subs.items()):
            if sub["expires"] < time.monotonic():
                self._subs.pop(key, None)
                continue
            url = f"{sub['url']}?commandID={command_id}"
            try:
                async with self._session.post(
                    url,
                    data=body.encode("utf-8"),
                    headers={"Content-Type": XML_TYPE},
                    timeout=aiohttp.ClientTimeout(total=5.0),
                    allow_redirects=False,
                ) as response:
                    if response.status < 400:
                        sub["failures"] = 0
                        continue
                    sub["failures"] = int(sub["failures"]) + 1
            except (TimeoutError, aiohttp.ClientError, OSError):
                sub["failures"] = int(sub["failures"]) + 1
            if int(sub["failures"]) >= self.MAX_FAILURES:
                LOGGER.debug("Dropping unreachable timeline subscriber %s", sub["url"])
                self._subs.pop(key, None)


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------
def create_app(player: RoomPlayer, subscribers: TimelineSubscribers) -> web.Application:
    denials = {}

    @web.middleware
    async def boundary(request, handler):
        policy = player.policy
        try:
            if not policy.allowed_peer(request.remote or ""):
                raise SecurityError("Controller network is not approved")
            origin = policy.validate_browser(request, player.config.bridge_ip)
            target = request.headers.get("X-Plex-Target-Client-Identifier", "")
            if target and target != player.machine_identifier:
                raise SecurityError("Wrong player identifier")
            command_id = request.query.get("commandID", "")
            if command_id and not re.fullmatch(r"[0-9]{1,20}", command_id):
                raise SecurityError("Invalid command identifier")
            if len(request.query.getall("commandID", [])) > 1:
                raise SecurityError("Duplicate command identifier")
            if request.method != "OPTIONS" and request.path.startswith("/player/playback/"):
                # A rejected request must not change room state or replace its
                # media server/queue. Authorization precedes dispatch.
                if request.path.endswith("/playMedia"):
                    from .plexapi import PlexServer

                    policy.canonical_server(
                        PlexServer(
                            address=request.query.get("address", ""),
                            port=int(request.query.get("port", "32400")),
                            protocol=request.query.get("protocol", "http"),
                            machine_identifier=request.query.get("machineIdentifier", ""),
                        )
                    )
                    if request.query.get("containerKey"):
                        policy.validate_media_key(request.query["containerKey"], queue=True)
                    if request.query.get("key"):
                        policy.validate_media_key(request.query["key"])
                await policy.authorize(request, player._plex._session)
            response = await handler(request)
        except (SecurityError, ValueError) as error:
            reason = str(error) if isinstance(error, SecurityError) else "Malformed request"
            key = (request.remote, request.path, reason)
            now = time.monotonic()
            if denials.get(key, 0) < now - 60:
                if len(denials) >= 64:
                    denials.pop(next(iter(denials)))
                denials[key] = now
                LOGGER.warning(
                    "Companion rejected: peer=%s path=%s reason=%s",
                    request.remote,
                    request.path,
                    reason,
                )
            return web.Response(status=403, text="Request is outside the configured trust policy")
        except (TimeoutError, aiohttp.ClientError, OSError):
            return web.Response(status=503, text="Controller authorization is unavailable")
        if origin:
            response.headers.update(CORS_HEADERS)
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
        return response

    app = web.Application(middlewares=[boundary], client_max_size=16384)
    app[PLAYER_KEY] = player
    app[SUBS_KEY] = subscribers

    app.router.add_get("/resources", handle_resources)
    app.router.add_get("/player/timeline/poll", handle_poll)
    app.router.add_get("/player/timeline/subscribe", handle_subscribe)
    app.router.add_get("/player/timeline/unsubscribe", handle_unsubscribe)
    app.router.add_get("/player/playback/{command}", handle_playback)
    app.router.add_get("/player/mirror/details", handle_noop)
    app.router.add_get("/player/application/{command}", handle_noop)
    app.router.add_get("/player/navigation/{command}", handle_noop)
    app.router.add_route("OPTIONS", "/{tail:.*}", handle_options)
    return app


def _player(request: web.Request) -> RoomPlayer:
    return request.app[PLAYER_KEY]


def _note_command_id(request: web.Request, player: RoomPlayer) -> str:
    command_id = request.query.get("commandID", "")
    if command_id:
        player.command_id = command_id
    return player.command_id


async def handle_options(request: web.Request) -> web.Response:
    return web.Response(status=200, headers=_headers(_player(request)))


async def handle_resources(request: web.Request) -> web.Response:
    return _xml(_player(request), resources_xml(_player(request)))


async def handle_noop(request: web.Request) -> web.Response:
    """Commands a speaker has no screen to act on, acknowledged so the
    controller does not treat the player as broken."""
    player = _player(request)
    _note_command_id(request, player)
    return _ok(player)


# -- timelines ---------------------------------------------------------
async def handle_poll(request: web.Request) -> web.Response:
    """``wait=1`` asks the player to hold the request open until something
    changes, which is how a controller gets prompt updates without hammering."""
    player = _player(request)
    command_id = _note_command_id(request, player)
    if request.query.get("wait") in ("1", "true"):
        await player.wait_for_change(MAX_POLL_WAIT)
    return _xml(player, player.timeline_xml(command_id))


async def handle_subscribe(request: web.Request) -> web.Response:
    player = _player(request)
    command_id = _note_command_id(request, player)
    try:
        port = int(request.query.get("port", "0"))
    except ValueError:
        port = 0
    if not port:
        return _xml(
            player, '<?xml version="1.0"?>\n<Response code="400" status="Bad Request" />', 400
        )

    host = request.remote or ""
    client_id = request.headers.get("X-Plex-Client-Identifier", "")
    request.app[SUBS_KEY].add(host, port, request.query.get("protocol", "http"), client_id)
    LOGGER.info("%s: a controller at %s subscribed to timelines", player.zone.name, host)
    # The subscriber expects a timeline promptly, not at the next state change.
    asyncio.create_task(request.app[SUBS_KEY].publish(player.timeline_xml(command_id), command_id))
    return _ok(player)


async def handle_unsubscribe(request: web.Request) -> web.Response:
    player = _player(request)
    _note_command_id(request, player)
    request.app[SUBS_KEY].remove(
        request.headers.get("X-Plex-Client-Identifier", ""), request.remote or ""
    )
    return _ok(player)


# -- playback ----------------------------------------------------------
async def handle_playback(request: web.Request) -> web.Response:
    player = _player(request)
    command = request.match_info["command"]
    _note_command_id(request, player)
    params = dict(request.query)

    LOGGER.debug("%s: %s %s", player.zone.name, command, _loggable(params))
    try:
        await _dispatch(player, command, params, dict(request.headers))
    except SecurityError:
        return _xml(player, '<?xml version="1.0"?><Response code="403" status="Forbidden"/>', 403)
    except (UPnPError, TimeoutError, aiohttp.ClientError, OSError) as error:
        LOGGER.warning("%s: %s unavailable (%s)", player.zone.name, command, type(error).__name__)
        return _xml(
            player,
            '<?xml version="1.0"?><Response code="503" status="Service Unavailable"/>',
            503,
        )
    except Exception:
        LOGGER.exception("%s: %s failed", player.zone.name, command)
        return _xml(
            player,
            '<?xml version="1.0"?>\n<Response code="500" status="Internal Server Error" />',
            500,
        )
    return _ok(player)


async def _dispatch(
    player: RoomPlayer,
    command: str,
    params: dict[str, str],
    headers: dict[str, str] | None = None,
) -> None:
    if command == "playMedia":
        await player.play_media(params, headers)
    elif command == "play":
        await player.play()
    elif command == "pause":
        await player.pause()
    elif command == "playPause":
        await player.play_pause()
    elif command == "stop":
        await player.stop()
    elif command == "skipNext":
        await player.skip_next()
    elif command == "skipPrevious":
        await player.skip_previous()
    elif command == "skipTo":
        await player.skip_to(params)
    elif command == "seekTo":
        with contextlib.suppress(ValueError):
            await player.seek_to(int(params.get("offset", "0")))
    elif command == "stepForward":
        await player.step(30)
    elif command == "stepBack":
        await player.step(-15)
    elif command == "setParameters":
        await player.set_parameters(params)
    elif command == "refreshPlayQueue":
        await player.refresh_queue(params)
    elif command in ("setStreams", "setVolume"):
        await player.set_parameters(params)
    else:
        LOGGER.debug("%s: ignoring unknown command %r", player.zone.name, command)


def _loggable(params: dict[str, str]) -> dict[str, str]:
    """Parameters minus the access token, which should not reach a log file."""
    return {k: v for k, v in params.items() if k.lower() not in ("token", "x-plex-token")}
