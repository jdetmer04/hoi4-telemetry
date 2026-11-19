#!/usr/bin/env python3
"""Compare one country across several runs in a single self-contained HTML page.

    python compare.py [--tag GER] [--until 1943.06] [--out compare.html] RUN [RUN ...]

Each RUN is a directory under archive/ (or a path) with a telemetry.sqlite.
Runs are drawn on a shared in-game-date axis, clipped to --until so a long run
is not compared against a short one past the shorter run's end.
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from viz import date_to_t

ARCHIVE = Path(__file__).parent / 'archive'

# (key, title, note, sql returning (date, value) for :tag)
METRICS = [
    ('civ', 'Civilian factories', '', "SELECT date, civ_factories FROM country WHERE tag=:tag"),
    ('mil', 'Military factories', '', "SELECT date, mil_factories FROM country WHERE tag=:tag"),
    ('naval', 'Naval factories (dockyards)', '', "SELECT date, naval_factories FROM country WHERE tag=:tag"),
    ('total', 'Total factories', 'civilian + military + naval',
     "SELECT date, civ_factories + mil_factories + naval_factories FROM country WHERE tag=:tag"),
    ('manpower', 'Manpower pool', 'available, summed over controlled states',
     "SELECT date, manpower FROM country WHERE tag=:tag"),
    ('divisions', 'Divisions', '', "SELECT date, divisions FROM country WHERE tag=:tag"),
    ('techs', 'Technologies researched', '', "SELECT date, techs FROM country_stats WHERE tag=:tag"),
    ('stability', 'Stability', '', "SELECT date, stability FROM country_stats WHERE tag=:tag"),
    ('war_support', 'War support', '', "SELECT date, war_support FROM country_stats WHERE tag=:tag"),
    ('pp', 'Political power', 'unspent', "SELECT date, political_power FROM country_stats WHERE tag=:tag"),
    ('convoys', 'Convoys', '', "SELECT date, convoys FROM country_stats WHERE tag=:tag"),
    ('fuel', 'Fuel stockpile', '', "SELECT date, fuel FROM country_stats WHERE tag=:tag"),
    ('infra', 'Infrastructure levels built', 'cumulative',
     "SELECT date, total FROM constructed_total WHERE tag=:tag AND building='infrastructure'"),
    ('sunk', 'Convoys sunk', 'the save\'s own counter of convoys this nation sank',
     "SELECT date, convoys_destroyed FROM country_stats WHERE tag=:tag"),
    ('ships', 'Ships in service', '', "SELECT date, SUM(count) FROM ships WHERE tag=:tag GROUP BY date"),
    ('subs', 'Submarines', '', "SELECT date, SUM(count) FROM ships WHERE tag=:tag AND role='submarine' GROUP BY date"),
    ('surface', 'Surface warships', 'destroyers, cruisers, battleships, carriers',
     "SELECT date, SUM(count) FROM ships WHERE tag=:tag AND role!='submarine' GROUP BY date"),
    ('fighters', 'Fighters', '', "SELECT date, SUM(count) FROM planes WHERE tag=:tag AND role IN ('fighter','heavy_fighter') GROUP BY date"),
    ('bombers', 'Bombers, CAS and naval bombers', '',
     "SELECT date, SUM(count) FROM planes WHERE tag=:tag AND role IN ('cas','tac_bomber','nav_bomber','strategic_bomber') GROUP BY date"),
    ('planes', 'Aircraft in service', '', "SELECT date, SUM(count) FROM planes WHERE tag=:tag GROUP BY date"),
]
CASUALTIES = [
    ('cas_lost', 'Casualties suffered', 'cumulative, wars Germany fights directly'),
    ('cas_dealt', 'Casualties inflicted', 'cumulative, wars Germany fights directly'),
    ('cas_ratio', 'Casualty ratio', 'enemy losses per German loss'),
]
# cumulative equipment produced, one chart per category group
PRODUCED = [
    ('p_inf', 'Infantry equipment produced', 'cumulative', ('infantry',)),
    ('p_armor', 'Tanks produced', 'cumulative; armor and mechanized', ('armor', 'mechanized')),
    ('p_motor', 'Trucks produced', 'cumulative; motorized, mostly used for supply', ('motorized',)),
    ('p_art', 'Artillery + AT + AA produced', 'cumulative', ('artillery', 'anti_tank', 'anti_air')),
    ('p_air', 'Aircraft produced', 'cumulative', ('fighter', 'heavy_fighter', 'cas', 'tactical_bomber', 'strategic_bomber', 'naval_bomber')),
    ('p_navy', 'Ships + convoys produced', 'cumulative',
     ('convoy', 'submarine', 'screen_ship', 'capital_ship', 'carrier')),
]


def load(run, tag, until):
    path = Path(run)
    if not (path / 'telemetry.sqlite').exists():
        path = ARCHIVE / run
    db = sqlite3.connect(path / 'telemetry.sqlite')
    dates = [d for (d,) in db.execute("SELECT date FROM snapshot ORDER BY date")
             if d[:7] <= until]
    t = {d: date_to_t(d) for d in dates}
    out = {'name': path.name, 't': [t[d] for d in dates], 'm': {}}

    def put(key, rows):
        vals = {d: v for d, v in rows}
        out['m'][key] = [vals.get(d) for d in dates]

    for key, _, _, sql in METRICS:
        put(key, db.execute(sql, {'tag': tag}))
    # cumulative casualties: wars vanish from the save when they end, so carry each
    # war pair's running maximum forward instead of reading the latest row
    peak = {}
    lost, dealt = {}, {}
    rows = db.execute("SELECT date, first, second, start_date, first_casualties, second_casualties "
                      "FROM war_casualties WHERE first=? OR second=? ORDER BY date", (tag, tag)).fetchall()
    by_date = {}
    for d, f, sec, st, fc, sc in rows:
        mine, theirs = (fc, sc) if f == tag else (sc, fc)
        by_date.setdefault(d, []).append(((f, sec, st), mine, theirs))
    for d in dates:
        for k, mine, theirs in by_date.get(d, []):
            a, b = peak.get(k, (0, 0))
            peak[k] = (max(a, mine), max(b, theirs))
        lost[d] = sum(v[0] for v in peak.values())
        dealt[d] = sum(v[1] for v in peak.values())
    out['m']['cas_lost'] = [lost[d] for d in dates]
    out['m']['cas_dealt'] = [dealt[d] for d in dates]
    out['m']['cas_ratio'] = [dealt[d] / lost[d] if lost[d] > 1000 else None for d in dates]
    for key, _, _, cats in PRODUCED:
        q = ("SELECT date, SUM(total) FROM produced_total WHERE tag=? AND category IN (%s) GROUP BY date"
             % ','.join('?' * len(cats)))
        put(key, db.execute(q, (tag, *cats)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('runs', nargs='+')
    ap.add_argument('--tag', default='GER')
    ap.add_argument('--until', default='1944.10', help='last YYYY.MM to include')
    ap.add_argument('--out', default='compare.html')
    a = ap.parse_args()

    runs = [load(r, a.tag, a.until) for r in a.runs]
    charts = [(k, t, n) for k, t, n, *_ in METRICS] + CASUALTIES + [(k, t, n) for k, t, n, _ in PRODUCED]
    data = {'tag': a.tag, 'until': a.until, 'runs': runs, 'charts': charts}
    tpl = (Path(__file__).parent / 'compare_template.html').read_text(encoding='utf-8')
    out = Path(a.out)
    out.write_text(tpl.replace('/*DATA*/null', json.dumps(data, separators=(',', ':'))), encoding='utf-8')
    print(f"{out} ({out.stat().st_size // 1024} kB): {a.tag} in {', '.join(r['name'] for r in runs)}")


if __name__ == '__main__':
    sys.exit(main())
