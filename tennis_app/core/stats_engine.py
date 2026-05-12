"""
Advanced statistics computation engine for tennis analytics.

Includes:
- Score string parser (sets, games, tiebreaks)
- Computed stats from existing match data
- Aggregation helpers (career, season, surface, tournament level)
- tennisratio-style derived metrics (Dominance Ratio, Pressure Points, etc.)
"""

import logging
import re
from collections import defaultdict

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Score parser
# ---------------------------------------------------------------------------

def parse_score(score_str):
    """
    Parse a tennis score string into structured data.

    Examples:
        "6-4 7-6(3) 3-6 6-2"  -> 4 sets
        "7-6(7) 6-7(3) 7-6(5)" -> 3 tiebreak sets
        "6-4 6-4"              -> straight set win
        "W/O", "RET", ""       -> None (incomplete)

    Returns dict with:
        sets_won, sets_lost, games_won, games_lost,
        tiebreaks_won, tiebreaks_lost, deciding_set (bool),
        set_scores (list of (w_games, l_games, tb_points|None)),
        is_straight_sets (bool), total_sets, total_games
    Or None if score cannot be parsed.
    """
    if not score_str or not isinstance(score_str, str):
        return None

    score_str = score_str.strip()

    # Filter out walkovers, retirements, defaults, incomplete
    if any(tag in score_str.upper() for tag in ("W/O", "DEF", "UNP", "ABD")):
        return None

    # Remove RET/retirement suffix but still parse the played sets
    clean = re.sub(r'\s*(RET|Ret|ret)\.?$', '', score_str).strip()
    if not clean:
        return None

    # Split into set tokens
    set_tokens = clean.split()

    sets_won = 0
    sets_lost = 0
    games_won = 0
    games_lost = 0
    tiebreaks_won = 0
    tiebreaks_lost = 0
    set_scores = []

    for token in set_tokens:
        # Match patterns like: 6-4, 7-6(3), 7-6(10), 6-7(5), [10-8]
        # Also handle super-tiebreak: [10-5], [7-5]
        m = re.match(r'\[?(\d+)-(\d+)\]?(?:\((\d+)\))?$', token)
        if not m:
            continue

        w_games = int(m.group(1))
        l_games = int(m.group(2))
        tb_points = int(m.group(3)) if m.group(3) else None

        games_won += w_games
        games_lost += l_games

        # Determine set winner
        if w_games > l_games:
            sets_won += 1
            if tb_points is not None:
                tiebreaks_won += 1
        elif l_games > w_games:
            sets_lost += 1
            if tb_points is not None:
                tiebreaks_lost += 1
        else:
            # Equal games (shouldn't happen in a completed set unless
            # it's a tiebreak situation like 7-6 without parenthetical)
            # This handles edge cases
            if tb_points is not None:
                if w_games > l_games:
                    sets_won += 1
                    tiebreaks_won += 1
                else:
                    sets_lost += 1
                    tiebreaks_lost += 1

        set_scores.append((w_games, l_games, tb_points))

    if not set_scores:
        return None

    total_sets = sets_won + sets_lost
    best_of = 5 if total_sets > 3 else 3
    deciding_set = (total_sets == best_of and
                    sets_won > 0 and sets_lost > 0 and
                    abs(sets_won - sets_lost) == 1)
    is_straight = sets_lost == 0

    return {
        "sets_won": sets_won,
        "sets_lost": sets_lost,
        "games_won": games_won,
        "games_lost": games_lost,
        "tiebreaks_won": tiebreaks_won,
        "tiebreaks_lost": tiebreaks_lost,
        "deciding_set": deciding_set,
        "set_scores": set_scores,
        "is_straight_sets": is_straight,
        "total_sets": total_sets,
        "total_games": games_won + games_lost,
        "best_of": best_of,
    }


# ---------------------------------------------------------------------------
# Match-level stat computation (from existing DB fields)
# ---------------------------------------------------------------------------

