#!/usr/bin/env python3
"""Render a telemetry database as a self-contained HTML report.

    python viz.py archive/<run> [--db PATH] [--out PATH]

Writes <archive>/report.html (no network needed to view it). The overview
tracks the 8 nations with the highest peak factory count; a chart can't keep
more than 8 line colors apart, so the rest stay out of the overview but can
be picked in the per-country section.
"""

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

from extract import FACTORY_BUILDINGS

FEATURED = 8          # categorical palette size
DETAIL = 24           # countries selectable in the per-country section
CATEGORIES = 7        # stacked-area categories before folding into "Other"
FAMILIES = 12         # production / stockpile small multiples per country
RESOURCES = 8         # resource lines per chart (palette size)
WARS = 25             # rows in the wars table
FOREIGN = 15          # rows in the foreign-designs table


def date_to_t(d):
    y, m, day = (int(x) for x in d.split('.')[:3])
    return round(y + (m - 1) / 12 + (day - 1) / 365, 4)


def family(equipment):
    """infantry_equipment_3 -> infantry_equipment; ship_hull_light_2 -> ship_hull_light"""
    return re.sub(r'_\d+$', '', equipment or '?')


def series(db, sql, dates, tags):
    """{tag: [value per date]} from rows of (date, tag, value); missing -> None."""
    idx = {d: i for i, d in enumerate(dates)}
    out = {t: [None] * len(dates) for t in tags}
    for d, t, v in db.execute(sql):
        if t in out and d in idx:
            out[t][idx[d]] = v
    return out


def stacked(rows, dates, tags):
    """{tag: {category: [value per date]}} from rows of (date, tag, category, value),
    with categories folded to the global top CATEGORIES + Other."""
    idx = {d: i for i, d in enumerate(dates)}
    raw = defaultdict(lambda: defaultdict(lambda: [0] * len(dates)))
    totals = defaultdict(float)
    for d, t, cat, v in rows:
        if t in tags and d in idx:
            raw[t][cat][idx[d]] += v or 0
            totals[cat] += v or 0
    keep = sorted(totals, key=totals.get, reverse=True)[:CATEGORIES]
    out = {}
    for t, cats in raw.items():
        folded = defaultdict(lambda: [0] * len(dates))
        for cat, vals in cats.items():
            key = cat if cat in keep else 'Other'
            folded[key] = [a + b for a, b in zip(folded[key], vals)]
        out[t] = {k: [round(x, 1) for x in v] for k, v in folded.items()}
    order = keep + (['Other'] if len(totals) > len(keep) else [])
    return order, out


def families(db, dates, tags):
    """{tag: [[family, [active factories per date]], ...]} for each country's top
    FAMILIES equipment families. Drawn as small multiples, so nothing is folded."""
    idx = {d: i for i, d in enumerate(dates)}
    raw = defaultdict(lambda: defaultdict(lambda: [0] * len(dates)))
    for d, t, e, v in db.execute('SELECT date, tag, equipment, SUM(active_factories) '
                                 'FROM production_lines GROUP BY 1, 2, 3'):
        if t in tags and d in idx:
            raw[t][family(e)][idx[d]] += v or 0
    return {t: sorted(([f, v] for f, v in fams.items() if any(v)),
                      key=lambda fv: -sum(fv[1]))[:FAMILIES]
            for t, fams in raw.items()}


def casualties(db, dates, tags):
    """Cumulative losses per country, carrying each war's last-seen totals forward
    after it ends (ended wars vanish from the save)."""
    wars = defaultdict(list)
    for row in db.execute('SELECT date, first, second, start_date, first_casualties, '
                          'second_casualties FROM war_casualties ORDER BY date'):
        wars[row[1:4]].append((row[0], row[4], row[5]))
    out = {t: [0] * len(dates) for t in tags}
    for (first, second, _), rows in wars.items():
        if first not in out and second not in out:
            continue
        j, last = 0, (0, 0)
        for i, d in enumerate(dates):
            while j < len(rows) and rows[j][0] <= d:
                last = rows[j][1:]
                j += 1
            if first in out:
                out[first][i] += last[0]
            if second in out:
                out[second][i] += last[1]
    return out


