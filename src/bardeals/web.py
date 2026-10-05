"""Local web UI for browsing deals.

A small read-only Flask app over the same SQLite database the CLI reads. It
never scrapes and never calls the LLM - ``run_daily.py`` does that on a cron
schedule and this just renders whatever is on disk.

Design notes, all driven by the actual user (a UC student on a phone):

* **Cheapest first by default.** The question is "where is it cheap tonight",
  not "what has Murphy's got". Grouping by bar is a query-param away.
* **Progressive enhancement.** The map adds interactive 3D exploration, while
  sorting, day selection, and the deal list also work without JavaScript.
* **Freshness is a first-class element.** Scraping is frequently blocked, so a
  stale database is the normal case; the page says so plainly rather than
  quietly showing old prices as if they were today's.
* **Nothing is hidden.** Low-confidence deals get a dimmed section, bars with
  nothing tonight get named, and every deal links back to its source post.

One connection is opened per request and closed on teardown. A module-level
connection would raise ``ProgrammingError`` under the threaded dev server,
because SQLite objects may only be used in the thread that created them.
"""

from __future__ import annotations

import sqlite3
import re
import math
import urllib.parse
from datetime import date, datetime, time, timedelta
from typing import Any, Sequence

from flask import Flask, abort, g, jsonify, render_template, request, url_for

from . import db, report
from .config import Config, get_config, setup_logging
from .dates import describe_day_of_week, get_tz

#: A run older than this many days gets a loud staleness banner.
STALE_AFTER_DAYS = 2

#: Number of days offered in the day strip, starting with today.
DAY_STRIP_LENGTH = 7

#: A window closing within this many minutes is "last call" - worth hurrying for.
URGENT_MINUTES = 30

#: Price caps offered as one-tap filters.
PRICE_CAPS = [("", "Any"), ("2", "$2"), ("3", "$3"), ("5", "$5"), ("10", "$10")]


# --------------------------------------------------------------------------- #
# view models
# --------------------------------------------------------------------------- #


#: Ports Chrome, Firefox and Safari refuse to connect to, because they belong to
#: protocols that could be abused via a crafted HTTP request (SMTP, NFS, SIP...).
#: Binding one works fine - the *browser* is what refuses, with a bare
#: ``ERR_UNSAFE_PORT`` and no hint that the port is the problem. 5060/5061 (SIP)
#: are the ones most likely to be picked by accident.
BLOCKED_BROWSER_PORTS = frozenset({
    1, 7, 9, 11, 13, 15, 17, 19, 20, 21, 22, 23, 25, 37, 42, 43, 53, 69, 77, 79,
    87, 95, 101, 102, 103, 104, 109, 110, 111, 113, 115, 117, 119, 123, 135, 137,
    139, 143, 161, 179, 389, 427, 465, 512, 513, 514, 515, 526, 530, 531, 532,
    540, 548, 554, 556, 563, 587, 601, 636, 989, 990, 993, 995, 1719, 1720, 1723,
    2049, 3659, 4045, 4190, 5060, 5061, 6000, 6566, 6665, 6666, 6667, 6668, 6669,
    6679, 6697, 10080,
})

#: Offered as alternatives when the chosen port is blocked.
SUGGESTED_PORTS = (8080, 8000, 5000, 3000)


def port_is_browser_safe(port: int) -> bool:
    """Whether a browser will actually connect to this port.

    Args:
        port: TCP port number.

    Returns:
        False when browsers hard-block it (``ERR_UNSAFE_PORT``).
    """
    return port not in BLOCKED_BROWSER_PORTS


def _parse_hhmm(value: str | None) -> time | None:
    """Parse an ``HH:MM`` string, returning None when absent or malformed."""
    if not value:
        return None
    try:
        hour, minute = (int(part) for part in str(value).split(":")[:2])
        return time(hour, minute)
    except (TypeError, ValueError):
        return None


def is_on_now(row: sqlite3.Row, now: datetime) -> bool:
    """Whether a deal's time window contains ``now``.

    Deals with no stated window are not "on now" - saying so would be a guess.
    A window whose end is before its start is treated as running past midnight,
    which is how bars actually write "9pm-2am".
    """
    start = _parse_hhmm(row["start_time"])
    end = _parse_hhmm(row["end_time"])
    if start is None and end is None:
        return False

    current = now.time()
    if start is not None and end is not None:
        if end <= start:  # crosses midnight
            return current >= start or current <= end
        return start <= current <= end
    if start is not None:
        return current >= start
    return current <= end


