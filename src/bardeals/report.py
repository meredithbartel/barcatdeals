"""Rendering deals for a given day, week, or venue.

Two output forms share one shape: rich tables for the terminal and Markdown
for the daily report file.

Three rules apply everywhere:

* every deal shows its source permalink, so any row can be verified by hand;
* low-confidence deals are shown dimmed in a separate section rather than
  hidden, because a vague "same deal as always" is still worth knowing about;
* soft duplicates are collapsed, so a bar that posts its Thursday special
  three times appears once.
"""

from __future__ import annotations

import sqlite3
from collections import OrderedDict
from datetime import date, timedelta
from typing import Iterable, Sequence

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import db
from .config import Config
from .dates import describe_day_of_week

LOW_CONFIDENCE = "low"


# These four are the single source of truth for how a deal reads to a human, and
# are shared by the terminal tables, the Markdown reports, and the web UI. They
# only need ``__getitem__``, so a sqlite3.Row or a plain dict both work.
#
# The ``placeholder`` argument is why they are parameterized rather than
# duplicated: a terminal table needs "-" to keep its columns aligned, while HTML
# wants an empty string so the template can omit the element entirely.


def format_price(row, placeholder: str = "-") -> str:
    """Format a deal's price, e.g. ``$2`` or ``$2.50``.

    Args:
        row: Anything indexable with a ``price`` key.
        placeholder: Returned when the deal has no numeric price.
    """
    price = row["price"]
    if price is None:
        return placeholder
    return f"${price:,.2f}".replace(".00", "")


def format_time(row, placeholder: str = "-") -> str:
    """Format a deal's time window, e.g. ``17:00-21:00`` or ``from 17:00``.

    Args:
        row: Anything indexable with ``start_time`` / ``end_time`` keys.
        placeholder: Returned when the deal states no times.
    """
    start, end = row["start_time"], row["end_time"]
    if start and end:
        return f"{start}-{end}"
    if start:
        return f"from {start}"
    if end:
        return f"until {end}"
    return placeholder


def format_when(row, unknown: str = "unknown") -> str:
    """Describe when a deal runs, e.g. ``every Thursday`` or ``Thu Sep 17``.

    Args:
        row: Anything indexable with recurrence/day_of_week/specific_date keys.
        unknown: Returned when the deal carries no usable date information.
    """
    recurrence = row["recurrence"]
    if recurrence == "daily":
        return "every day"
    if recurrence == "weekly":
        weekday = describe_day_of_week(row["day_of_week"])
        return f"every {weekday}" if weekday else "weekly"
    if row["specific_date"]:
        try:
            # %-d / %#d are platform-specific; zero-padded %d is portable.
            return date.fromisoformat(row["specific_date"]).strftime("%a %b %d")
        except (ValueError, TypeError):
            return str(row["specific_date"])
    return unknown


def source_label(row, long: bool = False) -> str:
    """Say where the deal was read from.

    Args:
        row: Anything indexable with a ``source`` key.
        long: Use the wordier phrasing the web UI shows ("from flyer") instead
            of the terse column label the terminal uses ("flyer").
    """
    # A website deal's `source` is always 'caption' (the page text), so the
    # useful distinction for the reader is which site it came from, not which
    # part of a post.
    try:
        if row["source_kind"] == "website":
            return "from website" if long else "web"
    except (KeyError, IndexError):
        pass

    table = (
        {"image": "from flyer", "caption": "from caption", "both": "flyer + caption"}
        if long
        else {"image": "flyer", "caption": "text", "both": "both"}
    )
    return table.get(row["source"], row["source"])


# Internal aliases kept so the rich/Markdown renderers below read unchanged.
_format_price = format_price
_format_time = format_time
_format_when = format_when
_source_label = source_label


def _split_by_confidence(
    rows: Sequence[sqlite3.Row],
) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    """Partition rows into (confident, low-confidence)."""
    confident = [row for row in rows if row["confidence"] != LOW_CONFIDENCE]
    uncertain = [row for row in rows if row["confidence"] == LOW_CONFIDENCE]
    return confident, uncertain


