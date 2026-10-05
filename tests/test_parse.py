"""Tests for LLM extraction, driven by fixtures and a stubbed client.

No network calls. A fake Anthropic client returns canned tool-use responses so
the normalization, date-resolution, and failure paths are all exercised
deterministically.

The zero-deal path gets the most attention here: most real posts contain no
deal, so hallucinating one is the failure mode that would quietly ruin the
output.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from bardeals import db
from bardeals.config import load_config
from bardeals.parse import (
    DEAL_TOOL,
    SYSTEM_PROMPT,
    TOOL_NAME,
    DealParser,
    build_user_content,
    encode_image,
)
from bardeals.scrape import Scraper

WED_NIGHT = datetime(2026, 9, 17, 2, 30, tzinfo=timezone.utc)  # 2026-09-16 22:30 EDT


# --------------------------------------------------------------------------- #
# fake client
# --------------------------------------------------------------------------- #


class FakeBlock:
    """Stand-in for a tool_use content block."""

    def __init__(self, payload, name=TOOL_NAME, block_type="tool_use"):
        self.type = block_type
        self.name = name
        self.input = payload


class FakeTextBlock:
    """Stand-in for a plain text content block."""

    def __init__(self, text="I could not find a deal."):
        self.type = "text"
        self.text = text


class FakeResponse:
    """Stand-in for a Message response."""

    def __init__(self, content):
        self.content = content

    def to_json(self):
        return json.dumps({"content": [getattr(b, "type", "?") for b in self.content]})


class FakeMessages:
    def __init__(self, responses, error=None):
        self._responses = list(responses)
        self._error = error
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._responses.pop(0) if self._responses else FakeResponse([])


class FakeClient:
    """Minimal Anthropic client stand-in."""

    def __init__(self, deals=None, *, content=None, error=None, responses=None):
        if responses is None:
            if content is None:
                content = [FakeBlock({"deals": deals if deals is not None else []})]
            responses = [FakeResponse(content)]
        self.messages = FakeMessages(responses, error=error)


@pytest.fixture
def config():
    return load_config()


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "parse.db")
    db.init_db(connection)
    yield connection
    connection.close()


@pytest.fixture
def fixture_posts(config):
    """Every fixture post, keyed by shortcode."""
    scraper = Scraper(config, use_fixtures=True)
    posts = {}
    for username in ("murphyspubcincy", "ladder_19"):
        for post in scraper.fetch_recent_posts(username, limit=20):
            posts[post.shortcode] = post
    return posts


def parser_with(config, client) -> DealParser:
    return DealParser(config, client=client)


# --------------------------------------------------------------------------- #
# schema and prompt
# --------------------------------------------------------------------------- #


def test_tool_schema_is_strict_and_closed():
    assert DEAL_TOOL["strict"] is True
    schema = DEAL_TOOL["input_schema"]
    assert schema["additionalProperties"] is False
    item = schema["properties"]["deals"]["items"]
    assert item["additionalProperties"] is False
    # Strict mode requires every property to be required; nullability is in the type.
    assert set(item["required"]) == set(item["properties"])


def test_prompt_carries_every_required_example():
    for marker in [
        "$2 domestics tonight",
        "Every Thursday",
        "Trivia at 8",
        "same deal as always",
        "ONE DEAL PER DAY",
        "ZERO deals",
    ]:
        assert marker in SYSTEM_PROMPT, marker


def test_prompt_states_the_image_outranks_the_caption():
    assert "IMAGE MATTERS MORE THAN THE CAPTION" in SYSTEM_PROMPT


# --------------------------------------------------------------------------- #
# message construction
# --------------------------------------------------------------------------- #


def test_flyer_post_sends_the_image_before_the_text(fixture_posts):
    content = build_user_content(fixture_posts["FIXLADDER01"])
    assert content[0]["type"] == "image"
    assert content[0]["source"]["media_type"] == "image/jpeg"
    assert content[-1]["type"] == "text"


def test_post_without_an_image_sends_text_only(fixture_posts):
    content = build_user_content(fixture_posts["FIXMURPH01"])
    assert [block["type"] for block in content] == ["text"]
    assert "no image available" in content[0]["text"]


def test_prompt_states_the_local_post_date_not_the_utc_one(fixture_posts):
    """FIXLADDER01 is 22:30 EDT on the 16th, 02:30 UTC on the 17th."""
    post = fixture_posts["FIXLADDER01"]
    assert post.posted_at.date().isoformat() == "2026-09-16"
    text = build_user_content(post)[-1]["text"]
    assert "2026-09-16" in text


def test_caption_is_included_verbatim(fixture_posts):
    text = build_user_content(fixture_posts["FIXMURPH01"])[-1]["text"]
    assert "$2 domestics tonight!" in text


def test_encode_image_handles_missing_and_empty_files(tmp_path):
    assert encode_image(None) is None
    assert encode_image(tmp_path / "nope.jpg") is None
    empty = tmp_path / "empty.jpg"
    empty.write_bytes(b"")
    assert encode_image(empty) is None


def test_encode_image_skips_oversized_files(tmp_path):
    from bardeals import parse

    big = tmp_path / "big.jpg"
    big.write_bytes(b"\xff" * (parse.MAX_IMAGE_BYTES + 1))
    assert encode_image(big) is None


# --------------------------------------------------------------------------- #
# the zero-deal path
# --------------------------------------------------------------------------- #


def test_empty_deal_list_is_a_clean_success(config, fixture_posts):
    """"🍻 see you tonight" must yield nothing, with no error."""
    parser = parser_with(config, FakeClient(deals=[]))
    result = parser.parse_post(fixture_posts["FIXMURPH04"])
    assert result.deals == []
    assert result.ok
    assert result.error is None


def test_model_answering_without_a_tool_call_yields_no_deals(config, fixture_posts):
    parser = parser_with(config, FakeClient(content=[FakeTextBlock()]))
    result = parser.parse_post(fixture_posts["FIXMURPH04"])
    assert result.deals == []
    assert not result.ok
    assert "tool call" in result.error


def test_deals_field_of_the_wrong_type_yields_no_deals(config, fixture_posts):
    parser = parser_with(config, FakeClient(content=[FakeBlock({"deals": "none"})]))
    result = parser.parse_post(fixture_posts["FIXMURPH04"])
    assert result.deals == []
    assert not result.ok


def test_a_deal_with_no_text_is_dropped(config, fixture_posts):
    parser = parser_with(config, FakeClient(deals=[{"deal_text": "   ", "price": 2}]))
    assert parser.parse_post(fixture_posts["FIXMURPH01"]).deals == []


def test_non_object_entries_are_ignored(config, fixture_posts):
    parser = parser_with(config, FakeClient(deals=["$2 drafts", None, 42]))
    assert parser.parse_post(fixture_posts["FIXMURPH01"]).deals == []


# --------------------------------------------------------------------------- #
# the spec's worked examples
# --------------------------------------------------------------------------- #


def test_tonight_deal_is_dated_and_one_off(config, fixture_posts):
    parser = parser_with(config, FakeClient(deals=[{
        "deal_text": "$2 domestics", "item": "domestics", "price": 2,
        "day_of_week": None, "specific_date": "2026-09-17", "start_time": None,
        "end_time": None, "recurrence": "once", "confidence": "high", "source": "caption",
    }]))
    deal = parser.parse_post(fixture_posts["FIXMURPH01"]).deals[0]
    assert deal.specific_date == "2026-09-17"
    assert deal.recurrence == "once"
    assert deal.price == 2.0
    assert deal.venue_name == "Murphy's Pub"


def test_weekly_deal_keeps_weekday_and_no_date(config, fixture_posts):
    parser = parser_with(config, FakeClient(deals=[{
        "deal_text": "half off wings", "item": "wings", "price": None,
        "day_of_week": 3, "specific_date": None, "start_time": None, "end_time": None,
        "recurrence": "weekly", "confidence": "high", "source": "caption",
    }]))
    deal = parser.parse_post(fixture_posts["FIXMURPH02"]).deals[0]
    assert deal.day_of_week == 3
    assert deal.specific_date is None
    assert deal.recurrence == "weekly"
    assert deal.price is None


def test_only_the_deal_is_kept_not_the_event(config, fixture_posts):
    """"Trivia at 8, $3 wells" -> one row, the drink special."""
    parser = parser_with(config, FakeClient(deals=[{
        "deal_text": "$3 wells", "item": "wells", "price": 3, "day_of_week": None,
        "specific_date": "2026-09-12", "start_time": None, "end_time": None,
        "recurrence": "once", "confidence": "high", "source": "caption",
    }]))
    deals = parser.parse_post(fixture_posts["FIXMURPH03"]).deals
    assert len(deals) == 1
    assert deals[0].item == "wells"


def test_vague_deal_keeps_text_and_nulls_everything_else(config, fixture_posts):
    parser = parser_with(config, FakeClient(deals=[{
        "deal_text": "same deal as always this week", "item": None, "price": None,
        "day_of_week": None, "specific_date": None, "start_time": None, "end_time": None,
        "recurrence": "unknown", "confidence": "low", "source": "caption",
    }]))
    deals = parser.parse_post(fixture_posts["FIXLADDER02"]).deals
    # "this week" is real date language, so it expands to that week's dates.
    assert all(deal.confidence == "low" for deal in deals)
    assert all(deal.item is None and deal.price is None for deal in deals)
    assert all("same deal as always" in deal.deal_text for deal in deals)


def test_weekly_flyer_grid_becomes_one_row_per_day(config, fixture_posts):
    grid = [
        {"deal_text": f"day {dow} special", "item": None, "price": 3.0, "day_of_week": dow,
         "specific_date": None, "start_time": None, "end_time": None,
         "recurrence": "weekly", "confidence": "high", "source": "image"}
        for dow in range(7)
    ]
    parser = parser_with(config, FakeClient(deals=grid))
    deals = parser.parse_post(fixture_posts["FIXLADDER01"]).deals
    assert len(deals) == 7
    assert sorted(deal.day_of_week for deal in deals) == list(range(7))
    assert all(deal.source == "image" for deal in deals)
    assert all(deal.recurrence == "weekly" for deal in deals)


# --------------------------------------------------------------------------- #
# date resolution fallback
# --------------------------------------------------------------------------- #


def test_dateless_deal_inherits_the_captions_tonight(config, fixture_posts):
    """The flyer case: no date from the model, "TONIGHT" in the caption."""
    parser = parser_with(config, FakeClient(deals=[{
        "deal_text": "$1 wings 5-9pm", "item": "wings", "price": 1, "day_of_week": None,
        "specific_date": None, "start_time": "17:00", "end_time": "21:00",
        "recurrence": "unknown", "confidence": "high", "source": "image",
    }]))
    deals = parser.parse_post(fixture_posts["FIXLADDER01"]).deals
    assert len(deals) == 1
    assert deals[0].specific_date == "2026-09-16"  # local date, not the UTC 17th
    assert deals[0].recurrence == "once"


def test_model_supplied_date_is_not_overridden(config, fixture_posts):
    parser = parser_with(config, FakeClient(deals=[{
        "deal_text": "$5 pitchers", "item": "pitchers", "price": 5, "day_of_week": None,
        "specific_date": "2026-12-25", "start_time": None, "end_time": None,
        "recurrence": "once", "confidence": "high", "source": "image",
    }]))
    assert parser.parse_post(fixture_posts["FIXLADDER01"]).deals[0].specific_date == "2026-12-25"


def test_daily_recurrence_is_left_alone(config, fixture_posts):
    parser = parser_with(config, FakeClient(deals=[{
        "deal_text": "$2 tacos", "item": "tacos", "price": 2, "day_of_week": None,
        "specific_date": None, "start_time": None, "end_time": None,
        "recurrence": "daily", "confidence": "high", "source": "caption",
    }]))
    deals = parser.parse_post(fixture_posts["FIXMURPH01"]).deals
    assert len(deals) == 1
    assert deals[0].recurrence == "daily"
    assert deals[0].specific_date is None


# --------------------------------------------------------------------------- #
# field cleaning
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw,expected",
    [("$2.50", 2.5), (2, 2.0), ("3", 3.0), (None, None), ("free", None), (-1, None), (True, None)],
)
def test_price_cleaning(raw, expected):
    from bardeals.parse import _clean_price

    assert _clean_price(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [("5:00", "05:00"), ("17:30", "17:30"), ("25:00", None), ("nonsense", None), (None, None)],
)
def test_time_cleaning(raw, expected):
    from bardeals.parse import _clean_time

    assert _clean_time(raw) == expected


@pytest.mark.parametrize("raw,expected", [("2026-09-17", "2026-09-17"), ("nope", None),
                                          ("2026-13-40", None), (None, None)])
def test_date_cleaning(raw, expected):
    from bardeals.parse import _clean_date

    assert _clean_date(raw) == expected


def test_out_of_range_weekday_from_the_model_is_nulled(config, fixture_posts):
    parser = parser_with(config, FakeClient(deals=[{
        "deal_text": "$2 drafts", "item": None, "price": 2, "day_of_week": 11,
        "specific_date": "2026-09-17", "start_time": None, "end_time": None,
        "recurrence": "once", "confidence": "high", "source": "caption",
    }]))
    assert parser.parse_post(fixture_posts["FIXMURPH01"]).deals[0].day_of_week is None


def test_bogus_enums_are_coerced(config, fixture_posts):
    parser = parser_with(config, FakeClient(deals=[{
        "deal_text": "$2 drafts", "item": None, "price": 2, "day_of_week": None,
        "specific_date": "2026-09-17", "start_time": None, "end_time": None,
        "recurrence": "fortnightly", "confidence": "certain", "source": "telepathy",
    }]))
    deal = parser.parse_post(fixture_posts["FIXMURPH01"]).deals[0]
    assert deal.recurrence == "unknown"
    assert deal.confidence == "low"
    assert deal.source == "caption"


# --------------------------------------------------------------------------- #
# failure handling
# --------------------------------------------------------------------------- #


def test_api_failure_returns_an_error_not_an_exception(config, fixture_posts):
    parser = parser_with(config, FakeClient(error=RuntimeError("connection reset")))
    result = parser.parse_post(fixture_posts["FIXMURPH01"])
    assert result.deals == []
    assert not result.ok
    assert "connection reset" in result.error


def test_strict_rejection_retries_without_strict(config, fixture_posts):
    """Older models may reject a strict schema; fall back rather than fail."""

    class PickyMessages(FakeMessages):
        def create(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise ValueError("tool schema is invalid: strict not supported")
            return FakeResponse([FakeBlock({"deals": []})])

    client = FakeClient(deals=[])
    client.messages = PickyMessages([])
    parser = parser_with(config, client)
    result = parser.parse_post(fixture_posts["FIXMURPH01"])

    assert result.ok
    assert len(client.messages.calls) == 2
    assert client.messages.calls[0]["tools"][0].get("strict") is True
    assert "strict" not in client.messages.calls[1]["tools"][0]


def test_unrelated_errors_are_not_retried(config, fixture_posts):
    parser = parser_with(config, FakeClient(error=RuntimeError("rate limited")))
    parser.parse_post(fixture_posts["FIXMURPH01"])
    assert len(parser.client.messages.calls) == 1


# --------------------------------------------------------------------------- #
# request shape and storage
# --------------------------------------------------------------------------- #


def test_request_forces_the_tool_and_uses_the_configured_model(config, fixture_posts):
    client = FakeClient(deals=[])
    parser_with(config, client).parse_post(fixture_posts["FIXMURPH01"])
    call = client.messages.calls[0]
    assert call["model"] == config.model == "claude-sonnet-4-5"
    assert call["tool_choice"] == {"type": "tool", "name": TOOL_NAME}
    assert call["system"] is SYSTEM_PROMPT


def test_parse_and_store_writes_deals_and_the_raw_response(config, conn, fixture_posts):
    post = fixture_posts["FIXMURPH01"]
    db.insert_post(conn, post)
    client = FakeClient(deals=[{
        "deal_text": "$2 domestics", "item": "domestics", "price": 2, "day_of_week": None,
        "specific_date": "2026-09-16", "start_time": None, "end_time": None,
        "recurrence": "once", "confidence": "high", "source": "caption",
    }])
    result = parser_with(config, client).parse_and_store(post, conn)

    assert len(result.deals) == 1
    assert db.count_deals(conn) == 1
    assert db.get_post(conn, post.shortcode)["parsed"] == 1
    logged = db.latest_parse_log(conn, post.shortcode)
    assert logged["deal_count"] == 1
    assert logged["model"] == config.model


def test_parse_and_store_marks_a_zero_deal_post_parsed(config, conn, fixture_posts):
    """A no-deal post must not be retried on the next run."""
    post = fixture_posts["FIXMURPH04"]
    db.insert_post(conn, post)
    parser_with(config, FakeClient(deals=[])).parse_and_store(post, conn)

    assert db.count_deals(conn) == 0
    assert db.get_post(conn, post.shortcode)["parsed"] == 1
    assert db.unparsed_posts(conn) == []


def test_a_failed_parse_leaves_the_post_for_retry(config, conn, fixture_posts):
    post = fixture_posts["FIXMURPH01"]
    db.insert_post(conn, post)
    parser_with(config, FakeClient(error=RuntimeError("boom"))).parse_and_store(post, conn)

    assert db.get_post(conn, post.shortcode)["parsed"] == 0
    assert db.latest_parse_log(conn, post.shortcode)["error"]


@pytest.mark.parametrize("content", [
    [FakeTextBlock()],
    [FakeBlock({"deals": "none"})],
    [FakeBlock({})],
    [FakeBlock({"deals": [{"deal_text": 42}]})],
])
def test_invalid_extraction_remains_retryable(config, conn, fixture_posts, content):
    """A failed response cannot permanently hide a post's actual specials."""
    post = fixture_posts["FIXMURPH01"]
    db.insert_post(conn, post)
    result = parser_with(config, FakeClient(content=content)).parse_and_store(post, conn)

    assert not result.ok
    assert result.deals == []
    assert db.count_deals(conn) == 0
    assert db.get_post(conn, post.shortcode)["parsed"] == 0
    assert db.latest_parse_log(conn, post.shortcode)["error"]
    assert db.latest_parse_log(conn, post.shortcode)["raw_response"]

    # A later successful response can finish processing the same post.
    parser_with(config, FakeClient(deals=[])).parse_and_store(post, conn)
    assert db.get_post(conn, post.shortcode)["parsed"] == 1


