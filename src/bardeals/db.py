"""SQLite storage: schema, migrations, and insert/query helpers.

Plain ``sqlite3`` on purpose - the query shapes here are simple enough that an
ORM would only hide them.

Two tables:

``posts``
    One row per Instagram post we have seen. Its primary purpose beyond
    storage is caching: a shortcode present here is never refetched, never
    re-downloaded, and never sent to the LLM again.

``deals``
    Zero or more extracted deals per post. Most posts yield none.

Deduplication happens at two levels:

*Hard* - a unique index stops the same deal being inserted twice from the same
post, so re-parsing a post is idempotent.

*Soft* - :func:`find_soft_duplicates` and :func:`dedupe_rows` collapse the same
deal posted repeatedly across *different* posts, so a bar that posts its Thursday
wing special three times shows up once.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

RECURRENCES = ("once", "weekly", "daily", "unknown")
CONFIDENCES = ("high", "medium", "low")
SOURCES = ("caption", "image", "both")

_WHITESPACE = re.compile(r"\s+")


def normalize_text(text: str | None) -> str:
    """Collapse a deal description to a comparable key.

    Lowercased, whitespace-collapsed, trailing punctuation stripped. Used for
    both the hard unique index and soft cross-post dedupe so that
    ``"$2 Domestic  Drafts!"`` and ``"$2 domestic drafts"`` compare equal.
    """
    if not text:
        return ""
    return _WHITESPACE.sub(" ", text).strip().lower().rstrip(".!")


@dataclass
class Deal:
    """One extracted deal, as stored in the ``deals`` table."""

    shortcode: str
    venue_slug: str
    venue_name: str
    deal_text: str
    item: str | None = None
    price: float | None = None
    day_of_week: int | None = None
    specific_date: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    recurrence: str = "unknown"
    confidence: str = "medium"
    source: str = "caption"
    id: int | None = None
    created_at: str | None = None

    def __post_init__(self) -> None:
        """Coerce enum-ish fields to known values so bad LLM output can't poison the DB."""
        if self.recurrence not in RECURRENCES:
            self.recurrence = "unknown"
        if self.confidence not in CONFIDENCES:
            self.confidence = "low"
        if self.source not in SOURCES:
            self.source = "caption"
        if self.day_of_week is not None and not 0 <= int(self.day_of_week) <= 6:
            self.day_of_week = None

    @property
    def norm_text(self) -> str:
        """Normalized dedupe key for this deal's description."""
        return normalize_text(self.deal_text)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to plain values (for logging and fixtures)."""
        return asdict(self)


# --------------------------------------------------------------------------- #
# schema
# --------------------------------------------------------------------------- #

# Each entry is applied in order; PRAGMA user_version records how far we got.
MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS posts (
            shortcode   TEXT PRIMARY KEY,
            username    TEXT NOT NULL,
            caption     TEXT NOT NULL DEFAULT '',
            posted_at   TEXT NOT NULL,           -- ISO-8601 UTC
            permalink   TEXT NOT NULL,
            image_path  TEXT,
            fetched_at  TEXT NOT NULL,           -- ISO-8601 UTC
            parsed      INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_posts_username ON posts(username);
        CREATE INDEX IF NOT EXISTS idx_posts_parsed   ON posts(parsed);

        CREATE TABLE IF NOT EXISTS deals (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            shortcode     TEXT NOT NULL REFERENCES posts(shortcode) ON DELETE CASCADE,
            venue_slug    TEXT NOT NULL,
            venue_name    TEXT NOT NULL,
            deal_text     TEXT NOT NULL,
            norm_text     TEXT NOT NULL DEFAULT '',  -- internal dedupe key
            item          TEXT,
            price         REAL,
            day_of_week   INTEGER,                   -- 0=Mon .. 6=Sun
            specific_date TEXT,                      -- ISO date
            start_time    TEXT,                      -- HH:MM
            end_time      TEXT,                      -- HH:MM
            recurrence    TEXT NOT NULL DEFAULT 'unknown',
            confidence    TEXT NOT NULL DEFAULT 'medium',
            source        TEXT NOT NULL DEFAULT 'caption',
            created_at    TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_deals_shortcode ON deals(shortcode);
        CREATE INDEX IF NOT EXISTS idx_deals_venue     ON deals(venue_slug);
        CREATE INDEX IF NOT EXISTS idx_deals_date      ON deals(specific_date);
        CREATE INDEX IF NOT EXISTS idx_deals_dow       ON deals(day_of_week);

        -- Hard dedupe. COALESCE matters: SQLite treats NULLs in a UNIQUE index
        -- as distinct, so a plain UNIQUE(...) would happily insert the same
        -- nullable-date deal twice.
        CREATE UNIQUE INDEX IF NOT EXISTS idx_deals_unique ON deals(
            shortcode,
            norm_text,
            COALESCE(day_of_week, -1),
            COALESCE(specific_date, ''),
            COALESCE(start_time, ''),
            COALESCE(end_time, '')
        );
        """,
    ),
    (
        2,
        """
        -- Raw model output, kept alongside each parse for debugging bad
        -- extractions (see `bardeals explain`).
        CREATE TABLE IF NOT EXISTS parse_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            shortcode  TEXT NOT NULL,
            model      TEXT NOT NULL,
            raw_response TEXT NOT NULL,
            deal_count INTEGER NOT NULL DEFAULT 0,
            error      TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_parse_log_shortcode ON parse_log(shortcode);
        """,
    ),
    (
        3,
        """
        -- Where a post came from. Instagram carries what a bar is doing
        -- tonight; its website carries the standing weekly specials.
        ALTER TABLE posts ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'instagram';

        CREATE INDEX IF NOT EXISTS idx_posts_source ON posts(source_kind);
        """,
    ),
]


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open a connection with row access by name and foreign keys enforced.

    Args:
        db_path: Path to the SQLite file. Parent directories are created.

    Returns:
        An open :class:`sqlite3.Connection`.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> int:
    """Apply any pending migrations.

    Args:
        conn: Open connection.

    Returns:
        The schema version after migrating.
    """
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, script in MIGRATIONS:
        if version > current:
            conn.executescript(script)
            conn.execute(f"PRAGMA user_version = {version}")
            current = version
    conn.commit()
    return current


