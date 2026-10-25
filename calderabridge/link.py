# SPDX-License-Identifier: GPL-3.0-or-later
"""Link one configured player without exposing a writable admin website."""
from __future__ import annotations

import argparse
import asyncio

import aiohttp

from .config import Config
from .plexauth import PlexAccount, PlexIdentity


async def link(name, uid=""):
    config = Config.from_env()
    if not uid:
        if len(config.sonos_devices) != 1:
            raise SystemExit("Select an allowlisted Sonos room with --uid.")
        uid = next(iter(config.sonos_devices))
    if uid not in config.sonos_devices:
        raise SystemExit("Selected room is not in SONOS_DEVICES.")
    identity = PlexIdentity(config)
    client_id = config.machine_identifier(uid)
    if identity.client_id != client_id:
        identity = PlexIdentity(config, room_uid=uid)
    if identity.linked:
        print("Player already linked; existing credential retained.")
        return
    async with aiohttp.ClientSession() as session:
        account = PlexAccount(identity, session)
        pin = await account.request_pin(identity.client_id, name)
        print(f"Enter {pin.code} at https://plex.tv/link", flush=True)
        token = await account.wait_for_pin(pin)
        await account.adopt(token)
    print("Player linked. Credential saved privately; start the bridge when idle.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True, help="Player name shown in Plex")
    parser.add_argument("--uid", default="", help="Allowlisted Sonos room UID")
    args = parser.parse_args()
    asyncio.run(link(args.name, args.uid))


if __name__ == "__main__":
    main()
