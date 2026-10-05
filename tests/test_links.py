"""Tests for source links.

A control labelled "verify" makes a promise to the reader, so the rule these
tests enforce is simple: the UI never renders an anchor it knows is a dead end.

No network - reachability is decided from the stored URL and shortcode.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from bardeals import db
from bardeals.config import load_config
from bardeals.db import Deal
from bardeals.scrape import RawPost
from bardeals.web import create_app, is_sample_source, source_link

pytest.importorskip("flask")


@pytest.fixture
def config(tmp_path):
    return replace(load_config(), db_path=tmp_path / "links.db")


@pytest.fixture
def conn(config):
    connection = db.connect(config.db_path)
    db.init_db(connection)
    yield connection
    connection.close()


@pytest.fixture
def client(config, conn):
    app = create_app(config)
    app.config.update(TESTING=True)
    return app.test_client()


def add(conn, shortcode, permalink, username="murphyspubcincy", source_kind="instagram"):
    post = RawPost(
        shortcode=shortcode,
        username=username,
        caption="",
        posted_at=datetime.now(timezone.utc),
        image_url=None,
        local_image_path=None,
        permalink=permalink,
        is_video=False,
        typename="GraphImage",
    )
    post.source_kind = source_kind
    db.insert_post(conn, post)
    db.insert_deal(
        conn,
        Deal(
            shortcode=shortcode,
            venue_slug=username,
            venue_name="Murphy's Pub",
            deal_text=f"deal from {shortcode}",
            price=2.0,
            recurrence="daily",
            confidence="high",
            source="caption",
        ),
    )


def text(response) -> str:
    return response.get_data(as_text=True)


# --------------------------------------------------------------------------- #
# detecting unreachable sources
# --------------------------------------------------------------------------- #


def test_reserved_demo_domain_is_sample():
    """example.invalid is reserved and never resolves - this was the live bug."""
    assert is_sample_source("https://example.invalid/demo/DEMOWEEK00", "DEMOWEEK00")


def test_invented_fixture_shortcodes_are_sample():
    """instagram.com/p/FIXMURPH01/ is a real host but a 404 page."""
    assert is_sample_source("https://www.instagram.com/p/FIXMURPH01/", "FIXMURPH01")


def test_demo_shortcode_alone_is_enough():
    assert is_sample_source("https://www.instagram.com/p/DEMOX/", "DEMOX")


def test_a_real_post_is_not_sample():
    assert not is_sample_source("https://www.instagram.com/p/C9xKq2LStuV/", "C9xKq2LStuV")


def test_a_real_website_is_not_sample():
    assert not is_sample_source("https://murphys-pub.com/", "web-874b186a076dea35")


# --------------------------------------------------------------------------- #
# what the control resolves to
# --------------------------------------------------------------------------- #


def test_sample_row_points_at_the_real_profile():
    """A dead demo link becomes the bar's actual Instagram, which does open."""
    link = source_link("https://example.invalid/demo/X", "DEMOX", "murphyspubcincy")
    assert link["kind"] == "sample"
    assert link["url"] == "https://www.instagram.com/murphyspubcincy/"
    assert "sample" in link["title"].lower()


def test_real_post_keeps_the_verify_label():
    link = source_link("https://www.instagram.com/p/C9xKq2L/", "C9xKq2L", "ladder_19")
    assert link["kind"] == "post"
    assert link["url"] == "https://www.instagram.com/p/C9xKq2L/"
    assert "verify" in link["label"]


def test_website_row_says_view_page_not_verify():
    """"Verify" implies a source post; a scraped page is a different promise."""
    link = source_link("https://murphys-pub.com/", "web-abc", "murphyspubcincy", "website")
    assert link["kind"] == "page"
    assert "view page" in link["label"]


def test_missing_permalink_yields_no_anchor():
    link = source_link(None, "web-abc", None)
    assert link["kind"] == "none"
    assert link["url"] is None


def test_non_http_permalink_yields_no_anchor():
    """A javascript: or relative URL must never become an anchor."""
    for bad in ("javascript:alert(1)", "/relative/path", "ftp://x/y", "not a url"):
        assert source_link(bad, "abc123", "murphyspubcincy")["url"] is None, bad


def test_sample_without_a_venue_has_nowhere_to_go():
    link = source_link("https://example.invalid/x", "DEMOX", None)
    assert link["kind"] == "none"
    assert link["url"] is None


# --------------------------------------------------------------------------- #
# rendered pages
# --------------------------------------------------------------------------- #


def _hrefs(body: str) -> list[str]:
    return re.findall(r'href="([^"]+)"', body)


def test_no_page_ever_links_to_the_dead_demo_domain(client, conn):
    """The reported bug: clicking verify landed on a dead page."""
    add(conn, "DEMOWEEK00", "https://example.invalid/demo/DEMOWEEK00")
    for path in ("/", "/week", "/venue/murphyspubcincy", "/?sort=price", "/?sort=bar"):
        body = text(client.get(path))
        assert "example.invalid" not in body, path


def test_sample_rows_are_labelled_as_sample(client, conn):
    add(conn, "DEMOWEEK00", "https://example.invalid/demo/DEMOWEEK00")
    assert "chip--sample" in text(client.get("/"))


def test_sample_row_links_to_the_profile(client, conn):
    add(conn, "DEMOWEEK00", "https://example.invalid/demo/DEMOWEEK00")
    assert "https://www.instagram.com/murphyspubcincy/" in _hrefs(text(client.get("/")))


def test_real_post_link_survives_untouched(client, conn):
    add(conn, "C9xKq2LStuV", "https://www.instagram.com/p/C9xKq2LStuV/")
    assert "https://www.instagram.com/p/C9xKq2LStuV/" in _hrefs(text(client.get("/")))


def test_a_row_with_no_source_renders_text_not_a_link(client, conn):
    add(conn, "web-nolink", "", source_kind="website")
    body = text(client.get("/"))
    assert "verify--none" in body
    assert 'href=""' not in body


def test_every_rendered_href_is_openable(client, conn):
    """Sweep every link on the page for dead-end hosts and empty targets."""
    add(conn, "DEMOWEEK00", "https://example.invalid/demo/DEMOWEEK00")
    add(conn, "C9xKq2LStuV", "https://www.instagram.com/p/C9xKq2LStuV/", username="ladder_19")
    add(conn, "web-abc", "https://murphys-pub.com/", source_kind="website")

    for path in ("/", "/week", "/venue/murphyspubcincy"):
        for href in _hrefs(text(client.get(path))):
            assert href.strip(), f"empty href on {path}"
            assert "example.invalid" not in href, f"{href} on {path}"
            assert not href.lower().startswith("javascript:"), f"{href} on {path}"


def test_week_view_omits_dead_links_entirely(client, conn):
    add(conn, "DEMOWEEK00", "https://example.invalid/demo/DEMOWEEK00")
    body = text(client.get("/week"))
    assert "example.invalid" not in body


def test_json_api_still_exposes_the_raw_permalink(client, conn):
    """The API is for scripts; it should report what is stored, not a substitute."""
    add(conn, "DEMOWEEK00", "https://example.invalid/demo/DEMOWEEK00")
    payload = client.get("/api/deals.json").get_json()
    deal = payload["deals"][0]
    assert deal["permalink"] == "https://example.invalid/demo/DEMOWEEK00"
    assert deal["verify"]["kind"] == "sample"
