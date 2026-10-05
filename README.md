# bardeals

Tracks drink and food deals posted by Cincinnati-area bars on Instagram.

Once a day it pulls recent posts from a list of bar accounts **and reads the
bars' own websites**, sends the text and images to Claude for structured
extraction, stores the results in SQLite, and shows a clean per-bar list of
what's running today or this week — in the terminal or a local web UI.

The image matters more than the caption — bars bake prices into a JPEG flyer and
caption it "TONIGHT 🔥" — so extraction is multimodal by design.

---

## ⚠️ Read this first: anonymous scraping is currently blocked

As of the last live test (2026-09-09), Instagram returns **HTTP 429 on the very
first anonymous request** to `api/v1/users/web_profile_info/` from a residential
IP — not after a burst, on request #1:

```
REQ #0001 [profile] murphyspubcincy (1/150 this hour)
ConnectionException (attempt 1/3): 429 Too Many Requests — backing off 34s
```

Plain profile HTML still returns 200, but the page is a React shell containing
no timeline data (no `_sharedData`, no `edge_owner_to_timeline_media`), so there
is no HTML fallback. Instagram has gated the anonymous JSON endpoint behind a
session.

This is handled gracefully — the scraper backs off, skips the account, and
completes the run — but **it means live fetching may return nothing on your
network**. Options:

1. **Run `--fixtures` mode.** Everything downstream of fetching works offline.
2. **Try a different IP.** The 429 may be IP-reputation-based.
3. **Use the Graph API `business_discovery` endpoint** (see *Legal* below).

The fetch layer is one module behind the `RawPost` dataclass, so swapping it out
does not touch storage, parsing, or reporting.

---

## Setup

Requires Python 3.11+.

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows
# source .venv/bin/activate     # macOS / Linux

pip install -e ".[dev]"

cp .env.example .env            # then add your ANTHROPIC_API_KEY
bardeals init-db
```

`.env` is gitignored; `.env.example` is committed.

> **Windows note:** `zoneinfo` has no system IANA database on Windows, so
> `tzdata` is a hard dependency. Without it every date resolution raises
> `ZoneInfoNotFoundError`.

Website scraping needs `beautifulsoup4` (HTML) and `pypdf` (menu PDFs); the web
UI needs `flask`. All three come with `pip install -e ".[dev]"`.

### Verify it works, offline

```bash
bardeals scrape --fixtures --no-parse   # store fixture posts, no LLM spend
bardeals today --date 2026-09-17        # fixtures are anchored to that week
bardeals week  --start 2026-09-14
pytest -q
```

---

## Usage

```bash
bardeals scrape                  # fetch + parse + store, all venues
bardeals scrape --venue ladder_19
bardeals scrape --fixtures       # offline, from tests/fixtures/
bardeals scrape --no-parse       # fetch only, skip LLM spend