def by_category(db, sql, dates, tags, prefix):
    """Flatten rows of (date, tag, category, value) into overview metrics named
    '<prefix>:<category>', ordered by the featured nations' latest total."""
    idx = {d: i for i, d in enumerate(dates)}
    out = defaultdict(lambda: {t: [None] * len(dates) for t in tags})
    for d, t, cat, v in db.execute(sql):
        if t in tags and d in idx:
            out[cat][t][idx[d]] = v
    last = {cat: sum(max((v for v in vs if v is not None), default=0) for vs in s.values())
            for cat, s in out.items()}
    order = [c for c in sorted(last, key=last.get, reverse=True) if last[c]]
    return order, {f'{prefix}:{c}': out[c] for c in order}


def industry(db, dates, tags):
    """Per country: military factories, how many are on a production line, and
    how many of those sit on lines short of resources."""
    idx = {d: i for i, d in enumerate(dates)}
    out = {t: {k: [None] * len(dates) for k in ('mil', 'assigned', 'starved')} for t in tags}
    for d, t, mil, assigned, starved in db.execute(
            'SELECT c.date, c.tag, c.mil_factories, COALESCE(SUM(l.active_factories), 0), '
            'COALESCE(SUM(CASE WHEN l.resources_missing > 0 THEN l.active_factories END), 0) '
            'FROM country c LEFT JOIN production_lines l '
            "ON l.date = c.date AND l.tag = c.tag AND l.kind = 'military' GROUP BY 1, 2"):
        if t in out and d in idx:
            for k, v in (('mil', mil), ('assigned', assigned), ('starved', starved)):
                out[t][k][idx[d]] = v
    return out


def resources(db, dates, tags):
    """{tag: {flow: {resource: [per date]}}} plus the resource order (by world output)."""
    idx = {d: i for i, d in enumerate(dates)}
    flows = ('produced', 'imported', 'exported')
    out = {}
    totals = defaultdict(float)
    for d, t, res, *vals in db.execute(f'SELECT date, tag, resource, {", ".join(flows)} FROM resources'):
        totals[res] += vals[0] or 0
        if t in tags and d in idx:
            per = out.setdefault(t, {f: {} for f in flows})
            for f, v in zip(flows, vals):
                per[f].setdefault(res, [0] * len(dates))[idx[d]] = round(v or 0, 1)
    return sorted(totals, key=totals.get, reverse=True)[:RESOURCES], out


def stockpiles(db, dates, tags):
    """{tag: [[family, [amount per date]], ...]}, the FAMILIES biggest at each
    country's last snapshot. Negative (owed) amounts are clipped to 0."""
    idx = {d: i for i, d in enumerate(dates)}
    raw = defaultdict(lambda: defaultdict(lambda: [0] * len(dates)))
    for d, t, e, v in db.execute('SELECT date, tag, equipment, SUM(amount) FROM stockpile GROUP BY 1, 2, 3'):
        if t in tags and d in idx:
            raw[t][family(e)][idx[d]] += max(0, v or 0)
    out = {}
    for t, fams in raw.items():
        last = max((i for vs in fams.values() for i, v in enumerate(vs) if v), default=0)
        out[t] = sorted(([f, [round(x) for x in v]] for f, v in fams.items() if any(v)),
                        key=lambda fv: -fv[1][last])[:FAMILIES]
    return out


