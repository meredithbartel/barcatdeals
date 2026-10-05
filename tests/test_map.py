"""Offline map metadata tests: honest locations, prices, filters and sources."""

from dataclasses import replace
from datetime import date, datetime, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml
from flask import template_rendered

from bardeals import db
from bardeals.config import Venue, load_config
from bardeals.web import build_map_data, create_app


DAY = date(2099, 9, 16)


@pytest.fixture
def config(tmp_path):
    return replace(load_config(), db_path=tmp_path / "map.db", venues=[
        Venue("a", "Bar A", address="1 Vine St", latitude=39.13, longitude=-84.51,
              location_source="https://example.org/location"),
        Venue("b", "Bar B", address="2 Vine St"),
        Venue("c", "Bar C"),
    ])


@pytest.mark.parametrize("latitude,longitude", [
    (None, -84.5), (39.1, None), ("bad", -84.5), (91, -84.5), (39.1, -181),
    (float("nan"), -84.5), (39.1, float("inf")), (True, False), ({}, []),
])
def test_invalid_or_incomplete_coordinate_pairs_stay_unplaced(latitude, longitude):
    venue = Venue("bar", "Bar", latitude=latitude, longitude=longitude)
    assert venue.latitude is None
    assert venue.longitude is None


@pytest.mark.parametrize("latitude,longitude", [(0, 0), (90, 180), (-90, -180), ("39.13", "-84.51")])
def test_valid_coordinate_pairs_include_zero_and_boundaries(latitude, longitude):
    venue = Venue("bar", "Bar", latitude=latitude, longitude=longitude)
    assert venue.latitude == float(latitude)
    assert venue.longitude == float(longitude)


def test_load_config_reads_optional_locations_safely(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"venues": [
        {"slug": "mapped", "latitude": "39.13", "longitude": "-84.51",
         "location_source": " https://example.org/location "},
        {"slug": "partial", "latitude": 39.13, "location_source": {}},
        {"slug": "missing"},
    ]}), encoding="utf-8")
    venues = load_config(path).venues
    assert (venues[0].latitude, venues[0].longitude) == (39.13, -84.51)
    assert venues[0].location_source == "https://example.org/location"
    assert venues[1].latitude is venues[1].longitude is venues[1].location_source is None
    assert venues[2].latitude is venues[2].longitude is None


def test_load_config_keeps_old_handles_and_parses_restaurant_listings(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"venues": [
        {"slug": "existing"},
        {"slug": "restaurant", "kind": "restaurant", "instagram": False},
        {"slug": "both", "kind": "bar_restaurant", "instagram": "false"},
        {"slug": "invalid", "kind": "unknown"},
    ]}), encoding="utf-8")
    venues = load_config(path).venues
    assert (venues[0].kind, venues[0].instagram) == ("bar", True)
    assert (venues[1].kind, venues[1].instagram) == ("restaurant", False)
    assert (venues[2].kind_label, venues[2].instagram) == ("Bar & restaurant", False)
    assert venues[3].kind == "bar"


@pytest.mark.parametrize("website,expected", [
    ("https://restaurant.example/menu", "https://restaurant.example/menu"),
    ("http://restaurant.example/", "http://restaurant.example/"),
    ("javascript:alert(1)", None), ("//restaurant.example", None),
    ("https:", None), ("https://[bad", None), (None, None),
])
def test_map_metadata_labels_places_and_exposes_only_web_urls(config, website, expected):
    config = replace(config, venues=[Venue("dining", "Dining", kind="restaurant", instagram=False,
                                         website=website)])
    place = build_map_data(config, {}, DAY)["venues"][0]
    assert (place["kind"], place["kind_label"]) == ("restaurant", "Restaurant")
    assert place["website_url"] == expected


def test_map_counts_only_confident_prices_and_keeps_unconfirmed_context(config):
    data = {
        "is_today": True,
        "deals": [
            {"venue_slug": "a", "price": 4, "on_now": True},
            {"venue_slug": "a", "price": None},
        ],
        "uncertain": [{"venue_slug": "a", "price": 1, "on_now": True,
                       "verify": {"kind": "sample"}}],
        "undated": [{"venue_slug": "b", "price": 0}],
    }
    payload = build_map_data(config, data, DAY)
    first, second, third = payload["venues"]
    assert first["cheapest"] == 4
    assert first["deal_count"] == 2
    assert first["on_now_count"] == 1
    assert first["unconfirmed_count"] == 1
    assert first["has_samples"] is payload["has_samples"] is True
    assert first["venue_url"] == "/venue/a"
    assert parse_qs(urlsplit(first["directions_url"]).query)["destination"] == ["39.13,-84.51"]
    assert second["cheapest"] is None
    assert second["deal_count"] == 0
    assert second["unconfirmed_count"] == 1
    assert second["latitude"] is second["longitude"] is None
    assert "2 Vine St" in parse_qs(urlsplit(second["directions_url"]).query)["destination"][0]
    assert third["directions_url"] is None
    assert payload["mapped_count"] == 1
    assert payload["unmapped_count"] == 2
    assert payload["day"] == DAY.isoformat()


