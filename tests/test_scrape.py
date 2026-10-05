"""Offline tests for the scraping layer.

Live scraping is deliberately not tested. These cover the parts that run
without a network: the rate-limit ceiling, fixture loading, the cache
predicate, and post serialization.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from bardeals.config import Venue, load_config
from bardeals.scrape import RateLimiter, RawPost, Scraper, permalink_for


@pytest.fixture
def config():
    """Config loaded from the repo's real config.yaml."""
    return load_config()


@pytest.fixture
def logger():
    """A quiet logger that discards records."""
    log = logging.getLogger("bardeals.test")
    log.addHandler(logging.NullHandler())
    log.propagate = False
    return log


def test_rate_limiter_counts_requests(logger):
    limiter = RateLimiter(10, logger)
    for index in range(4):
        limiter.acquire("test", f"n{index}")
    assert limiter.total == 4
    assert limiter.used_this_hour == 4


def test_rate_limiter_blocks_at_ceiling(logger, monkeypatch):
    """Once the ceiling is hit, the next acquire must sleep."""
    slept: list[float] = []
    monkeypatch.setattr("bardeals.scrape.time.sleep", lambda seconds: slept.append(seconds))

    limiter = RateLimiter(3, logger, window_seconds=60.0)
    for index in range(3):
        limiter.acquire("test", f"n{index}")
    assert slept == []

    limiter.acquire("test", "over-the-line")
    assert len(slept) == 1
    assert 0 < slept[0] <= 61


def test_rate_limiter_forgets_old_requests(logger):
    """Requests older than the window stop counting against the ceiling."""
    limiter = RateLimiter(3, logger, window_seconds=0.0)
    for index in range(5):
        limiter.acquire("test", f"n{index}")
    assert limiter.total == 5
    assert limiter.used_this_hour <= 1


def test_rawpost_roundtrip():
    post = RawPost(
        shortcode="ABC123",
        username="murphyspubcincy",
        caption="$2 domestics tonight! \U0001f37b",
        posted_at=datetime(2026, 9, 17, 23, 0, tzinfo=timezone.utc),
        image_url="https://example.invalid/x.jpg",
        local_image_path=None,
        permalink=permalink_for("ABC123"),
        is_video=False,
        typename="GraphImage",
    )
    restored = RawPost.from_dict(post.to_dict())
    assert restored == post


def test_rawpost_from_dict_assumes_utc_when_naive():
    restored = RawPost.from_dict(
        {
            "shortcode": "XYZ",
            "username": "ladder_19",
            "caption": "",
            "posted_at": "2026-09-17T23:00:00",
        }
    )
    assert restored.posted_at.tzinfo is timezone.utc
    assert restored.permalink == "https://www.instagram.com/p/XYZ/"


def test_fixture_mode_loads_posts(config):
    scraper = Scraper(config, use_fixtures=True)
    posts = scraper.fetch_recent_posts("murphyspubcincy", limit=10)
    assert posts, "expected fixture posts for murphyspubcincy"
    assert all(post.username == "murphyspubcincy" for post in posts)
    assert scraper.limiter.total == 0, "fixture mode must make no requests"


def test_fixture_mode_resolves_flyer_image(config):
    scraper = Scraper(config, use_fixtures=True)
    posts = {p.shortcode: p for p in scraper.fetch_recent_posts("ladder_19", limit=10)}
    flyer = posts["FIXLADDER01"]
    assert flyer.local_image_path is not None
    assert flyer.local_image_path.endswith("FIXLADDER01.jpg")


def test_fixture_mode_honors_cache_predicate(config):
    """A shortcode already in the DB is skipped entirely."""
    seen = {"FIXMURPH01"}
    scraper = Scraper(config, use_fixtures=True, is_cached=lambda code: code in seen)
    codes = {post.shortcode for post in scraper.fetch_recent_posts("murphyspubcincy", limit=10)}
    assert "FIXMURPH01" not in codes
    assert codes, "other posts should still come through"


def test_fixture_limit_is_respected(config):
    scraper = Scraper(config, use_fixtures=True)
    assert len(scraper.fetch_recent_posts("murphyspubcincy", limit=2)) == 2


def test_missing_fixture_returns_empty(config):
    """A missing account must not raise - one dead account never kills a run."""
    scraper = Scraper(config, use_fixtures=True)
    assert scraper.fetch_recent_posts("definitely_not_a_real_bar", limit=5) == []


@pytest.mark.parametrize("handles,expected", [
    (None, ["existing_handle"]),
    (["new_restaurant", "existing_handle", "unknown_handle"], ["existing_handle", "unknown_handle"]),
    (["NEW_RESTAURANT"], []),
])
def test_fetch_all_skips_listings_without_instagram(config, monkeypatch, handles, expected):
    config = replace(config, venues=[Venue("existing_handle", "Existing Bar"),
                                    Venue("new_restaurant", "Restaurant", instagram=False)])
    scraper = Scraper(config, use_fixtures=True)
    fetched = []

    def fake_fetch(username, limit):
        fetched.append(username)
        return []

    monkeypatch.setattr(scraper, "fetch_recent_posts", fake_fetch)
    assert list(scraper.fetch_all(handles)) == []
    assert fetched == expected


def test_unreadable_fixture_returns_empty(config, tmp_path, logger):
    """Corrupt fixture JSON is logged and skipped, not raised."""
    broken = tmp_path / "brokenbar.json"
    broken.write_text("{not json", encoding="utf-8")

    from dataclasses import replace

    scraper = Scraper(replace(config, fixtures_dir=tmp_path), use_fixtures=True, logger=logger)
    assert scraper.fetch_recent_posts("brokenbar", limit=5) == []


def test_fetch_all_covers_every_configured_instagram_venue(config, monkeypatch):
    """Only configured Instagram accounts are fetched, not website-only listings."""
    visited: list[str] = []

    def fake_fetch(self, username, limit=None):
        visited.append(username)
        return []

    monkeypatch.setattr(Scraper, "fetch_recent_posts", fake_fetch)
    scraper = Scraper(config, use_fixtures=True)
    list(scraper.fetch_all())
    assert visited == [venue.slug for venue in config.venues if venue.instagram]
    assert len(visited) == 11


def test_fixtures_on_disk_are_wellformed(config):
    """Every fixture file parses and carries the fields RawPost needs."""
    for path in config.fixtures_dir.glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["posts"], f"{path.name} has no posts"
        for item in payload["posts"]:
            post = RawPost.from_dict(item)
            assert post.shortcode and post.username
            assert post.posted_at.tzinfo is not None
