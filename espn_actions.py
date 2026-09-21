#!/usr/bin/env python3
"""Roster read + write operations for the autonomous ESPN Fantasy Check agent.

Credentials come from the environment:
  ESPN_S2, ESPN_SWID, ESPN_LEAGUE_ID, ESPN_TEAM_ID, ESPN_SEASON_YEAR

Reads go through espn_api. Writes hit ESPN's (undocumented) transactions
endpoint directly. Every mutating subcommand is a DRY RUN that only prints the
payload unless --execute is passed, and always echoes ESPN's raw response so
failures are visible in the Actions log.

Subcommands:
  show           dump full league state as JSON (also written to --out)
  find           resolve a player name substring to id(s) + status
  poll           cheap state-diff check; exits/reports whether a full agent
                 run is warranted before the next scheduled one (see
                 cmd_poll's docstring for the exact trigger conditions)
  set-lineup     start/sit — --move PID:SLOT (repeatable) [--execute]
  add-drop       free-agent add, optional paired drop [--execute]
  waiver         waiver/FAAB claim — --add PID [--drop PID] --bid N [--execute]
  trade-propose  --to-team TID --send PID,PID --receive PID,PID [--execute]
  trade-respond  --id TXID (--accept | --decline) [--execute]
"""
import argparse
import json
import os
import sys
import time

import requests
from espn_api.football import League
from espn_api.football.constant import PRO_TEAM_MAP

S2 = os.environ["ESPN_S2"]
SWID = os.environ["ESPN_SWID"]
LID = int(os.environ["ESPN_LEAGUE_ID"].strip())
TID = int(os.environ["ESPN_TEAM_ID"].strip())
YEAR = int(os.environ["ESPN_SEASON_YEAR"].strip())

READ_BASE = f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{YEAR}/segments/0/leagues/{LID}"
WRITE_BASE = f"https://lm-api-writes.fantasy.espn.com/apis/v3/games/ffl/seasons/{YEAR}/segments/0/leagues/{LID}"

SLOT_NAME_TO_ID = {
    "QB": 0, "RB": 2, "RB/WR": 3, "WR": 4, "WR/TE": 5, "TE": 6, "OP": 7,
    "SUPERFLEX": 7, "D/ST": 16, "DST": 16, "K": 17, "P": 18, "HC": 19,
    "BE": 20, "BENCH": 20, "IR": 21, "FLEX": 23, "RB/WR/TE": 23,
}
SLOT_ID_TO_NAME = {v: k for k, v in {
    "QB": 0, "RB": 2, "RB/WR": 3, "WR": 4, "WR/TE": 5, "TE": 6, "OP": 7,
    "D/ST": 16, "K": 17, "P": 18, "HC": 19, "BE": 20, "IR": 21, "FLEX": 23,
}.items()}

PLAYER_KEYS = [
    "name", "playerId", "position", "lineupSlot", "injuryStatus", "proTeam",
    "pro_opponent", "on_bye_week", "projected_points", "points", "eligibleSlots",
    "posRank", "percent_started", "percent_owned",
]


def slot_id(name):
    key = str(name).upper().replace(" ", "")
    if key in SLOT_NAME_TO_ID:
        return SLOT_NAME_TO_ID[key]
    return SLOT_NAME_TO_ID.get(str(name).upper())


def session():
    s = requests.Session()
    s.cookies.set("espn_s2", S2)
    s.cookies.set("SWID", SWID)
    s.headers.update({
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-fantasy-platform": "kona-PROD",
        "x-fantasy-source": "kona",
        "Referer": f"https://fantasy.espn.com/football/team?leagueId={LID}&teamId={TID}",
        "User-Agent": "Mozilla/5.0 espn-fantasy-check-agent",
    })
    return s


def league():
    return League(league_id=LID, year=YEAR, espn_s2=S2, swid=SWID)


def pdump(p):
    return {k: getattr(p, k, None) for k in PLAYER_KEYS}