#: Hosts and shortcode prefixes that mark a row as sample data rather than
#: something really scraped. ``example.invalid`` is a reserved, deliberately
#: non-routable domain used by seed_demo.py; ``FIX``/``DEMO`` shortcodes are
#: invented, so the Instagram post URLs built from them 404.
SAMPLE_HOSTS = ("example.invalid", "example.com", "example.org")
SAMPLE_SHORTCODE_PREFIXES = ("DEMO", "FIX")


def is_sample_source(permalink: str | None, shortcode: str | None) -> bool:
    """Whether this row's source link points at nothing real.

    Args:
        permalink: The stored source URL.
        shortcode: The stored shortcode.

    Returns:
        True when the link cannot be opened, because the row is demo or
        fixture data rather than something actually fetched.
    """
    if permalink:
        host = urllib.parse.urlparse(permalink).netloc.lower()
        if any(host == bad or host.endswith("." + bad) for bad in SAMPLE_HOSTS):
            return True
    if shortcode and shortcode.upper().startswith(SAMPLE_SHORTCODE_PREFIXES):
        return True
    return False


def source_link(
    permalink: str | None,
    shortcode: str | None,
    venue_slug: str | None,
    source_kind: str = "instagram",
) -> dict[str, Any]:
    """Work out what the "verify" control should actually do.

    A link labelled *verify* makes a promise, so it must never be a dead end.
    Sample rows are redirected to the bar's real Instagram profile - which does
    exist and is genuinely where you would go to check - and labelled as sample
    data so nobody mistakes them for a scraped deal.

    Args:
        permalink: The stored source URL.
        shortcode: The stored shortcode.
        venue_slug: The bar's Instagram handle, used as the fallback target.
        source_kind: ``"instagram"`` or ``"website"``.

    Returns:
        ``{"url", "label", "kind", "title"}``. ``kind`` is ``post``, ``page``,
        ``sample`` or ``none``; ``url`` is None only when ``kind`` is ``none``,
        in which case the template must not render an anchor at all.
    """
    if is_sample_source(permalink, shortcode):
        if venue_slug:
            return {
                "url": f"https://www.instagram.com/{venue_slug}/",
                "label": f"@{venue_slug} \u2197",
                "kind": "sample",
                "title": "Sample data - this opens the bar's real Instagram profile",
            }
        return {"url": None, "label": "sample data", "kind": "none",
                "title": "Sample data with no source to check"}

    if not permalink or not permalink.startswith(("http://", "https://")):
        return {"url": None, "label": "no source", "kind": "none",
                "title": "This deal has no source link on record"}

    if source_kind == "website":
        return {"url": permalink, "label": "view page \u2197", "kind": "page",
                "title": "Open the page this was read from"}

    return {"url": permalink, "label": "verify \u2197", "kind": "post",
            "title": "Open the original Instagram post"}


def _row_get(row, key: str, default: Any = None) -> Any:
    """Read a column that may not exist on older rows or test doubles."""
    try:
        value = row[key]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


#: Rough category buckets, so a student can filter to "cheap beer" without the
#: LLM having to classify anything. Matched on whole words against the deal
#: text and item, most specific bucket first: "bottles of wine" must land in
#: wine rather than beer, and "$5 margs" in cocktails rather than food.
CATEGORY_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("wine", ("wine", "wines", "prosecco", "champagne", "sangria", "rose")),
    ("shots", ("shot", "shots", "shooter", "shooters", "jameson", "fireball",
               "jager", "jagerbomb", "tequila", "whiskey", "whisky", "bourbon")),
    ("cocktails", ("well", "wells", "cocktail", "cocktails", "margarita", "margaritas",
                   "marg", "margs", "martini", "martinis", "mule", "mules", "mixed",
                   "long", "islands", "spritz", "mimosa", "mimosas", "bloody", "marys",
                   "you-call-its", "slushy", "vodka", "gin", "rum")),
    ("beer", ("beer", "beers", "draft", "drafts", "draught", "lager", "ipa", "ale",
              "pitcher", "pitchers", "bucket", "buckets", "tallboy", "tallboys",
              "domestic", "domestics", "import", "imports", "pbr", "guinness",
              "killians", "bottle", "bottles", "can", "cans", "brew", "brews", "stout")),
    ("food", ("wing", "wings", "pizza", "pizzas", "slice", "slices", "taco", "tacos",
              "burger", "burgers", "fries", "nacho", "nachos", "appetizer", "appetizers",
              "apps", "slider", "sliders", "queso", "burrito", "burritos", "quesadilla",
              "sandwich", "sandwiches", "pasta", "salad", "coney", "coneys", "hotdog",
              "food", "entree", "entrees", "lunch", "brunch", "meal", "meals", "wing",
              "chips", "dip", "buffet", "kids")),
]