def foreign(db, tags):
    """Equipment in service designed by another country, at each country's last
    snapshot: stockpile + aircraft in wings + ships. Mostly captured enemy kit,
    plus licensed and bought designs."""
    out = defaultdict(Counter)
    for t, creator, e, amount in db.execute("""
            WITH last AS (SELECT tag, MAX(date) AS date FROM country GROUP BY tag),
            held AS (
                SELECT date, tag, variant_id, amount FROM stockpile
                UNION ALL SELECT date, tag, variant_id, count FROM planes
                UNION ALL SELECT date, tag, variant_id, count FROM ships)
            SELECT h.tag, v.creator, v.equipment, SUM(h.amount)
            FROM held h JOIN last USING (tag, date) JOIN variant v USING (variant_id)
            WHERE v.creator IS NOT NULL AND v.creator != h.tag AND h.amount > 0
            GROUP BY 1, 2, 3"""):
        if t in tags:
            out[t][(creator, family(e))] += amount
    return {t: [[c, f, round(n)] for (c, f), n in rows.most_common(FOREIGN)] for t, rows in out.items()}


def governments(db, tags):
    """{tag: [[date, ruling_party], ...]}: the first snapshot and every change."""
    out = {}
    for d, t, party in db.execute('SELECT date, tag, ruling_party FROM country_stats ORDER BY date'):
        if t in tags and party and (t not in out or out[t][-1][1] != party):
            out.setdefault(t, []).append([d, party])
    return out


def wars(db):
    """The WARS bloodiest wars: final (or latest) casualties of each side."""
    last = db.execute('SELECT MAX(date) FROM snapshot').fetchone()[0]
    return [list(r) for r in db.execute(
        'SELECT first, second, start_date, date, first_casualties, second_casualties, date = ? '
        'FROM war_final ORDER BY first_casualties + second_casualties DESC LIMIT ?', (last, WARS))]


def identities(db, tags):
    """Each tag's latest original/cosmetic tag. Dynamic tags (D01..) only mean
    something next to the country they split from: 'D05 (SPA)'."""
    latest = {t: (o, c) for t, o, c in
              db.execute('SELECT tag, original_tag, cosmetic_tag FROM country_tag ORDER BY date')}
    labels, names = {}, {}
    for t in tags:
        orig, cosmetic = latest.get(t, (None, None))
        labels[t] = f'{t} ({orig})' if orig else t
        if cosmetic:
            names[t] = cosmetic.replace('_', ' ')
    return labels, names


