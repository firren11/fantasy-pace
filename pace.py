#!/usr/bin/env python3
"""Pace-räknare för ESPN Fantasy Hockey (Swedish Crosscheckers).

Logik enligt Pacecounter.xlsx, per lag och position:
  Kvar att spela  = summa kvarvarande NHL-matcher för nuvarande startare
  Borde ha kvar   = matchgräns - spelade enligt fantasy
  Före pace       = Kvar att spela - Borde ha kvar
                    (>0: startarna slår i taket så många matcher för tidigt,
                     <0: matcher blir över även om alla startare spelar allt)
  Möjlig          = bänkens kvarvarande matcher + Före pace
"""
import datetime as dt
import html
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
import os
_cfg_file = ROOT / "config.json"
CONFIG = json.loads(_cfg_file.read_text()) if _cfg_file.exists() else {}
# I molnet (GitHub Actions) kommer inställningarna från miljövariabler/secrets
CONFIG = {
    "league_id": int(os.environ.get("LEAGUE_ID", CONFIG.get("league_id", 8143))),
    "season": int(os.environ.get("SEASON", CONFIG.get("season", 2027))),
    "espn_s2": os.environ.get("ESPN_S2", CONFIG.get("espn_s2", "")),
    "swid": os.environ.get("SWID", CONFIG.get("swid", "")),
}
OUT = ROOT / "output"
HISTORY = ROOT / "history"

FANTASY_URL = ("https://lm-api-reads.fantasy.espn.com/apis/v3/games/fhl/seasons/"
               f"{CONFIG['season']}/segments/0/leagues/{CONFIG['league_id']}")
NHL = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl"
NHL_STANDINGS = "https://site.api.espn.com/apis/v2/sports/hockey/nhl/standings"

# ESPN lineupSlotId
SLOTS = {3: "F", 4: "D", 6: "UTIL", 5: "G"}
BENCH, IR = 7, 8
GP_STAT = 30


def fantasy(*views):
    r = requests.get(FANTASY_URL, params=[("view", v) for v in views],
                     cookies={"espn_s2": CONFIG["espn_s2"], "SWID": CONFIG["swid"]}, timeout=30)
    r.raise_for_status()
    return r.json()


def nhl_teams():
    data = requests.get(f"{NHL}/teams", timeout=30).json()
    teams = {int(t["team"]["id"]): t["team"]["abbreviation"]
             for t in data["sports"][0]["leagues"][0]["teams"]}

    def remaining(tid):
        events = requests.get(f"{NHL}/teams/{tid}/schedule",
                              params={"season": CONFIG["season"], "seasontype": 2}, timeout=30).json()["events"]
        states = [e["competitions"][0]["status"]["type"]["state"] for e in events]
        return tid, {"total": len(states), "left": states.count("pre"),
                     "played": len(states) - states.count("pre")}

    with ThreadPoolExecutor(8) as ex:
        sched = dict(ex.map(remaining, teams))
    return {tid: {"abbr": teams[tid], **sched[tid]} for tid in teams}