def compute_bye_weeks(lg):
    """Full-season bye week per NFL team abbreviation, e.g. {"DET": 5, ...}.

    The per-scoring-period views everything else here uses (roster, matchup,
    free_agents) only ever carry on_bye_week for the CURRENT week -- ESPN
    doesn't expose a forward look through them. That's the data gap flagged
    repeatedly in the daily reports: byes start ~Week 5 and 2-3-week-ahead
    depth planning was blocked without this.

    espn_api's League._get_all_pro_schedule() fetches the *entire season's*
    published NFL schedule in one lightweight call (view=proTeamSchedules_wl,
    the same one it uses internally, just not surfaced to us). A team's bye
    is simply the first week in that data with no game listed.
    """
    schedule = lg._get_all_pro_schedule()
    final_week = getattr(lg, "finalScoringPeriod", None) or 18
    byes = {}
    for team_id, games_by_week in schedule.items():
        name = PRO_TEAM_MAP.get(team_id)
        if not name or team_id == 0:
            continue
        for week in range(1, final_week + 1):
            if not games_by_week.get(str(week)):
                byes[name] = week
                break
    return byes


def fetch_wire_status(week, size=300):
    """Best-effort playerId -> 'FREEAGENT' | 'WAIVERS' for the whole wire.

    espn_api's free_agents() queries kona_player_info with
    filterStatus=[FREEAGENT,WAIVERS] but never surfaces which of the two
    applies to a given player, so every prior run had to learn a player's
    waiver status by trial-and-error (attempt add-drop, read the 409). This
    hits the same endpoint directly and pulls the per-entry "status" field
    ESPN's own filter is named after. If ESPN ever renames/removes it, this
    degrades to an empty dict and callers must treat that player as unknown
    rather than assume either status.
    """
    try:
        filters = {"players": {"filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
                                "limit": size,
                                "sortPercOwned": {"sortPriority": 1, "sortAsc": False}}}
        r = session().get(READ_BASE, params={"view": "kona_player_info", "scoringPeriodId": week},
                          headers={"x-fantasy-filter": json.dumps(filters)})
        if not r.ok:
            return {}
        out = {}
        for entry in r.json().get("players", []):
            pid, status = entry.get("id"), entry.get("status")
            if pid is not None and status:
                out[pid] = status
        return out
    except Exception:
        return {}


def post_transaction(body, execute, path="/transactions/"):
    print("REQUEST %s%s" % (WRITE_BASE, path))
    print(json.dumps(body, indent=2))
    if not execute:
        print("\nDRY RUN — nothing sent. Re-run with --execute to submit.")
        return 0
    r = session().post(WRITE_BASE + path, json=body)
    print("\nESPN response: HTTP %s" % r.status_code)
    try:
        print(json.dumps(r.json(), indent=2))
    except Exception:
        print(r.text[:4000])
    return 0 if r.ok else 1