def build(db, run):
    dates = [r[0] for r in db.execute('SELECT date FROM snapshot ORDER BY date')]
    peak = db.execute('SELECT tag, MAX(civ_factories + mil_factories + naval_factories) p '
                      'FROM country GROUP BY tag ORDER BY p DESC').fetchall()
    featured = [t for t, _ in peak[:FEATURED]]
    detail = [t for t, _ in peak[:DETAIL]]
    war_rows = wars(db)
    labels, names = identities(db, set(detail) | {t for w in war_rows for t in w[:2]})
    factory_list = ', '.join(f"'{b}'" for b in FACTORY_BUILDINGS.values())
    produced_order, produced = by_category(
        db, 'SELECT date, tag, category, total FROM produced_total', dates, featured, 'produced')
    constructed_order, constructed = by_category(
        db, 'SELECT date, tag, building, total FROM constructed_total', dates, featured, 'constructed')

    overview = {
        'factories': series(db, 'SELECT date, tag, civ_factories + mil_factories + naval_factories FROM country', dates, featured),
        'civ_factories': series(db, 'SELECT date, tag, civ_factories FROM country', dates, featured),
        'mil_factories': series(db, 'SELECT date, tag, mil_factories FROM country', dates, featured),
        'naval_factories': series(db, 'SELECT date, tag, naval_factories FROM country', dates, featured),
        'divisions': series(db, 'SELECT date, tag, divisions FROM country', dates, featured),
        'manpower': series(db, 'SELECT date, tag, CAST(manpower AS INT) FROM country', dates, featured),
        'planes': series(db, 'SELECT date, tag, CAST(SUM(count) AS INT) FROM planes GROUP BY 1, 2', dates, featured),
        'ships': series(db, 'SELECT date, tag, SUM(count) FROM ships GROUP BY 1, 2', dates, featured),
        'casualties': casualties(db, dates, featured),
        'idle_mil': series(db, 'SELECT c.date, c.tag, MAX(0, c.mil_factories - COALESCE(SUM(l.active_factories), 0)) '
                               'FROM country c LEFT JOIN production_lines l ON l.date = c.date AND l.tag = c.tag '
                               "AND l.kind = 'military' GROUP BY 1, 2", dates, featured),
        'damaged': series(db, 'SELECT date, tag, SUM(level - healthy) FROM buildings '
                              f'WHERE building IN ({factory_list}) GROUP BY 1, 2', dates, featured),
        'stability': series(db, 'SELECT date, tag, ROUND(stability * 100) FROM country_stats', dates, featured),
        'war_support': series(db, 'SELECT date, tag, ROUND(war_support * 100) FROM country_stats', dates, featured),
        'techs': series(db, 'SELECT date, tag, techs FROM country_stats', dates, featured),
        'fuel': series(db, 'SELECT date, tag, CAST(fuel AS INT) FROM country_stats', dates, featured),
        'convoys_destroyed': series(db, 'SELECT date, tag, convoys_destroyed FROM country_stats', dates, featured),
        **produced, **constructed,
    }

    tags = set(detail)
    plane_order, planes = stacked(db.execute('SELECT date, tag, role, SUM(count) FROM planes GROUP BY 1, 2, 3'),
                                  dates, tags)
    ship_order, ships = stacked(db.execute('SELECT date, tag, role, SUM(count) FROM ships GROUP BY 1, 2, 3'),
                                dates, tags)
    alloc_order, alloc = stacked(((d, t, family(e), v) for d, t, e, v in db.execute(
        "SELECT date, tag, equipment, active_factories FROM production_lines WHERE kind = 'military'")),
        dates, tags)
    resource_order, resource_flows = resources(db, dates, tags)
    return {
        'run': run, 'dates': dates, 't': [date_to_t(d) for d in dates],
        'featured': featured, 'detail': detail, 'labels': labels, 'names': names,
        'overview': overview,
        'produced_categories': produced_order, 'constructed_categories': constructed_order,
        'production': families(db, dates, tags),
        'stacks': {
            'allocation': {'order': alloc_order, 'data': alloc},
            'planes': {'order': plane_order, 'data': planes},
            'ships': {'order': ship_order, 'data': ships},
        },
        'industry': industry(db, dates, tags),
        'resources': {'order': resource_order, 'data': resource_flows},
        'stockpile': stockpiles(db, dates, tags),
        'foreign': foreign(db, tags),
        'government': governments(db, tags | set(featured)),
        'wars': war_rows,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('archive', type=Path)
    ap.add_argument('--db', type=Path, help='default: <archive>/telemetry.sqlite')
    ap.add_argument('--out', type=Path, help='default: <archive>/report.html')
    args = ap.parse_args()
    db_path = args.db or args.archive / 'telemetry.sqlite'
    if not db_path.exists():
        sys.exit(f'{db_path} not found: run extract.py {args.archive} first')
    db = sqlite3.connect(db_path)
    if not db.execute('SELECT 1 FROM snapshot LIMIT 1').fetchone():
        sys.exit(f'{db_path} has no snapshots yet')
    data = build(db, args.archive.name)
    out = args.out or args.archive / 'report.html'
    template = (Path(__file__).parent / 'report_template.html').read_text(encoding='utf-8')
    # </ inside JSON would end the script tag early
    payload = json.dumps(data, separators=(',', ':')).replace('</', '<\\/')
    out.write_text(template.replace('/*DATA*/null', payload), encoding='utf-8')
    print(f'{out} ({out.stat().st_size / 1e3:.0f} kB): {len(data["dates"])} snapshots, '
          f'featured {", ".join(data["featured"])}')


if __name__ == '__main__':
    main()
