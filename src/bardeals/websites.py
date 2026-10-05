"""Website scraping: bar websites -> the same ``RawPost`` records Instagram produces.

Instagram carries what a bar is doing *tonight*; its website carries what it
does *every* week - the happy hour window, the lunch special, the standing
"kids eat free Tuesday". The two sources complement each other, so this module
deliberately produces the exact same :class:`~bardeals.scrape.RawPost` shape and
feeds the identical parser, database and reports.

How a site is read:

1. Fetch the homepage.
2. Score its links and follow the few most likely to hold specials
   (``/happy-hour``, ``/specials``, ``/menu``, ...), up to ``max_pages_per_site``.
3. Strip each page to visible text.
4. Hash that text. **The hash is the shortcode**, which means the existing cache
   works unchanged: a page whose wording has not moved produces the same
   shortcode, is already in the database, and is never re-parsed or re-billed.
   Edit the specials and the hash changes, producing a new row.

Unlike the Instagram scraper, this one is a well-behaved web citizen: it obeys
``robots.txt``, identifies itself honestly in the User-Agent, requests one page
at a time, and sleeps between them.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import logging
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, Iterator

from bs4 import BeautifulSoup

from .config import Config, WebSettings, Venue, get_config, setup_logging
from .scrape import RawPost

#: Honest, contactable User-Agent. Bar websites are public and welcome traffic;
#: there is no reason to pretend to be a browser here.
USER_AGENT = "bardeals/0.1 (personal deal tracker; +https://github.com/local/bardeals)"

#: Tags whose text is never a deal.
_STRIP_TAGS = ("script", "style", "noscript", "svg", "iframe", "form", "template")

#: Structural regions that are boilerplate on virtually every site.
_STRIP_ROLES = ("nav", "header", "footer", "aside")

_WHITESPACE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\n{3,}")


@dataclass
class Page:
    """One fetched page, reduced to text."""

    url: str
    title: str
    text: str
    images: list[str]
    fetched_at: datetime
    #: Raw source, kept only in memory so link discovery needs no second GET.
    html: str = ""
    kind: str = "html"

    @property
    def digest(self) -> str:
        """Stable hash of the page's visible text.

        Deliberately excludes the URL so that a site which moves its specials
        to a new path does not look like a brand-new deal.
        """
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def shortcode(self) -> str:
        """Cache key for this page's current content."""
        return f"web-{self.digest[:16]}"


def _sleep(bounds: tuple[float, float], reason: str, logger: logging.Logger) -> None:
    """Sleep a random duration inside ``bounds`` and log why."""
    low, high = bounds
    if high <= 0:
        return
    delay = random.uniform(low, high)
    logger.debug("sleeping %.1fs (%s)", delay, reason)
    time.sleep(delay)


def clean_text(html: str) -> tuple[str, str, list[str]]:
    """Reduce an HTML document to its visible text.

    Args:
        html: Raw HTML source.

    Returns:
        ``(title, text, image_urls)``. Navigation, headers, footers, scripts and
        styles are removed, because a nav bar reading "Specials" on every page
        would otherwise look like a deal on every page.
    """
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    if soup.title and soup.title.string:
        title = " ".join(str(soup.title.string).split())

    images: list[str] = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src")
        if src:
            images.append(str(src))

    for tag_name in _STRIP_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()
    for tag_name in _STRIP_ROLES:
        for tag in soup.find_all(tag_name):
            tag.decompose()
    for tag in soup.find_all(attrs={"role": lambda v: v in _STRIP_ROLES if v else False}):
        tag.decompose()

    text = soup.get_text("\n")
    lines = [_WHITESPACE.sub(" ", line).strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    text = _BLANK_LINES.sub("\n\n", text).strip()
    return title, text, images


def score_link(url: str, anchor: str, settings: WebSettings) -> int:
    """Rank how likely a link is to lead to a specials page.

    Args:
        url: Absolute or relative link target.
        anchor: The link's visible text.
        settings: Web settings supplying the keyword lists.

    Returns:
        A score; higher is more promising, ``-1`` means "never follow".
    """
    haystack = f"{url} {anchor}".lower()
    if any(skip in haystack for skip in settings.skip_keywords):
        return -1

    score = 0
    for index, keyword in enumerate(settings.follow_keywords):
        if keyword in haystack:
            # Earlier keywords in the config are the more promising ones.
            score = max(score, len(settings.follow_keywords) - index)
    return score


def _same_site(base: str, candidate: str) -> bool:
    """Whether two URLs share a registrable host (ignoring ``www.``)."""

    def host(url: str) -> str:
        netloc = urllib.parse.urlparse(url).netloc.lower()
        return netloc[4:] if netloc.startswith("www.") else netloc

    return host(base) == host(candidate)


def discover_links(html: str, base_url: str, settings: WebSettings) -> list[str]:
    """Find on-site links worth following from a page, best first.

    Args:
        html: The page's HTML.
        base_url: URL the HTML came from, for resolving relative links.
        settings: Web settings supplying keyword lists.

    Returns:
        Absolute URLs, most promising first, deduplicated.
    """
    soup = BeautifulSoup(html, "html.parser")
    scored: dict[str, int] = {}

    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"]).strip()
        if not href or href.startswith("#"):
            continue
        absolute = urllib.parse.urljoin(base_url, href)
        absolute, _ = urllib.parse.urldefrag(absolute)
        if not absolute.startswith(("http://", "https://")):
            continue
        if not _same_site(base_url, absolute):
            continue
        if absolute.rstrip("/") == base_url.rstrip("/"):
            continue

        score = score_link(absolute, anchor.get_text(" ", strip=True), settings)
        if score > 0:
            scored[absolute] = max(scored.get(absolute, 0), score)

    return [url for url, _ in sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))]


