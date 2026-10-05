#!/usr/bin/env python3
"""Populate the database with realistic DEMO deals anchored to the current week.

Why this exists: anonymous Instagram scraping is currently blocked (HTTP 429), so a
fresh checkout has an empty database and the web UI has nothing to show. This writes a
plausible week of Clifton-area bar specials so the UI can be seen working end to end.

Everything written here is clearly marked: posts get shortcodes prefixed ``DEMO`` and
permalinks pointing at a non-routable ``example.invalid`` host, so demo rows can never
be mistaken for real scraped data and are trivial to delete:

    python seed_demo.py --clear

Run ``bardeals scrape`` for real data once fetching works again.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from bardeals import db  # noqa: E402
from bardeals.config import get_config, setup_logging  # noqa: E402
from bardeals.db import Deal  # noqa: E402
from bardeals.scrape import RawPost  # noqa: E402

DEMO_PREFIX = "DEMO"

# (venue_slug, deal_text, item, price, day_of_week, start, end, confidence, source)
# day_of_week: 0=Mon .. 6=Sun, None = every day.
WEEKLY: list[tuple] = [
    # Murphy's Pub - the classic UC dive.
    ("murphyspubcincy", "$2 domestic drafts", "domestic drafts", 2.0, 0, None, None, "high", "image"),
    ("murphyspubcincy", "$3 wells", "wells", 3.0, 1, "20:00", None, "high", "caption"),
    ("murphyspubcincy", "half off wings", "wings", None, 3, "17:00", "21:00", "high", "caption"),
    ("murphyspubcincy", "$6 domestic pitchers", "pitchers", 6.0, 4, None, None, "high", "image"),

    # Ladder 19 - OTR, flyer-heavy poster.
    ("ladder_19", "$3 craft drafts", "craft drafts", 3.0, 0, None, None, "high", "image"),
    ("ladder_19", "$2 tacos", "tacos", 2.0, 1, "17:00", "22:00", "high", "image"),
    ("ladder_19", "half off bottles of wine", "wine", None, 2, None, None, "high", "image"),
    ("ladder_19", "$1 wings", "wings", 1.0, 3, "17:00", "21:00", "high", "image"),
    ("ladder_19", "$4 well drinks", "wells", 4.0, 4, None, None, "high", "image"),
    ("ladder_19", "$12 domestic buckets", "domestic buckets", 12.0, 5, None, None, "high", "image"),
    ("ladder_19", "$5 bloody marys", "bloody marys", 5.0, 6, None, "14:00", "high", "image"),

    # Scotty's on Vine.
    ("scottysonvine", "$2.50 tallboys", "tallboys", 2.5, 1, None, None, "high", "caption"),
    ("scottysonvine", "$5 burger + fries", "burger", 5.0, 2, "17:00", "22:00", "high", "image"),
    ("scottysonvine", "$3 shot specials", "shots", 3.0, 4, "21:00", None, "medium", "caption"),

    # Uncle Woody's - the UC institution.
    ("unclewoodys_uc", "$1 sliders", "sliders", 1.0, 0, "21:00", None, "high", "image"),
    ("unclewoodys_uc", "$2 you-call-its", "well drinks", 2.0, 2, "22:00", None, "high", "caption"),
    ("unclewoodys_uc", "$8 domestic buckets", "domestic buckets", 8.0, 3, None, None, "high", "image"),
    ("unclewoodys_uc", "$3 Long Islands", "long islands", 3.0, 5, None, None, "medium", "caption"),

    # Dive Bar.
    ("divebarcincinnati", "$2 PBR tallboys", "PBR", 2.0, None, None, None, "high", "image"),
    ("divebarcincinnati", "half off appetizers", "appetizers", None, 1, "16:00", "19:00", "high", "caption"),
    ("divebarcincinnati", "$4 whiskey shots", "whiskey", 4.0, 4, None, None, "medium", "caption"),

    # Clifton Heights Tavern.
    ("cliftontavern513", "$3 drafts", "drafts", 3.0, 2, None, None, "high", "caption"),
    ("cliftontavern513", "$5 apps all night", "appetizers", 5.0, 3, "20:00", None, "high", "image"),
    ("cliftontavern513", "$10 buckets", "buckets", 10.0, 5, None, None, "medium", "image"),

    # Mac's Pizza Pub.
    ("macspizzaclifton", "$2 pizza slices", "pizza slice", 2.0, 0, None, None, "high", "image"),
    ("macspizzaclifton", "$1 off all drafts", "drafts", None, 2, "16:00", "20:00", "high", "caption"),
    ("macspizzaclifton", "$7 large cheese pizza", "pizza", 7.0, 6, None, None, "high", "image"),

    # Martino's on Vine.
    ("martinosonvine", "$3 Italian drafts", "drafts", 3.0, 3, None, None, "medium", "caption"),
    ("martinosonvine", "half off pasta", "pasta", None, 1, "17:00", "21:00", "high", "image"),

    # Top Cat's.
    ("topcatscincy", "$2 well drinks before 11", "wells", 2.0, 4, None, "23:00", "high", "image"),
    ("topcatscincy", "no cover + $3 drafts", "drafts", 3.0, 3, "21:00", None, "medium", "caption"),

    # Long Bar.
    ("longbarcincy", "$2 domestic bottles", "domestic bottles", 2.0, 1, None, None, "high", "caption"),
    ("longbarcincy", "$5 pitchers", "pitchers", 5.0, 2, "20:00", None, "high", "image"),

    # El Vaquero.
    ("elvaqueroclifton", "$5 house margaritas", "margaritas", 5.0, 1, None, None, "high", "image"),
    ("elvaqueroclifton", "$2 street tacos", "tacos", 2.0, 2, "16:00", "21:00", "high", "image"),
    ("elvaqueroclifton", "$8 margarita pitchers", "margaritas", 8.0, 4, None, None, "high", "image"),
]

# One-off deals, as (venue_slug, deal_text, item, price, days_from_today, confidence).
ONE_OFFS: list[tuple] = [
    ("murphyspubcincy", "$2 domestics for the game", "domestics", 2.0, 0, "high"),
    ("topcatscincy", "free cover with student ID", "cover", None, 1, "medium"),
    ("divebarcincinnati", "$3 Guinness for St. Practice Day", "Guinness", 3.0, 2, "high"),
]

# Vague posts -> low confidence, undated. These exercise the dimmed section.
VAGUE: list[tuple] = [
    ("scottysonvine", "same deal as always this week", 1),
    ("longbarcincy", "usual specials running tonight", 2),
]


def _post(shortcode: str, username: str, caption: str, posted_at: datetime) -> RawPost:
    """Build a demo RawPost with an obviously-fake permalink."""
    return RawPost(
        shortcode=shortcode,
        username=username,
        caption=caption,
        posted_at=posted_at,
        image_url=None,
        local_image_path=None,
        # example.invalid is reserved and non-routable: demo rows can never masquerade
        # as real Instagram links.
        permalink=f"https://example.invalid/demo/{shortcode}",
        is_video=False,
        typename="GraphImage",
    )


def clear(conn) -> int:
    """Delete every demo post (and, by cascade, its deals).

    Returns:
        Number of demo posts removed.
    """
    cursor = conn.execute(
        "DELETE FROM posts WHERE shortcode LIKE ?", (f"{DEMO_PREFIX}%",)
    )
    conn.execute("DELETE FROM parse_log WHERE shortcode LIKE ?", (f"{DEMO_PREFIX}%",))
    conn.commit()
    return cursor.rowcount


def seed(conn, config, today: date) -> tuple[int, int]:
    """Write the demo week into the database.

    Args:
        conn: Open connection.
        config: Loaded config, used for venue display names.
        today: The date the demo week is anchored to.

    Returns:
        ``(posts_written, deals_written)``.
    """
    posts = deals = 0
    midday = time(18, 30)

    # One "flyer" post per venue carrying that venue's weekly grid.
    by_venue: dict[str, list[tuple]] = {}
    for row in WEEKLY:
        by_venue.setdefault(row[0], []).append(row)

    for index, (slug, rows) in enumerate(sorted(by_venue.items())):
        shortcode = f"{DEMO_PREFIX}WEEK{index:02d}"
        posted = datetime.combine(today - timedelta(days=3), midday, tzinfo=timezone.utc)
        conn_post = _post(shortcode, slug, "Weekly specials \U0001f4c5", posted)
        if db.insert_post(conn, conn_post):
            posts += 1
        db.mark_parsed(conn, shortcode)

        for _slug, text, item, price, dow, start, end, confidence, source in rows:
            deal = Deal(
                shortcode=shortcode,
                venue_slug=slug,
                venue_name=config.venue_name(slug),
                deal_text=text,
                item=item,
                price=price,
                day_of_week=dow,
                specific_date=None,
                start_time=start,
                end_time=end,
                recurrence="weekly" if dow is not None else "daily",
                confidence=confidence,
                source=source,
            )
            if db.insert_deal(conn, deal) is not None:
                deals += 1

    for index, (slug, text, item, price, offset, confidence) in enumerate(ONE_OFFS):
        shortcode = f"{DEMO_PREFIX}ONE{index:02d}"
        posted = datetime.combine(today, midday, tzinfo=timezone.utc)
        if db.insert_post(conn, _post(shortcode, slug, text, posted)):
            posts += 1
        db.mark_parsed(conn, shortcode)
        deal = Deal(
            shortcode=shortcode,
            venue_slug=slug,
            venue_name=config.venue_name(slug),
            deal_text=text,
            item=item,
            price=price,
            day_of_week=None,
            specific_date=(today + timedelta(days=offset)).isoformat(),
            start_time=None,
            end_time=None,
            recurrence="once",
            confidence=confidence,
            source="caption",
        )
        if db.insert_deal(conn, deal) is not None:
            deals += 1

    for index, (slug, text, days_ago) in enumerate(VAGUE):
        shortcode = f"{DEMO_PREFIX}VAGUE{index:02d}"
        posted = datetime.combine(today - timedelta(days=days_ago), midday, tzinfo=timezone.utc)
        if db.insert_post(conn, _post(shortcode, slug, text, posted)):
            posts += 1
        db.mark_parsed(conn, shortcode)
        deal = Deal(
            shortcode=shortcode,
            venue_slug=slug,
            venue_name=config.venue_name(slug),
            deal_text=text,
            item=None,
            price=None,
            day_of_week=None,
            specific_date=None,
            start_time=None,
            end_time=None,
            recurrence="unknown",
            confidence="low",
            source="caption",
        )
        if db.insert_deal(conn, deal) is not None:
            deals += 1

    return posts, deals


def main(argv: list[str] | None = None) -> int:
    """Seed or clear the demo dataset."""
    parser = argparse.ArgumentParser(description="Seed realistic demo deals for the web UI.")
    parser.add_argument("--clear", action="store_true", help="Remove demo rows and exit.")
    parser.add_argument("--date", default=None, help="Anchor date (YYYY-MM-DD); defaults to today.")
    args = parser.parse_args(argv)

    config = get_config()
    setup_logging(config)
    conn = db.connect(config.db_path)
    db.init_db(conn)

    if args.clear:
        removed = clear(conn)
        print(f"removed {removed} demo post(s)")
        conn.close()
        return 0

    today = date.fromisoformat(args.date) if args.date else datetime.now().astimezone().date()
    posts, deals = seed(conn, config, today)
    print(f"seeded {posts} demo post(s) and {deals} demo deal(s), anchored to {today}")
    print(f"database now holds {db.count_posts(conn)} post(s), {db.count_deals(conn)} deal(s)")
    print("remove them any time with: python seed_demo.py --clear")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
