# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit network trust for Companion, Plex and Sonos; no discovery-based trust."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import unquote, urlsplit

import aiohttp

MAX_XML_BYTES = 2 * 1024 * 1024
PAIR_TTL = 12 * 3600
MAX_CONTROLLERS = 64
# Only endpoints used by this bridge. In particular, media keys are not arbitrary
# paths to an authenticated Plex administrative API.
PLEX_PATH = re.compile(
    r"/(?:identity|servers|library/sections|playQueues/[0-9]+|"
    r"library/metadata/[0-9]+(?:/(?:children|allLeaves))?|"
    r"library/parts/[0-9]+/[0-9]+/[A-Za-z0-9_.-]+|"
    r"audio/:/transcode/universal/start|photo/:/transcode|:/timeline)\Z"
)


class SecurityError(ValueError):
    """A request is outside the configured trust boundary."""


async def limited_text(response, limit=MAX_XML_BYTES):
    data = await response.content.read(limit + 1)
    if len(data) > limit:
        raise SecurityError("Response exceeds the configured size limit")
    # read(n) may return less than n even when more data is pending.
    while not response.content.at_eof():
        part = await response.content.read(min(65536, limit + 1 - len(data)))
        data += part
        if len(data) > limit:
            raise SecurityError("Response exceeds the configured size limit")
        if not part:
            break
    return data.decode(response.charset or "utf-8", errors="replace")


