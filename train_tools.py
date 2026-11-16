"""Tools the agent calls. Every tool returns a dict with `success` and `reason`;
booking and cancelling ask [y/N] first."""
import json
import uuid
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

import availability as av
import segments as seg
from data_source import Timetable
from options import plan_options

BOOKINGS_PATH = Path(__file__).parent / "data" / "bookings.json"
CLASSES = av.CLASSES

_tt = None


def _timetable() -> Timetable:
    global _tt
    if _tt is None:
        _tt = Timetable()
    return _tt


def _ok(reason: str, **extra) -> dict:
    return {"success": True, "reason": reason, **extra}


def _fail(reason: str, **extra) -> dict:
    return {"success": False, "reason": reason, **extra}


def _confirm(prompt: str) -> bool:
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _parse_date(text):
    try:
        return date.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def _parse_time(text):
    try:
        h, m = str(text).split(":")
        h, m = int(h), int(m)
    except ValueError:
        return None
    return h * 60 + m if 0 <= h < 24 and 0 <= m < 60 else None


def _clock(minutes: int) -> str:
    rem = minutes % 1440
    return f"{rem // 60:02d}:{rem % 60:02d}"


# ---------- bookings storage ----------
def _load_bookings() -> list:
    if not BOOKINGS_PATH.exists():
        return []
    return json.loads(BOOKINGS_PATH.read_text())


def _save_bookings(bookings: list) -> None:
    BOOKINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    BOOKINGS_PATH.write_text(json.dumps(bookings, indent=2))


# ---------- seats inside a train ----------
def _find_ride(tt: Timetable, train: str, from_codes, to_codes):
    """The first (board, alight) stops on `train` that run from one of from_codes
    to one of to_codes, or None."""
    stops = tt.stops_of(train)
    for board in stops:
        if board.station in from_codes and board.dep is not None:
            for alight in stops[board.seq + 1:]:
                if alight.station in to_codes:
                    return board, alight
    return None


def _booked_counts(tt: Timetable) -> Counter:
    """Confirmed bookings per (train, run date, class, segment)."""
    counts = Counter()
    for b in _load_bookings():
        if b["status"] != "confirmed":
            continue
        for leg in b["legs"]:
            cls = leg.get("travel_class", b.get("travel_class"))
            day = _parse_date(leg["date"])
            ride = _find_ride(tt, leg["train"], {leg["from_code"]}, {leg["to_code"]})
            if cls not in CLASSES or day is None or ride is None:
                continue
            board, alight = ride
            run_day = seg.run_date(day, board.dep)
            for s in range(board.seq, alight.seq):
                counts[(leg["train"], run_day, cls, s)] += 1
    return counts


def _seats_fn(train: str, run_day: date, booked: Counter):
    def fn(cls, s):
        return max(0, seg.segment_seats(train, run_day, cls, s)
                   - booked[(train, run_day, cls, s)])
    return fn


