"""
Standalone data-refresh script for SRCFL End Zone.

Fetches current + historical league data from ESPN and writes it to
public/data/league-cache.json as a real, committed static file. The site
reads this file directly (fast, no live ESPN call on page load) instead
of calling the /api/sync-espn function on every visit.

WHY A SEPARATE SCRIPT INSTEAD OF api/sync-espn.py: Vercel serverless
functions on the Hobby plan have a hard 10-second execution limit. This
script walks full matchup history across every season (16+ years x ~15
weeks each) to compute Rivalries, Manager Profiles, and historical
single-week/game records — that's a few hundred ESPN API calls, which
comfortably blows past 10 seconds. GitHub Actions has no such limit
(default timeout is hours), so that's where this heavier lifting lives.
api/sync-espn.py is kept lighter (current season + this week's awards
only) and still works for quick manual/on-demand checks.

USAGE:
  Locally:
    export ESPN_LEAGUE_ID=298919
    export ESPN_SWID="{your-swid}"
    export ESPN_S2="your-espn-s2-value"
    pip install espn-api --break-system-packages
    python3 scripts/fetch_espn_data.py

  Via GitHub Actions: runs automatically on the schedule in
  .github/workflows/refresh-data.yml, using repo secrets of the same
  names. Can also be triggered manually from the Actions tab.
"""

import os
import sys
import json
from datetime import datetime, timezone
from collections import defaultdict

try:
    from espn_api.football import League
except ImportError:
    print("espn-api not installed. Run: pip install espn-api --break-system-packages")
    sys.exit(1)


MAX_WEEKS_TO_SCAN = 18  # safe upper bound; we stop early per-season once weeks come back empty


def team_manager_name(team):
    """
    Builds the manager's display name from ESPN's owner data. Normalizes
    whitespace (strip + collapse internal runs to a single space) since
    ESPN's raw firstName/lastName fields have been observed to carry
    inconsistent spacing across different seasons for the same real
    person — e.g. a trailing space in one year's API response and not
    another. Un-normalized, that produces two entries that render
    identically ("Adam Christie" showing up twice) but are actually
    different string keys everywhere names get grouped by (Lifetime
    Trajectory, All-Time Standings, Rivalries, etc.), splitting one
    manager's real history into two disconnected partial ones.
    """
    if not team or not getattr(team, "owners", None):
        return _normalize_name(getattr(team, "team_name", "Unknown"))
    o = team.owners[0]
    name = o.get("firstName", "") + " " + o.get("lastName", "") if isinstance(o, dict) else str(o)
    return _normalize_name(name)


def _normalize_name(name):
    return " ".join(str(name).split())


def build_manager_index(league):
    """
    NOTE on team.standing vs team.final_standing: espn-api exposes both.
    `standing` is a live/regular-season-style rank that does NOT reliably
    account for the playoff bracket outcome. `final_standing` is ESPN's
    actual post-season computed rank (1 = champion, 2 = runner-up, etc.)
    for a completed season. Using `standing` alone caused Trophy Case /
    Playoff History to show the wrong champion for at least one season
    (2024: showed the wrong manager instead of the real playoff winner).
    We prefer final_standing when ESPN has populated it (i.e. > 0), and
    fall back to standing only for the current, still-in-progress season
    where final_standing isn't set yet (no playoffs have happened).
    """
    managers = []
    for team in league.teams:
        final_standing = getattr(team, "final_standing", 0) or 0
        managers.append({
            "manager": team_manager_name(team),
            "team_name": team.team_name,
            "team_id": team.team_id,
            "wins": team.wins,
            "losses": team.losses,
            "points_for": round(team.points_for, 1),
            "points_against": round(team.points_against, 1),
            "standing": final_standing if final_standing > 0 else team.standing,
            "division": getattr(team, "division_name", None),
        })
    return managers


def build_current_season(league):
    return {
        "year": league.year,
        "current_week": league.current_week,
        "teams": build_manager_index(league),
    }


