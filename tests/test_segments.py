from datetime import date, timedelta

import pytest

import availability as av
import segments as seg
import train_tools as tools
from routing import find_direct, find_routes
from test_routing import hm, make_timetable

D = date(2026, 11, 13)

# One train A -> B -> C -> D -> E: segments 0 (A-B), 1 (B-C), 2 (C-D), 3 (D-E).
LINE = {
    "T": [("A", None, hm(8)), ("B", hm(9), hm(9, 5)), ("C", hm(10), hm(10, 5)),
          ("D", hm(11), hm(11, 5)), ("E", hm(12), None)],
}


@pytest.fixture(autouse=True)
def plenty_of_seats(monkeypatch):
    monkeypatch.setattr(av, "SOLD_OUT_CHANCE", 0.0)
    monkeypatch.setattr(av, "FORCED_SOLD_OUT", set())
    monkeypatch.setattr(seg, "FORCED_SEGMENT_SOLD_OUT", set())
    seg._block_seats.cache_clear()      # cached seat counts must not leak between tests
    yield
    seg._block_seats.cache_clear()


def force(train, cls, segments):
    for s in segments:
        seg.FORCED_SEGMENT_SOLD_OUT.add((train, cls, s))


def force_split_shape(train="T"):
    """SL only A->C, 3A only C->E, 2A nowhere: only a class change works."""
    for c in av.CLASSES:
        if c not in ("SL", "3A"):
            force(train, c, [0, 1, 2, 3])
    force(train, "SL", [2, 3])
    force(train, "3A", [0, 1])


# ---------- the segment model ----------
def test_run_date_for_overnight_boarding():
    assert seg.run_date(D, hm(22)) == D
    assert seg.run_date(D + timedelta(days=1), 1440 + hm(0, 40)) == D


def test_availability_is_the_same_within_a_block():
    day = D
    values = {seg.segment_seats("T", day, "SL", s) for s in range(seg.BLOCK)}
    assert len(values) == 1


def test_forced_sold_out_applies_to_every_segment():
    av.FORCED_SOLD_OUT.add(("T", None))
    assert not seg.any_class("T", D, 0)


def test_class_plan_whole_class():
    whole, plan = seg.class_plan(0, 4, lambda c, s: 5)
    assert set(whole) == set(av.CLASSES)
    assert plan[0][:2] == (0, 4)


def test_class_plan_split():
    seats = {"SL": [3, 3, 0, 0], "3A": [0, 0, 7, 7], "2A": [0, 0, 0, 0]}
    whole, plan = seg.class_plan(0, 4, lambda c, s: seats[c][s])
    assert whole == {}
    assert plan == [(0, 2, "SL", 3), (2, 4, "3A", 7)]


def test_class_plan_uses_fewest_changes():
    seats = {"SL": [2, 2, 0, 0], "3A": [0, 2, 2, 0], "2A": [0, 0, 2, 2]}
    _, plan = seg.class_plan(0, 4, lambda c, s: seats[c][s])
    assert [(p[0], p[1], p[2]) for p in plan] == [(0, 2, "SL"), (2, 4, "2A")]


def test_class_plan_none_when_a_stretch_has_no_seats():
    seats = {"SL": [1, 0, 1], "3A": [1, 0, 1], "2A": [1, 0, 1]}
    whole, plan = seg.class_plan(0, 3, lambda c, s: seats[c][s])
    assert whole == {} and plan is None


# ---------- routing with class plans ----------
def test_direct_prefers_a_single_class_when_one_exists(tmp_path):
    tt = make_timetable(tmp_path, LINE)
    leg = find_direct(tt, ["A"], ["E"], D)[0]
    assert leg.seats and leg.split == ()


def test_direct_finds_a_class_change(tmp_path):
    tt = make_timetable(tmp_path, LINE)
    force_split_shape()
    leg = find_direct(tt, ["A"], ["E"], D)[0]
    assert leg.seats == {}
    assert [(p.from_station, p.to_station, p.travel_class) for p in leg.split] == [
        ("A", "C", "SL"), ("C", "E", "3A")]
    assert (leg.split[0].depart, leg.split[0].arrive) == (hm(8), hm(10))
    assert (leg.split[1].depart, leg.split[1].arrive) == (hm(10, 5), hm(12))


def test_direct_skipped_when_a_stretch_has_no_seats_at_all(tmp_path):
    tt = make_timetable(tmp_path, LINE)
    for cls in av.CLASSES:
        force("T", cls, [1])
    assert find_direct(tt, ["A"], ["E"], D) == []
    assert len(find_direct(tt, ["A"], ["B"], D)) == 1      # before the gap
    assert len(find_direct(tt, ["C"], ["E"], D)) == 1      # after the gap


def test_overnight_seats_follow_the_run_date(tmp_path):
    trains = {"N": [("S", None, hm(22)), ("M", 1440 + hm(0, 30), 1440 + hm(0, 40)),
                    ("T", 1440 + hm(2), None)]}
    tt = make_timetable(tmp_path, trains)
    av.FORCED_SOLD_OUT.add(("N", D.isoformat()))             # the run that left on D
    assert find_direct(tt, ["M"], ["T"], D + timedelta(days=1)) == []
    assert len(find_direct(tt, ["M"], ["T"], D + timedelta(days=2))) == 1