bardeals today                   # deals active today, grouped by bar
bardeals week                    # next 7 days, grouped by day
bardeals venue ladder_19         # everything known for one bar
bardeals venues                  # list configured bars
bardeals explain SHORTCODE       # debug one post's parse
bardeals explain SHORTCODE --reparse   # call the model again
bardeals duplicates              # deals that look like repeats
bardeals report                  # write data/reports/YYYY-MM-DD.md
bardeals serve                   # local web UI (see below)
bardeals init-db
```

`today` and `week` are the main output. Every deal shows its source permalink so
you can click through and verify. A deal is active today if `specific_date` is
today, **or** it recurs weekly on today's weekday, **or** it recurs daily.

Low-confidence deals are shown in a dimmed, separate section rather than hidden.
Deals that carry no date at all (`"same deal as always this week"`) appear under
**Undated** if the post is within the last 7 days.

`--date` / `--start` let you report on any day, which is how you inspect the
fixture week.

---

## Web UI

```bash
bardeals serve                       # http://127.0.0.1:8080
bardeals serve --port 8000 --debug
```

> **Ports browsers refuse.** Chrome, Firefox and Safari hard-block a list of
> ports (SMTP, NFS, SIP and friends) with a bare `ERR_UNSAFE_PORT` and no hint
> that the port is the problem — `5060` and `5061` are the easiest to pick by
> accident. `bardeals serve` refuses those up front and suggests a safe one, so
> you never end up debugging a server that is running perfectly.

A small read-only Flask app over the same SQLite database. It never scrapes and
never calls the LLM — `run_daily.py` does that on cron, and this renders whatever
is on disk. Open it on your phone; the layout is built for one thumb at 8pm.

| Route | What it does |
| --- | --- |
| `/` | Neighborhood map, places, and tonight's specials |
| `/?kind=food` | Restaurants and bars serving food |
| `/?kind=drinks` | Bars and bar-restaurants |
| `/?panel=saved` | Your saved places in this browser |
| `/?sort=price` | Same deals, cheapest first |
| `/?sort=bar` | Same deals, grouped by bar |
| `/week` | All seven days at a glance |
| `/?day=YYYY-MM-DD` | Any day in the next week |
| `/?max=3&cat=beer&q=wings` | Filters (compose freely) |
| `/venue/<slug>` | One bar's full weekly grid + map link |
| `/api/deals.json` | Same data as JSON, filters honoured |
| `/healthz` | Liveness + row counts |

What it shows, and why:

- **Grouped by time, not just price.** At 8pm the real question is *what can I
  walk into right now*, so today's view splits into **On now** / **Starting
  later** / **Anytime today** / **Already finished**, with live countdowns
  ("ends in 33m", "starts in 1h 30m"). A happy hour that ended at 8 is shown
  dimmed under *Already finished* rather than listed as if it were still on.
- **Cheapest first inside every group.** Unpriced deals ("half off wings") sort
  last rather than being dropped.
- **Filters that survive each other:** price cap, category (beer / cocktails /
  shots / wine / food), and free-text search compose, and each link keeps the
  others. Categories come from a keyword heuristic, not an LLM call — it costs
  nothing and re-parsing every stored deal to add a category would not be worth
  the spend.
- **A price cap never hides an unpriced deal.** "Half off wings" might be the
  cheapest thing there; we cannot prove it is over budget.
- **Filtered counts stay honest.** The header reads "3 of 11 deals" while a
  filter is active, and the "nothing posted today" list is suppressed — it
  would be a lie when a filter is doing the hiding.
- **Addresses and a map link** for the 23 venues whose address was actually
  verified. The rest are left blank rather than guessed.
- **A day strip** (Today / Tomorrow / the next five days) plus a **Week** view,
  so weekly recurring deals are browsable without a calendar.
- **Freshness is a first-class element.** A run older than two days gets a
  banner saying so. With scraping frequently blocked, stale data is the normal
  case and the page says it plainly instead of showing old prices as if they
  were today's.
- **Nothing is hidden.** Low-confidence deals get a dimmed "Unconfirmed"
  section, undated ones their own section, and bars with nothing tonight are
  *named* so you can see they were checked rather than skipped.
- **Every deal has a `verify ↗` link** to the original Instagram post.
- **Progressive enhancement.** The deal list and filters work without
  JavaScript. The interactive map adds 3D buildings, selectable price markers,
  and linked place details. Warm paper, evergreen, and terracotta colors carry
  across the map, weekly schedule, and venue pages.

### Interactive neighborhood map

The home page is a map explorer, with 23 mapped bars and restaurants around UC,
Clifton Heights, and the Gaslight District. Drag to pan, scroll or pinch to zoom, use **3D view** to switch between
tilted buildings and a flat map, and select a price marker or **Locate** on a
deal to see the place's specials and directions. **All places** fits the full view.
The map uses a light architectural style with softly lit buildings, green parks,
and sparse street labels. The camera starts at a 50-degree tilt. Nearby pins
group into a bar count; select the group to choose a bar by name or zoom into
the block. **Short Vine**, **Calhoun & McMillan**, and **Ludlow & Gaslight** jump to
the local bar and restaurant corridors.
The opening view starts close to Short Vine so buildings are visible immediately;
opening a filtered or saved search frames its results instead. A ring on the
ground highlights the selected location.
**Move / Rotate** changes mouse dragging; the **Tilt**
slider, zoom buttons, left/right rotation, and **North** reset offer explicit
camera controls. On phones, use two fingers on the map so normal page scrolling
stays available. Details sit beside the map on desktop and below it on phones.
Previous/next buttons cycle through places without covering the map; **Back to exploring**
returns to the list and the camera view before selection.

Day, price, category, and search changes update both the map and the list;
updating filters preserves the camera position.
**List** offers a full-width view. Browser Back restores the previous filters.

MapLibre GL JS 5.12.0 is bundled locally in `static/vendor/`, including its
license. [OpenFreeMap](https://openfreemap.org/) serves the vector map using
OpenStreetMap data. **No map API key or account is needed**, but loading map
tiles requires internet and WebGL. An unavailable map leaves the stored deal
list usable. Extracting fresh specials still requires the Anthropic API key.

Coordinates and evidence links live in `config.yaml`. Scotty's remains unpinned
because its location records conflict. Building footprints/heights come from
map data; heights have a three-metre minimum and missing heights use an
illustrative eight metres. Flat footprints and 3D buildings are separate display
modes, with street labels drawn above the buildings.
Sample specials stay labeled and do not represent verified live offers.
`/api/map.json` exposes the same filtered venue metadata used by the map.
Camera, marker-grouping, and saved-plan checks run with
`node --test tests/map_camera.test.cjs tests/map_theme.test.cjs tests/night_plan.test.cjs`.

The September 10 expansion adds Adriatico's, Drunken Bento, Elephant Walk,
Krishna Carryout, Thai Express, Skyline Chili Clifton, Ambar India, Adeep India,
Arlin's, Fries Cafe, Gaslight Bar & Grill, Alive & Well, and Mio's Clifton.
New places appear in the **Places** directory and on the map even without deals;
name searches can find them. Price/category filters still require matching deals.
Restaurant pins have a food icon, and details link to official websites/menus.
No new specials were fabricated or extracted as part of this expansion.
`kind` accepts `bar`, `restaurant`, or `bar_restaurant`; `instagram: false`
disables account links and scraping when the slug is not a verified handle.

The **Everything / Food / Drinks** filters work alongside search and deal filters.
**Places / Specials** switches between the directory and daily offers. Tap a heart
to save a place, or **+** to add it to **My night**. Plans hold up to five stops,
support reordering and removal, and open a walking route in Google Maps when all
stops have confirmed locations. Favorites and plans persist in local browser
storage; no account or AI call is involved. They do not sync between browsers.

The interface has Tonight / This week navigation, a desktop filter sidebar,
mobile controls with larger touch targets, and direct map links where a verified
address is available. Filters carry across day and week views.
Sample deals are labeled both on the page and in schedule rows, and uncertain
deals remain readable. Keyboard users can skip directly to the main content;
selected days and filters are announced to assistive technology.

Price caps are inclusive ("Max price"). Offers without a known price remain
visible with an explanation. A zero-dollar cap works; invalid, negative, and
non-finite caps are ignored. Future dates never claim a deal is on now.

Malformed or truncated extraction responses are logged as failures and remain
retryable, so a broken model response cannot silently mark a post as processed.

### Seeing it with data

With scraping blocked, a fresh database is empty and the UI will (honestly) say
so. To see the interface working:

```bash
python seed_demo.py          # realistic week of Clifton specials, anchored to today
bardeals serve
python seed_demo.py --clear  # remove it again
```

Demo rows are unmistakable: shortcodes prefixed `DEMO` and permalinks pointing at
the reserved, non-routable `example.invalid` domain. They can never be confused
with real scraped data.

> The dev server is Flask's, bound to `127.0.0.1`. That is correct for a
> personal tool on your own machine and is not hardened for exposure to a
> network — don't bind it to `0.0.0.0` on shared wifi.

---

## Sources: Instagram *and* bar websites

The tool reads two kinds of source, and they complement each other:

| Source | Carries | Module |
| --- | --- | --- |
| **Instagram** | What a bar is doing *tonight* — flyers, one-off specials | `scrape.py` |
| **Bar websites** | What a bar does *every week* — happy hour windows, lunch specials, "kids eat free Tuesday" | `websites.py` |

```bash
bardeals scrape                    # both
bardeals scrape --source instagram
bardeals scrape --source web
python src/bardeals/websites.py --venue elvaqueroclifton     # standalone
python src/bardeals/websites.py --url https://example.com/   # inspect one page
```

### How a website is read

1. Fetch the homepage.
2. Score its links and follow the most promising few — `/happy-hour` beats
   `/specials` beats `/menu` — up to `max_pages_per_site`.
3. Strip each page to visible text (nav, header, footer, scripts removed, so a
   nav link reading "Specials" doesn't look like a deal on every page).
4. **Hash that text — the hash is the shortcode.** This reuses the existing
   cache exactly: an unchanged page produces the same shortcode, is already in
   the database, and costs no LLM call. Edit the specials and the hash changes.

**PDFs are followed too.** A lot of bars publish their menu and happy-hour sheet
as a PDF; Mac's Pizza Pub is one, and skipping PDFs would miss the only page
worth reading. Scanned image-only PDFs yield no text and are reported as such.

Website deals are parsed with a **different prompt** (`WEBSITE_PROMPT`) from
Instagram posts. The dominant failure mode there is reading a menu as a list of
deals, so the prompt's first rule is *a menu is not a list of deals* — a
"Cheeseburger 12.99" line is a regular price, not an offer.

### Being a good web citizen

| Control | Behaviour |
| --- | --- |
| `robots.txt` | Obeyed. Disallowed URLs are skipped and logged. |
| User-Agent | Honest (`bardeals/0.1`) first; retried once as a browser **only on a 403** |
| Delay | 1.5–3s between pages on a site, 2–5s between sites |
| Refusal breaker | After 2 consecutive 403s from a host, the rest of that site is skipped for the run |
| Page budget | 5 pages per site per run |

The two-User-Agent dance is not paranoia, it's measured. Across the four real
sites: `vaquerorestaurant.com` refuses the bot UA, and `macspizzapub.com`
refuses the *browser* UA. Neither string alone works.

### What this actually yields — measured, not assumed

This was checked against the real sites before building it, and the result is
mixed:

| Venue | Site | Result |
| --- | --- | --- |
| El Vaquero | `vaquerorestaurant.com` | ✅ **Real specials page** — "$10.99 lunch specials Mon–Fri 11am–2pm", daily happy hour 4–8pm, Kids Eat Free Tuesday |
| Mac's Pizza Pub | `macspizzapub.com` | ✅ **Menu PDF**, 9.5k chars extracted |
| Murphy's Pub | `murphys-pub.com` | ⚠️ Brochureware — no specials page at all |
| Ladder 19 | `ladder19.com` | ❌ JavaScript-rendered; this fetcher runs no JS |
| The other 7 | — | No site of their own |

**Be realistic about the yield.** Most dive-bar websites are brochureware. The
specials that Cincinnati Magazine quotes for Murphy's Pub are *not* on Murphy's
own site. Website scraping adds genuine standing-specials coverage for the
venues that maintain a real site, and costs almost nothing for the ones that
don't — but it is not a replacement for Instagram.

**Facebook is where a lot of these bars actually post**, and it is not
supported. Uncle Woody's "website" *is* a Facebook page. Facebook is
auth-walled and aggressively anti-scraping; scraping it would be both
impractical and a clear ToS violation. If you need those venues, the sanctioned
route is the Meta Graph API with a Page access token, which requires the page
owner's consent.

### Adding a site

```yaml
venues:
  - slug: scottysonvine
    name: Scotty's on Vine
    website: https://example.com/
    # `pages` pins URLs link discovery cannot reach
    pages:
      - https://example.com/happy-hour
