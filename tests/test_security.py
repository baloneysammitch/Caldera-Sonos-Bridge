"""Regression cases for the fork's network and controller trust boundaries."""

import time
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from defusedxml import ElementTree as DET

from calderabridge import web as admin_web
from calderabridge.companion import TimelineSubscribers, create_app
from calderabridge.config import Config
from calderabridge.plexapi import PlexClient
from calderabridge.security import NetworkPolicy, SecurityError, limited_text
from calderabridge.soap import SoapClient
from calderabridge.sonos import ZoneInfo

from .test_companion import subscribers as subscribers
from .test_player import play_params
from .test_web import bridge as bridge


@pytest.fixture
async def native_client(player, subscribers):
    async with TestClient(TestServer(create_app(player, subscribers))) as client:
        yield client


def controller(token=None, identifier="verified-controller", **extra):
    result = {"X-Plex-Client-Identifier": identifier, **extra}
    if token is not None:
        result["X-Plex-Token"] = token
    return result


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.2",
        "169.254.169.254",
        "192.0.2.4",
        "localhost",
        "127.0.0.1@attacker.example",
        "attacker.example",
        "[::1]",
    ],
)
async def test_playmedia_cannot_reach_an_unapproved_host(
    native_client, fake_plex, fake_sonos, address
):
    response = await native_client.get(
        "/player/playback/playMedia",
        params=play_params(fake_plex, address=address),
        headers=controller("tok-123"),
    )
    assert response.status == 403
    assert not fake_sonos.calls
    assert not fake_plex.requests


@pytest.mark.parametrize(
    "field,value",
    [
        ("port", "80"),
        ("port", "bad"),
        ("protocol", "file"),
        ("protocol", "ftp"),
        ("machineIdentifier", "other-server"),
        ("key", "/:/prefs"),
        ("containerKey", "/library/sections"),
        ("containerKey", "/playQueues/1/../../:/prefs"),
    ],
)
async def test_invalid_server_or_media_key_is_rejected_before_effects(
    native_client, fake_plex, fake_sonos, field, value
):
    response = await native_client.get(
        "/player/playback/playMedia",
        params=play_params(fake_plex, **{field: value}),
        headers=controller("tok-123"),
    )
    assert response.status == 403
    assert not fake_sonos.calls
    assert not fake_plex.requests


async def test_fake_token_cannot_pair_or_play(native_client, fake_plex, fake_sonos, config):
    response = await native_client.get(
        "/player/playback/playMedia",
        params=play_params(fake_plex, token="not-a-credential"),
        headers=controller("not-a-credential"),
    )
    assert response.status == 403
    assert not fake_sonos.calls
    assert not (Path(config.config_dir) / "controllers.json").exists()


async def test_no_credential_or_prior_pairing_cannot_control(native_client, fake_sonos):
    response = await native_client.get("/player/playback/pause", headers=controller())
    assert response.status == 403
    assert not fake_sonos.calls


async def test_native_pairing_preserves_credentialless_pause_and_skip(
    native_client, fake_plex, fake_sonos, config
):
    response = await native_client.get(
        "/player/playback/playMedia", params=play_params(fake_plex), headers=controller("tok-123")
    )
    assert response.status == 200
    assert (await native_client.get("/player/playback/pause", headers=controller())).status == 200
    assert fake_sonos.transport_state == "PAUSED_PLAYBACK"
    assert (
        await native_client.get("/player/playback/skipNext", headers=controller())
    ).status == 200
    assert (
        await native_client.get(
            "/player/playback/pause", headers=controller(identifier="unpaired-controller")
        )
    ).status == 403
    pair_file = Path(config.config_dir) / "controllers.json"
    assert pair_file.stat().st_mode & 0o777 == 0o600
    assert "tok-123" not in pair_file.read_text()
    assert "verified-controller" not in pair_file.read_text()
    policy = NetworkPolicy(config)
    request = SimpleNamespace(
        remote="127.0.0.1", path="/player/playback/pause", headers=controller(), query={}
    )
    await policy.authorize(request, None)  # A process restart retains the pair, not a token.
    request.remote = "127.0.0.2"
    with pytest.raises(SecurityError):
        await policy.authorize(request, None)


