"""Generate synthetic fixtures for offline development.

These are hand-authored, NOT captured from Instagram. Live anonymous fetching
was blocked (HTTP 429 on ``web_profile_info``) when this project was built, so
the fixture set had to be written by hand to unblock the rest of the pipeline.

``scrape.Scraper._save_fixture`` overwrites these entries with real data on the
first successful live run, so this script is only a bootstrap.

Run::

    python tests/fixtures/make_fixtures.py
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageDraw

FIXTURES_DIR = Path(__file__).resolve().parent
IMAGES_DIR = FIXTURES_DIR / "images"

# Anchored to a fixed week so date-resolution tests stay deterministic.
# 2026-09-16 is a Wednesday.
BASE = datetime(2026, 9, 16, 22, 30, tzinfo=timezone.utc)


def _post(
    shortcode: str,
    username: str,
    caption: str,
    days_offset: int,
    *,
    has_image: bool = False,
    is_video: bool = False,
    typename: str = "GraphImage",
) -> dict:
    """Build one fixture post dict matching ``RawPost.to_dict``."""
    posted_at = BASE + timedelta(days=days_offset)
    return {
        "shortcode": shortcode,
        "username": username,
        "caption": caption,
        "posted_at": posted_at.isoformat(),
        "image_url": f"https://example.invalid/{shortcode}.jpg",
        "local_image_path": str(IMAGES_DIR / f"{shortcode}.jpg") if has_image else None,
        "permalink": f"https://www.instagram.com/p/{shortcode}/",
        "is_video": is_video,
        "typename": typename,
    }


FIXTURES: dict[str, list[dict]] = {
    "murphyspubcincy": [
        # Caption-only deal with a relative date -> resolves to the post's date.
        _post("FIXMURPH01", "murphyspubcincy", "$2 domestics tonight! \U0001f37b", 1),
        # Recurring weekly deal.
        _post("FIXMURPH02", "murphyspubcincy", "Every Thursday: half off wings", -2),
        # A deal plus a non-deal event; only the deal should be extracted.
        _post("FIXMURPH03", "murphyspubcincy", "Trivia at 8, $3 wells", -4),
        # Deliberate no-deal post. Extracting anything here is a bug.
        _post("FIXMURPH04", "murphyspubcincy", "\U0001f37b see you tonight", -5, is_video=True,
              typename="GraphVideo"),
    ],
    "ladder_19": [
        # Flyer image: prices live in the JPEG, caption says nothing useful.
        _post("FIXLADDER01", "ladder_19", "TONIGHT \U0001f525", 0, has_image=True),
        # Vague reference -> low confidence, text preserved, nulls elsewhere.
        _post("FIXLADDER02", "ladder_19", "same deal as always this week", -1),
        # Carousel with no deal content.
        _post("FIXLADDER03", "ladder_19", "Great crowd for the game last night \U0001f3c8", -3,
              typename="GraphSidecar"),
    ],
}

WEEKLY_GRID = [
    ("MONDAY", "$3 Craft Drafts"),
    ("TUESDAY", "$2 Tacos / $5 Margs"),
    ("WEDNESDAY", "Half Off Bottles of Wine"),
    ("THURSDAY", "$1 Wings 5-9pm"),
    ("FRIDAY", "$4 Well Drinks"),
    ("SATURDAY", "$12 Domestic Buckets"),
    ("SUNDAY", "$5 Bloody Marys til 2pm"),
]


def make_flyer(path: Path) -> None:
    """Render a weekly-specials flyer whose prices exist only in the image."""
    width, height = 900, 1200
    image = Image.new("RGB", (width, height), (18, 22, 34))
    draw = ImageDraw.Draw(image)

    draw.rectangle([(0, 0), (width, 150)], fill=(196, 48, 43))
    draw.text((60, 55), "LADDER 19  -  WEEKLY SPECIALS", fill=(255, 255, 255))

    y = 230
    for day, deal in WEEKLY_GRID:
        draw.rectangle([(50, y - 20), (width - 50, y + 80)], outline=(90, 98, 120), width=2)
        draw.text((80, y + 5), day, fill=(240, 196, 82))
        draw.text((330, y + 5), deal, fill=(245, 245, 245))
        y += 120

    draw.text((60, height - 90), "1211 Vine St - Over-the-Rhine", fill=(160, 168, 190))
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "JPEG", quality=88)


def main() -> None:
    """Write all fixture JSON files and the flyer image."""
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    make_flyer(IMAGES_DIR / "FIXLADDER01.jpg")

    for username, posts in FIXTURES.items():
        ordered = sorted(posts, key=lambda item: item["posted_at"], reverse=True)
        payload = {
            "username": username,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "synthetic": True,
            "note": "Hand-authored fixture; not captured from Instagram.",
            "posts": ordered,
        }
        target = FIXTURES_DIR / f"{username}.json"
        with target.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
        print(f"wrote {target.name} ({len(ordered)} posts)")


if __name__ == "__main__":
    main()
