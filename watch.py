#!/usr/bin/env python3
"""Archive every new HOI4 autosave as a gzipped snapshot.

    python watch.py <run-name> [--save PATH] [--archive DIR]

Polls the autosave every 2s. When its mtime changes, waits for the size to
settle, reads it into memory (read-only; the original is never modified),
and writes archive/<run-name>/<date>.hoi4.gz, where <date> is the in-game
date from the header, zero-padded so files sort chronologically
(1936.2.1.2 -> 1936.02.01.02).
"""

import argparse
import gzip
import os
import sys
import time
from pathlib import Path

import pdxparse

DEFAULT_SAVE = (Path.home() / '.local/share/Paradox Interactive/Hearts of Iron IV'
                / 'save games/autosave.hoi4')
POLL = 2.0
SETTLE = 1.0          # seconds between size checks while waiting for the write
SETTLE_TIMEOUT = 120


def log(msg):
    print(time.strftime('%H:%M:%S'), msg, flush=True)


def wait_stable(path):
    """Block until size and mtime stop changing; return the final stat."""
    deadline = time.monotonic() + SETTLE_TIMEOUT
    prev = None
    while time.monotonic() < deadline:
        try:
            st = path.stat()
        except FileNotFoundError:   # game may delete-and-recreate
            prev = None
            time.sleep(SETTLE)
            continue
        cur = (st.st_size, st.st_mtime_ns)
        if cur == prev and st.st_size > 0:
            return st
        prev = cur
        time.sleep(SETTLE)
    raise TimeoutError(f'{path} did not settle within {SETTLE_TIMEOUT}s')


def read_complete(path):
    """Read the save once it is stable and complete.
    Returns (data, mtime_ns of what was read); data is None on failure."""
    for _ in range(5):
        st = wait_stable(path)
        with open(path, 'rb') as f:
            data = f.read()
        after = path.stat()
        if (after.st_size, after.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
            continue                        # changed while we were reading
        # plaintext saves end with a checksum line once fully written
        if data.startswith(b'HOI4txt') and b'checksum=' not in data[-256:]:
            time.sleep(SETTLE)
            continue
        return data, st.st_mtime_ns
    return None, after.st_mtime_ns


def run_game_id(out_dir):
    """game_unique_id of the campaign already archived in this run, if any."""
    for snap in sorted(out_dir.glob('*.hoi4.gz')):
        with gzip.open(snap, 'rb') as f:
            gid = pdxparse.header_field(f.read(4096), 'game_unique_id')
        if gid:
            return gid
    return None


def archive(data, out_dir, run_gid):
    """Write one snapshot. run_gid is the campaign this run belongs to (None
    until the first snapshot); returns it, set from this save if it was None."""
    try:
        pdxparse.check_header(data)
    except ValueError as e:
        log(f'skipped: {e}')
        return run_gid
    date = pdxparse.header_date(data)
    if not date:
        log('skipped: no date= in header')
        return run_gid
    # one run = one campaign; a different game would silently corrupt the series
    gid = pdxparse.header_field(data, 'game_unique_id')
    if run_gid and gid != run_gid:
        log(f'{date}: skipped, save is from a different game ({gid}) than this '
            f'run ({run_gid}); use a new run name')
        return run_gid
    dest = out_dir / f'{pdxparse.padded_date(date)}.hoi4.gz'
    if dest.exists():
        log(f'{date}: already archived ({dest.name}), keeping the existing copy '
            '(a reloaded earlier save replays dates)')
        return run_gid or gid
    tmp = dest.with_suffix('.gz.tmp')
    with gzip.open(tmp, 'wb', compresslevel=6) as f:
        f.write(data)
    os.replace(tmp, dest)
    log(f'{date}: {len(data) / 1e6:.1f} MB -> {dest} ({dest.stat().st_size / 1e6:.1f} MB)')
    return run_gid or gid


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('run', help='run name; snapshots go to <archive>/<run>/')
    ap.add_argument('--save', type=Path, default=DEFAULT_SAVE, help='autosave to watch')
    ap.add_argument('--archive', type=Path, default=Path(__file__).parent / 'archive',
                    help='archive root')
    args = ap.parse_args()

    out_dir = args.archive / args.run
    out_dir.mkdir(parents=True, exist_ok=True)
    run_gid = run_game_id(out_dir)
    resuming = run_gid is not None
    log(f'{"resuming" if resuming else "new run"}: watching {args.save} -> {out_dir}/')

    # On a new run the autosave already on disk belongs to whatever ran before,
    # so wait for the next one. When resuming, pick it up now (the game-id check
    # in archive() rejects it if it's from another campaign) so a save written
    # while the watcher was stopped isn't lost.
    last = None
    if not resuming:
        try:
            last = args.save.stat().st_mtime_ns
        except FileNotFoundError:
            pass

    while True:
        try:
            mtime = args.save.stat().st_mtime_ns
        except FileNotFoundError:
            mtime = None
        if mtime is not None and mtime != last:
            try:
                data, last = read_complete(args.save)
                if data is None:
                    log('skipped: save kept changing while being read')
                else:
                    run_gid = archive(data, out_dir, run_gid)
            except (OSError, TimeoutError) as e:
                log(f'error: {e}')
                last = mtime
        time.sleep(POLL)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
