"""LLM extraction: caption + image -> structured deals.

One multimodal message carries both the flyer image and the caption, and the
model answers through a strict tool schema. The reply is never regex-parsed -
if the tool call is missing or malformed, the post yields zero deals and the
raw response is logged for inspection. Failed responses remain eligible for a
later retry; only a valid empty deal list counts as a successful no-deal parse.

Two things drive the design:

*The image matters more than the caption.* Bars bake prices into a JPEG and
caption it "TONIGHT 🔥". The prompt says so explicitly and the image is placed
before the text in the message.

*Most posts contain no deal at all.* Returning an empty list is the easy path,
not an error path, and the prompt is loaded with counter-examples so "🍻 see
you tonight" cannot hallucinate a special.
"""

from __future__ import annotations

import base64
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path
from typing import Any, Sequence

from . import dates
from .config import Config, get_config, setup_logging
from .db import Deal

#: Anthropic caps a single image at 5MB; stay well under it.
MAX_IMAGE_BYTES = 3_500_000

TOOL_NAME = "record_deals"

#: Strict schema. Every field is required and nullable rather than optional, so
#: the model must make an explicit decision about each one instead of quietly
#: omitting it.
DEAL_TOOL: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Record every food or drink deal advertised in this bar's Instagram post. "
        "Call this exactly once. If the post advertises no deal, call it with an "
        "empty deals list - that is the normal case, not a failure."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "deals": {
                "type": "array",
                "description": "One entry per distinct deal. Empty when the post has none.",
                "items": {
                    "type": "object",
                    "properties": {
                        "deal_text": {
                            "type": "string",
                            "description": (
                                "Short verbatim-ish description of the deal as advertised, "
                                'e.g. "$2 domestic drafts" or "half off wings".'
                            ),
                        },
                        "item": {
                            "type": ["string", "null"],
                            "description": (
                                'What is on offer, e.g. "domestic drafts", "wells", '
                                '"pizza slice". Null if not identifiable.'
                            ),
                        },
                        "price": {
                            "type": ["number", "null"],
                            "description": (
                                "Numeric price in dollars, e.g. 2 for $2. Null for "
                                'percentage or unpriced offers like "half off".'
                            ),
                        },
                        "day_of_week": {
                            "type": ["integer", "null"],
                            "description": (
                                "0=Monday .. 6=Sunday, for weekly recurring deals. "
                                "Null if the deal is not weekly."
                            ),
                        },
                        "specific_date": {
                            "type": ["string", "null"],
                            "description": (
                                "ISO date (YYYY-MM-DD) for a one-off deal. Null for "
                                "recurring deals. Resolve relative words like "
                                '"tonight" against the post date given in the prompt.'
                            ),
                        },
                        "start_time": {
                            "type": ["string", "null"],
                            "description": '24-hour HH:MM start time, e.g. "17:00". Null if absent.',
                        },
                        "end_time": {
                            "type": ["string", "null"],
                            "description": '24-hour HH:MM end time, e.g. "21:00". Null if absent.',
                        },
                        "recurrence": {
                            "type": "string",
                            "enum": ["once", "weekly", "daily", "unknown"],
                            "description": (
                                "'once' for a single dated occurrence, 'weekly' for every "
                                "week on day_of_week, 'daily' for every day, 'unknown' "
                                "when the post does not say."
                            ),
                        },
                        "confidence": {
                            "type": "string",
                            "enum": ["high", "medium", "low"],
                            "description": (
                                "'high' when price and timing are stated plainly. 'low' for "
                                'vague references like "same deal as always".'
                            ),
                        },
                        "source": {
                            "type": "string",
                            "enum": ["caption", "image", "both"],
                            "description": (
                                "Where the deal information actually came from. Use 'image' "
                                "when the prices are only in the flyer."
                            ),
                        },
                    },
                    "required": [
                        "deal_text", "item", "price", "day_of_week", "specific_date",
                        "start_time", "end_time", "recurrence", "confidence", "source",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["deals"],
        "additionalProperties": False,
    },
}

SYSTEM_PROMPT = """\
You extract drink and food deals from Instagram posts by bars in Cincinnati, Ohio.

You are given a post's caption and, usually, its image. Report what the post \
actually advertises by calling the record_deals tool exactly once.

THE IMAGE MATTERS MORE THAN THE CAPTION.
Bars post a flyer with the real prices baked into the JPEG and write something \
like "TONIGHT 🔥" as the caption. Read the image carefully and take prices, \
days, and times from it. Set source to reflect where the information really \
came from: 'image' when only the flyer has it, 'caption' when only the text \
has it, 'both' when they agree.

MOST POSTS CONTAIN NO DEAL. That is normal and expected.
Return an empty deals list for anything that is not an advertised food or \
drink offer: photos of customers, game-day hype, staff announcements, live \
music, "see you tonight", holiday greetings. Never invent a deal to avoid \
returning an empty list. An empty list is a correct, complete answer.

A deal requires an actual offer - a price, a discount, or a free item. \
Atmosphere, events, and hours are not deals.

WORKED EXAMPLES

1. Caption "$2 domestics tonight!" posted Wednesday 2026-09-17:
   one deal. deal_text "$2 domestics", item "domestics", price 2, \
specific_date "2026-09-17", day_of_week null, recurrence "once", \
confidence "high", source "caption".
   "Tonight" means the date the post was published, which is given below.

2. Caption "Every Thursday: half off wings":
   one deal. deal_text "half off wings", item "wings", price null (it is a \
percentage, not a dollar amount), day_of_week 3, specific_date null, \
recurrence "weekly", confidence "high".
   Recurring deals get day_of_week and no specific_date.

3. Caption "Trivia at 8, $3 wells":
   ONE deal, not two. Trivia is an event, not a deal - do not record it. \
Record only deal_text "$3 wells", item "wells", price 3, start_time "20:00" \
only if the time clearly applies to the drink special rather than the trivia.

4. Caption "same deal as always this week":
   one deal with confidence "low". Preserve deal_text as "same deal as always \
this week" and leave item, price, day_of_week, specific_date, start_time and \
end_time all null, recurrence "unknown". Do not guess what the deal was.

5. A flyer image showing a weekly grid of Monday through Sunday specials:
   emit ONE DEAL PER DAY - seven entries, each with its own day_of_week \
(0=Monday .. 6=Sunday), its own deal_text and price, recurrence "weekly", \
specific_date null, source "image".

6. Caption "🍻 see you tonight" with a photo of a crowded bar:
   ZERO deals. Return an empty list.

Report each distinct offer separately. If a single post advertises "$2 drafts \
and $5 pitchers", that is two deals.
"""


WEBSITE_PROMPT = """\
You extract drink and food deals from the WEBSITE of a bar in Cincinnati, Ohio.

You are given text scraped from one page of the bar's own site - a homepage, a \
specials page, or a menu (sometimes extracted from a PDF). Report what the page \
advertises by calling the record_deals tool exactly once.

A MENU IS NOT A LIST OF DEALS. This is the most important rule.
A menu listing "Cheeseburger 12.99" or "Margherita Pizza 15" states a REGULAR \
PRICE, not a deal. Recording menu items as deals would bury the report under \
every dish the kitchen serves. Record a row ONLY when the page describes an \
actual OFFER:
  - a discount ("half off", "$1 off", "2 for 1", "BOGO")
  - a happy hour, or any price tied to a time window
  - a price explicitly tied to a day ("Taco Tuesday $2 tacos")
  - something free ("kids eat free", "free chips with any entree")
  - an explicitly named special ("$10.99 lunch special")
If you cannot tell whether a price is a discount or just the menu price, it is \
the menu price. Do not record it.

WEBSITE DEALS ARE USUALLY RECURRING, NOT ONE-OFF.
A website states what a bar does every week, so prefer recurrence "weekly" with \
a day_of_week when a day is named, or "daily" when the page says every day or \
gives a daily happy hour. Use specific_date only when the page names an actual \
calendar date. A website almost never means "tonight".

WORKED EXAMPLES

1. "Enjoy $10.99 Mexican lunch specials Monday through Friday from 11 a.m. to \
2 p.m." -> FIVE deals, one per weekday (day_of_week 0, 1, 2, 3, 4). Each has \
deal_text "$10.99 Mexican lunch special", price 10.99, start_time "11:00", \
end_time "14:00", recurrence "weekly", confidence "high". A Monday-through-\
Friday span becomes one row per day.

2. "Daily Happy Hour runs from 4 to 8 p.m. with margarita specials" -> ONE deal. \
deal_text "happy hour margarita specials", item "margaritas", price null (no \
amount is stated), start_time "16:00", end_time "20:00", recurrence "daily", \
confidence "medium" - the discount is real but unpriced.

3. "Kids Eat Free every Tuesday with one free kids meal per adult entree" -> ONE \
deal. deal_text "kids eat free", price 0, day_of_week 1, recurrence "weekly", \
confidence "high".

4. A menu section reading "WINGS 10.99 / BONELESS WINGS 11.99 / NACHOS 13" -> \
ZERO deals. Those are menu prices.

5. Navigation text, addresses, phone numbers, opening hours, "Order Online", \
job postings, catering enquiries -> ZERO deals. Opening hours are not a happy \
hour.

6. "We have an extensive beer list and daily specials on cocktails" -> ZERO \
deals. It claims specials exist but names none, so there is nothing to record. \
Do not invent one.

Returning an empty list is a correct, complete answer, and is the right answer \
for most pages.
"""


@dataclass
class ParseResult:
    """Outcome of parsing one post."""

    shortcode: str
    deals: list[Deal] = field(default_factory=list)
    raw_response: str = ""
    error: str | None = None
    model: str = ""

    @property
    def ok(self) -> bool:
        """True when the parse completed without an error."""
        return self.error is None


def encode_image(path: str | Path | None, logger: logging.Logger | None = None) -> dict | None:
    """Build a base64 image content block for the Messages API.

    Args:
        path: Local image path, or None.
        logger: Optional logger for skip reasons.

    Returns:
        An image content block, or None when there is no usable image.
    """
    log = logger or logging.getLogger("bardeals")
    if not path:
        return None
    image_path = Path(path)
    try:
        if not image_path.exists():
            log.debug("no image on disk at %s", image_path)
            return None
        size = image_path.stat().st_size
        if size == 0:
            log.warning("empty image file %s", image_path)
            return None
        if size > MAX_IMAGE_BYTES:
            log.warning("image %s is %d bytes, too large to send - skipping", image_path, size)
            return None
        payload = base64.standard_b64encode(image_path.read_bytes()).decode("ascii")
    except OSError as exc:
        log.warning("could not read image %s: %s", image_path, exc)
        return None

    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": payload},
    }