# --------------------------------------------------------------------------- show
def build_snapshot(fa_size=12):
    """Fetch the full league snapshot dict. Shared by `show` and `poll` so
    both always see the exact same shape of data."""
    errors = []
    lg = league()
    week = getattr(lg, "current_week", None) or getattr(lg, "nfl_week", None)

    try:
        bye_weeks = compute_bye_weeks(lg)  # {"DET": 5, ...} -- full season, not just this week
    except Exception as e:
        bye_weeks = {}
        errors.append("bye_weeks: %s: %s" % (type(e).__name__, e))

    def stamp_byes(players):
        for p in players:
            p["bye_week"] = bye_weeks.get(p.get("proTeam"))
        return players

    teams = []
    my_team = None
    for t in lg.teams:
        entry = {
            "team_id": t.team_id,
            "name": t.team_name,
            "record": "%s-%s-%s" % (getattr(t, "wins", 0), getattr(t, "losses", 0), getattr(t, "ties", 0)),
            "faab_remaining": getattr(t, "acquisition_budget", None),
            "roster": stamp_byes([pdump(p) for p in getattr(t, "roster", [])]),
        }
        teams.append(entry)
        if t.team_id == TID:
            my_team = entry
    if my_team is None:
        errors.append("No team with team_id=%s" % TID)

    acq = {}
    try:
        r = session().get(READ_BASE, params={"view": "mSettings"})
        if r.ok:
            s = (r.json().get("settings") or {}).get("acquisitionSettings") or {}
            acq = {
                "acquisitionType": s.get("acquisitionType"),
                "isUsingAcquisitionBudget": s.get("isUsingAcquisitionBudget"),
                "acquisitionBudget": s.get("acquisitionBudget"),
                "waiverHours": s.get("waiverHours"),
                "waiverProcessOrder": s.get("waiverProcessOrder"),
            }
        else:
            errors.append("acquisition settings read: HTTP %s" % r.status_code)
    except Exception as e:
        errors.append("acquisition settings: %s: %s" % (type(e).__name__, e))

    matchup = None
    proj_by_pid = {}
    try:
        for b in lg.box_scores(week):
            home, away = getattr(b, "home_team", None), getattr(b, "away_team", None)
            for lu in (getattr(b, "home_lineup", []), getattr(b, "away_lineup", [])):
                for p in lu:
                    proj_by_pid[getattr(p, "playerId", None)] = getattr(p, "projected_points", None)
            hid, aid = getattr(home, "team_id", None), getattr(away, "team_id", None)
            if hid == TID or aid == TID:
                mine_home = hid == TID
                matchup = {
                    "opponent": (away if mine_home else home).team_name,
                    "my_projected": getattr(b, "home_projected" if mine_home else "away_projected", None),
                    "opp_projected": getattr(b, "away_projected" if mine_home else "home_projected", None),
                    "my_lineup": stamp_byes([pdump(p) for p in (b.home_lineup if mine_home else b.away_lineup)]),
                    "opp_lineup": stamp_byes([pdump(p) for p in (b.away_lineup if mine_home else b.home_lineup)]),
                }
    except Exception as e:
        errors.append("box_scores(%s): %s: %s" % (week, type(e).__name__, e))

    # backfill projections onto every roster player we can
    for entry in teams:
        for p in entry["roster"]:
            if p.get("projected_points") in (None, 0) and p.get("playerId") in proj_by_pid:
                p["projected_points"] = proj_by_pid[p["playerId"]]

    wire_status = fetch_wire_status(week, size=max(300, fa_size * 6))

    free_agents = {}
    for pos in ["QB", "RB", "WR", "TE", "D/ST", "K"]:
        try:
            free_agents[pos] = stamp_byes([pdump(p) for p in lg.free_agents(week=week, size=fa_size, position=pos)])
            for p in free_agents[pos]:
                p["wire_status"] = wire_status.get(p.get("playerId"))  # FREEAGENT | WAIVERS | None (unknown)
        except Exception as e:
            errors.append("free_agents %s: %s: %s" % (pos, type(e).__name__, e))

    # per-team FAAB remaining, if the league uses an acquisition budget
    if my_team is not None and acq.get("acquisitionBudget") is not None:
        try:
            r = session().get(READ_BASE, params={"view": "mTeam"})
            if r.ok:
                for t in r.json().get("teams", []):
                    if t.get("id") == TID:
                        spent = (t.get("transactionCounter") or {}).get("acquisitionBudgetSpent", 0)
                        my_team["faab_remaining"] = acq["acquisitionBudget"] - spent
        except Exception as e:
            errors.append("faab remaining: %s: %s" % (type(e).__name__, e))

    pending_trades = []
    try:
        s = session()
        r = s.get(READ_BASE, params=[("view", "mPendingTransactions"), ("view", "mTransactions2")])
        if r.ok:
            for tx in (r.json().get("pendingTransactions") or r.json().get("transactions") or []):
                if tx.get("type", "").startswith("TRADE") and (
                    tx.get("teamId") == TID
                    or any(it.get("fromTeamId") == TID or it.get("toTeamId") == TID for it in tx.get("items", []))
                ):
                    pending_trades.append(tx)
        else:
            errors.append("pending trades read: HTTP %s" % r.status_code)
    except Exception as e:
        errors.append("pending trades: %s: %s" % (type(e).__name__, e))

    return {
        "league": {"name": getattr(lg.settings, "name", None), "id": LID, "season": YEAR,
                   "current_week": week, "scoring_period_id": week},
        "my_team_id": TID,
        "my_team": my_team,
        "acquisition_settings": acq,
        "matchup": matchup,
        "free_agents": free_agents,
        "all_teams": teams,
        "pending_trades": pending_trades,
        "slot_ids": SLOT_NAME_TO_ID,
        "bye_weeks": bye_weeks,  # {"DET": 5, ...} full season, every NFL team
        "errors": errors,
        "fetched_at": time.time(),
    }


