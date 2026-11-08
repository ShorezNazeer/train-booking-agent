"""Step 10: tools the agent will call. Every tool returns a dict with
`success` and `reason`; booking and cancelling ask [y/N] first."""
import json
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import availability as av
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


def _seats_left(train: str, day: date, cls: str) -> int:
    base = av.seats_available(train, day, cls)
    booked = sum(
        1
        for b in _load_bookings()
        if b["status"] == "confirmed" and b["travel_class"] == cls
        for leg in b["legs"]
        if leg["train"] == train and leg["date"] == day.isoformat()
    )
    return max(0, base - booked)


# ---------- describing journeys for the agent ----------
def _describe_journey(tt: Timetable, journey, travel_date: date) -> dict:
    legs = []
    for leg in journey.legs:
        dep_day = travel_date + timedelta(days=leg.depart // 1440)
        arr_day = travel_date + timedelta(days=leg.arrive // 1440)
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
            "seats": leg.seats,
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
    """Direct trains with seats, or ranked multi-leg alternatives if none."""
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


def check_seats(train_number: str, travel_date: str,
                travel_class: str = None) -> dict:
    """Seats left on a train, per class (or for one class)."""
    d = _parse_date(travel_date)
    if d is None:
        return _fail("travel_date must be YYYY-MM-DD.")
    tt = _timetable()
    if not tt.stops_of(str(train_number)):
        return _fail(f"Unknown train number {train_number}.")
    if travel_class is not None and travel_class not in CLASSES:
        return _fail(f"travel_class must be one of {list(CLASSES)}.")
    classes = [travel_class] if travel_class else list(CLASSES)
    seats = {c: _seats_left(str(train_number), d, c) for c in classes}
    return _ok(f"Seats on train {train_number} for {d}.", seats=seats)


def book_ticket(legs: list, travel_class: str, passenger_name: str,
                confirm=None) -> dict:
    """Book one or more connected legs. Each leg is a dict with train,
    from_code, to_code and date (the date you board that train)."""
    if not isinstance(legs, list) or not legs:
        return _fail("Give at least one leg to book.")
    if travel_class not in CLASSES:
        return _fail(f"travel_class must be one of {list(CLASSES)}.")
    if not str(passenger_name).strip():
        return _fail("I need the passenger's name.")

    tt = _timetable()
    checked = []
    for i, leg in enumerate(legs, start=1):
        if not isinstance(leg, dict) or not all(
                k in leg for k in ("train", "from_code", "to_code", "date")):
            return _fail(f"Leg {i} needs train, from_code, to_code and date.")
        day = _parse_date(leg["date"])
        if day is None:
            return _fail(f"Leg {i} has an invalid date.")
        train = str(leg["train"])
        stops = tt.stops_of(train)
        if not stops:
            return _fail(f"Leg {i}: unknown train {train}.")
        board = next((s for s in stops
                      if s.station == leg["from_code"] and s.dep is not None), None)
        if board is None or not any(
                s.station == leg["to_code"] and s.seq > board.seq for s in stops):
            return _fail(f"Leg {i}: train {train} does not run from "
                         f"{leg['from_code']} to {leg['to_code']}.")
        if _seats_left(train, day, travel_class) < 1:
            return _fail(f"Leg {i}: no {travel_class} seats left on train "
                         f"{train} on {day}.")
        checked.append({"train": train, "from_code": leg["from_code"],
                        "to_code": leg["to_code"], "date": day.isoformat()})
    for a, b in zip(checked, checked[1:]):
        if a["to_code"] != b["from_code"]:
            return _fail("The legs don't connect: each leg must start where the "
                         "previous one ends.")

    summary = "; ".join(
        f"{tt.train_name(c['train'])} ({c['train']}) {tt.station_name(c['from_code'])}"
        f" -> {tt.station_name(c['to_code'])} on {c['date']}" for c in checked)
    ask = confirm or _confirm
    if not ask(f"Book {travel_class} for {passenger_name}: {summary}?"):
        return _fail("Booking was not confirmed, so nothing was booked.")

    pnr = uuid.uuid4().hex[:10].upper()
    booking = {
        "pnr": pnr,
        "passenger": passenger_name.strip(),
        "travel_class": travel_class,
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
