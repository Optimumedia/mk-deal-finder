# MK Deal Finder

Scans North Macedonian real estate listings twice a day and surfaces deals for four
"researchers". It runs on GitHub's free infrastructure, so it costs **€0** and needs
no computer of yours to stay on.

| # | Researcher | Looks for | Where |
|---|------------|-----------|-------|
| 1 | **Airbnb / Booking arbitrage** | Flats for long-term rent that are cheap for their location, with estimated short-term revenue, costs, monthly profit and payback | Skopje |
| 2 | **Land for a house / villa** | Plots priced below the local median €/m², preferring building land (градежно), with electricity, water and road confirmed in the ad | Skopje, Ohrid, and mountain areas anywhere |
| 3 | **Fix & flip** | Flats and houses priced well under the local median €/m², with renovation, taxes, profit and ROI worked out | Whole country |
| 4 | **Motivated sellers** *(added)* | Sellers who cut their price, use "urgent" wording or have been listed for weeks. Good targets for a low offer. | Whole country, every property type |

## How it works

```
GitHub Actions (06:15 and 18:15 Skopje time)
  └─ python -m scraper.run
       1. read Reklama5 search pages      (morning: every page; evening: new ads only)
       2. read detail pages of promising listings  (description, GPS, land type)
       3. build market medians (€/m² by type → city → district)
       4. run the 4 researchers → score 0–100 each
       5. write docs/data.json   → dashboard on GitHub Pages
       6. send new top deals to Telegram (optional)
       7. commit data/deals.db   (history: price cuts, days on market)
```

**Utilities rule.** Each deal shows ⚡ power, 💧 water and 🛣 road badges:
- ✓ the ad says it has it  · ✗ the ad says it's missing (never shown)  · ~ assumed (built flats and houses in towns)  · ? not mentioned
- **Land is only "qualified" when all three are confirmed in the ad.** Untick *Utilities confirmed only* to see near-misses worth a phone call.

**"Under market"** means asking €/m² compared with the median of similar listings in the same district, falling back to city and then country when there are too few comparables. Each deal says which level it was compared against. Asking prices run above final sale prices, so treat discounts as a lead to check, not a fact.

## Setup (once, about 10 minutes)

1. **Create a GitHub repository** and push this folder to it.
   A **public** repo gets unlimited Actions minutes and free GitHub Pages. A private repo
   gets 2,000 free minutes/month, which is enough (about 1,700 used), but GitHub Pages on
   private repos needs a paid plan. If the repo is private, rely on Telegram alerts or open
   `docs/index.html` locally.
2. **Enable Pages:** Settings → Pages → Source: *Deploy from a branch* → `main` / `/docs`.
   Your dashboard will be at `https://<you>.github.io/<repo>/`.
3. **Let the workflow push:** Settings → Actions → General → Workflow permissions →
   *Read and write permissions*.
4. **Start the first run:** Actions → *Scrape deals* → *Run workflow* → mode `full`.
   The first run reads about 2,300 pages slowly and politely, so it takes about 2 hours.
   After that, runs take about 45 minutes (morning) and 10 minutes (evening).
5. **Telegram alerts (optional, free):**
   - Message [@BotFather](https://t.me/BotFather) → `/newbot` and copy the token.
   - Send your bot any message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates`
     and copy `chat.id`.
   - Repo → Settings → Secrets and variables → Actions → add secrets
     `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. You can also add the variable `DASHBOARD_URL`.

## Tuning

Every threshold is in [`config.toml`](config.toml): rent range, nightly rates, occupancy,
renovation €/m², minimum discount, mountain keywords, "urgent" keywords and so on.
Commit a change and the next run uses it.

**Calibrate the Airbnb numbers.** `adr_by_rooms` (nightly rate) and `occupancy_center`
are starting estimates. Check 10–15 comparable Airbnb/Booking listings near Macedonia Square
for real nightly prices and calendar availability, then update them. The profit figures are
only as good as these two numbers.

## Run it on your PC

```bash
pip install -r requirements.txt
python -m scraper.run                 # normal run
python -m scraper.run --no-scrape     # just re-score existing data after editing config.toml
python -m unittest discover tests     # tests against saved real pages
```

Open the dashboard locally with `python -m http.server -d docs 8000`, then go to http://localhost:8000.

**If GitHub's servers get blocked.** Sites sometimes refuse cloud IPs. The run then stops
on its own (it never tries to get around a block), the Run log shows `blocked`, and GitHub
emails you. The free fallback is to run it from your own PC with Windows Task Scheduler
twice a day (`run_local.ps1`), then push the data.

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
