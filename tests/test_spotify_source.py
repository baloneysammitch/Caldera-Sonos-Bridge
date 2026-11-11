"""Spotify Connect uses a virtual transport and Track=0, independently of Q:0."""
import pytest

from calderabridge.security import SecurityError
from calderabridge.soap import UPnPError

from .test_player import play_params
from .test_queue_ownership import music

SPOTIFY = 'x-sonos-vli:RINCON_AAA01400:2,spotify:example'


def cloud_source(fake, monkeypatch):
    original = fake._GetPositionInfo
    fake.av_transport_uri = SPOTIFY
    fake.transport_state = 'PLAYING'

    def position(args):
        data = original(args)
        if fake.av_transport_uri == SPOTIFY:
            data.update(Track='0', TrackURI=SPOTIFY)
        return data
    monkeypatch.setattr(fake, '_GetPositionInfo', position)
    monkeypatch.setattr(fake, '_GetMediaInfo', lambda args: {'CurrentURI': fake.av_transport_uri}, raising=False)


async def test_track_zero_external_source_releases_false_plex_timeline(player, fake_sonos, fake_plex, monkeypatch):
    await player.play_media(play_params(fake_plex))
    cloud_source(fake_sonos, monkeypatch)
    fake_sonos.calls.clear()
    await player.refresh()
    assert player.current is None
    assert not player._loaded and not player.queue.tracks
    assert music(player).get('state') == 'stopped'
    assert not any(not a.startswith('Get') for a in fake_sonos.actions())


@pytest.mark.parametrize('command', ['play', 'pause', 'stop', 'skip_next', 'skip_previous', 'seek_to'])
async def test_stale_plex_controls_never_operate_spotify(player, fake_sonos, fake_plex, monkeypatch, command):
    await player.play_media(play_params(fake_plex))
    cloud_source(fake_sonos, monkeypatch)
    fake_sonos.calls.clear()
    with pytest.raises(SecurityError):
        await getattr(player, command)(*[1000] if command == 'seek_to' else [])
    assert not any(not a.startswith('Get') for a in fake_sonos.actions())


async def test_late_client_stop_after_ownership_loss_leaves_spotify_playing(player, fake_sonos, fake_plex, monkeypatch):
    await player.play_media(play_params(fake_plex))
    cloud_source(fake_sonos, monkeypatch)
    await player.refresh()
    fake_sonos.calls.clear()
    with pytest.raises(SecurityError):
        await player.stop()
    assert fake_sonos.transport_state == 'PLAYING'
    assert not any(not a.startswith('Get') for a in fake_sonos.actions())


async def test_explicit_play_releases_cloud_source_before_switching_to_local_queue(player, fake_sonos, fake_plex, monkeypatch):
    cloud_source(fake_sonos, monkeypatch)
    original = fake_sonos._SetAVTransportURI

    def sticky(args):
        if fake_sonos.av_transport_uri == SPOTIFY and fake_sonos.transport_state == 'PLAYING':
            return {}
        return original(args)
    monkeypatch.setattr(fake_sonos, '_SetAVTransportURI', sticky)
    await player.play_media(play_params(fake_plex))
    assert fake_sonos.av_transport_uri == player.coordinator().queue_uri()
    assert fake_sonos.actions().index('Stop') < fake_sonos.actions().index('SetAVTransportURI')
    assert fake_sonos.transport_state == 'PLAYING' and player.current.title == 'Track 1'


async def test_unconfirmed_source_switch_is_not_reported_as_plex_playback(player, fake_sonos, fake_plex, monkeypatch):
    cloud_source(fake_sonos, monkeypatch)
    monkeypatch.setattr(fake_sonos, '_SetAVTransportURI', lambda args: {})
    await player.play_media(play_params(fake_plex))
    assert player.state == 'stopped' and player.current is None
    assert player.last_error and 'Play' not in fake_sonos.actions()


async def test_rejected_start_is_not_reported_as_playing(player, fake_sonos, fake_plex):
    fake_sonos.errors['Play'] = UPnPError(701, 'Transition not available')
    await player.play_media(play_params(fake_plex))
    assert player.state == 'stopped' and player.last_error
