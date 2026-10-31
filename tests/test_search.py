"""Tests for the search pipeline, indexer health and payload inspection.

No network: pyackett is replaced by a fake that returns scripted reports.
"""

from datetime import datetime

import pytest
from pyackett.core.models import IndexerSearchStatus, ReleaseInfo, SearchReport

from media_box import torrents
from media_box.indexer_health import BENCH_STEPS, IndexerHealth
from media_box.mover import inspect_payload


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def rel(title, h=None, seeders=10, size=1_000_000_000, origin="tpb", link=None, magnet=True):
    m = f"magnet:?xt=urn:btih:{h}&dn=x" if (h and magnet) else None
    return ReleaseInfo(title=title, info_hash=h, magnet_uri=m, link=link or m, seeders=seeders,
                       peers=seeders + 1, size=size, origin_id=origin, origin_name=origin.upper(),
                       publish_date=datetime(2024, 1, 1))


class FakePyackett:
    """Scripted pyackett: script(query, indexer_ids) -> {iid: (status, [releases])}."""

    def __init__(self, script):
        self.script = script
        self.calls = []

    async def search_detailed(self, query, indexer_ids=None, deadline=None, **kw):
        self.calls.append((query, list(indexer_ids or [])))
        rep = SearchReport()
        for iid, (status, releases) in self.script(query, indexer_ids).items():
            if iid not in indexer_ids:
                continue
            rep.statuses[iid] = IndexerSearchStatus(
                iid, status, result_count=len(releases),
                http_status=429 if status == "rate_limited" else (403 if status == "blocked" else None),
                retry_after=None)
            rep.results.extend(releases)
        return rep


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """Point torrents.py at a fake pyackett + temp health file."""
    def install(script, ids):
        fake = FakePyackett(script)
        monkeypatch.setattr(torrents, "_pyackett_instance", fake)
        monkeypatch.setattr(torrents, "_indexer_ids", ids)
        monkeypatch.setattr(torrents, "_health", IndexerHealth(tmp_path / "health.json"))
        async def no_imdb(title, timeout=4.0):
            return "tt0903747" if "breaking bad" in title.lower() else None
        monkeypatch.setattr(torrents, "resolve_imdb", no_imdb)
        return fake
    return install


# ---------------------------------------------------------------------------
# Query parsing and variants
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("q,title,season,episode", [
    ("Breaking Bad S03E07", "Breaking Bad", 3, 7),
    ("breaking bad s3e7 1080p", "breaking bad 1080p", 3, 7),
    ("Your Friends & Neighbors S02", "Your Friends & Neighbors", 2, None),
    ("The Wire Season 4", "The Wire", 4, None),
    ("My Cousin Vinny 1992", "My Cousin Vinny 1992", None, None),
])
def test_parse_query(q, title, season, episode):
    p = torrents.parse_query(q)
    assert (p.title, p.season, p.episode) == (title, season, episode)


def test_variants():
    v = torrents._variants(torrents.parse_query("Your Friends & Neighbors S02E03"))
    assert v[0] == ("Your Friends and Neighbors", 2, 3)
    assert ("Your Friends Neighbors", 2, 3) in v
    v = torrents._variants(torrents.parse_query("Blade Runner: 2049 S01E01"))
    titles = [t for t, _, _ in v]
    assert "Blade Runner 2049" in titles and "Blade Runner" in titles
    assert any(e is None for _, _, e in v)  # season-pack fallback


# ---------------------------------------------------------------------------
# Merge / annotate
# ---------------------------------------------------------------------------

def test_merge_by_infohash_keeps_best_seeders_and_all_sources():
    h = "a" * 40
    rows = torrents.merge([
        rel("Movie 2020 1080p", h, seeders=5, origin="tpb"),
        rel("Movie.2020.1080p", h.upper(), seeders=50, origin="knaben"),
        rel("Other", "b" * 40, seeders=7),
        rel("Dead", "c" * 40, seeders=0),
    ])
    assert len(rows) == 2  # dupes merged, zero-seeder dropped
    m = rows[0]
    assert m["Seeders"] == 50 and len(m["Sources"]) == 2
    assert m["Tracker"] == "TPB +1"