def test_truncated_response_does_not_store_partial_deals(config, conn, fixture_posts, monkeypatch):
    post = fixture_posts["FIXMURPH01"]
    db.insert_post(conn, post)
    response = FakeResponse([FakeBlock({"deals": [{"deal_text": "$2 drafts"}]})])
    response.stop_reason = "max_tokens"
    parser = parser_with(config, FakeClient(deals=[]))
    monkeypatch.setattr(parser, "_call_model", lambda *args, **kwargs: response)

    result = parser.parse_and_store(post, conn)

    assert not result.ok
    assert "truncated" in result.error
    assert db.count_deals(conn) == 0
    assert db.get_post(conn, post.shortcode)["parsed"] == 0


def test_reparsing_the_same_post_does_not_duplicate_deals(config, conn, fixture_posts):
    post = fixture_posts["FIXMURPH01"]
    db.insert_post(conn, post)
    payload = [{
        "deal_text": "$2 domestics", "item": "domestics", "price": 2, "day_of_week": None,
        "specific_date": "2026-09-16", "start_time": None, "end_time": None,
        "recurrence": "once", "confidence": "high", "source": "caption",
    }]
    for _ in range(3):
        parser_with(config, FakeClient(deals=payload)).parse_and_store(post, conn)
    assert db.count_deals(conn) == 1