def compute_match_stats(match, player_id, player_name=None):
    """
    Compute derived statistics for a single match from the player's perspective.

    Parameters
    ----------
    match : dict
        A match row from the database.
    player_id : str
        The player whose stats we're computing.
    player_name : str, optional
        Full name of the player (used as fallback when winner_id is empty,
        which happens for SCRAPED matches where ID resolution failed).

    Returns dict of computed stats (None values for unavailable data).
    """
    is_winner = str(match.get("winner_id")) == str(player_id)

    # Fallback for SCRAPED matches where both winner_id and loser_id are ''
    if not is_winner and not match.get("winner_id") and player_name:
        w_name = (match.get("winner_name") or "").replace("-", " ").strip().lower()
        p_norm = player_name.replace("-", " ").strip().lower()
        is_winner = w_name == p_norm

    # Prefix: 'w_' for player stats, 'l_' for opponent stats
    if is_winner:
        p, o = "w_", "l_"
        won = True
    else:
        p, o = "l_", "w_"
        won = False

    def _g(prefix, key):
        val = match.get(f"{prefix}{key}")
        if val is None or val == "":
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    # Basic serve stats
    aces = _g(p, "ace")
    dfs = _g(p, "df")
    svpt = _g(p, "svpt")
    first_in = _g(p, "1stIn")
    first_won = _g(p, "1stWon")
    second_won = _g(p, "2ndWon")
    sv_gms = _g(p, "SvGms")
    bp_saved = _g(p, "bpSaved")
    bp_faced = _g(p, "bpFaced")

    # Opponent serve stats
    o_aces = _g(o, "ace")
    o_dfs = _g(o, "df")
    o_svpt = _g(o, "svpt")
    o_first_in = _g(o, "1stIn")
    o_first_won = _g(o, "1stWon")
    o_second_won = _g(o, "2ndWon")
    o_sv_gms = _g(o, "SvGms")
    o_bp_saved = _g(o, "bpSaved")
    o_bp_faced = _g(o, "bpFaced")

    stats = {"won": won}

    # --- Serve percentages ---
    if svpt and svpt > 0:
        if first_in is not None:
            stats["first_serve_pct"] = round(first_in / svpt * 100, 1)
        if first_in and first_in > 0 and first_won is not None:
            stats["first_serve_won_pct"] = round(first_won / first_in * 100, 1)
        second_attempts = svpt - (first_in or 0)
        if second_attempts > 0 and second_won is not None:
            stats["second_serve_won_pct"] = round(second_won / second_attempts * 100, 1)
        total_serve_won = (first_won or 0) + (second_won or 0)
        stats["total_serve_won_pct"] = round(total_serve_won / svpt * 100, 1)

    if aces is not None:
        stats["aces"] = int(aces)
    if dfs is not None:
        stats["double_faults"] = int(dfs)

    # --- Break points ---
    if bp_faced is not None and bp_faced > 0:
        stats["bp_faced"] = int(bp_faced)
        if bp_saved is not None:
            stats["bp_saved"] = int(bp_saved)
            stats["bp_save_pct"] = round(bp_saved / bp_faced * 100, 1)

    # --- Return stats ---
    if o_svpt and o_svpt > 0:
        o_total_won = (o_first_won or 0) + (o_second_won or 0)
        return_pts_won = o_svpt - o_total_won
        stats["return_pts_won_pct"] = round(return_pts_won / o_svpt * 100, 1)

        if o_first_in and o_first_in > 0 and o_first_won is not None:
            stats["first_return_won_pct"] = round(
                (o_first_in - o_first_won) / o_first_in * 100, 1)
        o_second_attempts = o_svpt - (o_first_in or 0)
        if o_second_attempts > 0 and o_second_won is not None:
            stats["second_return_won_pct"] = round(
                (o_second_attempts - o_second_won) / o_second_attempts * 100, 1)

    # --- Break points on return (converted) ---
    if o_bp_faced is not None and o_bp_faced > 0:
        bp_won = o_bp_faced - (o_bp_saved or 0)
        stats["bp_converted"] = int(bp_won)
        stats["bp_opportunities"] = int(o_bp_faced)
        stats["bp_conversion_pct"] = round(bp_won / o_bp_faced * 100, 1)

    # --- Total points won ---
    if svpt and o_svpt:
        p_serve_won = (first_won or 0) + (second_won or 0)
        o_serve_won = (o_first_won or 0) + (o_second_won or 0)
        total_points = svpt + o_svpt
        points_won = p_serve_won + (o_svpt - o_serve_won)
        stats["total_points_won"] = int(points_won)
        stats["total_points_played"] = int(total_points)
        stats["total_points_won_pct"] = round(points_won / total_points * 100, 1)

    # --- Dominance Ratio (tennisratio style) ---
    # BP created / BP faced = attacking power vs defensive pressure
    if o_bp_faced is not None and bp_faced is not None and bp_faced > 0:
        stats["dominance_ratio"] = round(o_bp_faced / bp_faced, 2)

    # --- Service games held ---
    if sv_gms is not None and bp_faced is not None:
        bp_lost = bp_faced - (bp_saved or 0)
        games_broken = bp_lost  # each break = 1 lost service game
        stats["service_games_held"] = int(sv_gms - games_broken)
        stats["service_games_total"] = int(sv_gms)
        if sv_gms > 0:
            stats["hold_pct"] = round((sv_gms - games_broken) / sv_gms * 100, 1)

    # --- Score parsing ---
    score_data = parse_score(match.get("score", ""))
    if score_data:
        stats["sets_won"] = score_data["sets_won"] if won else score_data["sets_lost"]
        stats["sets_lost"] = score_data["sets_lost"] if won else score_data["sets_won"]
        stats["games_won"] = score_data["games_won"] if won else score_data["games_lost"]
        stats["games_lost"] = score_data["games_lost"] if won else score_data["games_won"]
        stats["tiebreaks_won"] = score_data["tiebreaks_won"] if won else score_data["tiebreaks_lost"]
        stats["tiebreaks_lost"] = score_data["tiebreaks_lost"] if won else score_data["tiebreaks_won"]
        stats["deciding_set"] = score_data["deciding_set"]
        stats["straight_sets"] = score_data["is_straight_sets"] if won else False

    stats["minutes"] = match.get("minutes")
    stats["round"] = match.get("round") or ""

    return stats


