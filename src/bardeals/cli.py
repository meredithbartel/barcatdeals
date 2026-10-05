"""Command-line entrypoints.

    bardeals init-db
    bardeals scrape [--venue X] [--fixtures] [--no-parse]
    bardeals today
    bardeals week
    bardeals venue X
    bardeals explain SHORTCODE

``today`` and ``week`` are the main output.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import click
from rich.console import Console

from . import db, report
from .config import Config, get_config, load_config, setup_logging
from .dates import DEFAULT_TZ, post_date
from .parse import DealParser
from .scrape import RawPost, Scraper
from .websites import WebsiteScraper

console = Console()


def _open(config: Config) -> sqlite3.Connection:
    """Open the database, applying migrations first."""
    conn = db.connect(config.db_path)
    db.init_db(conn)
    return conn


def _today(config: Config) -> date:
    """Today's date in the bars' local timezone."""
    return post_date(datetime.now(tz=None).astimezone(), config.timezone)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--config", "config_path", type=click.Path(exists=True, dir_okay=False),
              help="Path to config.yaml (defaults to the project root).")
@click.option("-v", "--verbose", is_flag=True, help="Debug logging to the terminal.")
@click.pass_context
def cli(ctx: click.Context, config_path: str | None, verbose: bool) -> None:
    """Track drink and food deals posted by Cincinnati-area bars on Instagram."""
    config = load_config(config_path) if config_path else get_config()
    ctx.obj = {"config": config, "logger": setup_logging(config, verbose=verbose)}


@cli.command("init-db")
@click.pass_context
def init_db_command(ctx: click.Context) -> None:
    """Create the database and apply migrations."""
    config: Config = ctx.obj["config"]
    conn = _open(config)
    console.print(
        f"[green]Database ready[/green] at {config.db_path} "
        f"(schema v{db.schema_version(conn)})"
    )
    console.print(f"[dim]{db.count_posts(conn)} post(s), {db.count_deals(conn)} deal(s).[/dim]")
    conn.close()


@cli.command()
@click.option("--venue", multiple=True, help="Venue slug to scrape (repeatable).")
@click.option("--limit", type=int, default=None, help="Posts per account.")
@click.option("--fixtures", is_flag=True, help="Read tests/fixtures/*.json instead of Instagram.")
@click.option("--no-parse", is_flag=True, help="Fetch and store posts without calling the LLM.")
@click.option(
    "--source",
    type=click.Choice(["all", "instagram", "web"]),
    default="all",
    show_default=True,
    help="Which sources to read: Instagram posts, bar websites, or both.",
)
@click.pass_context
def scrape(
    ctx: click.Context,
    venue: tuple[str, ...],
    limit: int | None,
    fixtures: bool,
    no_parse: bool,
    source: str,
) -> None:
    """Fetch new posts and pages, extract deals, and store both.

    Content already in the database is skipped entirely: no refetch, no image
    download, and no LLM call. For websites the cache key is a hash of the
    page's text, so an unchanged page costs nothing.
    """
    config: Config = ctx.obj["config"]
    logger = ctx.obj["logger"]
    conn = _open(config)

    known = db.known_shortcodes(conn)
    cached = lambda code: code in known  # noqa: E731
    parser = None if no_parse else DealParser(config, logger=logger)

    stored = deals_found = failures = 0
    posts: list[RawPost] = []
    scraper = None
    web_scraper = None

    if source in ("all", "instagram"):
        scraper = Scraper(config, use_fixtures=fixtures, is_cached=cached, logger=logger)
        with console.status("[cyan]Fetching Instagram posts...", spinner="dots"):
            posts.extend(scraper.fetch_all(venue or None, limit))

    if source in ("all", "web") and config.web.enabled and not fixtures:
        targets = [v for v in config.venues_with_websites()
                   if not venue or v.slug in venue]
        if targets:
            web_scraper = WebsiteScraper(config, is_cached=cached, logger=logger)
            with console.status("[cyan]Fetching bar websites...", spinner="dots"):
                posts.extend(web_scraper.fetch_all(targets))
        elif venue:
            console.print("[dim]No websites configured for the selected venue(s).[/dim]")

    # A cached page still tells us where it lives now. Repair any stored URL
    # that has moved, otherwise "UNCHANGED" quietly preserves a dead link.
    moved = 0
    if web_scraper:
        for shortcode, url in web_scraper.unchanged:
            if db.refresh_permalink(conn, shortcode, url):
                moved += 1
        if moved:
            console.print(f"[dim]Updated {moved} moved page link(s).[/dim]")

    if not posts:
        console.print("[yellow]Nothing new.[/yellow]")
        if scraper and scraper.skipped_accounts:
            console.print(f"[dim]Skipped accounts: {', '.join(scraper.skipped_accounts)}[/dim]")
        if web_scraper and web_scraper.skipped_sites:
            console.print(f"[dim]Skipped sites: {', '.join(web_scraper.skipped_sites)}[/dim]")
        conn.close()
        return

    for post in posts:
        try:
            if db.insert_post(conn, post):
                stored += 1
        except sqlite3.Error as exc:
            logger.exception("could not store %s: %s", post.shortcode, exc)
            failures += 1
            continue

        if parser is None:
            continue

        result = parser.parse_and_store(post, conn)
        if result.ok:
            deals_found += len(result.deals)
            label = f"{len(result.deals)} deal(s)" if result.deals else "no deals"
            marker = "web" if getattr(post, "source_kind", "") == "website" else "ig"
            console.print(f"  [dim]({marker}) {post.username}/{post.shortcode}[/dim] {label}")
        else:
            failures += 1
            console.print(f"  [red]{post.username}/{post.shortcode} parse failed[/red]")

    console.print()
    from_web = sum(1 for p in posts if getattr(p, "source_kind", "") == "website")
    console.print(
        f"[green]{stored} new item(s)[/green] "
        f"[dim]({len(posts) - from_web} from Instagram, {from_web} from websites)[/dim], "
        f"[green]{deals_found} deal(s)[/green]"
        + (f", [red]{failures} failure(s)[/red]" if failures else "")
    )
    if scraper and scraper.skipped_accounts:
        console.print(f"[dim]Skipped accounts: {', '.join(scraper.skipped_accounts)}[/dim]")
    if web_scraper and web_scraper.skipped_sites:
        console.print(f"[dim]Skipped sites: {', '.join(web_scraper.skipped_sites)}[/dim]")

    duplicates = db.find_soft_duplicates(conn)
    if duplicates:
        console.print(
            f"[dim]{len(duplicates)} repeated deal(s) collapsed in reports "
            f"(see `bardeals venue`).[/dim]"
        )
    conn.close()


@cli.command()
@click.option("--date", "day", type=click.DateTime(formats=["%Y-%m-%d"]), default=None,
              help="Report on a specific date instead of today.")
@click.pass_context
def today(ctx: click.Context, day: datetime | None) -> None:
    """Show deals active today, grouped by bar."""
    config: Config = ctx.obj["config"]
    conn = _open(config)
    report.render_day(conn, config, day.date() if day else _today(config), console=console)
    conn.close()


@cli.command()
@click.option("--days", type=int, default=7, show_default=True, help="Days to cover.")
@click.option("--start", type=click.DateTime(formats=["%Y-%m-%d"]), default=None,
              help="First day of the window (defaults to today).")
@click.pass_context
def week(ctx: click.Context, days: int, start: datetime | None) -> None:
    """Show the next 7 days of deals, grouped by day."""
    config: Config = ctx.obj["config"]
    conn = _open(config)
    report.render_week(
        conn, config, start.date() if start else _today(config), days, console=console
    )
    conn.close()


@cli.command()
@click.argument("venue_slug")
@click.pass_context
def venue(ctx: click.Context, venue_slug: str) -> None:
    """Show everything known for one bar."""
    config: Config = ctx.obj["config"]
    conn = _open(config)
    report.render_venue(conn, config, venue_slug, console=console)
    conn.close()


@cli.command()
@click.pass_context
def venues(ctx: click.Context) -> None:
    """List the configured bars."""
    config: Config = ctx.obj["config"]
    console.print()
    for item in config.venues:
        console.print(f"  [cyan]{item.slug:<22}[/cyan] {item.name}")
    console.print(f"\n[dim]{len(config.venues)} venue(s) from config.yaml[/dim]\n")


@cli.command()
@click.argument("shortcode")
@click.option("--reparse", is_flag=True, help="Call the model again instead of showing the last run.")
@click.pass_context
def explain(ctx: click.Context, shortcode: str, reparse: bool) -> None:
    """Debug one post's parse: caption, image path, and raw model output."""
    config: Config = ctx.obj["config"]
    logger = ctx.obj["logger"]
    conn = _open(config)

    row = db.get_post(conn, shortcode)
    if row is None:
        console.print(f"[red]No stored post with shortcode {shortcode}.[/red]")
        console.print("[dim]Run `bardeals scrape` first, or check the shortcode.[/dim]")
        conn.close()
        raise SystemExit(1)

    console.print()
    console.rule(f"[bold]{config.venue_name(row['username'])} / {shortcode}")
    console.print(f"[bold]Permalink:[/bold]  {row['permalink']}")
    console.print(f"[bold]Posted at:[/bold]  {row['posted_at']}")
    console.print(f"[bold]Image:[/bold]      {row['image_path'] or '(none)'}")
    if row["image_path"] and not Path(row["image_path"]).exists():
        console.print("[yellow]            image file is missing from disk[/yellow]")
    console.print(f"[bold]Parsed:[/bold]     {'yes' if row['parsed'] else 'no'}")

    console.print("\n[bold]Caption[/bold]")
    console.print(row["caption"] or "[dim](no caption)[/dim]")

    if reparse:
        console.print("\n[bold]Re-running the parser...[/bold]")
        post = RawPost(
            shortcode=row["shortcode"],
            username=row["username"],
            caption=row["caption"],
            posted_at=datetime.fromisoformat(row["posted_at"]),
            image_url=None,
            local_image_path=row["image_path"],
            permalink=row["permalink"],
            is_video=False,
            typename="GraphImage",
        )
        result = DealParser(config, logger=logger).parse_post(post)
        console.print(f"\n[bold]Extracted {len(result.deals)} deal(s)[/bold]")
        for deal in result.deals:
            console.print(f"  - {json.dumps(deal.to_dict(), default=str)}")
        if result.error:
            console.print(f"[red]Error: {result.error}[/red]")
        raw = result.raw_response
    else:
        logged = db.latest_parse_log(conn, shortcode)
        if logged is None:
            console.print("\n[yellow]No parse on record. Use --reparse to run one.[/yellow]\n")
            conn.close()
            return
        console.print(
            f"\n[bold]Last parse:[/bold] {logged['created_at']} via {logged['model']} "
            f"-> {logged['deal_count']} deal(s)"
        )
        if logged["error"]:
            console.print(f"[red]Error: {logged['error']}[/red]")
        raw = logged["raw_response"]

        stored = [r for r in db.deals_for_venue(conn, row["username"])
                  if r["shortcode"] == shortcode]
        if stored:
            console.print("\n[bold]Stored deals[/bold]")
            for deal_row in stored:
                console.print(
                    f"  - {deal_row['deal_text']} "
                    f"[dim](recurrence={deal_row['recurrence']}, "
                    f"dow={deal_row['day_of_week']}, date={deal_row['specific_date']}, "
                    f"conf={deal_row['confidence']}, src={deal_row['source']})[/dim]"
                )

    console.print("\n[bold]Raw model response[/bold]")
    try:
        console.print_json(raw or "{}")
    except (ValueError, TypeError):
        console.print(raw or "[dim](empty)[/dim]")
    console.print()
    conn.close()