def collect_season_matchups(league, up_to_week=None):
    """
    Pulls every completed matchup for one League object (one season).
    Returns a list of dicts, one per team per matchup (so each real-world
    game produces two entries — one from each side's perspective — which
    makes head-to-head and "best single game" lookups simpler later).
    Stops scanning once it hits a week with no scored data, or at
    up_to_week if given (used for the current, still-in-progress season).
    """
    results = []
    limit = up_to_week or MAX_WEEKS_TO_SCAN
    consecutive_empty = 0

    for week in range(1, limit + 1):
        try:
            box_scores = league.box_scores(week)
        except Exception:
            consecutive_empty += 1
            if consecutive_empty >= 2:
                break
            continue

        if not box_scores:
            consecutive_empty += 1
            if consecutive_empty >= 2:
                break
            continue

        week_has_score = False
        for m in box_scores:
            home_score = getattr(m, "home_score", 0) or 0
            away_score = getattr(m, "away_score", 0) or 0
            home_team = getattr(m, "home_team", None)
            away_team = getattr(m, "away_team", None)
            if home_score <= 0 and away_score <= 0:
                continue
            week_has_score = True

            matchup_type = getattr(m, "matchup_type", None) or getattr(m, "playoff_tier_type", None)
            is_playoff = bool(matchup_type) and str(matchup_type).upper() not in ("NONE", "")

            if home_team is not None and away_team is not None:
                results.append({
                    "week": week, "manager": team_manager_name(home_team),
                    "opponent": team_manager_name(away_team), "score": home_score,
                    "opponent_score": away_score, "won": home_score > away_score,
                    "is_playoff": is_playoff,
                })
                results.append({
                    "week": week, "manager": team_manager_name(away_team),
                    "opponent": team_manager_name(home_team), "score": away_score,
                    "opponent_score": home_score, "won": away_score > home_score,
                    "is_playoff": is_playoff,
                })

        consecutive_empty = 0 if week_has_score else consecutive_empty + 1
        if consecutive_empty >= 2:
            break

    return results


def build_historical_seasons_and_matchups(league_id, swid, espn_s2, start_year, end_year):
    """
    One pass per year: builds both the season standings (for Trophy Case /
    Playoff History / Lifetime Trajectory / All-Time Standings) AND that
    year's full matchup log (for Rivalries / best-week / championship /
    playoff records / head-to-head logs), reusing the same League object
    instead of connecting twice.

    NOTE: ESPN's API has been observed to return season standings fine for
    old seasons while returning zero usable box-score/matchup data for
    those same years (a different, flakier API call, made once per week).
    That failure is silent by design in collect_season_matchups (so one
    bad week doesn't kill a whole season's standings) — which means a
    year can look "successful" here while quietly contributing 0 games
    to every matchup-derived stat (Rivalries, head-to-head logs, best
    single week/game). We print a per-year matchup count so that's
    visible in the Action's logs instead of failing invisibly.
    """
    seasons = []
    all_matchups = []  # flat list across every year, each entry tagged with "year"

    for year in range(start_year, end_year + 1):
        try:
            yr_league = League(league_id=int(league_id), year=year, swid=swid, espn_s2=espn_s2)
            seasons.append({"year": year, "teams": build_manager_index(yr_league)})
            year_matchups = collect_season_matchups(yr_league)
            for m in year_matchups:
                m["year"] = year
            all_matchups.extend(year_matchups)
            games_this_year = len(year_matchups) // 2  # each real game = 2 entries
            flag = "  <-- 0 matchups pulled, only standings" if games_this_year == 0 else ""
            print(f"  {year}: {games_this_year} games{flag}")
        except Exception as e:
            seasons.append({"year": year, "error": str(e)})
            print(f"  {year}: FAILED ({e})")

    return seasons, all_matchups


