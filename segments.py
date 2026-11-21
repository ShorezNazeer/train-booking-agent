"""Per-segment seat availability, so one train can be booked in different classes.

A segment is the hop between two consecutive stops: segment s runs from stop s
to stop s + 1. Seats are keyed by the train's *run date* (the day it left its
origin), and they change only every BLOCK stops, like a real reservation chart.
Still simulated: nothing here talks to IRCTC.
"""
from datetime import timedelta
from functools import lru_cache

import availability as av

BLOCK = 8  # availability can change at most every BLOCK stops

# Demo/testing hook: (train, travel_class, segment) entries forced sold out or available.
FORCED_SEGMENT_SOLD_OUT: set = set()
FORCED_SEGMENT_AVAILABLE: dict = {}

_train_meta_cache: dict = {}


def _get_train_meta(train: str):
    """Retrieve (classes_tuple, runs_days) for a train from trains.db."""
    t_str = str(train)
    if t_str not in _train_meta_cache:
        classes = None
        runs_days = None
        try:
            import sqlite3
            from data_source import DB_PATH
            if DB_PATH.exists():
                with sqlite3.connect(DB_PATH) as conn:
                    row = conn.execute(
                        "SELECT classes, runs_days FROM trains WHERE number = ?",
                        (t_str,)
                    ).fetchone()
                    if row:
                        if row[0]:
                            classes = tuple(c.strip() for c in row[0].split(",") if c.strip())
                        if row[1]:
                            runs_days = row[1].strip()
        except Exception:
            pass
        _train_meta_cache[t_str] = (classes, runs_days)
    return _train_meta_cache[t_str]


def train_classes(train: str):
    return _get_train_meta(train)[0]


def train_runs_days(train: str):
    return _get_train_meta(train)[1]


def run_date(boarding_date, board_dep_abs):
    """The date the train left its origin, given the date you board and your
    boarding stop's departure minute (counted from the origin's day 1)."""
    return boarding_date - timedelta(days=board_dep_abs // 1440)


@lru_cache(maxsize=None)
def _block_seats(train, run_day, cls, block, chance):
    u = av._unit(f"{train}#{block}", run_day, cls)
    if u < chance:
        return 0
    scaled = (u - chance) / (1 - chance)
    cap = av.CAPACITY.get(cls, 50)
    return 1 + int(scaled * (cap - 1))


def segment_seats(train, run_day, cls, s):
    """Seats left in one class on segment s of this train's run."""
    # 1. Enforce train classes if train is in database
    t_classes, t_runs_days = _get_train_meta(train)
    if t_classes is not None and cls not in t_classes:
        return 0

    # 2. Enforce running days if train is in database
    if t_runs_days is not None:
        from data_source import train_runs_on_date
        if not train_runs_on_date(t_runs_days, run_day):
            return 0

    if cls not in av.CAPACITY:
        return 0

    if av.FORCED_SOLD_OUT or FORCED_SEGMENT_SOLD_OUT:
        if ((train, None) in av.FORCED_SOLD_OUT
                or (train, run_day.isoformat()) in av.FORCED_SOLD_OUT
                or (train, cls, s) in FORCED_SEGMENT_SOLD_OUT):
            return 0
    if (train, cls, s) in FORCED_SEGMENT_AVAILABLE:
        return FORCED_SEGMENT_AVAILABLE[(train, cls, s)]
    return _block_seats(train, run_day, cls, s // BLOCK, av.SOLD_OUT_CHANCE)


def any_class(train, run_day, s, classes=None):
    """True if at least one class has a seat on segment s."""
    if classes is None:
        t_classes = train_classes(train)
        classes = t_classes if t_classes else av.CLASSES
    return any(segment_seats(train, run_day, c, s) > 0 for c in classes)


def forced_trains():
    """Trains that have per-segment overrides (demo/test hook)."""
    return frozenset(t for t, _, _ in FORCED_SEGMENT_SOLD_OUT) | frozenset(t for t, _, _ in FORCED_SEGMENT_AVAILABLE)


def first_dead_segment(train, run_day, from_seq, n_segments, per_segment=False, classes=None):
    """Index of the first segment at or after from_seq that no class can carry
    (n_segments if there is none). Availability is constant inside a block, so
    this checks once per block unless the train has per-segment overrides."""
    if (train, None) in av.FORCED_SOLD_OUT or (train, run_day.isoformat()) in av.FORCED_SOLD_OUT:
        return from_seq

    t_classes, t_runs_days = _get_train_meta(train)
    if t_runs_days is not None:
        from data_source import train_runs_on_date
        if not train_runs_on_date(t_runs_days, run_day):
            return from_seq

    if classes is None:
        classes = t_classes if t_classes else av.CLASSES
    else:
        if t_classes:
            classes = tuple(c for c in classes if c in t_classes)
        if not classes:
            return from_seq

    s = from_seq
    while s < n_segments:
        if per_segment or FORCED_SEGMENT_SOLD_OUT or FORCED_SEGMENT_AVAILABLE:
            if not any_class(train, run_day, s, classes=classes):
                return s
            s += 1
        else:
            block = s // BLOCK
            if not any(segment_seats(train, run_day, c, s) > 0 for c in classes):
                return s
            s = (block + 1) * BLOCK
    return n_segments



def available_runs(from_seq, to_seq, seats_fn, cls):
    """Maximal stretches in [from_seq, to_seq) where `cls` has seats, as
    (start_seg, end_seg, fewest_seats_in_stretch). seats_fn(cls, seg) -> int."""
    runs, start, low = [], None, 0
    for s in range(from_seq, to_seq):
        try:
            n = seats_fn(cls, s)
        except (KeyError, IndexError):
            n = 0
        if n > 0:
            if start is None:
                start, low = s, n
            else:
                low = min(low, n)
        elif start is not None:
            runs.append((start, s, low))
            start = None
    if start is not None:
        runs.append((start, to_seq, low))
    return runs


def class_plan(from_seq, to_seq, seats_fn, classes=None):
    """Ways to ride from stop from_seq to stop to_seq.

    Returns (whole, plan):
      whole: {cls: fewest seats} for classes with a seat on every segment.
      plan:  [(from_seq, to_seq, cls, fewest_seats), ...] covering the trip
             with the fewest class changes, or None if some segment has no
             seat in any class.
    """
    classes = tuple(classes or av.CLASSES)
    runs = {c: available_runs(from_seq, to_seq, seats_fn, c) for c in classes}
    whole = {c: r[0][2] for c, r in runs.items()
             if len(r) == 1 and (r[0][0], r[0][1]) == (from_seq, to_seq)}

    plan, pos = [], from_seq
    while pos < to_seq:
        best = None
        for c in classes:
            for a, b, _ in runs[c]:
                if a <= pos < b and (best is None or b > best[1]):
                    best = (c, b)
        if best is None:
            return whole, None
        c, end = best
        low = min(seats_fn(c, s) for s in range(pos, end))
        plan.append((pos, end, c, low))
        pos = end
    return whole, plan
