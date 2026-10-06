# MK Deal Finder

Scans North Macedonian real estate listings twice a day and surfaces deals for four
"researchers". It costs **€0**: a free Windows scheduled task on your PC does the scraping,
and GitHub hosts the code, the data and the dashboard.

**Dashboard:** https://optimumedia.github.io/mk-deal-finder/

| # | Researcher | Looks for | Where |
|---|------------|-----------|-------|
| 1 | **Airbnb / Booking arbitrage** | Flats for long-term rent that are cheap for their location, with estimated short-term revenue, costs, monthly profit and payback | Skopje |
| 2 | **Land for a house / villa** | Plots priced below the local median €/m², preferring building land (градежно), with electricity, water and road confirmed in the ad | Skopje, Ohrid, and mountain areas anywhere |
| 3 | **Fix & flip** | Flats and houses priced well under the local median €/m², with renovation, taxes, profit and ROI worked out | Whole country |
| 4 | **Motivated sellers** *(added)* | Sellers who cut their price, use "urgent" wording or have been listed for weeks. Good targets for a low offer. | Whole country, every property type |
| 5 | **Bailiff auctions** *(added)* | Official enforcement sales (KIRSM register) compared with what similar property asks on the open market; sales of a mere ownership share are hidden | Whole country |

### Ratings

Every deal gets a rating, and every list is sorted by it:

| | Rating | Score | Notes |
|---|---|---|---|
| 💎 | Once-in-a-lifetime | 90+ | only with solid evidence |
| 🔥 | Exceptional deal | 80+ | only with solid evidence |
| ⭐ | Great deal | 70+ | |
| 👍 | Good deal | 55+ | |
| 👀 | Worth a look | under 55 | |

"Solid evidence" means the detail page has been read, nothing is left to ask the seller, there are no ⚠ warnings, and the comparison is local, not national. Without it a deal is capped at ⭐, so a parsing glitch can never be called once-in-a-lifetime.

### Statuses

- **Ready**: nothing left to check.
- **❓ Needs info**: promising, but key facts aren't confirmed in the ad. The deal lists the exact questions to ask the seller, for example "Is there a water connection?" or "Is it building land?".
- **⏳ Checking details**: the platform hasn't read the detail page yet. That usually happens on the next run.

## How it works

```
Windows Task Scheduler on your PC (06:15 and 18:15) → run_local.ps1
  └─ python -m scraper.run
       1. read Reklama5 search pages      (morning: every page; evening: new ads only)
       2. read detail pages of promising listings  (description, GPS, land type)
       3. build market medians (€/m² by type → city → district)
       4. run the 4 researchers → score 0–100 each
       5. write docs/data.json   → dashboard on GitHub Pages
       6. send new top deals to Telegram (optional)
       7. commit + push data/deals.db and docs/data.json → GitHub Pages updates
```

**Utilities rule.** Each deal shows ⚡ power, 💧 water and 🛣 road badges:
- ✓ the ad says it has it  · ✗ the ad says it's missing (never shown)  · ~ assumed (built flats and houses in towns)  · ? not mentioned
- **Land is only "qualified" when all three are confirmed in the ad.** Untick *Utilities confirmed only* to see near-misses worth a phone call.

**"Under market"** means asking €/m² compared with the median of similar listings in the same district, falling back to city and then country when there are too few comparables. Each deal says which level it was compared against. Asking prices run above final sale prices, so treat discounts as a lead to check, not a fact.

## Why it runs on your PC, not on GitHub

Reklama5's Cloudflare protection answers requests from GitHub's data-centre servers with
a 403 bot challenge (checked 2026-10-06). From a home connection the site works normally.
The scraper never tries to get around a block. If one happens, the run stops and the
dashboard's Run log shows `blocked`.

The cloud workflow (`.github/workflows/scrape.yml`) can still be started by hand. Its
schedule is commented out; if the block is ever lifted, put the schedule back in.

## Setup on a PC (done once)

1. Install Python 3.11+ (the setup script creates its own `.venv` and installs the dependencies there).
2. Register the two daily tasks (06:15 and 18:15). Re-running the script replaces them:
   ```powershell
   powershell -ExecutionPolicy Bypass -File setup_schedule.ps1
   ```
   The tasks still run on battery, and a run missed while the PC was off starts as soon
   as it's back on. Results are pushed to GitHub, and the log is `data\local-run.log`.
