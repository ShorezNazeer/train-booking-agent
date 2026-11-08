"""Steps 6-7: route finding. Step 6 is the direct search; multi-leg comes next."""
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

import availability as av
from data_source import Timetable


@dataclass(frozen=True)
class Leg:
    train: str
    name: str
    from_station: str
    to_station: str
    depart: int   # minutes since midnight of the travel date
    arrive: int   # may exceed 1440 for overnight journeys
    seats: dict   # {class: seats left}

    @property
    def duration(self) -> int:
        return self.arrive - self.depart


def fmt_time(minutes: int) -> str:
    """Format minutes since midnight as HH:MM, with +Nd for later days."""
    days, rem = divmod(minutes, 1440)
    suffix = f" +{days}d" if days else ""
    return f"{rem // 60:02d}:{rem % 60:02d}{suffix}"


def find_direct(tt: Timetable, from_codes, to_codes,
                travel_date: date, earliest: int = 0) -> list:
    """Direct trains with seats on travel_date, leaving at or after `earliest`
    (minutes since midnight). Trains are assumed to run daily, and seats are
    looked up by the date you board."""
    legs = []
    for r in tt.direct_trains(from_codes, to_codes):
        depart = r["depart"] % 1440
        if depart < earliest:
            continue
        seats = av.availability(r["train"], travel_date)
        if not seats:
            continue
        legs.append(Leg(
            train=r["train"], name=r["name"],
            from_station=r["from"], to_station=r["to"],
            depart=depart, arrive=depart + r["duration_min"], seats=seats,
        ))
    return sorted(legs, key=lambda leg: leg.depart)


# ---------------- Step 7: multi-leg search ----------------
MIN_LAYOVER = 60        # minutes needed to change trains
MAX_LAYOVER = 12 * 60   # don't wait longer than this between trains
HORIZON = 36 * 60       # give up on journeys longer than this
INF = float("inf")


@dataclass(frozen=True)
class Journey:
    legs: tuple

    @property
    def depart(self) -> int:
        return self.legs[0].depart

    @property
    def arrive(self) -> int:
        return self.legs[-1].arrive

    @property
    def duration(self) -> int:
        return self.arrive - self.depart

    @property
    def transfers(self) -> int:
        return len(self.legs) - 1

    def layovers(self) -> list:
        return [b.depart - a.arrive for a, b in zip(self.legs, self.legs[1:])]


@dataclass
class _Label:
    arrive: int
    leg: Optional[Leg]
    parent: Optional["_Label"]


def find_routes(tt: Timetable, from_codes, to_codes, travel_date: date,
                earliest: int = 0, max_trains: int = 3,
                min_layover: int = MIN_LAYOVER, max_layover: int = MAX_LAYOVER,
                horizon: int = HORIZON) -> list:
    """Best journey for each number of trains (1..max_trains), earliest
    arrival first among equals. All times are minutes since midnight of
    travel_date. The first train must leave on travel_date."""
    to_set = set(to_codes)
    limit = earliest + horizon
    seat_cache = {}

    def seats_for(train, board_time):
        day = travel_date + timedelta(days=board_time // 1440)
        key = (train, day)
        if key not in seat_cache:
            seat_cache[key] = av.availability(train, day)
        return seat_cache[key]

    frontier = {c: _Label(earliest, None, None) for c in from_codes}
    best = {c: earliest for c in from_codes}   # earliest arrival seen per station
    journeys = []

    for k in range(1, max_trains + 1):
        new = {}
        for station, label in frontier.items():
            ready = label.arrive if k == 1 else label.arrive + min_layover
            for board in tt.trains_at(station):
                if board.dep is None:
                    continue
                if label.leg is not None and board.train == label.leg.train:
                    continue
                # earliest time this train leaves the station at or after `ready`
                depart = (ready // 1440) * 1440 + board.dep % 1440
                if depart < ready:
                    depart += 1440
                if k == 1:
                    if depart >= 1440:          # first train must leave on the travel date
                        continue
                elif depart - label.arrive > max_layover:
                    continue
                seats = seats_for(board.train, depart)
                if not seats:
                    continue
                for alight in tt.stops_of(board.train)[board.seq + 1:]:
                    if alight.arr is None:
                        continue
                    arrive = depart + (alight.arr - board.dep)
                    if arrive > limit:
                        break
                    if arrive < best.get(alight.station, INF):
                        best[alight.station] = arrive
                        new[alight.station] = _Label(arrive, Leg(
                            train=board.train, name=tt.train_name(board.train),
                            from_station=station, to_station=alight.station,
                            depart=depart, arrive=arrive, seats=seats), label)

        reached = [new[c] for c in to_set if c in new]
        if reached:
            lab = min(reached, key=lambda x: x.arrive)
            legs = []
            while lab.leg is not None:
                legs.append(lab.leg)
                lab = lab.parent
            journeys.append(Journey(tuple(reversed(legs))))
        for c in to_set:
            new.pop(c, None)        # no need to continue past the destination
        if not new:
            break
        frontier = new
    return journeys


def plan_trip(tt: Timetable, from_text: str, to_text: str, travel_date: date,
              earliest: int = 0, **kwargs) -> list:
    """Direct trains if any have seats; otherwise the best multi-leg routes."""
    src = tt.resolve_station(from_text)
    dst = tt.resolve_station(to_text)
    if not src:
        raise ValueError(f"I don't know a station called '{from_text}'.")
    if not dst:
        raise ValueError(f"I don't know a station called '{to_text}'.")
    direct = find_direct(tt, src, dst, travel_date, earliest)
    if direct:
        return [Journey((leg,)) for leg in direct]
    return find_routes(tt, src, dst, travel_date, earliest, **kwargs)