def cmd_show(args):
    data = build_snapshot(fa_size=args.fa_size)
    my_team, matchup, pending_trades = data["my_team"], data["matchup"], data["pending_trades"]

    text = json.dumps(data, indent=2, default=str)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text + "\n")
    print("league: %s (season %s, week %s)" % (data["league"]["name"], YEAR, data["league"]["current_week"]))
    print("my_team: %s" % (my_team and {k: my_team[k] for k in ("team_id", "name", "record", "faab_remaining")}))
    print("acquisitions: %s" % data["acquisition_settings"])
    print("roster: %s | matchup: %s | pending_trades: %s | errors: %s" % (
        len(my_team["roster"]) if my_team else 0,
        ("vs " + matchup["opponent"]) if matchup else "none",
        len(pending_trades), data["errors"] or "none"))
    if not args.out:
        print(text)
    return 0


# --------------------------------------------------------------------------- poll
CONCERNING_STATUSES = {"OUT", "DOUBTFUL", "SUSPENSION", "INJURY_RESERVE"}
QUESTIONABLE_STATUSES = {"QUESTIONABLE"}


def _slot_gap_key(pos, pid):
    return "lineup_gap:%s:%s" % (pos, pid)


def cmd_poll(args):
    """Cheap, no-LLM check for whether a special (out-of-schedule) full agent
    run is warranted before the next daily one. Diffs the current snapshot
    against the last poll's state and raises a reason the moment any of the
    following becomes newly true (resolved conditions drop out on their own,
    so a condition that recurs later re-triggers):

      Roster health (mine)
        1. A current STARTER's injuryStatus newly becomes OUT/DOUBTFUL/
           SUSPENSION/INJURY_RESERVE.
        2. A current STARTER's injuryStatus newly becomes QUESTIONABLE.
        3. Any ROSTERED player (mine) newly clears from a concerning/
           questionable status back to healthy (a return from injury).
        4. A BENCHED player's projection newly exceeds a STARTER's at a slot
           the benched player is eligible for (a lineup-optimality gap
           appears, independent of *why* — catches anything 1-3 miss, e.g.
           a pure projection swing with no status change at all).

      Waivers / free agency
        5. A free-agent/waiver-pool player newly out-projects one of my
           rostered players at his own position.
        6. A tracked wire player's status flips WAIVERS -> FREEAGENT (a
           confirmed, ESPN-reported clear — see fetch_wire_status).
        7. A tracked wire player crosses its estimated waiver-clear time
           (first-seen + acquisition_settings.waiverHours) while status 6
           is still unknown — a fallback for when ESPN's status field is
           unavailable, since that's exactly the failure mode issues #5-#7
           hit repeatedly by trial-and-error 409s.
        8. acquisition_settings itself changes (waiverHours, acquisitionType,
           acquisitionBudget, isUsingAcquisitionBudget) — a commissioner
           rule change.

      Trades
        9. A new pending trade appears that I did not propose (teamId != my
           team) — an incoming offer needing a timely response.
       10. An existing pending trade's status changes (e.g. PENDING ->
           EXECUTED/ACCEPTED/DECLINED/CANCELED).
       11. A pending trade I proposed is within 6 hours of its
           expirationDate and still PENDING.

      Systemic / market
       12. The snapshot's errors array becomes non-empty when the previous
           poll's was empty (new API/auth failure — see issues #1, #2).
       13. A tracked wire player's percent_owned jumps 15+ points between
           polls (a fast-moving breakout, as Jalen Coker was in #6/#7).
    """
    state_path = args.state
    prev = {}
    if os.path.exists(state_path):
        try:
            with open(state_path) as f:
                prev = json.load(f)
        except Exception:
            prev = {}
    prev_open = prev.get("open_reasons", {})
    prev_first_seen = prev.get("wire_first_seen", {})
    prev_wire_owned = prev.get("wire_owned", {})

    data = build_snapshot(fa_size=args.fa_size)
    now = time.time()
    my_team = data["my_team"] or {"roster": []}
    roster = my_team["roster"]
    starters = [p for p in roster if p.get("lineupSlot") not in ("BE", "IR")]
    bench = [p for p in roster if p.get("lineupSlot") == "BE"]

    reasons = {}  # key -> human-readable description, for everything TRUE right now

    # 1/2/3 — health status of rostered players
    for p in roster:
        pid, status, slot = p.get("playerId"), p.get("injuryStatus"), p.get("lineupSlot")
        if slot not in ("BE", "IR") and status in CONCERNING_STATUSES:
            reasons["starter_out:%s" % pid] = "STARTER %s is now %s" % (p.get("name"), status)
        elif slot not in ("BE", "IR") and status in QUESTIONABLE_STATUSES:
            reasons["starter_q:%s" % pid] = "STARTER %s is now QUESTIONABLE" % p.get("name")
        was_concerning = prev.get("player_status", {}).get(str(pid)) in (CONCERNING_STATUSES | QUESTIONABLE_STATUSES)
        if was_concerning and status not in (CONCERNING_STATUSES | QUESTIONABLE_STATUSES):
            reasons["player_cleared:%s" % pid] = "%s cleared to a healthy status (was %s)" % (
                p.get("name"), prev.get("player_status", {}).get(str(pid)))

    # 4 — any bench player now out-projecting an eligible starter
    for s in starters:
        s_proj = s.get("projected_points") or 0
        for b in bench:
            if s.get("lineupSlot") in (b.get("eligibleSlots") or []):
                b_proj = b.get("projected_points") or 0
                if b_proj > s_proj + 0.01:
                    reasons[_slot_gap_key(s.get("lineupSlot"), b.get("playerId"))] = (
                        "%s (bench, %.2f proj) now out-projects starter %s (%.2f proj) at %s" % (
                            b.get("name"), b_proj, s.get("name"), s_proj, s.get("lineupSlot")))

    # 5/6/7/13 — wire scan
    my_worst_by_pos = {}
    for p in roster:
        pos = p.get("position")
        proj = p.get("projected_points") or 0
        if pos not in my_worst_by_pos or proj < my_worst_by_pos[pos]:
            my_worst_by_pos[pos] = proj

    wire_first_seen = dict(prev_first_seen)
    wire_owned = {}
    waiver_hours = (data["acquisition_settings"] or {}).get("waiverHours") or 24
    for pos, players in data["free_agents"].items():
        for p in players:
            pid = str(p.get("playerId"))
            proj = p.get("projected_points") or 0
            owned = p.get("percent_owned") or 0
            wire_owned[pid] = owned
            if pid not in wire_first_seen:
                wire_first_seen[pid] = now

            if proj > my_worst_by_pos.get(p.get("position"), 0):
                reasons["wire_upgrade:%s" % pid] = "%s (FA/wire, %.2f proj) beats my worst %s (%.2f proj)" % (
                    p.get("name"), proj, p.get("position"), my_worst_by_pos.get(p.get("position"), 0))

            prev_status = prev.get("wire_status", {}).get(pid)
            if prev_status == "WAIVERS" and p.get("wire_status") == "FREEAGENT":
                reasons["wire_cleared:%s" % pid] = "%s confirmed cleared waivers (now FREEAGENT)" % p.get("name")
            elif p.get("wire_status") is None and prev_status is None:
                seen_for_hours = (now - wire_first_seen[pid]) / 3600.0
                if seen_for_hours >= waiver_hours:
                    reasons["wire_clear_eta:%s" % pid] = (
                        "%s has been on the wire ~%.0fh (>= %sh waiver period) — likely clear, status unconfirmed"
                        % (p.get("name"), seen_for_hours, waiver_hours))

            prev_owned = prev_wire_owned.get(pid, owned)
            if owned - prev_owned >= args.ownership_jump:
                reasons["ownership_spike:%s" % pid] = "%s ownership jumped %.1f -> %.1f%%" % (
                    p.get("name"), prev_owned, owned)

    # 8 — acquisition settings changed
    if prev.get("acquisition_settings") and prev["acquisition_settings"] != data["acquisition_settings"]:
        reasons["acq_settings_changed"] = "acquisition_settings changed: %s -> %s" % (
            prev["acquisition_settings"], data["acquisition_settings"])

    # 9/10/11 — trades
    my_team_id = data["my_team_id"]
    prev_trades = {str(t.get("id")): t for t in prev.get("pending_trades", [])}
    for tx in data["pending_trades"]:
        txid = str(tx.get("id"))
        status = tx.get("status")
        if txid not in prev_trades and tx.get("teamId") != my_team_id:
            reasons["trade_incoming:%s" % txid] = "New incoming trade proposal %s (status %s)" % (txid, status)
        prev_status = prev_trades.get(txid, {}).get("status")
        if txid in prev_trades and status != prev_status:
            reasons["trade_status:%s:%s" % (txid, status)] = "Trade %s status changed %s -> %s" % (
                txid, prev_status, status)
        exp = tx.get("expirationDate")
        if tx.get("teamId") == my_team_id and status == "PENDING" and isinstance(exp, (int, float)):
            hours_left = (exp / 1000.0 - now) / 3600.0
            if 0 < hours_left <= 6:
                reasons["trade_expiring:%s" % txid] = "My proposed trade %s expires in ~%.1fh" % (txid, hours_left)

    # 12 — new API/auth errors
    if data["errors"] and not prev.get("errors"):
        reasons["errors_appeared"] = "Snapshot errors appeared: %s" % data["errors"]

    prev_open_keys = set(prev_open.keys())
    new_keys = set(reasons.keys()) - prev_open_keys
    triggered = bool(new_keys)

    new_state = {
        "checked_at": now,
        "open_reasons": reasons,
        "player_status": {str(p.get("playerId")): p.get("injuryStatus") for p in roster},
        "wire_status": {str(p.get("playerId")): p.get("wire_status")
                        for players in data["free_agents"].values() for p in players},
        "wire_first_seen": wire_first_seen,
        "wire_owned": wire_owned,
        "acquisition_settings": data["acquisition_settings"],
        "pending_trades": data["pending_trades"],
        "errors": data["errors"],
    }
    with open(state_path, "w") as f:
        json.dump(new_state, f, indent=2, default=str)

    print("poll: %s open condition(s), %s new" % (len(reasons), len(new_keys)))
    for k in sorted(new_keys):
        print("  NEW: %s" % reasons[k])
    for k in sorted(prev_open_keys & set(reasons.keys())):
        print("  (still open, already alerted): %s" % reasons[k])

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as f:
            f.write("trigger=%s\n" % ("true" if triggered else "false"))
            f.write("reasons<<POLL_REASONS_EOF\n")
            for k in sorted(new_keys):
                f.write("- %s\n" % reasons[k])
            f.write("POLL_REASONS_EOF\n")
    return 0