def test_missing_api_key_raises_only_when_a_call_is_attempted(config):
    from dataclasses import replace

    parser = DealParser(replace(config, anthropic_api_key=None))
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        _ = parser.client


def test_low_confidence_deal_is_never_date_expanded(config, fixture_posts):
    """"same deal as always this week" must stay one row with nulls, not seven."""
    parser = parser_with(config, FakeClient(deals=[{
        "deal_text": "same deal as always this week", "item": None, "price": None,
        "day_of_week": None, "specific_date": None, "start_time": None, "end_time": None,
        "recurrence": "unknown", "confidence": "low", "source": "caption",
    }]))
    deals = parser.parse_post(fixture_posts["FIXLADDER02"]).deals
    assert len(deals) == 1
    deal = deals[0]
    assert deal.deal_text == "same deal as always this week"
    assert deal.confidence == "low"
    assert deal.specific_date is None
    assert deal.day_of_week is None
    assert deal.item is None and deal.price is None
    assert deal.recurrence == "unknown"


def test_all_weekend_still_expands_for_confident_deals(config, fixture_posts):
    """Multi-day expansion is still right when the deal is not vague."""
    parser = parser_with(config, FakeClient(deals=[{
        "deal_text": "$4 shots all weekend", "item": "shots", "price": 4, "day_of_week": None,
        "specific_date": None, "start_time": None, "end_time": None,
        "recurrence": "unknown", "confidence": "high", "source": "caption",
    }]))
    # FIXMURPH02 is posted Monday 2026-09-14, so the whole weekend is still ahead.
    deals = parser.parse_post(fixture_posts["FIXMURPH02"]).deals
    assert len(deals) == 3  # Fri, Sat, Sun
    assert all(deal.recurrence == "once" for deal in deals)
    assert sorted(deal.specific_date for deal in deals) == [
        "2026-09-18", "2026-09-19", "2026-09-20"
    ]


def test_all_weekend_posted_on_a_saturday_covers_only_what_remains(config, fixture_posts):
    """FIXMURPH03 is posted Saturday 2026-09-12; Friday has already gone."""
    parser = parser_with(config, FakeClient(deals=[{
        "deal_text": "$4 shots all weekend", "item": "shots", "price": 4, "day_of_week": None,
        "specific_date": None, "start_time": None, "end_time": None,
        "recurrence": "unknown", "confidence": "high", "source": "caption",
    }]))
    deals = parser.parse_post(fixture_posts["FIXMURPH03"]).deals
    assert sorted(deal.specific_date for deal in deals) == ["2026-09-12", "2026-09-13"]
