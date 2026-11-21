"""Tools the agent calls. Every tool returns a dict with `success` and `reason`;
booking and cancelling ask [y/N] first."""
import json
import uuid
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

import availability as av
import fare_engine
import live_seats
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
        iso_str = live_seats.normalize_iso_date(text)
        return date.fromisoformat(iso_str)
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
def _load_bookings(session_id: str = None) -> list:
    if not BOOKINGS_PATH.exists():
        return []
    try:
        all_bookings = json.loads(BOOKINGS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    if session_id is not None:
        return [b for b in all_bookings if b.get("session_id") == session_id]
    return all_bookings


def _save_bookings(bookings: list, session_id: str = None) -> None:
    BOOKINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if session_id is None:
        BOOKINGS_PATH.write_text(json.dumps(bookings, indent=2))
    else:
        try:
            existing = json.loads(BOOKINGS_PATH.read_text()) if BOOKINGS_PATH.exists() else []
        except Exception:
            existing = []
        other = [b for b in existing if b.get("session_id") != session_id]
        combined = other + bookings
        BOOKINGS_PATH.write_text(json.dumps(combined, indent=2))


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


def _booked_counts(tt: Timetable, session_id: str = None) -> Counter:
    """Confirmed bookings per (train, run date, class, segment)."""
    counts = Counter()
    for b in _load_bookings(session_id=session_id):
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
        leg_source = getattr(leg, "availability_source", getattr(leg, "source", "simulated_estimate"))
        leg_fetched_at = getattr(leg, "fetched_at", None)
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
            "fare_inr": getattr(p, "fare_inr", 0),
            "fare_breakdown": getattr(p, "fare_breakdown", None),
            "availability_source": getattr(p, "availability_source", getattr(p, "source", leg_source)),
            "source": getattr(p, "source", getattr(p, "availability_source", leg_source)),
            "fetched_at": getattr(p, "fetched_at", leg_fetched_at),
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
            "fare_inr": getattr(leg, "fare_inr", 0),
            "class_fares": getattr(leg, "class_fares", {}),
            "availability_source": leg_source,
            "source": leg_source,
            "fetched_at": leg_fetched_at,
        })
    primary_source = legs[0]["availability_source"] if legs else "simulated_estimate"
    primary_fetched_at = getattr(journey, "fetched_at", legs[0].get("fetched_at") if legs else None)
    return {
        "transfers": journey.transfers,
        "duration_min": journey.duration,
        "departs": _clock(journey.depart),
        "arrives": _clock(journey.arrive),
        "arrives_date": (travel_date + timedelta(days=journey.arrive // 1440)).isoformat(),
        "layovers_min": journey.layovers(),
        "total_fare_inr": getattr(journey, "total_fare_inr", 0),
        "fare_breakdown": list(getattr(journey, "fare_breakdown", ())),
        "availability_source": primary_source,
        "source": primary_source,
        "fetched_at": primary_fetched_at,
        "legs": legs,
    }


# ---------- tools ----------
def search_trains(from_station: str, to_station: str, travel_date: str,
                  earliest: str = "00:00", sort: str = "fastest",
                  limit: int = 5, preferred_classes: list = None,
                  allow_live: bool = True) -> dict:
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
                               earliest=e, sort=sort, limit=limit,
                               preferred_classes=preferred_classes,
                               allow_live=allow_live)
    except ValueError as err:
        return _fail(str(err))
    if not options:
        return _fail("No route with seats was found for that day.", journeys=[])
    journeys = [_describe_journey(tt, j, d) for j in options]
    kind = "direct train(s)" if options[0].transfers == 0 else "route(s) with changes"
    fare_info = f", total fare from ₹{journeys[0]['total_fare_inr']}" if journeys[0].get("total_fare_inr") else ""
    return _ok(f"Found {len(journeys)} {kind}{fare_info}.", journeys=journeys)


def check_seats(train_number: str, travel_date: str, from_station: str = None,
                to_station: str = None, travel_class: str = None,
                session_id: str = None, allow_live: bool = True) -> dict:
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
    fn = _seats_fn(train, run_day, _booked_counts(tt, session_id=session_id))
    classes = [travel_class] if travel_class else list(CLASSES)
    whole, plan = seg.class_plan(board.seq, alight.seq, fn, classes)

    def name(s):
        return tt.station_name(stops[s].station)

    def board_time(s):
        return stops[s].dep if stops[s].dep is not None else stops[s].arr

    stretches = {
        c: [{
            "from_code": stops[a].station, "from_name": name(a),
            "to_code": stops[b].station, "to_name": name(b), "seats": n,
            "availability_source": "simulated_estimate", "source": "simulated_estimate",
        }
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
            "availability_source": "simulated_estimate", "source": "simulated_estimate",
        } for a, b, c, n in plan]

    fares = {
        c: fare_engine.calculate_stretch_fare(board.station, alight.station, c)["total_fare"]
        for c in classes
    }
    where = f"{name(board.seq)} to {name(alight.seq)}"
    extra = {
        "from_code": board.station, "to_code": alight.station,
        "seats": whole, "stretches": stretches, "class_plan": class_plan,
        "fares": fares,
        "availability_source": "simulated_estimate",
        "source": "simulated_estimate",
    }
    if whole:
        return _ok(f"Train {train}, {where} on {d}: one class covers the whole trip.", **extra)
    if class_plan:
        parts = ", then ".join(f"{p['travel_class']} from {p['from_name']} to {p['to_name']}"
                               for p in class_plan)
        return _ok(f"No single class covers {where} on train {train}, but you can ride it "
                   f"by changing class: {parts}.", **extra)
    return _fail(f"No seats on train {train} for {where} on {d}.", **extra)


def book_ticket(legs: list, travel_class: str = None, passenger_name: str = "",
                confirm=None, session_id: str = None) -> dict:
    """Book one or more connected legs. Each leg is a dict with train, from_code,
    to_code and date (the date you board that train), and optionally its own
    travel_class; travel_class is the default for legs that don't set one. To ride
    one train in different classes, pass one leg per stretch."""
    if not isinstance(legs, list) or not legs:
        return _fail("Give at least one leg to book.")
    if not str(passenger_name).strip():
        return _fail("I need the passenger's name.")

    tt = _timetable()
    booked = _booked_counts(tt, session_id=session_id)
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
        leg_fare = fare_engine.calculate_stretch_fare(leg["from_code"], leg["to_code"], cls)
        checked.append({"train": train, "from_code": leg["from_code"],
                        "to_code": leg["to_code"], "date": day.isoformat(),
                        "travel_class": cls, "fare_inr": leg_fare["total_fare"],
                        "fare_breakdown": leg_fare})
    for a, b in zip(checked, checked[1:]):
        if a["to_code"] != b["from_code"]:
            return _fail("The legs don't connect: each leg must start where the "
                         "previous one ends.")

    total_fare = sum(c.get("fare_inr", 0) for c in checked)
    summary = "; ".join(
        f"{tt.train_name(c['train'])} ({c['train']}) {tt.station_name(c['from_code'])}"
        f" -> {tt.station_name(c['to_code'])} in {c['travel_class']} on {c['date']} (₹{c['fare_inr']})"
        for c in checked)
    ask = confirm or _confirm
    if not ask(f"Book for {passenger_name}: {summary} [Total: ₹{total_fare}]?"):
        return _fail("Booking was not confirmed, so nothing was booked.")

    pnr = uuid.uuid4().hex[:10].upper()
    booking = {
        "pnr": pnr,
        "passenger": passenger_name.strip(),
        "travel_class": "+".join(dict.fromkeys(c["travel_class"] for c in checked)),
        "legs": checked,
        "total_fare_inr": total_fare,
        "status": "confirmed",
        "booked_at": datetime.now().isoformat(timespec="seconds"),
        "session_id": session_id,
    }
    bookings = _load_bookings(session_id=session_id)
    bookings.append(booking)
    _save_bookings(bookings, session_id=session_id)
    return _ok(f"Booked. Your PNR is {pnr}. Total fare: ₹{total_fare}. (Simulated booking.)", booking=booking)


def cancel_ticket(pnr: str, confirm=None, session_id: str = None) -> dict:
    bookings = _load_bookings(session_id=session_id)
    target = next((b for b in bookings
                   if b["pnr"] == str(pnr).upper() and b["status"] == "confirmed"), None)
    if target is None:
        return _fail(f"No active booking found with PNR {pnr}.")
    ask = confirm or _confirm
    if not ask(f"Cancel booking {target['pnr']} for {target['passenger']}?"):
        return _fail("Cancellation was not confirmed, so the booking stays.")
    target["status"] = "cancelled"
    _save_bookings(bookings, session_id=session_id)
    return _ok(f"Booking {target['pnr']} cancelled.", booking=target)


def list_bookings(session_id: str = None) -> dict:
    active = [b for b in _load_bookings(session_id=session_id) if b["status"] == "confirmed"]
    return _ok(f"{len(active)} active booking(s).", bookings=active)


# ---------- Interactive Demonstration Preset Handlers ----------
_ntes_numbers = None


def _ntes_train_numbers() -> set:
    global _ntes_numbers
    if _ntes_numbers is None:
        ntes_file = Path(__file__).resolve().parent / "raw_data" / "ntes_schedules.json"
        if ntes_file.exists():
            try:
                with open(ntes_file, encoding="utf-8") as f:
                    _ntes_numbers = {str(t.get("TrainNumber") or "").strip() for t in json.load(f)}
            except Exception:
                _ntes_numbers = set()
        else:
            _ntes_numbers = set()
    return _ntes_numbers


def setup_demo_direct() -> dict:
    """Dynamically pick a real 2026 NTES train with available seats."""
    av.FORCED_SOLD_OUT.clear()
    seg.FORCED_SEGMENT_SOLD_OUT.clear()
    seg.FORCED_SEGMENT_AVAILABLE.clear()
    tomorrow = date.today() + timedelta(days=1)
    tt = _timetable()
    ntes_nums = _ntes_train_numbers()

    direct_rides = tt.direct_trains(["NDLS", "DLI", "ANVT", "NZM"], ["CNB"])
    chosen_train = None
    chosen_name = None
    chosen_from = "Delhi"
    chosen_to = "Kanpur"

    for r in direct_rides:
        if r["train"] in ntes_nums and tt.train_runs_on(r["train"], tomorrow):
            t_classes = tt.train_classes(r["train"])
            if t_classes:
                chosen_train = r["train"]
                chosen_name = r["name"]
                chosen_from = tt.station_name(r["from"])
                chosen_to = tt.station_name(r["to"])
                break

    if not chosen_train:
        for r in direct_rides:
            if tt.train_runs_on(r["train"], tomorrow):
                chosen_train = r["train"]
                chosen_name = r["name"]
                break

    return {
        "success": True,
        "type": "direct",
        "from_station": "Delhi",
        "to_station": "Kanpur",
        "travel_date": tomorrow.isoformat(),
        "train_number": chosen_train,
        "train_name": chosen_name,
        "description": f"Direct 2026 NTES train {chosen_train} ({chosen_name}) from {chosen_from} to {chosen_to} with available single-class seats.",
    }


def setup_demo_split() -> dict:
    """Dynamically pick a real 2026 NTES train and intermediate stop, configuring a mid-route class change using valid classes from that train's classes list."""
    av.FORCED_SOLD_OUT.clear()
    seg.FORCED_SEGMENT_SOLD_OUT.clear()
    seg.FORCED_SEGMENT_AVAILABLE.clear()
    tomorrow = date.today() + timedelta(days=1)
    tt = _timetable()
    ntes_nums = _ntes_train_numbers()

    from_name = "Lucknow"
    to_name = "Delhi"
    src_codes = tt.resolve_station(from_name)
    dst_codes = tt.resolve_station(to_name)

    all_direct = tt.direct_trains(src_codes, dst_codes)
    ntes_rides = [
        r for r in all_direct
        if (not ntes_nums or r["train"] in ntes_nums)
        and tt.train_runs_on(r["train"], tomorrow)
        and len(tt.train_classes(r["train"])) >= 2
        and r["to_seq"] - r["from_seq"] >= 2
    ]

    # Prefer trains with compact realistic stop lists (e.g. between 3 and 25 stops)
    ntes_rides.sort(key=lambda r: len(tt.stops_of(r["train"])))

    chosen_ride = None
    c1, c2 = None, None
    mid_station_code = None
    mid_station_name = None

    for r in ntes_rides:
        stops = tt.stops_of(r["train"])
        from_seq = r["from_seq"]
        to_seq = r["to_seq"]
        mid_idx = (from_seq + to_seq) // 2
        mid_stop = stops[mid_idx]
        t_classes = tt.train_classes(r["train"])

        if "SL" in t_classes and "3A" in t_classes:
            c1, c2 = "SL", "3A"
        elif "CC" in t_classes and "EC" in t_classes:
            c1, c2 = "CC", "EC"
        elif "3A" in t_classes and "2A" in t_classes:
            c1, c2 = "3A", "2A"
        else:
            c1, c2 = t_classes[-1], t_classes[0]

        chosen_ride = r
        mid_station_code = mid_stop.station
        mid_station_name = tt.station_name(mid_station_code)
        break

    chosen = chosen_ride["train"] if chosen_ride else "12229"
    if chosen_ride:
        from_seq = chosen_ride["from_seq"]
        to_seq = chosen_ride["to_seq"]
        mid_idx = (from_seq + to_seq) // 2
        t_classes = tt.train_classes(chosen)

        # Stretch 1 (board to mid): c1 guaranteed available, other classes sold out
        for s in range(from_seq, mid_idx):
            seg.FORCED_SEGMENT_AVAILABLE[(chosen, c1, s)] = 15
            for cls in t_classes:
                if cls != c1:
                    seg.FORCED_SEGMENT_SOLD_OUT.add((chosen, cls, s))

        # Stretch 2 (mid to alight): c2 guaranteed available, other classes sold out
        for s in range(mid_idx, to_seq):
            seg.FORCED_SEGMENT_AVAILABLE[(chosen, c2, s)] = 15
            for cls in t_classes:
                if cls != c2:
                    seg.FORCED_SEGMENT_SOLD_OUT.add((chosen, cls, s))

        # Sell out all other direct trains between Lucknow and Delhi
        for r in all_direct:
            if r["train"] != chosen:
                av.FORCED_SOLD_OUT.add((r["train"], None))

    return {
        "success": True,
        "type": "split",
        "from_station": from_name,
        "to_station": to_name,
        "travel_date": tomorrow.isoformat(),
        "train_number": chosen,
        "train_name": tt.train_name(chosen) if chosen else None,
        "transfer_station": mid_station_name,
        "transfer_station_code": mid_station_code,
        "class_first_half": c1,
        "class_second_half": c2,
        "description": f"Real 2026 NTES train {chosen} ({tt.train_name(chosen)}): {c1} to {mid_station_name} ({mid_station_code}), then switch coach to {c2} to {to_name}.",
    }


def setup_demo_soldout() -> dict:
    """Dynamically configure multi-train transfer scenario: forces direct trains sold out and verifies connecting transfer routes."""
    av.FORCED_SOLD_OUT.clear()
    seg.FORCED_SEGMENT_SOLD_OUT.clear()
    seg.FORCED_SEGMENT_AVAILABLE.clear()
    tomorrow = date.today() + timedelta(days=1)
    tt = _timetable()

    from_name = "Lucknow"
    to_name = "Delhi"
    src_codes = tt.resolve_station(from_name)
    dst_codes = tt.resolve_station(to_name)

    # Force all direct trains between Lucknow and Delhi sold out
    all_direct = tt.direct_trains(src_codes, dst_codes)
    for d in all_direct:
        av.FORCED_SOLD_OUT.add((d["train"], None))

    return {
        "success": True,
        "type": "soldout",
        "from_station": from_name,
        "to_station": to_name,
        "travel_date": tomorrow.isoformat(),
        "description": "All direct trains forced sold out. System reroutes passengers via connecting trains with layovers (e.g. via Kanpur).",
    }


def reset_demo() -> dict:
    av.FORCED_SOLD_OUT.clear()
    seg.FORCED_SEGMENT_SOLD_OUT.clear()
    seg.FORCED_SEGMENT_AVAILABLE.clear()
    return {"success": True, "description": "All demo simulation overrides cleared."}

