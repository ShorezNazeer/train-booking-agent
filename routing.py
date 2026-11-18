"""Route finding: direct search, multi-leg search, and per-segment class plans."""
from dataclasses import dataclass
from datetime import date, timedelta
from typing import NamedTuple, Optional

import live_seats
import segments as seg
from data_source import Timetable


@dataclass(frozen=True)
class Piece:
    """One stretch of a leg that is booked in a single class."""
    from_station: str
    to_station: str
    travel_class: str
    seats: int
    depart: int   # minutes since midnight of the travel date
    arrive: int


@dataclass(frozen=True)
class Leg:
    train: str
    name: str
    from_station: str
    to_station: str
    depart: int   # minutes since midnight of the travel date
    arrive: int   # may exceed 1440 for overnight journeys
    seats: dict   # {class: seats left} for classes that cover the whole leg
    split: tuple = ()  # Pieces to book instead when no single class covers the leg

    @property
    def duration(self) -> int:
        return self.arrive - self.depart


def fmt_time(minutes: int) -> str:
    """Format minutes since midnight as HH:MM, with +Nd for later days."""
    days, rem = divmod(minutes, 1440)
    suffix = f" +{days}d" if days else ""
    return f"{rem // 60:02d}:{rem % 60:02d}{suffix}"


def _leg_seats(tt: Timetable, train: str, run_day: date, board_seq: int,
               alight_seq: int, leg_depart: int, board_dep: int):
    """Seats for riding `train` from stop board_seq to alight_seq.
    Returns (seats, split), where split is a tuple of Pieces used when no single
    class covers the whole ride, or None if some stretch has no seat at all."""
    def seats_fn(cls, s):
        return seg.segment_seats(train, run_day, cls, s)

    whole, plan = seg.class_plan(board_seq, alight_seq, seats_fn)
    if whole:
        return whole, ()
    if plan is None:
        return None
    stops = tt.stops_of(train)
    pieces = []
    for a, b, cls, n in plan:
        sa, sb = stops[a], stops[b]
        dep_a = sa.dep if sa.dep is not None else sa.arr
        arr_b = sb.arr if sb.arr is not None else sb.dep
        pieces.append(Piece(sa.station, sb.station, cls, n,
                            leg_depart + dep_a - board_dep,
                            leg_depart + arr_b - board_dep))
    return {}, tuple(pieces)


def find_direct(tt: Timetable, from_codes, to_codes,
                travel_date: date, earliest: int = 0) -> list:
    """Direct trains you can ride on travel_date, leaving at or after `earliest`
    (minutes since midnight). A train counts if one class has seats for the whole
    ride, or if the ride can be covered by changing class along the way.
    Trains are assumed to run daily."""
    legs = []
    for r in tt.direct_trains(from_codes, to_codes):
        depart = r["depart"] % 1440          # time of day you board
        if depart < earliest:
            continue
        run_day = seg.run_date(travel_date, r["depart"])
        got = live_seats.materialize_leg_seats(
            tt,
            train=r["train"],
            from_station=r["from"],
            to_station=r["to"],
            run_day=run_day,
            board_seq=r["from_seq"],
            alight_seq=r["to_seq"],
            leg_depart=depart,
            board_dep=r["depart"],
        )
        if got is None:
            continue
        seats, split = got
        legs.append(Leg(
            train=r["train"], name=r["name"],
            from_station=r["from"], to_station=r["to"],
            depart=depart, arrive=depart + r["duration_min"],
            seats=seats, split=split,
        ))
    return sorted(legs, key=lambda leg: leg.depart)


# ---------------- multi-leg search ----------------
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


class _RawLeg(NamedTuple):
    """A leg found during the search; seats are worked out later, only for the
    legs that end up in a result."""
    train: str
    from_station: str
    to_station: str
    depart: int
    arrive: int
    board_seq: int
    alight_seq: int
    board_dep: int
    run_day: date


@dataclass
class _Label:
    arrive: int
    leg: Optional[_RawLeg]
    parent: Optional["_Label"]


def _materialize(tt: Timetable, raw: _RawLeg) -> Leg:
    got = live_seats.materialize_leg_seats(
        tt,
        train=raw.train,
        from_station=raw.from_station,
        to_station=raw.to_station,
        run_day=raw.run_day,
        board_seq=raw.board_seq,
        alight_seq=raw.alight_seq,
        leg_depart=raw.depart,
        board_dep=raw.board_dep,
    )
    assert got is not None, f"no seats on a leg the search accepted: {raw}"
    seats, split = got
    return Leg(train=raw.train, name=tt.train_name(raw.train),
               from_station=raw.from_station, to_station=raw.to_station,
               depart=raw.depart, arrive=raw.arrive, seats=seats, split=split)


def find_routes(tt: Timetable, from_codes, to_codes, travel_date: date,
                earliest: int = 0, max_trains: int = 3,
                min_layover: int = MIN_LAYOVER, max_layover: int = MAX_LAYOVER,
                horizon: int = HORIZON) -> list:
    """Best journey for each number of trains (1..max_trains), earliest
    arrival first among equals. All times are minutes since midnight of
    travel_date. The first train must leave on travel_date. A leg may need a
    class change along the way (see Leg.split)."""
    to_set = set(to_codes)
    limit = earliest + horizon
    forced = seg.forced_trains()

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
                stops = tt.stops_of(board.train)
                run_day = None
                dead = 0
                for alight in stops[board.seq + 1:]:
                    if alight.arr is None:
                        continue
                    arrive = depart + (alight.arr - board.dep)
                    if arrive > limit:
                        break
                    if arrive < best.get(alight.station, INF):
                        if run_day is None:
                            # first hop that no class can carry: can't ride past it
                            run_day = seg.run_date(
                                travel_date + timedelta(days=depart // 1440), board.dep)
                            dead = seg.first_dead_segment(
                                board.train, run_day, board.seq, len(stops) - 1,
                                board.train in forced)
                        if alight.seq - 1 >= dead:
                            break
                        best[alight.station] = arrive
                        new[alight.station] = _Label(arrive, _RawLeg(
                            board.train, station, alight.station, depart, arrive,
                            board.seq, alight.seq, board.dep, run_day), label)

        reached = [new[c] for c in to_set if c in new]
        if reached:
            lab = min(reached, key=lambda x: x.arrive)
            legs = []
            while lab.leg is not None:
                legs.append(_materialize(tt, lab.leg))
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
    """Direct trains if any can be ridden; otherwise the best multi-leg routes."""
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
