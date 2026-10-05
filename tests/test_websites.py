"""Tests for the website scraping layer.

No network. HTML is fed in directly and the fetch layer is stubbed, so these
cover the parts that decide what gets read and what gets billed: text
extraction, link scoring, content-hash caching, robots.txt, and the refusal
circuit breaker.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from bardeals.config import Venue, WebSettings, load_config
from bardeals.websites import (
    Page,
    RobotsCache,
    WebsiteScraper,
    clean_text,
    discover_links,
    score_link,
)


@pytest.fixture
def config():
    return load_config()


@pytest.fixture
def settings(config):
    return config.web


SPECIALS_HTML = """
<!doctype html>
<html><head><title>Specials | El Vaquero</title></head>
<body>
  <nav><a href="/menu">Menu</a><a href="/page/specials">Specials</a></nav>
  <header><h1>El Vaquero</h1></header>
  <main>
    <h2>$10.99 Mexican Lunch Specials</h2>
    <p>Monday through Friday from 11 a.m. to 2 p.m.</p>
    <h2>Happy Hour</h2>
    <p>Daily Happy Hour runs from 4 to 8 p.m. with margarita specials.</p>
    <img src="/img/margarita.jpg">
  </main>
  <footer><p>Copyright 2026. Follow us on Facebook.</p></footer>
  <script>var tracking = 1;</script>
  <style>body { color: red; }</style>
