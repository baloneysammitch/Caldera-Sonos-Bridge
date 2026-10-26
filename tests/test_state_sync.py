"""Observable state-sync regressions with simulated speakers and controllers."""

import asyncio
from types import SimpleNamespace

import pytest
from defusedxml import ElementTree as DET

from calderabridge.soap import UPnPError

from .test_companion import client as client
from .test_companion import subscribers as subscribers
from .test_player import play_params


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    # Patch this module's clock only; asyncio and network timeouts stay real.
    monkeypatch.setattr("calderabridge.player.time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


def music(player):
    return next(t for t in DET.fromstring(player.timeline_xml()) if t.get("type") == "music")


async def test_pause_keeps_progress_since_last_speaker_read(player, fake_plex, clock):
    await player.play_media(play_params(fake_plex))
    player._mark_position(30_000)
    clock[0] += 3
    await player.pause()
    assert music(player).get("state") == "paused"
    assert int(music(player).get("time")) == 33_000


async def test_resume_does_not_count_two_minutes_spent_paused(player, fake_plex, clock):
    await player.play_media(play_params(fake_plex))
    await player.pause()
    player._mark_position(30_000)
    clock[0] += 120
    await player.play()
    assert music(player).get("state") == "playing"
    assert int(music(player).get("time")) == 30_000
    clock[0] += 2
    assert int(music(player).get("time")) == 32_000


@pytest.mark.parametrize("failure", ["rejected", "timeout"])
@pytest.mark.parametrize("command,action,params", [
    ("pause", "Pause", {}),
    ("play", "Play", {}),
    ("stop", "Stop", {}),
    ("seekTo", "Seek", {"offset": "90000"}),
])
async def test_failed_commands_do_not_ack_success_or_invent_state(
    client, player, fake_sonos, fake_plex, clock, failure, command, action, params
):
    await player.play_media(play_params(fake_plex))
    if command == "play":
        await player.pause()
    player._mark_position(30_000)
    before = (player.state, player.position_now_ms)
    fake_sonos.errors[action] = UPnPError(701, "Unavailable") if failure == "rejected" else TimeoutError()
    response = await client.get("/player/playback/" + command, params=params)
    assert response.status == 503
    assert (player.state, player.position_now_ms) == before
    assert int(music(player).get("time")) == 30_000


@pytest.mark.parametrize("change", ["volume", "mute"])
async def test_paused_sonos_changes_wake_waiting_controller(
    player, fake_sonos, fake_plex, change
):
    await player.play_media(play_params(fake_plex))
    await player.pause()
    await player.refresh()
    waiting = asyncio.create_task(player.wait_for_change(3))
    await asyncio.sleep(0)
    if change == "volume":
        fake_sonos.volume = 42
    else:
        fake_sonos.mute = True
    await player.refresh()
    await asyncio.wait_for(waiting, timeout=0.2)
    assert music(player).get(change) == ("42" if change == "volume" else "1")