# ---------------------------------------------------------------------------
# Aggregation engine
# ---------------------------------------------------------------------------

def aggregate_stats(match_stats_list):
    """
    Aggregate a list of per-match stat dicts into career/period summaries.

    Parameters
    ----------
    match_stats_list : list[dict]
        Output from compute_match_stats for each match.

    Returns dict of aggregated stats.
    """
    if not match_stats_list:
        return {}

    n = len(match_stats_list)
    wins = sum(1 for s in match_stats_list if s.get("won"))
    losses = n - wins

    agg = {
        "matches": n,
        "wins": wins,
        "losses": losses,
        "win_pct": round(wins / n * 100, 1) if n else 0,
    }

    # Sum / average numeric fields
    _sum_fields = [
        "aces", "double_faults", "bp_faced", "bp_saved",
        "bp_converted", "bp_opportunities",
        "total_points_won", "total_points_played",
        "service_games_held", "service_games_total",
        "sets_won", "sets_lost", "games_won", "games_lost",
        "tiebreaks_won", "tiebreaks_lost",
    ]
    _avg_fields = [
        "first_serve_pct", "first_serve_won_pct", "second_serve_won_pct",
        "total_serve_won_pct", "bp_save_pct", "bp_conversion_pct",
        "return_pts_won_pct", "first_return_won_pct", "second_return_won_pct",
        "total_points_won_pct", "hold_pct", "dominance_ratio",
    ]

    for field in _sum_fields:
        vals = [s[field] for s in match_stats_list if s.get(field) is not None]
        if vals:
            agg[field] = sum(vals)

    for field in _avg_fields:
        vals = [s[field] for s in match_stats_list if s.get(field) is not None]
        if vals:
            agg[f"avg_{field}"] = round(sum(vals) / len(vals), 1)
            agg[f"{field}_count"] = len(vals)

    # Derived aggregations
    if agg.get("aces") is not None and n > 0:
        agg["aces_per_match"] = round(agg["aces"] / n, 1)
    if agg.get("double_faults") is not None and n > 0:
        agg["dfs_per_match"] = round(agg["double_faults"] / n, 1)

    # Overall BP save %
    if agg.get("bp_faced") and agg["bp_faced"] > 0:
        agg["overall_bp_save_pct"] = round(
            (agg.get("bp_saved", 0)) / agg["bp_faced"] * 100, 1)
    # Overall BP conversion %
    if agg.get("bp_opportunities") and agg["bp_opportunities"] > 0:
        agg["overall_bp_conversion_pct"] = round(
            (agg.get("bp_converted", 0)) / agg["bp_opportunities"] * 100, 1)
    # Overall hold %
    if agg.get("service_games_total") and agg["service_games_total"] > 0:
        agg["overall_hold_pct"] = round(
            agg.get("service_games_held", 0) / agg["service_games_total"] * 100, 1)

    # Deciding set record
    deciding = [s for s in match_stats_list if s.get("deciding_set")]
    if deciding:
        dw = sum(1 for s in deciding if s.get("won"))
        agg["deciding_set_wins"] = dw
        agg["deciding_set_losses"] = len(deciding) - dw
        agg["deciding_set_pct"] = round(dw / len(deciding) * 100, 1)

    # Straight set wins
    straight = sum(1 for s in match_stats_list
                   if s.get("straight_sets") and s.get("won"))
    if wins > 0:
        agg["straight_set_wins"] = straight
        agg["straight_set_win_pct"] = round(straight / wins * 100, 1)

    # Tiebreak record
    tb_won = agg.get("tiebreaks_won", 0)
    tb_lost = agg.get("tiebreaks_lost", 0)
    if tb_won + tb_lost > 0:
        agg["tiebreak_pct"] = round(tb_won / (tb_won + tb_lost) * 100, 1)

    # Minutes
    mins = [s["minutes"] for s in match_stats_list
            if s.get("minutes") is not None and s["minutes"] > 0]
    if mins:
        agg["avg_match_duration"] = round(sum(mins) / len(mins), 0)
        agg["total_match_minutes"] = round(sum(mins), 0)

    # Dominance Ratio (tennisratio) — career average
    # Already in avg_dominance_ratio from the loop above

    # Breakpoints Prevail = BP converted / BP lost
    bp_lost = (agg.get("bp_faced", 0) or 0) - (agg.get("bp_saved", 0) or 0)
    bp_conv = agg.get("bp_converted", 0) or 0
    if bp_lost > 0:
        agg["breakpoints_prevail"] = round(bp_conv / bp_lost, 2)

    # --- Tournament progression by round ---
    # For each round, count how many times the player played (and won) that round.
    # "Titles" = won the F.  "Finals" = played F (win or loss).  etc.
    _round_order = ["F", "SF", "QF", "R16", "R32", "R64", "R128", "RR"]
    rounds_played: dict[str, int] = {}
    rounds_won: dict[str, int] = {}
    for s in match_stats_list:
        r = s.get("round") or ""
        if not r:
            continue
        rounds_played[r] = rounds_played.get(r, 0) + 1
        if s.get("won"):
            rounds_won[r] = rounds_won.get(r, 0) + 1
    if rounds_played:
        agg["rounds_played"] = rounds_played
        agg["rounds_won"] = rounds_won
        # Convenience fields for the most common rounds
        agg["titles"] = rounds_won.get("F", 0)
        agg["finals"] = rounds_played.get("F", 0)
        agg["semis"] = rounds_played.get("SF", 0)
        agg["quarters"] = rounds_played.get("QF", 0)
        agg["r16"] = rounds_played.get("R16", 0)
        agg["r32"] = rounds_played.get("R32", 0)
        agg["r64"] = rounds_played.get("R64", 0)

    return agg


