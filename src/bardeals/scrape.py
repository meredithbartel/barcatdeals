"""Instagram scraping layer.

Wraps :mod:`instaloader` in anonymous (no-login) mode and turns bar accounts
into :class:`RawPost` records with a locally downloaded image.

Anonymous scraping is the fragile part of this tool, so pacing is enforced
here rather than left to the caller:

* a random sleep between accounts and between individual posts,
* a hard rolling-hour request ceiling,
* exponential backoff on ``ConnectionException`` / ``TooManyRequestsException``,
* a per-account failure budget, after which the account is skipped so one dead
  handle can never abort the run.

Every request is logged with a timestamp so the real pacing is auditable after
the fact in ``data/bardeals.log``.

Runnable standalone::

    python src/bardeals/scrape.py --venue murphyspubcincy --limit 3
    python src/bardeals/scrape.py --fixtures
"""

from __future__ import annotations

import json
import random
import shutil
import time
import urllib.request
from collections import deque
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator

import instaloader
from instaloader.exceptions import (
    ConnectionException,
    InstaloaderException,
    ProfileNotExistsException,
    TooManyRequestsException,
)

try:  # package import
    from .config import Config, ScrapeSettings, get_config, setup_logging
except ImportError:  # executed directly as a script: python src/bardeals/scrape.py
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from bardeals.config import Config, ScrapeSettings, get_config, setup_logging

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


@dataclass
class RawPost:
    """One Instagram post, before any LLM parsing has happened."""

    shortcode: str
    username: str
    caption: str
    posted_at: datetime
    image_url: str | None
    local_image_path: str | None
    permalink: str
    is_video: bool
    typename: str
    #: Where this record came from: "instagram" or "website".
    source_kind: str = "instagram"

    def to_dict(self) -> dict:
        """Serialize to JSON-safe primitives (for fixtures)."""
        data = asdict(self)
        data["posted_at"] = self.posted_at.astimezone(timezone.utc).isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "RawPost":
        """Rebuild a post from its :meth:`to_dict` form."""
        posted_at = datetime.fromisoformat(str(data["posted_at"]))
        if posted_at.tzinfo is None:
            posted_at = posted_at.replace(tzinfo=timezone.utc)
        return cls(
            shortcode=str(data["shortcode"]),
            username=str(data["username"]),
            caption=str(data.get("caption") or ""),
            posted_at=posted_at.astimezone(timezone.utc),
            image_url=data.get("image_url"),
            local_image_path=data.get("local_image_path"),
            permalink=str(data.get("permalink") or permalink_for(data["shortcode"])),
            is_video=bool(data.get("is_video", False)),
            typename=str(data.get("typename") or "GraphImage"),
            source_kind=str(data.get("source_kind") or "instagram"),
        )


def permalink_for(shortcode: str) -> str:
    """Public web URL for a post shortcode."""
    return f"https://www.instagram.com/p/{shortcode}/"


class RateLimiter:
    """Rolling-hour request ceiling plus timestamped request logging.

    Instagram's anonymous endpoints tolerate a low, steady request rate. This
    tracks every request in a one-hour window and blocks once the configured
    ceiling is reached, rather than letting a burst trip a soft ban.
    """

    def __init__(self, max_per_hour: int, logger, window_seconds: float = 3600.0) -> None:
        """Args:
            max_per_hour: Request ceiling inside one window.
            logger: Where request lines are written.
            window_seconds: Length of the rolling window. Only overridden by
                tests, so the ceiling can be exercised without waiting an hour.
        """
        self.max_per_hour = max_per_hour
        self.logger = logger
        self.window_seconds = window_seconds
        self._events: deque[float] = deque()
        self.total = 0

    def _prune(self, now: float) -> None:
        while self._events and now - self._events[0] > self.window_seconds:
            self._events.popleft()

    @property
    def used_this_hour(self) -> int:
        """Number of requests made in the trailing 60 minutes."""
        self._prune(time.monotonic())
        return len(self._events)

    def acquire(self, kind: str, detail: str = "") -> None:
        """Block until a request slot is free, then record the request.

        Args:
            kind: Short request category, e.g. ``profile``, ``post``, ``image``.
            detail: Free-text detail for the log line (username, shortcode, URL).
        """
        now = time.monotonic()
        self._prune(now)
        if len(self._events) >= self.max_per_hour:
            wait = self.window_seconds - (now - self._events[0]) + 1
            self.logger.warning(
                "RATE LIMIT reached (%d/hr) - sleeping %.0fs before %s %s",
                self.max_per_hour,
                wait,
                kind,
                detail,
            )
            time.sleep(max(wait, 0))
            self._prune(time.monotonic())

        self._events.append(time.monotonic())
        self.total += 1
        self.logger.info(
            "REQ #%04d [%s] %s (%d/%d this hour)",
            self.total,
            kind,
            detail,
            len(self._events),
            self.max_per_hour,
        )