@pytest.mark.parametrize(
    "headers",
    [
        controller("tok-123", Origin="http://evil.example"),
        controller("tok-123", Host="evil.example"),
        controller("tok-123", **{"Sec-Fetch-Site": "cross-site"}),
        controller("tok-123", **{"Sec-Fetch-Dest": "image"}),
        controller("tok-123", Origin="null"),
    ],
)
async def test_browser_and_rebinding_requests_are_denied(native_client, fake_sonos, headers):
    response = await native_client.get("/player/playback/pause", headers=headers)
    assert response.status == 403
    assert response.headers.get("Access-Control-Allow-Origin") is None
    assert not fake_sonos.calls


async def test_approved_browser_gets_only_its_exact_cors_origin(native_client):
    response = await native_client.options(
        "/player/playback/play", headers={"Origin": "https://approved.example"}
    )
    assert response.status == 200
    assert response.headers["Access-Control-Allow-Origin"] == "https://approved.example"
    assert response.headers["Vary"] == "Origin"
    response = await native_client.get("/resources")
    assert "Access-Control-Allow-Origin" not in response.headers


@pytest.mark.parametrize("command", ['0" injected="yes', "1<script/>", "a", "1" * 21, "-1"])
async def test_hostile_command_ids_are_rejected(native_client, player, command):
    response = await native_client.get("/player/timeline/poll", params={"commandID": command})
    assert response.status == 403
    assert player.command_id == "0"


async def test_timeline_serializer_is_safe_independently(player):
    root = DET.fromstring(player.timeline_xml('0" injected="yes'))
    assert root.get("injected") is None
    assert root.get("commandID") == '0" injected="yes'


async def test_duplicates_do_not_poison_command_state(native_client, player):
    response = await native_client.get("/player/timeline/poll?commandID=1&commandID=2")
    assert response.status == 403
    assert player.command_id == "0"


@pytest.mark.parametrize("path", ["/settings", "/settings/reset", "/plex/link", "/plex/unlink"])
async def test_admin_routes_require_separate_admin_auth(bridge, path):
    async with TestClient(TestServer(admin_web.create_app(bridge))) as client:
        response = await client.post(path)
        assert response.status == 403


async def test_admin_defaults_read_only_even_with_a_bearer(bridge):
    bridge.config.admin_token_file = ""
    async with TestClient(TestServer(admin_web.create_app(bridge))) as client:
        response = await client.post(
            "/plex/unlink", headers={"Authorization": "Bearer test-only-admin-token-32-characters"}
        )
        assert response.status == 403


async def test_admin_saved_banner_escapes_query_input(bridge):
    async with TestClient(TestServer(admin_web.create_app(bridge))) as client:
        response = await client.get("/", params={"saved": '<script id="canary">bad</script>'})
        body = await response.text()
        assert '<script id="canary">' not in body
        assert "&lt;script" in body


async def test_redirect_does_not_reach_another_origin(config):
    received = []

    async def canary(request):
        received.append(True)
        return web.Response(text="<MediaContainer/>")

    target = web.Application()
    target.router.add_get("/canary", canary)
    async with TestServer(target) as target_server:

        async def redirect(request):
            raise web.HTTPFound(str(target_server.make_url("/canary")))

        app = web.Application()
        app.router.add_get("/identity", redirect)
        async with TestServer(app) as server, aiohttp.ClientSession() as session:
            config.plex_server_port = server.port
            client = PlexClient(session, policy=NetworkPolicy(config))
            assert await client._get(str(server.make_url("/identity"))) is None
    assert not received


async def test_response_size_is_bounded():
    async def big(request):
        return web.Response(body=b"x" * 2048)

    app = web.Application()
    app.router.add_get("/", big)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        async with session.get(server.make_url("/")) as response:
            with pytest.raises(SecurityError):
                await limited_text(response, limit=1024)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.2:1400/ZoneGroupTopology/Control",
        "http://192.168.1.10:80/ZoneGroupTopology/Control",
        "http://user@192.168.1.10:1400/ZoneGroupTopology/Control",
        "http://192.168.1.10:1400/arbitrary",
    ],
)
async def test_sonos_calls_do_not_escape_configured_device(config, url):
    async with aiohttp.ClientSession() as session:
        soap = SoapClient(session, policy=NetworkPolicy(config))
        with pytest.raises(SecurityError):
            await soap.call(url, "unused", "Play")