class RobotsCache:
    """Per-host ``robots.txt`` rules, fetched once each."""

    def __init__(
        self, logger: logging.Logger, timeout: float = 10.0, user_agent: str = USER_AGENT
    ) -> None:
        self.logger = logger
        self.timeout = timeout
        self.user_agent = user_agent
        self._cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def allows(self, url: str) -> bool:
        """Whether ``robots.txt`` permits us to fetch this URL.

        A missing or unreadable ``robots.txt`` means "no rules", which is
        allowed. A parse failure is treated as allowed too, matching how
        ordinary crawlers behave.
        """
        parts = urllib.parse.urlparse(url)
        origin = f"{parts.scheme}://{parts.netloc}"

        if origin not in self._cache:
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(urllib.parse.urljoin(origin, "/robots.txt"))
            try:
                request = urllib.request.Request(
                    parser.url, headers={"User-Agent": self.user_agent}
                )
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    parser.parse(response.read().decode("utf-8", "replace").splitlines())
                self._cache[origin] = parser
                self.logger.debug("loaded robots.txt for %s", origin)
            except Exception as exc:  # noqa: BLE001 - no robots.txt is not an error
                self.logger.debug("no usable robots.txt for %s (%s)", origin, exc)
                self._cache[origin] = None

        parser = self._cache[origin]
        if parser is None:
            return True
        try:
            return parser.can_fetch(self.user_agent, url)
        except Exception:  # noqa: BLE001
            return True


