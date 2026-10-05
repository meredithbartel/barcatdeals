"""Tests for rendering.

Rendering is checked through a captured rich console rather than by eyeballing
formatting: the assertions are about what information reaches the reader -
every deal carries a permalink, low-confidence deals appear but dimmed and
separated, and repeats collapse.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from rich.console import Console

from bardeals import db, report
from bardeals.config import load_config
from bardeals.db import Deal
from bardeals.scrape import RawPost

THURSDAY = date(2026, 9, 17)


@pytest.fixture
def config():
    return load_config()


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "report.db")
    db.init_db(connection)
    yield connection
    connection.close()


def add_post(conn, shortcode, username="murphyspubcincy", days_ago=0):
    post = RawPost(
        shortcode=shortcode,
        username=username,
        caption="",
        posted_at=datetime(2026, 9, 16, 22, 0, tzinfo=timezone.utc) - timedelta(days=days_ago),
        image_url=None,
        local_image_path=None,
        permalink=f"https://www.instagram.com/p/{shortcode}/",
        is_video=False,
        typename="GraphImage",
    )
    db.insert_post(conn, post)
    return post


def add_deal(conn, shortcode, **overrides):
    values = {
        "shortcode": shortcode,
        "venue_slug": "murphyspubcincy",
        "venue_name": "Murphy's Pub",
        "deal_text": "$2 domestic drafts",
        "item": "domestic drafts",
        "price": 2.0,
        "recurrence": "weekly",
        "day_of_week": 3,
        "confidence": "high",
        "source": "caption",
    }
    values.update(overrides)
    db.insert_deal(conn, Deal(**values))


def capture(render, *args, width=200, **kwargs):
    """Render into a string instead of the terminal."""
    console = Console(width=width, record=True, force_terminal=False, no_color=True)
    render(*args, console=console, **kwargs)
    return console.export_text()


# --------------------------------------------------------------------------- #
# day view
# --------------------------------------------------------------------------- #


def test_day_view_shows_the_deal_and_its_permalink(conn, config):
    add_post(conn, "AAA")
    add_deal(conn, "AAA")
    output = capture(report.render_day, conn, config, THURSDAY)
    assert "$2 domestic drafts" in output
    assert "https://www.instagram.com/p/AAA/" in output
    assert "Murphy's Pub" in output


def test_every_rendered_deal_carries_a_link(conn, config):
    for index, shortcode in enumerate(["AAA", "BBB", "CCC"]):
        add_post(conn, shortcode, days_ago=index)
        add_deal(conn, shortcode, deal_text=f"deal {index}", recurrence="daily", day_of_week=None)
    output = capture(report.render_day, conn, config, THURSDAY)
    for shortcode in ["AAA", "BBB", "CCC"]:
        assert shortcode in output


def test_empty_day_says_so_without_crashing(conn, config):
    output = capture(report.render_day, conn, config, THURSDAY)
    assert "No deals on record" in output
    assert "no posts yet" in output


def test_stored_posts_but_no_deals_reports_that(conn, config):
    add_post(conn, "AAA")
    output = capture(report.render_day, conn, config, THURSDAY)
    assert "No deals on record" in output
    assert "1 post(s) stored" in output


def test_deals_are_grouped_by_bar(conn, config):
    add_post(conn, "AAA", username="murphyspubcincy")
    add_post(conn, "BBB", username="ladder_19", days_ago=1)
    add_deal(conn, "AAA")
    add_deal(conn, "BBB", venue_slug="ladder_19", venue_name="Ladder 19",
             deal_text="$1 wings")
    output = capture(report.render_day, conn, config, THURSDAY)
    assert "Murphy's Pub" in output
    assert "Ladder 19" in output


# --------------------------------------------------------------------------- #
# low confidence
# --------------------------------------------------------------------------- #


def test_low_confidence_deals_are_shown_not_hidden(conn, config):
    add_post(conn, "AAA")
    add_deal(conn, "AAA", deal_text="mystery special", confidence="low")
    output = capture(report.render_day, conn, config, THURSDAY)
    assert "mystery special" in output
    assert "unconfirmed" in output.lower()


def test_low_confidence_is_separated_from_confident_deals(conn, config):
    add_post(conn, "AAA")
    add_deal(conn, "AAA", deal_text="$2 drafts", confidence="high")
    add_deal(conn, "AAA", deal_text="mystery special", confidence="low")
    output = capture(report.render_day, conn, config, THURSDAY)
    assert output.index("$2 drafts") < output.index("unconfirmed")
    assert output.index("unconfirmed") < output.index("mystery special")


def test_undated_recent_deal_still_appears(conn, config):
    """"same deal as always" has no date but must not vanish."""
    add_post(conn, "AAA", days_ago=1)
    add_deal(conn, "AAA", deal_text="same deal as always this week",
             recurrence="unknown", day_of_week=None, price=None, confidence="low")
    output = capture(report.render_day, conn, config, THURSDAY)
    assert "same deal as always" in output
    assert "Undated" in output


def test_old_undated_deals_fall_out_of_the_window(conn, config):
    add_post(conn, "AAA", days_ago=60)
    add_deal(conn, "AAA", deal_text="ancient mystery",
             recurrence="unknown", day_of_week=None, confidence="low")
    assert db.undated_recent_deals(conn, THURSDAY) == []


def test_undated_query_ignores_scheduled_deals(conn, config):
    add_post(conn, "AAA")
    add_deal(conn, "AAA", recurrence="weekly", day_of_week=3)
    add_deal(conn, "AAA", deal_text="daily thing", recurrence="daily", day_of_week=None)
    assert db.undated_recent_deals(conn, THURSDAY) == []


# --------------------------------------------------------------------------- #
# dedupe at render time
# --------------------------------------------------------------------------- #


def test_a_deal_posted_three_times_is_rendered_once(conn, config):
    for index, shortcode in enumerate(["W1", "W2", "W3"]):
        add_post(conn, shortcode, days_ago=index * 7)
        add_deal(conn, shortcode)
    output = capture(report.render_day, conn, config, THURSDAY)
    assert output.count("$2 domestic drafts") == 1
    assert "1 deal(s)" in output


# --------------------------------------------------------------------------- #
# week view
# --------------------------------------------------------------------------- #


def test_week_view_covers_seven_days(conn, config):
    add_post(conn, "AAA")
    add_deal(conn, "AAA")
    output = capture(report.render_week, conn, config, date(2026, 9, 14))
    for label in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]:
        assert label in output


def test_week_view_places_a_weekly_deal_on_its_day_only(conn, config):
    add_post(conn, "AAA")
    add_deal(conn, "AAA", deal_text="thursday only", day_of_week=3, recurrence="weekly")
    output = capture(report.render_week, conn, config, date(2026, 9, 14))
    assert output.count("thursday only") == 1
    assert "nothing on record" in output


def test_week_view_spreads_a_flyer_grid_across_the_week(conn, config):
    add_post(conn, "FLYER", username="ladder_19")
    for dow in range(7):
        add_deal(conn, "FLYER", venue_slug="ladder_19", venue_name="Ladder 19",
                 deal_text=f"special {dow}", day_of_week=dow, recurrence="weekly",
                 source="image")
    output = capture(report.render_week, conn, config, date(2026, 9, 14))
    for dow in range(7):
        assert f"special {dow}" in output


# --------------------------------------------------------------------------- #
# venue view
# --------------------------------------------------------------------------- #


def test_venue_view_lists_everything_for_one_bar(conn, config):
    add_post(conn, "AAA")
    add_post(conn, "BBB", username="ladder_19", days_ago=1)
    add_deal(conn, "AAA", deal_text="murphys deal")
    add_deal(conn, "BBB", venue_slug="ladder_19", venue_name="Ladder 19",
             deal_text="ladder deal")
    output = capture(report.render_venue, conn, config, "murphyspubcincy")
    assert "murphys deal" in output
    assert "ladder deal" not in output


def test_venue_view_with_no_deals(conn, config):
    output = capture(report.render_venue, conn, config, "topcatscincy")
    assert "No deals on record" in output


# --------------------------------------------------------------------------- #
# formatting helpers
# --------------------------------------------------------------------------- #


def test_price_and_time_formatting(conn, config):
    add_post(conn, "AAA")
    add_deal(conn, "AAA", price=2.0, start_time="17:00", end_time="21:00")
    add_deal(conn, "AAA", deal_text="half off wings", price=None, start_time="20:00")
    output = capture(report.render_day, conn, config, THURSDAY)
    assert "$2" in output
    assert "17:00-21:00" in output
    assert "from 20:00" in output


def test_recurrence_is_described_in_words(conn, config):
    add_post(conn, "AAA")
    add_deal(conn, "AAA", recurrence="weekly", day_of_week=3)
    add_deal(conn, "AAA", deal_text="daily deal", recurrence="daily", day_of_week=None)
    output = capture(report.render_day, conn, config, THURSDAY)
    assert "every Thursday" in output
    assert "every day" in output


def test_source_is_labelled(conn, config):
    add_post(conn, "AAA")
    add_deal(conn, "AAA", source="image")
    assert "flyer" in capture(report.render_day, conn, config, THURSDAY)


# --------------------------------------------------------------------------- #
# markdown
# --------------------------------------------------------------------------- #


def test_markdown_report_has_a_heading_and_a_table(conn, config):
    add_post(conn, "AAA")
    add_deal(conn, "AAA")
    markdown = report.day_markdown(conn, config, THURSDAY)
    assert markdown.startswith("# Deals for Thursday, September 17, 2026")
    assert "## Murphy's Pub" in markdown
    assert "| Deal | Price | Time | When | Source | Link |" in markdown
    assert "[post](https://www.instagram.com/p/AAA/)" in markdown


def test_markdown_escapes_pipes_in_deal_text(conn, config):
    """A pipe in "$2 tacos | $5 margs" would otherwise break the table."""
    add_post(conn, "AAA")
    add_deal(conn, "AAA", deal_text="$2 tacos | $5 margs")
    markdown = report.day_markdown(conn, config, THURSDAY)
    assert "$2 tacos \\| $5 margs" in markdown


def test_markdown_report_when_empty(conn, config):
    markdown = report.day_markdown(conn, config, THURSDAY)
    assert "_No deals on record for this day._" in markdown


def test_markdown_hides_low_confidence_behind_details(conn, config):
    add_post(conn, "AAA")
    add_deal(conn, "AAA", deal_text="$2 drafts", confidence="high")
    add_deal(conn, "AAA", deal_text="mystery", confidence="low")
    markdown = report.day_markdown(conn, config, THURSDAY)
    assert "<details><summary>Unconfirmed</summary>" in markdown
    assert "mystery" in markdown