#: Filter labels shown in the UI, in display order.
CATEGORY_LABELS = [
    ("all", "All"),
    ("beer", "Beer"),
    ("cocktails", "Cocktails"),
    ("shots", "Shots"),
    ("wine", "Wine"),
    ("food", "Food"),
]

_WORD = re.compile(r"[a-z0-9\-]+")


def classify_deal(deal_text: str | None, item: str | None = None) -> str:
    """Bucket a deal into beer / cocktails / shots / wine / food / other.

    A keyword heuristic rather than an LLM call: classification is only used
    for filtering, it costs nothing, and re-parsing every stored deal to add a
    category column would not be worth the spend.

    Args:
        deal_text: The deal description.
        item: The extracted item, when the model identified one.

    Returns:
        A category key; ``"other"`` when nothing matches.
    """
    words = set(_WORD.findall(f"{deal_text or ''} {item or ''}".lower()))
    if not words:
        return "other"

    best, best_hits = "other", 0
    for category, keywords in CATEGORY_KEYWORDS:
        hits = sum(1 for keyword in keywords if keyword in words)
        # Strictly greater keeps the earlier (more specific) bucket on a tie,
        # so "bottles of wine" stays wine instead of falling through to beer.
        if hits > best_hits:
            best, best_hits = category, hits
    return best


def _minutes_until(target: time, now: datetime) -> int:
    """Whole minutes from ``now`` until ``target`` today (negative if passed)."""
    current = now.hour * 60 + now.minute
    return (target.hour * 60 + target.minute) - current


def _humanize(minutes: int) -> str:
    """Render a minute count as "45m" or "2h 10m"."""
    minutes = max(minutes, 0)
    if minutes < 60:
        return f"{minutes}m"
    hours, rest = divmod(minutes, 60)
    return f"{hours}h" if rest == 0 else f"{hours}h {rest}m"


def time_status(row, now: datetime, *, is_today: bool) -> dict[str, Any]:
    """Classify a deal as on now, later, finished, or untimed.

    Only meaningful for today: for any other day "on now" is nonsense, so
    every deal comes back as ``anytime``.

    Args:
        row: Anything indexable with ``start_time`` / ``end_time``.
        now: Current local time.
        is_today: Whether the day being rendered is actually today.

    Returns:
        ``{"status", "label", "sort_key"}``. ``status`` is one of ``now``,
        ``later``, ``over`` or ``anytime``.
    """
    start = _parse_hhmm(row["start_time"])
    end = _parse_hhmm(row["end_time"])

    if not is_today or (start is None and end is None):
        return {"status": "anytime", "label": "", "sort_key": 1,
                "starts_at": None, "ends_at": None,
                "minutes_left": None, "urgent": False}

    def absolute(clock: time | None, minutes_away: int | None) -> str | None:
        """Turn an HH:MM into a full timestamp the browser can count down to.

        The page is served once and may sit open for hours, so the client needs
        a real instant rather than a pre-rendered "33m" that quietly rots.
        """
        if clock is None or minutes_away is None:
            return None
        moment = now.replace(hour=clock.hour, minute=clock.minute, second=0, microsecond=0)
        if minutes_away > 0 and moment < now:
            moment += timedelta(days=1)
        elif minutes_away < 0 and moment > now:
            moment -= timedelta(days=1)
        return moment.isoformat()

    if is_on_now(row, now):
        remaining = None
        if end is not None:
            remaining = _minutes_until(end, now)
            if remaining < 0:  # the window ends tomorrow, not later this morning
                remaining += 24 * 60
        ends_at = absolute(end, remaining)

        if end is not None and remaining is not None:
            # Scarcity is the decision trigger: "$2 drafts, 18 minutes left" is a
            # different message from "$2 drafts, on now".
            urgent = 0 <= remaining <= URGENT_MINUTES
            if 0 <= remaining <= 90:
                return {"status": "now", "label": f"ends in {_humanize(remaining)}",
                        "sort_key": 0, "starts_at": None, "ends_at": ends_at,
                        "minutes_left": remaining, "urgent": urgent}
            return {"status": "now", "label": f"until {end:%H:%M}", "sort_key": 0,
                    "starts_at": None, "ends_at": ends_at,
                    "minutes_left": remaining, "urgent": False}
        return {"status": "now", "label": "on now", "sort_key": 0,
                "starts_at": None, "ends_at": None,
                "minutes_left": None, "urgent": False}

    if start is not None:
        until = _minutes_until(start, now)
        if until > 0:
            return {"status": "later", "label": f"starts in {_humanize(until)}",
                    "sort_key": 2, "starts_at": absolute(start, until), "ends_at": None,
                    "minutes_left": None, "urgent": False}

    return {"status": "over", "label": "finished for today", "sort_key": 3,
            "starts_at": None, "ends_at": None,
            "minutes_left": None, "urgent": False}