def _sleep_random(bounds: tuple[float, float], reason: str, logger) -> None:
    """Sleep a uniform random duration inside ``bounds`` and log it."""
    low, high = bounds
    if high <= 0:
        return
    delay = random.uniform(low, high)
    logger.debug("sleeping %.1fs (%s)", delay, reason)
    time.sleep(delay)


class Scraper:
    """Fetches recent posts for configured venues, with pacing and caching."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        use_fixtures: bool = False,
        is_cached: Callable[[str], bool] | None = None,
        logger=None,
    ) -> None:
        """Create a scraper.

        Args:
            config: Loaded config; read from ``config.yaml`` when omitted.
            use_fixtures: Read from ``tests/fixtures/*.json`` instead of the network.
            is_cached: Predicate returning True for shortcodes already stored.
                Cached posts are skipped entirely - no refetch, no image
                download, no downstream LLM call.
            logger: Logger to use; the package logger by default.
        """
        self.config = config or get_config()
        self.settings: ScrapeSettings = self.config.scrape
        self.use_fixtures = use_fixtures
        self.is_cached = is_cached or (lambda _shortcode: False)
        self.logger = logger or setup_logging(self.config)
        self.limiter = RateLimiter(self.settings.max_requests_per_hour, self.logger)
        self.config.images_dir.mkdir(parents=True, exist_ok=True)
        self._loader: instaloader.Instaloader | None = None
        self.skipped_accounts: list[str] = []

    # ------------------------------------------------------------------ #
    # instaloader plumbing
    # ------------------------------------------------------------------ #

    @property
    def loader(self) -> instaloader.Instaloader:
        """Lazily built instaloader instance (anonymous, downloads disabled).

        instaloader's own file-writing features are all turned off; we use it
        only as a metadata client and handle image bytes ourselves so filenames
        stay predictable (``data/images/{shortcode}.jpg``).
        """
        if self._loader is None:
            self._loader = instaloader.Instaloader(
                quiet=True,
                download_pictures=False,
                download_videos=False,
                download_video_thumbnails=False,
                download_geotags=False,
                download_comments=False,
                save_metadata=False,
                compress_json=False,
                user_agent=USER_AGENT,
                max_connection_attempts=1,  # we do our own backoff
            )
        return self._loader

    # ------------------------------------------------------------------ #
    # image download
    # ------------------------------------------------------------------ #

    def _image_url_for(self, post: instaloader.Post) -> str | None:
        """Pick the image to download: first sidecar node, or the display URL.

        For carousels this is the first slide; for videos ``post.url`` is
        already the thumbnail frame.
        """
        try:
            if post.typename == "GraphSidecar":
                for node in post.get_sidecar_nodes():
                    return node.display_url
            return post.url
        except InstaloaderException as exc:
            self.logger.warning("could not resolve image url for %s: %s", post.shortcode, exc)
            return None

    def _download_image(self, shortcode: str, url: str | None) -> str | None:
        """Download ``url`` to ``data/images/{shortcode}.jpg``.

        Returns the path as a string, or None if there was nothing to download
        or the download failed. Never raises - a missing image is not worth
        aborting a run over.
        """
        if not url:
            return None
        target = self.config.images_dir / f"{shortcode}.jpg"
        if target.exists() and target.stat().st_size > 0:
            self.logger.debug("image cached on disk: %s", target.name)
            return str(target)

        self.limiter.acquire("image", shortcode)
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read()
            if not payload:
                raise ValueError("empty image response")
            target.write_bytes(payload)
            self.logger.debug("saved image %s (%d bytes)", target.name, len(payload))
            return str(target)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("image download failed for %s: %s", shortcode, exc)
            return None

    # ------------------------------------------------------------------ #
    # fixtures
    # ------------------------------------------------------------------ #

    def _fixture_path(self, username: str) -> Path:
        return self.config.fixtures_dir / f"{username}.json"

    def _load_fixture(self, username: str, limit: int) -> list[RawPost]:
        """Read posts for one account from ``tests/fixtures/{username}.json``."""
        path = self._fixture_path(username)
        if not path.exists():
            self.logger.warning("no fixture for %s at %s", username, path)
            return []
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            self.logger.error("unreadable fixture %s: %s", path, exc)
            return []

        posts: list[RawPost] = []
        for item in payload.get("posts", [])[:limit]:
            post = RawPost.from_dict(item)
            if self.is_cached(post.shortcode):
                self.logger.info("CACHED %s/%s - skipping", username, post.shortcode)
                continue
            post.local_image_path = self._resolve_fixture_image(post)
            posts.append(post)
        self.logger.info("FIXTURE %s -> %d post(s)", username, len(posts))
        return posts

    def _resolve_fixture_image(self, post: RawPost) -> str | None:
        """Point a fixture post at a real local image, if one exists.

        Fixture images live in ``tests/fixtures/images/`` (committed) because
        ``data/images/`` is gitignored.
        """
        candidates: list[Path] = []
        if post.local_image_path:
            raw = Path(post.local_image_path)
            candidates.append(raw)
        candidates.append(self.config.fixtures_dir / "images" / f"{post.shortcode}.jpg")
        candidates.append(self.config.images_dir / f"{post.shortcode}.jpg")
        for candidate in candidates:
            try:
                if candidate.exists():
                    return str(candidate)
            except OSError:
                continue
        return None

    def _save_fixture(self, username: str, posts: list[RawPost]) -> None:
        """Persist a successful live fetch for offline development."""
        if not posts:
            return
        self.config.fixtures_dir.mkdir(parents=True, exist_ok=True)
        images_dir = self.config.fixtures_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)

        existing: dict[str, dict] = {}
        path = self._fixture_path(username)
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as handle:
                    for item in json.load(handle).get("posts", []):
                        existing[item["shortcode"]] = item
            except (OSError, json.JSONDecodeError, KeyError) as exc:
                self.logger.warning("ignoring unreadable fixture %s: %s", path, exc)

        for post in posts:
            existing[post.shortcode] = post.to_dict()
            if post.local_image_path:
                source = Path(post.local_image_path)
                destination = images_dir / f"{post.shortcode}.jpg"
                if source.exists() and not destination.exists():
                    shutil.copyfile(source, destination)

        merged = sorted(existing.values(), key=lambda item: item["posted_at"], reverse=True)
        payload = {
            "username": username,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "posts": merged,
        }
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
        self.logger.info("saved fixture %s (%d post(s))", path.name, len(merged))

    # ------------------------------------------------------------------ #
    # fetching
    # ------------------------------------------------------------------ #

    def _fetch_live(self, username: str, limit: int) -> list[RawPost]:
        """Make one live attempt at an account. Raises on connection errors."""
        self.limiter.acquire("profile", username)
        profile = instaloader.Profile.from_username(self.loader.context, username)

        collected: list[RawPost] = []
        seen = 0
        for post in profile.get_posts():
            if seen >= limit:
                break
            seen += 1

            if self.is_cached(post.shortcode):
                self.logger.info("CACHED %s/%s - skipping", username, post.shortcode)
                continue

            self.limiter.acquire("post", f"{username}/{post.shortcode}")
            image_url = self._image_url_for(post)
            raw = RawPost(
                shortcode=post.shortcode,
                username=username,
                caption=post.caption or "",
                posted_at=post.date_utc.replace(tzinfo=timezone.utc),
                image_url=image_url,
                local_image_path=None,
                permalink=permalink_for(post.shortcode),
                is_video=bool(post.is_video),
                typename=str(post.typename),
            )
            raw.local_image_path = self._download_image(raw.shortcode, image_url)
            collected.append(raw)
            _sleep_random(
                self.settings.sleep_between_posts, f"after {post.shortcode}", self.logger
            )

        return collected

    def fetch_recent_posts(self, username: str, limit: int | None = None) -> list[RawPost]:
        """Fetch recent posts for one account, retrying with backoff.

        Posts whose shortcode is already known (see ``is_cached``) are skipped
        without any network or disk work. After
        ``scrape.max_retries_per_account`` failures the account is abandoned and
        an empty list is returned - a dead account never aborts the run.

        Args:
            username: Instagram handle, without the leading ``@``.
            limit: Maximum posts to inspect; falls back to config.

        Returns:
            Newly fetched posts, newest first. May be empty.
        """
        limit = limit or self.settings.posts_per_account

        if self.use_fixtures:
            return self._load_fixture(username, limit)

        for attempt in range(self.settings.max_retries_per_account):
            try:
                posts = self._fetch_live(username, limit)
                self.logger.info("OK %s -> %d new post(s)", username, len(posts))
                if posts and self.settings.save_fixtures:
                    try:
                        self._save_fixture(username, posts)
                    except OSError as exc:
                        self.logger.warning("could not save fixture for %s: %s", username, exc)
                return posts

            except ProfileNotExistsException as exc:
                self.logger.error("account %s does not exist or is private: %s", username, exc)
                self.skipped_accounts.append(username)
                return []

            except (TooManyRequestsException, ConnectionException) as exc:
                delay = self.settings.backoff_base_seconds * (2**attempt) + random.uniform(0, 5)
                remaining = self.settings.max_retries_per_account - attempt - 1
                self.logger.warning(
                    "%s on %s (attempt %d/%d): %s - backing off %.0fs",
                    type(exc).__name__,
                    username,
                    attempt + 1,
                    self.settings.max_retries_per_account,
                    exc,
                    delay,
                )
                if remaining <= 0:
                    break
                time.sleep(delay)

            except Exception as exc:  # noqa: BLE001 - keep the run alive
                self.logger.exception("unexpected error scraping %s: %s", username, exc)
                break

        self.logger.error(
            "giving up on %s after %d attempt(s) - continuing with other accounts",
            username,
            self.settings.max_retries_per_account,
        )
        self.skipped_accounts.append(username)
        return []

    def fetch_all(
        self, usernames: Iterable[str] | None = None, limit: int | None = None
    ) -> Iterator[RawPost]:
        """Yield posts for every account, pacing between accounts.

        Args:
            usernames: Handles to scrape; configured Instagram venues by default.
                Configured listings with ``instagram=False`` are always skipped.
            limit: Posts per account; falls back to config.

        Yields:
            :class:`RawPost` objects as each account completes.
        """
        candidates = list(usernames) if usernames else [v.slug for v in self.config.venues]
        handles = [username for username in candidates
                   if (venue := self.config.venue_by_slug(username)) is None or venue.instagram]
        for index, username in enumerate(handles):
            self.logger.info("--- account %d/%d: %s ---", index + 1, len(handles), username)
            for post in self.fetch_recent_posts(username, limit):
                yield post
            if index < len(handles) - 1 and not self.use_fixtures:
                _sleep_random(
                    self.settings.sleep_between_accounts, "before next account", self.logger
                )


def fetch_recent_posts(
    username: str,
    limit: int = 10,
    *,
    config: Config | None = None,
    use_fixtures: bool = False,
    is_cached: Callable[[str], bool] | None = None,
) -> list[RawPost]:
    """Fetch recent posts for one account.

    Args:
        username: Instagram handle.
        limit: Maximum posts to inspect.
        config: Optional preloaded config.
        use_fixtures: Read from fixtures instead of the network.
        is_cached: Predicate marking already-stored shortcodes.

    Returns:
        A list of :class:`RawPost`, newest first.
    """
    scraper = Scraper(config, use_fixtures=use_fixtures, is_cached=is_cached)
    return scraper.fetch_recent_posts(username, limit)


def _main(argv: list[str] | None = None) -> int:
    """Standalone entrypoint, so the scraper can be verified on its own."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Fetch recent Instagram posts for bar accounts."
    )
    parser.add_argument("--venue", action="append", help="handle to scrape (repeatable)")
    parser.add_argument("--limit", type=int, default=None, help="posts per account")
    parser.add_argument("--fixtures", action="store_true", help="read tests/fixtures/*.json")
    parser.add_argument("--no-save-fixtures", action="store_true", help="do not write fixtures")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    config = get_config()
    if args.no_save_fixtures:
        config = replace(config, scrape=replace(config.scrape, save_fixtures=False))
    logger = setup_logging(config, verbose=args.verbose)

    started = time.time()
    scraper = Scraper(config, use_fixtures=args.fixtures, logger=logger)
    posts = list(scraper.fetch_all(args.venue, args.limit))

    print()
    for post in posts:
        caption = " ".join((post.caption or "").split())
        ellipsis = "..." if len(caption) > 160 else ""
        print(f"[{post.username}] {post.shortcode}  {post.posted_at:%Y-%m-%d %H:%M UTC}")
        print(f"    type={post.typename} video={post.is_video}")
        print(f"    image={post.local_image_path}")
        print(f"    link={post.permalink}")
        print(f"    caption={caption[:160]}{ellipsis}")
        print()

    account_count = len(args.venue) if args.venue else len(config.venues)
    elapsed = time.time() - started
    print(
        f"{len(posts)} post(s) from {account_count} account(s) in {elapsed:.1f}s "
        f"using {scraper.limiter.total} request(s)"
    )
    if scraper.skipped_accounts:
        print(f"skipped: {', '.join(scraper.skipped_accounts)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