# ---------------------------------------------------------------------------
# Pressure Points (tennisratio-style)
# ---------------------------------------------------------------------------

def compute_pressure_stats(matches, player_id, player_name=None):
    """
    Compute pressure-point statistics for a player.

    These measure performance in critical game scores (0-30, 15-30,
    30-30, deuce, 0-40, 15-40, 30-40, ad-out). We approximate from
    break point data since we don't have point-by-point data.

    Returns dict with pressure metrics.
    """
    stats_list = []
    for m in matches:
        ms = compute_match_stats(m, player_id, player_name=player_name)
        if ms:
            stats_list.append(ms)

    if not stats_list:
        return {}

    agg = aggregate_stats(stats_list)

    pressure = {}

    # Clutch serving: BP save % (serving under pressure)
    if agg.get("overall_bp_save_pct") is not None:
        pressure["clutch_serve_pct"] = agg["overall_bp_save_pct"]

    # Clutch returning: BP conversion % (returning under pressure)
    if agg.get("overall_bp_conversion_pct") is not None:
        pressure["clutch_return_pct"] = agg["overall_bp_conversion_pct"]

    # Dominance metrics
    if agg.get("avg_dominance_ratio") is not None:
        pressure["dominance_ratio"] = agg["avg_dominance_ratio"]
    if agg.get("breakpoints_prevail") is not None:
        pressure["breakpoints_prevail"] = agg["breakpoints_prevail"]

    # Deciding set performance
    if agg.get("deciding_set_pct") is not None:
        pressure["deciding_set_win_pct"] = agg["deciding_set_pct"]

    # Tiebreak performance
    if agg.get("tiebreak_pct") is not None:
        pressure["tiebreak_win_pct"] = agg["tiebreak_pct"]

    return pressure