def schema_version(conn: sqlite3.Connection) -> int:
    """Current schema version recorded in the database."""
    return conn.execute("PRAGMA user_version").fetchone()[0]


# --------------------------------------------------------------------------- #
# posts
# --------------------------------------------------------------------------- #


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def post_exists(conn: sqlite3.Connection, shortcode: str) -> bool:
    """True if this shortcode has already been stored."""
    row = conn.execute("SELECT 1 FROM posts WHERE shortcode = ?", (shortcode,)).fetchone()
    return row is not None


def known_shortcodes(conn: sqlite3.Connection) -> set[str]:
    """Every shortcode already stored.

    Loaded once per run and used as the scraper's cache predicate, so the hot
    path is a set lookup rather than a query per post.
    """
    return {row[0] for row in conn.execute("SELECT shortcode FROM posts")}


def insert_post(conn: sqlite3.Connection, post: Any) -> bool:
    """Store a :class:`~bardeals.scrape.RawPost`, ignoring one already present.

    Args:
        conn: Open connection.
        post: A ``RawPost``-shaped object.

    Returns:
        True if a new row was inserted, False if it already existed.
    """
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO posts
            (shortcode, username, caption, posted_at, permalink, image_path,
             fetched_at, parsed, source_kind)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
        """,
        (
            post.shortcode,
            post.username,
            post.caption or "",
            post.posted_at.astimezone(timezone.utc).isoformat(),
            post.permalink,
            post.local_image_path,
            _utcnow(),
            getattr(post, "source_kind", "instagram"),
        ),
    )
    inserted = cursor.rowcount > 0
    if not inserted and post.permalink:
        # The row already exists, but its shortcode does not pin the URL: a
        # website shortcode is a hash of the page *text* (deliberately excluding
        # the URL, so a moved page is not mistaken for a new deal). A bar that
        # moves /specials to /happy-hour without changing a word would otherwise
        # keep every one of its deals pointing at a now-404 path forever.
        conn.execute(
            "UPDATE posts SET permalink = ? WHERE shortcode = ? AND permalink <> ?",
            (post.permalink, post.shortcode, post.permalink),
        )
    conn.commit()
    return inserted


def refresh_permalink(conn: sqlite3.Connection, shortcode: str, permalink: str) -> bool:
    """Point an already-stored post at its current URL.

    Used when the scraper sees a page it has cached: the content is unchanged so
    nothing needs re-parsing, but the page may have moved and the stored link
    would otherwise rot.

    Args:
        conn: Open connection.
        shortcode: The stored post's shortcode.
        permalink: The URL the page was just found at.

    Returns:
        True if the stored URL actually changed.
    """
    if not permalink:
        return False
    cursor = conn.execute(
        "UPDATE posts SET permalink = ? WHERE shortcode = ? AND permalink <> ?",
        (permalink, shortcode, permalink),
    )
    conn.commit()
    return cursor.rowcount > 0


def mark_parsed(conn: sqlite3.Connection, shortcode: str, parsed: bool = True) -> None:
    """Flag a post as having been through the LLM parser."""
    conn.execute("UPDATE posts SET parsed = ? WHERE shortcode = ?", (int(parsed), shortcode))
    conn.commit()


def get_post(conn: sqlite3.Connection, shortcode: str) -> sqlite3.Row | None:
    """Fetch one stored post, or None."""
    return conn.execute("SELECT * FROM posts WHERE shortcode = ?", (shortcode,)).fetchone()


def unparsed_posts(conn: sqlite3.Connection, limit: int | None = None) -> list[sqlite3.Row]:
    """Posts stored but not yet parsed, oldest first.

    Args:
        conn: Open connection.
        limit: Optional cap on rows returned.

    Returns:
        Rows from ``posts`` awaiting a parse.
    """
    sql = "SELECT * FROM posts WHERE parsed = 0 ORDER BY posted_at ASC"
    params: tuple = ()
    if limit:
        sql += " LIMIT ?"
        params = (limit,)
    return list(conn.execute(sql, params))


def count_posts(conn: sqlite3.Connection) -> int:
    """Total posts stored."""
    return conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]


# --------------------------------------------------------------------------- #
# deals
# --------------------------------------------------------------------------- #


def insert_deal(conn: sqlite3.Connection, deal: Deal) -> int | None:
    """Insert one deal, ignoring an exact repeat from the same post.

    Args:
        conn: Open connection.
        deal: The deal to store.

    Returns:
        The new row id, or None if the unique index rejected it as a duplicate.
    """
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO deals
            (shortcode, venue_slug, venue_name, deal_text, norm_text, item, price,
             day_of_week, specific_date, start_time, end_time,
             recurrence, confidence, source, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            deal.shortcode,
            deal.venue_slug,
            deal.venue_name,
            deal.deal_text,
            deal.norm_text,
            deal.item,
            deal.price,
            deal.day_of_week,
            deal.specific_date,
            deal.start_time,
            deal.end_time,
            deal.recurrence,
            deal.confidence,
            deal.source,
            deal.created_at or _utcnow(),
        ),
    )
    conn.commit()
    return cursor.lastrowid if cursor.rowcount > 0 else None


def insert_deals(conn: sqlite3.Connection, deals: Iterable[Deal]) -> int:
    """Insert many deals.

    Args:
        conn: Open connection.
        deals: Deals to store.

    Returns:
        How many were actually inserted (duplicates are not counted).
    """
    return sum(1 for deal in deals if insert_deal(conn, deal) is not None)


def delete_deals_for_post(conn: sqlite3.Connection, shortcode: str) -> int:
    """Remove every deal from one post, so it can be cleanly re-parsed.

    Returns:
        Number of rows deleted.
    """
    cursor = conn.execute("DELETE FROM deals WHERE shortcode = ?", (shortcode,))
    conn.commit()
    return cursor.rowcount


def count_deals(conn: sqlite3.Connection) -> int:
    """Total deals stored."""
    return conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0]


_DEAL_SELECT = """
    SELECT d.*, p.permalink, p.posted_at, p.image_path, p.caption, p.source_kind
    FROM deals d
    JOIN posts p ON p.shortcode = d.shortcode