</body></html>
"""


# --------------------------------------------------------------------------- #
# text extraction
# --------------------------------------------------------------------------- #


def test_clean_text_pulls_the_title():
    title, _, _ = clean_text(SPECIALS_HTML)
    assert title == "Specials | El Vaquero"


def test_clean_text_keeps_the_deals():
    _, text, _ = clean_text(SPECIALS_HTML)
    assert "$10.99 Mexican Lunch Specials" in text
    assert "Monday through Friday from 11 a.m. to 2 p.m." in text
    assert "Daily Happy Hour runs from 4 to 8 p.m." in text


def test_clean_text_drops_scripts_and_styles():
    _, text, _ = clean_text(SPECIALS_HTML)
    assert "tracking" not in text
    assert "color: red" not in text


def test_clean_text_drops_nav_header_and_footer():
    """A nav link reading "Specials" would otherwise look like a deal on every page."""
    _, text, _ = clean_text(SPECIALS_HTML)
    assert "Copyright 2026" not in text
    assert "Follow us on Facebook" not in text


def test_clean_text_collects_images():
    _, _, images = clean_text(SPECIALS_HTML)
    assert "/img/margarita.jpg" in images


def test_clean_text_on_empty_or_broken_html():
    assert clean_text("") == ("", "", [])
    title, text, _ = clean_text("<html><body><p>hi<p>there</body>")
    assert "hi" in text and "there" in text


def test_clean_text_on_a_javascript_shell():
    """ladder19.com is a JS app: no text means no page, not a crash."""
    _, text, _ = clean_text('<html><body><div id="root"></div><script>app()</script></body></html>')
    assert text == ""


# --------------------------------------------------------------------------- #
# link scoring
# --------------------------------------------------------------------------- #


def test_specials_links_outrank_menu_links(settings):
    specials = score_link("https://bar.com/specials", "Specials", settings)
    menu = score_link("https://bar.com/menu", "Menu", settings)
    assert specials > menu > 0


def test_happy_hour_is_the_top_signal(settings):
    happy = score_link("https://bar.com/happy-hour", "Happy Hour", settings)
    assert happy >= score_link("https://bar.com/specials", "Specials", settings)


def test_social_and_ordering_links_are_never_followed(settings):
    for url in [
        "https://facebook.com/bar",
        "https://www.instagram.com/bar",
        "https://bar.com/cart",
        "https://doordash.com/store/bar",
        "mailto:hi@bar.com",
        "https://bar.com/login",
    ]:
        assert score_link(url, "Order", settings) == -1, url


def test_unrelated_links_score_zero(settings):
    assert score_link("https://bar.com/jobs", "Join Our Team", settings) == 0
    assert score_link("https://bar.com/about", "About", settings) == 0


def test_pdfs_are_followed(settings):
    """Bar menus and happy-hour sheets are very often PDFs."""
    assert score_link("https://bar.com/uploads/menu.pdf", "Menu", settings) > 0


def test_discover_links_stays_on_site(settings):
    html = """
    <a href="/specials">Specials</a>
    <a href="https://othersite.com/specials">Elsewhere</a>
    <a href="https://www.bar.com/happy-hour">Happy Hour</a>
    """
    links = discover_links(html, "https://bar.com/", settings)
    assert "https://bar.com/specials" in links
    assert "https://www.bar.com/happy-hour" in links  # www is the same site
    assert not any("othersite.com" in link for link in links)


def test_discover_links_orders_by_promise(settings):
    html = '<a href="/menu">Menu</a><a href="/happy-hour">Happy Hour</a>'
    links = discover_links(html, "https://bar.com/", settings)
    assert links[0].endswith("/happy-hour")


def test_discover_links_ignores_anchors_and_self(settings):
    html = '<a href="#top">Top</a><a href="/">Home</a><a href="/specials">Specials</a>'
    links = discover_links(html, "https://bar.com/", settings)
    assert links == ["https://bar.com/specials"]


def test_discover_links_deduplicates(settings):
    html = '<a href="/specials">Specials</a><a href="/specials">Our Specials</a>'
    assert len(discover_links(html, "https://bar.com/", settings)) == 1


# --------------------------------------------------------------------------- #
# content hashing / caching
# --------------------------------------------------------------------------- #


def _page(text: str, url: str = "https://bar.com/specials") -> Page:
    return Page(url=url, title="t", text=text, images=[],
                fetched_at=datetime.now(timezone.utc))


def test_identical_text_hashes_identically():
    assert _page("$2 drafts").shortcode == _page("$2 drafts").shortcode


def test_changed_text_changes_the_hash():
    assert _page("$2 drafts").shortcode != _page("$3 drafts").shortcode


def test_the_hash_ignores_the_url():
    """A site moving its specials page must not look like a brand-new deal."""
    left = _page("$2 drafts", "https://bar.com/specials")
    right = _page("$2 drafts", "https://bar.com/page/specials")
    assert left.shortcode == right.shortcode


def test_shortcodes_are_namespaced():
    assert _page("x").shortcode.startswith("web-")


def test_unchanged_page_is_skipped_entirely(config, monkeypatch):
    """The whole point of hashing: an unchanged page costs no LLM call."""
    page = _page("$10.99 lunch special Monday to Friday")
    scraper = WebsiteScraper(config, is_cached=lambda code: code == page.shortcode)
    monkeypatch.setattr(scraper, "fetch_page", lambda url: page)
    monkeypatch.setattr(scraper, "_candidate_links", lambda venue, home: [])

    venue = Venue(slug="testbar", name="Test Bar", website="https://bar.com/")
    assert scraper.fetch_site(venue) == []


def test_changed_page_produces_a_post(config, monkeypatch):
    page = _page("$10.99 lunch special Monday to Friday")
    scraper = WebsiteScraper(config, is_cached=lambda code: False)
    monkeypatch.setattr(scraper, "fetch_page", lambda url: page)
    monkeypatch.setattr(scraper, "_candidate_links", lambda venue, home: [])

    venue = Venue(slug="testbar", name="Test Bar", website="https://bar.com/")
    posts = scraper.fetch_site(venue)
    assert len(posts) == 1
    assert posts[0].source_kind == "website"
    assert posts[0].username == "testbar"
    assert "$10.99 lunch special" in posts[0].caption
    assert posts[0].permalink == "https://bar.com/specials"


def test_duplicate_pages_within_one_site_are_collapsed(config, monkeypatch):
    """Many sites serve the same menu at two URLs."""
    scraper = WebsiteScraper(config)
    monkeypatch.setattr(scraper, "fetch_page", lambda url: _page("same text", url))
    monkeypatch.setattr(
        scraper, "_candidate_links", lambda venue, home: ["https://bar.com/menu"]
    )
    venue = Venue(slug="testbar", name="Test Bar", website="https://bar.com/")
    assert len(scraper.fetch_site(venue)) == 1


# --------------------------------------------------------------------------- #
# failure handling
# --------------------------------------------------------------------------- #


def test_unreadable_homepage_skips_the_site(config, monkeypatch):
    scraper = WebsiteScraper(config)
    monkeypatch.setattr(scraper, "fetch_page", lambda url: None)
    venue = Venue(slug="jsbar", name="JS Bar", website="https://ladder19.com/")
    assert scraper.fetch_site(venue) == []
    assert "jsbar" in scraper.skipped_sites


def test_pinned_pages_are_used_when_the_homepage_fails(config, monkeypatch):
    """El Vaquero's homepage 403s, but its specials page is pinned in config."""
    calls: list[str] = []

    def fake_fetch(url):
        calls.append(url)
        return None if url.endswith("/clifton") else _page("$10.99 lunch special", url)

    scraper = WebsiteScraper(config)
    monkeypatch.setattr(scraper, "fetch_page", fake_fetch)
    venue = Venue(
        slug="elvaqueroclifton",
        name="El Vaquero",
        website="https://vaquerorestaurant.com/clifton",
        pages=("https://vaquerorestaurant.com/page/specials",),
    )
    posts = scraper.fetch_site(venue)
    assert len(posts) == 1
    assert posts[0].permalink.endswith("/page/specials")


def test_pinned_pages_come_before_discovered_ones(config, monkeypatch):
    order: list[str] = []

    def fake_fetch(url):
        order.append(url)
        return _page(f"text for {url}", url)

    scraper = WebsiteScraper(config)
    monkeypatch.setattr(scraper, "fetch_page", fake_fetch)
    monkeypatch.setattr(scraper, "_candidate_links",
                        lambda venue, home: ["https://bar.com/menu"])
    venue = Venue(slug="b", name="B", website="https://bar.com/",
                  pages=("https://bar.com/pinned",))
    scraper.fetch_site(venue)
    assert order[1] == "https://bar.com/pinned"