def build_user_content(post: Any, logger: logging.Logger | None = None) -> list[dict]:
    """Assemble the multimodal user turn for one post.

    The image is placed first so the model reads the flyer before the caption,
    matching the instruction that the image outranks the text.

    Args:
        post: A ``RawPost``-shaped object.
        logger: Optional logger.

    Returns:
        A list of content blocks for the Messages API.
    """
    content: list[dict] = []
    image_block = encode_image(getattr(post, "local_image_path", None), logger)
    if image_block:
        content.append(image_block)

    local = dates.to_local(post.posted_at)
    caption = post.caption or ""

    if getattr(post, "source_kind", "instagram") == "website":
        kind = "menu PDF" if getattr(post, "typename", "") == "WebsitePDF" else "web page"
        lines = [
            f"Bar: {post.username}",
            f"Source: the bar's own {kind} at {post.permalink}",
            f"Fetched: {local:%A %Y-%m-%d at %H:%M} local time (America/New_York)",
            "",
            "This is website text, not a social post. It describes what the bar does",
            "on an ongoing basis, and most of it is menu items and boilerplate.",
            "",
            "Page text:",
            caption if caption.strip() else "(no text)",
        ]
    else:
        lines = [
            f"Bar Instagram account: @{post.username}",
            f"Post published: {local:%A %Y-%m-%d at %H:%M} local time (America/New_York)",
            f'Today\'s date for "tonight"/"today" is therefore {local:%Y-%m-%d}.',
            "",
            "Image attached above: " + ("yes" if image_block else "no image available"),
            "",
            "Caption:",
            caption if caption.strip() else "(no caption)",
        ]

    content.append({"type": "text", "text": "\n".join(lines)})
    return content