def deal_view(row: sqlite3.Row, now: datetime, *, is_today: bool = True) -> dict[str, Any]:
    """Convert a joined deal row into a template-friendly dict.

    ``sqlite3.Row`` is neither JSON-serializable nor attribute-accessible in a
    template, so every view goes through here.
    """
    price = row["price"]
    return {
        "id": row["id"],
        "deal_text": row["deal_text"],
        "item": row["item"],
        "day_of_week": row["day_of_week"],
        "specific_date": row["specific_date"],
        "price": price,
        # Empty placeholders, not "-": the template omits the element entirely
        # rather than rendering a dash the way the terminal tables do.
        "price_label": report.format_price(row, placeholder="") or None,
        "window": report.format_time(row, placeholder=""),
        "when": report.format_when(row, unknown="date unknown"),
        "recurrence": row["recurrence"],
        "confidence": row["confidence"],
        "source": row["source"],
        "source_kind": _row_get(row, "source_kind", "instagram"),
        "source_label": report.source_label(row, long=True),
        "venue_slug": row["venue_slug"],
        "venue_name": row["venue_name"],
        "permalink": row["permalink"],
        "shortcode": row["shortcode"],
        "posted_at": row["posted_at"],
        "verify": source_link(
            row["permalink"],
            row["shortcode"],
            row["venue_slug"],
            _row_get(row, "source_kind", "instagram"),
        ),
        "on_now": is_today and is_on_now(row, now),
        "category": classify_deal(row["deal_text"], row["item"]),
        "timing": time_status(row, now, is_today=is_today),
    }


def _cheapest_first(view: dict) -> tuple:
    """Order deals cheapest first, with unpriced offers last."""
    price = view["price"]
    return (price is None, price if price is not None else 0.0,
            (view["venue_name"] or "").lower())


def group_by_venue(views: Sequence[dict]) -> list[dict]:
    """Group deal views by bar, cheapest deal first within each bar."""
    grouped: dict[str, dict] = {}
    for view in views:
        bucket = grouped.setdefault(
            view["venue_slug"],
            {"venue_slug": view["venue_slug"], "venue_name": view["venue_name"], "deals": []},
        )
        bucket["deals"].append(view)

    for bucket in grouped.values():
        bucket["deals"].sort(key=_cheapest_first)
        bucket["cheapest"] = next(
            (d["price"] for d in bucket["deals"] if d["price"] is not None), None
        )
    return sorted(grouped.values(), key=lambda b: b["venue_name"].lower())


def _official_website(value: str | None) -> str | None:
    """Expose only absolute HTTP(S) website links to the browser."""
    if not value:
        return None
    url = value.strip()
    try:
        parsed = urllib.parse.urlsplit(url)
        return url if parsed.scheme in ("http", "https") and parsed.hostname else None
    except ValueError:
        return None


def _matches_kind(venue_kind: str, selected_kind: str) -> bool:
    """Filter places independently from a special's food/drink category."""
    if selected_kind == "food":
        return venue_kind in ("restaurant", "bar_restaurant")
    if selected_kind == "drinks":
        return venue_kind in ("bar", "bar_restaurant")
    return True


