#!/usr/bin/env python3
"""Pace-räknare för ESPN Fantasy (Swedish Crosscheckers i hockey, Swedish Crossovers i basket).

Logik enligt Pacecounter.xlsx, per lag och position:
  Kvar att spela  = summa kvarvarande matcher för nuvarande startare
  Borde ha kvar   = matchgräns - spelade enligt fantasy
  Före pace       = Kvar att spela - Borde ha kvar
                    (>0: startarna slår i taket så många matcher för tidigt,
                     <0: matcher blir över även om alla startare spelar allt)
  Möjlig          = bänkens kvarvarande matcher + Före pace
"""
import datetime as dt
import html
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output"
HISTORY = ROOT / "history"
SEASON = 2027

# positions: etikett -> (slot med matchgräns, startplatser, slot bänkspelare måste kunna spela, eller None)
LEAGUES = {
    "hockey": {
        "game": "fhl", "sport": "hockey/nhl", "pro": "NHL", "league_id": 8143, "season_games": 84,
        "config": "config.json", "env": "", "path": "", "gp_stat": 30, "bench": 7, "ir": 8,
        "positions": {"F": (3, [3], 3), "D": (4, [4], 4), "UTIL": (6, [6], 6), "G": (5, [5], 5)},
    },
    "basket": {
        "game": "fba", "sport": "basketball/nba", "pro": "NBA", "league_id": 11568, "season_games": 82,
        "config": "config_fba.json", "env": "_FBA", "path": "basket", "gp_stat": 42, "bench": 12, "ir": 13,
        "positions": {"Totalt": (15, [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11], None)},
    },
}


def credentials(lg):
    # I molnet (GitHub Actions) kommer cookies från secrets via miljövariabler
    f = ROOT / lg["config"]
    cfg = json.loads(f.read_text()) if f.exists() else {}
    return {"espn_s2": os.environ.get("ESPN_S2" + lg["env"], cfg.get("espn_s2", "")),
            "SWID": os.environ.get("SWID" + lg["env"], cfg.get("swid", ""))}


def fantasy(lg, *views):
    url = (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/{lg['game']}/seasons/"
           f"{SEASON}/segments/0/leagues/{lg['league_id']}")
    r = requests.get(url, params=[("view", v) for v in views], cookies=credentials(lg), timeout=30)
    r.raise_for_status()
    return r.json()


def pro_teams(lg):
    base = f"https://site.api.espn.com/apis/site/v2/sports/{lg['sport']}"
    data = requests.get(f"{base}/teams", timeout=30).json()
    teams = {int(t["team"]["id"]): t["team"]["abbreviation"]
             for t in data["sports"][0]["leagues"][0]["teams"]}

    def remaining(tid):
        events = requests.get(f"{base}/teams/{tid}/schedule",
                              params={"season": SEASON, "seasontype": 2}, timeout=30).json()["events"]
        # Pågående matcher räknas som spelade, precis som i fantasy. Kvar räknas mot hela
        # säsongen eftersom NBA inte schemalägger de sista Cup-matcherna förrän i december.
        played = sum(e["competitions"][0]["status"]["type"]["state"] != "pre" for e in events)
        return tid, {"played": played, "left": lg["season_games"] - played}

    with ThreadPoolExecutor(8) as ex:
        sched = dict(ex.map(remaining, teams))
    return {tid: {"abbr": teams[tid], **sched[tid]} for tid in teams}