def build():
    league = fantasy("mTeam", "mSettings", "mRoster", "mMatchupScore")
    nhl = nhl_teams()
    limits = {int(k): v["limitValue"] for k, v in
              league["settings"]["rosterSettings"]["lineupSlotStatLimits"].items()}
    counts = {int(k): v for k, v in league["settings"]["rosterSettings"]["lineupSlotCounts"].items()}

    used = {}
    for m in league["schedule"]:
        for side in ("home", "away"):
            if side in m:
                t = m[side]
                by_slot = t.get("cumulativeScoreLive", t.get("cumulativeScore", {})).get("statBySlot") or {}
                used[t["teamId"]] = {int(k): v["value"] for k, v in by_slot.items() if v["statId"] == GP_STAT}
        # Rotisserie-schema har lagen i "teams"-listan
        for t in m.get("teams", []):
            by_slot = t.get("cumulativeScoreLive", t.get("cumulativeScore", {})).get("statBySlot") or {}
            used[t["teamId"]] = {int(k): v["value"] for k, v in by_slot.items() if v["statId"] == GP_STAT}

    teams = []
    for t in league["teams"]:
        players = []
        for e in t["roster"]["entries"]:
            p = e["playerPoolEntry"]["player"]
            pro = nhl.get(p.get("proTeamId"), {"abbr": "FA", "left": 0, "played": 0})
            players.append({
                "name": p["fullName"], "slot": e["lineupSlotId"], "eligible": p["eligibleSlots"],
                "team": pro["abbr"], "played": pro["played"], "left": pro["left"],
                "injury": p.get("injuryStatus") if p.get("injured") or p.get("injuryStatus") not in (None, "ACTIVE") else None,
            })
        positions = {}
        for slot, label in SLOTS.items():
            starters = [p for p in players if p["slot"] == slot]
            bench = [p for p in players if p["slot"] == BENCH and slot in p["eligible"]]
            limit = limits.get(slot, 0)
            gp = used.get(t["id"], {}).get(slot, 0)
            left_starters = sum(p["left"] for p in starters)
            ahead = left_starters - (limit - gp)
            positions[label] = {
                "slots": counts.get(slot, 0), "limit": limit, "used": gp,
                "should_have_left": limit - gp, "starters_left": left_starters,
                "ahead": ahead, "bench_left": sum(p["left"] for p in bench),
                "possible": sum(p["left"] for p in bench) + ahead,
                "starters": starters, "bench": bench,
            }
        teams.append({"id": t["id"], "name": t["name"].strip(), "abbrev": t["abbrev"],
                      "points": t.get("points", 0), "positions": positions,
                      "ir": [p for p in players if p["slot"] == IR]})

    teams.sort(key=lambda x: -x["points"])
    avg_played = sum(v["played"] for v in nhl.values()) / len(nhl)
    season_len = max(v["total"] for v in nhl.values())
    return {"league": league["settings"]["name"], "generated": dt.datetime.now().isoformat(timespec="minutes"),
            "avg_played": avg_played, "season_len": season_len, "teams": teams}


# ---------- HTML ----------

def cls(v):
    return "neg" if v < 0 else "pos" if v > 0 else "zero"


def fmt(v):
    v = int(round(v))
    return f"+{v}" if v > 0 else str(v)


def player_rows(ps):
    if not ps:
        return '<tr><td colspan="4" class="muted">–</td></tr>'
    out = []
    for p in sorted(ps, key=lambda p: -p["left"]):
        label = {"INJURY_RESERVE": "IR", "DAY_TO_DAY": "DTD"}.get(p["injury"], p["injury"])
        inj = f' <span class="inj">{html.escape(label)}</span>' if p["injury"] else ""
        out.append(f'<tr><td>{html.escape(p["name"])}{inj}</td><td>{p["team"]}</td>'
                   f'<td class="num">{p["played"]}</td><td class="num">{p["left"]}</td></tr>')
    return "".join(out)