class DealParser:
    """Extracts deals from posts via the Anthropic Messages API."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        client: Any = None,
        logger: logging.Logger | None = None,
    ) -> None:
        """Create a parser.

        Args:
            config: Loaded config; read from ``config.yaml`` when omitted.
            client: An Anthropic client. Injected by tests; built lazily from
                ``ANTHROPIC_API_KEY`` otherwise.
            logger: Logger to use.

        Raises:
            RuntimeError: Only when a real API call is attempted with no key.
        """
        self.config = config or get_config()
        self.logger = logger or setup_logging(self.config)
        self._client = client

    @property
    def client(self) -> Any:
        """The Anthropic client, constructed on first use."""
        if self._client is None:
            if not self.config.anthropic_api_key:
                raise RuntimeError(
                    "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key."
                )
            import anthropic

            self._client = anthropic.Anthropic(api_key=self.config.anthropic_api_key)
        return self._client

    # ------------------------------------------------------------------ #
    # model call
    # ------------------------------------------------------------------ #

    def _call_model(self, content: list[dict], system: str = SYSTEM_PROMPT) -> Any:
        """Send one request, retrying without ``strict`` if the schema is rejected.

        Older models do not all accept strict schemas with nullable type
        unions. Rather than fail the run, fall back to the same schema without
        ``strict`` - the response is validated in Python either way.
        """
        request = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": content}],
            "tools": [DEAL_TOOL],
            "tool_choice": {"type": "tool", "name": TOOL_NAME},
        }
        try:
            return self.client.messages.create(**request)
        except Exception as exc:  # noqa: BLE001
            message = str(exc).lower()
            if "strict" not in message and "schema" not in message:
                raise
            self.logger.warning("strict schema rejected (%s) - retrying without strict", exc)
            relaxed = dict(DEAL_TOOL)
            relaxed.pop("strict", None)
            request["tools"] = [relaxed]
            return self.client.messages.create(**request)

    @staticmethod
    def _extract_tool_input(response: Any) -> dict | None:
        """Pull the tool call's arguments out of a response.

        Returns None when the model answered without a usable tool call.
        """
        for block in getattr(response, "content", []) or []:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", "") == TOOL_NAME:
                payload = getattr(block, "input", None)
                if isinstance(payload, dict):
                    return payload
                # Defensive: some transports hand back a JSON string.
                if isinstance(payload, str):
                    try:
                        loaded = json.loads(payload)
                        return loaded if isinstance(loaded, dict) else None
                    except json.JSONDecodeError:
                        return None
        return None

    @staticmethod
    def _serialize(response: Any) -> str:
        """Best-effort JSON serialization of a response, for the parse log."""
        for method in ("to_json", "model_dump_json"):
            serializer = getattr(response, method, None)
            if callable(serializer):
                try:
                    return serializer()
                except Exception:  # noqa: BLE001
                    continue
        try:
            return json.dumps(response, default=str)
        except (TypeError, ValueError):
            return repr(response)

    # ------------------------------------------------------------------ #
    # normalization
    # ------------------------------------------------------------------ #

    def _build_deals(self, post: Any, raw_deals: Sequence[dict]) -> list[Deal]:
        """Turn raw tool arguments into validated :class:`Deal` rows."""
        venue_name = self.config.venue_name(post.username)
        deals: list[Deal] = []

        for entry in raw_deals:
            if not isinstance(entry, dict):
                self.logger.warning("ignoring non-object deal entry: %r", entry)
                continue
            text = (entry.get("deal_text") or "").strip()
            if not text:
                self.logger.warning("ignoring deal with no deal_text in %s", post.shortcode)
                continue

            deal = Deal(
                shortcode=post.shortcode,
                venue_slug=post.username,
                venue_name=venue_name,
                deal_text=text,
                item=_clean_str(entry.get("item")),
                price=_clean_price(entry.get("price")),
                day_of_week=_clean_int(entry.get("day_of_week")),
                specific_date=_clean_date(entry.get("specific_date")),
                start_time=_clean_time(entry.get("start_time")),
                end_time=_clean_time(entry.get("end_time")),
                recurrence=str(entry.get("recurrence") or "unknown"),
                confidence=str(entry.get("confidence") or "medium"),
                source=str(entry.get("source") or "caption"),
            )
            deals.extend(self._resolve_dates(deal, post))

        return deals

    def _resolve_dates(self, deal: Deal, post: Any) -> list[Deal]:
        """Fill in missing dates from the post's own language.

        The model's own answer wins when it supplied one - it saw the flyer.
        This only covers the gap where it left both the date and the weekday
        null, and expands multi-day phrases like "all weekend" into one row per
        date. Low-confidence deals are left untouched: a vague reference gets
        its text preserved and everything else null.
        """
        if deal.specific_date or deal.day_of_week is not None:
            return [deal]
        if deal.recurrence == "daily":
            return [deal]
        if deal.confidence == "low":
            # "same deal as always this week" must keep its text and leave every
            # other field null. Expanding it across the week would flood every
            # day's report with a deal nobody can actually describe.
            return [deal]

        resolution = dates.resolve_deal_dates(deal.deal_text, post.caption or "", post.posted_at)
        if resolution.recurrence == "unknown":
            return [deal]

        self.logger.debug(
            "%s: resolved %r -> %s via %r",
            post.shortcode,
            deal.deal_text,
            resolution.iso_dates or resolution.recurrence,
            resolution.matched,
        )

        if resolution.recurrence == "weekly":
            deal.day_of_week = resolution.day_of_week
            deal.recurrence = "weekly"
            return [deal]

        if resolution.recurrence == "daily":
            deal.recurrence = "daily"
            return [deal]

        if not resolution.dates:
            return [deal]

        expanded: list[Deal] = []
        for day in resolution.dates:
            clone = Deal(**{**deal.to_dict(), "id": None})
            clone.specific_date = day.isoformat()
            clone.recurrence = "once"
            expanded.append(clone)
        return expanded

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #

    def parse_post(self, post: Any) -> ParseResult:
        """Extract deals from a single post.

        Never raises: an API failure, a malformed reply, or a missing tool call
        all return a :class:`ParseResult` with zero deals and an error recorded.

        Args:
            post: A ``RawPost``-shaped object.

        Returns:
            The parse outcome, including the raw model response.
        """
        content = build_user_content(post, self.logger)
        source_kind = getattr(post, "source_kind", "instagram")
        # A bar's website needs different instructions from its Instagram: the
        # dominant failure there is reading a menu as a list of deals.
        system = WEBSITE_PROMPT if source_kind == "website" else SYSTEM_PROMPT
        has_image = any(block["type"] == "image" for block in content)
        self.logger.info(
            "PARSE [%s] %s/%s (image=%s, text=%d chars)",
            source_kind,
            post.username,
            post.shortcode,
            has_image,
            len(post.caption or ""),
        )

        try:
            response = self._call_model(content, system=system)
        except Exception as exc:  # noqa: BLE001 - one bad post never aborts a run
            self.logger.exception("LLM call failed for %s: %s", post.shortcode, exc)
            return ParseResult(
                shortcode=post.shortcode,
                error=f"{type(exc).__name__}: {exc}",
                model=self.config.model,
            )

        raw = self._serialize(response)
        if getattr(response, "stop_reason", None) == "max_tokens":
            error = "Model response was truncated before extraction completed"
            self.logger.warning("%s: %s", post.shortcode, error)
            return ParseResult(shortcode=post.shortcode, raw_response=raw,
                               error=error, model=self.config.model)

        payload = self._extract_tool_input(response)

        if payload is None:
            error = "Model returned no valid deal extraction tool call"
            self.logger.warning("%s: %s", post.shortcode, error)
            return ParseResult(shortcode=post.shortcode, raw_response=raw,
                               error=error, model=self.config.model)

        raw_deals = payload.get("deals")
        if not isinstance(raw_deals, list):
            error = "Model tool response must contain a deals list"
            self.logger.warning("%s: %s (got %s)", post.shortcode, error, type(raw_deals))
            return ParseResult(shortcode=post.shortcode, raw_response=raw,
                               error=error, model=self.config.model)

        try:
            deals = self._build_deals(post, raw_deals)
        except Exception as exc:  # noqa: BLE001 - malformed output must not abort a run
            self.logger.exception("Invalid deal extraction for %s: %s", post.shortcode, exc)
            return ParseResult(shortcode=post.shortcode, raw_response=raw,
                               error=f"Invalid deal extraction: {type(exc).__name__}: {exc}",
                               model=self.config.model)
        self.logger.info("%s -> %d deal(s)", post.shortcode, len(deals))
        return ParseResult(
            shortcode=post.shortcode, deals=deals, raw_response=raw, model=self.config.model
        )

    def parse_and_store(self, post: Any, conn: sqlite3.Connection) -> ParseResult:
        """Parse a post, store its deals, and record the raw response.

        A valid empty deal list marks the post parsed. Failed or malformed
        responses leave it unparsed so a future run can retry it.

        Args:
            post: A ``RawPost``-shaped object.
            conn: Open database connection.

        Returns:
            The parse outcome.
        """
        from . import db

        result = self.parse_post(post)
        db.log_parse(
            conn,
            shortcode=post.shortcode,
            model=result.model or self.config.model,
            raw_response=result.raw_response,
            deal_count=len(result.deals),
            error=result.error,
        )
        if result.deals:
            db.insert_deals(conn, result.deals)
        if result.ok:
            db.mark_parsed(conn, post.shortcode)
        return result


# --------------------------------------------------------------------------- #
# field cleaning
# --------------------------------------------------------------------------- #


def _clean_str(value: Any) -> str | None:
    """Trim a string field, mapping blanks and JSON nulls to None."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_price(value: Any) -> float | None:
    """Coerce a price to a positive float, or None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        price = float(str(value).replace("$", "").strip())
    except (TypeError, ValueError):
        return None
    return price if price >= 0 else None


def _clean_int(value: Any) -> int | None:
    """Coerce a weekday to an int in 0-6, or None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= 6 else None


def _clean_date(value: Any) -> str | None:
    """Validate an ISO date string, or None."""
    text = _clean_str(value)
    if not text:
        return None
    try:
        return _date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _clean_time(value: Any) -> str | None:
    """Normalize a time to zero-padded ``HH:MM``, or None."""
    text = _clean_str(value)
    if not text:
        return None
    parts = text.split(":")
    if len(parts) < 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except (TypeError, ValueError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"
