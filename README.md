# HOI4 telemetry

Record a Hearts of Iron IV campaign month by month, then chart it.

- **`watch.py`** watches the autosave while you play and keeps a gzipped copy of every one. It only reads the original save and never changes it.
- **`extract.py`** parses the archived snapshots into a SQLite database with one row per (date, country), plus production lines, stockpiles, wars and more.
- **`viz.py`** turns the database into a single self-contained `report.html`, with interactive charts that work offline.
- **`compare.py`** puts one country from several runs on shared charts, for example your Germany against two AI Germanys.

Nothing in the code names a country tag or an equipment type, so it works with overhaul mods as well as vanilla. Kaiserreich has been tested, including dynamic tags, tag switches and civil wars.

Requirements: Python 3.9+ and nothing else. There are no third-party packages.

## 1. Set up the game

### Saves must be plaintext, not binary

By default HOI4 writes binary saves (`HOI4bin`), which this tool cannot read. Before you start the campaign, turn on plaintext saves.

1. Quit the game.
2. Open `settings.txt` in the HOI4 user folder:
   - Linux: `~/.local/share/Paradox Interactive/Hearts of Iron IV/settings.txt`
   - Windows: `Documents\Paradox Interactive\Hearts of Iron IV\settings.txt`
   - macOS: `~/Documents/Paradox Interactive/Hearts of Iron IV/settings.txt`
3. Set:

   ```
   save_as_binary=no
   ```

4. Start the game again. The `-debug` launch option also produces plaintext saves.

You can check a save by looking at its first bytes. A plaintext save starts with `HOI4txt`, and a binary one starts with `HOI4bin`. If the save is a zip (it starts with `PK`), turn save compression off as well. Every script rejects binary and compressed saves with a message that says what to change.

Plaintext saves are large: 70 to 100 MB in the mid-game. `watch.py` stores them gzipped at about 9 MB each.

### Autosave frequency

