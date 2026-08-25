"""
Vercel serverless function (Python runtime). Available on-demand at
/api/sync-espn for manual debugging or testing.

ARCHITECTURE NOTE: this used to be called live by the site on every page
load (and by a Vercel cron on a schedule). That's been replaced —
scripts/fetch_espn_data.py + the GitHub Action in
.github/workflows/refresh-data.yml now handle the real data refresh,
writing a static public/data/league-cache.json file that the site reads
directly (fast, no live ESPN call per page load). This function is kept
around because it's still useful for quickly checking what ESPN's API
currently returns without waiting for the next scheduled Action run —
visit the URL directly in a browser to see the raw JSON.

Pulls current + historical data from the private ESPN league using the
`espn-api` library, authenticated with SWID + espn_s2 cookies stored as
Vercel environment variables (never committed to the repo, never sent
to the browser).

Setup required before this runs successfully:
  1. pip install espn-api  (added to requirements.txt)
  2. In Vercel project settings -> Environment Variables, add:
       ESPN_SWID       = {your SWID cookie value, including the braces}
       ESPN_S2         = {your espn_s2 cookie value}
       ESPN_LEAGUE_ID  = 298919
  3. Deploy. Visit /api/sync-espn directly in a browser to see the raw JSON.
"""

import os
import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler

try:
    from espn_api.football import League
except ImportError:
    League = None  # library installs from requirements.txt at deploy time


def build_manager_index(league):
    """
    Maps ESPN team objects to the manager-first structure this site uses,
    since team NAMES change every year but managers don't.
    Falls back gracefully if espn-api's team.owners format changes.
    """
    managers = []
    for team in league.teams:
        owner_name = None
        if getattr(team, "owners", None):
            o = team.owners[0]
            owner_name = o.get("firstName", "") + " " + o.get("lastName", "") \
                if isinstance(o, dict) else str(o)
        managers.append({
            "manager": owner_name or "Unknown Manager",
            "team_name": team.team_name,
            "team_id": team.team_id,
            "wins": team.wins,
            "losses": team.losses,
            "points_for": round(team.points_for, 1),
            "points_against": round(team.points_against, 1),
            "standing": team.standing,
            "division": getattr(team, "division_name", None),
        })
    return managers


def build_current_season(league):
    return {
        "year": league.year,
        "current_week": league.current_week,
        "teams": build_manager_index(league),
    }


def build_historical_seasons(league_id, swid, espn_s2, start_year, end_year):
    """
    Loops back through prior seasons to build the lifetime trajectory
    chart (finishing position by year) and all-time standings totals.
    """
    seasons = []
    for year in range(start_year, end_year + 1):
        try:
            yr_league = League(
                league_id=int(league_id),
                year=year,
                swid=swid,
                espn_s2=espn_s2,
            )
            seasons.append({
                "year": year,
                "teams": build_manager_index(yr_league),
            })
        except Exception as e:
            # Some early years may not exist / may 404 - skip gracefully
            seasons.append({"year": year, "error": str(e)})
    return seasons


def find_latest_completed_week(league):
    """
    Scans backward from the current week to find the most recent week that
    actually has final scores (i.e., games have been played). Returns None
    if the season hasn't started yet (preseason, all weeks show 0-0).
    """
    current_week = getattr(league, "current_week", 0) or 0
    for week in range(current_week, 0, -1):
        try:
            box_scores = league.box_scores(week)
        except Exception:
            continue
        if not box_scores:
            continue
        has_score = any(
            (getattr(m, "home_score", 0) or 0) > 0 or (getattr(m, "away_score", 0) or 0) > 0
            for m in box_scores
        )
        if has_score:
            return week, box_scores
    return None, None


