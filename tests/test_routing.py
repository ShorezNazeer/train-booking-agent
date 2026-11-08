import sqlite3
from datetime import date

import pytest

import availability as av
from data_source import Timetable
from options import plan_options, remove_dominated
from routing import Journey, Leg, find_direct, find_routes, plan_trip

D = date(2026, 11, 13)


def hm(h, m=0):
    return h * 60 + m


def make_timetable(tmp_path, trains):
    """trains: {number: [(station, arrival, departure), ...]} with None for
    the missing arrival at the first stop and the missing departure at the last."""
    db = tmp_path / "fake.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE stations (code TEXT, name TEXT, state TEXT, zone TEXT)")
    conn.execute("CREATE TABLE trains (number TEXT, name TEXT, type TEXT, from_code TEXT, to_code TEXT)")
    conn.execute(
        "CREATE TABLE stops (train_number TEXT, seq INTEGER, station_code TEXT, "
        "day INTEGER, arr_abs INTEGER, dep_abs INTEGER)"
    )
    codes = set()
    for number, stops in trains.items():
        conn.execute(
            "INSERT INTO trains VALUES (?,?,?,?,?)",
            (number, f"Train {number}", None, stops[0][0], stops[-1][0]),
        )
        for seq, (station, arr, dep) in enumerate(stops):
            codes.add(station)
            first_time = dep if dep is not None else arr
            conn.execute(
                "INSERT INTO stops VALUES (?,?,?,?,?,?)",
                (number, seq, station, first_time // 1440 + 1, arr, dep),
            )
    for code in codes:
        conn.execute("INSERT INTO stations VALUES (?,?,?,?)", (code, f"Station {code}", None, None))
    conn.commit()
    conn.close()
    return Timetable(db)


@pytest.fixture(autouse=True)
def plenty_of_seats(monkeypatch):
    """Every train always has seats unless a test forces it sold out."""
    monkeypatch.setattr(av, "SOLD_OUT_CHANCE", 0.0)
    monkeypatch.setattr(av, "FORCED_SOLD_OUT", set())


# ---------- direct search ----------
def test_direct_train_found(tmp_path):
    tt = make_timetable(tmp_path, {"T1": [("A", None, hm(8)), ("B", hm(10), None)]})
    legs = find_direct(tt, ["A"], ["B"], D)
    assert len(legs) == 1
    assert (legs[0].depart, legs[0].arrive) == (hm(8), hm(10))


def test_direct_wrong_direction_not_found(tmp_path):
    tt = make_timetable(tmp_path, {"T1": [("A", None, hm(8)), ("B", hm(10), None)]})
    assert find_direct(tt, ["B"], ["A"], D) == []


def test_direct_sold_out_is_skipped(tmp_path):
    tt = make_timetable(tmp_path, {"T1": [("A", None, hm(8)), ("B", hm(10), None)]})
    av.FORCED_SOLD_OUT.add(("T1", None))
    assert find_direct(tt, ["A"], ["B"], D) == []


def test_direct_respects_earliest(tmp_path):
    tt = make_timetable(tmp_path, {"T1": [("A", None, hm(8)), ("B", hm(10), None)]})
    assert find_direct(tt, ["A"], ["B"], D, earliest=hm(9)) == []


def test_overnight_train_duration(tmp_path):
    tt = make_timetable(tmp_path, {"N": [("S", None, hm(23)), ("T", 1440 + hm(1), None)]})
    leg = find_direct(tt, ["S"], ["T"], D)[0]
    assert leg.duration == 120
    assert leg.arrive == hm(23) + 120


# ---------- multi-leg search ----------
TWO_TRAINS = {
    "T2": [("A", None, hm(8)), ("C", hm(10), None)],
    "T3": [("C", None, hm(11)), ("D", hm(13), None)],
}


def test_two_train_route(tmp_path):
    tt = make_timetable(tmp_path, TWO_TRAINS)
    journeys = find_routes(tt, ["A"], ["D"], D)
    assert len(journeys) == 1
    j = journeys[0]
    assert [leg.train for leg in j.legs] == ["T2", "T3"]
    assert j.transfers == 1
    assert j.arrive == hm(13)
    assert j.layovers() == [60]


def test_connection_with_short_layover_is_rejected(tmp_path):
    trains = {
        "T5": [("P", None, hm(8)), ("Q", hm(10), None)],
        "T6": [("Q", None, hm(10, 30)), ("R", hm(12), None)],
    }
    tt = make_timetable(tmp_path, trains)
    assert find_routes(tt, ["P"], ["R"], D) == []                  # 30 min < 60 min
    assert len(find_routes(tt, ["P"], ["R"], D, min_layover=20)) == 1


def test_connection_into_next_day(tmp_path):
    trains = {
        "T5": [("P", None, hm(21)), ("Q", hm(23), None)],
        "T7": [("Q", None, hm(6)), ("R", hm(8), None)],
    }
    tt = make_timetable(tmp_path, trains)
    j = find_routes(tt, ["P"], ["R"], D)[0]
    assert j.legs[1].depart == 1440 + hm(6)
    assert j.arrive == 1440 + hm(8)
    assert j.layovers() == [hm(7)]


def test_no_route(tmp_path):
    trains = {
        "T1": [("A", None, hm(8)), ("B", hm(10), None)],
        "T9": [("X", None, hm(8)), ("Y", hm(10), None)],
    }
    tt = make_timetable(tmp_path, trains)
    assert find_routes(tt, ["A"], ["Y"], D) == []


# ---------- planning ----------
def test_plan_trip_prefers_direct_then_reroutes(tmp_path):
    trains = dict(TWO_TRAINS)
    trains["DX"] = [("A", None, hm(8)), ("D", hm(14), None)]
    tt = make_timetable(tmp_path, trains)

    journeys = plan_trip(tt, "A", "D", D)
    assert [len(j.legs) for j in journeys] == [1]

    av.FORCED_SOLD_OUT.add(("DX", None))
    journeys = plan_trip(tt, "A", "D", D)
    assert [leg.train for leg in journeys[0].legs] == ["T2", "T3"]


def test_unknown_station_raises(tmp_path):
    tt = make_timetable(tmp_path, TWO_TRAINS)
    with pytest.raises(ValueError):
        plan_trip(tt, "nowhere", "D", D)


# ---------- ranking ----------
def _leg(dep, arr):
    return Leg("X", "X", "A", "B", dep, arr, {"SL": 1})


def test_dominated_journey_is_removed():
    good = Journey((_leg(hm(8), hm(10)),))
    worse = Journey((_leg(hm(8), hm(12)),))
    assert remove_dominated([good, worse]) == [good]


def test_trade_offs_are_kept():
    early = Journey((_leg(hm(8), hm(10)),))
    later = Journey((_leg(hm(9), hm(11)),))
    assert len(remove_dominated([early, later])) == 2


def test_plan_options_ranking(tmp_path):
    trains = dict(TWO_TRAINS)                       # A 08:00 -> D 13:00 via C
    trains["T9"] = [("A", None, hm(8, 30)), ("E", hm(9, 30), None)]
    trains["T10"] = [("E", None, hm(10, 45)), ("D", hm(15, 45), None)]   # slower
    tt = make_timetable(tmp_path, trains)
    options = plan_options(tt, "A", "D", D, sort="fastest")
    assert len(options) == 2
    assert options[0].legs[1].train == "T3"
    assert options[0].duration < options[1].duration