Each autosave becomes one data point. **Monthly** autosaves (in the game's settings) give smooth charts: a 1936 to 1944 run is about 100 snapshots, or about 800 MB of archive.

## 2. Record a run

Start the watcher before you load or start the campaign, and leave it running while you play:

```bash
python3 watch.py KR-test-2
```

- Snapshots go to `archive/<run>/<date>.hoi4.gz`, named by the in-game date and zero-padded so they sort: `1936.02.01.02.hoi4.gz`.
- The watcher polls every 2s. It waits for the game to finish writing, checks for the `checksum=` line that ends a complete save, and writes the copy through a temp file, so a half-written snapshot never lands in the archive.
- **One run is one campaign.** The first snapshot's `game_unique_id` is recorded, and saves from any other game are skipped with a warning. To start a new campaign, use a new run name.
- **New run:** the autosave already on disk is ignored, because it belongs to whatever you played before. Recording starts with the next autosave.
- **Resuming:** restarting with an existing run name picks the current autosave up right away, as long as it is from the same campaign. Nothing saved while the watcher was stopped is lost, except autosaves the game overwrote in the meantime.
- **Same date twice:** if a reload replays a date that is already archived, the first copy is kept.
- Stop the watcher with Ctrl-C.

Options: `--save PATH` watches a different save file (the default is the Linux autosave path above, under `save games/autosave.hoi4`). `--archive DIR` changes the archive root.

## 3. Build the database

```bash
python3 extract.py archive/KR-test-2
```

- This writes `archive/<run>/telemetry.sqlite`.
- It is **incremental**: dates already loaded are skipped, so you can re-run it while the watcher is still recording.
- Each snapshot is committed in one transaction. An interrupted run never leaves a half-loaded date.
- It runs about 5 to 6 s per save per worker. `-j N` sets the number of parallel workers (default 4, about 0.3 GB of RAM each), so 100 snapshots take about 2 minutes.
- `--rebuild` deletes the database and starts over. Use it after changing `extract.py`.
- `--db PATH` writes somewhere else.

A snapshot that fails to parse is reported and skipped. The exit code is 1 if anything failed.

## 4. Make the report

```bash
python3 viz.py archive/KR-test-2
```

This writes `archive/<run>/report.html`. Open it in any browser; it needs no network.

- **Overview** compares the 8 nations with the highest peak factory count. Eight is as many lines as the colour palette can keep apart. Click the nation chips to hide or show them. The overview has three groups:
  - **Industry:** equipment produced (a dropdown per category), factories, buildings constructed, idle military factories, and damaged factories.
  - **Military:** divisions, manpower, planes, ships, casualties, convoys sunk, and a table of the 25 bloodiest wars.
  - **Politics, research & supply:** stability, war support, technologies, fuel, and ruling-party history.
- **Country detail** covers any of the top 24 nations:
  - production by equipment family, and military industry (factories, assigned, short of resources)
  - factory allocation, air force and navy composition
  - resources produced, imported and exported
  - stockpile by family
  - foreign designs in service (mostly captured kit)
- Every chart has hover tooltips and a **Table** view. The page follows the system light/dark setting.

## 5. Compare runs

To see how one country played out across several runs (say, you against the AI), use `compare.py`:

```bash
python3 compare.py --tag GER test-run-0 test-run-1 jack-plays-germany-0 --out archive/compare-ger.html
```

- Each run is a name under `archive/` or a path to a run directory. It needs that run's `telemetry.sqlite`, so run `extract.py` first.
- `--tag` is the country (default `GER`). `--until YYYY.MM` clips every run at a date (default `1944.10`), so a long run is not compared past the end of a short one.
- The output is one self-contained page: a table of values at key dates, then line charts with all runs on a shared in-game-date axis. Hover a chart to read every run at that month.
- The charts cover factories, manpower, divisions, technologies, stability, war support, political power, convoys (the pool, and convoys sunk), ships and planes in service, equipment produced, and cumulative casualties with a casualty ratio.
- Casualties count only wars the country fights directly, so allies' losses are not included. Wars vanish from the save when they end, so each war pair's running maximum is carried forward instead of dropping out.
- The runs are separate campaigns that diverge, so some differences come from the world and not from the player.

## Data model

All dates are zero-padded text (`1941.06.01.02`, i.e. y.m.d.hour), so they sort and compare as strings. `tag` is the country tag in the save (`GER`, `ENG`, `D05`, …).

| Table | One row per | What's in it |
|---|---|---|
| `snapshot` | date | game id, game version, source file |
| `country` | date, tag | civ / mil / naval factories, manpower, divisions |
| `country_tag` | date, tag | `original_tag` (for dynamic tags: `D05` → `SPA`), `cosmetic_tag` (government-specific name key) |
| `country_stats` | date, tag | stability, war support (0..1), political and command power, ruling party, technologies researched, fuel and max fuel, convoys, convoys destroyed |
| `buildings` | date, tag, building | level and healthy (undamaged) level, for every building type |
| `resources` | date, tag, resource | produced, imported, exported per day |
| `produced_total` | date, tag, category | running total of equipment produced, by the game's categories (infantry, armor, fighter, screen_ship, …) |
| `constructed_total` | date, tag, building | running total of buildings constructed |
| `production_lines` | date, tag, line | equipment, variant, active and requested factories, priority, speed, output, resource shortfall |
| `stockpile` | date, tag, variant | equipment amount (can be negative: owed) |
| `planes` / `ships` | date, tag, role, variant | count by air-wing or ship role |
| `war_casualties` | date, war | each side's cumulative losses (`first_casualties` = losses suffered by `first`) |
| `variant` | variant | equipment type, design name, creator country |

Views:
- **`war_final`** holds each war's last-seen totals, because a war's record disappears from the save when it ends.
- **`country_casualties`** sums those totals per country.

### Things to know when reading the data

- **Territory counts under the controller, not the owner.** Factories, buildings and manpower are summed over the states a country *controls*. An occupier's factory count includes occupied land until the peace deal returns or transfers it, so it can drop sharply at the peace.
- **Tag switches break series.** When a country changes tag (formables, or Kaiserreich's RUS → RSA), the old tag's series ends and the new one starts. Running totals (`produced_total`, `constructed_total`) restart at 0 on the new tag, and a dead tag's totals stay frozen.
- **The running totals lag a little.** The game updates them weekly, so they trail the snapshot date by up to a week.
- **Casualties stop at the last snapshot before a peace.** Wars vanish from the save when they end, so losses in the last few weeks before a peace (up to one autosave interval) are missing.
- **Stability can be negative.** That is real: it happens on the way to a civil war, for example.
- **`convoys_destroyed`** is the save's own counter of convoys the country has **sunk** (not lost). This was confirmed by play: a submarine-heavy Germany run showed over 2,000 here while the Allies' counts stayed under 1,000.
- **Manpower** is the available manpower pool of controlled states, which matches the in-game number.

### Example queries

```sql
-- infantry equipment produced, per nation, as of the last snapshot
SELECT tag, MAX(total) FROM produced_total
WHERE category = 'infantry' GROUP BY tag ORDER BY 2 DESC LIMIT 10;

-- the bloodiest wars
SELECT first, second, start_date, first_casualties, second_casualties
FROM war_final ORDER BY first_casualties + second_casualties DESC LIMIT 10;

-- every change of government
SELECT tag, MIN(date), ruling_party FROM country_stats
GROUP BY tag, ruling_party ORDER BY tag, 2;
```

## Code layout

| File | Role |
|---|---|
| `pdxparse.py` | Parser for Paradox plaintext saves. `block_tree` / `find_blocks` locate blocks by brace-matching alone, and `parse_bytes` parses only the region you give it. Building a tree of a whole 100 MB save would take minutes; parsing just the needed sections takes seconds. |
| `watch.py` | Autosave watcher and archiver. |
| `extract.py` | Snapshot → SQLite. `extract_file` locates the sections it needs in one pass, then hands each part to a `read_*` function. The schema is the `SCHEMA` string at the top. |
| `viz.py` | SQLite → JSON. Each chart's data is shaped by a small function and injected into the template. |
| `report_template.html` | The report page: plain JS and SVG, no libraries. Data arrives as the `D` object. |
| `compare.py` | SQLite (several runs) → JSON for one country. `METRICS` holds the SQL for each chart, and `PRODUCED` lists which equipment categories each production chart adds up. |
| `compare_template.html` | The comparison page: plain JS and SVG. Data arrives as the `D` object. |

### Adding a metric

1. Find the field in a save. Plaintext saves can be grepped: `zcat archive/<run>/<date>.hoi4.gz | grep -n field_name | head`.
2. Add a column or table to `SCHEMA` in `extract.py`, and fill it in the matching `read_*` function. Country-level fields go in `read_country`. If the field sits in a country sub-block that isn't parsed yet, add the block's name to `COUNTRY_SECTIONS`.
3. Re-extract with `--rebuild`.
4. Add a `series(...)` entry to the `overview` dict in `viz.py`, and a matching spec entry in `OVERVIEW` in `report_template.html`.

To change what a production chart counts in the comparison page, edit the category names in `PRODUCED` in `compare.py` (list them with `select distinct category from produced_total`). Unlike the rest of the project, `compare.py` names vanilla equipment categories and ship and plane roles, so a mod with different names needs those lists updated.

Keep it mod-agnostic: read keys and categories from the save instead of listing tags or equipment names. The only hardcoded game strings are the three factory building names (`industrial_complex`, `arms_factory`, `dockyard`), which are engine built-ins.
