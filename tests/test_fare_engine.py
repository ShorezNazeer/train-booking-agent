import pytest
import fare_engine
from routing import Leg, Piece


def test_haversine_distance():
    # NDLS to CNB
    d = fare_engine.haversine_distance(28.6423, 77.2200, 26.4542, 80.3510)
    assert 380 < d < 420


def test_station_distance_km():
    # Known primary stations
    km = fare_engine.station_distance_km("NDLS", "CNB")
    assert 440 < km < 500

    # Minimum distance enforced
    assert fare_engine.station_distance_km("NDLS", "NDLS") >= 15

    # Fallback distance for unknown codes
    assert fare_engine.station_distance_km("XYZABC", "UNKNOWN") == 120


def test_base_fare_telescopic_and_hierarchy():
    dist = 500
    fare_1a = fare_engine.calculate_base_fare(dist, "1A")
    fare_2a = fare_engine.calculate_base_fare(dist, "2A")
    fare_3a = fare_engine.calculate_base_fare(dist, "3A")
    fare_sl = fare_engine.calculate_base_fare(dist, "SL")
    fare_2s = fare_engine.calculate_base_fare(dist, "2S")

    # Strict price hierarchy
    assert fare_1a > fare_2a > fare_3a > fare_sl > fare_2s

    # Minimum base fare enforcement on short distances
    short_dist = 5
    min_3a = fare_engine.MIN_BASE_FARE["3A"]
    assert fare_engine.calculate_base_fare(short_dist, "3A") == min_3a


def test_calculate_stretch_fare_gst_and_surcharges():
    # AC Class (3A): GST must be 5%
    fare_3a = fare_engine.calculate_stretch_fare("NDLS", "CNB", "3A", is_superfast=True)
    assert fare_3a["reservation_charge"] == 40
    assert fare_3a["superfast_charge"] == 45
    assert fare_3a["gst"] > 0
    # Must be rounded to nearest multiple of 5
    assert fare_3a["total_fare"] % 5 == 0

    # Non-AC Class (SL): GST must be 0%
    fare_sl = fare_engine.calculate_stretch_fare("NDLS", "CNB", "SL", is_superfast=True)
    assert fare_sl["reservation_charge"] == 20
    assert fare_sl["superfast_charge"] == 30
    assert fare_sl["gst"] == 0.0
    assert fare_sl["total_fare"] % 5 == 0

    # Non-superfast passenger
    fare_sl_slow = fare_engine.calculate_stretch_fare("NDLS", "CNB", "SL", is_superfast=False)
    assert fare_sl_slow["superfast_charge"] == 0
    assert fare_sl_slow["total_fare"] <= fare_sl["total_fare"]


def test_calculate_journey_fare_split_and_direct():
    # Split journey (two stretches in same train)
    p1 = Piece(
        from_station="LKO",
        to_station="CNB",
        travel_class="SL",
        seats=10,
        depart=360,
        arrive=430,
        fare_inr=150,
        fare_breakdown={"distance_km": 75, "total_fare": 150, "travel_class": "SL"},
    )
    p2 = Piece(
        from_station="CNB",
        to_station="NDLS",
        travel_class="3A",
        seats=5,
        depart=440,
        arrive=800,
        fare_inr=750,
        fare_breakdown={"distance_km": 440, "total_fare": 750, "travel_class": "3A"},
    )
    leg = Leg(
        train="12003",
        name="Shatabdi Express",
        from_station="LKO",
        to_station="NDLS",
        depart=360,
        arrive=800,
        seats={},
        split=(p1, p2),
    )
    total_fare, breakdowns = fare_engine.calculate_journey_fare((leg,))
    assert total_fare == 900
    assert len(breakdowns) == 2
    assert breakdowns[0]["travel_class"] == "SL"
    assert breakdowns[1]["travel_class"] == "3A"
