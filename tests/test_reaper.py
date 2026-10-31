"""Dead-torrent reaper: must never remove a torrent just restored after a restart."""

import time

import pytest

from media_box.torrent_client import TorrentClient

SIX_H = 6 * 3600


@pytest.fixture
def client():
    """A TorrentClient with only the reaper's state — no libtorrent session."""
    c = object.__new__(TorrentClient)
    c._dead_timeout = SIX_H
    c._started_at = time.time()
    c._meta = {}
    c._health = {}
    c.removed = []
    c._remove = lambda h, delete_files=False: c.removed.append(h)
    c._emit_event = lambda *a, **k: None
    return c


def torrent(h, *, added_ago, progress=0.0, state="metaDL"):
    return {"hash": h, "name": h, "progress": progress, "state": state,
            "added_on": int(time.time() - added_ago)}


def test_restored_old_torrent_not_reaped_right_after_restart(client):
    # the production incident: added months ago, restored without resume
    # data, reads 0% while refetching metadata -> must not be removed
    assert not client._reap_if_dead(torrent("a", added_ago=90 * 86400))
    assert client.removed == []


def test_completed_torrent_never_reaped(client):
    client._started_at -= 2 * SIX_H
    client._meta["a"] = {"completed_on": 1}
    assert not client._reap_if_dead(torrent("a", added_ago=90 * 86400))


def test_checking_torrent_not_reaped(client):
    client._started_at -= 2 * SIX_H
    assert not client._reap_if_dead(torrent("a", added_ago=90 * 86400, state="checkingDL"))


def test_dead_torrent_reaped_after_timeout_in_this_session(client):
    client._started_at -= SIX_H + 60
    assert client._reap_if_dead(torrent("a", added_ago=90 * 86400))
    assert client.removed == ["a"]


def test_fresh_torrent_not_reaped(client):
    client._started_at -= 2 * SIX_H
    assert not client._reap_if_dead(torrent("a", added_ago=3600))


def test_partial_progress_never_reaped(client):
    client._started_at -= 2 * SIX_H
    assert not client._reap_if_dead(torrent("a", added_ago=90 * 86400, progress=0.01, state="stalledDL"))


def test_queued_torrent_never_reaped(client):
    # 2026-10-31: torrents waiting for a download slot read 0% with no peers;
    # they must not be treated as dead (by the reaper or the health checks)
    client._started_at -= 2 * SIX_H
    client.on_torrent_event = None
    client._check_health([torrent("q", added_ago=90 * 86400, state="queuedDL")])
    assert client.removed == []
