"""Step 8: alternatives and ranking on top of routing.py."""
from routing import Journey, find_direct, find_routes

SORT_KEYS = {
    "fastest": lambda j: (j.duration, j.transfers, j.arrive),
    "earliest": lambda j: (j.arrive, j.transfers, j.duration),
    "fewest_changes": lambda j: (j.transfers, j.duration, j.arrive),
}


def _dominates(a, b):
    """a is at least as good as b on departure (later is better), arrival
    (earlier is better) and changes, and strictly better on at least one."""
    at = (-a.depart, a.arrive, a.transfers)
    bt = (-b.depart, b.arrive, b.transfers)
    return all(x <= y for x, y in zip(at, bt)) and at != bt


def remove_dominated(journeys):
    return [j for j in journeys if not any(_dominates(o, j) for o in journeys)]


def _key(j):
    return tuple((leg.train, leg.from_station, leg.to_station, leg.depart)
                 for leg in j.legs)


def collect_options(tt, src, dst, travel_date, earliest=0, max_searches=4, allow_live=True, **kw):
    """Run the search repeatedly, each time starting just after the earliest
    departure found so far, to surface later alternatives."""
    found = {}
    start = earliest
    for _ in range(max_searches):
        journeys = find_routes(tt, src, dst, travel_date, start, allow_live=allow_live, **kw)
        if not journeys:
            break
        for j in journeys:
            found.setdefault(_key(j), j)
        start = min(j.depart for j in journeys) + 1
        if start >= 1440:
            break
    return remove_dominated(list(found.values()))


def plan_options(tt, from_text, to_text, travel_date, earliest=0,
                 sort="fastest", limit=5, preferred_classes=None, allow_live=True, **kw):
    """Direct trains with seats if there are any, otherwise ranked multi-leg
    alternatives. sort is one of: fastest, earliest, fewest_changes."""
    src = tt.resolve_station(from_text)
    dst = tt.resolve_station(to_text)
    if not src:
        raise ValueError(f"I don't know a station called '{from_text}'.")
    if not dst:
        raise ValueError(f"I don't know a station called '{to_text}'.")
    if sort not in SORT_KEYS:
        raise ValueError(f"sort must be one of {sorted(SORT_KEYS)}")

    classes = tuple(preferred_classes) if preferred_classes else None
    direct_legs = find_direct(tt, src, dst, travel_date, earliest, preferred_classes=classes, allow_live=allow_live)
    direct = []
    if direct_legs:
        import fare_engine
        for leg in direct_legs:
            tot, bdown = fare_engine.calculate_journey_fare((leg,), preferred_classes=classes)
            direct.append(Journey((leg,), total_fare_inr=tot, fare_breakdown=bdown, fetched_at=leg.fetched_at))

    options = direct or collect_options(tt, src, dst, travel_date, earliest, preferred_classes=classes, allow_live=allow_live, **kw)
    return sorted(options, key=SORT_KEYS[sort])[:limit]