"""


def deals_active_on(conn: sqlite3.Connection, day: date) -> list[sqlite3.Row]:
    """Deals running on a given date.

    A deal is active if its ``specific_date`` is that day, or it recurs weekly
    on that weekday, or it recurs daily.

    Args:
        conn: Open connection.
        day: The date to test.

    Returns:
        Matching joined rows, highest confidence first.
    """
    sql = (
        _DEAL_SELECT
        + """
        WHERE d.specific_date = :iso
           OR (d.recurrence = 'weekly' AND d.day_of_week = :dow)
           OR d.recurrence = 'daily'
        ORDER BY d.venue_name COLLATE NOCASE,
                 CASE d.confidence WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                 d.price IS NULL, d.price
        """
    )
    return list(conn.execute(sql, {"iso": day.isoformat(), "dow": day.weekday()}))


def undated_recent_deals(
    conn: sqlite3.Connection, day: date, window_days: int = 7
) -> list[sqlite3.Row]:
    """Recent deals that carry no date at all.

    A vague post - "same deal as always this week" - yields a deal with no
    specific_date, no weekday, and unknown recurrence. It cannot be placed on a
    calendar, but it was posted recently and is still worth surfacing, so the
    reports show it dimmed alongside the dated deals rather than dropping it.

    Args:
        conn: Open connection.
        day: The day being reported on.
        window_days: How far back a post may be and still count.

    Returns:
        Undated deal rows from posts within the window.
    """
    earliest = (day - timedelta(days=window_days)).isoformat()
    sql = (
        _DEAL_SELECT
        + """
        WHERE d.specific_date IS NULL
          AND d.day_of_week IS NULL
          AND d.recurrence NOT IN ('daily', 'weekly')
          AND date(p.posted_at) BETWEEN :earliest AND :latest
        ORDER BY d.venue_name COLLATE NOCASE, p.posted_at DESC
        """
    )
    return list(conn.execute(sql, {"earliest": earliest, "latest": day.isoformat()}))


def deals_for_venue(conn: sqlite3.Connection, venue_slug: str) -> list[sqlite3.Row]:
    """Every deal known for one venue, newest post first."""
    sql = (
        _DEAL_SELECT
        + """
        WHERE d.venue_slug = ? COLLATE NOCASE
        ORDER BY p.posted_at DESC, d.id ASC
        """
    )
    return list(conn.execute(sql, (venue_slug,)))


def deals_for_week(
    conn: sqlite3.Connection, start: date, days: int = 7
) -> list[tuple[date, list[sqlite3.Row]]]:
    """Active deals for each of ``days`` consecutive days.

    Args:
        conn: Open connection.
        start: First day of the window.
        days: How many days to cover.

    Returns:
        ``(date, rows)`` pairs in chronological order.
    """
    return [
        (start + timedelta(days=offset), deals_active_on(conn, start + timedelta(days=offset)))
        for offset in range(days)
    ]


def all_deals(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every stored deal, newest post first."""
    return list(conn.execute(_DEAL_SELECT + " ORDER BY p.posted_at DESC, d.id ASC"))