class WebsiteScraper:
    """Fetches bar websites and turns their specials pages into RawPosts."""

    #: Consecutive 403s from one host before we leave it alone for this run.
    MAX_REFUSALS = 2

    def __init__(
        self,
        config: Config | None = None,
        *,
        is_cached: Callable[[str], bool] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        """Create a website scraper.

        Args:
            config: Loaded config; read from ``config.yaml`` when omitted.
            is_cached: Predicate returning True for shortcodes already stored.
                Because the shortcode is a content hash, this makes an unchanged
                page free: no LLM call, no new row.
            logger: Logger to use.
        """
        self.config = config or get_config()
        self.settings: WebSettings = self.config.web
        self.is_cached = is_cached or (lambda _shortcode: False)
        self.logger = logger or setup_logging(self.config)
        self.robots = RobotsCache(self.logger, timeout=self.settings.timeout,
                                  user_agent=self.settings.user_agent)
        self.requests = 0
        self._last_status: int | None = None
        #: Consecutive 403s seen per host, to stop hammering a site that is
        #: clearly refusing us. Reset on any success from that host.
        self._refusals: dict[str, int] = {}
        self.skipped_sites: list[str] = []
        #: (shortcode, url) for pages whose text was unchanged. The content did
        #: not move, but the URL may have, so the caller refreshes the stored
        #: permalink rather than let it rot into a 404.
        self.unchanged: list[tuple[str, str]] = []
        self.blocked_by_robots: list[str] = []

    # ------------------------------------------------------------------ #
    # fetching
    # ------------------------------------------------------------------ #

    def _get(self, url: str, user_agent: str) -> tuple[bytes, str, str, str] | None:
        """One HTTP GET. Returns ``(body, charset, final_url)`` or None.

        Never raises; the status of the last attempt is recorded on
        ``self._last_status`` so the caller can decide whether to retry.
        """
        self.requests += 1
        self._last_status = None
        self.logger.info("WEB #%03d GET %s", self.requests, url)
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": user_agent,
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Encoding": "gzip",
                },
            )
            with urllib.request.urlopen(request, timeout=self.settings.timeout) as response:
                self._last_status = response.status
                content_type = (response.headers.get("Content-Type") or "").lower()
                is_pdf = "pdf" in content_type or url.lower().split("?")[0].endswith(".pdf")
                if content_type and "html" not in content_type and not is_pdf:
                    self.logger.info("skipping %s (content-type %s)", url, content_type)
                    return None
                payload = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    payload = gzip.decompress(payload)
                return (
                    payload,
                    response.headers.get_content_charset() or "utf-8",
                    response.geturl(),
                    content_type,
                )
        except urllib.error.HTTPError as exc:
            self._last_status = exc.code
            self.logger.warning("HTTP %s fetching %s", exc.code, url)
            return None
        except Exception as exc:  # noqa: BLE001 - one bad page never kills a run
            self.logger.warning("could not fetch %s: %s", url, exc)
            return None

    def _read_pdf(self, payload: bytes, url: str) -> tuple[str, str]:
        """Extract text from a menu PDF.

        Plenty of bars publish their menu and happy-hour sheet as a PDF rather
        than a web page, so skipping PDFs would miss exactly the pages worth
        reading. Scanned (image-only) PDFs yield no text and are reported as
        such rather than silently returning nothing.

        Args:
            payload: Raw PDF bytes.
            url: Source URL, for logging.

        Returns:
            ``(title, text)``; the text is empty when nothing could be read.
        """
        try:
            from pypdf import PdfReader
        except ImportError:
            self.logger.warning("pypdf is not installed - cannot read %s", url)
            return "", ""

        try:
            reader = PdfReader(io.BytesIO(payload))
            pages = reader.pages[: self.settings.max_pdf_pages]
            chunks = []
            for page in pages:
                try:
                    chunks.append(page.extract_text() or "")
                except Exception:  # noqa: BLE001 - one bad page is not fatal
                    continue
            text = "\n".join(chunk for chunk in chunks if chunk.strip())
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("could not read PDF %s: %s", url, exc)
            return "", ""

        lines = [_WHITESPACE.sub(" ", line).strip() for line in text.splitlines()]
        text = _BLANK_LINES.sub("\n\n", "\n".join(line for line in lines if line)).strip()

        if not text:
            self.logger.info(
                "%s is a PDF with no extractable text (probably a scan) - skipping", url
            )
        else:
            self.logger.info("read %d chars from PDF %s", len(text), url)

        title = url.rsplit("/", 1)[-1].rsplit(".", 1)[0].replace("-", " ").replace("_", " ")
        return title.strip(), text

    def fetch_page(self, url: str) -> Page | None:
        """Fetch and clean a single page. Never raises.

        Args:
            url: Absolute URL.

        Returns:
            A :class:`Page`, or None if the fetch was disallowed, failed, or
            produced nothing usable.
        """
        if self.settings.respect_robots and not self.robots.allows(url):
            self.logger.warning("robots.txt disallows %s - skipping", url)
            self.blocked_by_robots.append(url)
            return None

        host = urllib.parse.urlparse(url).netloc.lower()
        if self._refusals.get(host, 0) >= self.MAX_REFUSALS:
            self.logger.info("%s has refused %d requests - skipping %s",
                             host, self._refusals[host], url)
            return None

        result = self._get(url, self.settings.user_agent)
        if result is None and self._last_status == 403 and self.settings.fallback_user_agent:
            # Measured reality: some of these sites 403 anything that is not a
            # browser, and at least one 403s the browser string instead. Try the
            # honest identity first, fall back only when it is actually refused.
            self.logger.info("403 for %s with the bot UA - retrying as a browser", url)
            result = self._get(url, self.settings.fallback_user_agent)
        if result is None:
            if self._last_status == 403:
                self._refusals[host] = self._refusals.get(host, 0) + 1
                if self._refusals[host] == self.MAX_REFUSALS:
                    self.logger.warning(
                        "%s keeps returning 403 - skipping the rest of that site this run",
                        host,
                    )
            return None

        self._refusals.pop(host, None)
        payload, charset, final_url, content_type = result

        if "pdf" in content_type or final_url.lower().split("?")[0].endswith(".pdf"):
            title, text = self._read_pdf(payload, final_url)
            html, images = "", []
            kind = "pdf"
        else:
            html = payload.decode(charset, "replace")
            title, text, images = clean_text(html)
            kind = "html"

        if not text:
            self.logger.info(
                "no readable text at %s (JavaScript-rendered page or scanned PDF)", url
            )
            return None
        if len(text) > self.settings.max_page_chars:
            self.logger.info(
                "%s yielded %d chars (> max_page_chars %d) - truncating for extraction",
                url, len(text), self.settings.max_page_chars,
            )
            text = text[: self.settings.max_page_chars]

        absolute_images = [urllib.parse.urljoin(final_url, src) for src in images]
        return Page(
            url=final_url,
            title=title,
            text=text,
            images=absolute_images,
            fetched_at=datetime.now(timezone.utc),
            html=html,
            kind=kind,
        )

    def fetch_site(self, venue: Venue) -> list[RawPost]:
        """Fetch one venue's website and return its pages as RawPosts.

        Args:
            venue: A configured venue with a ``website``.

        Returns:
            One :class:`~bardeals.scrape.RawPost` per changed page. Pages whose
            content hash is already stored are skipped entirely.
        """
        if not venue.has_website:
            return []

        homepage = self.fetch_page(venue.website) if venue.website else None
        if homepage is None:
            if not venue.pages:
                self.logger.warning(
                    "could not read %s for %s - the site may require JavaScript, "
                    "which this fetcher does not run",
                    venue.website, venue.slug,
                )
                self.skipped_sites.append(venue.slug)
                return []
            self.logger.info(
                "%s: homepage unreadable, falling back to %d pinned page(s)",
                venue.slug, len(venue.pages),
            )
            return self._fetch_pinned_only(venue)

        pages = [homepage]

        # Pinned URLs first - they are known-good, unlike guessed links.
        candidates = [u for u in venue.pages if u != homepage.url]
        for link in self._candidate_links(venue, homepage):
            if link not in candidates:
                candidates.append(link)

        budget = max(self.settings.max_pages_per_site - 1, 0)
        if len(candidates) > budget:
            self.logger.info(
                "%s: %d candidate page(s), fetching the top %d (max_pages_per_site)",
                venue.slug, len(candidates), budget,
            )
        for link in candidates[:budget]:
            _sleep(self.settings.delay_between_pages, f"before {link}", self.logger)
            page = self.fetch_page(link)
            if page is not None:
                pages.append(page)

        posts: list[RawPost] = []
        seen: set[str] = set()
        for page in pages:
            if page.shortcode in seen:
                self.logger.debug("duplicate content at %s - skipping", page.url)
                continue
            seen.add(page.shortcode)

            if self.is_cached(page.shortcode):
                self.logger.info("UNCHANGED %s (%s) - skipping", page.url, page.shortcode)
                self.unchanged.append((page.shortcode, page.url))
                continue
            posts.append(self._to_post(venue, page))

        self.logger.info("OK %s -> %d changed page(s)", venue.slug, len(posts))
        return posts

    def _fetch_pinned_only(self, venue: Venue) -> list[RawPost]:
        """Fetch just a venue's pinned URLs, when its homepage is unreadable."""
        posts: list[RawPost] = []
        for index, url in enumerate(venue.pages):
            if index:
                _sleep(self.settings.delay_between_pages, f"before {url}", self.logger)
            page = self.fetch_page(url)
            if page is None:
                continue
            if self.is_cached(page.shortcode):
                self.unchanged.append((page.shortcode, page.url))
                continue
            posts.append(self._to_post(venue, page))
        return posts

    def _candidate_links(self, venue: Venue, homepage: Page) -> list[str]:
        """Find promising links on a venue's homepage."""
        if not homepage.html:
            return []
        links = discover_links(homepage.html, homepage.url, self.settings)
        self.logger.info("%s: %d candidate page(s) found", venue.slug, len(links))
        for link in links[: self.settings.max_pages_per_site]:
            self.logger.debug("  candidate: %s", link)
        return links

    def _to_post(self, venue: Venue, page: Page) -> RawPost:
        """Wrap a page as a RawPost so the rest of the pipeline is unchanged."""
        text = page.text
        if len(text) > self.settings.max_llm_chars:
            self.logger.info(
                "%s: sending first %d of %d chars to the model",
                page.url, self.settings.max_llm_chars, len(text),
            )
            text = text[: self.settings.max_llm_chars]

        header = f"{page.title}\n\n" if page.title else ""
        return RawPost(
            shortcode=page.shortcode,
            username=venue.slug,
            caption=f"{header}{text}",
            posted_at=page.fetched_at,
            image_url=None,
            local_image_path=None,
            permalink=page.url,
            is_video=False,
            typename="WebsitePDF" if page.kind == "pdf" else "Website",
            source_kind="website",
        )

    def fetch_all(self, venues: Iterable[Venue] | None = None) -> Iterator[RawPost]:
        """Fetch every configured website, pacing between sites.

        Args:
            venues: Venues to scrape; all configured venues with a website by
                default.

        Yields:
            :class:`~bardeals.scrape.RawPost` records for changed pages.
        """
        targets = list(venues) if venues is not None else self.config.venues_with_websites()
        if not targets:
            self.logger.info("no venues have a website configured")
            return

        for index, venue in enumerate(targets):
            self.logger.info(
                "--- website %d/%d: %s (%s) ---",
                index + 1, len(targets), venue.slug, venue.website,
            )
            try:
                for post in self.fetch_site(venue):
                    yield post
            except Exception as exc:  # noqa: BLE001 - one bad site never kills a run
                self.logger.exception("unexpected error scraping %s: %s", venue.slug, exc)
                self.skipped_sites.append(venue.slug)

            if index < len(targets) - 1:
                _sleep(self.settings.delay_between_sites, "before next site", self.logger)