```

Only 4 of the 11 venues have a `website` filled in, because those are the only
ones verified to exist. The rest are left blank rather than guessed at — a
wrong URL would waste requests and could hit someone else's site.

---

## Configuration

Everything tunable lives in `config.yaml` — venues, pacing, model name, paths.
No venue or tuning value is hardcoded in Python.

```yaml
llm:
  model: claude-sonnet-4-5
scrape:
  posts_per_account: 10
  sleep_between_accounts: [8, 15]
  sleep_between_posts: [2, 5]
  max_requests_per_hour: 150
  max_retries_per_account: 3
venues:
  - slug: murphyspubcincy
    name: Murphy's Pub
  # ... 11 bars
```

To add a bar, add a `slug`/`name` pair. Nothing else changes.

---

## Instagram rate limiting

Anonymous scraping is fragile, so pacing is enforced in `scrape.py` rather than
left to the caller:

| Control | Default | Why |
| --- | --- | --- |
| Sleep between accounts | random 8–15s | Avoids a machine-gun access pattern |
| Sleep between posts | random 2–5s | Same, at post granularity |
| Rolling-hour ceiling | 150 requests | Hard cap; blocks until a slot frees |
| Backoff on 429 | 30s × 2ⁿ + jitter | 30s → 60s → 120s |
| Failure budget | 3 attempts/account | Then skip the account and continue |

**One dead account never kills a run.** A private, renamed, or rate-limited
account is logged, skipped, and the run moves on.

Every request is logged with a timestamp to `data/bardeals.log` (rotating, 2MB ×
5), so you can audit what the pacing actually was:

```
2026-09-09T17:22:11-0400 | INFO | bardeals | REQ #0001 [profile] murphyspubcincy (1/150 this hour)
```

### Caching is the most important cost control

A shortcode already in `posts` is skipped **entirely** — no refetch, no image
download, no LLM call. The daily run therefore only pays for genuinely new
posts. A second run minutes later costs nothing:

```
CACHED murphyspubcincy/FIXMURPH01 - skipping
0 new post(s), 0 deal(s), 0 failure(s) in 0.0s
```

---

## Cost per run

Model is `claude-sonnet-4-5` ($3 / $15 per MTok input / output — verify current
rates at <https://www.anthropic.com/pricing>).

Per post, one multimodal call:

| Component | ~Tokens |
| --- | --- |
| System prompt | 674 |
| Tool schema | 593 |
| Caption + metadata | 150 |
| Image (1080×1080, ≈ w·h/750) | 1,555 |
| **Input total** | **≈ 2,970** |
| Output (structured JSON) | ≈ 250 |

| Scenario | Cost |
| --- | --- |
| One post with an image | **$0.013** |
| One text-only post | $0.008 |
| Cold first run (11 bars × 10 posts) | **≈ $1.39** |
| Typical daily run (~15 new posts) | **≈ $0.19** |
| A month of daily runs | **≈ $5.70** |

Estimates use ~4 chars/token; actual usage varies with caption and image size.
Use `--no-parse` to fetch without spending anything.

---

## Daily run

`run_daily.py` scrapes, parses, and writes `data/reports/YYYY-MM-DD.md`. It runs
once and exits — scheduling belongs to cron, not to a daemon.

```bash
python run_daily.py
python run_daily.py --fixtures --no-parse   # offline dry run
```

### crontab

Runs at 10:05am daily. Instagram traffic is spread over ~5 minutes by the
built-in sleeps, so the exact minute doesn't matter much — just avoid `:00`.

```cron
5 10 * * * cd /path/to/bardeals && .venv/bin/python run_daily.py >> data/cron.log 2>&1
```

On Windows, use Task Scheduler with:

```
Program:   C:\path\to\bardeals\.venv\Scripts\python.exe
Arguments: run_daily.py
Start in:  C:\path\to\bardeals
```

Exit code is `0` when the run completes (individual account or post failures are
tolerated and logged) and `1` only when it could not run at all.

---

## Architecture

```
src/bardeals/
  config.py   # config.yaml + .env, rotating log setup
  db.py       # schema, migrations, insert/query helpers, dedupe
  scrape.py   # instaloader wrapper -> RawPost (standalone-runnable)
  websites.py # bar websites + menu PDFs -> RawPost (standalone-runnable)
  parse.py    # multimodal LLM extraction -> Deal
  dates.py    # relative date resolution in America/New_York
  report.py   # shared formatting + rich tables + markdown rendering
  web.py      # read-only Flask UI
  templates/  # Jinja templates (_deal.html holds the shared card macro)
  static/     # one CSS file
  cli.py      # click entrypoints