def build_rivalries(all_matchups, min_meetings=3, top_n=6):
    """
    Aggregates every manager-pair's head-to-head record across all
    matchups seen. Returns the pairs that have played each other the
    most (a reasonable proxy for "real rivalry" — same-division opponents
    naturally meet more often), capped at top_n so the section doesn't
    sprawl.
    """
    pairs = defaultdict(lambda: {"a_wins": 0, "b_wins": 0, "games": [], "manager_a": None, "manager_b": None})

    for m in all_matchups:
        a, b = sorted([m["manager"], m["opponent"]])
        key = (a, b)
        pairs[key]["manager_a"] = a
        pairs[key]["manager_b"] = b
        if m["manager"] == a:
            pairs[key]["games"].append({
                "year": m["year"], "week": m["week"],
                "a_score": m["score"], "b_score": m["opponent_score"],
            })
            if m["won"]:
                pairs[key]["a_wins"] += 1
            elif m["score"] != m["opponent_score"]:
                pairs[key]["b_wins"] += 1

    rivalries = []
    for (a, b), data in pairs.items():
        total = len(data["games"])
        if total < min_meetings:
            continue
        last_game = max(data["games"], key=lambda g: (g["year"], g["week"]))
        margin = round(abs(last_game["a_score"] - last_game["b_score"]), 1)
        leader = a if last_game["a_score"] > last_game["b_score"] else b
        rivalries.append({
            "manager_a": a, "manager_b": b,
            "a_wins": data["a_wins"], "b_wins": data["b_wins"],
            "total_games": total,
            "last_meeting": {"year": last_game["year"], "winner": leader, "margin": margin},
        })

    rivalries.sort(key=lambda r: -r["total_games"])
    return rivalries[:top_n]


def build_head_to_head_log(all_matchups, manager_a, manager_b):
    """
    Every individual game between two specific managers, across every
    season on file — used for the Grudge Bowl section, which wants the
    full history for one pinned pair rather than just aggregate stats.
    Each real-world game appears twice in all_matchups (once per side),
    so we only take entries seen from manager_a's perspective to avoid
    double-counting. Notes which games were playoff games using the same
    is_playoff flag collect_season_matchups already tags.
    """
    games = []
    for m in all_matchups:
        if m["manager"] != manager_a or m["opponent"] != manager_b:
            continue
        if m["score"] > m["opponent_score"]:
            winner = manager_a
        elif m["opponent_score"] > m["score"]:
            winner = manager_b
        else:
            winner = None  # tie
        games.append({
            "year": m["year"], "week": m["week"],
            "a_score": round(m["score"], 1), "b_score": round(m["opponent_score"], 1),
            "winner": winner,
            "is_playoff": bool(m.get("is_playoff")),
        })

    games.sort(key=lambda g: (g["year"], g["week"]), reverse=True)

    a_wins = sum(1 for g in games if g["winner"] == manager_a)
    b_wins = sum(1 for g in games if g["winner"] == manager_b)

    return {
        "manager_a": manager_a, "manager_b": manager_b,
        "a_wins": a_wins, "b_wins": b_wins,
        "total_games": len(games),
        "games": games,
    }