# ---------- describing journeys for the agent ----------
def _describe_journey(tt: Timetable, journey, travel_date: date) -> dict:
    legs = []
    for leg in journey.legs:
        dep_day = travel_date + timedelta(days=leg.depart // 1440)
        arr_day = travel_date + timedelta(days=leg.arrive // 1440)
        pieces = [{
            "from_code": p.from_station,
            "from_name": tt.station_name(p.from_station),
            "to_code": p.to_station,
            "to_name": tt.station_name(p.to_station),
            "travel_class": p.travel_class,
            "seats": p.seats,
            "date": (travel_date + timedelta(days=p.depart // 1440)).isoformat(),
            "departs": _clock(p.depart),
            "arrives": _clock(p.arrive),
        } for p in leg.split]
        legs.append({
            "train": leg.train,
            "train_name": leg.name,
            "from_code": leg.from_station,
            "from_name": tt.station_name(leg.from_station),
            "to_code": leg.to_station,
            "to_name": tt.station_name(leg.to_station),
            "date": dep_day.isoformat(),       # the date you board this train
            "departs": _clock(leg.depart),
            "arrives": _clock(leg.arrive),
            "arrives_date": arr_day.isoformat(),
            "seats": leg.seats,                # classes that cover the whole leg
            "needs_class_change": bool(pieces),
            "class_plan": pieces,              # stretches, each in its own class
        })
    return {
        "transfers": journey.transfers,
        "duration_min": journey.duration,
        "departs": _clock(journey.depart),
        "arrives": _clock(journey.arrive),
        "arrives_date": (travel_date + timedelta(days=journey.arrive // 1440)).isoformat(),
        "layovers_min": journey.layovers(),
        "legs": legs,
    }


# ---------- tools ----------
def search_trains(from_station: str, to_station: str, travel_date: str,
                  earliest: str = "00:00", sort: str = "fastest",
                  limit: int = 5) -> dict:
    """Direct trains you can ride, or ranked multi-leg alternatives if none."""
    d = _parse_date(travel_date)
    if d is None:
        return _fail("travel_date must be YYYY-MM-DD, for example 2026-11-13.")
    if d < date.today():
        return _fail("That date is in the past.")
    e = _parse_time(earliest)
    if e is None:
        return _fail("earliest must be a 24-hour time like 18:30.")
    tt = _timetable()
    try:
        options = plan_options(tt, from_station, to_station, d,
                               earliest=e, sort=sort, limit=limit)
    except ValueError as err:
        return _fail(str(err))
    if not options:
        return _fail("No route with seats was found for that day.", journeys=[])
    journeys = [_describe_journey(tt, j, d) for j in options]
    kind = "direct train(s)" if options[0].transfers == 0 else "route(s) with changes"
    return _ok(f"Found {len(journeys)} {kind}.", journeys=journeys)


def check_seats(train_number: str, travel_date: str, from_station: str = None,
                to_station: str = None, travel_class: str = None) -> dict:
    """Look inside one train: which classes have seats from one stop to another,
    where each class has seats along the way, and how to cover the trip by
    changing class if no single class does. Without from/to it covers the whole
    run. travel_date is the date you board (at from_station, or at the origin)."""
    d = _parse_date(travel_date)
    if d is None:
        return _fail("travel_date must be YYYY-MM-DD.")
    tt = _timetable()
    train = str(train_number)
    stops = tt.stops_of(train)
    if not stops:
        return _fail(f"Unknown train number {train_number}.")
    if travel_class is not None and travel_class not in CLASSES:
        return _fail(f"travel_class must be one of {list(CLASSES)}.")

    from_codes = set(tt.resolve_station(from_station)) if from_station else {stops[0].station}
    to_codes = set(tt.resolve_station(to_station)) if to_station else {stops[-1].station}
    if not from_codes:
        return _fail(f"I don't know a station called '{from_station}'.")
    if not to_codes:
        return _fail(f"I don't know a station called '{to_station}'.")
    ride = _find_ride(tt, train, from_codes, to_codes)
    if ride is None:
        return _fail(f"Train {train} does not run from that station to that station.")

    board, alight = ride
    run_day = seg.run_date(d, board.dep)
    fn = _seats_fn(train, run_day, _booked_counts(tt))
    classes = [travel_class] if travel_class else list(CLASSES)
    whole, plan = seg.class_plan(board.seq, alight.seq, fn, classes)

    def name(s):
        return tt.station_name(stops[s].station)

    def board_time(s):
        return stops[s].dep if stops[s].dep is not None else stops[s].arr

    stretches = {
        c: [{"from_code": stops[a].station, "from_name": name(a),
             "to_code": stops[b].station, "to_name": name(b), "seats": n}
            for a, b, n in seg.available_runs(board.seq, alight.seq, fn, c)]
        for c in classes
    }
    class_plan = []
    if not whole and plan:
        class_plan = [{
            "from_code": stops[a].station, "from_name": name(a),
            "to_code": stops[b].station, "to_name": name(b),
            "travel_class": c, "seats": n,
            "date": (run_day + timedelta(days=board_time(a) // 1440)).isoformat(),
        } for a, b, c, n in plan]

    where = f"{name(board.seq)} to {name(alight.seq)}"
    extra = {"from_code": board.station, "to_code": alight.station,
             "seats": whole, "stretches": stretches, "class_plan": class_plan}
    if whole:
        return _ok(f"Train {train}, {where} on {d}: one class covers the whole trip.", **extra)
    if class_plan:
        parts = ", then ".join(f"{p['travel_class']} from {p['from_name']} to {p['to_name']}"
                               for p in class_plan)
        return _ok(f"No single class covers {where} on train {train}, but you can ride it "
                   f"by changing class: {parts}.", **extra)
    return _fail(f"No seats on train {train} for {where} on {d}.", **extra)


def book_ticket(legs: list, travel_class: str = None, passenger_name: str = "",
                confirm=None) -> dict:
    """Book one or more connected legs. Each leg is a dict with train, from_code,
    to_code and date (the date you board that train), and optionally its own
    travel_class; travel_class is the default for legs that don't set one. To ride
    one train in different classes, pass one leg per stretch."""
    if not isinstance(legs, list) or not legs:
        return _fail("Give at least one leg to book.")
    if not str(passenger_name).strip():
        return _fail("I need the passenger's name.")

    tt = _timetable()
    booked = _booked_counts(tt)
    checked = []
    for i, leg in enumerate(legs, start=1):
        if not isinstance(leg, dict) or not all(
                k in leg for k in ("train", "from_code", "to_code", "date")):
            return _fail(f"Leg {i} needs train, from_code, to_code and date.")
        cls = leg.get("travel_class") or travel_class
        if cls not in CLASSES:
            return _fail(f"Leg {i}: travel_class must be one of {list(CLASSES)}.")
        day = _parse_date(leg["date"])
        if day is None:
            return _fail(f"Leg {i} has an invalid date.")
        train = str(leg["train"])
        if not tt.stops_of(train):
            return _fail(f"Leg {i}: unknown train {train}.")
        ride = _find_ride(tt, train, {leg["from_code"]}, {leg["to_code"]})
        if ride is None:
            return _fail(f"Leg {i}: train {train} does not run from "
                         f"{leg['from_code']} to {leg['to_code']}.")
        board, alight = ride
        run_day = seg.run_date(day, board.dep)
        fn = _seats_fn(train, run_day, booked)
        runs = seg.available_runs(board.seq, alight.seq, fn, cls)
        if not (len(runs) == 1 and (runs[0][0], runs[0][1]) == (board.seq, alight.seq)):
            return _fail(f"Leg {i}: no {cls} seats left on train {train} from "
                         f"{tt.station_name(board.station)} to "
                         f"{tt.station_name(alight.station)} on {day}.")
        checked.append({"train": train, "from_code": leg["from_code"],
                        "to_code": leg["to_code"], "date": day.isoformat(),
                        "travel_class": cls})
    for a, b in zip(checked, checked[1:]):
        if a["to_code"] != b["from_code"]:
            return _fail("The legs don't connect: each leg must start where the "
                         "previous one ends.")

    summary = "; ".join(
        f"{tt.train_name(c['train'])} ({c['train']}) {tt.station_name(c['from_code'])}"
        f" -> {tt.station_name(c['to_code'])} in {c['travel_class']} on {c['date']}"
        for c in checked)
    ask = confirm or _confirm
    if not ask(f"Book for {passenger_name}: {summary}?"):
        return _fail("Booking was not confirmed, so nothing was booked.")

    pnr = uuid.uuid4().hex[:10].upper()
    booking = {
        "pnr": pnr,
        "passenger": passenger_name.strip(),
        "travel_class": "+".join(dict.fromkeys(c["travel_class"] for c in checked)),
        "legs": checked,
        "status": "confirmed",
        "booked_at": datetime.now().isoformat(timespec="seconds"),
    }
    bookings = _load_bookings()
    bookings.append(booking)
    _save_bookings(bookings)
    return _ok(f"Booked. Your PNR is {pnr}. (Simulated booking.)", booking=booking)


def cancel_ticket(pnr: str, confirm=None) -> dict:
    bookings = _load_bookings()
    target = next((b for b in bookings
                   if b["pnr"] == str(pnr).upper() and b["status"] == "confirmed"), None)
    if target is None:
        return _fail(f"No active booking found with PNR {pnr}.")
    ask = confirm or _confirm
    if not ask(f"Cancel booking {target['pnr']} for {target['passenger']}?"):
        return _fail("Cancellation was not confirmed, so the booking stays.")
    target["status"] = "cancelled"
    _save_bookings(bookings)
    return _ok(f"Booking {target['pnr']} cancelled.", booking=target)


def list_bookings() -> dict:
    active = [b for b in _load_bookings() if b["status"] == "confirmed"]
    return _ok(f"{len(active)} active booking(s).", bookings=active)