def build_map_data(
    config: Config, day_data: dict[str, Any], day: date, *, filters_active: bool = False,
    query: str = "", kind: str = "all",
) -> dict[str, Any]:
    """Build map metadata from the same filtered deal views shown in the list.

    Unconfirmed and undated specials provide context, but never contribute to
    advertised prices or live deal counts. Venues without verified coordinates
    remain in the payload so the interface can explain why they lack a pin.
    Pass ``query`` only when price and category filters are absent, so a search
    can also discover places that have no collected specials.
    """
    confident: dict[str, list[dict]] = {}
    uncertain: dict[str, list[dict]] = {}
    for view in day_data.get("deals", []):
        confident.setdefault(view.get("venue_slug"), []).append(view)
    for view in day_data.get("uncertain", []) + day_data.get("undated", []):
        uncertain.setdefault(view.get("venue_slug"), []).append(view)

    is_today = bool(day_data.get("is_today", False))
    needle = query.strip().casefold()
    venues = []
    for venue in config.venues:
        if not _matches_kind(venue.kind, kind):
            continue
        deals = confident.get(venue.slug, [])
        unconfirmed = uncertain.get(venue.slug, [])
        matches_place = needle and (needle in venue.name.casefold()
                                    or needle in (venue.address or "").casefold())
        if filters_active and not (deals or unconfirmed or matches_place):
            continue

        prices = [v["price"] for v in deals
                  if isinstance(v.get("price"), (int, float))
                  and not isinstance(v["price"], bool)
                  and math.isfinite(v["price"]) and v["price"] >= 0]
        destination = None
        if venue.latitude is not None and venue.longitude is not None:
            destination = f"{venue.latitude},{venue.longitude}"
        elif venue.address:
            destination = f"{venue.name}, {venue.address}, Cincinnati, OH"
        directions = ("https://www.google.com/maps/dir/?" + urllib.parse.urlencode(
            {"api": 1, "destination": destination}
        )) if destination else None

        venues.append({
            "slug": venue.slug,
            "name": venue.name,
            "kind": venue.kind,
            "kind_label": venue.kind_label,
            "website_url": _official_website(venue.website),
            "address": venue.address,
            "latitude": venue.latitude,
            "longitude": venue.longitude,
            "location_source": venue.location_source,
            "cheapest": min(prices) if prices else None,
            "deal_count": len(deals),
            "on_now_count": sum(1 for v in deals if is_today and v.get("on_now", False)),
            "has_samples": any((v.get("verify") or {}).get("kind") == "sample"
                               for v in deals + unconfirmed),
            "unconfirmed_count": len(unconfirmed),
            "venue_url": "/venue/" + urllib.parse.quote(venue.slug, safe=""),
            "directions_url": directions,
        })

    mapped_count = sum(1 for venue in venues if venue["latitude"] is not None)
    return {
        "venues": venues,
        "mapped_count": mapped_count,
        "unmapped_count": len(venues) - mapped_count,
        "day": day.isoformat(),
        "is_today": is_today,
        "has_samples": any(venue["has_samples"] for venue in venues),
        "place_counts": {
            "total": len(venues),
            "food": sum(_matches_kind(venue["kind"], "food") for venue in venues),
            "drinks": sum(_matches_kind(venue["kind"], "drinks") for venue in venues),
        },
    }


