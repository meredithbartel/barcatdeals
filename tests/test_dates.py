"""Tests for relative date resolution.

This is the module most likely to be silently wrong, so the cases below are
deliberately picky about timezone boundaries and weekday arithmetic.

Reference calendar (2026):
    Mon 14 Sep | Tue 15 | Wed 16 | Thu 17 | Fri 18 | Sat 19 | Sun 20
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from bardeals import dates
from bardeals.dates import Resolution, post_date, resolve, resolve_deal_dates

# 2026-09-16 22:30 EDT == 2026-09-17 02:30 UTC. Posted Wednesday night.
WED_NIGHT = datetime(2026, 9, 17, 2, 30, tzinfo=timezone.utc)
# 2026-09-16 14:00 EDT == 18:00 UTC. Same Wednesday, safely mid-afternoon.
WED_AFTERNOON = datetime(2026, 9, 16, 18, 0, tzinfo=timezone.utc)
# 2026-09-19 20:00 EDT == 2026-09-20 00:00 UTC. Posted Saturday night.
SAT_NIGHT = datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc)


def test_calendar_assumptions_hold():
    """Guard the dates the rest of this file reasons about."""
    assert date(2026, 9, 14).weekday() == dates.MONDAY
    assert date(2026, 9, 16).weekday() == dates.WEDNESDAY
    assert date(2026, 9, 17).weekday() == dates.THURSDAY
    assert date(2026, 9, 18).weekday() == dates.FRIDAY
    assert date(2026, 9, 20).weekday() == dates.SUNDAY


# --------------------------------------------------------------------------- #
# timezone anchoring
# --------------------------------------------------------------------------- #


def test_late_night_post_belongs_to_the_previous_local_day():
    """The whole reason this module exists: 02:30 UTC is last night in Ohio."""
    assert post_date(WED_NIGHT) == date(2026, 9, 16)


def test_afternoon_post_keeps_its_date():
    assert post_date(WED_AFTERNOON) == date(2026, 9, 16)


def test_naive_timestamps_are_treated_as_utc():
    naive = datetime(2026, 9, 17, 2, 30)
    assert post_date(naive) == date(2026, 9, 16)


def test_utc_date_would_have_been_wrong():
    """Documents the bug this module prevents."""
    assert WED_NIGHT.date() == date(2026, 9, 17)
    assert post_date(WED_NIGHT) == date(2026, 9, 16)


def test_dst_boundary_november():
    """01:30 UTC on 2026-11-02 is 2026-11-01 21:30 EDT, the day DST ends."""
    moment = datetime(2026, 11, 2, 1, 30, tzinfo=timezone.utc)
    assert post_date(moment) == date(2026, 11, 1)


def test_local_conversion_reports_eastern_offset():
    local = dates.to_local(WED_AFTERNOON)
    assert local.hour == 14
    assert local.utcoffset().total_seconds() == -4 * 3600  # EDT in September


# --------------------------------------------------------------------------- #
# the cases named in the spec
# --------------------------------------------------------------------------- #


def test_tonight_resolves_to_the_post_date():
    result = resolve("$2 domestics tonight!", WED_NIGHT)
    assert result.specific_date == "2026-09-16"
    assert result.recurrence == "once"


def test_today_resolves_to_the_post_date():
    assert resolve("Happy hour today", WED_AFTERNOON).specific_date == "2026-09-16"


def test_tomorrow_is_the_next_day():
    assert resolve("$3 wells tomorrow", WED_AFTERNOON).specific_date == "2026-09-17"


def test_tomorrow_from_a_late_night_post_is_still_relative_to_local_date():
    assert resolve("$3 wells tomorrow", WED_NIGHT).specific_date == "2026-09-17"


def test_every_thursday_is_weekly_with_no_specific_date():
    result = resolve("Every Thursday: half off wings", WED_AFTERNOON)
    assert result.day_of_week == dates.THURSDAY == 3
    assert result.recurrence == "weekly"
    assert result.specific_date is None


def test_plural_weekday_is_also_weekly():
    result = resolve("Thursdays are for wings", WED_AFTERNOON)
    assert result.recurrence == "weekly"
    assert result.day_of_week == 3


def test_this_thursday_is_the_next_occurrence():
    result = resolve("this Thursday $1 wings", WED_AFTERNOON)
    assert result.specific_date == "2026-09-17"
    assert result.recurrence == "once"


def test_bare_weekday_is_the_next_occurrence():
    assert resolve("Monday funday $5 pitchers", WED_AFTERNOON).specific_date == "2026-09-21"


def test_weekday_matching_the_post_day_resolves_to_that_day():
    """"Wednesday" on a Wednesday means today, not next week."""
    assert resolve("Wednesday wings", WED_AFTERNOON).specific_date == "2026-09-16"


def test_all_weekend_covers_friday_saturday_sunday():
    result = resolve("$4 shots all weekend", WED_AFTERNOON)
    assert result.iso_dates == ["2026-09-18", "2026-09-19", "2026-09-20"]


def test_all_weekend_posted_mid_weekend_drops_days_already_gone():
    result = resolve("$4 shots all weekend", SAT_NIGHT)
    assert result.iso_dates == ["2026-09-19", "2026-09-20"]


def test_every_monday_is_weekly_without_a_date():
    result = resolve("every Monday $5 burgers", WED_AFTERNOON)
    assert result.recurrence == "weekly"
    assert result.day_of_week == dates.MONDAY == 0
    assert result.specific_date is None


# --------------------------------------------------------------------------- #
# recurrence vs one-off precedence
# --------------------------------------------------------------------------- #


def test_recurring_phrasing_beats_the_bare_weekday_inside_it():
    """"every Thursday" must not degrade into a one-off Thursday."""
    result = resolve("every Thursday", WED_AFTERNOON)
    assert result.recurrence == "weekly"
    assert result.dates == []


def test_daily_recurrence_variants(  ):
    for phrase in ["every day", "everyday", "daily specials", "7 days a week"]:
        assert resolve(phrase, WED_AFTERNOON).recurrence == "daily", phrase


def test_daily_beats_a_weekday_mention():
    assert resolve("$2 tacos daily, even Sunday", WED_AFTERNOON).recurrence == "daily"


def test_all_week_spans_seven_days():
    result = resolve("$5 pitchers all week", WED_AFTERNOON)
    assert len(result.dates) == 7
    assert result.iso_dates[0] == "2026-09-16"
    assert result.iso_dates[-1] == "2026-09-22"


def test_explicit_iso_date_wins():
    result = resolve("Block party 2026-10-03, $3 drafts", WED_AFTERNOON)
    assert result.specific_date == "2026-10-03"
    assert result.recurrence == "once"


def test_invalid_iso_date_falls_through_instead_of_raising():
    assert resolve("2026-13-45 nonsense tonight", WED_AFTERNOON).specific_date == "2026-09-16"


# --------------------------------------------------------------------------- #
# non-matches: the important negative path
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text",
    ["", "see you soon", "great crowd last night", "$2 domestics", "\U0001f37b"],
)
def test_unrecognised_text_yields_unknown(text):
    result = resolve(text, WED_AFTERNOON)
    assert result.recurrence == "unknown"
    assert result.dates == []
    assert result.day_of_week is None


def test_none_text_is_safe():
    assert resolve(None, WED_AFTERNOON).recurrence == "unknown"


def test_substrings_do_not_match_weekdays():
    """"sunny" must not read as Sunday, "monster" not as Monday."""
    assert resolve("sunny patio, monster burgers", WED_AFTERNOON).recurrence == "unknown"


def test_satisfaction_does_not_match_saturday():
    assert resolve("satisfaction guaranteed", WED_AFTERNOON).recurrence == "unknown"


# --------------------------------------------------------------------------- #
# weekday abbreviations
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "abbrev,expected",
    [
        ("mon", 0), ("tue", 1), ("tues", 1), ("wed", 2), ("weds", 2),
        ("thu", 3), ("thur", 3), ("thurs", 3), ("fri", 4), ("sat", 5), ("sun", 6),
    ],
)
def test_abbreviations_resolve(abbrev, expected):
    result = resolve(f"every {abbrev} $2 drafts", WED_AFTERNOON)
    assert result.day_of_week == expected
    assert result.recurrence == "weekly"


def test_longest_weekday_spelling_wins():
    """"thursday" must not be truncated to "thurs" or "thu"."""
    assert resolve("every thursday", WED_AFTERNOON).day_of_week == 3


def test_case_is_ignored():
    assert resolve("EVERY FRIDAY", WED_AFTERNOON).day_of_week == 4
    assert resolve("Every Friday", WED_AFTERNOON).day_of_week == 4


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def test_next_weekday_includes_today_by_default():
    wednesday = date(2026, 9, 16)
    assert dates.next_weekday(wednesday, dates.WEDNESDAY) == wednesday


def test_next_weekday_can_exclude_today():
    wednesday = date(2026, 9, 16)
    assert dates.next_weekday(wednesday, dates.WEDNESDAY, include_start=False) == date(2026, 9, 23)


def test_next_weekday_wraps_across_the_week():
    assert dates.next_weekday(date(2026, 9, 18), dates.MONDAY) == date(2026, 9, 21)


def test_weekend_dates_from_a_sunday_returns_only_sunday():
    assert dates.weekend_dates(date(2026, 9, 20)) == [date(2026, 9, 20)]


def test_describe_day_of_week():
    assert dates.describe_day_of_week(3) == "Thursday"
    assert dates.describe_day_of_week(None) == ""
    assert dates.describe_day_of_week(99) == ""


# --------------------------------------------------------------------------- #
# deal/caption fallback
# --------------------------------------------------------------------------- #


def test_deal_text_wins_over_caption():
    result = resolve_deal_dates("half off wings every Thursday", "TONIGHT!", WED_AFTERNOON)
    assert result.recurrence == "weekly"
    assert result.day_of_week == 3


def test_caption_fills_in_for_a_dateless_flyer_deal():
    """The flyer case: prices in the image, the date only in the caption."""
    result = resolve_deal_dates("$1 wings 5-9pm", "TONIGHT \U0001f525", WED_NIGHT)
    assert result.specific_date == "2026-09-16"
    assert result.recurrence == "once"


def test_neither_source_has_dates():
    result = resolve_deal_dates("$1 wings", "\U0001f37b", WED_AFTERNOON)
    assert result.recurrence == "unknown"
    assert result.dates == []


def test_resolution_defaults_are_empty():
    result = Resolution()
    assert result.specific_date is None
    assert result.iso_dates == []