@cli.command("report")
@click.option("--date", "day", type=click.DateTime(formats=["%Y-%m-%d"]), default=None)
@click.pass_context
def report_command(ctx: click.Context, day: datetime | None) -> None:
    """Write today's report to data/reports/YYYY-MM-DD.md."""
    config: Config = ctx.obj["config"]
    conn = _open(config)
    target_day = day.date() if day else _today(config)
    config.reports_dir.mkdir(parents=True, exist_ok=True)
    path = config.reports_dir / f"{target_day.isoformat()}.md"
    path.write_text(report.day_markdown(conn, config, target_day), encoding="utf-8")
    console.print(f"[green]Wrote[/green] {path}")
    conn.close()


@cli.command()
@click.option("--host", default="127.0.0.1", show_default=True,
              help="Interface to bind. Loopback only by default.")
@click.option("--port", default=8080, show_default=True, type=int,
              help="Port to serve on. Browser-blocked ports are refused.")
@click.option("--debug", is_flag=True, help="Flask reloader and debugger.")
@click.pass_context
def serve(ctx: click.Context, host: str, port: int, debug: bool) -> None:
    """Start the local web UI for browsing deals."""
    config: Config = ctx.obj["config"]
    try:
        from .web import SUGGESTED_PORTS, create_app, port_is_browser_safe
    except ImportError as exc:  # pragma: no cover - depends on the install
        console.print(f"[red]Flask is not installed:[/red] {exc}")
        console.print('[dim]Install it with: pip install -e ".[web]"[/dim]')
        raise SystemExit(1)

    # Serving on one of these works, but every browser refuses to load it with a
    # bare ERR_UNSAFE_PORT that gives no clue the port is at fault.
    if not port_is_browser_safe(port):
        console.print(f"[red]Port {port} is blocked by web browsers[/red] "
                      "(ERR_UNSAFE_PORT).")
        console.print(
            "[dim]It belongs to a protocol browsers refuse to speak HTTP to. "
            f"Try one of: {', '.join(str(p) for p in SUGGESTED_PORTS)}.[/dim]"
        )
        raise SystemExit(2)

    conn = _open(config)
    posts, deals = db.count_posts(conn), db.count_deals(conn)
    conn.close()

    console.print()
    console.print(f"[bold green]Bar deals[/bold green] -> [link]http://{host}:{port}[/link]")
    console.print(f"[dim]{posts} post(s), {deals} deal(s) in {config.db_path.name}[/dim]")
    if posts == 0:
        console.print("[yellow]Database is empty.[/yellow] "
                      "[dim]Run `bardeals scrape` or `python seed_demo.py`.[/dim]")
    console.print("[dim]Ctrl+C to stop.[/dim]")
    console.print()

    create_app(config).run(host=host, port=port, debug=debug)


@cli.command()
@click.pass_context
def duplicates(ctx: click.Context) -> None:
    """List deals that look like repeats of each other across posts."""
    config: Config = ctx.obj["config"]
    conn = _open(config)
    groups = db.find_soft_duplicates(conn)

    console.print()
    if not groups:
        console.print("[dim]No likely duplicates found.[/dim]\n")
        conn.close()
        return

    for group in groups:
        console.print(f"[cyan]{config.venue_name(group.venue_slug)}[/cyan]: {group.norm_text}")
        for row in sorted(group.rows, key=lambda r: r["posted_at"], reverse=True):
            marker = "keep" if row["id"] == group.canonical["id"] else "dupe"
            style = "green" if marker == "keep" else "dim"
            console.print(f"  [{style}]{marker}[/{style}] {row['posted_at'][:10]} "
                          f"{row['permalink']}")
        console.print()
    console.print(f"[dim]{len(groups)} duplicate group(s).[/dim]\n")
    conn.close()


if __name__ == "__main__":
    cli()