def test_multi_leg_uses_a_class_change_on_one_leg(tmp_path):
    trains = dict(LINE)
    trains["U"] = [("E", None, hm(14)), ("F", hm(16), None)]
    tt = make_timetable(tmp_path, trains)
    force_split_shape()
    j = find_routes(tt, ["A"], ["F"], D)[0]
    assert [leg.train for leg in j.legs] == ["T", "U"]
    assert len(j.legs[0].split) == 2 and j.legs[1].split == ()


# ---------- tools ----------
@pytest.fixture
def toolbox(tmp_path, monkeypatch):
    tt = make_timetable(tmp_path, LINE)
    monkeypatch.setattr(tools, "_tt", tt)
    monkeypatch.setattr(tools, "BOOKINGS_PATH", tmp_path / "bookings.json")
    return tt


YES = lambda prompt: True
LATER = (date.today() + timedelta(days=5)).isoformat()


def test_check_seats_reports_the_class_change(toolbox):
    force_split_shape()
    r = tools.check_seats("T", LATER, from_station="A", to_station="E")
    assert r["success"] and r["seats"] == {}
    assert [(p["from_code"], p["to_code"], p["travel_class"]) for p in r["class_plan"]] == [
        ("A", "C", "SL"), ("C", "E", "3A")]
    assert [(s["from_code"], s["to_code"]) for s in r["stretches"]["SL"]] == [("A", "C")]
    assert [(s["from_code"], s["to_code"]) for s in r["stretches"]["3A"]] == [("C", "E")]
    assert r["stretches"]["2A"] == []


def test_check_seats_single_class_and_bad_input(toolbox):
    r = tools.check_seats("T", LATER, from_station="A", to_station="B")
    assert r["success"] and set(r["seats"]) == set(av.CLASSES) and r["class_plan"] == []
    assert not tools.check_seats("T", LATER, from_station="E", to_station="A")["success"]
    assert not tools.check_seats("NOPE", LATER)["success"]


def test_search_reports_the_class_plan_with_dates(toolbox):
    force_split_shape()
    r = tools.search_trains("A", "E", LATER)
    assert r["success"]
    leg = r["journeys"][0]["legs"][0]
    assert leg["needs_class_change"] and leg["seats"] == {}
    assert [(p["travel_class"], p["date"]) for p in leg["class_plan"]] == [
        ("SL", LATER), ("3A", LATER)]


def test_book_a_class_change_on_one_train(toolbox):
    force_split_shape()
    legs = [
        {"train": "T", "from_code": "A", "to_code": "C", "date": LATER, "travel_class": "SL"},
        {"train": "T", "from_code": "C", "to_code": "E", "date": LATER, "travel_class": "3A"},
    ]
    r = tools.book_ticket(legs, passenger_name="Asha", confirm=YES)
    assert r["success"], r["reason"]
    assert r["booking"]["travel_class"] == "SL+3A"
    assert tools.list_bookings()["reason"] == "1 active booking(s)."


def test_book_refuses_a_class_that_is_sold_out_on_a_stretch(toolbox):
    force_split_shape()
    legs = [{"train": "T", "from_code": "A", "to_code": "E", "date": LATER}]
    r = tools.book_ticket(legs, "SL", "Asha", confirm=YES)       # SL is gone from C
    assert not r["success"] and "no SL seats" in r["reason"]


def test_book_declined_books_nothing(toolbox):
    legs = [{"train": "T", "from_code": "A", "to_code": "B", "date": LATER}]
    r = tools.book_ticket(legs, "SL", "Asha", confirm=lambda p: False)
    assert not r["success"]
    assert tools.list_bookings()["reason"] == "0 active booking(s)."


def test_bookings_take_seats_only_on_their_own_stretch(toolbox, monkeypatch):
    monkeypatch.setattr(av, "CAPACITY", {"SL": 1, "3A": 1, "2A": 1})   # one seat each
    ab = [{"train": "T", "from_code": "A", "to_code": "C", "date": LATER}]
    ce = [{"train": "T", "from_code": "C", "to_code": "E", "date": LATER}]
    first = tools.book_ticket(ab, "SL", "Asha", confirm=YES)
    assert first["success"]
    assert not tools.book_ticket(ab, "SL", "Ravi", confirm=YES)["success"]   # same stretch, same class
    assert tools.book_ticket(ab, "3A", "Ravi", confirm=YES)["success"]       # other class is fine
    assert tools.book_ticket(ce, "SL", "Ravi", confirm=YES)["success"]       # same class, other stretch
    tools.cancel_ticket(first["booking"]["pnr"], confirm=YES)
    assert tools.book_ticket(ab, "SL", "Ravi", confirm=YES)["success"]       # seat released


def test_legs_must_connect(toolbox):
    legs = [
        {"train": "T", "from_code": "A", "to_code": "B", "date": LATER},
        {"train": "T", "from_code": "C", "to_code": "E", "date": LATER},
    ]
    r = tools.book_ticket(legs, "SL", "Asha", confirm=YES)
    assert not r["success"] and "connect" in r["reason"]