tests/
  fixtures/   # offline post fixtures + a generated flyer image
data/
  bardeals.db, bardeals.log, images/ (gitignored), reports/
```

`scrape.py` runs on its own for debugging the fetch layer in isolation:

```bash
python src/bardeals/scrape.py --venue murphyspubcincy --limit 3 -v
python src/bardeals/scrape.py --fixtures
```

### Deduplication

**Hard** — a unique index prevents the same deal being inserted twice from the
same post, making re-parsing idempotent. It uses `COALESCE` on the nullable
columns because SQLite treats `NULL`s in a `UNIQUE` index as distinct, so a
plain `UNIQUE(...)` would happily insert the same undated deal twice.

**Soft** — `find_soft_duplicates()` flags the same deal across *different*
posts (same venue, same normalized text, overlapping dates), and `dedupe_rows()`
collapses them at render time, keeping the newest post's row. A bar that posts
its Thursday special three times shows up once. Nothing is deleted; run
`bardeals duplicates` to see what was collapsed.

### Date resolution

`dates.py` resolves relative language against the post's own timestamp in
`America/New_York`. The timezone conversion is the point: Instagram timestamps
are UTC, and **a post published at 01:30 UTC is the previous evening in
Cincinnati**. Resolving "tonight" against the UTC date would silently place
every late-night post one day late.

| Phrase | Resolves to |
| --- | --- |
| "tonight", "today" | the post's local date |
| "tomorrow" | post date + 1 |
| "this Thursday", "Thursday" | next occurrence ≥ post date |
| "all weekend" | Fri/Sat/Sun, dropping days already past |
| "every Monday", "Mondays" | weekly recurrence, no specific date |

### Fixtures

`tests/fixtures/*.json` back `--fixtures` mode. **The committed fixtures are
hand-authored, not captured from Instagram** — the live 429 above meant there
was never a successful run to capture from. They are marked `"synthetic": true`
and cover the tricky cases: a dated one-off, a weekly recurrence, a deal beside
a non-deal event, a vague low-confidence post, a flyer whose prices exist only
in the image, and two posts with no deal at all.

`scrape.py` overwrites them with real data on the first successful live run.
Regenerate the synthetic set with `python tests/fixtures/make_fixtures.py`.

---

## Tests

```bash
pytest -q          # 298 tests
```

Covers `dates.py` (timezone boundaries, DST, weekday arithmetic, negative
matches), `db.py` dedupe (hard and soft), `parse.py` against fixtures with a
stubbed client, terminal/Markdown rendering, the web UI through Flask's test
client, and the website layer (text extraction, link scoring, content-hash
caching, robots.txt, refusal breaker). Live scraping is deliberately not tested.

The zero-deal path gets the most attention: most real posts contain no deal, so
hallucinating one is the failure mode that would quietly ruin the output.

---

## Legal

**Anonymous scraping is against Instagram's Terms of Service.** This is a
personal-use tool for reading publicly posted specials from a handful of
neighbourhood bars. It logs in to nothing, collects nothing about people, and
paces itself deliberately.

If this ever needs to be public or commercial, the sanctioned path is the
Instagram Graph API's
[`business_discovery`](https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/business_discovery)
endpoint, which returns recent media for business and creator accounts given a
Meta app and a linked Instagram Business account. That is the supported,
ToS-compliant route — and, given the 429 above, possibly the only reliable one.

Deal data belongs to the bars that posted it. Every rendered deal links back to
its source post.

---

## Publishing

The live site runs on [Render](https://render.com) from `render.yaml`. It is
the same read-only web UI, served by gunicorn over `deploy/bardeals.db` - a
cleaned snapshot of the local database with sample/demo rows and the raw LLM
parse log removed. Scraping and parsing stay on your own machine.

```bash
python run_daily.py              # scrape + parse locally, as before
python publish.py                # snapshot, commit, push -> Render redeploys
python publish.py --dry-run      # write the snapshot only; see what would go live
```

Render redeploys on every push to `main`; a deploy takes a minute or two. The
free plan sleeps after 15 idle minutes, so the first visit after a quiet spell
takes ~30-50 s.
