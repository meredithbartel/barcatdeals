"""Resolve relative date language against a post's own timestamp.

Bars write "tonight", "this Thursday", "all weekend" - never dates. This module
turns that language into concrete dates or a weekly recurrence, anchored to
when the post was actually published, in ``America/New_York``.

The timezone conversion is the subtle part and the reason this module exists
separately. Instagram timestamps are UTC, and a post published at 01:30 UTC is
*the previous evening* in Cincinnati. Resolving "tonight" against the UTC date
would silently place every late-night post one day late.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

DEFAULT_TZ = "America/New_York"

MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY, SATURDAY, SUNDAY = range(7)

#: Weekday spellings bars actually use, mapped to Python's Monday=0 convention.
WEEKDAYS: dict[str, int] = {
    "monday": MONDAY, "mon": MONDAY,
    "tuesday": TUESDAY, "tues": TUESDAY, "tue": TUESDAY,
    "wednesday": WEDNESDAY, "weds": WEDNESDAY, "wed": WEDNESDAY,
    "thursday": THURSDAY, "thurs": THURSDAY, "thur": THURSDAY, "thu": THURSDAY,
    "friday": FRIDAY, "fri": FRIDAY,
    "saturday": SATURDAY, "sat": SATURDAY,
    "sunday": SUNDAY, "sun": SUNDAY,
}

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# Longest spellings first so "thursday" wins over "thurs" / "thu".
_WEEKDAY_ALTERNATION = "|".join(sorted(WEEKDAYS, key=len, reverse=True))

_RE_EVERY_WEEKDAY = re.compile(
    rf"\b(?:every|each|all)\s+(?P<day>{_WEEKDAY_ALTERNATION})\b", re.IGNORECASE
)
# "Thursdays" / "Thurs." - the plural alone implies a recurring special.
_RE_PLURAL_WEEKDAY = re.compile(rf"\b(?P<day>{_WEEKDAY_ALTERNATION})s\b", re.IGNORECASE)
_RE_WEEKDAY = re.compile(rf"\b(?P<day>{_WEEKDAY_ALTERNATION})\b", re.IGNORECASE)

_RE_DAILY = re.compile(
    r"\b(?:every\s*day|everyday|daily|7\s*days\s*a\s*week|seven\s*days\s*a\s*week"
    r"|all\s*day\s*every\s*day)\b",
    re.IGNORECASE,
)
_RE_WEEKEND = re.compile(r"\b(?:all\s+weekend|this\s+weekend|the\s+weekend|weekend\s+long)\b",
                         re.IGNORECASE)
_RE_ALL_WEEK = re.compile(r"\b(?:all\s+week|this\s+week|week\s+long|all\s+week\s+long)\b",
                          re.IGNORECASE)
_RE_TOMORROW = re.compile(r"\btomorrow\b", re.IGNORECASE)
_RE_TODAY = re.compile(r"\b(?:tonight|today|this\s+evening|this\s+afternoon|now)\b", re.IGNORECASE)
_RE_ISO_DATE = re.compile(r"\b(?P<iso>\d{4}-\d{2}-\d{2})\b")


@dataclass
class Resolution:
    """The outcome of resolving one piece of date language.

    Attributes:
        dates: Concrete dates the deal runs on. Empty for pure recurrence.
        day_of_week: 0=Monday .. 6=Sunday, when the deal recurs weekly.
        recurrence: One of ``once``, ``weekly``, ``daily``, ``unknown``.
        matched: The substring that produced this resolution, for debugging.
    """

    dates: list[date] = field(default_factory=list)
    day_of_week: int | None = None
    recurrence: str = "unknown"
    matched: str | None = None

    @property
    def specific_date(self) -> str | None:
        """First resolved date as an ISO string, or None for recurring deals."""
        return self.dates[0].isoformat() if self.dates else None

    @property
    def iso_dates(self) -> list[str]:
        """All resolved dates as ISO strings."""
        return [day.isoformat() for day in self.dates]


def get_tz(tz_name: str = DEFAULT_TZ) -> ZoneInfo:
    """Return the tzinfo for a timezone name."""
    return ZoneInfo(tz_name)


def to_local(moment: datetime, tz_name: str = DEFAULT_TZ) -> datetime:
    """Convert a post timestamp into local bar time.

    Args:
        moment: The post timestamp. Naive values are assumed to be UTC,
            matching what Instagram returns.
        tz_name: IANA timezone name.

    Returns:
        The same instant expressed in ``tz_name``.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(get_tz(tz_name))


def post_date(moment: datetime, tz_name: str = DEFAULT_TZ) -> date:
    """The local calendar date a post belongs to.

    This is the anchor every relative phrase is resolved against. A post made
    at 01:30 UTC resolves to the previous day in Cincinnati, which is why
    "tonight" must never be computed from the UTC date.
    """
    return to_local(moment, tz_name).date()


