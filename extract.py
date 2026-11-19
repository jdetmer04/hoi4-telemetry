#!/usr/bin/env python3
"""Parse every snapshot in an archive folder into a SQLite database.

    python extract.py archive/<run> [--db PATH] [-j N] [--rebuild]

Incremental: dates already in the `snapshot` table are skipped, so it can be
re-run while watch.py is still adding files. Each snapshot is committed in one
transaction, with its `snapshot` row last, so an interrupted run never leaves
a half-loaded date behind.

Nothing here names a country tag or an equipment type. The only game-specific
strings are the three factory building names, which are engine built-ins
shared by vanilla and the big overhaul mods; every building type is also kept
raw in the `buildings` table.
"""

import argparse
import gzip
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import pdxparse as p

FACTORY_BUILDINGS = {
    'civ_factories': 'industrial_complex',
    'mil_factories': 'arms_factory',
    'naval_factories': 'dockyard',
}
COUNTRY_SECTIONS = {'units', 'production', 'politics', 'technology', 'fuel_status',
                    'country_reports', 'convoys'}
RESOURCE_FLOWS = ('produced', 'imported', 'exported')    # sub-blocks of the country's `resources`

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshot (
    date TEXT PRIMARY KEY,          -- zero-padded in-game date, sorts as text
    game_id TEXT, version TEXT, file TEXT
);
CREATE TABLE IF NOT EXISTS country (
    date TEXT, tag TEXT,
    civ_factories REAL, mil_factories REAL, naval_factories REAL,
    manpower REAL,                  -- sum of manpower_pool.available over controlled states
    divisions INTEGER,
    PRIMARY KEY (date, tag)
);
CREATE TABLE IF NOT EXISTS country_tag (
    date TEXT, tag TEXT,
    original_tag TEXT,              -- set on dynamic countries (civil-war sides etc.): D05 -> SPA
    cosmetic_tag TEXT,              -- government-specific name key, e.g. SPA_carlists_kingdom
    PRIMARY KEY (date, tag)
);
CREATE TABLE IF NOT EXISTS country_stats (
    date TEXT, tag TEXT,
    stability REAL, war_support REAL,   -- 0..1
    political_power REAL, command_power REAL,
    ruling_party TEXT,              -- ideology group key: fascism / paternal_autocrat / ...
    techs INTEGER,                  -- technologies researched
    fuel REAL, max_fuel REAL,
    convoys REAL,                   -- convoys in the convoy pool
    convoys_destroyed INTEGER,      -- convoys this country has sunk (the save's own counter)
    PRIMARY KEY (date, tag)
);
CREATE TABLE IF NOT EXISTS resources (
    date TEXT, tag TEXT, resource TEXT,
    produced REAL, imported REAL, exported REAL,    -- per day, as of the snapshot
    PRIMARY KEY (date, tag, resource)
);
-- country_reports: running totals since game start, updated weekly. They stay
-- frozen when a tag dies, and a new tag (e.g. after a tag switch) starts at 0.
CREATE TABLE IF NOT EXISTS produced_total (
    date TEXT, tag TEXT,
    category TEXT,                  -- engine archetype group: infantry, armor, fighter, screen_ship, ...
    total REAL,
    PRIMARY KEY (date, tag, category)
);
CREATE TABLE IF NOT EXISTS constructed_total (
    date TEXT, tag TEXT, building TEXT, total REAL,
    PRIMARY KEY (date, tag, building)
);
CREATE TABLE IF NOT EXISTS buildings (
    date TEXT, tag TEXT, building TEXT,
    level REAL, healthy REAL,       -- healthy excludes damaged levels
    PRIMARY KEY (date, tag, building)
);
CREATE TABLE IF NOT EXISTS planes (
    date TEXT, tag TEXT,
    role TEXT,                      -- air wing definition: fighter, cas, cv_nav_bomber, ...
    equipment TEXT, variant_id INTEGER, count REAL,
    PRIMARY KEY (date, tag, role, variant_id)
);
CREATE TABLE IF NOT EXISTS ships (
    date TEXT, tag TEXT,
    role TEXT,                      -- ship definition: destroyer, battleship, ...
    equipment TEXT, variant_id INTEGER, count INTEGER,
    PRIMARY KEY (date, tag, role, variant_id)
);
CREATE TABLE IF NOT EXISTS production_lines (
    date TEXT, tag TEXT, line_id INTEGER,
    kind TEXT,                      -- military | naval
    equipment TEXT, variant_id INTEGER,
    active_factories REAL, requested_factories REAL,
    priority INTEGER, speed REAL, produced REAL, cost REAL,
    amount INTEGER,                 -- units ordered; -1 = repeat forever
    resources_missing REAL,         -- sum of resource `need`: shortfall throttling the line
    PRIMARY KEY (date, tag, line_id)
);
CREATE TABLE IF NOT EXISTS stockpile (
    date TEXT, tag TEXT, equipment TEXT, variant_id INTEGER, amount REAL,
    PRIMARY KEY (date, tag, variant_id)
);
CREATE TABLE IF NOT EXISTS war_casualties (
    date TEXT, first TEXT, second TEXT, start_date TEXT,
    first_casualties INTEGER,       -- cumulative losses suffered by `first`
    second_casualties INTEGER,
    PRIMARY KEY (date, first, second, start_date)
);
CREATE TABLE IF NOT EXISTS variant (
    variant_id INTEGER PRIMARY KEY,
    equipment TEXT, name TEXT, creator TEXT
);

-- A war's record disappears from the save when it ends, so its final totals
-- are in the last snapshot it appears in.
CREATE VIEW IF NOT EXISTS war_final AS
SELECT w.* FROM war_casualties w
JOIN (SELECT first, second, start_date, MAX(date) AS date
      FROM war_casualties GROUP BY first, second, start_date) m
  USING (first, second, start_date, date);

CREATE VIEW IF NOT EXISTS country_casualties AS
SELECT tag, SUM(c) AS casualties FROM (
    SELECT first AS tag, first_casualties AS c FROM war_final
    UNION ALL
    SELECT second, second_casualties FROM war_final
) GROUP BY tag;
"""


def d(v):
    """A block as a dict; the parser returns an empty `{}` as a list."""
    return v if isinstance(v, dict) else {}


def ref_id(ref):
    """`{ id=1364 type=70 }` -> 1364"""
    return int(ref['id']) if isinstance(ref, dict) and 'id' in ref else None


def equipment_amounts(block):
    """Yield (variant_id, amount) from an `equipment={ equipment={id amount} ... }` block."""
    if not isinstance(block, dict):
        return
    for e in p.as_list(block.get('equipment')):
        if isinstance(e, dict):
            yield ref_id(e.get('id')), p.num(e.get('amount'))


_SCALAR = re.compile(rb'\b(original_tag|cosmetic_tag|stability|war_support|command_power|'
                     rb'convoys_destroyed)\s*=\s*(?:"([^"]*)"|([^\s{}"]+))')


def country_scalars(data, start, end, spans):
    """The few country-level scalars we keep, read only from the gaps between
    the country's sub-blocks (so a nested block's fields can't match)."""
    found = {}
    pos = start
    for _, ks, ke, _ in spans + [(None, end, end, None)]:
        for m in _SCALAR.finditer(data, pos, ks):
            v = m.group(2) if m.group(2) is not None else m.group(3)
            found[m.group(1).decode()] = v.decode('utf-8', 'replace') or None
        pos = ke + 1
    return found


def _descend(key, level):
    """Block levels extract_file needs located: countries -> TAG -> section ->
    the sub-blocks of `resources`."""
    return key == 'countries' if level == 1 else level == 2 or key == 'resources'


def read_variants(equipments):
    """{variant_id: (equipment, name, creator)} from the global `equipments` table."""
    variants = {}
    for equipment, vs in equipments.items():
        for v in p.as_list(vs):
            if isinstance(v, dict) and 'id' in v:
                variants[ref_id(v['id'])] = (equipment, v.get('name'), v.get('creator'))
    return variants


def read_states(states):
    """Building (level, healthy) totals and available manpower, by controller."""
    buildings = defaultdict(lambda: [0.0, 0.0])
    manpower = Counter()
    for st in states.values():
        if not isinstance(st, dict) or 'owner' not in st:
            continue
        tag = st.get('controller', st['owner'])
        manpower[tag] += p.num(d(st.get('manpower_pool')).get('available'))
        for name, lv in d(st.get('buildings')).items():
            if isinstance(lv, dict):
                buildings[(tag, name)][0] += p.num(lv.get('level'))
                buildings[(tag, name)][1] += p.num(lv.get('healthy_levels'))
    return buildings, manpower


def read_air(strategic_air):
    """{(tag, role, variant_id): planes} from strategic_air.<TAG>.air_wing_pool[].air_wings[]."""
    planes = Counter()
    for tag, blk in strategic_air.items():
        for pool in p.as_list(d(blk).get('air_wing_pool')):
            pool = d(pool)
            for wing in p.as_list(pool.get('air_wings')):
                for vid, amount in equipment_amounts(d(wing).get('equipment')):
                    planes[(tag, pool.get('definition'), vid)] += amount
    return planes


def read_country(data, date, tag, start, end, spans, equipment_of, rows):
    """Append one country's rows; return (identity, stats, divisions, {(role, variant_id): ships})."""
    c = {k: p.parse_bytes(data, s, e) for k, s, e, _ in spans if k in COUNTRY_SECTIONS}
    sc = country_scalars(data, start, end, spans)

    # `resources` is mostly trade routes; parse only the per-resource flows
    flows = {}
    for k, _, _, kids in spans:
        if k == 'resources':
            flows = {f: d(p.parse_bytes(data, fs, fe)) for f, fs, fe, _ in kids if f in RESOURCE_FLOWS}
    for res in sorted({r for blk in flows.values() for r in blk}):
        rows['resources'].append((date, tag, res,
                                  *(p.num(flows.get(f, {}).get(res)) for f in RESOURCE_FLOWS)))

    pol, fuel = d(c.get('politics')), d(c.get('fuel_status'))
    techs = d(d(c.get('technology')).get('technologies'))
    stats = (
        p.num(sc.get('stability'), None), p.num(sc.get('war_support'), None),
        p.num(pol.get('political_power'), None), p.num(sc.get('command_power'), None),
        pol.get('ruling_party'),
        sum(1 for t in techs.values() if 'level' in d(t)),
        p.num(fuel.get('fuel'), None), p.num(fuel.get('max_fuel'), None),
        sum(a for _, a in equipment_amounts(d(c.get('convoys')).get('equipment'))),
        int(p.num(sc.get('convoys_destroyed'))))

    rep = d(c.get('country_reports'))
    for table, key in (('produced_total', 'equipment_production'), ('constructed_total', 'construction')):
        rows[table] += [(date, tag, k, p.num(v)) for k, v in d(rep.get(key)).items() if p.num(v)]

    units = d(c.get('units'))
    ships = Counter()
    for fleet in p.as_list(units.get('fleet')):
        for tf in p.as_list(d(fleet).get('task_force')):
            for ship in p.as_list(d(tf).get('ship')):
                ship = d(ship)
                for vid, _ in equipment_amounts(ship.get('equipment')):
                    ships[(ship.get('definition'), vid)] += 1

    prod = d(c.get('production'))
    for kind in ('military', 'naval'):
        for ln in p.as_list(prod.get(f'{kind}_lines')):
            if not isinstance(ln, dict):
                continue
            vid = ref_id(ln.get('equipment_variant_index'))
            rows['production_lines'].append((
                date, tag, ref_id(ln.get('id')), kind, equipment_of(vid), vid,
                p.num(ln.get('active_factories')), p.num(ln.get('requested_factories')),
                int(p.num(ln.get('priority'))), p.num(ln.get('speed')),
                p.num(ln.get('produced')), p.num(ln.get('cost')),
                int(p.num(ln.get('amount'))),
                sum(p.num(r.get('need')) for r in ln.get('resources') or [] if isinstance(r, dict))))
    stock = Counter()
    for vid, amount in equipment_amounts(prod.get('equipments')):
        stock[vid] += amount
    rows['stockpile'] += [(date, tag, equipment_of(vid), vid, amt) for vid, amt in stock.items() if amt]

    # wars: diplomacy is ~20% of the save, so parse only its war_relation blocks
    for k, s, e, _ in spans:
        if k == 'diplomacy':
            for ws, we in p.find_blocks(data, 'war_relation', s, e):
                wr = d(p.parse_bytes(data, ws, we))
                if wr.get('first') == tag:      # each pair once
                    rows['war_casualties'].append((
                        date, wr['first'], wr.get('second'), wr.get('start_date'),
                        int(p.num(wr.get('first_casualties'))),
                        int(p.num(wr.get('second_casualties')))))

    identity = (sc.get('original_tag'), sc.get('cosmetic_tag'))
    return identity, stats, len(p.as_list(units.get('division'))), ships


def extract_file(path):
    """Parse one snapshot into {table: [rows]}. Runs in a worker process."""
    with gzip.open(path, 'rb') as f:
        data = f.read()
    p.check_header(data)
    date = p.padded_date(p.header_date(data))
    top = {}
    for key, s, e, kids in p.block_tree(data, depth=4, descend=_descend):
        top.setdefault(key, (s, e, kids))     # these sections each appear once

    def section(key):
        return d(p.parse_bytes(data, *top[key][:2])) if key in top else {}

    variants = read_variants(section('equipments'))

    def equipment_of(vid):
        return variants.get(vid, ('?',))[0]

    rows = defaultdict(list)
    rows['snapshot'].append((date, p.header_field(data, 'game_unique_id'),
                             p.header_field(data, 'version'), Path(path).name))
    rows['variant'] = [(vid, *v) for vid, v in variants.items()]

    # states: factories, all buildings and manpower go to the controller
    buildings, manpower = read_states(section('states'))
    rows['buildings'] = [(date, tag, name, lv, hl) for (tag, name), (lv, hl) in buildings.items()]

    divisions, ships, ident, stats = Counter(), Counter(), {}, {}
    for tag, cs, ce, spans in top['countries'][2]:
        if tag is None:
            continue
        ident[tag], stats[tag], divisions[tag], own = read_country(
            data, date, tag, cs, ce, spans, equipment_of, rows)
        for (role, vid), n in own.items():
            ships[(tag, role, vid)] += n
    rows['ships'] = [(date, tag, role, equipment_of(vid), vid, n)
                     for (tag, role, vid), n in ships.items()]

    planes = read_air(section('strategic_air'))
    rows['planes'] = [(date, tag, role, equipment_of(vid), vid, n)
                      for (tag, role, vid), n in planes.items() if n]

    # a country gets a row if it controls territory or fields anything
    fielded = {t for t, n in divisions.items() if n} | {t for t, *_ in planes} | {t for t, *_ in ships}
    for tag in sorted(set(manpower) | fielded):
        rows['country'].append((
            date, tag,
            *(buildings.get((tag, b), (0.0,))[0] for b in FACTORY_BUILDINGS.values()),
            manpower[tag], divisions[tag]))
        rows['country_tag'].append((date, tag, *ident.get(tag, (None, None))))
        if tag in stats:
            rows['country_stats'].append((date, tag, *stats[tag]))
    # running totals only for countries that still exist (dead tags keep stale ones)
    alive = {r[1] for r in rows['country']}
    for table in ('produced_total', 'constructed_total', 'resources'):
        rows[table] = [r for r in rows[table] if r[1] in alive]
    return date, dict(rows)


def insert(db, rows):
    for table, rs in rows.items():
        if table in ('snapshot', 'variant') or not rs:
            continue
        db.executemany(f'INSERT OR REPLACE INTO {table} VALUES ({",".join("?" * len(rs[0]))})', rs)
    db.executemany('INSERT OR IGNORE INTO variant VALUES (?,?,?,?)', rows['variant'])
    db.executemany('INSERT INTO snapshot VALUES (?,?,?,?)', rows['snapshot'])   # last


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('archive', type=Path, help='folder of <date>.hoi4.gz snapshots')
    ap.add_argument('--db', type=Path, help='default: <archive>/telemetry.sqlite')
    ap.add_argument('-j', '--jobs', type=int, default=4,
                    help='parallel parsers, ~0.3 GB RAM each (default 4)')
    ap.add_argument('--rebuild', action='store_true', help='delete the db and start over')
    args = ap.parse_args()

    db_path = args.db or args.archive / 'telemetry.sqlite'
    if args.rebuild and db_path.exists():
        db_path.unlink()
    db = sqlite3.connect(db_path)
    db.executescript(SCHEMA)
    done = {r[0] for r in db.execute('SELECT date FROM snapshot')}

    files = sorted(args.archive.glob('*.hoi4.gz'))
    todo = [f for f in files if f.name.removesuffix('.hoi4.gz') not in done]
    print(f'{len(files)} snapshots, {len(files) - len(todo)} already loaded, {len(todo)} to parse',
          flush=True)
    if not todo:
        return

    t0 = time.time()
    failed = 0
    with ProcessPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {pool.submit(extract_file, str(f)): f for f in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            f = futures[fut]
            try:
                date, rows = fut.result()
            except Exception as e:
                failed += 1
                print(f'[{i}/{len(todo)}] {f.name}: FAILED {type(e).__name__}: {e}', flush=True)
                continue
            try:
                with db:
                    insert(db, rows)
            except sqlite3.IntegrityError as e:   # e.g. a renamed file whose date is already loaded
                failed += 1
                print(f'[{i}/{len(todo)}] {f.name}: FAILED to store {date}: {e}', flush=True)
                continue
            print(f'[{i}/{len(todo)}] {date}: {len(rows.get("country", []))} countries, '
                  f'{len(rows.get("production_lines", []))} lines, {len(rows.get("war_casualties", []))} war pairs',
                  flush=True)
    db.close()
    print(f'done in {time.time() - t0:.0f}s, {failed} failed -> {db_path}')
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