# --------------------------------------------------------------------------- find
def cmd_find(args):
    lg = league()
    week = getattr(lg, "current_week", None)
    needle = args.name.lower()
    hits = []
    for t in lg.teams:
        for p in getattr(t, "roster", []):
            if needle in p.name.lower():
                hits.append({**pdump(p), "owner": t.team_name, "owner_team_id": t.team_id})
    for pos in ["QB", "RB", "WR", "TE", "D/ST", "K"]:
        try:
            for p in lg.free_agents(week=week, size=200, position=pos):
                if needle in p.name.lower():
                    hits.append({**pdump(p), "owner": "FREE AGENT", "owner_team_id": None})
        except Exception:
            pass
    print(json.dumps(hits, indent=2, default=str))
    return 0


# ---------------------------------------------------------------------- set-lineup
def cmd_set_lineup(args):
    lg = league()
    week = getattr(lg, "current_week", None)
    current = {}
    for t in lg.teams:
        if t.team_id == TID:
            for p in t.roster:
                current[p.playerId] = p.lineupSlot
    items = []
    for mv in args.move:
        pid_s, slot_s = mv.split(":")
        pid = int(pid_s)
        to_id = slot_id(slot_s)
        if to_id is None:
            print("Unknown slot %r in %r" % (slot_s, mv), file=sys.stderr)
            return 2
        from_id = slot_id(current.get(pid, "BE"))
        items.append({"playerId": pid, "type": "LINEUP",
                      "fromLineupSlotId": from_id, "toLineupSlotId": to_id})
    body = {"isLeagueManager": False, "teamId": TID, "type": "ROSTER",
            "memberId": SWID, "scoringPeriodId": week, "executionType": "EXECUTE",
            "items": items}
    return post_transaction(body, args.execute)


