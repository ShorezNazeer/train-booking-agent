import datetime
import pytest
from fastapi.testclient import TestClient

from api import app
from data_source import Timetable, train_runs_on_date
from import_real_data import infer_classes
from routing import find_direct, plan_trip
import segments as seg
import train_tools as tools


def test_universal_class_inference_patterns():
    # 1. RAJDHANI -> ['1A', '2A', '3A']
    assert infer_classes("DBRT RAJDHANI", "RAJ") == ["1A", "2A", "3A"]
    assert infer_classes("HWH RAJDHANI") == ["1A", "2A", "3A"]
    assert infer_classes("MUMBAI RAJDHANI EXPRESS") == ["1A", "2A", "3A"]

    # 2. VANDE BHARAT, TEJAS, GATIMAAN, DOUBLE DECKER, SHATABDI (excluding JAN SHATABDI) -> ['CC', 'EC']
    assert infer_classes("VANDE BHARAT EX", "VNDB") == ["CC", "EC"]
    assert infer_classes("TEJAS EXPRESS", "TEJ") == ["CC", "EC"]
    assert infer_classes("GATIMAAN EXPRESS", "GT") == ["CC", "EC"]
    assert infer_classes("LJN DOUBLE DKR", "DD") == ["CC", "EC"]
    assert infer_classes("JAIPUR DOUBLE DECKER") == ["CC", "EC"]
    assert infer_classes("SHATABDI EXPRESS", "SHT") == ["CC", "EC"]

    # 3. JAN SHATABDI -> ['2S', 'CC'] (must not be treated as Shatabdi)
    assert infer_classes("NDLS JAN SHATABDI", "JSH") == ["2S", "CC"]
    assert infer_classes("DEHRADUN JANSHATABDI") == ["2S", "CC"]

    # 4. GARIB RATH -> ['3A']
    assert infer_classes("RNC GARIB RATH", "GBR") == ["3A"]
    assert infer_classes("GARIBRATH EXP") == ["3A"]

    # 5. HUMSAFAR (except CHAMPARAN) -> ['3A']
    assert infer_classes("ANVT HUMSAFAR", "HUM") == ["3A"]
    assert infer_classes("PRAYAGRAJ HMSFR EXP") == ["3A"]
    # Champaran Humsafar exception -> standard express classes
    assert infer_classes("CHAMPARAN HUMSAFAR") == ["2A", "3A", "3E", "SL", "2S"]

    # 6. Any train containing ' AC ' or starting/ending with 'AC EXP' / 'AC SF' -> ['1A', '2A', '3A']
    assert infer_classes("ARUNACHAL AC SF") == ["1A", "2A", "3A"]
    assert infer_classes("AC EXPRESS") == ["1A", "2A", "3A"]
    assert infer_classes("LTT AC SF EXP") == ["1A", "2A", "3A"]
    assert infer_classes("DELHI AC SPECIAL") == ["1A", "2A", "3A"]

    # 7. AMRIT BHARAT, JANSADHARAN, ANTYODAYA -> ['SL', '2S']
    assert infer_classes("AMRIT BHARAT EXP", "AMTB") == ["SL", "2S"]
    assert infer_classes("JANSADHARAN EXP") == ["SL", "2S"]
    assert infer_classes("ANTYODAYA EXPRESS", "ANT") == ["SL", "2S"]

    # 8. MEMU, DEMU, EMU, PASSENGER, LOCAL -> ['2S']
    assert infer_classes("GZB-NDLS MEMU", "MEMU") == ["2S"]
    assert infer_classes("DELHI DEMU LOCAL", "DEMU") == ["2S"]
    assert infer_classes("SUBURBAN LOCAL EMU", "EMU") == ["2S"]
    assert infer_classes("BAREILLY PASSENGER", "PAS") == ["2S"]

    # 9. Standard Mail/Express/Superfast -> ['2A', '3A', '3E', 'SL', '2S']
    assert infer_classes("LUCKNOW MAIL") == ["2A", "3A", "3E", "SL", "2S"]
    assert infer_classes("GORAKHDHAM SUPERFAST EXP") == ["2A", "3A", "3E", "SL", "2S"]
    assert infer_classes("KASHI EXPRESS") == ["2A", "3A", "3E", "SL", "2S"]