def freshness(last_fetch: str | None, now: datetime) -> dict[str, Any]:
    """Describe how old the data is, for the staleness banner."""
    if not last_fetch:
        return {"known": False, "stale": True, "label": "never fetched", "days": None}
    try:
        fetched = datetime.fromisoformat(last_fetch)
    except ValueError:
        return {"known": False, "stale": True, "label": "unknown", "days": None}

    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=now.tzinfo)
    delta = now - fetched.astimezone(now.tzinfo)
    seconds = max(delta.total_seconds(), 0)
    days = int(seconds // 86400)

    if seconds < 3600:
        label = "just now" if seconds < 300 else f"{int(seconds // 60)} min ago"
    elif seconds < 86400:
        hours = int(seconds // 3600)
        label = f"{hours} hour{'s' if hours != 1 else ''} ago"
    else:
        label = f"{days} day{'s' if days != 1 else ''} ago"

    return {"known": True, "stale": days >= STALE_AFTER_DAYS, "label": label, "days": days}


# --------------------------------------------------------------------------- #
# app
# --------------------------------------------------------------------------- #


def create_app(config: Config | None = None) -> Flask:
    """Build the Flask application.

    Args:
        config: Loaded config; read from ``config.yaml`` when omitted.

    Returns:
        A configured :class:`flask.Flask` app.
    """
    config = config or get_config()
    logger = setup_logging(config)
    app = Flask(__name__)
    app.config["BARDEALS"] = config

    # ---------------------------------------------------------------- #
    # per-request connection
    # ---------------------------------------------------------------- #

    def connection() -> sqlite3.Connection:
        """Open (once per request) a connection to the deals database."""
        if "conn" not in g:
            g.conn = db.connect(config.db_path)
            db.init_db(g.conn)
        return g.conn

    @app.teardown_appcontext
    def _close(_exc: BaseException | None) -> None:
        conn = g.pop("conn", None)
        if conn is not None:
            conn.close()

    def now() -> datetime:
        """Current time in the bars' local timezone."""
        return datetime.now(tz=get_tz(config.timezone))

    def parse_day(raw: str | None) -> date:
        """Read a ``?day=YYYY-MM-DD`` parameter, defaulting to today."""
        if not raw:
            return now().date()
        try:
            return date.fromisoformat(raw)
        except ValueError:
            abort(400, "day must be YYYY-MM-DD")

    def day_strip(selected: date, today: date) -> list[dict]:
        """The horizontal day picker: today plus the next six days."""
        strip = []
        for offset in range(DAY_STRIP_LENGTH):
            day = today + timedelta(days=offset)
            if offset == 0:
                label, sub = "Today", day.strftime("%b %d")
            elif offset == 1:
                label, sub = "Tomorrow", day.strftime("%b %d")
            else:
                label, sub = day.strftime("%a"), day.strftime("%b %d")
            strip.append(
                {
                    "iso": day.isoformat(),
                    "label": label,
                    "sub": sub,
                    "selected": day == selected,
                }
            )
        return strip

    def read_filters() -> dict[str, Any]:
        """Read deal filters and the independent place-type filter."""
        raw_max = request.args.get("max", "").strip()
        try:
            max_price = float(raw_max) if raw_max else None
        except ValueError:
            max_price = None
        if max_price is not None and (not math.isfinite(max_price) or max_price < 0):
            max_price = None

        category = request.args.get("cat", "all").strip().lower()
        if category not in {key for key, _ in CATEGORY_LABELS}:
            category = "all"

        kind = request.args.get("kind", "all").strip().lower()
        if kind not in ("all", "food", "drinks"):
            kind = "all"

        query = request.args.get("q", "").strip()
        return {
            "max_price": max_price,
            "category": category,
            "query": query,
            "kind": kind,
            "active": max_price is not None or category != "all" or bool(query) or kind != "all",
        }

    def apply_filters(views: list[dict], filters: dict) -> list[dict]:
        """Narrow a list of deal views by the active filters.

        An unpriced deal ("half off wings") survives a price cap: we cannot
        prove it is over budget, and silently dropping it would hide a
        genuinely cheap offer.
        """
        result = views
        if filters["kind"] != "all":
            result = [v for v in result if _matches_kind(
                place.kind if (place := config.venue_by_slug(v["venue_slug"])) else "bar",
                filters["kind"],
            )]
        if filters["max_price"] is not None:
            cap = filters["max_price"]
            result = [v for v in result if v["price"] is None or v["price"] <= cap]
        if filters["category"] != "all":
            result = [v for v in result if v["category"] == filters["category"]]
        if filters["query"]:
            needle = filters["query"].lower()
            result = [
                v for v in result
                if needle in (v["deal_text"] or "").lower()
                or needle in (v["item"] or "").lower()
                or needle in (v["venue_name"] or "").lower()
            ]
        return result

    def group_by_time(views: list[dict]) -> list[dict]:
        """Split today's deals into on-now / later / anytime / finished.

        This is the actual mental model at 8pm: what can I walk into right now,
        what starts soon, and what is already over.
        """
        buckets = {
            "now": {"key": "now", "title": "On now", "deals": []},
            "later": {"key": "later", "title": "Starting later", "deals": []},
            "anytime": {"key": "anytime", "title": "Anytime today", "deals": []},
            "over": {"key": "over", "title": "Already finished", "deals": []},
        }
        for view in views:
            buckets[view["timing"]["status"]]["deals"].append(view)
        for bucket in buckets.values():
            bucket["deals"].sort(key=_cheapest_first)
            prices = [d["price"] for d in bucket["deals"] if d["price"] is not None]
            bucket["cheapest"] = min(prices) if prices else None
            bucket["urgent_count"] = sum(
                1 for d in bucket["deals"] if d["timing"].get("urgent")
            )
        return [b for b in buckets.values() if b["deals"]]

    def load_day(day: date, moment: datetime, filters: dict | None = None) -> dict[str, Any]:
        """Assemble everything the day view needs."""
        conn = connection()
        is_today = day == moment.date()
        rows = db.dedupe_rows(db.deals_active_on(conn, day))
        views = [deal_view(row, moment, is_today=is_today) for row in rows]

        confident = [v for v in views if v["confidence"] != "low"]
        uncertain = [v for v in views if v["confidence"] == "low"]

        # Counts describe the whole day, before filtering, so the header does
        # not claim a quiet night when the user simply narrowed the view.
        total_before_filters = len(confident)
        prices_before = [v["price"] for v in confident if v["price"] is not None]

        if filters:
            confident = apply_filters(confident, filters)
            uncertain = apply_filters(uncertain, filters)

        # Sort here rather than in the route, so the HTML page and the JSON API
        # agree on ordering and neither contradicts the "cheapest" field.
        confident.sort(key=_cheapest_first)
        uncertain.sort(key=_cheapest_first)

        undated_rows = db.dedupe_rows(db.undated_recent_deals(conn, day))
        undated = [deal_view(row, moment, is_today=is_today) for row in undated_rows]
        if filters:
            undated = apply_filters(undated, filters)

        with_deals = {v["venue_slug"] for v in confident}
        quiet = [v for v in config.venues if v.slug not in with_deals
                 and _matches_kind(v.kind, filters["kind"] if filters else "all")]

        prices = [v["price"] for v in confident if v["price"] is not None]
        return {
            "deals": confident,
            "uncertain": uncertain,
            "undated": undated,
            "quiet_venues": quiet,
            "cheapest": min(prices) if prices else None,
            "cheapest_overall": min(prices_before) if prices_before else None,
            "on_now_count": sum(1 for v in confident if v["timing"]["status"] == "now"),
            "venue_count": len(with_deals),
            "total_before_filters": total_before_filters,
            "is_today": is_today,
            "has_samples": any(v["verify"]["kind"] == "sample"
                               for v in confident + uncertain + undated),
        }

    def map_for_filters(data: dict, day: date, filters: dict) -> dict:
        """Use one map/directory filter contract for HTML and JSON routes."""
        deal_filters = filters["max_price"] is not None or filters["category"] != "all"
        return build_map_data(config, data, day,
                              filters_active=deal_filters or bool(filters["query"]),
                              query=filters["query"] if not deal_filters else "",
                              kind=filters["kind"])

    # ---------------------------------------------------------------- #
    # routes
    # ---------------------------------------------------------------- #

    @app.route("/")
    def index():
        """Deals for a chosen day, cheapest first or grouped by bar."""
        moment = now()
        today = moment.date()
        day = parse_day(request.args.get("day"))
        # "time" is the natural default on the day you are actually standing
        # in; on other days it degrades to plain cheapest-first.
        default_sort = "time" if day == today else "price"
        sort = request.args.get("sort", default_sort)
        if sort not in ("price", "bar", "time"):
            sort = default_sort
        if day != today and sort == "time":
            sort = "price"

        filters = read_filters()
        data = load_day(day, moment, filters)
        map_data = map_for_filters(data, day, filters)
        deals = data["deals"]
        is_today = data["is_today"]

        venues = group_by_venue(deals) if sort == "bar" else []
        # Time grouping only makes sense for today; on other days it would
        # claim things are "on now" that are simply scheduled.
        time_groups = group_by_time(deals) if (sort == "time" and is_today) else []

        conn = connection()
        return render_template(
            "index.html",
            day=day,
            today=today,
            is_today=is_today,
            sort=sort,
            deals=deals,
            venues=venues,
            time_groups=time_groups,
            filters=filters,
            categories=CATEGORY_LABELS,
            price_caps=PRICE_CAPS,
            uncertain=data["uncertain"],
            undated=data["undated"],
            quiet_venues=data["quiet_venues"],
            cheapest=data["cheapest"],
            on_now_count=data["on_now_count"],
            venue_count=data["venue_count"],
            total_before_filters=data["total_before_filters"],
            day_strip=day_strip(day, today),
            freshness=freshness(db.last_fetched_at(conn), moment),
            total_posts=db.count_posts(conn),
            total_deals=db.count_deals(conn),
            has_samples=data["has_samples"],
            map_data=map_data,
            directory=map_data["venues"],
            place_counts=map_data["place_counts"],
            catalogue=build_map_data(config, {}, day)["venues"],
            venue_address=config.venue_address,
            now=moment,
        )

    @app.route("/week")
    def week():
        """The next seven days at a glance, for planning rather than tonight."""
        moment = now()
        today = moment.date()
        start = parse_day(request.args.get("start"))
        filters = read_filters()

        days = []
        for offset in range(7):
            day = start + timedelta(days=offset)
            data = load_day(day, moment, filters)
            days.append(
                {
                    "date": day,
                    "is_today": day == today,
                    "label": "Today" if day == today
                    else ("Tomorrow" if day == today + timedelta(days=1)
                          else day.strftime("%A")),
                    "deals": data["deals"],
                    "cheapest": data["cheapest"],
                    "venue_count": data["venue_count"],
                }
            )

        conn = connection()
        return render_template(
            "week.html",
            days=days,
            start=start,
            today=today,
            filters=filters,
            categories=CATEGORY_LABELS,
            price_caps=PRICE_CAPS,
            freshness=freshness(db.last_fetched_at(conn), moment),
            total=sum(len(d["deals"]) for d in days),
            has_samples=any(v["verify"]["kind"] == "sample"
                            for d in days for v in d["deals"]),
            now=moment,
        )

    @app.route("/venue/<slug>")
    def venue(slug: str):
        """Everything known for a single place."""
        moment = now()
        conn = connection()
        place = config.venue_by_slug(slug)
        if place is None:
            abort(404)

        rows = db.dedupe_rows(db.deals_for_venue(conn, slug))
        views = [deal_view(row, moment, is_today=False) for row in rows]
        website_url = _official_website(place.website)
        if not place.instagram:
            for view in views:
                if view["verify"]["kind"] == "sample":
                    view["verify"].update(url=website_url,
                                          label="official website" if website_url else "sample data",
                                          title="Sample data; check the place's current offers")

        by_weekday: list[dict] = []
        for weekday in range(7):
            same = [v for v in views
                    if v["recurrence"] == "weekly" and v["day_of_week"] == weekday]
            by_weekday.append(
                {
                    "weekday": weekday,
                    "name": describe_day_of_week(weekday),
                    "deals": sorted(same, key=lambda d: (d["price"] is None, d["price"] or 0.0)),
                    "is_today": weekday == moment.weekday(),
                }
            )

        other = [v for v in views if v["recurrence"] != "weekly"]
        return render_template(
            "venue.html",
            venue_name=config.venue_name(slug),
            venue_slug=slug,
            address=config.venue_address(slug),
            website_url=website_url,
            instagram_url=(f"https://www.instagram.com/{urllib.parse.quote(place.slug, safe='')}/"
                           if place.instagram else None),
            by_weekday=by_weekday,
            other=other,
            total=len(views),
            has_samples=any(v["verify"]["kind"] == "sample" for v in views),
            freshness=freshness(db.last_fetched_at(conn), moment),
            now=moment,
        )

    @app.route("/api/map.json")
    def api_map():
        """Map venues for the selected day and existing browsing filters."""
        moment = now()
        day = parse_day(request.args.get("day"))
        filters = read_filters()
        data = load_day(day, moment, filters)
        return jsonify(map_for_filters(data, day, filters))

    @app.route("/api/deals.json")
    def api_deals():
        """Today's deals as JSON, for scripting or a widget."""
        moment = now()
        day = parse_day(request.args.get("day"))
        data = load_day(day, moment, read_filters())
        conn = connection()
        return jsonify(
            {
                "day": day.isoformat(),
                "generated_at": moment.isoformat(),
                "last_fetched_at": db.last_fetched_at(conn),
                "cheapest": data["cheapest"],
                "deals": data["deals"],
                "low_confidence": data["uncertain"],
                "undated": data["undated"],
                "quiet_venues": [{"slug": v.slug, "name": v.name} for v in data["quiet_venues"]],
            }
        )

    @app.route("/healthz")
    def healthz():
        """Liveness probe that also reports database reachability."""
        try:
            conn = connection()
            return jsonify({"ok": True, "posts": db.count_posts(conn),
                            "deals": db.count_deals(conn)})
        except sqlite3.Error as exc:
            logger.exception("health check failed: %s", exc)
            return jsonify({"ok": False, "error": str(exc)}), 500

    @app.errorhandler(404)
    def not_found(_exc):
        return render_template("error.html", code=404,
                               message="No such page."), 404

    @app.errorhandler(400)
    def bad_request(exc):
        return render_template("error.html", code=400,
                               message=getattr(exc, "description", "Bad request.")), 400

    @app.errorhandler(500)
    def server_error(_exc):
        logger.exception("unhandled error rendering a page")
        return render_template("error.html", code=500,
                               message="Something broke. Check data/bardeals.log."), 500

    @app.template_global()
    def browse_url(endpoint: str, **overrides: Any) -> str:
        """Keep browsing filters when changing views, with explicit date selection."""
        args = {key: request.args[key] for key in ("max", "cat", "q", "sort", "kind")
                if key in request.args}
        for key, value in overrides.items():
            if value in (None, "", "all"):
                args.pop(key, None)
            else:
                args[key] = value
        return url_for(endpoint, **args)

    @app.template_global()
    def page_url(**overrides: Any) -> str:
        """Current URL with some query parameters changed.

        Lets a filter link flip one thing (the category, say) while keeping the
        day, sort order and search box the user already chose. Passing an empty
        value clears that parameter.
        """
        args = dict(request.args)
        for key, value in overrides.items():
            if value in (None, "", "all"):
                args.pop(key, None)
            else:
                args[key] = value
        return url_for(request.endpoint, **args)

    @app.template_filter("weekday_name")
    def _weekday_name(value: int | None) -> str:
        return describe_day_of_week(value)

    return app


def run(host: str = "127.0.0.1", port: int = 5000, debug: bool = False) -> None:
    """Start the development server.

    Args:
        host: Interface to bind. Defaults to loopback only.
        port: TCP port.
        debug: Enable Flask's reloader and debugger.
    """
    create_app().run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    run(debug=True)