def test_a_site_that_raises_never_kills_the_run(config, monkeypatch):
    def boom(venue):
        raise RuntimeError("site exploded")

    scraper = WebsiteScraper(config)
    monkeypatch.setattr(scraper, "fetch_site", boom)
    venues = [Venue(slug="a", name="A", website="https://a.com/")]
    assert list(scraper.fetch_all(venues)) == []
    assert "a" in scraper.skipped_sites


def test_venue_without_a_website_yields_nothing(config):
    scraper = WebsiteScraper(config)
    assert scraper.fetch_site(Venue(slug="x", name="X")) == []


def test_refusal_circuit_breaker(config, monkeypatch):
    """After repeated 403s from a host, stop hitting it for the rest of the run."""
    scraper = WebsiteScraper(config)
    attempts: list[str] = []

    def refuse(url, user_agent):
        attempts.append(url)
        scraper._last_status = 403
        return None

    monkeypatch.setattr(scraper, "_get", refuse)
    for index in range(5):
        scraper.fetch_page(f"https://blocked.com/page{index}")

    # 2 user-agent attempts per page until the breaker trips at MAX_REFUSALS.
    assert len(attempts) <= scraper.MAX_REFUSALS * 2
    assert scraper._refusals["blocked.com"] >= scraper.MAX_REFUSALS


def test_a_success_resets_the_refusal_counter(config, monkeypatch):
    scraper = WebsiteScraper(config)
    scraper._refusals["bar.com"] = 1
    monkeypatch.setattr(
        scraper, "_get",
        lambda url, ua: (b"<html><body><p>$2 drafts</p></body></html>", "utf-8", url, "text/html"),
    )
    scraper.fetch_page("https://bar.com/specials")
    assert "bar.com" not in scraper._refusals


# --------------------------------------------------------------------------- #
# robots.txt
# --------------------------------------------------------------------------- #


def test_robots_disallow_is_respected(config, monkeypatch, tmp_path):
    scraper = WebsiteScraper(config)
    monkeypatch.setattr(scraper.robots, "allows", lambda url: False)
    assert scraper.fetch_page("https://bar.com/specials") is None
    assert "https://bar.com/specials" in scraper.blocked_by_robots


def test_robots_can_be_turned_off(config, monkeypatch):
    relaxed = replace(config, web=replace(config.web, respect_robots=False))
    scraper = WebsiteScraper(relaxed)
    monkeypatch.setattr(
        scraper, "_get",
        lambda url, ua: (b"<html><body><p>$2 drafts</p></body></html>", "utf-8", url, "text/html"),
    )
    called: list[str] = []
    monkeypatch.setattr(scraper.robots, "allows", lambda url: called.append(url) or True)
    assert scraper.fetch_page("https://bar.com/x") is not None
    assert called == [], "robots must not even be consulted when disabled"


def test_missing_robots_txt_means_allowed(config, monkeypatch):
    import logging

    cache = RobotsCache(logging.getLogger("test"))
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: (_ for _ in ()).throw(OSError("404")),
    )
    assert cache.allows("https://bar.com/anything") is True


# --------------------------------------------------------------------------- #
# limits
# --------------------------------------------------------------------------- #


def test_page_budget_is_respected(config, monkeypatch):
    tight = replace(config, web=replace(config.web, max_pages_per_site=3))
    scraper = WebsiteScraper(tight)
    fetched: list[str] = []

    def fake_fetch(url):
        fetched.append(url)
        return _page(f"unique text {url}", url)

    monkeypatch.setattr(scraper, "fetch_page", fake_fetch)
    monkeypatch.setattr(
        scraper, "_candidate_links",
        lambda venue, home: [f"https://bar.com/p{i}" for i in range(10)],
    )
    scraper.fetch_site(Venue(slug="b", name="B", website="https://bar.com/"))
    assert len(fetched) == 3  # homepage + 2


def test_long_page_text_is_capped_for_the_model(config, monkeypatch):
    small = replace(config, web=replace(config.web, max_llm_chars=200))
    scraper = WebsiteScraper(small)
    monkeypatch.setattr(scraper, "fetch_page", lambda url: _page("x" * 5000, url))
    monkeypatch.setattr(scraper, "_candidate_links", lambda venue, home: [])
    posts = scraper.fetch_site(Venue(slug="b", name="B", website="https://bar.com/"))
    assert len(posts[0].caption) <= 260  # 200 + the title header


def test_website_settings_defaults_are_sane():
    settings = WebSettings()
    assert settings.respect_robots is True
    assert settings.max_pages_per_site >= 1
    assert settings.timeout > 0


def test_configured_venues_have_absolute_urls(config):
    for venue in config.venues_with_websites():
        assert venue.website is None or venue.website.startswith("https://"), venue.slug
        for url in venue.pages:
            assert url.startswith("https://"), url