def build(lg):
    league = fantasy(lg, "mTeam", "mSettings", "mRoster", "mMatchupScore")
    pro = pro_teams(lg)
    limits = {int(k): v["limitValue"] for k, v in
              league["settings"]["rosterSettings"]["lineupSlotStatLimits"].items()}
    counts = {int(k): v for k, v in league["settings"]["rosterSettings"]["lineupSlotCounts"].items()}

    used = {}
    for m in league["schedule"]:
        # Rotisserie-schema har lagen i "teams"-listan, H2H i "home"/"away"
        for t in m.get("teams", []) + [m[s] for s in ("home", "away") if s in m]:
            by_slot = t.get("cumulativeScoreLive", t.get("cumulativeScore", {})).get("statBySlot") or {}
            used[t["teamId"]] = {int(k): v["value"] for k, v in by_slot.items() if v["statId"] == lg["gp_stat"]}

    teams = []
    for t in league["teams"]:
        players = []
        for e in t["roster"]["entries"]:
            p = e["playerPoolEntry"]["player"]
            pt = pro.get(p.get("proTeamId"), {"abbr": "FA", "left": 0, "played": 0})
            players.append({
                "name": p["fullName"], "slot": e["lineupSlotId"], "eligible": p["eligibleSlots"],
                "team": pt["abbr"], "played": pt["played"], "left": pt["left"],
                "injury": p.get("injuryStatus") if p.get("injured") or p.get("injuryStatus") not in (None, "ACTIVE") else None,
            })
        positions = {}
        for label, (limit_slot, starter_slots, bench_slot) in lg["positions"].items():
            starters = [p for p in players if p["slot"] in starter_slots]
            bench = [p for p in players if p["slot"] == lg["bench"]
                     and (bench_slot is None or bench_slot in p["eligible"])]
            limit = limits.get(limit_slot, 0)
            gp = used.get(t["id"], {}).get(limit_slot, 0)
            left_starters = sum(p["left"] for p in starters)
            ahead = left_starters - (limit - gp)
            positions[label] = {
                "slots": sum(counts.get(s, 0) for s in starter_slots), "limit": limit, "used": gp,
                "should_have_left": limit - gp, "starters_left": left_starters,
                "starters_played": sum(p["played"] for p in starters),
                "ahead": ahead, "bench_left": sum(p["left"] for p in bench),
                "possible": sum(p["left"] for p in bench) + ahead,
                "starters": starters, "bench": bench,
            }
        teams.append({"id": t["id"], "name": t["name"].strip(), "abbrev": t["abbrev"],
                      "points": t.get("points", 0), "positions": positions,
                      "ir": [p for p in players if p["slot"] == lg["ir"]]})

    teams.sort(key=lambda x: -x["points"])
    avg_played = sum(v["played"] for v in pro.values()) / len(pro)
    return {"league": league["settings"]["name"], "pro": lg["pro"],
            "generated": dt.datetime.now().isoformat(timespec="minutes"),
            "avg_played": avg_played, "season_len": lg["season_games"], "teams": teams}


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


def render(data, nav):
    pos_order = list(data["teams"][0]["positions"])
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
    <dt>Startarnas lag har spelat</dt><dd>{x["starters_played"]}</dd>
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
nav{{margin-bottom:16px;font-size:13px}} nav a{{margin-right:12px}} nav b{{margin-right:12px}}
</style></head><body>
<nav>{nav}</nav>
<h1>{html.escape(data["league"])} – pace</h1>
<p class="muted">Uppdaterad {data["generated"].replace("T", " ")} · {data["pro"]}-lagen har i snitt spelat {data["avg_played"]:.1f} av {data["season_len"]} matcher ({pct:.0f} % av säsongen)</p>
<p class="legend"><b>Före pace</b> = kvarvarande matcher för nuvarande startare − (gräns − spelade).
<span class="neg">Positivt</span>: startarna slår i taket så många matcher för tidigt. <span class="pos">Negativt</span>: så många matcher blir över även om alla startare spelar resten.
<b>Möjlig</b> = bänkens kvarvarande matcher + före pace; negativt betyder att bänken inte räcker för att täcka upp.</p>
<div class="wrap"><table><thead><tr><th></th><th>Lag</th><th class="num">Poäng</th>{head}</tr>
<tr><th></th><th></th><th></th>{sub}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>
{"".join(details)}
</body></html>'''


def main():
    failed = []
    for key, lg in LEAGUES.items():
        try:
            data = build(lg)
        except Exception as e:
            print(f"FEL {key}: {e}", file=sys.stderr)
            failed.append(key)
            continue
        nav = " ".join(
            f'<b>{other["pro"]}</b>' if k == key else
            f'<a href="{"../" * bool(lg["path"])}{other["path"] + "/" if other["path"] else ""}">{other["pro"]}</a>'
            for k, other in LEAGUES.items())
        out = OUT / lg["path"]
        out.mkdir(parents=True, exist_ok=True)
        (out / "index.html").write_text(render(data, nav))
        (out / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1))
        HISTORY.mkdir(exist_ok=True)
        (HISTORY / f"{dt.date.today()}-{key}.json").write_text(json.dumps(data, ensure_ascii=False))
        print(f"OK {key} {data['generated']} -> {out / 'index.html'}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
