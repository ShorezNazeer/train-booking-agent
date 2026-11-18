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

# Demo/testing hook: (train, travel_class, segment) entries forced sold out.
FORCED_SEGMENT_SOLD_OUT: set = set()


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
    return 1 + int(scaled * (av.CAPACITY[cls] - 1))


def segment_seats(train, run_day, cls, s):
    """Seats left in one class on segment s of this train's run."""
    if av.FORCED_SOLD_OUT or FORCED_SEGMENT_SOLD_OUT:
        if ((train, None) in av.FORCED_SOLD_OUT
                or (train, run_day.isoformat()) in av.FORCED_SOLD_OUT
                or (train, cls, s) in FORCED_SEGMENT_SOLD_OUT):
            return 0
    return _block_seats(train, run_day, cls, s // BLOCK, av.SOLD_OUT_CHANCE)


def any_class(train, run_day, s, classes=None):
    """True if at least one class has a seat on segment s."""
    classes = classes or av.CLASSES
    return any(segment_seats(train, run_day, c, s) > 0 for c in classes)


def forced_trains():
    """Trains that have per-segment overrides (demo/test hook)."""
    return frozenset(t for t, _, _ in FORCED_SEGMENT_SOLD_OUT)


def first_dead_segment(train, run_day, from_seq, n_segments, per_segment=False, classes=None):
    """Index of the first segment at or after from_seq that no class can carry
    (n_segments if there is none). Availability is constant inside a block, so
    this checks once per block unless the train has per-segment overrides."""
    if (train, None) in av.FORCED_SOLD_OUT or (train, run_day.isoformat()) in av.FORCED_SOLD_OUT:
        return from_seq
    classes = classes or av.CLASSES
    s = from_seq
    while s < n_segments:
        if per_segment:
            if not any_class(train, run_day, s, classes=classes):
                return s
            s += 1
        else:
            block = s // BLOCK
            if not any(_block_seats(train, run_day, c, block, av.SOLD_OUT_CHANCE) > 0
                       for c in classes):
                return s
            s = (block + 1) * BLOCK
    return n_segments


def available_runs(from_seq, to_seq, seats_fn, cls):
    """Maximal stretches in [from_seq, to_seq) where `cls` has seats, as
    (start_seg, end_seg, fewest_seats_in_stretch). seats_fn(cls, seg) -> int."""
    runs, start, low = [], None, 0
    for s in range(from_seq, to_seq):
        n = seats_fn(cls, s)
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
