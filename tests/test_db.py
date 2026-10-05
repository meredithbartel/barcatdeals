"""Tests for the storage layer, concentrating on deduplication."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from bardeals import db
from bardeals.db import Deal
from bardeals.scrape import RawPost


@pytest.fixture
def conn(tmp_path):
    """A migrated, empty database."""
    connection = db.connect(tmp_path / "test.db")
    db.init_db(connection)
    yield connection
    connection.close()


def make_post(shortcode: str, username: str = "murphyspubcincy", days_ago: int = 0) -> RawPost:
    """Build a RawPost for insertion."""
    return RawPost(
        shortcode=shortcode,
        username=username,
        caption=f"caption for {shortcode}",
        posted_at=datetime(2026, 9, 16, 22, 0, tzinfo=timezone.utc) - timedelta(days=days_ago),
        image_url=None,
        local_image_path=None,
        permalink=f"https://www.instagram.com/p/{shortcode}/",
        is_video=False,
        typename="GraphImage",
    )


def make_deal(shortcode: str, **overrides) -> Deal:
    """Build a Deal with sensible defaults."""
    values = {
        "shortcode": shortcode,
        "venue_slug": "murphyspubcincy",
        "venue_name": "Murphy's Pub",
        "deal_text": "$2 domestic drafts",
        "item": "domestic drafts",
        "price": 2.0,
        "recurrence": "unknown",
        "confidence": "high",
        "source": "caption",
    }
    values.update(overrides)
    return Deal(**values)


# --------------------------------------------------------------------------- #
# schema
# --------------------------------------------------------------------------- #


def test_init_db_creates_tables(conn):
    tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {"posts", "deals", "parse_log"} <= tables


def test_init_db_is_idempotent(conn):
    before = db.schema_version(conn)
    db.init_db(conn)
    db.init_db(conn)
    assert db.schema_version(conn) == before == db.MIGRATIONS[-1][0]


def test_foreign_key_is_enforced(conn):
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        db.insert_deal(conn, make_deal("NOSUCHPOST"))


# --------------------------------------------------------------------------- #
# posts
# --------------------------------------------------------------------------- #


def test_insert_post_then_duplicate(conn):
    assert db.insert_post(conn, make_post("AAA")) is True
    assert db.insert_post(conn, make_post("AAA")) is False
    assert db.count_posts(conn) == 1


def test_known_shortcodes_and_exists(conn):
    db.insert_post(conn, make_post("AAA"))
    db.insert_post(conn, make_post("BBB"))
    assert db.known_shortcodes(conn) == {"AAA", "BBB"}
    assert db.post_exists(conn, "AAA")
    assert not db.post_exists(conn, "CCC")


def test_mark_parsed_moves_post_out_of_the_queue(conn):
    db.insert_post(conn, make_post("AAA"))
    assert [row["shortcode"] for row in db.unparsed_posts(conn)] == ["AAA"]
    db.mark_parsed(conn, "AAA")
    assert db.unparsed_posts(conn) == []
    assert db.get_post(conn, "AAA")["parsed"] == 1


def test_deleting_a_post_cascades_to_its_deals(conn):
    db.insert_post(conn, make_post("AAA"))
    db.insert_deal(conn, make_deal("AAA"))
    conn.execute("DELETE FROM posts WHERE shortcode = 'AAA'")
    conn.commit()
    assert db.count_deals(conn) == 0


# --------------------------------------------------------------------------- #
# hard dedupe (same post)
# --------------------------------------------------------------------------- #


def test_same_deal_twice_from_same_post_is_rejected(conn):
    db.insert_post(conn, make_post("AAA"))
    assert db.insert_deal(conn, make_deal("AAA", specific_date="2026-09-17")) is not None
    assert db.insert_deal(conn, make_deal("AAA", specific_date="2026-09-17")) is None
    assert db.count_deals(conn) == 1


def test_hard_dedupe_survives_null_dates(conn):
    """The COALESCE case: SQLite treats NULLs in a UNIQUE index as distinct."""
    db.insert_post(conn, make_post("AAA"))
    assert db.insert_deal(conn, make_deal("AAA")) is not None
    assert db.insert_deal(conn, make_deal("AAA")) is None
    assert db.count_deals(conn) == 1


def test_hard_dedupe_ignores_case_and_spacing(conn):
    db.insert_post(conn, make_post("AAA"))
    db.insert_deal(conn, make_deal("AAA", deal_text="$2 domestic drafts"))
    assert db.insert_deal(conn, make_deal("AAA", deal_text="$2  Domestic   Drafts!")) is None
    assert db.count_deals(conn) == 1


def test_different_deals_from_one_post_both_stored(conn):
    """A flyer with a weekly grid emits one row per day; all must land."""
    db.insert_post(conn, make_post("FLYER"))
    deals = [
        make_deal("FLYER", deal_text=f"day {dow} special", day_of_week=dow, recurrence="weekly")
        for dow in range(7)
    ]
    assert db.insert_deals(conn, deals) == 7
    assert db.count_deals(conn) == 7


def test_same_text_different_day_is_not_a_duplicate(conn):
    db.insert_post(conn, make_post("AAA"))
    db.insert_deal(conn, make_deal("AAA", day_of_week=0, recurrence="weekly"))
    assert db.insert_deal(conn, make_deal("AAA", day_of_week=4, recurrence="weekly")) is not None
    assert db.count_deals(conn) == 2


def test_reparsing_a_post_is_idempotent(conn):
    db.insert_post(conn, make_post("AAA"))
    db.insert_deals(conn, [make_deal("AAA"), make_deal("AAA", deal_text="$5 pitchers")])
    db.insert_deals(conn, [make_deal("AAA"), make_deal("AAA", deal_text="$5 pitchers")])
    assert db.count_deals(conn) == 2

    assert db.delete_deals_for_post(conn, "AAA") == 2
    assert db.count_deals(conn) == 0


def test_bad_enum_values_are_coerced_not_stored(conn):
    db.insert_post(conn, make_post("AAA"))
    deal = make_deal("AAA", recurrence="every other tuesday", confidence="certain", source="vibes")
    db.insert_deal(conn, deal)
    row = conn.execute("SELECT * FROM deals").fetchone()
    assert row["recurrence"] == "unknown"
    assert row["confidence"] == "low"
    assert row["source"] == "caption"


def test_out_of_range_day_of_week_is_nulled(conn):
    db.insert_post(conn, make_post("AAA"))
    db.insert_deal(conn, make_deal("AAA", day_of_week=9))
    assert conn.execute("SELECT day_of_week FROM deals").fetchone()[0] is None


# --------------------------------------------------------------------------- #
# active-on queries
# --------------------------------------------------------------------------- #


def test_specific_date_is_active_only_on_that_day(conn):
    db.insert_post(conn, make_post("AAA"))
    db.insert_deal(conn, make_deal("AAA", specific_date="2026-09-17", recurrence="once"))
    assert len(db.deals_active_on(conn, date(2026, 9, 17))) == 1
    assert db.deals_active_on(conn, date(2026, 9, 18)) == []


def test_weekly_deal_is_active_on_its_weekday(conn):
    db.insert_post(conn, make_post("AAA"))
    # 2026-09-17 is a Thursday (weekday 3).
    db.insert_deal(conn, make_deal("AAA", day_of_week=3, recurrence="weekly"))
    assert date(2026, 9, 17).weekday() == 3
    assert len(db.deals_active_on(conn, date(2026, 9, 17))) == 1
    assert len(db.deals_active_on(conn, date(2026, 9, 24))) == 1
    assert db.deals_active_on(conn, date(2026, 9, 18)) == []


def test_daily_deal_is_active_every_day(conn):
    db.insert_post(conn, make_post("AAA"))
    db.insert_deal(conn, make_deal("AAA", recurrence="daily"))
    for offset in range(4):
        assert len(db.deals_active_on(conn, date(2026, 9, 17) + timedelta(days=offset))) == 1


def test_week_view_spans_seven_days(conn):
    db.insert_post(conn, make_post("AAA"))
    db.insert_deal(conn, make_deal("AAA", day_of_week=3, recurrence="weekly"))
    week = db.deals_for_week(conn, date(2026, 9, 14))
    assert len(week) == 7
    assert [day for day, rows in week if rows] == [date(2026, 9, 17)]


def test_deals_for_venue_scopes_to_one_bar(conn):
    db.insert_post(conn, make_post("AAA", username="murphyspubcincy"))
    db.insert_post(conn, make_post("BBB", username="ladder_19"))
    db.insert_deal(conn, make_deal("AAA"))
    db.insert_deal(conn, make_deal("BBB", venue_slug="ladder_19", venue_name="Ladder 19"))
    assert len(db.deals_for_venue(conn, "murphyspubcincy")) == 1
    assert len(db.deals_for_venue(conn, "ladder_19")) == 1


def test_active_query_joins_the_permalink(conn):
    """Every rendered deal needs a clickable source."""
    db.insert_post(conn, make_post("AAA"))
    db.insert_deal(conn, make_deal("AAA", recurrence="daily"))
    row = db.deals_active_on(conn, date(2026, 9, 17))[0]
    assert row["permalink"] == "https://www.instagram.com/p/AAA/"


# --------------------------------------------------------------------------- #
# soft dedupe (across posts)
# --------------------------------------------------------------------------- #


def test_same_weekly_deal_posted_three_times_is_one_group(conn):
    """The headline case: a bar reposts its Thursday special every week."""
    for index, shortcode in enumerate(["W1", "W2", "W3"]):
        db.insert_post(conn, make_post(shortcode, days_ago=index * 7))
        db.insert_deal(conn, make_deal(shortcode, day_of_week=3, recurrence="weekly"))

    groups = db.find_soft_duplicates(conn)
    assert len(groups) == 1
    assert len(groups[0].rows) == 3
    assert groups[0].canonical["shortcode"] == "W1"  # newest post
    assert len(groups[0].duplicate_ids) == 2


def test_soft_dedupe_collapses_the_rendered_rows(conn):
    for index, shortcode in enumerate(["W1", "W2", "W3"]):
        db.insert_post(conn, make_post(shortcode, days_ago=index * 7))
        db.insert_deal(conn, make_deal(shortcode, day_of_week=3, recurrence="weekly"))

    rows = db.deals_active_on(conn, date(2026, 9, 17))
    assert len(rows) == 3
    collapsed = db.dedupe_rows(rows)
    assert len(collapsed) == 1
    assert collapsed[0]["shortcode"] == "W1"


def test_same_text_on_different_weekdays_stays_separate(conn):
    db.insert_post(conn, make_post("A1", days_ago=0))
    db.insert_post(conn, make_post("A2", days_ago=1))
    db.insert_deal(conn, make_deal("A1", day_of_week=0, recurrence="weekly"))
    db.insert_deal(conn, make_deal("A2", day_of_week=4, recurrence="weekly"))
    assert db.find_soft_duplicates(conn) == []


def test_different_venues_are_never_duplicates(conn):
    db.insert_post(conn, make_post("A1", username="murphyspubcincy"))
    db.insert_post(conn, make_post("B1", username="ladder_19"))
    db.insert_deal(conn, make_deal("A1", recurrence="daily"))
    db.insert_deal(conn, make_deal("B1", venue_slug="ladder_19", venue_name="Ladder 19",
                                   recurrence="daily"))
    assert db.find_soft_duplicates(conn) == []


def test_weekly_deal_absorbs_the_tonight_repost(conn):
    """"$2 domestics tonight!" on a Thursday is the weekly special, reposted."""
    db.insert_post(conn, make_post("WEEKLY", days_ago=7))
    db.insert_post(conn, make_post("TONIGHT", days_ago=0))
    db.insert_deal(conn, make_deal("WEEKLY", day_of_week=3, recurrence="weekly"))
    db.insert_deal(conn, make_deal("TONIGHT", specific_date="2026-09-17", recurrence="once"))

    groups = db.find_soft_duplicates(conn)
    assert len(groups) == 1
    assert {row["shortcode"] for row in groups[0].rows} == {"WEEKLY", "TONIGHT"}


def test_one_off_deals_on_different_dates_stay_separate(conn):
    db.insert_post(conn, make_post("D1", days_ago=0))
    db.insert_post(conn, make_post("D2", days_ago=1))
    db.insert_deal(conn, make_deal("D1", specific_date="2026-09-17", recurrence="once"))
    db.insert_deal(conn, make_deal("D2", specific_date="2026-09-24", recurrence="once"))
    assert db.find_soft_duplicates(conn) == []


def test_distinct_deals_are_left_alone(conn):
    db.insert_post(conn, make_post("A1"))
    db.insert_post(conn, make_post("A2", days_ago=1))
    db.insert_deal(conn, make_deal("A1", deal_text="$2 domestic drafts", recurrence="daily"))
    db.insert_deal(conn, make_deal("A2", deal_text="$5 pitchers", recurrence="daily"))
    assert db.find_soft_duplicates(conn) == []
    assert len(db.dedupe_rows(db.deals_active_on(conn, date(2026, 9, 17)))) == 2


def test_dedupe_rows_on_empty_input(conn):
    assert db.dedupe_rows([]) == []


def test_normalize_text_examples():
    assert db.normalize_text("$2  Domestic  Drafts!") == "$2 domestic drafts"
    assert db.normalize_text("  Half Off Wings.  ") == "half off wings"
    assert db.normalize_text(None) == ""