# ---------------------------------------------------------------------------
# Filter helpers
# ---------------------------------------------------------------------------

def filter_matches(matches, surface=None, year=None, tourney_level=None,
                   opponent_id=None, round_=None):
    """Filter a list of match dicts by surface, year, level, round, opponent.

    ``tourney_level`` and ``round_`` may be a scalar or an iterable of
    scalars (multi-select).
    """
    def _as_set(value):
        if value in (None, "", []):
            return None
        if isinstance(value, (list, tuple, set)):
            vals = {v for v in value if v not in (None, "")}
            return vals or None
        return {value}

    result = matches
    if surface:
        result = [m for m in result if m.get("surface") == surface]
    if year:
        yr = str(year)
        result = [m for m in result
                  if str(m.get("tourney_date", ""))[:4] == yr]
    levels = _as_set(tourney_level)
    if levels:
        result = [m for m in result if m.get("tourney_level") in levels]
    rounds = _as_set(round_)
    if rounds:
        result = [m for m in result if m.get("round") in rounds]
    if opponent_id:
        oid = str(opponent_id)
        result = [m for m in result
                  if str(m.get("winner_id")) == oid or str(m.get("loser_id")) == oid]
    return result


def compute_player_advanced_stats(matches, player_id, surface=None,
                                  year=None, tourney_level=None,
                                  round_=None, player_name=None):
    """
    Convenience function: filter matches, compute per-match stats,
    aggregate, and return the full advanced stats dict.
    """
    filtered = filter_matches(matches, surface=surface, year=year,
                              tourney_level=tourney_level, round_=round_)
    if not filtered:
        return {}

    per_match = []
    for m in filtered:
        ms = compute_match_stats(m, player_id, player_name=player_name)
        if ms:
            per_match.append(ms)

    agg = aggregate_stats(per_match)
    pressure = compute_pressure_stats(filtered, player_id,
                                      player_name=player_name)
    agg["pressure"] = pressure

    return agg


# ---------------------------------------------------------------------------
# Year-by-year advanced stats
# ---------------------------------------------------------------------------

def compute_yearly_advanced_stats(matches, player_id, player_name=None):
    """
    Compute advanced stats broken down by year.

    Returns dict: { "2023": {stats}, "2024": {stats}, ... }
    """
    by_year = defaultdict(list)
    for m in matches:
        yr = str(m.get("tourney_date", ""))[:4]
        if yr and yr.isdigit():
            by_year[yr].append(m)

    result = {}
    for yr in sorted(by_year.keys()):
        stats = compute_player_advanced_stats(
            by_year[yr], player_id, player_name=player_name)
        if stats:
            result[yr] = stats

    return result
