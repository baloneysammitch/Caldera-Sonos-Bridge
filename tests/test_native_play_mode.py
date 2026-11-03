"""A Plex queue already has its playback order; native shuffle must not reorder it."""

import pytest

from calderabridge.soap import UPnPError

from .test_player import play_params


@pytest.mark.parametrize('inherited_mode', ['SHUFFLE_NOREPEAT', 'REPEAT_ONE'])
async def test_inherited_native_mode_cannot_break_plex_queue_order(
    player, fake_sonos, fake_plex, monkeypatch, inherited_mode
):
    fake_sonos.play_mode = inherited_mode
    original_add = fake_sonos._AddURIToQueue

    def native_shuffle(args):
        result = original_add(args)
        if fake_sonos.play_mode == 'SHUFFLE_NOREPEAT' and len(fake_sonos.queue) > 2:
            fake_sonos.queue.insert(1, fake_sonos.queue.pop())
        return result

    monkeypatch.setattr(fake_sonos, '_AddURIToQueue', native_shuffle)
    await player.play_media(play_params(fake_plex))
    assert fake_sonos.play_mode == 'NORMAL'
    actions = fake_sonos.actions()
    assert actions.index('SetPlayMode') < actions.index('RemoveAllTracksFromQueue')
    fake_sonos.advance()
    await player.refresh()
    assert player.current.title == 'Track 2'
    assert len(player.queue.tracks) == 3
    assert fake_sonos.transport_state == 'PLAYING'


async def test_failed_native_mode_reset_leaves_existing_queue_intact(
    player, fake_sonos, fake_plex
):
    fake_sonos.queue = [('http://example.invalid/previous.mp3', 'previous metadata')]
    fake_sonos.errors['SetPlayMode'] = UPnPError(701, 'Unavailable')
    await player.play_media(play_params(fake_plex))
    assert fake_sonos.queue == [('http://example.invalid/previous.mp3', 'previous metadata')]
    assert 'RemoveAllTracksFromQueue' not in fake_sonos.actions()
    assert 'Play' not in fake_sonos.actions()
    assert player.last_error


async def test_plex_shuffled_order_survives_native_mode_reset(
    player, fake_sonos, fake_plex, monkeypatch
):
    original = player._plex.play_queue

    async def shuffled(*args, **kwargs):
        queue = await original(*args, **kwargs)
        queue.tracks.reverse()
        queue.selected_item_id = queue.tracks[0].play_queue_item_id
        queue.shuffled = True
        return queue

    monkeypatch.setattr(player._plex, 'play_queue', shuffled)
    fake_sonos.play_mode = 'SHUFFLE_NOREPEAT'
    await player.play_media(play_params(fake_plex))
    assert fake_sonos.play_mode == 'NORMAL'
    assert player.queue.shuffled
    assert player.current.title == 'Track 3'
    fake_sonos.advance()
    await player.refresh()
    assert player.current.title == 'Track 2'
    assert len(player.queue.tracks) == 3