def test_map_filters_remove_quiet_venues_but_keep_uncertain_matches(config):
    data = {"deals": [{"venue_slug": "a", "price": 0}],
            "uncertain": [{"venue_slug": "b"}]}
    payload = build_map_data(config, data, DAY, filters_active=True)
    assert [venue["slug"] for venue in payload["venues"]] == ["a", "b"]
    assert payload["venues"][0]["cheapest"] == 0
    assert payload["mapped_count"] == payload["unmapped_count"] == 1


def test_empty_map_data_keeps_known_venues_without_inventing_locations(config):
    payload = build_map_data(config, {}, DAY)
    assert len(payload["venues"]) == 3
    assert payload["has_samples"] is False
    assert payload["is_today"] is False
    assert all(venue["deal_count"] == 0 and venue["cheapest"] is None
               for venue in payload["venues"])
    filtered = build_map_data(config, {}, DAY, filters_active=True)
    assert filtered["venues"] == []
    assert filtered["mapped_count"] == filtered["unmapped_count"] == 0


def test_map_on_now_is_false_for_other_days_even_with_stale_view_flags(config):
    payload = build_map_data(config, {"is_today": False,
                                    "deals": [{"venue_slug": "a", "on_now": True}]}, DAY)
    assert payload["venues"][0]["on_now_count"] == 0


@pytest.fixture
def client(config):
    conn = db.connect(config.db_path)
    db.init_db(conn)
    for slug, price in [("a", 0), ("b", 8)]:
        db.insert_post(conn, SimpleNamespace(
            shortcode=slug, username=slug, caption="", posted_at=datetime.now(timezone.utc),
            permalink=f"https://www.instagram.com/p/{slug}/", local_image_path=None,
        ))
        db.insert_deal(conn, db.Deal(shortcode=slug, venue_slug=slug, venue_name=f"Bar {slug}",
                                   deal_text="beer special", price=price, recurrence="daily"))
    conn.close()
    app = create_app(config)
    app.config.update(TESTING=True)
    return app.test_client()


def test_map_api_and_index_context_share_selected_day_and_zero_filter(client):
    params = {"day": DAY.isoformat(), "max": "0", "cat": "beer"}
    response = client.get("/api/map.json", query_string=params)
    assert response.status_code == 200
    payload = response.get_json()
    assert [venue["slug"] for venue in payload["venues"]] == ["a"]
    assert payload["venues"][0]["cheapest"] == 0
    assert payload["day"] == DAY.isoformat()
    assert payload["is_today"] is False
    contexts = []

    def record(sender, template, context, **extra):
        contexts.append(context)

    with template_rendered.connected_to(record, client.application):
        assert client.get("/", query_string=params).status_code == 200
    assert contexts[-1]["map_data"] == payload


def test_map_api_preserves_unplaced_venues_and_honors_search(client):
    payload = client.get("/api/map.json").get_json()
    assert len(payload["venues"]) == 3
    assert payload["unmapped_count"] == 2
    assert client.get("/api/map.json?q=absent").get_json()["venues"] == []
    assert client.get("/api/map.json?day=invalid").status_code == 400


def test_search_discovers_a_restaurant_without_deals_but_other_filters_stay_strict(config):
    config = replace(config, venues=[Venue("new_dining", "Campus Kitchen", kind="restaurant",
                                         instagram=False, address="10 College Ave")])
    client = create_app(config).test_client()
    for query in ("campus kitchen", "COLLEGE AVE"):
        payload = client.get("/api/map.json", query_string={"q": query}).get_json()
        assert [place["slug"] for place in payload["venues"]] == ["new_dining"]
        assert payload["venues"][0]["deal_count"] == 0
    assert client.get("/api/map.json?q=campus&max=0").get_json()["venues"] == []
    assert client.get("/api/map.json?q=campus&cat=food").get_json()["venues"] == []
    assert client.get("/api/map.json?q=unrelated").get_json()["venues"] == []


def test_restaurant_page_shows_official_website_without_inventing_an_instagram(config):
    config = replace(config, venues=[Venue("new_dining", "Campus Kitchen", kind="restaurant",
                                         instagram=False, website="https://restaurant.example/menu")])
    client = create_app(config).test_client()
    body = client.get("/venue/new_dining").get_data(as_text=True)
    assert 'href="https://restaurant.example/menu"' in body
    assert "Official website" in body
    assert "instagram.com/new_dining" not in body
    assert "Nothing on record for this place yet" in body

    conn = db.connect(config.db_path)
    db.insert_post(conn, SimpleNamespace(
        shortcode="DEMO_DINING", username="new_dining", caption="", posted_at=datetime.now(timezone.utc),
        permalink="https://example.invalid/sample", local_image_path=None,
    ))
    db.insert_deal(conn, db.Deal(shortcode="DEMO_DINING", venue_slug="new_dining",
                               venue_name="Campus Kitchen", deal_text="sample lunch", recurrence="daily"))
    conn.close()
    body = client.get("/venue/new_dining").get_data(as_text=True)
    assert "sample lunch" in body
    assert "instagram.com/new_dining" not in body