def test_weekday_filtering_alternate_day_trains():
    """Verify alternate-day trains 20503 and 20505 only operate on their running days.

    20503 runs Tue, Wed, Thu, Sat, Sun.
    20505 runs Mon, Fri.
    """
    tt = Timetable()
    assert tt.train_runs_days("20503") == "Tue,Wed,Thu,Sat,Sun"
    assert tt.train_runs_days("20505") == "Mon,Fri"

    mon = datetime.date(2026, 11, 23)  # Monday
    tue = datetime.date(2026, 11, 24)  # Tuesday

    assert train_runs_on_date("Mon,Fri", mon) is True
    assert train_runs_on_date("Mon,Fri", tue) is False
    assert train_runs_on_date("Tue,Wed,Thu,Sat,Sun", mon) is False
    assert train_runs_on_date("Tue,Wed,Thu,Sat,Sun", tue) is True

    # Search direct routes between Dibrugarh (DBRG) and New Delhi (NDLS)
    mon_routes = find_direct(tt, ["DBRG"], ["NDLS"], mon)
    mon_trains = [l.train for l in mon_routes]
    assert "20505" in mon_trains, "20505 must run on Monday"
    assert "20503" not in mon_trains, "20503 must not run on Monday"

    tue_routes = find_direct(tt, ["DBRG"], ["NDLS"], tue)
    tue_trains = [l.train for l in tue_routes]
    assert "20503" in tue_trains, "20503 must run on Tuesday"
    assert "20505" not in tue_trains, "20505 must not run on Tuesday"


def test_sl_only_filter_never_returns_all_ac_trains():
    """Ensure searching with preferred_classes=['SL'] never returns all-AC trains."""
    tt = Timetable()
    d = datetime.date(2026, 11, 23)

    # Route DBRG -> NDLS contains Rajdhani trains (20505, 12423) which are 1A,2A,3A only
    sl_routes = find_direct(tt, ["DBRG"], ["NDLS"], d, preferred_classes=["SL"])
    for leg in sl_routes:
        t_classes = tt.train_classes(leg.train)
        assert "SL" in t_classes, f"Train {leg.train} without SL class was returned for SL-only search"

    # Route NDLS -> CNB has Vande Bharat (CC, EC), Rajdhani (1A, 2A, 3A), Garib Rath (3A)
    sl_ndls_cnb = find_direct(tt, ["NDLS"], ["CNB"], d, preferred_classes=["SL"])
    for leg in sl_ndls_cnb:
        t_classes = tt.train_classes(leg.train)
        assert "SL" in t_classes, f"Train {leg.train} ({leg.name}) without SL returned for SL search"
        assert leg.train not in ("22436", "12424", "12878", "12004")


@pytest.fixture
def api_client():
    client = TestClient(app)
    yield client
    tools.reset_demo()


def test_demo_direct_preset(api_client):
    demo = api_client.post("/api/demo/direct").json()
    assert demo["success"] is True
    assert demo["type"] == "direct"
    assert demo["train_number"]

    # Verify search returns non-empty journeys
    res = api_client.get(
        "/api/search",
        params={
            "from_station": demo["from_station"],
            "to_station": demo["to_station"],
            "travel_date": demo["travel_date"],
        },
    ).json()
    assert res["success"] is True
    journeys = res.get("journeys", [])
    assert len(journeys) > 0
    # Direct demo should have a 0-transfer journey
    direct_journeys = [j for j in journeys if j["transfers"] == 0]
    assert len(direct_journeys) > 0


def test_demo_split_preset(api_client):
    demo = api_client.post("/api/demo/split").json()
    assert demo["success"] is True
    assert demo["type"] == "split"
    assert demo["transfer_station_code"]
    assert demo["class_first_half"]
    assert demo["class_second_half"]

    res = api_client.get(
        "/api/search",
        params={
            "from_station": demo["from_station"],
            "to_station": demo["to_station"],
            "travel_date": demo["travel_date"],
        },
    ).json()
    assert res["success"] is True
    journeys = res.get("journeys", [])
    assert len(journeys) > 0

    # Ensure at least one journey has a leg with mid-route class change
    split_legs = [
        leg
        for j in journeys
        for leg in j["legs"]
        if leg.get("needs_class_change") and len(leg.get("class_plan", [])) >= 2
    ]
    assert len(split_legs) > 0, "Split demo scenario must yield a leg with mid-route class change"


def test_demo_soldout_preset(api_client):
    demo = api_client.post("/api/demo/soldout").json()
    assert demo["success"] is True
    assert demo["type"] == "soldout"

    res = api_client.get(
        "/api/search",
        params={
            "from_station": demo["from_station"],
            "to_station": demo["to_station"],
            "travel_date": demo["travel_date"],
        },
    ).json()
    assert res["success"] is True
    journeys = res.get("journeys", [])
    assert len(journeys) > 0

    # Soldout demo should force multi-leg transfers
    transfer_journeys = [j for j in journeys if j["transfers"] > 0]
    assert len(transfer_journeys) > 0, "Soldout demo must return multi-leg journeys"


def test_demo_reset_clears_overrides(api_client):
    # Set up split demo
    api_client.post("/api/demo/split")
    assert bool(seg.FORCED_SEGMENT_AVAILABLE) is True or bool(seg.FORCED_SEGMENT_SOLD_OUT) is True

    # Reset
    res = api_client.post("/api/demo/reset").json()
    assert res["success"] is True
    assert len(seg.FORCED_SEGMENT_AVAILABLE) == 0
    assert len(seg.FORCED_SEGMENT_SOLD_OUT) == 0