@pytest.mark.parametrize(
    "mode", ["follower", "coordinator-with-followers", "stale", "address-spoof", "missing"]
)
async def test_mutation_requires_fresh_standalone_topology(config, mode):
    uid = "RINCON_AAA01400"
    zone = ZoneInfo(uid=uid, name="Kitchen", ip="192.168.1.10", coordinator_uid=uid)
    other = ZoneInfo(uid="OTHER", name="Other", ip="192.168.1.20", coordinator_uid=uid)
    if mode == "follower":
        zone.coordinator_uid = "OTHER"
    if mode == "address-spoof":
        zone.ip = "192.168.1.20"

    class Topology:
        last_refresh = 0
        last_refresh_ok = False

        async def refresh(self):
            self.last_refresh = time.monotonic()
            self.last_refresh_ok = mode != "stale"
            return False  # Unchanged topology is still a successful fresh read.

        def zone(self, selected):
            return None if mode == "missing" else zone

        def all_zones(self):
            return [zone, other] if mode == "coordinator-with-followers" else [zone]

    with pytest.raises(SecurityError):
        await NetworkPolicy(config).check_group(Topology(), uid)


async def test_unchanged_standalone_topology_is_allowed(config):
    zone = ZoneInfo(uid="RINCON_AAA01400", name="Kitchen", ip="192.168.1.10")

    class Topology:
        last_refresh = 0
        last_refresh_ok = False

        async def refresh(self):
            self.last_refresh = time.monotonic()
            self.last_refresh_ok = True
            return False

        def zone(self, uid):
            return zone

        def all_zones(self):
            return [zone]

    await NetworkPolicy(config).check_group(Topology(), zone.uid)


@pytest.mark.parametrize("value", ["*", "null", "https://example.org/path", "file://local"])
def test_browser_wildcards_and_non_origins_are_rejected(config, value):
    config.allowed_origins = [value]
    with pytest.raises(SecurityError):
        NetworkPolicy(config)


def test_missing_security_configuration_is_closed(tmp_path):
    policy = NetworkPolicy(Config(config_dir=str(tmp_path)))
    with pytest.raises(SecurityError):
        policy.require_config()


def test_settings_json_cannot_weaken_security(config):
    config.apply(
        {
            "require_standalone": False,
            "plex_server_address": "evil.example",
            "controller_networks": ["0.0.0.0/0"],
            "sonos_devices": {"FAKE": "127.0.0.1"},
            "admin_token_file": "/tmp/other",
        }
    )
    assert config.require_standalone
    assert config.plex_server_address == "127.0.0.1"
    assert config.controller_networks == ["127.0.0.1/32"]
    assert "FAKE" not in config.sonos_devices


async def test_pairing_and_callback_limits_are_bounded(player, config):
    policy = player.policy
    now = time.time()
    policy._pairs = {str(i).zfill(64): now + 60 for i in range(64)}
    request = SimpleNamespace(
        remote="127.0.0.1", path="/player/playback/pause", headers=controller("tok-123"), query={}
    )
    with pytest.raises(SecurityError):
        await policy.authorize(request, player._plex._session)
    async with aiohttp.ClientSession() as session:
        subscribers = TimelineSubscribers(session)
        for i in range(32):
            subscribers.add("127.0.0.1", 4000, "http", f"controller-{i}")
        with pytest.raises(SecurityError):
            subscribers.add("127.0.0.1", 4000, "http", "overflow")


@pytest.mark.parametrize(
    "port,protocol", [(22, "http"), (65536, "http"), (4000, "file"), (4000, "gopher")]
)
async def test_callback_cannot_target_privileged_ports_or_other_protocols(port, protocol):
    async with aiohttp.ClientSession() as session:
        with pytest.raises(SecurityError):
            TimelineSubscribers(session).add("127.0.0.1", port, protocol, "id")


@pytest.mark.parametrize("problem", ["anonymous-server", "wrong-identity"])
async def test_startup_does_not_trust_an_anonymous_or_different_server(config, fake_plex, problem):
    if problem == "anonymous-server":
        fake_plex.requires_auth = False
    else:
        fake_plex.machine_identifier = "other-server"
    async with aiohttp.ClientSession() as session:
        with pytest.raises(SecurityError):
            await NetworkPolicy(config).verify_server(session)
