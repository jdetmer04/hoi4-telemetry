"""Minimal parser for Paradox plaintext ("HOI4txt") save files.

Output model:
  * An object `{ k=v ... }` becomes a dict. A key that repeats becomes a list
    of its values, so use `as_list()` when a key may appear once or many times.
  * A bare list `{ 1 2 3 }` becomes a Python list.
  * Anonymous objects `{ {..} {..} }` become a list of dicts.
  * Scalars stay strings; callers convert what they need (`num()`).

`block_tree()` / `find_blocks()` locate blocks by brace-matching alone, so
callers can parse only the parts they need: building the full tree for a
100 MB save would take minutes.
"""

import re

_TOKEN = re.compile(
    rb'"((?:[^"\\]|\\.)*)"'      # 1: quoted string
    rb'|([{}])'                  # 2: brace
    rb'|(<=|>=|!=|=|<|>)'        # 3: operator
    rb'|([^\s{}=<>!"]+)'         # 4: bare word
)


def _block(toks, i, n):
    """Parse toks[i:] up to the matching '}' (or the end); returns (value, next i).
    Each token is a (quoted, brace, operator, word) tuple with one field set,
    except an empty quoted string, which has none."""
    obj = None    # dict once we see a key=value
    items = []    # bare items, used if this turns out to be a list
    while i < n:
        q, b, op, w = toks[i]
        i += 1
        if b == b'}':
            break
        if b:
            v, i = _block(toks, i, n)
            items.append(v)
            continue
        val = (op or w or q).decode('utf-8', 'replace')
        if i < n and toks[i][2]:                # key = value
            i += 1
            if i >= n:
                v = None
            elif toks[i][1] == b'{':
                v, i = _block(toks, i + 1, n)
            else:
                vq, vb, vop, vw = toks[i]
                i += 1
                v = (vop or vw or vq).decode('utf-8', 'replace') if vb != b'}' else None
                if vb == b'}':
                    i -= 1
                # `rgb { 1 2 3 }` / `hsv { ... }` style tagged values
                elif i < n and toks[i][1] == b'{':
                    tagged, i = _block(toks, i + 1, n)
                    v = {'__tag__': v, '__value__': tagged}
            if obj is None:
                obj = {}
            if val in obj:
                cur = obj[val]
                if isinstance(cur, _Multi):
                    cur.append(v)
                else:
                    obj[val] = _Multi([cur, v])
            else:
                obj[val] = v
        else:
            items.append(val)
    if obj is None:
        return items, i
    if items:
        # mixed content (rare): keep bare items under a sentinel key
        obj['__items__'] = items
    return obj, i


class _Multi(list):
    """List produced by a repeated key (vs. a literal `{ a b }` list)."""


def as_list(v):
    if v is None:
        return []
    if isinstance(v, _Multi):
        return list(v)
    return [v]


def num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def parse_bytes(data, start=0, end=None):
    toks = _TOKEN.findall(data, start, len(data) if end is None else end)
    return _block(toks, 0, len(toks))[0]


def check_header(data):
    if data.startswith(b'HOI4bin'):
        raise ValueError(
            'binary save: enable plaintext saves (launch option -debug, or '
            'set save_as_binary=no in settings.txt)')
    if data[:2] == b'PK':
        raise ValueError('compressed save: disable save compression in settings')
    if not data.startswith(b'HOI4txt'):
        raise ValueError('not a HOI4 plaintext save')


_SCAN = re.compile(rb'"(?:[^"\\]|\\.)*"|[{}]')
_NOT_KEY = b'{}"=<>!'


def _key_before(data, pos, start):
    """The key of the `key={` whose brace is at pos, or None if anonymous."""
    head = data[max(start, pos - 200):pos].rstrip()
    if not head.endswith(b'='):
        return None
    words = head[:-1].rsplit(None, 1)
    if not words:
        return None
    word = words[-1]
    word = word[max(word.rfind(c) for c in _NOT_KEY) + 1:]
    return word.decode() if word else None


def block_tree(data, start=0, end=None, depth=1, descend=None):
    """[(key, start, end, children)] for each `key={...}` block directly inside
    [start, end); start/end delimit the content between the braces, and
    anonymous blocks get key None. Brace-matching only, no tree of values.

    children lists the blocks one level further in, down to `depth` levels,
    for blocks where descend(key, level) is true (level 1 = the outermost);
    otherwise it is None. One pass, however deep."""
    end = len(data) if end is None else end
    root, stack, skip = [], [], 0      # stack: (key, start, children) per recorded open block
    for m in _SCAN.finditer(data, start, end):
        tok = m.group(0)
        if tok == b'{':
            if skip or (stack and stack[-1][2] is None):
                skip += 1
                continue
            level = len(stack) + 1
            key = _key_before(data, m.start(), start)
            want = level < depth and (descend is None or descend(key, level))
            stack.append((key, m.end(), [] if want else None))
        elif tok == b'}':
            if skip:
                skip -= 1
            elif stack:
                key, s, kids = stack.pop()
                (stack[-1][2] if stack else root).append((key, s, m.start(), kids))
    return root


def block_spans(data, start=0, end=None):
    """(key, start, end) for each block directly inside [start, end)."""
    return [(k, s, e) for k, s, e, _ in block_tree(data, start, end)]


def find_blocks(data, key, start=0, end=None):
    """(start, end) of every `key={...}` block at any depth in [start, end),
    without scanning anything else. A match nested in another is skipped."""
    end = len(data) if end is None else end
    pat = re.compile(rb'(?<![^\s{}])' + re.escape(key.encode()) + rb'\s*=\s*\{')
    pos = start
    while m := pat.search(data, pos, end):
        depth = 1
        for t in _SCAN.finditer(data, m.end(), end):
            if t.group(0) == b'{':
                depth += 1
            elif t.group(0) == b'}':
                depth -= 1
                if depth == 0:
                    break
        else:
            return                      # unterminated
        yield m.end(), t.start()
        pos = t.end()


def padded_date(date):
    """'1936.2.1.2' -> '1936.02.01.02', so dates sort as text."""
    if not date:
        raise ValueError('no date= in the save header')
    return '.'.join(part.zfill(4 if i == 0 else 2) for i, part in enumerate(date.split('.')))


_DATE = re.compile(rb'^date\s*=\s*"?([0-9.]+)"?', re.M)


def header_date(data_or_head):
    """In-game date from the save header, e.g. '1936.2.1.2' (y.m.d.hour)."""
    m = _DATE.search(data_or_head[:4096])
    return m.group(1).decode() if m else None


def header_field(data_or_head, key):
    """A top-level scalar from the save header, e.g. 'game_unique_id'."""
    m = re.search(rb'^' + re.escape(key.encode()) + rb'\s*=\s*"?([^"\r\n]*)"?',
                  data_or_head[:4096], re.M)
    return m.group(1).decode() if m else None