def test_existing_venue_page_keeps_its_instagram_link(client):
    assert 'href="https://www.instagram.com/a/"' in client.get("/venue/a").get_data(as_text=True)


@pytest.fixture
def kind_client(config):
    places = [Venue("tap", "Campus Tap", kind="bar"),
              Venue("kitchen", "Campus Kitchen", kind="restaurant"),
              Venue("hybrid", "Campus Pub and Kitchen", kind="bar_restaurant"),
              Venue("quiet", "Quiet Kitchen", kind="restaurant", address="10 College Ave")]
    config = replace(config, venues=places)
    conn = db.connect(config.db_path)
    db.init_db(conn)
    for place in places[:3]:
        db.insert_post(conn, SimpleNamespace(
            shortcode=place.slug, username=place.slug, caption="", posted_at=datetime.now(timezone.utc),
            permalink=f"https://www.instagram.com/p/{place.slug}/", local_image_path=None,
        ))
        for item in ("beer", "wings"):
            db.insert_deal(conn, db.Deal(shortcode=place.slug, venue_slug=place.slug, venue_name=place.name,
                                       deal_text=f"{item} special", price=3, recurrence="daily"))
    conn.close()
    app = create_app(config)
    app.config.update(TESTING=True)
    return app.test_client()


@pytest.mark.parametrize("kind,expected_deals,expected_places", [
    ("food", {"kitchen", "hybrid"}, {"kitchen", "hybrid", "quiet"}),
    ("drinks", {"tap", "hybrid"}, {"tap", "hybrid"}),
    ("all", {"tap", "kitchen", "hybrid"}, {"tap", "kitchen", "hybrid", "quiet"}),
    ("invalid", {"tap", "kitchen", "hybrid"}, {"tap", "kitchen", "hybrid", "quiet"}),
])
def test_kind_filter_matches_home_week_and_apis(kind_client, kind, expected_deals, expected_places):
    params = {"day": DAY.isoformat(), "kind": kind}
    payload = kind_client.get("/api/map.json", query_string=params).get_json()
    assert {place["slug"] for place in payload["venues"]} == expected_places
    deals = kind_client.get("/api/deals.json", query_string=params).get_json()["deals"]
    assert {deal["venue_slug"] for deal in deals} == expected_deals
    contexts = []

    def record(sender, template, context, **extra):
        contexts.append(context)

    with template_rendered.connected_to(record, kind_client.application):
        assert kind_client.get("/", query_string=params).status_code == 200
        assert kind_client.get("/week", query_string={"start": DAY.isoformat(), "kind": kind}).status_code == 200
    home, week = contexts
    assert home["map_data"] == payload
    assert home["directory"] == payload["venues"]
    assert home["place_counts"] == payload["place_counts"]
    assert {place["slug"] for place in home["catalogue"]} == {"tap", "kitchen", "hybrid", "quiet"}
    assert all(place["deal_count"] == 0 and place["cheapest"] is None for place in home["catalogue"])
    assert all({deal["venue_slug"] for deal in day["deals"]} == expected_deals for day in week["days"])


def test_kind_is_independent_of_deal_category(kind_client):
    deals = kind_client.get("/api/deals.json?kind=drinks&cat=food").get_json()["deals"]
    assert {deal["venue_slug"] for deal in deals} == {"tap", "hybrid"}
    assert all(deal["category"] == "food" for deal in deals)
    places = kind_client.get("/api/map.json?kind=food").get_json()
    assert places["place_counts"] == {"total": 3, "food": 3, "drinks": 1}


def test_kind_and_search_keep_undocumented_places_but_price_remains_strict(kind_client):
    for query in ("quiet", "college ave"):
        places = kind_client.get("/api/map.json", query_string={"kind": "food", "q": query}).get_json()["venues"]
        assert [place["slug"] for place in places] == ["quiet"]
    assert kind_client.get("/api/map.json?kind=drinks&q=quiet").get_json()["venues"] == []
    assert kind_client.get("/api/map.json?kind=food&q=quiet&max=3").get_json()["venues"] == []
    assert kind_client.get("/api/map.json?kind=food&q=quiet&cat=food").get_json()["venues"] == []


def test_navigation_helpers_keep_kind_with_date_search_and_price_changes(kind_client):
    app = kind_client.application
    with app.test_request_context("/?day=2099-09-16&kind=food&q=kitchen&max=3&cat=beer"):
        browse = app.jinja_env.globals["browse_url"]
        page = app.jinja_env.globals["page_url"]
        params = parse_qs(urlsplit(browse("week", start="2099-09-17")).query)
        assert params == {"kind": ["food"], "q": ["kitchen"], "max": ["3"],
                          "cat": ["beer"], "start": ["2099-09-17"]}
        assert parse_qs(urlsplit(page(day="2099-09-18", max="5", q="campus")).query) == {
            "kind": ["food"], "q": ["campus"], "max": ["5"], "cat": ["beer"], "day": ["2099-09-18"],
        }
        assert "kind" not in parse_qs(urlsplit(page(kind="all")).query)