def group_by_venue(rows: Iterable[sqlite3.Row]) -> "OrderedDict[str, list[sqlite3.Row]]":
    """Group deal rows by venue name, preserving query order.

    Args:
        rows: Joined deal rows.

    Returns:
        Venue name -> its rows.
    """
    grouped: OrderedDict[str, list[sqlite3.Row]] = OrderedDict()
    for row in rows:
        grouped.setdefault(row["venue_name"], []).append(row)
    return grouped


def _deal_table(rows: Sequence[sqlite3.Row], *, dim: bool = False, show_when: bool = True) -> Table:
    """Build a rich table for one group of deals."""
    table = Table(
        box=None,
        pad_edge=False,
        show_edge=False,
        header_style="dim" if dim else "bold",
        style="dim" if dim else "",
    )
    table.add_column("Deal", overflow="fold", max_width=40)
    table.add_column("Price", justify="right")
    table.add_column("Time")
    if show_when:
        table.add_column("When")
    table.add_column("Src")
    table.add_column("Link", overflow="fold")

    for row in rows:
        cells = [
            row["deal_text"],
            _format_price(row),
            _format_time(row),
        ]
        if show_when:
            cells.append(_format_when(row))
        cells += [_source_label(row), row["permalink"]]
        table.add_row(*cells)
    return table


def render_day(
    conn: sqlite3.Connection, config: Config, day: date, *, console: Console | None = None
) -> None:
    """Print deals active on one day, grouped by bar.

    Args:
        conn: Open connection.
        config: Loaded config.
        day: The day to report on.
        console: Optional console; a fresh one is created otherwise.
    """
    console = console or Console()
    rows = db.dedupe_rows(db.deals_active_on(conn, day))
    # Undated recent deals cannot be placed on a calendar, but they are shown
    # dimmed rather than silently dropped.
    undated = db.dedupe_rows(db.undated_recent_deals(conn, day))

    console.print()
    console.rule(f"[bold]Deals for {day:%A, %B %d, %Y}")

    if not rows and not undated:
        console.print("\n[dim]No deals on record for this day.[/dim]\n")
        _print_empty_hint(conn, console)
        return

    for venue_name, venue_rows in group_by_venue(rows).items():
        confident, uncertain = _split_by_confidence(venue_rows)
        blocks = []
        if confident:
            blocks.append(_deal_table(confident))
        if uncertain:
            if confident:
                blocks.append(Text())
            blocks.append(Text("unconfirmed", style="dim italic"))
            blocks.append(_deal_table(uncertain, dim=True))
        console.print()
        console.print(Panel(Group(*blocks), title=f"[bold]{venue_name}", title_align="left"))

    if undated:
        console.print()
        console.print(Text("Undated - recently mentioned, no date given", style="dim italic"))
        console.print(_deal_table(undated, dim=True))

    console.print()
    console.print(
        f"[dim]{len(rows)} deal(s) across {len(group_by_venue(rows))} bar(s)"
        + (f", plus {len(undated)} undated." if undated else ".")
        + "[/dim]"
    )
    console.print()


def render_week(
    conn: sqlite3.Connection,
    config: Config,
    start: date,
    days: int = 7,
    *,
    console: Console | None = None,
) -> None:
    """Print the next ``days`` days of deals, grouped by day.

    Args:
        conn: Open connection.
        config: Loaded config.
        start: First day of the window.
        days: Number of days to cover.
        console: Optional console.
    """
    console = console or Console()
    console.print()
    console.rule(f"[bold]Deals {start:%b %d} - {start + timedelta(days=days - 1):%b %d, %Y}")

    total = 0
    for day, raw_rows in db.deals_for_week(conn, start, days):
        rows = db.dedupe_rows(raw_rows)
        console.print()
        heading = f"[bold]{day:%A %b %d}[/bold]"
        if not rows:
            console.print(f"{heading}  [dim]nothing on record[/dim]")
            continue

        total += len(rows)
        console.print(heading)
        for venue_name, venue_rows in group_by_venue(rows).items():
            confident, uncertain = _split_by_confidence(venue_rows)
            console.print(f"  [cyan]{venue_name}[/cyan]")
            if confident:
                console.print(_deal_table(confident, show_when=False))
            if uncertain:
                console.print(Text("  unconfirmed", style="dim italic"))
                console.print(_deal_table(uncertain, dim=True, show_when=False))

    console.print()
    console.print(f"[dim]{total} deal(s) over {days} days.[/dim]")
    console.print()


