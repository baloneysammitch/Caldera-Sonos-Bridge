# Caldera Sonos Bridge — maintained security fork

Play a local Plex music library on a configured Sonos speaker from Plexamp.
This fork of [Lewis Menzies' Caldera Sonos Bridge](https://github.com/meltface-80/Caldera-Sonos-Bridge)
keeps its GPL-3.0-or-later license and stable player identities. Fork version: **0.1.12**.

This is an experimental, independently maintained fork. It is not a Plex or Sonos
product, and a passing package scan is not a guarantee of security. Existing Sonos
apps can still change grouping independently of this bridge.

## Security changes

- Plex addresses, port and server identity are configured by the administrator.
  Controller-supplied destinations and arbitrary media paths are rejected. Plex
  requests never follow redirects; plex.direct names are mapped to the explicitly
  configured literal LAN address without trusting DNS supplied by a controller.
- Playback requires a Plex credential accepted by that server. On startup the
  bridge verifies the server identity and refuses servers that accept an invalid
  token. A controller that successfully authenticates is paired to its source IP
  and client identifier for twelve hours, allowing subsequent credentialless
  Companion commands used by native clients. Those requests must carry a custom
  controller header or an approved browser Origin, preventing cross-site images
  and forms from turning a known client identifier into a playback command. Private pairing records survive a
  restart; credentials are never stored in those records.
- Discovery and timeline reads remain available within explicitly approved
  controller networks. They do not authorize playback. Browser Origins and player
  Host headers are checked, and CORS is limited to exact approved origins.
- Speakers are bound to administrator-configured UID/address pairs. Unsigned SSDP
  announcements cannot add destinations. Every mutating Sonos SOAP call checks
  fresh topology and rejects grouped rooms by default, including a coordinator
  with followers. Failure to refresh topology fails closed.
- Command IDs are bounded and validated, and timeline XML attributes are escaped.
  SOAP debug logging omits arguments, which can contain Plex credentials.
  The admin page also escapes its saved-settings query banner.
- Admin writes are disabled unless a separate private administrator bearer-token
  file is configured. The default UI is a read-only status page. Settings JSON
  cannot override network/controller trust fields.
- Self-updating and its HTTP endpoints are disabled. Do not mount a Docker socket.
  Use reviewed, pinned image replacements. Response sizes, failed authentication
  attempts, controller pairs and callback subscriptions are bounded.

These defaults intentionally change the original project's open-LAN trust model.
Use one configured room for an initial trial. Browser discovery and phone playback
should be checked on the actual network when adding devices. If the added rooms
already appear and play, no separate PIN linking is needed. Some clients require
account-based discovery instead; that optional fallback registers each player
under its stable room identifier. An unrelated server token does not create a
new device registration.

## Configuration

Create a private environment file using your own addresses and identifiers:

```dotenv
BRIDGE_NAME=Caldera Sonos Bridge
BRIDGE_IP=192.0.2.10
SETTINGS_BIND=127.0.0.1
HTTP_PORT=32700
PLAYER_PORT_BASE=32701
PLEX_SERVER_ADDRESS=192.0.2.10
PLEX_SERVER_PORT=32400
PLEX_SERVER_MACHINE_ID=your-existing-plex-machine-identifier
CONTROLLER_NETWORKS=192.0.2.0/24,127.0.0.1/32
ALLOWED_ORIGINS=https://app.plex.tv,http://192.0.2.10:32400
SONOS_DEVICES=RINCON_EXAMPLE01400=192.0.2.20
SONOS_HOSTS=192.0.2.20
INCLUDE_ZONES=Office
NAME_SUFFIX= (Sonos trial)
REQUIRE_STANDALONE=true
VOLUME_LIMIT=25
CONFIG_DIR=/config
```

The addresses above are documentation examples; replace them. `BRIDGE_NAME` sets
the visible bridge/product label; `NAME_SUFFIX` may be empty for plain room names. The Plex machine
identifier is available from the local server's `/identity` endpoint. Give the
config directory to UID/GID 10001, with mode 0700. Back up identity, settings,
controller-pair and port-assignment files together. Do not put tokens into Git or
images. Changing a controller's IP or client identifier requires a new authenticated
media request. Revoking a Plex token does not automatically revoke an existing
credentialless pairing before its twelve-hour expiry; remove `controllers.json`
and restart when idle to revoke all pairs immediately.

Build and inspect the image, then use its immutable local ID:

```bash
docker build -t caldera-sonos-bridge:reviewed .
docker image inspect caldera-sonos-bridge:reviewed --format '{{.Id}}'
```

Run on Linux with host networking for Plex GDM. Use a persistent `/config` bind
mount; drop all capabilities, enable no-new-privileges and a read-only root, and
apply memory/process limits. Only the player port and GDM should be reachable
from approved LAN clients. The status page defaults to loopback; if a separate
proxy is used, bind it only to that private interface. Terminate TLS at your
existing reverse proxy. Introduce no WAN forward or public admin route.

If a room is missing from account-based discovery, run `python -m calderabridge.link --uid RINCON_EXAMPLE01400
--name "Kitchen (Sonos)"` with the same environment/config mount. Repeat only for
missing allowlisted rooms, entering each code at https://plex.tv/link. Each room
has a separately revocable credential in `players/<stable-room-uuid>.json` (0600,
parent 0700). Existing `plex.json` identities remain supported without relinking
or changing their UUID. The CLI retains already linked credentials. Start/restart
the bridge after linking is complete and playback is idle.

List every playable room's exact UID/IP in `SONOS_DEVICES`, separated by commas,
and its IP in `SONOS_HOSTS`. `INCLUDE_ZONES` accepts comma-separated room names.
Retain port assignments in `ports.json` and allow only those player ports through
the LAN firewall. Bonded pairs and home-theater satellites stay in their native
Sonos room and appear as one player; invisible satellites are not added as
independent endpoints. The standalone guard permits those native bonds while
rejecting groups containing other playable rooms. Set `UNGROUP_ON_PLAY=false`
to preserve native grouping. This does not bypass the fresh topology guard.

Optional administrator API access requires `ADMIN_TOKEN_FILE` pointing to a
process-owned 0600 file containing at least 32 random characters. Supply it in an
`Authorization: Bearer ...` header. Neither browser forms nor a Plex playback
token bypass that requirement. Do not enable this on an exposed route.

The read-only `/healthz` endpoint checks every configured room's transport in
parallel, with a three-second timeout per room. It returns HTTP 503 if a room is
missing or unreachable and HTTP 200 only when all configured rooms respond.
It sends no playback/grouping commands and exposes no Plex credentials. Use it
for an uptime monitor over the same private status interface/proxy.

## Tests and maintenance

```bash
python -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.lock
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/ruff check calderabridge tests
```

Runtime dependencies are pinned with artifact hashes in `requirements.lock`.
CI builds without publishing; image publication is an explicit manual workflow.
No upstream issue/PR is automatically filed by the application or tests.

Restart recovery preserves the room identity, ports and paired-controller records.
The bridge can resume an existing native Sonos queue if its current URI belongs
to the configured Plex server. Full Plex queue context is not persisted: select
fresh media to rebuild/extend that context after a process restart.

Playback-state reconciliation preserves elapsed progress when pausing and resets
the interpolation clock on resume, so time spent paused is not counted as music.
Rejected/timed-out pause, resume, stop and seek commands return HTTP 503 and keep
the last observed state; paused volume/mute changes wake waiting timeline polls.
These paths have synthetic speaker/controller regression coverage. They do not
establish that every Plexamp client or external-source/restart scenario stays in
sync; full Plex queue context remains process-local.