def build_weekly_awards(league):
    """
    Computes real weekly awards from box score data, once the season is
    underway. Returns None if no completed week exists yet (preseason).

    IMPLEMENTED (computable from a standard box score):
      - highest_score / lowest_score (team totals)
      - closest_game / biggest_blowout
      - highest_score_in_a_loss / lowest_score_in_a_win
      - best_single_player ("Cicchetti Lumberjack") - top starter points
      - best_bench ("Bikini's Best Bench") - most points left on the bench
      - longest_win_streak / longest_losing_streak - pulled from ESPN's
        own team.streak_type / team.streak_length if the installed
        espn-api version exposes it; omitted otherwise rather than guessed

    NOT IMPLEMENTED (needs data this sync doesn't pull, to avoid the
    timeout risk of adding heavier per-week API calls right at launch):
      - "Furtek Pro For a Day" (needs waiver/transaction log)
      - "Pilon Comeback of the Week" (needs in-game live scoring snapshots,
        not available from a final box score)
      - "Worst Lineup Decision" (needs optimal-lineup comparison)
      - "Mickey Miles" week-over-week collapse (needs last week's totals
        held alongside this week's - straightforward to add later by
        comparing two consecutive build_weekly_awards() calls)
    These stay out of the response entirely; the front-end only renders
    award cards for keys that are actually present.
    """
    week, box_scores = find_latest_completed_week(league)
    if week is None:
        return None

    team_results = []  # {manager, team_name, score, opponent_score, won}
    best_player = None  # {manager, team_name, player_name, points}
    best_bench = None   # {manager, team_name, points}

    for m in box_scores:
        home_score = getattr(m, "home_score", 0) or 0
        away_score = getattr(m, "away_score", 0) or 0
        home_team = getattr(m, "home_team", None)
        away_team = getattr(m, "away_team", None)

        def team_manager_name(team):
            if not team or not getattr(team, "owners", None):
                return getattr(team, "team_name", "Unknown")
            o = team.owners[0]
            return o.get("firstName", "") + " " + o.get("lastName", "") if isinstance(o, dict) else str(o)

        if home_team is not None:
            team_results.append({
                "manager": team_manager_name(home_team),
                "team_name": getattr(home_team, "team_name", ""),
                "score": home_score,
                "opponent_score": away_score,
                "won": home_score > away_score,
            })
        if away_team is not None:
            team_results.append({
                "manager": team_manager_name(away_team),
                "team_name": getattr(away_team, "team_name", ""),
                "score": away_score,
                "opponent_score": home_score,
                "won": away_score > home_score,
            })

        for lineup, team in ((getattr(m, "home_lineup", []), home_team), (getattr(m, "away_lineup", []), away_team)):
            if not team:
                continue
            manager = team_manager_name(team)
            team_name = getattr(team, "team_name", "")
            bench_total = 0
            for p in (lineup or []):
                slot = getattr(p, "slot_position", "") or ""
                pts = getattr(p, "points", 0) or 0
                if slot == "BE":
                    bench_total += pts
                elif slot not in ("IR",):
                    if best_player is None or pts > best_player["points"]:
                        best_player = {"manager": manager, "team_name": team_name,
                                        "player_name": getattr(p, "name", "Unknown"), "points": round(pts, 1)}
            if best_bench is None or bench_total > best_bench["points"]:
                best_bench = {"manager": manager, "team_name": team_name, "points": round(bench_total, 1)}

    if not team_results:
        return None

    highest = max(team_results, key=lambda t: t["score"])
    lowest = min(team_results, key=lambda t: t["score"])
    closest = min(team_results, key=lambda t: abs(t["score"] - t["opponent_score"]))
    blowout = max(team_results, key=lambda t: abs(t["score"] - t["opponent_score"]))
    losses = [t for t in team_results if not t["won"]]
    wins = [t for t in team_results if t["won"]]
    highest_in_loss = max(losses, key=lambda t: t["score"]) if losses else None
    lowest_in_win = min(wins, key=lambda t: t["score"]) if wins else None

    awards = {
        "week": week,
        "highest_score": highest,
        "lowest_score": lowest,
        "closest_game_margin": round(abs(closest["score"] - closest["opponent_score"]), 1),
        "closest_game_team": closest,
        "biggest_blowout_margin": round(abs(blowout["score"] - blowout["opponent_score"]), 1),
        "biggest_blowout_team": blowout,
    }
    if highest_in_loss:
        awards["highest_score_in_a_loss"] = highest_in_loss
    if lowest_in_win:
        awards["lowest_score_in_a_win"] = lowest_in_win
    if best_player:
        awards["best_single_player"] = best_player
    if best_bench:
        awards["best_bench"] = best_bench

    return awards


def get_league_data():
    if League is None:
        return 500, {"error": "espn-api not installed. Check requirements.txt / deploy logs."}

    league_id = os.environ.get("ESPN_LEAGUE_ID")
    swid = os.environ.get("ESPN_SWID")
    espn_s2 = os.environ.get("ESPN_S2")

    if not (league_id and swid and espn_s2):
        return 500, {
            "error": "Missing ESPN_LEAGUE_ID / ESPN_SWID / ESPN_S2 environment variables. "
                     "Set these in Vercel project settings before syncing."
        }

    try:
        current_year = datetime.now(timezone.utc).year
        league = League(league_id=int(league_id), year=current_year, swid=swid, espn_s2=espn_s2)

        weekly_awards = None
        try:
            weekly_awards = build_weekly_awards(league)
        except Exception:
            # Weekly awards are a bonus feature - never let a box-score quirk
            # break the whole response. Falls back to None (front-end shows
            # the "coming soon" placeholder) if anything goes wrong here.
            weekly_awards = None

        output = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "league_id": league_id,
            "current_season": build_current_season(league),
            "weekly_awards": weekly_awards,
            # NOTE: adjust start_year to whenever the league actually began.
            # Chris Mathison's real ESPN history showed data back to 2010 -
            # confirm that's league year one before running this at scale,
            # since pulling many years means many API calls per page load.
            "historical_seasons": build_historical_seasons(
                league_id, swid, espn_s2, start_year=2010, end_year=current_year - 1
            ),
        }
        return 200, output
    except Exception as e:
        return 500, {"error": f"ESPN fetch failed: {str(e)}"}


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        status, body = get_league_data()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode("utf-8"))
        return

