#!/usr/bin/env python3
"""Daily entrypoint for cron / launchd / Task Scheduler.

Scrapes every configured venue, parses new posts, and writes today's report to
``data/reports/YYYY-MM-DD.md``.

This is deliberately not a daemon - it runs once and exits. Scheduling belongs
to cron. See the README for the crontab line.

    python run_daily.py
    python run_daily.py --fixtures      # offline dry run
    python run_daily.py --no-parse      # fetch only, skip LLM spend

Exit codes:
    0  the run completed (individual account or post failures are tolerated)
    1  the run could not complete at all (bad config, unwritable database)
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from bardeals import db, report  # noqa: E402
from bardeals.config import get_config, load_config, setup_logging  # noqa: E402
from bardeals.dates import post_date  # noqa: E402
from bardeals.parse import DealParser  # noqa: E402
from bardeals.scrape import Scraper  # noqa: E402
from bardeals.websites import WebsiteScraper  # noqa: E402


def run(
    config_path: str | None = None,
    *,
    use_fixtures: bool = False,
    parse: bool = True,
    limit: int | None = None,
    day: date | None = None,
) -> int:
    """Execute one daily run.

    Every stage is wrapped so that a failure in one account, one post, or one
    LLM call is logged and stepped over rather than aborting the run.

    Args:
        config_path: Optional explicit path to config.yaml.
        use_fixtures: Read fixtures instead of hitting Instagram.
        parse: Whether to run LLM extraction on new posts.
        limit: Posts per account; falls back to config.
        day: Report date; defaults to today in the configured timezone.

    Returns:
        A process exit code.
    """
    started = time.time()
    try:
        config = load_config(config_path) if config_path else get_config()
        logger = setup_logging(config)
    except Exception as exc:  # noqa: BLE001
        print(f"bardeals: could not load configuration: {exc}", file=sys.stderr)
        return 1

    logger.info("=" * 70)
    logger.info("DAILY RUN starting (fixtures=%s, parse=%s)", use_fixtures, parse)

    try:
        conn = db.connect(config.db_path)
        db.init_db(conn)
    except Exception as exc:  # noqa: BLE001
        logger.exception("could not open the database at %s: %s", config.db_path, exc)
        return 1

    stored = parsed = deals_found = failures = 0

    try:
        known = db.known_shortcodes(conn)
        logger.info("%d post(s) already known - those are skipped entirely", len(known))

        scraper = Scraper(
            config, use_fixtures=use_fixtures, is_cached=lambda c: c in known, logger=logger
        )
        parser = DealParser(config, logger=logger) if parse else None

        sources = list(scraper.fetch_all(None, limit))

        # Bar websites carry the standing weekly specials that never make it to
        # Instagram. Failures here are logged and stepped over like any other.
        web_scraper = None
        if config.web.enabled and not use_fixtures:
            try:
                web_scraper = WebsiteScraper(
                    config, is_cached=lambda c: c in known, logger=logger
                )
                sources.extend(web_scraper.fetch_all())
            except Exception as exc:  # noqa: BLE001
                logger.exception("website stage failed: %s", exc)

        # A cached page still reports its current URL; repair any that moved so
        # an "UNCHANGED" page cannot preserve a link that now 404s.
        if web_scraper:
            moved = sum(
                1 for shortcode, url in web_scraper.unchanged
                if db.refresh_permalink(conn, shortcode, url)
            )
            if moved:
                logger.info("refreshed %d moved page link(s)", moved)

        for post in sources:
            try:
                if db.insert_post(conn, post):
                    stored += 1
            except Exception as exc:  # noqa: BLE001
                logger.exception("could not store %s: %s", post.shortcode, exc)
                failures += 1
                continue

            if parser is None:
                continue

            try:
                result = parser.parse_and_store(post, conn)
                parsed += 1
                if result.ok:
                    deals_found += len(result.deals)
                else:
                    failures += 1
            except Exception as exc:  # noqa: BLE001
                logger.exception("parse blew up on %s: %s", post.shortcode, exc)
                failures += 1

        if scraper.skipped_accounts:
            logger.warning("accounts skipped this run: %s", ", ".join(scraper.skipped_accounts))
        if web_scraper and web_scraper.skipped_sites:
            logger.warning("sites skipped this run: %s", ", ".join(web_scraper.skipped_sites))
        logger.info(
            "instagram: %d request(s), websites: %d request(s)",
            scraper.limiter.total,
            web_scraper.requests if web_scraper else 0,
        )

    except Exception as exc:  # noqa: BLE001
        logger.exception("scrape/parse stage failed: %s", exc)
        failures += 1

    # The report is written even when the scrape failed, so a bad network day
    # still leaves a usable file describing what is already known.
    report_path: Path | None = None
    target_day = day or post_date(datetime.now().astimezone(), config.timezone)
    try:
        config.reports_dir.mkdir(parents=True, exist_ok=True)
        report_path = config.reports_dir / f"{target_day.isoformat()}.md"
        report_path.write_text(report.day_markdown(conn, config, target_day), encoding="utf-8")
        logger.info("wrote report %s", report_path)
    except Exception as exc:  # noqa: BLE001
        logger.exception("could not write the report: %s", exc)
        failures += 1

    elapsed = time.time() - started
    logger.info(
        "DAILY RUN done in %.1fs: %d new post(s), %d parsed, %d deal(s), %d failure(s)",
        elapsed, stored, parsed, deals_found, failures,
    )
    logger.info("=" * 70)

    print(
        f"bardeals: {stored} new post(s), {deals_found} deal(s), "
        f"{failures} failure(s) in {elapsed:.1f}s"
    )
    if report_path:
        print(f"bardeals: report at {report_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run once."""
    parser = argparse.ArgumentParser(description="Daily bardeals scrape, parse, and report.")
    parser.add_argument("--config", default=None, help="Path to config.yaml.")
    parser.add_argument("--fixtures", action="store_true", help="Offline run from fixtures.")
    parser.add_argument("--no-parse", action="store_true", help="Skip LLM extraction.")
    parser.add_argument("--limit", type=int, default=None, help="Posts per account.")
    parser.add_argument("--date", default=None, help="Report date (YYYY-MM-DD).")
    args = parser.parse_args(argv)

    day = date.fromisoformat(args.date) if args.date else None
    return run(
        args.config,
        use_fixtures=args.fixtures,
        parse=not args.no_parse,
        limit=args.limit,
        day=day,
    )


if __name__ == "__main__":
    raise SystemExit(main())
