"""Tests for the richer web UI: categories, time grouping, filters, week view.

Same approach as test_web.py - Flask's test client against a temporary
database, no server and no network. These cover the parts a student actually
touches: "what is on right now", "only show me things under $3", "just beer".
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from bs4 import BeautifulSoup
from flask import template_rendered

from bardeals import db
from bardeals.config import load_config
from bardeals.db import Deal
from bardeals.scrape import RawPost
from bardeals.web import _humanize, classify_deal, create_app, time_status

pytest.importorskip("flask")


@pytest.fixture
def config(tmp_path):
    return replace(load_config(), db_path=tmp_path / "ui.db")


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


def add_post(conn, shortcode="AAA", username="murphyspubcincy", days_ago=0):
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


def _row(start, end):
    return {"start_time": start, "end_time": end}


def _t(hour, minute=0):
    return datetime(2026, 9, 16, hour, minute, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# categories
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "deal_text,item,expected",
    [
        ("$3 craft drafts", "craft drafts", "beer"),
        ("$12 domestic buckets", "domestic buckets", "beer"),
        ("$2 PBR tallboys", "PBR", "beer"),
        ("half off bottles of wine", "wine", "wine"),
        ("$3 Jameson shots", "whiskey", "shots"),
        ("$5 house margaritas", "margaritas", "cocktails"),
        ("$2 you-call-its", "well drinks", "cocktails"),
        ("$1 wings", "wings", "food"),
        ("$2 street tacos", "tacos", "food"),
        ("$7 large cheese pizza", "pizza", "food"),
        ("free cover with student ID", "cover", "other"),
        ("", None, "other"),
        (None, None, "other"),
    ],
)
def test_deal_categories(deal_text, item, expected):
    assert classify_deal(deal_text, item) == expected


def test_wine_beats_beer_on_the_word_bottles():
    """"bottles of wine" must not fall into the beer bucket."""
    assert classify_deal("half off bottles of wine", None) == "wine"
    assert classify_deal("$2 domestic bottles", None) == "beer"


def test_classification_is_word_based_not_substring():
    """"hot dogs" must not match the keyword "shot"."""
    assert classify_deal("$2 hot dogs", None) != "shots"


# --------------------------------------------------------------------------- #
# time status
# --------------------------------------------------------------------------- #


def test_on_now_shows_urgency_when_closing_soon():
    status = time_status(_row("17:00", "21:00"), _t(20, 30), is_today=True)
    assert status["status"] == "now"
    assert status["label"] == "ends in 30m"


def test_on_now_shows_the_end_time_when_not_closing_soon():
    status = time_status(_row("17:00", "23:00"), _t(18, 0), is_today=True)
    assert status["status"] == "now"
    assert status["label"] == "until 23:00"


def test_starting_later_counts_down():
    status = time_status(_row("22:00", None), _t(20, 30), is_today=True)
    assert status["status"] == "later"
    assert status["label"] == "starts in 1h 30m"


def test_finished_window_is_marked_over():
    assert time_status(_row("16:00", "20:00"), _t(21, 30), is_today=True)["status"] == "over"


def test_untimed_deal_is_anytime():
    assert time_status(_row(None, None), _t(20), is_today=True)["status"] == "anytime"


def test_time_status_is_meaningless_on_another_day():
    """A deal is never "on now" while you are looking at a different day."""
    assert time_status(_row("17:00", "21:00"), _t(18), is_today=False)["status"] == "anytime"


def test_midnight_crossing_window_is_on_now():
    assert time_status(_row("21:00", "02:00"), _t(23), is_today=True)["status"] == "now"


def test_midnight_countdown_after_midnight_uses_todays_end():
    status = time_status(_row("21:00", "02:00"), _t(1, 30), is_today=True)
    assert status["label"] == "ends in 30m"


def test_midnight_countdown_before_midnight_uses_tomorrows_end():
    status = time_status(_row("21:00", "00:30"), _t(23, 45), is_today=True)
    assert status["label"] == "ends in 45m"


def test_humanize_minutes():
    assert _humanize(45) == "45m"
    assert _humanize(60) == "1h"
    assert _humanize(150) == "2h 30m"
    assert _humanize(-5) == "0m"


# --------------------------------------------------------------------------- #
# time grouping in the page
# --------------------------------------------------------------------------- #


def test_today_groups_deals_by_time(client, conn):
    add_post(conn)
    add_deal(conn, deal_text="anytime thing", start_time=None, end_time=None)
    add_deal(conn, deal_text="timed thing", start_time="00:01", end_time="23:59")
    body = text(client.get("/?sort=time"))
    assert "On now" in body
    assert "Anytime today" in body


def test_finished_deals_are_dimmed_but_not_called_unconfirmed(client, conn):
    """Two different meanings that must not be conflated in the UI."""
    add_post(conn)
    add_deal(conn, deal_text="long over", start_time="00:00", end_time="00:01")
    body = text(client.get("/?sort=time"))
    assert "Already finished" in body
    finished = body[body.index("Already finished"):]
    assert "deal--dim" in finished
    assert "chip--warn" not in finished


def test_other_days_do_not_use_time_grouping(client, conn):
    add_post(conn)
    add_deal(conn, start_time="17:00", end_time="21:00")
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date()
    assert "On now" not in text(client.get(f"/?day={tomorrow.isoformat()}&sort=time"))


def test_non_today_time_sort_normalizes_to_price(client, conn):
    contexts = []

    def record(sender, template, context, **extra):
        contexts.append(context)

    with template_rendered.connected_to(record, client.application):
        response = client.get("/?day=2099-09-16&sort=time")
    assert response.status_code == 200
    assert contexts[-1]["sort"] == "price"


def test_api_for_another_day_never_marks_deals_on_now(client, conn):
    add_post(conn)
    add_deal(conn, start_time="00:00", end_time="23:59")
    payload = client.get("/api/deals.json?day=2099-09-16").get_json()
    assert payload["deals"][0]["on_now"] is False
    assert payload["deals"][0]["timing"]["status"] == "anytime"


def test_time_is_the_default_sort_today(client, conn):
    add_post(conn)
    add_deal(conn, start_time=None, end_time=None)
    assert "Anytime today" in text(client.get("/"))


# --------------------------------------------------------------------------- #
# filters
# --------------------------------------------------------------------------- #


def test_price_filter(client, conn):
    add_post(conn)
    add_deal(conn, deal_text="cheap beer", price=2.0)
    add_deal(conn, deal_text="pricey beer", price=9.0)
    body = text(client.get("/?max=3"))
    assert "cheap beer" in body
    assert "pricey beer" not in body


def test_price_filter_keeps_unpriced_deals(client, conn):
    """"half off wings" may be the best deal there - do not silently drop it."""
    add_post(conn)
    add_deal(conn, deal_text="half off wings", price=None)
    assert "half off wings" in text(client.get("/?max=2"))


def test_category_filter(client, conn):
    add_post(conn)
    add_deal(conn, deal_text="$2 drafts", item="drafts", price=2.0)
    add_deal(conn, deal_text="$1 wings", item="wings", price=1.0)
    body = text(client.get("/?cat=beer"))
    assert "$2 drafts" in body
    assert "$1 wings" not in body


def test_search_matches_text_and_venue(client, conn):
    add_post(conn)
    add_deal(conn, deal_text="$1 wings", item="wings", price=1.0)
    add_deal(conn, deal_text="$2 drafts", item="drafts", price=2.0)
    assert "$1 wings" in text(client.get("/?q=wings"))
    assert "$2 drafts" not in text(client.get("/?q=wings"))
    assert "$1 wings" in text(client.get("/?q=murphy"))


def test_filters_combine(client, conn):
    add_post(conn)
    add_deal(conn, deal_text="cheap wings", item="wings", price=1.0)
    add_deal(conn, deal_text="pricey wings", item="wings", price=20.0)
    add_deal(conn, deal_text="cheap beer", item="drafts", price=1.0)
    body = text(client.get("/?cat=food&max=5"))
    assert "cheap wings" in body
    assert "pricey wings" not in body
    assert "cheap beer" not in body


def test_bad_filter_values_are_ignored(client, conn):
    add_post(conn)
    add_deal(conn)
    assert client.get("/?max=notanumber&cat=nonsense").status_code == 200


@pytest.mark.parametrize("cap", ["NaN", "inf", "-inf", "-2", "1e999"])
def test_invalid_price_caps_are_ignored_in_html_and_api(client, conn, cap):
    add_post(conn)
    add_deal(conn, deal_text="valid priced special", price=2)
    response = client.get("/", query_string={"max": cap})
    assert response.status_code == 200
    assert "valid priced special" in text(response)
    payload = client.get("/api/deals.json", query_string={"max": cap}).get_json()
    assert [d["price"] for d in payload["deals"]] == [2]


def test_zero_price_cap_is_an_active_filter(client, conn):
    add_post(conn)
    add_deal(conn, deal_text="paid special", price=2)
    body = text(client.get("/?max=0"))
    assert "Nothing matches those filters" in body
    assert "Clear filters" in body
    assert "paid special" not in body


def test_empty_filter_result_explains_itself(client, conn):
    add_post(conn)
    add_deal(conn, deal_text="$9 thing", price=9.0)
    body = text(client.get("/?max=2"))
    assert "Nothing matches those filters" in body
    assert "Clear filters" in body


def test_header_counts_are_honest_under_filters(client, conn):
    """The header must not imply a quiet night when the user narrowed the view."""
    add_post(conn)
    add_deal(conn, deal_text="a", price=1.0)
    add_deal(conn, deal_text="b", price=50.0)
    assert "of 2 deals" in text(client.get("/?max=5"))


def test_json_api_honours_filters(client, conn):
    add_post(conn)
    add_deal(conn, deal_text="cheap", price=1.0)
    add_deal(conn, deal_text="pricey", price=99.0)
    payload = client.get("/api/deals.json?max=5").get_json()
    assert [d["deal_text"] for d in payload["deals"]] == ["cheap"]


def test_filter_links_preserve_other_filters(client, conn):
    add_post(conn)
    add_deal(conn)
    body = text(client.get("/?cat=beer&max=3"))
    assert "max=3" in body


def test_quiet_venue_list_is_hidden_while_filtering(client, conn):
    """A filter hiding specials must not imply a bar has none on record."""
    add_post(conn)
    add_deal(conn, price=1.0)
    assert 'href="/venue/ladder_19"' in text(client.get("/"))
    assert 'href="/venue/ladder_19"' not in text(client.get("/?cat=wine"))


# --------------------------------------------------------------------------- #
# week view
# --------------------------------------------------------------------------- #


def test_week_page_renders_seven_days(client, conn):
    add_post(conn)
    add_deal(conn, recurrence="daily")
    body = text(client.get("/week"))
    assert "This week" in body
    assert body.count('class="weekday') >= 7


def test_week_page_honours_filters(client, conn):
    add_post(conn)
    add_deal(conn, deal_text="cheap thing", price=1.0, recurrence="daily")
    add_deal(conn, deal_text="pricey thing", price=80.0, recurrence="daily")
    body = text(client.get("/week?max=5"))
    assert "cheap thing" in body
    assert "pricey thing" not in body


def test_week_page_places_a_weekly_deal_on_one_day_only(client, conn):
    add_post(conn)
    add_deal(conn, deal_text="thursday only", recurrence="weekly", day_of_week=3)
    body = text(client.get("/week?start=2026-09-14"))
    # Accessible source-link labels repeat the offer in attributes, not in
    # the visible schedule. Count the actual schedule rows.
    rows = BeautifulSoup(body, "html.parser").select(".weekday-deals li")
    assert sum("thursday only" in row.get_text() for row in rows) == 1


def test_week_page_with_no_data(client):
    assert client.get("/week").status_code == 200


def test_week_page_is_linked_from_the_day_view(client, conn):
    add_post(conn)
    add_deal(conn)
    assert "/week" in text(client.get("/"))


def test_day_to_week_link_preserves_filters_and_selected_date(client):
    body = text(client.get("/?day=2099-09-16&max=3&cat=beer&q=drafts&sort=bar&unused=value"))
    links = [urlsplit(a["href"]) for a in BeautifulSoup(body, "html.parser").find_all("a", href=True)]
    week_links = [link for link in links if link.path == "/week"]
    assert week_links
    assert any(parse_qs(link.query) == {
        "max": ["3"], "cat": ["beer"], "q": ["drafts"], "sort": ["bar"],
        "start": ["2099-09-16"],
    } for link in week_links)


def test_week_day_links_preserve_filters_without_old_start(client):
    body = text(client.get("/week?start=2099-09-16&max=3&cat=beer&q=drafts&sort=bar&unused=value"))
    links = [urlsplit(a["href"]) for a in BeautifulSoup(body, "html.parser").find_all("a", href=True)]
    day_links = [parse_qs(link.query) for link in links if link.path == "/"]
    assert any(params == {
        "max": ["3"], "cat": ["beer"], "q": ["drafts"], "sort": ["bar"],
        "day": ["2099-09-16"],
    } for params in day_links)


@pytest.mark.parametrize("path", ["/", "/week", "/venue/murphyspubcincy"])
@pytest.mark.parametrize("sample", [False, True])
def test_sample_data_flag_matches_visible_sources(client, conn, path, sample):
    shortcode = "DEMO_SAMPLE" if sample else "REALPOST"
    add_post(conn, shortcode=shortcode)
    add_deal(conn, shortcode=shortcode)
    contexts = []

    def record(sender, template, context, **extra):
        contexts.append(context)

    with template_rendered.connected_to(record, client.application):
        response = client.get(path)
    assert response.status_code == 200
    assert contexts[-1]["has_samples"] is sample
    assert ("Sample deals included." in text(response)) is sample


# --------------------------------------------------------------------------- #
# addresses
# --------------------------------------------------------------------------- #


def test_known_address_is_shown_on_the_card(client, conn):
    add_post(conn, username="murphyspubcincy")
    add_deal(conn)
    assert "2329 W Clifton Ave" in text(client.get("/"))


def test_venue_page_links_the_address_to_a_map(client, conn):
    add_post(conn, username="murphyspubcincy")
    add_deal(conn)
    body = text(client.get("/venue/murphyspubcincy"))
    assert "google.com/maps" in body
    assert "2329" in body


def test_venue_without_an_address_still_renders(client, conn):
    add_post(conn, "BBB", username="topcatscincy")
    add_deal(conn, "BBB", venue_slug="topcatscincy", venue_name="Top Cat's")
    assert client.get("/venue/topcatscincy").status_code == 200


def test_config_addresses_are_never_fabricated():
    """Only verified addresses belong in config; the rest stay blank."""
    config = load_config()
    with_address = [v for v in config.venues if v.address]
    assert {v.slug for v in with_address} >= {
        "murphyspubcincy", "ladder_19", "unclewoodys_uc",
        "macspizzaclifton", "elvaqueroclifton",
        "divebarcincinnati", "cliftontavern513", "martinosonvine",
        "topcatscincy", "longbarcincy",
    }
    assert all(v.latitude is not None and v.longitude is not None and v.location_source
               for v in with_address)
    assert len(with_address) >= 23
    assert config.venue_by_slug("scottysonvine").latitude is None


# --------------------------------------------------------------------------- #
# browser-blocked ports
# --------------------------------------------------------------------------- #


def test_sip_ports_are_reported_as_unsafe():
    """5060/5061 are the ones most likely to be chosen by accident."""
    from bardeals.web import port_is_browser_safe

    assert not port_is_browser_safe(5060)
    assert not port_is_browser_safe(5061)


def test_common_dev_ports_are_safe():
    from bardeals.web import port_is_browser_safe

    for port in (8080, 8000, 5000, 3000, 5055, 8888):
        assert port_is_browser_safe(port), port


def test_well_known_blocked_ports():
    from bardeals.web import port_is_browser_safe

    for port in (22, 25, 110, 143, 6000):
        assert not port_is_browser_safe(port), port


def test_serve_refuses_a_blocked_port():
    """Serving there works, but the browser would show a bare ERR_UNSAFE_PORT."""
    from click.testing import CliRunner

    from bardeals.cli import cli

    result = CliRunner().invoke(cli, ["serve", "--port", "5061"])
    assert result.exit_code == 2
    assert "blocked by web browsers" in result.output
