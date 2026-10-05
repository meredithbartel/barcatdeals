"""Configuration loading for bardeals.

Reads ``config.yaml`` (venues, pacing, model name, paths) and ``.env``
(``ANTHROPIC_API_KEY``). Nothing venue- or tuning-specific is hardcoded in
Python; this module is the single place that knows where those values live.
"""

from __future__ import annotations

import logging
import logging.handlers
import math
import os
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Project root = two levels above this file (src/bardeals/config.py).
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


def _coordinate(value: object, limit: float) -> float | None:
    """Accept only finite coordinates in their geographic range."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and -limit <= number <= limit else None


@dataclass(frozen=True)
class Venue:
    """A place we track, and where to look for its deals."""

    slug: str
    name: str
    website: str | None = None
    #: Street address, when known. Drives the map link in the web UI.
    address: str | None = None
    #: Extra URLs to always fetch, for pages link discovery cannot find.
    pages: tuple[str, ...] = ()
    #: Verified map position; an incomplete or invalid pair stays unplaced.
    latitude: float | None = None
    longitude: float | None = None
    location_source: str | None = None
    kind: str = "bar"
    #: Existing slugs are Instagram handles; other listings must opt out.
    instagram: bool = True

    def __post_init__(self) -> None:
        latitude = _coordinate(self.latitude, 90)
        longitude = _coordinate(self.longitude, 180)
        if latitude is None or longitude is None:
            latitude = longitude = None
        object.__setattr__(self, "latitude", latitude)
        object.__setattr__(self, "longitude", longitude)
        source = self.location_source
        object.__setattr__(self, "location_source",
                           source.strip() or None if isinstance(source, str) else None)
        kind = self.kind.strip().lower() if isinstance(self.kind, str) else "bar"
        object.__setattr__(self, "kind", kind if kind in ("bar", "restaurant", "bar_restaurant") else "bar")
        instagram = self.instagram
        if isinstance(instagram, str):
            instagram = instagram.strip().lower() not in ("false", "no", "off", "0")
        elif not isinstance(instagram, bool):
            instagram = True
        object.__setattr__(self, "instagram", instagram)

    @property
    def kind_label(self) -> str:
        return {"bar": "Bar", "restaurant": "Restaurant", "bar_restaurant": "Bar & restaurant"}[self.kind]

    @property
    def has_website(self) -> bool:
        """Whether there is anything on the web to fetch for this venue."""
        return bool((self.website and self.website.strip()) or self.pages)


@dataclass(frozen=True)
class ScrapeSettings:
    """Rate-limit and pacing knobs for the Instagram scraper."""

    posts_per_account: int = 10
    sleep_between_accounts: tuple[float, float] = (8.0, 15.0)
    sleep_between_posts: tuple[float, float] = (2.0, 5.0)
    max_requests_per_hour: int = 150
    max_retries_per_account: int = 3
    backoff_base_seconds: float = 30.0
    save_fixtures: bool = True


@dataclass(frozen=True)
class WebSettings:
    """Politeness and scope knobs for scraping bar websites."""

    enabled: bool = True
    respect_robots: bool = True
    delay_between_pages: tuple[float, float] = (1.5, 3.0)
    delay_between_sites: tuple[float, float] = (2.0, 5.0)
    max_pages_per_site: int = 5
    timeout: float = 20.0
    max_page_chars: int = 20000
    max_llm_chars: int = 8000
    follow_pdfs: bool = True
    max_pdf_pages: int = 8
    user_agent: str = "bardeals/0.1 (personal deal tracker)"
    fallback_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
    follow_keywords: tuple[str, ...] = ()
    skip_keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    """Fully resolved configuration for a bardeals run."""

    timezone: str
    model: str
    max_tokens: int
    db_path: Path
    images_dir: Path
    reports_dir: Path
    log_path: Path
    fixtures_dir: Path
    scrape: ScrapeSettings
    web: WebSettings = field(default_factory=WebSettings)
    pages_dir: Path | None = None
    venues: list[Venue] = field(default_factory=list)
    anthropic_api_key: str | None = None

    def venue_by_slug(self, slug: str) -> Venue | None:
        """Return the configured venue with this Instagram handle, or None."""
        for venue in self.venues:
            if venue.slug.lower() == slug.lower():
                return venue
        return None

    def venue_address(self, slug: str) -> str | None:
        """Street address for a handle, or None when it was never verified."""
        venue = self.venue_by_slug(slug)
        return venue.address if venue else None

    def venues_with_websites(self) -> list[Venue]:
        """Configured venues that have a website worth fetching."""
        return [venue for venue in self.venues if venue.has_website]

    def venue_name(self, slug: str) -> str:
        """Human-readable bar name for a handle, falling back to the handle."""
        venue = self.venue_by_slug(slug)
        return venue.name if venue else slug


def _resolve(root: Path, value: str) -> Path:
    """Resolve a config path relative to the project root unless absolute."""
    path = Path(value)
    return path if path.is_absolute() else (root / path)


def _pair(value: object, default: tuple[float, float]) -> tuple[float, float]:
    """Coerce a two-element YAML list into a (low, high) float tuple."""
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return (float(value[0]), float(value[1]))
    return default


def load_config(path: Path | str | None = None) -> Config:
    """Load configuration from ``config.yaml`` and ``.env``.

    Args:
        path: Optional explicit path to the YAML config. Defaults to
            ``config.yaml`` beside the project root.

    Returns:
        A fully populated :class:`Config`.

    Raises:
        FileNotFoundError: If the config file does not exist.
    """
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(f"config file not found: {config_path}")

    root = config_path.resolve().parent
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    load_dotenv(root / ".env")

    paths = raw.get("paths", {}) or {}
    llm = raw.get("llm", {}) or {}
    scrape_raw = raw.get("scrape", {}) or {}
    defaults = ScrapeSettings()

    scrape = ScrapeSettings(
        posts_per_account=int(scrape_raw.get("posts_per_account", defaults.posts_per_account)),
        sleep_between_accounts=_pair(
            scrape_raw.get("sleep_between_accounts"), defaults.sleep_between_accounts
        ),
        sleep_between_posts=_pair(
            scrape_raw.get("sleep_between_posts"), defaults.sleep_between_posts
        ),
        max_requests_per_hour=int(
            scrape_raw.get("max_requests_per_hour", defaults.max_requests_per_hour)
        ),
        max_retries_per_account=int(
            scrape_raw.get("max_retries_per_account", defaults.max_retries_per_account)
        ),
        backoff_base_seconds=float(
            scrape_raw.get("backoff_base_seconds", defaults.backoff_base_seconds)
        ),
        save_fixtures=bool(scrape_raw.get("save_fixtures", defaults.save_fixtures)),
    )

    venues = [
        Venue(
            slug=str(item["slug"]),
            name=str(item.get("name", item["slug"])),
            website=(str(item["website"]).strip() or None) if item.get("website") else None,
            address=(str(item["address"]).strip() or None) if item.get("address") else None,
            pages=tuple(str(u).strip() for u in (item.get("pages") or []) if str(u).strip()),
            latitude=item.get("latitude"),
            longitude=item.get("longitude"),
            location_source=item.get("location_source"),
            kind=item.get("kind", "bar"),
            instagram=item.get("instagram", True),
        )
        for item in raw.get("venues", []) or []
    ]

    web_raw = raw.get("web", {}) or {}
    web_defaults = WebSettings()
    web = WebSettings(
        enabled=bool(web_raw.get("enabled", web_defaults.enabled)),
        respect_robots=bool(web_raw.get("respect_robots", web_defaults.respect_robots)),
        delay_between_pages=_pair(
            web_raw.get("delay_between_pages"), web_defaults.delay_between_pages
        ),
        delay_between_sites=_pair(
            web_raw.get("delay_between_sites"), web_defaults.delay_between_sites
        ),
        max_pages_per_site=int(web_raw.get("max_pages_per_site", web_defaults.max_pages_per_site)),
        timeout=float(web_raw.get("timeout", web_defaults.timeout)),
        max_page_chars=int(web_raw.get("max_page_chars", web_defaults.max_page_chars)),
        max_llm_chars=int(web_raw.get("max_llm_chars", web_defaults.max_llm_chars)),
        follow_pdfs=bool(web_raw.get("follow_pdfs", web_defaults.follow_pdfs)),
        max_pdf_pages=int(web_raw.get("max_pdf_pages", web_defaults.max_pdf_pages)),
        user_agent=str(web_raw.get("user_agent", web_defaults.user_agent)),
        fallback_user_agent=str(
            web_raw.get("fallback_user_agent", web_defaults.fallback_user_agent)
        ),
        follow_keywords=tuple(str(k).lower() for k in web_raw.get("follow_keywords", []) or []),
        skip_keywords=tuple(str(k).lower() for k in web_raw.get("skip_keywords", []) or []),
    )

    return Config(
        timezone=str(raw.get("timezone", "America/New_York")),
        model=str(llm.get("model", "claude-sonnet-4-5")),
        max_tokens=int(llm.get("max_tokens", 2048)),
        # BARDEALS_DB lets a deployment serve a published snapshot instead.
        db_path=_resolve(root, os.environ.get("BARDEALS_DB") or str(paths.get("db", "data/bardeals.db"))),
        images_dir=_resolve(root, str(paths.get("images", "data/images"))),
        reports_dir=_resolve(root, str(paths.get("reports", "data/reports"))),
        log_path=_resolve(root, str(paths.get("log", "data/bardeals.log"))),
        fixtures_dir=_resolve(root, str(paths.get("fixtures", "tests/fixtures"))),
        scrape=scrape,
        web=web,
        pages_dir=_resolve(root, str(paths.get("pages", "data/pages"))),
        venues=venues,
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
    )


@lru_cache(maxsize=1)
def get_config() -> Config:
    """Return the process-wide config, loading it on first use."""
    return load_config()


def enable_utf8_output() -> None:
    """Force UTF-8 on stdout/stderr.

    Windows consoles default to cp1252, which raises ``UnicodeEncodeError`` on
    the emoji that fill bar captions. Replacing unencodable characters is
    always preferable to crashing a run over a beer glass.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


_LOG_CONFIGURED = False


def setup_logging(config: Config | None = None, verbose: bool = False) -> logging.Logger:
    """Configure structured logging to ``data/bardeals.log`` with rotation.

    Safe to call repeatedly; handlers are only attached once.

    Args:
        config: Config supplying the log path. Loaded if omitted.
        verbose: Also echo DEBUG-level records to stderr.

    Returns:
        The ``bardeals`` package logger.
    """
    global _LOG_CONFIGURED
    enable_utf8_output()
    config = config or get_config()
    logger = logging.getLogger("bardeals")

    if not _LOG_CONFIGURED:
        config.log_path.parent.mkdir(parents=True, exist_ok=True)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False

        file_handler = logging.handlers.RotatingFileHandler(
            config.log_path, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S%z",
            )
        )
        logger.addHandler(file_handler)

        stream_handler = logging.StreamHandler()
        stream_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
        stream_handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s", "%H:%M:%S"))
        logger.addHandler(stream_handler)
        _LOG_CONFIGURED = True
    elif verbose:
        for handler in logger.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(
                handler, logging.handlers.RotatingFileHandler
            ):
                handler.setLevel(logging.DEBUG)

    return logger