def build_manager_profiles(league_id, swid, espn_s2, start_year, end_year, current_league):
    """
    Pulls trade + waiver-add counts per manager across seasons, using
    espn-api's recent_activity(). This endpoint is not reliably available
    for every past season depending on the installed espn-api version and
    how far back ESPN itself retains transaction logs — this function
    degrades gracefully (skips a year silently) rather than failing the
    whole run if a given year's activity log isn't available. Coverage
    may end up partial for older seasons; that's expected and OK.
    """
    activity_by_manager = defaultdict(lambda: {"trades": 0, "waiver_adds": 0})
    years_with_data = 0

    def tally_year(yr_league):
        nonlocal years_with_data
        try:
            activities = yr_league.recent_activity(size=1000)
        except Exception:
            return
        if not activities:
            return
        years_with_data += 1
        for act in activities:
            actions = getattr(act, "actions", None) or []
            for action in actions:
                try:
                    team, action_type, player, bid = action
                except Exception:
                    continue
                manager = team_manager_name(team)
                action_type = (action_type or "").upper()
                if "TRADE" in action_type:
                    activity_by_manager[manager]["trades"] += 1
                elif "ADD" in action_type or "WAIVER" in action_type:
                    activity_by_manager[manager]["waiver_adds"] += 1

    tally_year(current_league)
    for year in range(start_year, end_year + 1):
        try:
            yr_league = League(league_id=int(league_id), year=year, swid=swid, espn_s2=espn_s2)
            tally_year(yr_league)
        except Exception:
            continue

    if not activity_by_manager:
        return None

    trade_counts = [v["trades"] for v in activity_by_manager.values()]
    median_trades = sorted(trade_counts)[len(trade_counts) // 2] if trade_counts else 0

    profiles = []
    for manager, counts in activity_by_manager.items():
        trades, adds = counts["trades"], counts["waiver_adds"]
        if trades == 0 and adds <= 2:
            tag = "The Ghost"
            desc = "Sets a lineup and rarely touches the roster otherwise."
        elif trades > max(median_trades, 3):
            tag = "The Chaos Agent"
            desc = "Trades more than almost anyone else in the league."
        elif adds > 15:
            tag = "The Waiver Hawk"
            desc = "Lives on the waiver wire, adding constantly."
        else:
            tag = "The Steady Hand"
            desc = "A measured amount of activity — no extremes either way."
        profiles.append({"manager": manager, "trades": trades, "waiver_adds": adds, "tag": tag, "description": desc})

    profiles.sort(key=lambda p: -(p["trades"] + p["waiver_adds"]))
    return {"profiles": profiles, "coverage_note": f"Transaction data available for {years_with_data} season(s) — older years may be missing if ESPN's API doesn't expose that far back."}


def build_historical_bests(all_matchups):
    """
    Best Single Week, Best Championship Game, Best Playoff Game — all
    computed from the full matchup log. Championship game is approximated
    as the highest-scoring playoff matchup in each year's final week
    (heuristic: espn-api's playoff/matchup-type flags aren't consistently
    populated across all league configurations, so "final week + flagged
    playoff" is the most reliable signal available).
    """
    if not all_matchups:
        return {}

    best_week = max(all_matchups, key=lambda m: m["score"])

    playoff_matchups = [m for m in all_matchups if m.get("is_playoff")]
    best_playoff = max(playoff_matchups, key=lambda m: m["score"]) if playoff_matchups else None

    best_championship = None
    if playoff_matchups:
        by_year = defaultdict(list)
        for m in playoff_matchups:
            by_year[m["year"]].append(m)
        championship_candidates = []
        for year, matchups in by_year.items():
            final_week = max(m["week"] for m in matchups)
            championship_candidates.extend([m for m in matchups if m["week"] == final_week])
        if championship_candidates:
            best_championship = max(championship_candidates, key=lambda m: m["score"])

    result = {
        "best_single_week": {
            "manager": best_week["manager"], "score": round(best_week["score"], 1),
            "year": best_week["year"], "week": best_week["week"],
        }
    }
    if best_championship:
        result["best_championship_game"] = {
            "manager": best_championship["manager"], "score": round(best_championship["score"], 1),
            "year": best_championship["year"],
        }
    if best_playoff:
        result["best_playoff_game"] = {
            "manager": best_playoff["manager"], "score": round(best_playoff["score"], 1),
            "year": best_playoff["year"], "week": best_playoff["week"],
        }
    return result


def build_playoff_stats(all_matchups, all_years):
    """
    Real playoff-appearance stats across every season on file, computed
    from the same is_playoff-tagged matchup log used for historical bests.
    A manager "made the playoffs" in a given year if they appear in at
    least one matchup flagged is_playoff that year.

    Returns:
      - most_appearances: manager with the most playoff-season count,
        plus how many total seasons are on file (for the "X of Y" display)
      - longest_drought: manager with the longest CURRENT drought — i.e.
        counting back from the most recent season on file, how many
        consecutive years they've missed the playoffs. A manager who made
        it last season has a drought of 0 and won't win this.
    """
    if not all_matchups or not all_years:
        return {}

    years_sorted = sorted(all_years)
    playoff_years_by_manager = defaultdict(set)
    for m in all_matchups:
        if m.get("is_playoff"):
            playoff_years_by_manager[m["manager"]].add(m["year"])

    all_managers = set(m["manager"] for m in all_matchups)
    total_seasons = len(years_sorted)

    appearance_counts = {
        mgr: len(playoff_years_by_manager.get(mgr, set())) for mgr in all_managers
    }
    most_appearances_mgr = max(appearance_counts, key=lambda m: appearance_counts[m]) if appearance_counts else None

    droughts = {}
    for mgr in all_managers:
        made = playoff_years_by_manager.get(mgr, set())
        drought = 0
        for year in reversed(years_sorted):
            if year in made:
                break
            drought += 1
        droughts[mgr] = drought
    longest_drought_mgr = max(droughts, key=lambda m: droughts[m]) if droughts else None

    result = {"total_seasons": total_seasons}
    if most_appearances_mgr:
        result["most_appearances"] = {
            "manager": most_appearances_mgr,
            "count": appearance_counts[most_appearances_mgr],
            "total_seasons": total_seasons,
        }
    if longest_drought_mgr and droughts[longest_drought_mgr] > 0:
        result["longest_drought"] = {
            "manager": longest_drought_mgr,
            "seasons": droughts[longest_drought_mgr],
        }
    return result


def build_comeback_by_season(historical_seasons):
    """
    Real "Comeback Player of the Year" as a season-by-season AWARD (not a
    live in-progress stat): for every pair of consecutive completed
    seasons, whichever manager improved their final standing the most
    versus the year before wins that year's award. Only needs each
    season's final standings (not box-score/matchup data), so this isn't
    subject to the same ESPN historical-coverage gaps that can affect
    Rivalries/head-to-head/best-game stats.
    """
    completed = sorted(
        [s for s in historical_seasons if s.get("teams") and not s.get("error")],
        key=lambda s: s["year"],
    )
    results = []
    for i in range(1, len(completed)):
        prev_season, this_season = completed[i - 1], completed[i]
        prev_standing = {t["manager"]: t["standing"] for t in prev_season["teams"] if t.get("manager")}

        best = None
        for t in this_season["teams"]:
            manager = t.get("manager")
            if not manager or manager not in prev_standing:
                continue
            improvement = prev_standing[manager] - t["standing"]
            if best is None or improvement > best["improvement"]:
                best = {
                    "year": this_season["year"], "manager": manager, "team_name": t.get("team_name"),
                    "improvement": improvement,
                    "prev_standing": prev_standing[manager], "current_standing": t["standing"],
                }
        if best and best["improvement"] > 0:
            results.append(best)

    results.sort(key=lambda r: -r["year"])
    return results


def build_streaks(all_matchups):
    """
    Longest active win/loss streak (as of the most recent game each
    manager has played) and longest ever win/loss streak (at any point
    in their history) — computed by walking each manager's own games in
    real chronological order. Ties break a streak in both directions
    (neither extends a win nor a loss streak).
    """
    by_manager = defaultdict(list)
    for m in all_matchups:
        by_manager[m["manager"]].append(m)

    active_win, active_loss = {}, {}
    alltime_win, alltime_loss = {}, {}

    for manager, games in by_manager.items():
        games = sorted(games, key=lambda g: (g["year"], g["week"]))

        cur_win = cur_loss = 0
        best_win = best_loss = 0
        best_win_end = best_loss_end = None

        for g in games:
            if g["score"] > g["opponent_score"]:
                cur_win += 1
                cur_loss = 0
            elif g["opponent_score"] > g["score"]:
                cur_loss += 1
                cur_win = 0
            else:
                cur_win = cur_loss = 0

            if cur_win > best_win:
                best_win = cur_win
                best_win_end = {"year": g["year"], "week": g["week"]}
            if cur_loss > best_loss:
                best_loss = cur_loss
                best_loss_end = {"year": g["year"], "week": g["week"]}

        active_win[manager] = cur_win
        active_loss[manager] = cur_loss
        alltime_win[manager] = {"length": best_win, "through": best_win_end}
        alltime_loss[manager] = {"length": best_loss, "through": best_loss_end}

    result = {}

    top_active_win = max(active_win, key=lambda m: active_win[m]) if active_win else None
    if top_active_win and active_win[top_active_win] >= 2:
        result["active_win_streak"] = {"manager": top_active_win, "length": active_win[top_active_win]}

    top_active_loss = max(active_loss, key=lambda m: active_loss[m]) if active_loss else None
    if top_active_loss and active_loss[top_active_loss] >= 2:
        result["active_loss_streak"] = {"manager": top_active_loss, "length": active_loss[top_active_loss]}

    top_alltime_win = max(alltime_win, key=lambda m: alltime_win[m]["length"]) if alltime_win else None
    if top_alltime_win and alltime_win[top_alltime_win]["length"] >= 2:
        result["alltime_win_streak"] = {
            "manager": top_alltime_win, "length": alltime_win[top_alltime_win]["length"],
            "through": alltime_win[top_alltime_win]["through"],
        }

    top_alltime_loss = max(alltime_loss, key=lambda m: alltime_loss[m]["length"]) if alltime_loss else None
    if top_alltime_loss and alltime_loss[top_alltime_loss]["length"] >= 2:
        result["alltime_loss_streak"] = {
            "manager": top_alltime_loss, "length": alltime_loss[top_alltime_loss]["length"],
            "through": alltime_loss[top_alltime_loss]["through"],
        }

    return result


def build_best_never_won(historical_seasons, current_season):
    """
    "Best Team to Never Win It All" — highest all-time win percentage
    among managers with zero championships (standing == 1) across every
    completed season on file. Needs at least 20 combined games played to
    qualify, so one hot partial season doesn't win it on a tiny sample.
    """
    totals = defaultdict(lambda: {"wins": 0, "losses": 0, "team_name": None})
    champions = set()

    all_seasons = [s for s in historical_seasons if s.get("teams") and not s.get("error")]
    if current_season and current_season.get("teams"):
        all_seasons = all_seasons + [current_season]

    for season in all_seasons:
        for t in season["teams"]:
            manager = t.get("manager")
            if not manager:
                continue
            totals[manager]["wins"] += t.get("wins", 0)
            totals[manager]["losses"] += t.get("losses", 0)
            totals[manager]["team_name"] = t.get("team_name")
            if t.get("standing") == 1:
                champions.add(manager)

    candidates = []
    for manager, rec in totals.items():
        if manager in champions:
            continue
        games = rec["wins"] + rec["losses"]
        if games < 20:
            continue
        candidates.append({
            "manager": manager, "team_name": rec["team_name"],
            "wins": rec["wins"], "losses": rec["losses"],
            "win_pct": round(rec["wins"] / games, 3),
        })

    if not candidates:
        return None
    candidates.sort(key=lambda c: -c["win_pct"])
    return candidates[0]


def build_elo_ratings(all_matchups, k_factor=32, start_rating=1500):
    """
    All-time power rating: standard Elo, processed over every real game
    in chronological order (each real game counted once — we only walk
    it from the alphabetically-first manager's perspective to avoid
    double-processing the two matchup-log entries every game produces).
    Reflects strength of who you beat, not just raw win total — a
    manager who beats strong opponents gains more than one who beats
    weak ones, and vice versa on losses.
    """
    ratings = defaultdict(lambda: float(start_rating))
    seen_games = set()

    games = sorted(all_matchups, key=lambda g: (g["year"], g["week"]))
    for g in games:
        a, b = g["manager"], g["opponent"]
        if a > b:
            continue  # process each real game once, from the alphabetically-first side
        game_key = (g["year"], g["week"], a, b)
        if game_key in seen_games:
            continue
        seen_games.add(game_key)

        ra, rb = ratings[a], ratings[b]
        expected_a = 1 / (1 + 10 ** ((rb - ra) / 400))
        if g["score"] > g["opponent_score"]:
            actual_a = 1.0
        elif g["score"] < g["opponent_score"]:
            actual_a = 0.0
        else:
            actual_a = 0.5

        ratings[a] = ra + k_factor * (actual_a - expected_a)
        ratings[b] = rb + k_factor * ((1 - actual_a) - (1 - expected_a))

    result = [{"manager": m, "rating": round(r)} for m, r in ratings.items()]
    result.sort(key=lambda r: -r["rating"])
    return result


def build_comeback_player_of_year(historical_seasons, current_season):
    """
    "Pilon Comeback Player of the Year" — the manager whose current-season
    standing shows the biggest improvement over where they finished last
    season. E.g. finished 10th last year, currently sitting 2nd this year
    = an 8-spot improvement.

    Only computed once the current season has actually played at least one
    game (otherwise ESPN's preseason "standing" field is just placeholder
    ordering, not a real rank, and this would be meaningless noise). Also
    needs last season's data to exist at all — returns None for either
    case rather than showing something misleading.
    """
    if not historical_seasons or not current_season or not current_season.get("teams"):
        return None

    total_games_played = sum(t.get("wins", 0) + t.get("losses", 0) for t in current_season["teams"])
    if total_games_played == 0:
        return None  # preseason - standing field isn't meaningful yet

    completed = [s for s in historical_seasons if s.get("teams") and not s.get("error")]
    if not completed:
        return None
    last_season = max(completed, key=lambda s: s["year"])

    last_standing_by_manager = {t["manager"]: t["standing"] for t in last_season["teams"] if t.get("manager")}

    best = None
    for t in current_season["teams"]:
        manager = t.get("manager")
        current_standing = t.get("standing")
        prev_standing = last_standing_by_manager.get(manager)
        if manager is None or current_standing is None or prev_standing is None:
            continue
        improvement = prev_standing - current_standing  # positive = moved up (better)
        if best is None or improvement > best["improvement"]:
            best = {
                "manager": manager,
                "team_name": t.get("team_name"),
                "prev_year": last_season["year"],
                "prev_standing": prev_standing,
                "current_standing": current_standing,
                "improvement": improvement,
            }

    if best is None or best["improvement"] <= 0:
        return None  # nobody's actually improved yet - don't force an award that doesn't exist
    return best
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


def build_weekly_awards(league, previous_week_scores):
    """
    previous_week_scores: {manager: score} from the previous week, used
    for "Mickey Miles" (biggest week-over-week collapse). Comes from the
    box scores of (current completed week - 1), computed in this same run.
    """
    week, box_scores = find_latest_completed_week(league)
    if week is None:
        return None, {}

    team_results = []
    best_player = None
    best_bench = None
    this_week_scores = {}

    for m in box_scores:
        home_score = getattr(m, "home_score", 0) or 0
        away_score = getattr(m, "away_score", 0) or 0
        home_team = getattr(m, "home_team", None)
        away_team = getattr(m, "away_team", None)

        if home_team is not None:
            mgr = team_manager_name(home_team)
            this_week_scores[mgr] = home_score
            team_results.append({"manager": mgr, "team_name": getattr(home_team, "team_name", ""),
                                    "score": home_score, "opponent_score": away_score, "won": home_score > away_score})
        if away_team is not None:
            mgr = team_manager_name(away_team)
            this_week_scores[mgr] = away_score
            team_results.append({"manager": mgr, "team_name": getattr(away_team, "team_name", ""),
                                    "score": away_score, "opponent_score": home_score, "won": away_score > home_score})

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
        return None, this_week_scores

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

    if previous_week_scores:
        biggest_drop = None
        for mgr, score in this_week_scores.items():
            prev = previous_week_scores.get(mgr)
            if prev is None:
                continue
            drop = prev - score
            if biggest_drop is None or drop > biggest_drop["drop"]:
                biggest_drop = {"manager": mgr, "drop": round(drop, 1), "this_week": round(score, 1), "last_week": round(prev, 1)}
        if biggest_drop and biggest_drop["drop"] > 0:
            awards["mickey_miles"] = biggest_drop

    return awards, this_week_scores


def load_previous_cache(out_path):
    if not os.path.exists(out_path):
        return None
    try:
        with open(out_path) as f:
            return json.load(f)
    except Exception:
        return None


def compute_previous_ranks(previous_cache):
    """Manager -> rank (1 = best) as of the last committed run, for the Power Rankings MOVE column."""
    if not previous_cache or not previous_cache.get("current_season"):
        return {}
    teams = previous_cache["current_season"].get("teams", [])
    ranked = sorted(teams, key=lambda t: (-t.get("wins", 0), -t.get("points_for", 0)))
    return {t["manager"]: i + 1 for i, t in enumerate(ranked)}


def main():
    league_id = os.environ.get("ESPN_LEAGUE_ID")
    swid = os.environ.get("ESPN_SWID")
    espn_s2 = os.environ.get("ESPN_S2")

    if not (league_id and swid and espn_s2):
        print("Missing ESPN_LEAGUE_ID / ESPN_SWID / ESPN_S2 environment variables.")
        sys.exit(1)

    out_path = os.path.join(os.path.dirname(__file__), "..", "public", "data", "league-cache.json")
    previous_cache = load_previous_cache(out_path)
    previous_ranks = compute_previous_ranks(previous_cache)

    current_year = datetime.now(timezone.utc).year
    league = League(league_id=int(league_id), year=current_year, swid=swid, espn_s2=espn_s2)

    current_season = build_current_season(league)
    for t in current_season["teams"]:
        t["previous_rank"] = previous_ranks.get(t["manager"])

    print("Fetching historical seasons + full matchup log (this is the slow part)...")
    historical_seasons, all_matchups = build_historical_seasons_and_matchups(
        league_id, swid, espn_s2, start_year=2010, end_year=current_year - 1
    )

    # Include the CURRENT season's completed-week matchups too, so rivalries/bests
    # reflect this year's games as they happen, not just past seasons.
    current_matchups = collect_season_matchups(league, up_to_week=getattr(league, "current_week", None))
    for m in current_matchups:
        m["year"] = current_year
    all_matchups_with_current = all_matchups + current_matchups

    print(f"Collected {len(all_matchups_with_current)} matchup-sides across all seasons.")

    rivalries = build_rivalries(all_matchups_with_current)
    grudge_bowl_h2h = build_head_to_head_log(all_matchups_with_current, "Dallas Sessions", "Chris Mathison")
    historical_bests = build_historical_bests(all_matchups_with_current)
    completed_years = [s["year"] for s in historical_seasons if "error" not in s]
    playoff_stats = build_playoff_stats(all_matchups_with_current, completed_years)

    # Which years actually contributed matchup-level data vs. standings-only
    # (see the note in build_historical_seasons_and_matchups for why these
    # can differ) — exposed in the output so it's inspectable later, not
    # just visible in the Action's run logs.
    years_with_matchups = sorted(set(m["year"] for m in all_matchups_with_current))
    years_standings_only = sorted(set(completed_years) - set(years_with_matchups))
    matchup_data_coverage = {
        "years_with_matchup_data": years_with_matchups,
        "years_standings_only_no_matchups": years_standings_only,
    }

    print("Fetching manager transaction profiles...")
    manager_profiles = None
    try:
        manager_profiles = build_manager_profiles(league_id, swid, espn_s2, 2010, current_year - 1, league)
    except Exception as e:
        print(f"Manager profiles computation failed (non-fatal): {e}")

    # previous week's scores, for Mickey Miles — pull from (latest completed week - 1)
    # if it exists, using the matchups we already collected for the current season.
    previous_week_scores = {}
    try:
        weeks_seen = sorted(set(m["week"] for m in current_matchups))
        if len(weeks_seen) >= 2:
            second_to_last_week = weeks_seen[-2]
            for m in current_matchups:
                if m["week"] == second_to_last_week:
                    previous_week_scores[m["manager"]] = m["score"]
    except Exception:
        pass

    weekly_awards = None
    try:
        weekly_awards, _ = build_weekly_awards(league, previous_week_scores)
    except Exception as e:
        print(f"Weekly awards computation failed (non-fatal): {e}")

    comeback_player = None
    try:
        comeback_player = build_comeback_player_of_year(historical_seasons, current_season)
    except Exception as e:
        print(f"Comeback player of the year computation failed (non-fatal): {e}")

    comeback_by_season = []
    try:
        comeback_by_season = build_comeback_by_season(historical_seasons)
    except Exception as e:
        print(f"Comeback by season computation failed (non-fatal): {e}")

    streaks = {}
    try:
        streaks = build_streaks(all_matchups_with_current)
    except Exception as e:
        print(f"Streaks computation failed (non-fatal): {e}")

    best_never_won = None
    try:
        best_never_won = build_best_never_won(historical_seasons, current_season)
    except Exception as e:
        print(f"Best-never-won computation failed (non-fatal): {e}")

    elo_ratings = []
    try:
        elo_ratings = build_elo_ratings(all_matchups_with_current)
    except Exception as e:
        print(f"Elo ratings computation failed (non-fatal): {e}")

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "league_id": league_id,
        "current_season": current_season,
        "weekly_awards": weekly_awards,
        "historical_seasons": historical_seasons,
        "rivalries": rivalries,
        "grudge_bowl_h2h": grudge_bowl_h2h,
        "manager_profiles": manager_profiles,
        "historical_bests": historical_bests,
        "playoff_stats": playoff_stats,
        "matchup_data_coverage": matchup_data_coverage,
        "comeback_player_of_year": comeback_player,
        "comeback_by_season": comeback_by_season,
        "streaks": streaks,
        "best_never_won": best_never_won,
        "elo_ratings": elo_ratings,
    }

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"Wrote {out_path} — generated_at {output['generated_at']}")


if __name__ == "__main__":
    main()
