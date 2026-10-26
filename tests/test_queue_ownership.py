"""A native queue replacement must not inherit the old Plex track identities."""

import asyncio

import pytest
from defusedxml import ElementTree as ET

from .test_player import play_params


def music(player):
    return next(t for t in ET.fromstring(player.timeline_xml()) if t.get('type') == 'music')


@pytest.mark.parametrize('uri', [
    'same-server',
    'x-sonos-spotify:unrelated-song',
])
async def test_native_replacement_releases_stale_plex_queue(
    player, fake_sonos, fake_plex, uri
):
    fake_plex.tracks = 30
    await player.play_media(play_params(fake_plex))
    if uri == 'same-server':
        uri = f'http://127.0.0.1:{fake_plex.port}/library/parts/999/1/file.flac'
    fake_sonos.queue = [(uri, '')]
    fake_sonos.track_index = 1
    fake_sonos.calls.clear()
    reports_before = len(fake_plex.timelines)
    await player.refresh()

    assert player.state == 'playing'  # The native speaker keeps playing.
    assert player.current is None
    assert player.queue.tracks == []
    assert player._loaded == []
    timeline = music(player)
    assert timeline.get('state') == 'stopped'  # No managed Plex item remains.
    for key in ['key', 'ratingKey', 'playQueueID', 'containerKey', 'machineIdentifier']:
        assert timeline.get(key) is None
    assert not any(not action.startswith('Get') for action, _ in fake_sonos.calls)
    assert len(fake_plex.timelines) == reports_before


async def test_normal_native_advance_keeps_matching_plex_identity(player, fake_sonos, fake_plex):
    await player.play_media(play_params(fake_plex))
    fake_sonos.advance()
    await player.refresh()
    assert player.current.title == 'Track 2'
    assert music(player).get('ratingKey') == '102'


async def test_empty_uri_during_transition_does_not_discard_queue(
    player, fake_sonos, fake_plex, monkeypatch
):
    await player.play_media(play_params(fake_plex))
    monkeypatch.setattr(fake_sonos, 'current_uri', lambda: '')
    fake_sonos.transport_state = 'TRANSITIONING'
    await player.refresh()
    assert player.current.title == 'Track 1'
    assert len(player.queue.tracks) == 3


async def test_top_up_tracks_keep_their_uri_mapping(player, fake_sonos, fake_plex):
    fake_plex.tracks = 30
    await player.play_media(play_params(fake_plex))
    fake_sonos.track_index = 10
    await player.refresh()
    assert len(fake_sonos.queue) > 12
    fake_sonos.track_index = 13
    await player.refresh()
    assert player.current.title == 'Track 13'


async def test_fresh_plex_selection_recovers_after_native_replacement(
    player, fake_sonos, fake_plex
):
    await player.play_media(play_params(fake_plex))
    fake_sonos.queue = [('x-sonos-spotify:other', '')]
    await player.refresh()
    assert player.current is None
    await player.play_media(play_params(fake_plex))
    await player.refresh()
    assert player.current.title == 'Track 1'
    assert music(player).get('state') == 'playing'


async def test_poll_during_our_own_reload_keeps_new_queue_identity(
    player, fake_sonos, fake_plex, monkeypatch
):
    await player.play_media(play_params(fake_plex))
    entered, release = asyncio.Event(), asyncio.Event()
    original = fake_sonos.call

    async def delayed(url, service, action, args):
        result = await original(url, service, action, args)
        if action == 'AddURIToQueue':
            entered.set()
            await release.wait()
        return result

    monkeypatch.setattr(fake_sonos, 'call', delayed)
    loading = asyncio.create_task(player.play_media(play_params(fake_plex, playQueueItemID='903')))
    await asyncio.wait_for(entered.wait(), 1)
    polling = asyncio.create_task(player.refresh())
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(loading, polling)
    assert player.current.title == 'Track 3'
    assert music(player).get('ratingKey') == '103'