# --------------------------------------------------------------------------- #
# soft dedupe
# --------------------------------------------------------------------------- #


@dataclass
class DuplicateGroup:
    """A set of deal rows judged to be the same real-world special."""

    venue_slug: str
    norm_text: str
    rows: list[sqlite3.Row] = field(default_factory=list)

    @property
    def canonical(self) -> sqlite3.Row:
        """The row to actually display: the one from the most recent post."""
        return max(self.rows, key=lambda row: (row["posted_at"], row["id"]))

    @property
    def duplicate_ids(self) -> list[int]:
        """Ids of the rows suppressed in favour of :attr:`canonical`."""
        keep = self.canonical["id"]
        return sorted(row["id"] for row in self.rows if row["id"] != keep)


def _dates_overlap(left: sqlite3.Row, right: sqlite3.Row) -> bool:
    """Whether two deals describe an overlapping time window.

    Recurring deals overlap when they land on the same weekday (or either runs
    daily); one-off deals overlap when they share a date. A weekly deal also
    overlaps a one-off falling on its weekday, which is the common case of a
    bar announcing "tonight!" for a special that also runs every week.
    """
    if "daily" in (left["recurrence"], right["recurrence"]):
        return True

    left_dow, right_dow = left["day_of_week"], right["day_of_week"]
    left_date, right_date = left["specific_date"], right["specific_date"]

    if left_date and right_date:
        return left_date == right_date
    if left_dow is not None and right_dow is not None:
        return left_dow == right_dow

    # One recurring, one dated: compare the dated one's weekday.
    dow, iso = (left_dow, right_date) if left_dow is not None else (right_dow, left_date)
    if dow is not None and iso:
        try:
            return date.fromisoformat(iso).weekday() == dow
        except ValueError:
            return False

    # Not enough information either way - treat vague repeats as the same deal.
    return not (left_date or right_date or left_dow is not None or right_dow is not None)