def next_weekday(start: date, weekday: int, *, include_start: bool = True) -> date:
    """The next occurrence of a weekday on or after ``start``.

    Args:
        start: Day to search from.
        weekday: 0=Monday .. 6=Sunday.
        include_start: If True and ``start`` already falls on ``weekday``,
            return ``start`` itself.

    Returns:
        The resolved date.
    """
    delta = (weekday - start.weekday()) % 7
    if delta == 0 and not include_start:
        delta = 7
    return start + timedelta(days=delta)


def weekend_dates(anchor: date) -> list[date]:
    """Friday, Saturday and Sunday of the anchor's weekend.

    Days that already passed relative to ``anchor`` are dropped, so "all
    weekend" posted on a Saturday covers Saturday and Sunday only.
    """
    friday = next_weekday(anchor, FRIDAY) if anchor.weekday() <= FRIDAY else anchor - timedelta(
        days=anchor.weekday() - FRIDAY
    )
    return [day for day in (friday, friday + timedelta(days=1), friday + timedelta(days=2))
            if day >= anchor]


def week_dates(anchor: date, days: int = 7) -> list[date]:
    """``days`` consecutive dates starting at the anchor."""
    return [anchor + timedelta(days=offset) for offset in range(days)]


def resolve(text: str, posted_at: datetime, tz_name: str = DEFAULT_TZ) -> Resolution:
    """Resolve the date language in ``text`` against when it was posted.

    Recurring phrasing is checked before one-off phrasing, so "every Thursday"
    is a weekly deal rather than a bare weekday reference.

    Args:
        text: Caption or deal description.
        posted_at: When the post was published (UTC).
        tz_name: Local timezone for the bars.

    Returns:
        A :class:`Resolution`. Unrecognised text yields ``recurrence='unknown'``
        with no dates, which callers should treat as low confidence rather than
        guessing.

    Examples:
        >>> from datetime import datetime, timezone
        >>> wed = datetime(2026, 9, 17, 1, 30, tzinfo=timezone.utc)  # 9:30pm Sep 16 EDT
        >>> resolve("$2 domestics tonight!", wed).specific_date
        '2026-09-16'
        >>> resolve("Every Thursday: half off wings", wed).day_of_week
        3
    """
    anchor = post_date(posted_at, tz_name)
    text = text or ""

    # 1. Explicit ISO date wins outright.
    match = _RE_ISO_DATE.search(text)
    if match:
        try:
            return Resolution(
                dates=[date.fromisoformat(match.group("iso"))],
                recurrence="once",
                matched=match.group(0),
            )
        except ValueError:
            pass

    # 2. Daily recurrence.
    match = _RE_DAILY.search(text)
    if match:
        return Resolution(recurrence="daily", matched=match.group(0))

    # 3. Weekly recurrence: "every Thursday", "Thursdays".
    match = _RE_EVERY_WEEKDAY.search(text) or _RE_PLURAL_WEEKDAY.search(text)
    if match:
        return Resolution(
            day_of_week=WEEKDAYS[match.group("day").lower()],
            recurrence="weekly",
            matched=match.group(0),
        )

    # 4. Weekend and week spans.
    match = _RE_WEEKEND.search(text)
    if match:
        return Resolution(dates=weekend_dates(anchor), recurrence="once", matched=match.group(0))

    match = _RE_ALL_WEEK.search(text)
    if match:
        return Resolution(dates=week_dates(anchor), recurrence="once", matched=match.group(0))

    # 5. Relative single days.
    match = _RE_TOMORROW.search(text)
    if match:
        return Resolution(
            dates=[anchor + timedelta(days=1)], recurrence="once", matched=match.group(0)
        )

    match = _RE_TODAY.search(text)
    if match:
        return Resolution(dates=[anchor], recurrence="once", matched=match.group(0))

    # 6. A bare weekday: the next occurrence on or after the post date.
    match = _RE_WEEKDAY.search(text)
    if match:
        weekday = WEEKDAYS[match.group("day").lower()]
        return Resolution(
            dates=[next_weekday(anchor, weekday)],
            day_of_week=weekday,
            recurrence="once",
            matched=match.group(0),
        )

    return Resolution(recurrence="unknown")


def resolve_deal_dates(
    deal_text: str, caption: str, posted_at: datetime, tz_name: str = DEFAULT_TZ
) -> Resolution:
    """Resolve a deal's dates, falling back to its caption for context.

    Flyer-derived deals often carry no date language of their own ("$1 wings
    5-9pm"), while the caption says "TONIGHT". The deal text is tried first so
    its own wording wins when it has any.

    Args:
        deal_text: The extracted deal description.
        caption: The full post caption.
        posted_at: When the post was published (UTC).
        tz_name: Local timezone.

    Returns:
        The best available :class:`Resolution`.
    """
    resolution = resolve(deal_text, posted_at, tz_name)
    if resolution.recurrence != "unknown":
        return resolution
    return resolve(caption, posted_at, tz_name)


def describe_day_of_week(day_of_week: int | None) -> str:
    """Human-readable weekday name, or an empty string for None."""
    if day_of_week is None or not 0 <= day_of_week <= 6:
        return ""
    return WEEKDAY_NAMES[day_of_week]