# ------------------------------------------------------------------------ add-drop
def cmd_add_drop(args):
    lg = league()
    week = getattr(lg, "current_week", None)
    items = [{"playerId": args.add, "type": "ADD", "toTeamId": TID}]
    if args.drop:
        items.append({"playerId": args.drop, "type": "DROP", "fromTeamId": TID})
    body = {"isLeagueManager": False, "teamId": TID, "type": "FREEAGENT",
            "memberId": SWID, "scoringPeriodId": week, "executionType": "EXECUTE",
            "items": items}
    return post_transaction(body, args.execute)


# -------------------------------------------------------------------------- waiver
def cmd_waiver(args):
    lg = league()
    week = getattr(lg, "current_week", None)
    items = [{"playerId": args.add, "type": "ADD", "toTeamId": TID}]
    if args.drop:
        items.append({"playerId": args.drop, "type": "DROP", "fromTeamId": TID})
    body = {"isLeagueManager": False, "teamId": TID, "type": "WAIVER",
            "memberId": SWID, "scoringPeriodId": week, "executionType": "EXECUTE",
            "bidAmount": args.bid, "items": items}  # bidAmount ignored in non-FAAB leagues
    return post_transaction(body, args.execute)


# ------------------------------------------------------------------- trade-propose
def cmd_trade_propose(args):
    lg = league()
    week = getattr(lg, "current_week", None)
    send = [int(x) for x in args.send.split(",") if x.strip()]
    recv = [int(x) for x in args.receive.split(",") if x.strip()]
    items = [{"playerId": p, "type": "TRADE", "fromTeamId": TID, "toTeamId": args.to_team} for p in send]
    items += [{"playerId": p, "type": "TRADE", "fromTeamId": args.to_team, "toTeamId": TID} for p in recv]
    body = {"isLeagueManager": False, "teamId": TID, "type": "TRADE_PROPOSAL",
            "memberId": SWID, "scoringPeriodId": week, "executionType": "EXECUTE",
            "items": items}
    return post_transaction(body, args.execute)