3. Start a run right away: `Start-ScheduledTask "MK Deal Finder - morning"`.
   The first run reads about 2,300 pages slowly and politely, so it takes about 2 hours.
   After that, runs take about 45 minutes (morning) and 10 minutes (evening).
4. **Telegram alerts (optional, free):** run
   ```powershell
   powershell -ExecutionPolicy Bypass -File setup_telegram.ps1
   ```
   It walks you through creating a bot with @BotFather, finds your chat ID on its own,
   saves both to `.telegram` (git-ignored, never pushed) and sends a test message.
   After each run you get the new deals scoring at least 60 (`[notify]` in `config.toml`),
   up to 5 per researcher. Each deal is sent only once.

5. **Telegram bot.** It's registered by `setup_schedule.ps1` and starts at logon with no window.
   - **❓ Needs-info leads** come with the questions to ask and two buttons: **✅ Fits** and **❌ Not a fit**.
     Reply to the message with what you learned, for example `water yes, area 450, price 32000, building yes`.
     The facts are saved and the deal is re-scored with them on every run.
   - **Rejecting a deal.** Tap **❌** or **👎**, then pick a reason from the list: bad location, too small,
     basement, no clean title, agricultural, not a real discount, and about 20 more.
     Deals that match that reason lose points from then on.
     `/learn` (or `python -m scraper.learn`) shows what your rejections taught the platform and suggests settings.
   - **Quick check.** Forward a post from Facebook or Viber to the bot, or paste a Reklama5 link,
     and it replies at once with €/m² against the market and what's still unknown.
     Those platforms can't be scraped (they need a login and their terms forbid it), so this is how leads
     from them get in. Forwarded leads are never published.
   - Every other alert has **👍 Interested / 👎 Not for me** buttons. 👎 hides that deal for good;
     votes on similar deals (same district, city, property type) nudge their future alert
     scores by up to ±20. Votes stay on your PC (`data/feedback.db`), and the public
     dashboard never shows them.
   - Send `/top` for the current best deals, or `/status` to see how the last run went.
   - If a run is blocked, crashes or can't push to GitHub, you get a ⚠️ message.

To stop everything (both runs and the bot): `Unregister-ScheduledTask -TaskName "MK Deal Finder*" -Confirm:$false`

## Tuning

Every threshold is in [`config.toml`](config.toml): rent range, nightly rates, occupancy,
renovation €/m², minimum discount, mountain keywords, "urgent" keywords and so on.
Commit a change and the next run uses it.

**Calibrate the Airbnb numbers.** Nightly rates (`adr_by_rooms`) are automatically blended
with real Skopje flats advertised per night (стан за ноќевање). Each deal shows how many
ads its rate is based on. Those ads are the budget end of the market, and occupancy
(`occupancy_center`) is still an estimate. Check 10–15 comparable Airbnb/Booking listings near Macedonia Square
for real nightly prices and calendar availability, then update them. The profit figures are
only as good as these two numbers.

## Manual commands

```bash
pip install -r requirements.txt
python -m scraper.run                 # normal run
python -m scraper.run --no-scrape     # just re-score existing data after editing config.toml
python -m unittest discover tests     # tests against saved real pages
```

Open the dashboard locally with `python -m http.server -d docs 8000`, then go to http://localhost:8000.

## Data sources

| Source | Status |
|--------|--------|
| Reklama5.mk (mobile site) | ✅ used. robots.txt allows crawling, about 3 s between requests |
| KIRSM (kirm.mk), the bailiffs' register | ✅ used. Public official register, plain HTML, about 7 pages a run |
| novelestate.com, nedviznosti.com.mk | 🔧 being added (large Skopje rental stock; sitemap + structured data) |
| Facebook, Instagram, Viber | ⛔ login required and terms forbid scraping. Forward posts to the bot instead |
| mojdom.mk | ⛔ terms explicitly forbid scraping |
| Pazar3.mk | ⛔ not used. Answers automated requests with a Cloudflare bot challenge |
| Reklama5.mk desktop | ⛔ same bot challenge (the mobile site serves the same ads) |
| Airbnb / Booking | ⛔ not scraped. Their terms forbid it and they block bots, so the nightly rates are config estimates |

To add a source, create a module in `scraper/sources/` with `parse_list` and `parse_detail`
that return the same fields as `reklama5.py`.

## Not advice

The scores are screening tools built on asking prices and estimates. Before you commit any
money, check each deal in person and confirm the property title (имотен лист), zoning and
utility connections with the cadastre (Катастар) and the municipality, and for rental
arbitrage, get the landlord's written consent to sublet.