class NetworkPolicy:
    def __init__(self, config):
        self.config = config
        self.address = (
            str(ipaddress.ip_address(config.plex_server_address))
            if config.plex_server_address
            else ""
        )
        self.networks = [ipaddress.ip_network(n, strict=True) for n in config.controller_networks]
        self.devices = {
            uid: str(ipaddress.ip_address(ip)) for uid, ip in config.sonos_devices.items()
        }
        self.origins = set(config.allowed_origins)
        if "*" in self.origins or "null" in self.origins:
            raise SecurityError("Browser origins must be explicit HTTP(S) origins")
        for origin in self.origins:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in ("http", "https")
                or not parsed.hostname
                or parsed.username
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise SecurityError("Browser origins must be explicit HTTP(S) origins")
        self._pairs = {}
        self._verified = {}
        self._failures = {}
        self._pair_path = Path(config.config_dir) / "controllers.json"
        self._load_pairs()

    def require_config(self):
        if self.address and ipaddress.ip_address(self.address).version != 4:
            raise SecurityError("Configure an IPv4 Plex endpoint for Sonos audio")
        if (
            not self.address
            or not self.config.plex_machine_identifier
            or not self.networks
            or not self.devices
        ):
            raise SecurityError(
                "Configure PLEX_SERVER_ADDRESS, PLEX_SERVER_MACHINE_ID, CONTROLLER_NETWORKS and SONOS_DEVICES before starting"
            )

    def allowed_peer(self, peer):
        try:
            address = ipaddress.ip_address(peer)
        except ValueError:
            return False
        return any(address in network for network in self.networks)

    def validate_browser(self, request, bridge_ip):
        host = urlsplit("http://" + request.host)
        if (
            host.username
            or host.password
            or host.path
            or host.query
            or host.fragment
            or host.hostname not in {bridge_ip, "127.0.0.1", "::1", "localhost"}
        ):
            raise SecurityError("Unexpected player Host")
        origin = request.headers.get("Origin", "")
        if origin and origin not in self.origins:
            raise SecurityError("Browser origin is not approved")
        # Images/forms from another site may omit Origin. They must not become
        # authenticated controls merely because the browser shares a paired IP.
        if not origin and request.headers.get("Sec-Fetch-Site") in {"cross-site", "same-site"}:
            raise SecurityError("Cross-site requests require an approved Origin")
        if not origin and request.headers.get("Sec-Fetch-Dest", "empty") not in {
            "empty",
            "document",
        }:
            raise SecurityError("Embedded browser controls are not permitted")
        return origin

    def canonical_server(self, server):
        from .plexapi import lan_address

        if server.protocol not in {"http", "https"} or server.port != self.config.plex_server_port:
            raise SecurityError("Unexpected Plex protocol or port")
        candidate = server.address
        try:
            candidate = str(ipaddress.ip_address(candidate))
        except ValueError:
            candidate = lan_address(candidate)
        if not self.address or candidate != self.address:
            raise SecurityError("Plex destination is not the configured server")
        if (
            server.machine_identifier
            and server.machine_identifier != self.config.plex_machine_identifier
        ):
            raise SecurityError("Unexpected Plex server identity")
        # Preserve the existing direct-LAN audio route, using a literal approved
        # address. No controller-supplied DNS name is resolved or trusted.
        return replace(
            server,
            address=self.address,
            port=self.config.plex_server_port,
            protocol="http",
            machine_identifier=self.config.plex_machine_identifier,
        )

    def validate_plex_url(self, url):
        parsed = urlsplit(url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != self.address
            or parsed.port != self.config.plex_server_port
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise SecurityError("Plex request escaped the configured server")
        path = parsed.path
        for _ in range(3):
            path = unquote(path)
        if (
            not PLEX_PATH.fullmatch(path)
            or any(part in {".", ".."} for part in path.split("/"))
            or "\\" in path
        ):
            raise SecurityError("Unexpected Plex media path")

    def validate_media_key(self, value, queue=False):
        path = urlsplit(value).path
        expression = r"/playQueues/[0-9]+" if queue else r"(?:/library/metadata/)?[0-9]+"
        if not re.fullmatch(expression, path) or "#" in value or "\\" in value:
            raise SecurityError("Unexpected Plex media key")

    def validate_sonos_url(self, url):
        parsed = urlsplit(url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in self.devices.values()
            or parsed.port != 1400
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise SecurityError("Sonos destination is not configured")
        if parsed.path not in {
            "/xml/device_description.xml",
            "/ZoneGroupTopology/Control",
            "/MediaRenderer/AVTransport/Control",
            "/MediaRenderer/RenderingControl/Control",
            "/MediaRenderer/ConnectionManager/Control",
            "/DeviceProperties/Control",
            "/MediaServer/ContentDirectory/Control",
        }:
            raise SecurityError("Unexpected Sonos endpoint")

    def zone_allowed(self, zone):
        return self.devices.get(zone.uid) == zone.ip

    async def check_group(self, topology, uid):
        if not self.config.require_standalone:
            return
        if topology is None:
            raise SecurityError("Fresh Sonos topology is unavailable")
        await topology.refresh()
        if not topology.last_refresh_ok or time.monotonic() - topology.last_refresh > 2:
            raise SecurityError("Fresh Sonos topology is unavailable")
        zone = topology.zone(uid)
        if zone is None or not self.zone_allowed(zone) or not zone.is_coordinator:
            raise SecurityError("Selected speaker is not standalone")
        for member in topology.all_zones():
            if member.uid != uid and member.playable and member.coordinator_uid == uid:
                raise SecurityError("Selected speaker is grouped with another room")

    async def verify_server(self, session):
        from defusedxml import ElementTree as DET

        base = f"http://{self.address}:{self.config.plex_server_port}"
        async with session.get(
            base + "/identity", allow_redirects=False, timeout=aiohttp.ClientTimeout(total=5)
        ) as response:
            if response.status != 200:
                raise SecurityError("Configured Plex identity endpoint is unavailable")
            root = DET.fromstring(await limited_text(response))
            if root.get("machineIdentifier") != self.config.plex_machine_identifier:
                raise SecurityError("Configured Plex identity does not match")
        # Plex's 'allowed without authentication' setting would undermine token
        # validation. Refuse it instead of trusting any non-empty token.
        async with session.get(
            base + "/library/sections",
            headers={"X-Plex-Token": "caldera-deliberately-invalid-token"},
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=5),
        ) as response:
            if response.status != 401:
                raise SecurityError(
                    "Plex must reject invalid tokens before controller authorization can be enabled"
                )

    def _load_pairs(self):
        try:
            data = json.loads(self._pair_path.read_text())
            self._pairs = {
                k: v
                for k, v in data.items()
                if re.fullmatch(r"[0-9a-f]{64}", k)
                and isinstance(v, (int, float))
                and time.time() < v <= time.time() + PAIR_TTL
            }
        except (OSError, ValueError, AttributeError, TypeError):
            self._pairs = {}
        if len(self._pairs) > MAX_CONTROLLERS:
            self._pairs = {}

    def _key(self, peer, identifier):
        return hashlib.sha256((peer + "\0" + identifier).encode()).hexdigest()

    async def authorize(self, request, session):
        peer = request.remote or ""
        if not self.allowed_peer(peer):
            raise SecurityError("Controller network is not approved")
        identifier = (
            request.headers.get("X-Plex-Client-Identifier")
            or request.query.get("X-Plex-Client-Identifier")
            or request.query.get("clientIdentifier", "")
        )
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", identifier):
            raise SecurityError("A valid controller identifier is required")
        key = self._key(peer, identifier)
        token = (
            request.query.get("token")
            or request.query.get("X-Plex-Token")
            or request.headers.get("X-Plex-Token", "")
        )
        now = time.time()
        if not token:
            # An image/form/navigation request can carry query parameters but
            # cannot set this custom header. Do not let a public/nonsecret client
            # identifier become a browser CSRF credential merely through IP pairing.
            if not request.headers.get("Origin") and not request.headers.get(
                "X-Plex-Client-Identifier"
            ):
                raise SecurityError("Credentialless native controls require a controller header")
            if request.path.endswith("/playMedia") or self._pairs.get(key, 0) < now:
                raise SecurityError("Controller must first authenticate with Plex")
            return
        if len(token) > 512 or any(ord(char) < 33 for char in token):
            raise SecurityError("Invalid Plex credential")
        digest = hashlib.sha256(token.encode()).hexdigest()
        cache_key = (key, digest)
        if self._verified.get(cache_key, 0) < now:
            self._failures = {p: v for p, v in self._failures.items() if v[0] > now - 60}
            previous, count = self._failures.get(peer, (now, 0))
            if count >= 8 or (
                peer not in self._failures and len(self._failures) >= MAX_CONTROLLERS
            ):
                raise SecurityError("Too many failed authorization attempts")
            base = f"http://{self.address}:{self.config.plex_server_port}/library/sections"
            async with session.get(
                base,
                headers={"X-Plex-Token": token},
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as response:
                if response.status != 200:
                    self._failures[peer] = (previous, count + 1)
                    raise SecurityError("Plex rejected the controller credential")
                await limited_text(response)
            self._verified = {k: v for k, v in self._verified.items() if v > now}
            if len(self._verified) >= MAX_CONTROLLERS:
                self._verified.pop(next(iter(self._verified)))
            self._verified[cache_key] = now + 120
        self._pairs = {k: v for k, v in self._pairs.items() if v > now}
        if key not in self._pairs and len(self._pairs) >= MAX_CONTROLLERS:
            raise SecurityError("Controller pairing limit reached")
        self._pairs[key] = now + PAIR_TTL
        self._pair_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._pair_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self._pairs))
        temporary.chmod(0o600)
        temporary.replace(self._pair_path)
