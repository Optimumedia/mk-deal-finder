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

1. `pip install -r requirements.txt`
2. Register the two daily tasks (06:15 and 18:15). Re-running the script replaces them:
   ```powershell
   powershell -ExecutionPolicy Bypass -File setup_schedule.ps1
   ```
   The tasks still run on battery, and a run missed while the PC was off starts as soon
   as it's back on. Results are pushed to GitHub, and the log is `data\local-run.log`.
3. Start a run right away: `Start-ScheduledTask "MK Deal Finder - morning"`.
   The first run reads about 2,300 pages slowly and politely, so it takes about 2 hours.
   After that, runs take about 45 minutes (morning) and 10 minutes (evening).
4. **Telegram alerts (optional, free):**
   - Message [@BotFather](https://t.me/BotFather) → `/newbot` and copy the token.
   - Send your bot any message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates`
     and copy `chat.id`.
   - Store them as Windows user environment variables (the scheduled tasks pick them up):
     ```powershell
     setx TELEGRAM_BOT_TOKEN "<token>"
     setx TELEGRAM_CHAT_ID "<chat id>"
     setx DASHBOARD_URL "https://optimumedia.github.io/mk-deal-finder/"
     ```

To stop it: `Unregister-ScheduledTask -TaskName "MK Deal Finder*" -Confirm:$false`

## Tuning

Every threshold is in [`config.toml`](config.toml): rent range, nightly rates, occupancy,
renovation €/m², minimum discount, mountain keywords, "urgent" keywords and so on.
Commit a change and the next run uses it.

**Calibrate the Airbnb numbers.** `adr_by_rooms` (nightly rate) and `occupancy_center`
are starting estimates. Check 10–15 comparable Airbnb/Booking listings near Macedonia Square
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