def find_soft_duplicates(conn: sqlite3.Connection) -> list[DuplicateGroup]:
    """Flag the same deal repeated across different posts.

    Candidates share a venue and a normalized description; they are grouped
    only if their dates also overlap, so "$5 pitchers" on Monday and the same
    text on Friday stay separate deals.

    Args:
        conn: Open connection.

    Returns:
        One :class:`DuplicateGroup` per cluster of two or more rows.
    """
    sql = (
        _DEAL_SELECT
        + """
        WHERE (d.venue_slug, d.norm_text) IN (
            SELECT venue_slug, norm_text
            FROM deals
            GROUP BY venue_slug, norm_text
            HAVING COUNT(DISTINCT shortcode) > 1
        )
        ORDER BY d.venue_slug, d.norm_text, p.posted_at
        """
    )
    candidates: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in conn.execute(sql):
        candidates.setdefault((row["venue_slug"], row["norm_text"]), []).append(row)

    groups: list[DuplicateGroup] = []
    for (venue_slug, norm_text), rows in candidates.items():
        clusters: list[list[sqlite3.Row]] = []
        for row in rows:
            for cluster in clusters:
                if any(_dates_overlap(row, other) for other in cluster):
                    cluster.append(row)
                    break
            else:
                clusters.append([row])
        groups.extend(
            DuplicateGroup(venue_slug, norm_text, cluster)
            for cluster in clusters
            if len({r["shortcode"] for r in cluster}) > 1
        )
    return groups


def dedupe_rows(rows: Sequence[sqlite3.Row]) -> list[sqlite3.Row]:
    """Collapse repeats within a result set, keeping the newest post's row.

    Applied at render time so a bar posting the same special three times
    appears once, without deleting anything from the database.

    Args:
        rows: Joined deal rows, as returned by the query helpers.

    Returns:
        The same rows with soft duplicates removed, order preserved.
    """
    clusters: dict[tuple[str, str], list[list[sqlite3.Row]]] = {}
    for row in rows:
        key = (row["venue_slug"], row["norm_text"])
        for cluster in clusters.setdefault(key, []):
            if any(_dates_overlap(row, other) for other in cluster):
                cluster.append(row)
                break
        else:
            clusters[key].append([row])

    keep = {
        max(cluster, key=lambda row: (row["posted_at"], row["id"]))["id"]
        for group in clusters.values()
        for cluster in group
    }
    return [row for row in rows if row["id"] in keep]


# --------------------------------------------------------------------------- #
# parse log
# --------------------------------------------------------------------------- #


def log_parse(
    conn: sqlite3.Connection,
    shortcode: str,
    model: str,
    raw_response: str,
    deal_count: int = 0,
    error: str | None = None,
) -> int | None:
    """Record the model's raw output for one post.

    Kept so a bad extraction can be inspected later without re-running the
    model. Never raises - losing a debug record must not fail a run.

    Args:
        conn: Open connection.
        shortcode: The post that was parsed.
        model: Model id used.
        raw_response: Serialized model response.
        deal_count: How many deals came back.
        error: Error text, if the parse failed.

    Returns:
        The new row id, or None if the write failed.
    """
    try:
        cursor = conn.execute(
            """
            INSERT INTO parse_log (shortcode, model, raw_response, deal_count, error, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (shortcode, model, raw_response, deal_count, error, _utcnow()),
        )
        conn.commit()
        return cursor.lastrowid
    except sqlite3.Error:
        return None


def latest_parse_log(conn: sqlite3.Connection, shortcode: str) -> sqlite3.Row | None:
    """Most recent raw model response for a post, or None."""
    return conn.execute(
        "SELECT * FROM parse_log WHERE shortcode = ? ORDER BY id DESC LIMIT 1",
        (shortcode,),
    ).fetchone()


def last_fetched_at(conn: sqlite3.Connection) -> str | None:
    """Timestamp of the most recent successful fetch, or None.

    Drives the staleness banner in the web UI: with scraping frequently
    blocked, "how old is this data" is as important as the data itself.
    """
    row = conn.execute("SELECT MAX(fetched_at) FROM posts").fetchone()
    return row[0] if row and row[0] else None


def venues_with_deals_on(conn: sqlite3.Connection, day: date) -> set[str]:
    """Venue slugs having at least one deal active on ``day``."""
    return {row["venue_slug"] for row in deals_active_on(conn, day)}