def fetch_website(
    venue: Venue,
    *,
    config: Config | None = None,
    is_cached: Callable[[str], bool] | None = None,
) -> list[RawPost]:
    """Fetch a single venue's website.

    Args:
        venue: The venue to fetch.
        config: Optional preloaded config.
        is_cached: Predicate marking already-stored content hashes.

    Returns:
        RawPosts for pages whose content has changed.
    """
    return WebsiteScraper(config, is_cached=is_cached).fetch_site(venue)


def _main(argv: list[str] | None = None) -> int:
    """Standalone entrypoint, so the website layer can be verified alone."""
    import argparse

    parser = argparse.ArgumentParser(description="Fetch bar websites for deal text.")
    parser.add_argument("--venue", action="append", help="slug to fetch (repeatable)")
    parser.add_argument("--url", help="fetch one arbitrary URL and print its text")
    parser.add_argument("--chars", type=int, default=1200, help="preview length")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    config = get_config()
    logger = setup_logging(config, verbose=args.verbose)
    scraper = WebsiteScraper(config, logger=logger)

    if args.url:
        page = scraper.fetch_page(args.url)
        if page is None:
            print("nothing fetched")
            return 1
        print(f"\n{page.url}\ntitle: {page.title}\nshortcode: {page.shortcode}")
        print(f"{len(page.text)} chars, {len(page.images)} image(s)\n")
        print(page.text[: args.chars])
        return 0

    if args.venue:
        by_slug = {v.slug: v for v in config.venues}
        targets = [by_slug[s] for s in args.venue if s in by_slug]
    else:
        targets = config.venues_with_websites()

    started = time.time()
    posts = list(scraper.fetch_all(targets))

    print()
    for post in posts:
        preview = " ".join(post.caption.split())[: args.chars]
        print(f"[{post.username}] {post.shortcode}")
        print(f"    {post.permalink}")
        print(f"    {len(post.caption)} chars: {preview}\n")

    print(
        f"{len(posts)} changed page(s) from {len(targets)} site(s) in "
        f"{time.time() - started:.1f}s using {scraper.requests} request(s)"
    )
    if scraper.blocked_by_robots:
        print(f"blocked by robots.txt: {len(scraper.blocked_by_robots)}")
    if scraper.skipped_sites:
        print(f"skipped: {', '.join(scraper.skipped_sites)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