def render(data):
    pos_order = list(SLOTS.values())
    head = "".join(f'<th colspan="3" class="grp">{p}</th>' for p in pos_order)
    sub = "".join('<th class="num">Spelade</th><th class="num">Före pace</th><th class="num">Möjlig</th>'
                  for _ in pos_order)
    rows = []
    for i, t in enumerate(data["teams"], 1):
        cells = ""
        for p in pos_order:
            x = t["positions"][p]
            cells += (f'<td class="num">{int(x["used"])}<span class="muted">/{int(x["limit"])}</span></td>'
                      f'<td class="num {cls(-x["ahead"])}">{fmt(x["ahead"])}</td>'
                      f'<td class="num {cls(x["possible"])}">{fmt(x["possible"])}</td>')
        rows.append(f'<tr><td class="num muted">{i}</td><td><a href="#t{t["id"]}">{html.escape(t["name"])}</a></td>'
                    f'<td class="num">{t["points"]:g}</td>{cells}</tr>')

    details = []
    for t in data["teams"]:
        blocks = []
        for p in pos_order:
            x = t["positions"][p]
            blocks.append(f'''
<div class="card">
  <h4>{p} <span class="muted">({x["slots"]} platser · gräns {int(x["limit"])})</span></h4>
  <dl>
    <dt>Spelade enligt fantasy</dt><dd>{int(x["used"])}</dd>
    <dt>Borde ha kvar</dt><dd>{int(x["should_have_left"])}</dd>
    <dt>Kvar att spela (startare)</dt><dd>{x["starters_left"]}</dd>
    <dt>Före pace</dt><dd class="{cls(-x["ahead"])}">{fmt(x["ahead"])}</dd>
    <dt>Bänken kvar</dt><dd>{x["bench_left"]}</dd>
    <dt>Möjlig</dt><dd class="{cls(x["possible"])}">{fmt(x["possible"])}</dd>
  </dl>
  <table class="mini"><thead><tr><th>Startare</th><th>Lag</th><th class="num">Sp</th><th class="num">Kvar</th></tr></thead>
  <tbody>{player_rows(x["starters"])}</tbody></table>
  <table class="mini"><thead><tr><th>Bänk</th><th>Lag</th><th class="num">Sp</th><th class="num">Kvar</th></tr></thead>
  <tbody>{player_rows(x["bench"])}</tbody></table>
</div>''')
        ir = ", ".join(html.escape(p["name"]) for p in t["ir"]) or "–"
        details.append(f'<section id="t{t["id"]}"><h3>{html.escape(t["name"])} <span class="muted">{html.escape(t["abbrev"])}</span></h3>'
                       f'<div class="grid">{"".join(blocks)}</div><p class="muted">IR: {ir}</p></section>')

    pct = data["avg_played"] / data["season_len"] * 100
    return f'''<!doctype html><html lang="sv"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Fantasy Pace</title>
<style>
:root{{--bg:#f7f7f5;--fg:#1d1d1b;--muted:#77756f;--card:#fff;--line:#e4e2dc;--pos:#b4520a;--neg:#1f6fb2;--zero:#2e7d32;--inj:#c62828}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151514;--fg:#ecebe7;--muted:#9a9890;--card:#1f1f1d;--line:#33322f;--pos:#f0a35e;--neg:#7fb6e8;--zero:#7cc47f;--inj:#ef7b7b}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0;padding:24px 16px;max-width:1200px;margin:auto}}
h1{{font-size:22px;margin:0 0 4px}} h3{{margin:32px 0 8px}} h4{{margin:0 0 8px;font-size:14px}}
a{{color:inherit}} .muted{{color:var(--muted)}} .num{{text-align:right;font-variant-numeric:tabular-nums}}
.pos{{color:var(--pos);font-weight:600}} .neg{{color:var(--neg);font-weight:600}} .zero{{color:var(--zero);font-weight:600}}
.wrap{{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:8px}}
table{{border-collapse:collapse;width:100%}} th,td{{padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}}
th{{font-weight:600;font-size:12px;color:var(--muted);text-align:left}} th.grp{{text-align:center;border-left:1px solid var(--line)}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px}}
dl{{display:grid;grid-template-columns:1fr auto;gap:2px 12px;margin:0 0 10px}} dt{{color:var(--muted)}} dd{{margin:0;text-align:right;font-variant-numeric:tabular-nums}}
.mini td,.mini th{{padding:3px 4px;font-size:12px}} .mini{{margin-top:6px}}
.inj{{color:var(--inj);font-size:11px;font-weight:600}}
.legend{{margin:12px 0 16px;color:var(--muted);font-size:13px}}
</style></head><body>
<h1>{html.escape(data["league"])} – pace</h1>
<p class="muted">Uppdaterad {data["generated"].replace("T", " ")} · NHL-lagen har i snitt spelat {data["avg_played"]:.1f} av {data["season_len"]} matcher ({pct:.0f} % av säsongen)</p>
<p class="legend"><b>Före pace</b> = kvarvarande matcher för nuvarande startare − (gräns − spelade).
<span class="neg">Positivt</span>: startarna slår i taket så många matcher för tidigt. <span class="pos">Negativt</span>: så många matcher blir över även om alla startare spelar resten.
<b>Möjlig</b> = bänkens kvarvarande matcher + före pace; negativt betyder att bänken inte räcker för att täcka upp.</p>
<div class="wrap"><table><thead><tr><th></th><th>Lag</th><th class="num">Poäng</th>{head}</tr>
<tr><th></th><th></th><th></th>{sub}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>
{"".join(details)}
</body></html>'''


def main():
    data = build()
    OUT.mkdir(exist_ok=True)
    HISTORY.mkdir(exist_ok=True)
    (OUT / "index.html").write_text(render(data))
    (OUT / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1))
    (HISTORY / f"{dt.date.today()}.json").write_text(json.dumps(data, ensure_ascii=False))
    print(f"OK {data['generated']} -> {OUT / 'index.html'}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FEL: {e}", file=sys.stderr)
        raise