def test_merge_hashless_by_title_and_size():
    rows = torrents.merge([
        rel("My Movie 2026 1080p", None, origin="tgx", link="https://tgx/1"),
        rel("My.Movie.2026.1080p", None, origin="rarbg", link="https://rarbg/1"),
        rel("My Movie 2026 1080p", None, origin="x", link="https://x/1", size=5),
        rel("My Movie 2026 1080p", None, origin="y", link="https://y/1", size=1_003_000_000),
    ])
    assert len(rows) == 2  # 1.003 GB vs 1.0 GB: same release, different rounding
    assert [s["link"] for s in rows[0]["Sources"]] == ["https://tgx/1", "https://rarbg/1", "https://y/1"]


def test_merge_hashless_joins_hashed_row():
    rows = torrents.merge([
        rel("Show.S01E01.1080p", None, origin="tp2", link="https://tp2/1", seeders=40),
        rel("Show S01E01 1080p", "a" * 40, origin="tpb", seeders=30),  # hashed arrives later
    ])
    assert len(rows) == 1 and rows[0]["Seeders"] == 40 and len(rows[0]["Sources"]) == 2


def test_merge_infohash_from_base32_magnet():
    import base64
    raw = bytes(range(20))
    b32 = base64.b32encode(raw).decode()
    r = ReleaseInfo(title="x", magnet_uri=f"magnet:?xt=urn:btih:{b32}", seeders=1)
    assert torrents._infohash(r) == raw.hex()


@pytest.mark.parametrize("title,size,expect", [
    ("Movie 2020 1080p BluRay", 4e9, ""),
    ("Movie 2020 1080p BluRay", 2e8, "small for 1080p"),
    ("Show S01E01 1080p WEB", 5e8, ""),        # episodes get a lower floor
    ("Movie 2020 1080p.rar", 4e9, "archive"),
    ("Movie 2020 setup.exe", 4e9, "executable?"),
])
def test_annotate(title, size, expect):
    assert torrents.annotate({"Title": title, "Size": size}) == expect


# ---------------------------------------------------------------------------
# Indexer health
# ---------------------------------------------------------------------------

def test_health_benches_after_repeated_failures_and_recovers(tmp_path):
    h = IndexerHealth(tmp_path / "h.json")
    now = 1000.0
    assert h.record("a", "blocked", now=now) is None
    assert h.record("a", "blocked", now=now) is None
    note = h.record("a", "blocked", detail="HTTP 403", now=now)
    assert note and "benched" in note
    assert h.partition(["a", "b"], now=now + 1) == (["b"], [], ["a"])
    # cooldown over -> probe
    assert h.partition(["a"], now=now + BENCH_STEPS[0] + 1) == ([], ["a"], [])
    # failed probe -> benched again, longer
    h.record("a", "timeout", now=now + BENCH_STEPS[0] + 2)
    until, _ = h.bench_info("a")
    assert until == pytest.approx(now + BENCH_STEPS[0] + 2 + BENCH_STEPS[1])
    # successful probe -> fully back
    assert "recovered" in h.record("a", "ok", now=until + 1)
    assert h.partition(["a"], now=until + 2) == (["a"], [], [])


def test_health_empty_is_healthy(tmp_path):
    h = IndexerHealth(tmp_path / "h.json")
    for _ in range(10):
        h.record("a", "empty")
    assert h.partition(["a"])[0] == ["a"]


def test_health_rate_limit_benches_immediately_with_retry_after(tmp_path):
    h = IndexerHealth(tmp_path / "h.json")
    h.record("a", "rate_limited", retry_after=7200, now=0)
    until, reason = h.bench_info("a")
    assert until == 7200 and "rate limited" in reason


def test_health_canary_empty_benches(tmp_path):
    h = IndexerHealth(tmp_path / "h.json")
    assert h.record_canary("a", "empty", 0, now=0) is None
    assert "definition likely broken" in h.record_canary("a", "empty", 0, now=0)
    # a canary with results clears it
    h.reset("a")
    h.record_canary("a", "empty", 0)
    h.record_canary("a", "ok", 20)
    assert h._state["a"]["canary_empty"] == 0


def test_health_persists(tmp_path):
    p = tmp_path / "h.json"
    h = IndexerHealth(p)
    h.record("a", "rate_limited", retry_after=60, now=0)
    h.save()
    assert IndexerHealth(p).bench_info("a")[0] == 900  # min 15m step


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