# ------------------------------------------------------------------- trade-respond
def cmd_trade_respond(args):
    lg = league()
    week = getattr(lg, "current_week", None)
    body = {"isLeagueManager": False, "teamId": TID,
            "type": "TRADE_ACCEPT" if args.accept else "TRADE_DECLINE",
            "id": args.id, "memberId": SWID, "scoringPeriodId": week,
            "executionType": "EXECUTE", "items": []}
    return post_transaction(body, args.execute)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("show"); p.add_argument("--out", default="espn-data.json"); p.add_argument("--fa-size", type=int, default=12)
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("poll", description=cmd_poll.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--state", default=".github/state/poll-state.json", help="path to persisted poll state")
    p.add_argument("--fa-size", type=int, default=60)
    p.add_argument("--ownership-jump", type=float, default=15.0,
                   help="min percent_owned increase between polls to flag a breakout (default 15.0)")
    p.set_defaults(fn=cmd_poll)

    p = sub.add_parser("find"); p.add_argument("--name", required=True); p.set_defaults(fn=cmd_find)

    p = sub.add_parser("set-lineup")
    p.add_argument("--move", action="append", default=[], required=True, help="PID:SLOT, repeatable")
    p.add_argument("--execute", action="store_true"); p.set_defaults(fn=cmd_set_lineup)

    p = sub.add_parser("add-drop")
    p.add_argument("--add", type=int, required=True); p.add_argument("--drop", type=int)
    p.add_argument("--execute", action="store_true"); p.set_defaults(fn=cmd_add_drop)

    p = sub.add_parser("waiver")
    p.add_argument("--add", type=int, required=True); p.add_argument("--drop", type=int)
    p.add_argument("--bid", type=int, default=0, help="FAAB bid; ignored in non-FAAB leagues")
    p.add_argument("--execute", action="store_true"); p.set_defaults(fn=cmd_waiver)

    p = sub.add_parser("trade-propose")
    p.add_argument("--to-team", type=int, required=True)
    p.add_argument("--send", required=True); p.add_argument("--receive", required=True)
    p.add_argument("--execute", action="store_true"); p.set_defaults(fn=cmd_trade_propose)

    p = sub.add_parser("trade-respond")
    p.add_argument("--id", type=str, required=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--accept", action="store_true"); g.add_argument("--decline", action="store_true")
    p.add_argument("--execute", action="store_true"); p.set_defaults(fn=cmd_trade_respond)

    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
