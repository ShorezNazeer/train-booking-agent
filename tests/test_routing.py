import sqlite3
from datetime import date

import pytest

import availability as av
import segments as seg
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


def test_class_locked_seat_hopping_and_fare(tmp_path, monkeypatch):
    """Test that preferred_classes locks multi-stretch journeys to allowed classes and calculates fares."""
    # Train T_SPLIT stops at A (seq 0), B (seq 1), C (seq 2)
    trains = {
        "T_SPLIT": [("A", None, hm(8)), ("B", hm(10), hm(10, 10)), ("C", hm(12), None)],
    }
    tt = make_timetable(tmp_path, trains)

    # Mock segment_seats:
    # 2A is available on A->B (seq 0), 0 on B->C (seq 1)
    # 3A is 0 on A->B (seq 0), available on B->C (seq 1)
    # SL is available on entire trip A->B->C
    def fake_segment_seats(train, run_day, cls, seq):
        if cls == "2A" and seq == 0:
            return 3
        if cls == "3A" and seq == 1:
            return 4
        if cls == "SL":
            return 10
        return 0

    monkeypatch.setattr(seg, "segment_seats", fake_segment_seats)

    # 1. Unfiltered search: SL covers the whole trip
    direct_all = find_direct(tt, ["A"], ["C"], D)
    assert len(direct_all) == 1
    assert "SL" in direct_all[0].seats
    assert direct_all[0].fare_inr > 0

    # 2. Filtered search with preferred_classes=('2A', '3A') (AC Only):
    # SL must be excluded, forcing an AC seat-hop between 2A and 3A
    direct_ac = find_direct(tt, ["A"], ["C"], D, preferred_classes=("2A", "3A"))
    assert len(direct_ac) == 1
    leg = direct_ac[0]
    assert len(leg.split) == 2
    assert leg.split[0].travel_class == "2A"
    assert leg.split[0].from_station == "A" and leg.split[0].to_station == "B"
    assert leg.split[0].fare_inr > 0

    assert leg.split[1].travel_class == "3A"
    assert leg.split[1].from_station == "B" and leg.split[1].to_station == "C"
    assert leg.split[1].fare_inr > 0

    # Journey total fare must equal sum of the two split pieces
    journeys = plan_trip(tt, "A", "C", D, preferred_classes=("2A", "3A"))
    assert len(journeys) == 1
    j = journeys[0]
    expected_fare = leg.split[0].fare_inr + leg.split[1].fare_inr
    assert j.total_fare_inr == expected_fare
    assert len(j.fare_breakdown) == 2

    # 3. Filtered search with preferred_classes=('3A',):
    # Since 3A is 0 on A->B, no journey should be found
    direct_3a_only = find_direct(tt, ["A"], ["C"], D, preferred_classes=("3A",))
    assert len(direct_3a_only) == 0

