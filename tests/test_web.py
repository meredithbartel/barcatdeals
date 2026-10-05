"""Tests for the local web UI.

Exercised through Flask's test client against a temporary database, so no
server and no network. The assertions are about what reaches the reader:
cheapest-first ordering, live "on now" badges, honest empty and stale states,
low-confidence deals shown rather than hidden, and a verify link on every deal.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from bardeals import db
from bardeals.config import load_config
from bardeals.db import Deal
from bardeals.scrape import RawPost
from bardeals.web import create_app, deal_view, freshness, group_by_venue, is_on_now

pytest.importorskip("flask")

WEDNESDAY = date(2026, 9, 16)


@pytest.fixture
def config(tmp_path):
    """Config pointed at a throwaway database."""
    return replace(load_config(), db_path=tmp_path / "web.db")


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


def add_post(conn, shortcode, username="murphyspubcincy", days_ago=0):
    db.insert_post(
        conn,
        RawPost(
            shortcode=shortcode,
            username=username,
            caption="",
            posted_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
            image_url=None,
            local_image_path=None,
            permalink=f"https://www.instagram.com/p/{shortcode}/",
            is_video=False,
            typename="GraphImage",
        ),
    )


def add_deal(conn, shortcode="AAA", **overrides):
    values = {
        "shortcode": shortcode,
        "venue_slug": "murphyspubcincy",
        "venue_name": "Murphy's Pub",
        "deal_text": "$2 domestic drafts",
        "item": "domestic drafts",
        "price": 2.0,
        "recurrence": "daily",
        "confidence": "high",
        "source": "caption",
    }
    values.update(overrides)
    db.insert_deal(conn, Deal(**values))


def text(response) -> str:
    return response.get_data(as_text=True)


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #


def test_index_renders(client, conn):
    add_post(conn, "AAA")
    add_deal(conn)
    body = text(client.get("/"))
    assert "$2 domestic drafts" in body
    assert "Murphy&#39;s Pub" in body or "Murphy's Pub" in body


def test_every_deal_shows_a_verify_link(client, conn):
    add_post(conn, "AAA")
    add_deal(conn)
    body = text(client.get("/"))
    assert "https://www.instagram.com/p/AAA/" in body
    assert "verify" in body


def test_unknown_venue_is_404(client):
    assert client.get("/venue/not_a_real_bar").status_code == 404


def test_known_venue_renders(client, conn):
    add_post(conn, "AAA")
    add_deal(conn, "AAA", recurrence="weekly", day_of_week=3)
    response = client.get("/venue/murphyspubcincy")
    assert response.status_code == 200
    assert "$2 domestic drafts" in text(response)


def test_bad_day_parameter_is_400(client):
    assert client.get("/?day=not-a-date").status_code == 400


def test_healthz_reports_counts(client, conn):
    add_post(conn, "AAA")
    add_deal(conn)
    payload = client.get("/healthz").get_json()
    assert payload["ok"] is True
    assert payload["posts"] == 1 and payload["deals"] == 1


def test_json_api_is_serializable(client, conn):
    """sqlite3.Row is not JSON-serializable; the view layer must convert."""
    add_post(conn, "AAA")
    add_deal(conn)
    payload = client.get("/api/deals.json").get_json()
    assert payload["deals"][0]["deal_text"] == "$2 domestic drafts"
    assert payload["cheapest"] == 2.0
    assert "last_fetched_at" in payload


def test_static_css_is_served(client):
    response = client.get("/static/style.css")
    assert response.status_code == 200
    assert b"--accent" in response.data


# --------------------------------------------------------------------------- #
# ordering
# --------------------------------------------------------------------------- #


def test_cheapest_deal_comes_first(client, conn):
    add_post(conn, "AAA")
    add_deal(conn, "AAA", deal_text="expensive thing", price=12.0)
    add_deal(conn, "AAA", deal_text="cheap thing", price=1.0)
    add_deal(conn, "AAA", deal_text="middling thing", price=5.0)
    body = text(client.get("/"))
    assert body.index("cheap thing") < body.index("middling thing") < body.index("expensive thing")


def test_unpriced_deals_sort_last(client, conn):
    add_post(conn, "AAA")
    add_deal(conn, "AAA", deal_text="half off wings", price=None)
    add_deal(conn, "AAA", deal_text="ten dollar thing", price=10.0)
    body = text(client.get("/"))
    assert body.index("ten dollar thing") < body.index("half off wings")


def test_sort_by_bar_groups_venues(client, conn):
    add_post(conn, "AAA", username="murphyspubcincy")
    add_post(conn, "BBB", username="ladder_19", days_ago=1)
    add_deal(conn, "AAA")
    add_deal(conn, "BBB", venue_slug="ladder_19", venue_name="Ladder 19",
             deal_text="$1 wings", price=1.0)
    body = text(client.get("/?sort=bar"))
    assert "Ladder 19" in body and "$1 wings" in body


def test_invalid_sort_falls_back_to_price(client, conn):
    add_post(conn, "AAA")
    add_deal(conn)
    assert client.get("/?sort=nonsense").status_code == 200


def test_group_by_venue_reports_cheapest_per_bar():
    views = [
        {"venue_slug": "a", "venue_name": "A Bar", "price": 5.0},
        {"venue_slug": "a", "venue_name": "A Bar", "price": 2.0},
        {"venue_slug": "b", "venue_name": "B Bar", "price": None},
    ]
    grouped = {g["venue_slug"]: g for g in group_by_venue(views)}
    assert grouped["a"]["cheapest"] == 2.0
    assert grouped["a"]["deals"][0]["price"] == 2.0
    assert grouped["b"]["cheapest"] is None


# --------------------------------------------------------------------------- #
# "on now"
# --------------------------------------------------------------------------- #


def _row(start, end):
    return {"start_time": start, "end_time": end}


def test_on_now_inside_a_window():
    now = datetime(2026, 9, 16, 18, 30, tzinfo=timezone.utc)
    assert is_on_now(_row("17:00", "21:00"), now)


def test_not_on_now_before_a_window():
    now = datetime(2026, 9, 16, 15, 0, tzinfo=timezone.utc)
    assert not is_on_now(_row("17:00", "21:00"), now)


def test_not_on_now_after_a_window():
    now = datetime(2026, 9, 16, 23, 0, tzinfo=timezone.utc)
    assert not is_on_now(_row("17:00", "21:00"), now)


def test_window_crossing_midnight():
    """"9pm-2am" is how bars write it; 1am must count as inside."""
    late = datetime(2026, 9, 16, 1, 0, tzinfo=timezone.utc)
    assert is_on_now(_row("21:00", "02:00"), late)
    assert is_on_now(_row("21:00", "02:00"), datetime(2026, 9, 16, 22, 0, tzinfo=timezone.utc))
    assert not is_on_now(_row("21:00", "02:00"), datetime(2026, 9, 16, 15, 0, tzinfo=timezone.utc))


def test_open_ended_windows():
    now = datetime(2026, 9, 16, 23, 0, tzinfo=timezone.utc)
    assert is_on_now(_row("22:00", None), now)
    assert not is_on_now(_row(None, "22:00"), now)


def test_a_deal_with_no_times_is_never_on_now():
    """Claiming a timeless deal is "on now" would be a guess."""
    assert not is_on_now(_row(None, None), datetime(2026, 9, 16, 20, 0, tzinfo=timezone.utc))


def test_malformed_times_do_not_crash():
    assert not is_on_now(_row("nonsense", "also bad"),
                         datetime(2026, 9, 16, 20, 0, tzinfo=timezone.utc))


# --------------------------------------------------------------------------- #
# empty, stale and low-confidence states
# --------------------------------------------------------------------------- #


def test_empty_database_says_so_honestly(client):
    body = text(client.get("/"))
    assert "No deals on record" in body
    assert "No specials have been collected yet" in body


def test_stored_posts_but_no_deals_today(client, conn):
    add_post(conn, "AAA")
    body = text(client.get("/"))
    assert "No deals on record" in body
    assert "0 specials on record" in body
    assert "Explore the week" in body


def test_low_confidence_is_shown_not_hidden(client, conn):
    add_post(conn, "AAA")
    add_deal(conn, "AAA", deal_text="mystery special", confidence="low")
    body = text(client.get("/"))
    assert "mystery special" in body
    assert "Unconfirmed" in body or "unconfirmed" in body


def test_undated_deal_appears_in_its_own_section(client, conn):
    add_post(conn, "AAA", days_ago=1)
    add_deal(conn, "AAA", deal_text="same deal as always", recurrence="unknown",
             price=None, confidence="low")
    body = text(client.get("/"))
    assert "same deal as always" in body
    assert "no date given" in body


def test_bars_with_nothing_are_named(client, conn):
    """A place remains discoverable when it has no collected specials."""
    add_post(conn, "AAA")
    add_deal(conn)
    body = text(client.get("/"))
    assert 'href="/venue/ladder_19"' in body
    assert "Ladder 19" in body


def test_freshness_labels():
    now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
    assert freshness(None, now)["stale"] is True
    assert freshness(None, now)["label"] == "never fetched"
    assert freshness((now - timedelta(minutes=2)).isoformat(), now)["label"] == "just now"
    assert freshness((now - timedelta(minutes=30)).isoformat(), now)["label"] == "30 min ago"
    assert freshness((now - timedelta(hours=5)).isoformat(), now)["label"] == "5 hours ago"
    assert freshness((now - timedelta(days=3)).isoformat(), now)["label"] == "3 days ago"


def test_fresh_data_is_not_flagged_stale():
    now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
    assert freshness((now - timedelta(hours=6)).isoformat(), now)["stale"] is False


def test_old_data_is_flagged_stale():
    now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
    assert freshness((now - timedelta(days=4)).isoformat(), now)["stale"] is True


def test_unparseable_fetch_time_is_handled():
    now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
    assert freshness("not a timestamp", now)["stale"] is True


def test_stale_banner_appears(client, conn):
    """Staleness is about when we last FETCHED, not when the bar posted."""
    add_post(conn, "AAA", days_ago=9)
    add_deal(conn, "AAA")
    old = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
    conn.execute("UPDATE posts SET fetched_at = ?", (old,))
    conn.commit()
    body = text(client.get("/"))
    assert "may be out of date" in body


def test_recent_fetch_of_old_posts_is_not_stale(client, conn):
    """An old post fetched an hour ago is fresh data about an old deal."""
    add_post(conn, "AAA", days_ago=30)
    add_deal(conn, "AAA")
    body = text(client.get("/"))
    assert "may be out of date" not in body


# --------------------------------------------------------------------------- #
# day navigation
# --------------------------------------------------------------------------- #


def test_day_strip_offers_a_week(client, conn):
    add_post(conn, "AAA")
    add_deal(conn)
    body = text(client.get("/"))
    assert "Today" in body and "Tomorrow" in body
    assert body.count("daychip") >= 7


def test_specific_day_shows_that_days_deal(client, conn):
    add_post(conn, "AAA")
    add_deal(conn, "AAA", deal_text="thursday wings", recurrence="weekly", day_of_week=3)
    thursday = date(2026, 9, 17)
    assert thursday.weekday() == 3
    assert "thursday wings" in text(client.get(f"/?day={thursday.isoformat()}"))
    assert "thursday wings" not in text(client.get("/?day=2026-09-18"))


def test_dedupe_applies_to_the_web_view(client, conn):
    """A deal reposted three times renders once."""
    for index, shortcode in enumerate(["W1", "W2", "W3"]):
        add_post(conn, shortcode, days_ago=index * 7)
        add_deal(conn, shortcode, recurrence="weekly", day_of_week=WEDNESDAY.weekday())
    body = text(client.get(f"/?day={WEDNESDAY.isoformat()}"))
    assert body.count("$2 domestic drafts") == 1


# --------------------------------------------------------------------------- #
# view model
# --------------------------------------------------------------------------- #


def test_deal_view_matches_report_formatting(conn):
    """The web view and the terminal report must not drift apart."""
    from bardeals import report

    add_post(conn, "AAA")
    add_deal(conn, "AAA", price=2.5, start_time="17:00", end_time="21:00",
             recurrence="weekly", day_of_week=3)
    row = db.deals_active_on(conn, date(2026, 9, 17))[0]
    view = deal_view(row, datetime.now(timezone.utc))

    assert view["price_label"] == report.format_price(row) == "$2.50"
    assert view["window"] == report.format_time(row) == "17:00-21:00"
    assert view["when"] == report.format_when(row) == "every Thursday"


def test_deal_view_uses_empty_placeholders_not_dashes(conn):
    """HTML omits absent fields; the terminal renders "-" to keep columns."""
    add_post(conn, "AAA")
    add_deal(conn, "AAA", price=None, start_time=None, end_time=None)
    row = db.deals_active_on(conn, date(2026, 9, 16))[0]
    view = deal_view(row, datetime.now(timezone.utc))
    assert view["price_label"] is None
    assert view["window"] == ""


def test_json_api_and_html_agree_on_ordering(client, conn):
    """The API's first deal must actually be the one it calls cheapest."""
    add_post(conn, "AAA", username="murphyspubcincy")
    add_post(conn, "BBB", username="ladder_19", days_ago=1)
    add_deal(conn, "AAA", deal_text="pricey", price=9.0)
    add_deal(conn, "BBB", venue_slug="ladder_19", venue_name="Ladder 19",
             deal_text="bargain", price=1.0)

    payload = client.get("/api/deals.json").get_json()
    assert payload["deals"][0]["price"] == payload["cheapest"] == 1.0
    assert payload["deals"][0]["deal_text"] == "bargain"

    body = text(client.get("/"))
    assert body.index("bargain") < body.index("pricey")
