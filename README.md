# SRCFL End Zone

Live fan hub for the SRC Football League (ESPN league ID 298919).
Static site + a scheduled GitHub Action that pulls real data from your
private ESPN league and bakes it into a static file the site reads instantly.

## What's in here

```
public/index.html              the site itself — reads public/data/league-cache.json on page load
public/data/league-cache.json  the real ESPN data, refreshed by the GitHub Action below (git-ignored until first run)
public/data/keepers.json       the manually-maintained keeper submissions file (starts empty)
scripts/fetch_espn_data.py     the script that actually talks to ESPN and writes league-cache.json
.github/workflows/refresh-data.yml   runs that script on a schedule and commits the result
api/sync-espn.py               a Vercel function that does the same ESPN fetch, kept around for on-demand manual debugging only — the site doesn't call this automatically
requirements.txt               Python deps
vercel.json                     intentionally empty — the cron that used to live here got replaced by the GitHub Action above; kept as an empty {} since Vercel's schema validation rejects custom fields/comments in this file
```

## Setup — do this once

### 1. Get your ESPN cookies
Since the league is private, ESPN needs your login session to read it.
- Log into fantasy.espn.com in a desktop browser
- Open dev tools (F12 or right-click → Inspect) → Application/Storage → Cookies → fantasy.espn.com
- Copy the values for `SWID` (looks like `{ABC123-...}`, keep the braces) and `espn_s2` (a long string)

Treat these like a password. Don't paste them into chat, don't commit them to the repo.

### 2. Create a GitHub repo
- Create a new repo (private is fine, doesn't need to be public)
- Push everything in this folder to it

### 3. Add your ESPN cookies as GitHub repo secrets
This is what powers the actual data refresh now — GitHub repo → Settings →
Secrets and variables → Actions → New repository secret. Add three:
- `ESPN_LEAGUE_ID` = `298919`
- `ESPN_SWID` = *(your SWID cookie, including the `{ }`)*
- `ESPN_S2` = *(your espn_s2 cookie)*

### 4. Run the refresh once manually
GitHub repo → Actions tab → "Refresh ESPN data" workflow → "Run workflow"
button. This does the first real pull and commits `public/data/league-cache.json`
to the repo. After this, it also runs automatically twice a week (see the
schedule in `.github/workflows/refresh-data.yml` — easy to change).

### 5. Create a Vercel account and import the repo
- vercel.com → sign up with GitHub → "Import Project" → pick this repo
- Vercel will auto-detect the static site + the Python function
- (Optional) If you want `api/sync-espn.py` available for manual debugging,
  add the same three `ESPN_*` values as Vercel Environment Variables too —
  Vercel and GitHub Actions don't share secrets, so this is a separate step.
  Not required for the site itself to work, since it no longer calls that
  function automatically.

### 6. Deploy
Vercel gives you a live URL immediately (something like `srcfl-endzone.vercel.app`,
or connect a custom domain in Settings → Domains).

Every time the GitHub Action commits a fresh `league-cache.json`, that push
triggers a normal Vercel redeploy automatically, so the live site picks up
the new data within a minute or two of each scheduled refresh.

## What's live now vs. still placeholder

**Live, pulling real data automatically:**
- **Power Rankings** — current standings, including a real week-over-week MOVE
  column (comparing rank to the previous committed run)
- **All-Time Standings** — career totals across every season
- **Trophy Case & Playoff History** — built from ESPN's final "standing" field
  per season (worth an eyeball-check against what you remember actually happening)
- **Lifetime Trajectory chart** — real finishing position, all 12 managers, 2010–2025
- **All-Time Bests** — all four cards: Best Single Season, Best Single Week,
  Best Championship Game, Best Playoff Game. The latter three are built from a
  full matchup-log scan across every season — championship/playoff games are
  identified heuristically (final week of the season + ESPN's playoff matchup
  flag where available), so worth a sanity-check against what actually happened.
- **Rivalries** — real head-to-head records for the top 6 most-played manager
  pairs across all seasons, with last-meeting margin and year
- **Manager Profiles** — real trade/waiver-add counts per manager, pulled from
  ESPN's transaction log, with a tag ("The Chaos Agent," "The Ghost," etc.)
  assigned algorithmically from the actual numbers. Coverage may be partial for
  older seasons if ESPN's API doesn't expose transaction history that far back
  — the site shows a coverage note when this happens.
- **Weekly Awards** — activates automatically once the season has a completed
  week. Covers: highest/lowest score, closest game, biggest blowout, highest
  score in a loss, lowest score in a win, best single starter performance
  ("Cicchetti Lumberjack"), best bench ("Bikini's Best Bench"), and biggest
  week-over-week collapse ("Mickey Miles").
- **Keeper Rankings** — activates automatically once `public/data/keepers.json`
  has real entries. That file starts empty on purpose (see its own `_readme`
  and `_schema` fields for exactly what to fill in per player). Once all 12
  managers have submitted their two keepers each, fill in the `keepers` array
  (24 entries) and push — no code changes needed, the site picks it up on the
  next page load.

**Genuinely not possible, not just "not built yet":**
- **"Pilon Comeback of the Week"** — needs point-in-time score snapshots
  *during* a game week to detect a 2nd-half swing. ESPN's API only exposes
  that live resolution for the week that's currently in progress, not
  retroactively for weeks that already finished — this can't be reconstructed
  after the fact, only captured going forward if the site started polling
  live scores during game windows (a fundamentally different architecture).
- **"Furtek Pro For a Day"** (best waiver-add performance in a specific week)
  — needs correlating that week's transaction log with that week's box score
  per player. Buildable, just not done in this pass.
- **Worst lineup decision** — needs comparing the started lineup against the
  optimal possible lineup that week. Buildable, just not done in this pass.

**Known limitation:**
- Historical data only goes as far back as ESPN's API allows (confirmed 2010
  is this league's actual first season). Some older ESPN leagues don't expose
  data before a certain year even if the league existed earlier.
- The full historical matchup scan (powers Rivalries, Manager Profiles, and
  the historical "best" records) is the slow part of each refresh run —
  a few hundred ESPN API calls. This runs fine in GitHub Actions (no hard
  timeout) but would never work inside Vercel's 10-second function limit,
  which is exactly why this moved to a separate scheduled script instead of
  living in `api/sync-espn.py`.

## Notes

- Nothing here touches Squarespace — this fully replaces that plan, since
  Squarespace can't run scheduled scripts or serve dynamic data.
- Yahoo league integration would be a separate `api/sync-yahoo.py` function
  using Yahoo's OAuth flow (different from ESPN's cookie auth) — not built yet.