def render_venue(
    conn: sqlite3.Connection, config: Config, venue_slug: str, *, console: Console | None = None
) -> None:
    """Print everything known about one bar.

    Args:
        conn: Open connection.
        config: Loaded config.
        venue_slug: The bar's Instagram handle.
        console: Optional console.
    """
    console = console or Console()
    venue_name = config.venue_name(venue_slug)
    rows = db.deals_for_venue(conn, venue_slug)

    console.print()
    console.rule(f"[bold]{venue_name} [dim](@{venue_slug})")

    if not rows:
        console.print("\n[dim]No deals on record for this bar.[/dim]\n")
        _print_empty_hint(conn, console)
        return

    confident, uncertain = _split_by_confidence(db.dedupe_rows(rows))
    console.print()
    if confident:
        console.print(_deal_table(confident))
    if uncertain:
        console.print()
        console.print(Text("unconfirmed", style="dim italic"))
        console.print(_deal_table(uncertain, dim=True))

    console.print()
    console.print(f"[dim]{len(rows)} deal row(s) on record.[/dim]")
    console.print()


def _print_empty_hint(conn: sqlite3.Connection, console: Console) -> None:
    """Explain an empty report: no posts at all, or posts with no deals."""
    posts = db.count_posts(conn)
    if posts == 0:
        console.print("[dim]The database has no posts yet. Run `bardeals scrape` first.[/dim]\n")
    else:
        console.print(f"[dim]{posts} post(s) stored, {db.count_deals(conn)} deal(s) total.[/dim]\n")


# --------------------------------------------------------------------------- #
# markdown (for data/reports/YYYY-MM-DD.md)
# --------------------------------------------------------------------------- #


def day_markdown(conn: sqlite3.Connection, config: Config, day: date) -> str:
    """Render one day's deals as a Markdown document.

    Args:
        conn: Open connection.
        config: Loaded config.
        day: The day to report on.

    Returns:
        A Markdown string, suitable for ``data/reports/YYYY-MM-DD.md``.
    """
    rows = db.dedupe_rows(db.deals_active_on(conn, day))
    undated = db.dedupe_rows(db.undated_recent_deals(conn, day))
    lines = [f"# Deals for {day:%A, %B %d, %Y}", ""]

    if not rows and not undated:
        lines += ["_No deals on record for this day._", ""]
    else:
        for venue_name, venue_rows in group_by_venue(rows).items():
            confident, uncertain = _split_by_confidence(venue_rows)
            lines += [f"## {venue_name}", ""]
            if confident:
                lines += _markdown_table(confident)
            if uncertain:
                lines += ["", "<details><summary>Unconfirmed</summary>", ""]
                lines += _markdown_table(uncertain)
                lines += ["", "</details>", ""]
            lines.append("")

    if undated:
        lines += ["## Undated", "", "_Recently mentioned, but the post gave no date._", ""]
        lines += _markdown_table(undated)
        lines += [""]

    lines += [
        "---",
        "",
        f"_{len(rows)} deal(s) from {db.count_posts(conn)} stored post(s). "
        f"Generated by bardeals._",
        "",
    ]
    return "\n".join(lines)


def _markdown_table(rows: Sequence[sqlite3.Row]) -> list[str]:
    """Build a Markdown table for a group of deals."""
    lines = [
        "| Deal | Price | Time | When | Source | Link |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        deal_text = str(row["deal_text"]).replace("|", "\\|")
        lines.append(
            f"| {deal_text} | {_format_price(row)} | {_format_time(row)} "
            f"| {_format_when(row)} | {_source_label(row)} "
            f"| [post]({row['permalink']}) |"
        )
    return lines