async def test_pipeline_skips_benched_and_reports(engine):
    fake = engine(lambda q, ids: {
        "tpb": ("ok", [rel("Breaking Bad S03E07 1080p", "a" * 40)]),
        "dead": ("blocked", []),
        "knaben": ("ok", [rel("Breaking Bad S03E07 720p", "b" * 40), rel("Breaking Bad S03E07 2160p", "c" * 40)]),
    }, ["tpb", "dead", "knaben"])
    torrents.health()._state["dead"] = {**torrents.health()._get("dead"), "benched_until": 9e18,
                                        "bench_reason": "x"}
    out = await torrents.search("Breaking Bad S03E07", category=5000)
    assert all("dead" not in ids for _, ids in fake.calls)
    by_id = {o.id: o for o in out.outcomes}
    assert by_id["dead"].status == "benched"
    assert len(out.results) == 3 and out.variants == []


async def test_pipeline_routes_imdb_only_to_id_sources(engine):
    fake = engine(lambda q, ids: {
        "tpb": ("ok", [rel("Breaking Bad S03E07", c * 40) for c in "abc"]),
        "eztv-api": ("ok", [rel("Breaking Bad S03E07 eztv", "d" * 40)]),
    }, ["tpb", "eztv-api"])
    out = await torrents.search("Breaking Bad S03E07", category=5000)
    assert out.imdb == "tt0903747"
    for q, ids in fake.calls:
        if ids == ["eztv-api"]:
            assert q.imdb_id == "tt0903747" and q.season == 3 and q.episode == "7"
        else:
            assert q.imdb_id is None and q.get_query_string() == "Breaking Bad S03E07"


async def test_pipeline_id_source_skipped_without_imdb(engine):
    engine(lambda q, ids: {"tpb": ("ok", [rel(f"Movie {i}", str(i) * 40) for i in range(1, 5)])},
           ["tpb", "eztv-api"])
    out = await torrents.search("Some Movie 2020", category=2000)
    assert {o.id: o.status for o in out.outcomes}["eztv-api"] == "skipped"


async def test_pipeline_tries_variants_when_thin(engine):
    def script(q, ids):
        if "and" in (q.search_term or ""):
            return {"tpb": ("ok", [rel(f"Your Friends and Neighbors S02E0{i}", str(i) * 40) for i in range(1, 5)])}
        return {"tpb": ("empty", [])}
    fake = engine(script, ["tpb"])
    out = await torrents.search("Your Friends & Neighbors S02", category=5000)
    assert out.variants and "Your Friends and Neighbors S02" in out.variants[0]
    assert len(out.results) == 4
    assert len(fake.calls) == 2


async def test_pipeline_failures_feed_health(engine):
    engine(lambda q, ids: {"tpb": ("rate_limited", []), "lime": ("ok", [rel("x", "a" * 40)] * 3)},
           ["tpb", "lime"])
    out = await torrents.search("x")
    assert any("tpb benched" in n for n in out.notes)
    assert torrents.health().partition(["tpb", "lime"])[2] == ["tpb"]


async def test_resolve_any_falls_back_across_sources(monkeypatch):
    calls = []

    async def fake_resolve(link, tracker_id=None):
        calls.append(link)
        if "dead" in link:
            raise RuntimeError("404")
        return "magnet:?xt=urn:btih:" + "e" * 40

    monkeypatch.setattr(torrents, "resolve_link", fake_resolve)
    row = {"Title": "x", "Sources": [
        {"tracker": "A", "tracker_id": "a", "link": "https://dead/1", "magnet": None},
        {"tracker": "B", "tracker_id": "b", "link": "https://ok/1", "magnet": None},
    ]}
    src, via = await torrents.resolve_any(row)
    assert src.startswith("magnet:") and via == "B" and calls == ["https://dead/1", "https://ok/1"]


# ---------------------------------------------------------------------------
# Payload inspection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("names,verdict", [
    (["Movie/Movie.2020.1080p.mkv", "Movie/Sample/sample.mkv", "Movie/RARBG.txt"], "ok"),
    (["Movie/Movie.2020.1080p.mkv", "Movie/Codec.exe"], "reject"),
    (["Movie/Movie.2020.1080p.lnk"], "reject"),
    (["Show.S01E01.7z"], "warn"),
    (["Show/show.part1.rar", "Show/show.r00", "Show/show.nfo"], "warn"),
    (["Movie/readme.txt", "Movie/cover.jpg"], "reject"),
    (["Site www.example.com", "Movie.mkv"], "ok"),
])
def test_inspect_payload(names, verdict):
    assert inspect_payload([{"name": n} for n in names])[0] == verdict
