"""Tests for the theme layer, type scale and live countdowns.

These guard decisions that are easy to regress silently: a stylesheet edit that
reintroduces 8px text, a template change that drops the theme bootstrap, or a
timing change that stops emitting the timestamps the countdown ticks against.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bardeals import db
from bardeals.config import load_config
from bardeals.db import Deal
from bardeals.scrape import RawPost
from bardeals.web import create_app, time_status

pytest.importorskip("flask")

STATIC = Path(__file__).resolve().parents[1] / "src" / "bardeals" / "static"
TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "bardeals" / "templates"

#: Nothing in the UI may render below this. 12px is the floor.
MIN_FONT_REM = 0.75

_FONT_SIZE = re.compile(r"font-size:\s*(\.\d+|\d+(?:\.\d+)?)rem")


@pytest.fixture
def config(tmp_path):
    return replace(load_config(), db_path=tmp_path / "theme.db")


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


def seed(conn, start_time=None, end_time=None, price=2.0):
    db.insert_post(
        conn,
        RawPost(
            shortcode="AAA",
            username="murphyspubcincy",
            caption="",
            posted_at=datetime.now(timezone.utc),
            image_url=None,
            local_image_path=None,
            permalink="https://www.instagram.com/p/AAA/",
            is_video=False,
            typename="GraphImage",
        ),
    )
    db.insert_deal(
        conn,
        Deal(
            shortcode="AAA",
            venue_slug="murphyspubcincy",
            venue_name="Murphy's Pub",
            deal_text="$2 domestic drafts",
            item="drafts",
            price=price,
            recurrence="daily",
            start_time=start_time,
            end_time=end_time,
            confidence="high",
            source="caption",
        ),
    )


# --------------------------------------------------------------------------- #
# the type floor
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("sheet", ["explorer.css", "style.css", "theme.css"])
def test_no_stylesheet_renders_text_below_12px(sheet):
    """124 rules once sat below 11.5px, the smallest at 7px. Never again."""
    text = (STATIC / sheet).read_text(encoding="utf-8")
    offenders = []
    for match in _FONT_SIZE.finditer(text):
        raw = match.group(1)
        value = float(raw if not raw.startswith(".") else "0" + raw)
        if value < MIN_FONT_REM:
            offenders.append(match.group(0))
    assert not offenders, f"{sheet} has sub-12px type: {offenders[:5]}"


def test_theme_defines_a_named_type_scale():
    text = (STATIC / "theme.css").read_text(encoding="utf-8")
    for token in ("--fs-meta", "--fs-caption", "--fs-small", "--fs-body", "--fs-price"):
        assert token in text, token


# --------------------------------------------------------------------------- #
# dark mode
# --------------------------------------------------------------------------- #


def test_dark_palette_is_defined_both_ways():
    """System preference AND an explicit choice must both work."""
    text = (STATIC / "theme.css").read_text(encoding="utf-8")
    assert "prefers-color-scheme: dark" in text
    assert 'html[data-theme="dark"]' in text
    assert 'html:not([data-theme="light"])' in text


def test_explicit_light_choice_can_override_system_dark():
    """A visitor who picks light must keep it on a dark-mode phone."""
    text = (STATIC / "theme.css").read_text(encoding="utf-8")
    dark_block = text[text.index("@media (prefers-color-scheme: dark)"):]
    assert 'html:not([data-theme="light"])' in dark_block


def test_theme_bootstrap_runs_before_paint():
    """Otherwise a dark-mode visitor gets a white flash on every page."""
    head = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert "bardeals-theme" in head
    bootstrap = head.index("bardeals-theme")
    body = head.index("<body")
    assert bootstrap < body, "theme bootstrap must be in <head>"


def test_theme_css_loads_after_the_sheets_it_overrides():
    head = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert head.index("style.css") < head.index("theme.css")
    assert head.index("head_extra") < head.index("theme.css")


def test_toggle_is_hidden_until_javascript_enables_it():
    """A control that cannot work must not be offered."""
    head = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    button = head[head.index('id="theme-toggle"'):head.index('id="theme-toggle"') + 200]
    assert "hidden" in button


def test_toggle_survives_blocked_local_storage():
    """Private mode throws on localStorage; the page must still switch."""
    script = (STATIC / "theme.js").read_text(encoding="utf-8")
    assert script.count("try {") >= 2
    assert "catch" in script


def test_map_has_a_dark_palette():
    """A white map beside dark chrome looks broken."""
    script = (STATIC / "map-theme.js").read_text(encoding="utf-8")
    assert "PALETTES" in script
    assert "dark:" in script
    # Every colour must come from the palette, not a literal.
    create = script[script.index("function create("):script.index("function groupVenues")]
    literals = re.findall(r"'#[0-9a-fA-F]{6}'", create)
    assert not literals, f"hardcoded colours left in map style: {literals[:5]}"


def test_map_follows_theme_changes():
    script = (STATIC / "map.js").read_text(encoding="utf-8")
    assert "bardeals:theme" in script
    assert "themeMode()" in script


# --------------------------------------------------------------------------- #
# the restored summary
# --------------------------------------------------------------------------- #


def test_head_stat_is_visible_again(client, conn):
    """explorer.css hid the most useful sentence on the page."""
    seed(conn)
    body = client.get("/").get_data(as_text=True)
    assert 'class="head-stat"' in body

    theme = (STATIC / "theme.css").read_text(encoding="utf-8")
    block = theme[theme.index(".map-page .head-stat {"):]
    assert "display: flex !important" in block[:200]


# --------------------------------------------------------------------------- #
# live countdowns
# --------------------------------------------------------------------------- #


def test_timing_carries_absolute_timestamps():
    """A pre-rendered "33m" rots; the browser needs a real instant."""
    now = datetime(2026, 9, 11, 20, 30, tzinfo=timezone.utc)
    ending = time_status({"start_time": "17:00", "end_time": "21:00"}, now, is_today=True)
    assert ending["ends_at"] == "2026-09-11T21:00:00+00:00"

    starting = time_status({"start_time": "22:00", "end_time": None}, now, is_today=True)
    assert starting["starts_at"] == "2026-09-11T22:00:00+00:00"


def test_untimed_deals_carry_no_timestamps():
    now = datetime(2026, 9, 11, 20, 30, tzinfo=timezone.utc)
    status = time_status({"start_time": None, "end_time": None}, now, is_today=True)
    assert status["ends_at"] is None and status["starts_at"] is None


def test_window_crossing_midnight_resolves_to_tomorrow():
    """A 9pm-2am window ends on the following calendar day."""
    now = datetime(2026, 9, 11, 23, 30, tzinfo=timezone.utc)
    status = time_status({"start_time": "21:00", "end_time": "02:00"}, now, is_today=True)
    assert status["status"] == "now"
    assert status["ends_at"].startswith("2026-09-12T02:00")


def test_countdown_attributes_reach_the_page(client, conn):
    now = datetime.now().astimezone()
    later = (now + timedelta(hours=2)).strftime("%H:%M")
    seed(conn, start_time=now.strftime("%H:%M"), end_time=later)
    body = client.get("/").get_data(as_text=True)
    assert "data-ends-at=" in body


def test_countdown_is_progressive_enhancement(client, conn):
    """With JS off the server label is still correct at render time."""
    now = datetime.now().astimezone()
    seed(conn, start_time=now.strftime("%H:%M"), end_time=(now + timedelta(hours=2)).strftime("%H:%M"))
    body = client.get("/").get_data(as_text=True)
    assert re.search(r"ends in|until \d\d:\d\d|on now", body)


def test_countdown_script_stops_when_nothing_is_left_to_tick():
    script = (STATIC / "countdown.js").read_text(encoding="utf-8")
    assert "clearInterval" in script
    assert "visibilitychange" in script, "a woken phone shows stale text"


# --------------------------------------------------------------------------- #
# tap targets and motion
# --------------------------------------------------------------------------- #


def test_interactive_controls_meet_the_tap_target_floor():
    theme = (STATIC / "theme.css").read_text(encoding="utf-8")
    assert "min-height: 44px" in theme


def test_motion_respects_reduced_motion():
    theme = (STATIC / "theme.css").read_text(encoding="utf-8")
    assert "prefers-reduced-motion: reduce" in theme


def test_only_linked_stylesheets_exist():
    """Dead CSS is a trap: it looks authoritative and changes nothing."""
    linked = set()
    for template in TEMPLATES.glob("*.html"):
        text = template.read_text(encoding="utf-8")
        linked.update(re.findall(r"filename='([a-z-]+\.css)'", text))
    on_disk = {path.name for path in STATIC.glob("*.css")}
    assert on_disk == linked, f"unlinked stylesheets on disk: {on_disk - linked}"


def test_every_page_still_renders(client, conn):
    seed(conn)
    for path in ("/", "/week", "/venue/murphyspubcincy", "/?sort=price", "/?cat=beer"):
        assert client.get(path).status_code == 200, path


# --------------------------------------------------------------------------- #
# urgency and actionable summary
# --------------------------------------------------------------------------- #


def test_closing_window_is_marked_urgent():
    """"18 minutes left" is a different message from "on now"."""
    now = datetime(2026, 9, 11, 20, 45, tzinfo=timezone.utc)
    closing = time_status({"start_time": "17:00", "end_time": "21:00"}, now, is_today=True)
    assert closing["urgent"] is True
    assert closing["minutes_left"] == 15


def test_a_window_with_hours_left_is_not_urgent():
    now = datetime(2026, 9, 11, 20, 45, tzinfo=timezone.utc)
    relaxed = time_status({"start_time": "17:00", "end_time": "23:00"}, now, is_today=True)
    assert relaxed["urgent"] is False


def test_every_timing_shape_carries_the_urgency_keys():
    """Templates read these unconditionally; a missing key is a 500."""
    now = datetime(2026, 9, 11, 20, 45, tzinfo=timezone.utc)
    rows = [
        {"start_time": None, "end_time": None},
        {"start_time": "17:00", "end_time": "21:00"},
        {"start_time": "23:00", "end_time": None},
        {"start_time": "01:00", "end_time": "02:00"},
    ]
    for row in rows:
        for is_today in (True, False):
            status = time_status(row, now, is_today=is_today)
            assert set(status) >= {
                "status", "label", "sort_key", "starts_at",
                "ends_at", "minutes_left", "urgent",
            }, (row, is_today)


def test_summary_numbers_are_actionable(client, conn):
    """Reading "from $2" and wanting only $2 deals is the same impulse."""
    seed(conn, price=2.0)
    body = client.get("/").get_data(as_text=True)
    assert "stat-link" in body
    assert "max=2" in body


def test_on_now_count_links_to_the_on_now_group(client, conn):
    now = datetime.now().astimezone()
    seed(conn, start_time=now.strftime("%H:%M"),
         end_time=(now + timedelta(hours=3)).strftime("%H:%M"))
    body = client.get("/").get_data(as_text=True)
    assert "#on-now" in body
    assert 'id="on-now"' in client.get("/?sort=time").get_data(as_text=True)
